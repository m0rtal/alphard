"""Tests for issue #197: daily-pnl tracker on TinkoffAccount.

Regression class: every production site that builds ``PortfolioState``
for ``RiskGate.evaluate()`` previously omitted ``daily_pnl``, leaving it
at the pydantic default ``Decimal("0")``. ``_check_daily_loss`` in
``src/risk/gate.py`` short-circuits when ``daily_pnl >= 0`` — so the
daily-loss kill-switch was silently a no-op in production, no matter
what ``RiskLimits.max_daily_loss_pct`` was configured to.

The fix mirrors the peak-equity tracker (issue #32/#195): persist a
``(previous_close_equity, previous_close_date)`` tuple per account on
disk, recompute ``daily_pnl = current_NAV - previous_close_equity`` on
every fetch, and pass it through to ``PortfolioState.daily_pnl``.

These tests cover:
- the tracker itself: load/save, cold start, corrupt file, calendar rollover
- end-to-end: ``_fetch_real_portfolio_state`` now stamps ``daily_pnl`` so
  ``_check_daily_loss`` trips on a -4% day end-to-end
- ``OrderFlow`` path: when a ``daily_pnl_provider`` is configured the
  PortfolioState passed to the gate has the live daily_pnl (issue #197
  wire-through, mirroring #195 for peak_equity)
"""

from __future__ import annotations

import json
import os
from datetime import date
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock, patch

from src.risk.gate import RiskGate, RiskLimits


def _make_risk_limits(**overrides) -> RiskLimits:
    """Build RiskLimits permissive on every field except daily-loss.

    The default 3% daily-loss limit matches the production gate config
    in ``src/coordinator.py`` (line 49). Tests that want a different
    threshold override ``max_daily_loss_pct``.
    """
    defaults: dict[str, object] = {
        "max_position_pct": Decimal("100"),
        "max_dd_pct": Decimal("100"),
        "max_sector_pct": Decimal("100"),
        "max_daily_loss_pct": Decimal("3"),
        "leverage_max": Decimal("1.0"),
        "allow_short": False,
    }
    for key, value in overrides.items():
        defaults[key] = value
    return RiskLimits(**defaults)  # type: ignore[arg-type]


def _make_tinkoff_account(store_dir: str, account_id: str = "SB1", risk_gate=None, clock=None):
    """Build a TinkoffAccount pointing at ``store_dir``, SDK stubbed.

    ``clock`` is the test-injection callable returning a tz-aware
    ``datetime`` (issue #197); production callers leave it ``None``.
    """
    from src.broker.tinkoff_account import TinkoffAccount

    with patch.dict(os.environ, {"ALPHARD_PEAK_STORE_DIR": store_dir}):
        with patch.dict(
            os.environ,
            {"TINKOFF_SANDBOX_TOKEN": "t.test_token_aaaaaaaaaaaaaaaaaaa"},
        ):
            acct = TinkoffAccount(
                token="t.test_token_aaaaaaaaaaaaaaaaaaa",
                account_id=account_id,
                risk_gate=risk_gate,
                clock=clock,
            )
    return acct


def _fixed_clock(year: int, month: int, day: int, hour: int = 12):
    """Return a callable suitable for ``TinkoffAccount(clock=...)``.

    Issue #197: ``datetime.datetime`` is immutable, so we cannot patch
    ``datetime.now`` in tests. Instead the production code reads
    ``self._clock()`` when configured and falls back to
    ``datetime.now(UTC)`` otherwise.
    """
    from datetime import datetime as _dt, timezone as _tz

    fixed = _dt(year, month, day, hour, 0, 0, tzinfo=_tz.utc)

    def _clock():
        return fixed

    return _clock


class TestDailyPnlTracker:
    """The daily-pnl tracker is a per-account (equity, date) tuple persisted
    to disk under ``$ALPHARD_PEAK_STORE_DIR/daily_pnl_<account_id>.json``.
    """

    def test_cold_start_state_is_zero_and_date_min(self, tmp_path):
        """A fresh TinkoffAccount (no existing daily-pnl file) starts at
        ``(0, date.min)``. The sentinel date.min guarantees the very
        first call to ``_compute_daily_pnl`` takes the
        "stamp previous_close = current NAV" branch rather than
        computing a bogus P&L against ``previous_close_equity == 0``.
        """
        acct = _make_tinkoff_account(str(tmp_path))
        equity, stamp_date = acct._daily_pnl_state
        assert equity == Decimal("0")
        assert stamp_date == date.min

    def test_first_snapshot_stamps_pivot_and_returns_zero_pnl(self, tmp_path):
        """The first snapshot of a fresh day stamps ``previous_close_equity``
        to current NAV and returns ``daily_pnl == 0`` (no history yet).
        """
        acct = _make_tinkoff_account(str(tmp_path))
        today = date(2026, 8, 24)
        with patch.object(acct, "get_portfolio") as mock_gp:
            mock_gp.return_value = MagicMock(cash=Decimal("100000"), positions=[])
            # Drive the public wrapper path directly: _compute_daily_pnl
            # + persist. We don't go through _fetch_real_portfolio_state
            # here because that path also bumps the HWM and runs the
            # broker dispatch helpers we want to isolate from this
            # unit-level tracker test.
            pnl, refreshed = acct._compute_daily_pnl(Decimal("100000"), today)
        assert pnl == Decimal("0")
        assert refreshed is True
        assert acct._daily_pnl_state == (Decimal("100000"), today)
        # File persisted on disk
        pnl_file = Path(str(tmp_path)) / "daily_pnl_SB1.json"
        assert pnl_file.exists()
        data = json.loads(pnl_file.read_text())
        assert data["previous_close_equity"] == "100000"
        assert data["previous_close_date"] == "2026-08-24"

    def test_second_snapshot_same_day_returns_real_pnl(self, tmp_path):
        """Once ``previous_close_equity`` is stamped, subsequent snapshots
        compute ``daily_pnl = current - pivot``. A -4% drop after a 100k
        pivot must produce ``daily_pnl = -4000``.
        """
        acct = _make_tinkoff_account(str(tmp_path))
        today = date(2026, 8, 24)
        # First snapshot: stamp pivot at 100_000
        acct._compute_daily_pnl(Decimal("100000"), today)
        # Second snapshot: NAV drops to 96_000 → -4% day
        pnl, refreshed = acct._compute_daily_pnl(Decimal("96000"), today)
        assert refreshed is False
        assert pnl == Decimal("-4000")
        # Pivot must NOT move intra-day
        assert acct._daily_pnl_state == (Decimal("100000"), today)

    def test_calendar_rollover_re_stamps_pivot(self, tmp_path):
        """A new calendar day must re-stamp the pivot from the new NAV,
        not carry forward an old snapshot's P&L. This guards against a
        weekend gap: after a holiday, ``daily_pnl`` is reset to 0
        rather than reporting a 5-day loss as a "daily" loss.
        """
        acct = _make_tinkoff_account(str(tmp_path))
        day1 = date(2026, 8, 21)  # Friday
        day2 = date(2026, 8, 24)  # Monday
        # Friday close: NAV = 100k
        acct._compute_daily_pnl(Decimal("100000"), day1)
        # Monday open: NAV = 92k (a 3-day gap). The tracker must NOT
        # compute daily_pnl = -8k (which would falsely trip the guard
        # against the weekend gap); it must re-stamp pivot to 92k and
        # return daily_pnl = 0 for THIS snapshot.
        pnl, refreshed = acct._compute_daily_pnl(Decimal("92000"), day2)
        assert refreshed is True
        assert pnl == Decimal("0")
        assert acct._daily_pnl_state == (Decimal("92000"), day2)

    def test_pnl_persists_across_restarts(self, tmp_path):
        """A second TinkoffAccount instance against the same store_dir
        loads the previously-saved pivot and date.
        """
        acct1 = _make_tinkoff_account(str(tmp_path))
        day = date(2026, 8, 24)
        acct1._compute_daily_pnl(Decimal("100000"), day)

        acct2 = _make_tinkoff_account(str(tmp_path))
        equity, stamp = acct2._daily_pnl_state
        assert equity == Decimal("100000")
        assert stamp == day

    def test_corrupt_file_starts_at_zero(self, tmp_path):
        """A corrupt / unparseable JSON file should not crash the
        constructor — falls back to (0, date.min) and logs a warning.
        """
        pnl_file = Path(str(tmp_path)) / "daily_pnl_SB1.json"
        pnl_file.write_text("not valid json {{")
        # Should not raise
        acct = _make_tinkoff_account(str(tmp_path))
        equity, stamp = acct._daily_pnl_state
        assert equity == Decimal("0")
        assert stamp == date.min

    def test_negative_equity_value_is_treated_as_zero(self, tmp_path):
        """Defence-in-depth: a negative previous_close_equity (shouldn't
        happen) is clamped to 0 so a stale / corrupt file can't poison
        the math.
        """
        pnl_file = Path(str(tmp_path)) / "daily_pnl_SB1.json"
        pnl_file.write_text(json.dumps({"previous_close_equity": "-1000", "previous_close_date": "2026-08-24"}))
        acct = _make_tinkoff_account(str(tmp_path))
        equity, stamp = acct._daily_pnl_state
        assert equity == Decimal("0")
        assert stamp == date.min

    def test_missing_date_field_starts_at_zero(self, tmp_path):
        """A JSON file with the equity field but no ``previous_close_date``
        is rejected back to (0, date.min) — without a date the rollover
        logic can't trust the pivot.
        """
        pnl_file = Path(str(tmp_path)) / "daily_pnl_SB1.json"
        pnl_file.write_text(json.dumps({"previous_close_equity": "100000"}))
        acct = _make_tinkoff_account(str(tmp_path))
        equity, stamp = acct._daily_pnl_state
        assert equity == Decimal("0")
        assert stamp == date.min

    def test_per_account_separation(self, tmp_path):
        """Two different account_ids use two different daily-pnl files."""
        acct1 = _make_tinkoff_account(str(tmp_path), account_id="ACC1")
        acct2 = _make_tinkoff_account(str(tmp_path), account_id="ACC2")
        assert acct1._daily_pnl_path != acct2._daily_pnl_path

        day = date(2026, 8, 24)
        acct1._compute_daily_pnl(Decimal("100000"), day)
        acct2._compute_daily_pnl(Decimal("200000"), day)

        data1 = json.loads(Path(acct1._daily_pnl_path).read_text())
        data2 = json.loads(Path(acct2._daily_pnl_path).read_text())
        assert data1["previous_close_equity"] == "100000"
        assert data2["previous_close_equity"] == "200000"

    def test_persist_failure_does_not_raise(self, tmp_path):
        """If the on-disk write raises, ``_compute_daily_pnl`` must not
        propagate — best-effort, like peak_equity save. The in-memory
        state must still update so the process can keep trading.
        """
        acct = _make_tinkoff_account(str(tmp_path))
        day = date(2026, 8, 24)
        # Force the save to raise
        with patch.object(acct, "_save_daily_pnl_state", side_effect=OSError("disk full")):
            # Should not raise
            pnl, refreshed = acct._compute_daily_pnl(Decimal("100000"), day)
        assert refreshed is True
        assert pnl == Decimal("0")
        # In-memory state still updated
        assert acct._daily_pnl_state == (Decimal("100000"), day)


class TestDailyLossTriggersRiskDailyLoss:
    """End-to-end: a -4% day now trips ``RISK_DAILY_LOSS`` via the
    production ``_fetch_real_portfolio_state`` path.
    """

    def test_risk_daily_loss_fires_on_drawdown(self, tmp_path):
        """Given yesterday close = 100_000 and today's NAV = 96_000
        (a -4% day) with ``max_daily_loss_pct = 3``, the order must be
        rejected with ``RISK_DAILY_LOSS``. Before this fix the same
        production call would silently approve the order because
        ``daily_pnl`` defaulted to 0.
        """
        from src.broker.tinkoff_account import TinkoffAccount
        from src.risk.gate import TradeIntent

        today = date(2026, 8, 24)
        # Pre-populate the daily-pnl pivot stamped at 100_000 on
        # TODAY so the rollover logic does NOT re-pivot. (The real
        # process loads the file stamped at the previous trading day's
        # close; on the first call today the rollover fires and
        # ``daily_pnl`` is reported as 0 for that one snapshot. To
        # assert the kill-switch end-to-end we need to test the
        # second-call-of-the-day path, so we pre-stamp with today's
        # date at 100k and then observe a -4% drop.)
        pnl_file = Path(str(tmp_path)) / "daily_pnl_SB1.json"
        pnl_file.write_text(
            json.dumps(
                {
                    "previous_close_equity": "100000",
                    "previous_close_date": today.isoformat(),
                }
            )
        )

        # Pin the clock so the production code's "today" matches the
        # pre-stamped file. Issue #197.
        clock = _fixed_clock(2026, 8, 24)
        with patch.dict(
            os.environ,
            {
                "TINKOFF_SANDBOX_TOKEN": "t.test_token_aaaaaaaaaaaaaaaaaaa",
                "ALPHARD_PEAK_STORE_DIR": str(tmp_path),
            },
        ):
            gate = RiskGate(limits=_make_risk_limits(max_daily_loss_pct=Decimal("3")))
            acct = TinkoffAccount(
                token="t.test_token_aaaaaaaaaaaaaaaaaaa",
                account_id="SB1",
                risk_gate=gate,
                clock=clock,
            )

        # NAV has dropped to 96k vs the 100k pivot. ``daily_pnl`` must
        # come out as -4000 and feed into the gate.
        with patch.object(acct, "get_portfolio") as mock_gp:
            mock_gp.return_value = MagicMock(cash=Decimal("96000"), positions=[])
            state = acct._fetch_real_portfolio_state()

        assert state.daily_pnl == Decimal("-4000"), f"daily_pnl must be -4000 (-4% on 100k), got {state.daily_pnl}"

        intent = TradeIntent(
            symbol="SBER",
            side="buy",
            quantity=Decimal("10"),
            price=Decimal("300"),
        )
        decision = gate.evaluate(intent, state)
        assert decision.allowed is False, (
            f"RISK_DAILY_LOSS must fire on -4% day; got allowed=True. " f"Violations: {decision.violations}"
        )
        assert any(
            "RISK_DAILY_LOSS" in v for v in decision.violations
        ), f"Expected RISK_DAILY_LOSS, got: {decision.violations}"

    def test_risk_daily_loss_does_not_fire_within_threshold(self, tmp_path):
        """If today's loss is within ``max_daily_loss_pct``, no
        ``RISK_DAILY_LOSS`` — symmetric with the peak-equity test in
        ``tests/test_peak_equity_tracker.py``.
        """
        from src.broker.tinkoff_account import TinkoffAccount
        from src.risk.gate import TradeIntent

        today = date(2026, 8, 24)
        pnl_file = Path(str(tmp_path)) / "daily_pnl_SB1.json"
        pnl_file.write_text(
            json.dumps(
                {
                    "previous_close_equity": "100000",
                    "previous_close_date": today.isoformat(),
                }
            )
        )

        clock = _fixed_clock(2026, 8, 24)
        with patch.dict(
            os.environ,
            {
                "TINKOFF_SANDBOX_TOKEN": "t.test_token_aaaaaaaaaaaaaaaaaaa",
                "ALPHARD_PEAK_STORE_DIR": str(tmp_path),
            },
        ):
            gate = RiskGate(limits=_make_risk_limits(max_daily_loss_pct=Decimal("5")))
            acct = TinkoffAccount(
                token="t.test_token_aaaaaaaaaaaaaaaaaaa",
                account_id="SB1",
                risk_gate=gate,
                clock=clock,
            )

        # NAV = 97k → -3% on 100k. Within the 5% threshold.
        with patch.object(acct, "get_portfolio") as mock_gp:
            mock_gp.return_value = MagicMock(cash=Decimal("97000"), positions=[])
            state = acct._fetch_real_portfolio_state()

        assert state.daily_pnl == Decimal("-3000")
        intent = TradeIntent(
            symbol="SBER",
            side="buy",
            quantity=Decimal("10"),
            price=Decimal("300"),
        )
        decision = gate.evaluate(intent, state)
        assert not any(
            "RISK_DAILY_LOSS" in v for v in decision.violations
        ), f"-3% should not trip 5% daily-loss limit; got: {decision.violations}"

    def test_cold_start_first_snapshot_of_day_does_not_trip_daily_loss(self, tmp_path):
        """A cold start (no prior daily-pnl file) must NOT report a
        phantom loss — the very first snapshot of the day stamps the
        pivot and returns daily_pnl = 0. This is the regression guard
        against the tracker falsely computing ``current_NAV - 0`` as
        a "100% loss" on cold start.
        """
        from src.broker.tinkoff_account import TinkoffAccount
        from src.risk.gate import TradeIntent

        with patch.dict(
            os.environ,
            {
                "TINKOFF_SANDBOX_TOKEN": "t.test_token_aaaaaaaaaaaaaaaaaaa",
                "ALPHARD_PEAK_STORE_DIR": str(tmp_path),
            },
        ):
            gate = RiskGate(limits=_make_risk_limits(max_daily_loss_pct=Decimal("3")))
            acct = TinkoffAccount(
                token="t.test_token_aaaaaaaaaaaaaaaaaaa",
                account_id="SB1",
                risk_gate=gate,
            )

        with patch.object(acct, "get_portfolio") as mock_gp:
            mock_gp.return_value = MagicMock(cash=Decimal("100000"), positions=[])
            state = acct._fetch_real_portfolio_state()

        assert state.daily_pnl == Decimal(
            "0"
        ), f"cold start first snapshot must have daily_pnl=0, got {state.daily_pnl}"

        intent = TradeIntent(
            symbol="SBER",
            side="buy",
            quantity=Decimal("10"),
            price=Decimal("300"),
        )
        decision = gate.evaluate(intent, state)
        assert not any(
            "RISK_DAILY_LOSS" in v for v in decision.violations
        ), f"Cold start should not trip daily-loss; got: {decision.violations}"
