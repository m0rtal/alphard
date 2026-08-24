"""Tests for issue #197: daily-P&L basis tracker.

These tests cover the regression where ``_fetch_real_portfolio_state``
constructed ``PortfolioState(..., daily_pnl=...)`` was never invoked —
the field defaulted to ``Decimal("0")`` because every production
``PortfolioState`` constructor omitted it. The downstream effect:
``RiskGate._check_daily_loss`` short-circuits when ``daily_pnl >= 0``,
so the ``RISK_DAILY_LOSS`` kill-switch never tripped in production,
even after a -20% day.

The fix mirrors the ``peak_equity`` HWM pattern (issue #32): persist a
sibling ``daily_pnl_basis_{account_id}.json`` file storing
``previous_close_equity`` and ``last_trading_day`` (MSK); on each
snapshot, compute ``daily_pnl = current_nav − previous_close_equity``
and roll over when the trading day changes.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.risk.gate import RiskGate, RiskLimits, TradeIntent


def _make_risk_limits(**overrides) -> RiskLimits:
    """Build a RiskLimits with day-loss focus; other limits permissive."""
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


def _make_tinkoff_account_with_daily_dir(daily_dir: str, account_id: str = "SB1", risk_gate=None):
    """Build a TinkoffAccount pointing at ``daily_dir``; stub the SDK."""
    from src.broker.tinkoff_account import TinkoffAccount

    with patch.dict(os.environ, {"ALPHARD_PEAK_STORE_DIR": daily_dir}):
        with patch.dict(
            os.environ,
            {"TINKOFF_SANDBOX_TOKEN": "t.test_token_aaaaaaaaaaaaaaaaaaa"},
        ):
            acct = TinkoffAccount(
                token="t.test_token_aaaaaaaaaaaaaaaaaaa",
                account_id=account_id,
                risk_gate=risk_gate,
            )
    return acct


class TestDailyPnlBasisTracker:
    """The daily-P&L basis is a per-account (previous_close, last_day) pair
    persisted to disk. Cold start → daily_pnl == 0."""

    def test_cold_start_basis_is_zero(self, tmp_path):
        """A fresh TinkoffAccount (no basis file) starts at (0, None)."""
        acct = _make_tinkoff_account_with_daily_dir(str(tmp_path))
        assert acct._previous_close_equity == Decimal("0")
        assert acct._last_trading_day is None

    def test_first_snapshot_seeds_basis_with_current_nav(self, tmp_path):
        """The first snapshot of the day stamps previous_close = current."""
        acct = _make_tinkoff_account_with_daily_dir(str(tmp_path))
        with patch.object(acct, "get_portfolio") as mock_gp:
            mock_snap = MagicMock()
            mock_snap.cash = Decimal("100000")
            mock_snap.positions = []
            mock_gp.return_value = mock_snap
            state = acct._fetch_real_portfolio_state()

        # Issue #197: first call of the day reports daily_pnl == 0
        # (no intraday delta yet — current = previous_close).
        assert state.daily_pnl == Decimal("0")
        assert acct._previous_close_equity == Decimal("100000")
        assert acct._last_trading_day is not None

        basis_file = Path(str(tmp_path)) / "daily_pnl_basis_SB1.json"
        assert basis_file.exists()
        data = json.loads(basis_file.read_text())
        assert data["previous_close_equity"] == "100000"
        assert data["last_trading_day"]  # ISO string, non-empty

    def test_same_day_intraday_loss_is_reported(self, tmp_path):
        """Within the same trading day, daily_pnl = current_nav − basis."""
        from src.broker.tinkoff_account import _msk_today

        acct = _make_tinkoff_account_with_daily_dir(str(tmp_path))
        # Snapshot 1: NAV 1_000_000, basis stamped at 1_000_000, daily_pnl=0.
        with patch.object(acct, "get_portfolio") as mock_gp:
            mock_gp.return_value = MagicMock(cash=Decimal("1000000"), positions=[])
            state1 = acct._fetch_real_portfolio_state()
        assert state1.daily_pnl == Decimal("0")
        assert acct._previous_close_equity == Decimal("1000000")
        today = _msk_today()
        assert acct._last_trading_day == today

        # Snapshot 2: NAV drops to 960_000 — same day, daily_pnl = -40_000.
        with patch.object(acct, "get_portfolio") as mock_gp:
            mock_gp.return_value = MagicMock(cash=Decimal("960000"), positions=[])
            state2 = acct._fetch_real_portfolio_state()
        assert state2.daily_pnl == Decimal("-40000"), f"expected -40000 intraday loss, got {state2.daily_pnl}"
        assert acct._previous_close_equity == Decimal("1000000"), "basis must NOT change intraday; only on rollover"
        assert acct._last_trading_day == today, "basis day must NOT roll within a day"

    def test_same_day_intraday_profit_is_reported(self, tmp_path):
        """Positive intraday deltas must propagate as positive daily_pnl."""
        acct = _make_tinkoff_account_with_daily_dir(str(tmp_path))
        with patch.object(acct, "get_portfolio") as mock_gp:
            mock_gp.return_value = MagicMock(cash=Decimal("1000000"), positions=[])
            acct._fetch_real_portfolio_state()
        with patch.object(acct, "get_portfolio") as mock_gp:
            mock_gp.return_value = MagicMock(cash=Decimal("1030000"), positions=[])
            state = acct._fetch_real_portfolio_state()
        assert state.daily_pnl == Decimal("30000"), f"expected +30000, got {state.daily_pnl}"

    def test_day_rollover_resets_basis(self, tmp_path):
        """A new MSK trading day must stamp a fresh basis at current NAV.

        Issue #207: the rollover only fires when the persisted basis is
        trusted (schema_version=1, basis_valid=True). The test seeds a
        v1-trusted basis on yesterday so the rollover path is exercised
        legitimately — not the legacy/corrupt payload, which would now
        fail-closed (covered by ``test_untrusted_basis_blocks_rollover``).
        """
        from src.broker.tinkoff_account import _msk_today

        # Pre-populate with yesterday's basis in the v1 trusted schema.
        yesterday = (_msk_today() - timedelta(days=1)).isoformat()
        basis_file = Path(str(tmp_path)) / "daily_pnl_basis_SB1.json"
        basis_file.write_text(
            json.dumps(
                {
                    "previous_close_equity": "1000000",
                    "last_trading_day": yesterday,
                    "schema_version": 1,
                    "basis_valid": True,
                }
            )
        )

        acct = _make_tinkoff_account_with_daily_dir(str(tmp_path))
        assert acct._previous_close_equity == Decimal("1000000")
        assert acct._last_trading_day == date.fromisoformat(yesterday)
        assert acct._basis_trusted is True, (
            "v1 + basis_valid=True must load as trusted so the rollover " "path is exercised; see issue #207"
        )

        # First call today — rollover: stamp basis at 950k, daily_pnl = 0.
        with patch.object(acct, "get_portfolio") as mock_gp:
            mock_gp.return_value = MagicMock(cash=Decimal("950000"), positions=[])
            state = acct._fetch_real_portfolio_state()
        assert state.daily_pnl == Decimal("0"), f"day rollover must report daily_pnl=0, got {state.daily_pnl}"
        assert acct._previous_close_equity == Decimal("950000")
        assert acct._last_trading_day == _msk_today()
        assert acct._basis_trusted is True

        # Same day, NAV drops 4% of 950k → -38k loss trips the gate.
        with patch.object(acct, "get_portfolio") as mock_gp:
            mock_gp.return_value = MagicMock(cash=Decimal("912000"), positions=[])
            state2 = acct._fetch_real_portfolio_state()
        assert state2.daily_pnl == Decimal("-38000"), f"expected -38000 (-4% of 950k), got {state2.daily_pnl}"

    def test_basis_persists_across_restarts(self, tmp_path):
        """A second TinkoffAccount against the same dir loads persisted basis."""
        acct1 = _make_tinkoff_account_with_daily_dir(str(tmp_path))
        with patch.object(acct1, "get_portfolio") as mock_gp:
            mock_gp.return_value = MagicMock(cash=Decimal("1000000"), positions=[])
            acct1._fetch_real_portfolio_state()

        acct2 = _make_tinkoff_account_with_daily_dir(str(tmp_path))
        assert acct2._previous_close_equity == Decimal("1000000")
        assert acct2._last_trading_day is not None

    def test_corrupt_basis_file_starts_at_zero(self, tmp_path, caplog):
        """A corrupt / unparseable basis file falls back to cold-start."""
        basis_file = Path(str(tmp_path)) / "daily_pnl_basis_SB1.json"
        basis_file.write_text("not valid json {{")
        with caplog.at_level(logging.WARNING):
            acct = _make_tinkoff_account_with_daily_dir(str(tmp_path))
        assert acct._previous_close_equity == Decimal("0")
        assert acct._last_trading_day is None

    def test_negative_basis_value_is_clamped_to_zero(self, tmp_path):
        """Defence-in-depth: negative previous_close (shouldn't happen) → 0."""
        basis_file = Path(str(tmp_path)) / "daily_pnl_basis_SB1.json"
        basis_file.write_text(json.dumps({"previous_close_equity": "-1000"}))
        acct = _make_tinkoff_account_with_daily_dir(str(tmp_path))
        assert acct._previous_close_equity == Decimal("0")

    def test_unparseable_trading_day_is_ignored(self, tmp_path):
        """A garbage ``last_trading_day`` value triggers cold-start for the day,
        keeping ``previous_close_equity`` intact (it's still a valid Decimal)."""
        basis_file = Path(str(tmp_path)) / "daily_pnl_basis_SB1.json"
        basis_file.write_text(
            json.dumps(
                {
                    "previous_close_equity": "1000000",
                    "last_trading_day": "not-a-date",
                }
            )
        )
        acct = _make_tinkoff_account_with_daily_dir(str(tmp_path))
        assert acct._previous_close_equity == Decimal("1000000")
        assert acct._last_trading_day is None

    def test_per_account_separation(self, tmp_path):
        """Two account_ids use two different basis files."""
        acct1 = _make_tinkoff_account_with_daily_dir(str(tmp_path), account_id="ACC1")
        acct2 = _make_tinkoff_account_with_daily_dir(str(tmp_path), account_id="ACC2")
        assert acct1._daily_pnl_basis_path != acct2._daily_pnl_basis_path

        with patch.object(acct1, "get_portfolio") as mock_gp1:
            mock_gp1.return_value = MagicMock(cash=Decimal("100000"), positions=[])
            acct1._fetch_real_portfolio_state()
        with patch.object(acct2, "get_portfolio") as mock_gp2:
            mock_gp2.return_value = MagicMock(cash=Decimal("200000"), positions=[])
            acct2._fetch_real_portfolio_state()

        assert Path(acct1._daily_pnl_basis_path).exists()
        assert Path(acct2._daily_pnl_basis_path).exists()
        data1 = json.loads(Path(acct1._daily_pnl_basis_path).read_text())
        data2 = json.loads(Path(acct2._daily_pnl_basis_path).read_text())
        assert data1["previous_close_equity"] == "100000"
        assert data2["previous_close_equity"] == "200000"


class TestIssue207FailClosed:
    """Issue #207: a stale/corrupt persisted basis MUST NOT silently
    disarm ``RISK_DAILY_LOSS`` on calendar mismatch. The fix introduces
    a trust gate (``schema_version >= 1`` AND ``basis_valid == True``)
    so legacy / partial / corrupt files trigger a fail-closed
    ``BrokerError`` instead of being silently overwritten with today's
    NAV. The risk control is a financial safety invariant; an
    over-permissive fallback here is a release blocker.

    Each test below mirrors one of the four scenarios from the issue
    body and acceptance criteria.
    """

    def test_legacy_basis_without_schema_marker_is_untrusted(self, tmp_path, caplog):
        """A basis file written before issue #207 has no ``schema_version``
        field. It loads the values (for diagnostics) but the trust flag
        is False so the next calendar mismatch refuses to use it as a
        rollover source. This is the central regression test."""
        from src.broker.tinkoff_account import _msk_today

        yesterday = (_msk_today() - timedelta(days=1)).isoformat()
        basis_file = Path(str(tmp_path)) / "daily_pnl_basis_SB1.json"
        basis_file.write_text(
            json.dumps(
                {
                    "previous_close_equity": "1000000",
                    "last_trading_day": yesterday,
                    # NOTE: no schema_version, no basis_valid — legacy payload
                }
            )
        )

        with caplog.at_level(logging.WARNING):
            acct = _make_tinkoff_account_with_daily_dir(str(tmp_path))

        assert acct._previous_close_equity == Decimal("1000000")  # values preserved
        assert acct._last_trading_day == date.fromisoformat(yesterday)
        assert acct._basis_trusted is False, (
            "Legacy payload (no schema_version / basis_valid) MUST load as "
            "untrusted; otherwise issue #207 re-arms RISK_DAILY_LOSS bypass."
        )

    def test_corrupt_basis_blocks_rollover_with_broker_error(self, tmp_path):
        """The exact reproduction from the issue body: corrupt / partial
        file with a stale date. Calling ``_fetch_real_portfolio_state``
        on a calendar mismatch MUST raise ``BrokerError`` instead of
        silently overwriting the basis with current NAV.
        """
        from src.broker.tinkoff_account import _msk_today
        from src.broker.tinkoff_account import BrokerError

        yesterday = (_msk_today() - timedelta(days=1)).isoformat()
        basis_file = Path(str(tmp_path)) / "daily_pnl_basis_SB1.json"
        # Stale date with a previous_close value but missing the schema
        # marker (mimics a deployment with a pre-issue-#207 file).
        basis_file.write_text(
            json.dumps(
                {
                    "previous_close_equity": "1000000",
                    "last_trading_day": yesterday,
                }
            )
        )

        acct = _make_tinkoff_account_with_daily_dir(str(tmp_path))
        assert acct._basis_trusted is False
        # Capture the file content BEFORE the (rejected) call — the fix
        # must not silently rewrite it.
        pre_call_payload = basis_file.read_text()

        with patch.object(acct, "get_portfolio") as mock_gp:
            mock_gp.return_value = MagicMock(cash=Decimal("950000"), positions=[])
            with pytest.raises(BrokerError) as exc_info:
                acct._fetch_real_portfolio_state()

        # Fail-closed with a useful message.
        msg = str(exc_info.value)
        assert "Untrusted daily-P&L basis" in msg
        assert "basis_trusted=False" in msg
        assert "RISK_DAILY_LOSS" in msg

        # The persisted basis MUST NOT have been overwritten — that was
        # the bug.
        assert basis_file.read_text() == pre_call_payload, (
            "Issue #207 fix: stale/corrupt basis must not be silently " "overwritten on a calendar mismatch."
        )

    def test_basis_valid_false_blocks_rollover_with_broker_error(self, tmp_path):
        """A payload that explicitly carries ``basis_valid=False`` must
        also fail-closed (e.g. operator manually flagged a session as
        unusable). Schema_version is present and >= 1, but the
        explicit False must still disable rollover."""
        from src.broker.tinkoff_account import _msk_today
        from src.broker.tinkoff_account import BrokerError

        yesterday = (_msk_today() - timedelta(days=1)).isoformat()
        basis_file = Path(str(tmp_path)) / "daily_pnl_basis_SB1.json"
        basis_file.write_text(
            json.dumps(
                {
                    "previous_close_equity": "1000000",
                    "last_trading_day": yesterday,
                    "schema_version": 1,
                    "basis_valid": False,
                }
            )
        )

        acct = _make_tinkoff_account_with_daily_dir(str(tmp_path))
        assert acct._basis_trusted is False

        with patch.object(acct, "get_portfolio") as mock_gp:
            mock_gp.return_value = MagicMock(cash=Decimal("950000"), positions=[])
            with pytest.raises(BrokerError) as exc_info:
                acct._fetch_real_portfolio_state()
        assert "Untrusted daily-P&L basis" in str(exc_info.value)

    def test_persisted_basis_is_trusted_after_first_snapshot(self, tmp_path):
        """A snapshot of the day persists a v1 trusted payload — subsequent
        processes can use it as a legitimate rollover source."""
        acct = _make_tinkoff_account_with_daily_dir(str(tmp_path))
        with patch.object(acct, "get_portfolio") as mock_gp:
            mock_gp.return_value = MagicMock(cash=Decimal("1000000"), positions=[])
            acct._fetch_real_portfolio_state()

        basis_file = Path(str(tmp_path)) / "daily_pnl_basis_SB1.json"
        payload = json.loads(basis_file.read_text())
        assert payload["schema_version"] == 1
        assert payload["basis_valid"] is True
        assert payload["previous_close_equity"] == "1000000"
        assert payload["last_trading_day"]  # ISO string

        # New process loads it as trusted.
        acct2 = _make_tinkoff_account_with_daily_dir(str(tmp_path))
        assert acct2._basis_trusted is True
        assert acct2._previous_close_equity == Decimal("1000000")

    def test_trusted_basis_rollover_on_new_day(self, tmp_path):
        """The legitimate day-rollover path remains functional: a v1
        trusted basis on yesterday triggers a normal rollover to today
        at current NAV."""
        from src.broker.tinkoff_account import _msk_today

        yesterday = (_msk_today() - timedelta(days=1)).isoformat()
        basis_file = Path(str(tmp_path)) / "daily_pnl_basis_SB1.json"
        basis_file.write_text(
            json.dumps(
                {
                    "previous_close_equity": "1000000",
                    "last_trading_day": yesterday,
                    "schema_version": 1,
                    "basis_valid": True,
                }
            )
        )

        acct = _make_tinkoff_account_with_daily_dir(str(tmp_path))
        assert acct._basis_trusted is True

        with patch.object(acct, "get_portfolio") as mock_gp:
            mock_gp.return_value = MagicMock(cash=Decimal("950000"), positions=[])
            state = acct._fetch_real_portfolio_state()
        assert state.daily_pnl == Decimal("0")
        assert acct._previous_close_equity == Decimal("950000")
        assert acct._last_trading_day == _msk_today()

        # File on disk now reflects the new trusted basis.
        payload = json.loads(basis_file.read_text())
        assert payload["schema_version"] == 1
        assert payload["basis_valid"] is True
        assert payload["previous_close_equity"] == "950000"
        assert payload["last_trading_day"] == _msk_today().isoformat()

    def test_weekend_rollover_legitimate_path(self, tmp_path):
        """A weekend (Saturday/Sunday) rollover with a v1 trusted basis
        must still succeed — the broker hasn't traded, but the date
        advances. The fix must not over-restrict legitimate calendar
        transitions."""
        from src.broker.tinkoff_account import _msk_today

        # Simulate "last trading day was Friday, today is Monday".
        today = _msk_today()
        # Pick an arbitrary date 3 days ago (covers weekend + holiday).
        arbitrary_past = (today - timedelta(days=3)).isoformat()
        basis_file = Path(str(tmp_path)) / "daily_pnl_basis_SB1.json"
        basis_file.write_text(
            json.dumps(
                {
                    "previous_close_equity": "1000000",
                    "last_trading_day": arbitrary_past,
                    "schema_version": 1,
                    "basis_valid": True,
                }
            )
        )

        acct = _make_tinkoff_account_with_daily_dir(str(tmp_path))
        assert acct._basis_trusted is True
        with patch.object(acct, "get_portfolio") as mock_gp:
            mock_gp.return_value = MagicMock(cash=Decimal("950000"), positions=[])
            state = acct._fetch_real_portfolio_state()
        assert state.daily_pnl == Decimal("0")
        assert acct._last_trading_day == today

    def test_untrusted_basis_within_same_day_is_non_blocking(self, tmp_path):
        """An untrusted basis on the SAME calendar day must NOT block
        daily_pnl computation — the same-day branch doesn't trust the
        basis for the kill-switch override (it uses the persisted value
        directly). Issue #207 only blocks the calendar-mismatch path.
        """
        from src.broker.tinkoff_account import _msk_today

        today_iso = _msk_today().isoformat()
        basis_file = Path(str(tmp_path)) / "daily_pnl_basis_SB1.json"
        basis_file.write_text(
            json.dumps(
                {
                    "previous_close_equity": "1000000",
                    "last_trading_day": today_iso,
                    # No schema marker → untrusted, but the stored day is today.
                }
            )
        )

        acct = _make_tinkoff_account_with_daily_dir(str(tmp_path))
        assert acct._basis_trusted is False

        # Same-day branch: basis is used as-is, daily_pnl computed normally.
        with patch.object(acct, "get_portfolio") as mock_gp:
            mock_gp.return_value = MagicMock(cash=Decimal("960000"), positions=[])
            state = acct._fetch_real_portfolio_state()
        assert state.daily_pnl == Decimal("-40000"), (
            "Same-day branch uses the persisted basis directly; an "
            "untrusted marker does not block daily_pnl computation when "
            "the calendar hasn't changed."
        )

    def test_risk_daily_loss_trips_when_basis_fail_closed_raises(self, tmp_path):
        """End-to-end: when ``_fetch_real_portfolio_state`` raises
        ``BrokerError`` (issue #207 fail-closed), the OrderFlow path
        cannot construct a ``PortfolioState`` so no ``RISK_DAILY_LOSS``
        violation can be silently masked — the order is rejected by
        the surrounding ``try/except`` (caller side) and the kill-switch
        remains armed for any subsequent clean restart."""
        from src.broker.tinkoff_account import BrokerError

        from src.broker.tinkoff_account import _msk_today

        yesterday = (_msk_today() - timedelta(days=1)).isoformat()
        basis_file = Path(str(tmp_path)) / "daily_pnl_basis_SB1.json"
        basis_file.write_text(
            json.dumps(
                {
                    "previous_close_equity": "1000000",
                    "last_trading_day": yesterday,
                }
            )
        )

        gate = RiskGate(limits=_make_risk_limits(max_daily_loss_pct=Decimal("3")))
        acct = _make_tinkoff_account_with_daily_dir(str(tmp_path), risk_gate=gate)

        with patch.object(acct, "get_portfolio") as mock_gp:
            mock_gp.return_value = MagicMock(cash=Decimal("950000"), positions=[])
            with pytest.raises(BrokerError):
                acct._fetch_real_portfolio_state()

        # The gate itself is untouched and ready for the next clean
        # restart. No PortfolioState is built on the bad call, so the
        # kill-switch stays armed (no allowed=True with daily_pnl=0
        # leaking through).


class TestRiskDailyLossFiresEndToEnd:
    """End-to-end: ``_check_daily_loss`` now actually fires when daily_pnl
    is non-zero from the production ``_fetch_real_portfolio_state`` path."""

    def test_risk_daily_loss_fires_on_intraday_loss(self, tmp_path):
        """A -4% day (limit 3%) must produce RISK_DAILY_LOSS in the gate."""
        gate = RiskGate(limits=_make_risk_limits(max_daily_loss_pct=Decimal("3")))
        acct = _make_tinkoff_account_with_daily_dir(str(tmp_path), risk_gate=gate)

        # Stamp basis at 1_000_000 (first snapshot of the day → daily_pnl=0).
        with patch.object(acct, "get_portfolio") as mock_gp:
            mock_gp.return_value = MagicMock(cash=Decimal("1000000"), positions=[])
            state1 = acct._fetch_real_portfolio_state()
        assert state1.daily_pnl == Decimal("0")

        # NAV drops 4% → -40_000, daily_pnl reported, gate trips.
        with patch.object(acct, "get_portfolio") as mock_gp:
            mock_gp.return_value = MagicMock(cash=Decimal("960000"), positions=[])
            state2 = acct._fetch_real_portfolio_state()
        assert state2.daily_pnl == Decimal("-40000"), f"expected -40000, got {state2.daily_pnl}"

        intent = TradeIntent(
            symbol="SBER",
            side="buy",
            quantity=Decimal("10"),
            price=Decimal("100"),
        )
        decision = gate.evaluate(intent, state2)
        assert decision.allowed is False, (
            f"RISK_DAILY_LOSS must fire on -4% day vs 3% limit; " f"got allowed=True, violations={decision.violations}"
        )
        assert any(
            "RISK_DAILY_LOSS" in v for v in decision.violations
        ), f"Expected RISK_DAILY_LOSS violation, got: {decision.violations}"

    def test_risk_daily_loss_within_threshold_allows_trade(self, tmp_path):
        """A -2% day (within 3% limit) must NOT trip the kill-switch."""
        gate = RiskGate(limits=_make_risk_limits(max_daily_loss_pct=Decimal("3")))
        acct = _make_tinkoff_account_with_daily_dir(str(tmp_path), risk_gate=gate)

        with patch.object(acct, "get_portfolio") as mock_gp:
            mock_gp.return_value = MagicMock(cash=Decimal("1000000"), positions=[])
            acct._fetch_real_portfolio_state()
        with patch.object(acct, "get_portfolio") as mock_gp:
            mock_gp.return_value = MagicMock(cash=Decimal("980000"), positions=[])  # -2%
            state2 = acct._fetch_real_portfolio_state()

        assert state2.daily_pnl == Decimal("-20000")

        intent = TradeIntent(
            symbol="SBER",
            side="buy",
            quantity=Decimal("10"),
            price=Decimal("100"),
        )
        decision = gate.evaluate(intent, state2)
        assert not any(
            "RISK_DAILY_LOSS" in v for v in decision.violations
        ), f"-2% day within 3% limit must not trip; got: {decision.violations}"

    def test_risk_daily_loss_at_boundary_trips_due_to_denominator_shift(self, tmp_path):
        """Behaviour note (issue #197): in production ``PortfolioState``,
        ``total_equity`` IS the current NAV (not the previous-close). So
        when NAV drops from 1M to 970k, ``daily_pnl = -30k`` and
        ``loss_pct = 30k / 970k * 100 = 3.0928%`` — strictly greater
        than the 3% limit, so the gate trips.

        The ``test_risk_gate.TestDailyLoss.test_daily_loss_at_limit_allowed``
        unit test uses ``total_equity=1M`` with ``daily_pnl=-30k`` (i.e.
        NAV is held at 1M even though P&L is -30k) so loss_pct is exactly
        3.0000% — that's the ``>`` boundary the unit test exercises.
        In production the denominator shifts with NAV, so the gate's
        ``strict >`` semantics fire slightly earlier than the operator
        might expect. This test pins the production behaviour."""
        gate = RiskGate(limits=_make_risk_limits(max_daily_loss_pct=Decimal("3")))
        acct = _make_tinkoff_account_with_daily_dir(str(tmp_path), risk_gate=gate)

        # Stamp basis at 1_000_000.
        with patch.object(acct, "get_portfolio") as mock_gp:
            mock_gp.return_value = MagicMock(cash=Decimal("1000000"), positions=[])
            acct._fetch_real_portfolio_state()
        # NAV = 970_000 → -30_000, total_equity = 970_000.
        with patch.object(acct, "get_portfolio") as mock_gp:
            mock_gp.return_value = MagicMock(cash=Decimal("970000"), positions=[])
            state2 = acct._fetch_real_portfolio_state()

        # The denominator-shift makes this 3.0928%, slightly above 3% → trip.
        assert state2.daily_pnl == Decimal("-30000")
        assert state2.total_equity == Decimal("970000")

        intent = TradeIntent(
            symbol="SBER",
            side="buy",
            quantity=Decimal("10"),
            price=Decimal("100"),
        )
        decision = gate.evaluate(intent, state2)
        # Trip expected: production uses (NAV, NAV) basis, not (NAV, basis).
        assert any("RISK_DAILY_LOSS" in v for v in decision.violations), (
            f"Expected RISK_DAILY_LOSS at 3.0928% (denominator shift); " f"got: {decision.violations}"
        )

    def test_risk_daily_loss_does_not_fire_on_intraday_profit(self, tmp_path):
        """Positive daily_pnl never trips the daily-loss check."""
        gate = RiskGate(limits=_make_risk_limits(max_daily_loss_pct=Decimal("3")))
        acct = _make_tinkoff_account_with_daily_dir(str(tmp_path), risk_gate=gate)

        with patch.object(acct, "get_portfolio") as mock_gp:
            mock_gp.return_value = MagicMock(cash=Decimal("1000000"), positions=[])
            acct._fetch_real_portfolio_state()
        with patch.object(acct, "get_portfolio") as mock_gp:
            mock_gp.return_value = MagicMock(cash=Decimal("1050000"), positions=[])  # +5%
            state2 = acct._fetch_real_portfolio_state()

        assert state2.daily_pnl == Decimal("50000"), f"expected +50000, got {state2.daily_pnl}"

        intent = TradeIntent(
            symbol="SBER",
            side="buy",
            quantity=Decimal("10"),
            price=Decimal("100"),
        )
        decision = gate.evaluate(intent, state2)
        assert decision.meta["daily_loss_pct"] == 0.0
        assert not any("RISK_DAILY_LOSS" in v for v in decision.violations)

    def test_risk_daily_loss_repro_from_issue_body(self, tmp_path):
        """The exact scenario from issue #197's body comment:
        NAV 800k after a -20% day from peak 1M, daily_pnl = -200k.

        Without the fix, ``daily_pnl`` is always 0 and the gate allows the
        trade. With the fix, ``daily_pnl = -200000`` → -25% loss → trips
        ``max_daily_loss_pct = 3``."""
        gate = RiskGate(limits=_make_risk_limits(max_daily_loss_pct=Decimal("3")))
        acct = _make_tinkoff_account_with_daily_dir(str(tmp_path), risk_gate=gate)

        # Stamp basis at 1_000_000.
        with patch.object(acct, "get_portfolio") as mock_gp:
            mock_gp.return_value = MagicMock(cash=Decimal("1000000"), positions=[])
            acct._fetch_real_portfolio_state()

        # NAV = 800_000 → -200_000 (-20% day).
        with patch.object(acct, "get_portfolio") as mock_gp:
            mock_gp.return_value = MagicMock(cash=Decimal("800000"), positions=[])
            state2 = acct._fetch_real_portfolio_state()

        assert state2.daily_pnl == Decimal("-200000")

        intent = TradeIntent(
            symbol="SBER",
            side="buy",
            quantity=Decimal("10"),
            price=Decimal("100"),
        )
        decision = gate.evaluate(intent, state2)
        assert decision.allowed is False, (
            f"Issue #197 repro: -20% day vs 3% limit must reject; "
            f"got allowed=True, violations={decision.violations}"
        )
        assert any("RISK_DAILY_LOSS" in v for v in decision.violations)


class TestOrderFlowDailyPnlWiring:
    """Issue #197 wiring through ``OrderFlow.submit_market`` (the
    post-#195 production path via ``_portfolio_to_state_impl``)."""

    def test_orderflow_forwards_daily_pnl_to_portfolio_state(self):
        """OrderFlow.submit_market must propagate ``daily_pnl`` into the
        ``PortfolioState`` it builds, so ``RiskGate._check_daily_loss``
        can fire on the OrderFlow path when a caller passes a real value."""
        from src.broker.account import PortfolioSnapshot
        from src.broker.integration import OrderFlow
        from src.broker.orders import OrderSide, OrderStatus

        captured_state = {}

        class CapturingGate(RiskGate):
            def evaluate(self, intent, state):  # type: ignore[override]
                captured_state.update(
                    {
                        "daily_pnl": state.daily_pnl,
                        "total_equity": state.total_equity,
                        "peak_equity": state.peak_equity,
                    }
                )
                return super().evaluate(intent, state)

        capturing_gate = CapturingGate(limits=_make_risk_limits(max_daily_loss_pct=Decimal("3")))

        broker = MagicMock()
        broker.place_order.return_value = OrderStatus.SUBMITTED

        flow = OrderFlow(
            broker=broker,
            risk_gate=capturing_gate,
            quote_provider=lambda symbol: Decimal("100"),
        )

        portfolio = PortfolioSnapshot(
            account_id="SB1",
            cash=Decimal("1000000"),
            positions=[],
            timestamp=datetime.now(tz=timezone.utc),
        )
        # -4% intraday loss → gate must reject via RISK_DAILY_LOSS.
        flow.submit_market(
            "SBER",
            OrderSide.BUY,
            Decimal("10"),
            portfolio,
            daily_pnl=Decimal("-40000"),
        )

        assert captured_state["daily_pnl"] == Decimal("-40000"), (
            f"OrderFlow did not forward daily_pnl into PortfolioState; " f"captured={captured_state}"
        )

    def test_orderflow_default_daily_pnl_is_zero(self):
        """Backward-compat: callers that don't pass ``daily_pnl`` get
        ``Decimal(\"0\")`` — same fail-open behaviour the field always had."""
        from src.broker.account import PortfolioSnapshot
        from src.broker.integration import OrderFlow
        from src.broker.orders import OrderSide, OrderStatus

        captured_state = {}

        class CapturingGate(RiskGate):
            def evaluate(self, intent, state):  # type: ignore[override]
                captured_state["daily_pnl"] = state.daily_pnl
                return super().evaluate(intent, state)

        capturing_gate = CapturingGate(limits=_make_risk_limits(max_daily_loss_pct=Decimal("3")))

        broker = MagicMock()
        broker.place_order.return_value = OrderStatus.SUBMITTED

        flow = OrderFlow(
            broker=broker,
            risk_gate=capturing_gate,
            quote_provider=lambda symbol: Decimal("100"),
        )

        portfolio = PortfolioSnapshot(
            account_id="SB1",
            cash=Decimal("1000000"),
            positions=[],
            timestamp=datetime.now(tz=timezone.utc),
        )
        flow.submit_market("SBER", OrderSide.BUY, Decimal("10"), portfolio)
        assert captured_state["daily_pnl"] == Decimal(
            "0"
        ), f"Default daily_pnl must be 0, got {captured_state['daily_pnl']}"


class TestDailyPnlBasisAtomicWrite:
    """Issue #214: ``_save_daily_pnl_basis`` must be atomic (temp + replace)
    and the load path must fall back to ``.bak`` on corrupt primary so a
    SIGKILL mid-write does NOT silently disarm ``RISK_DAILY_LOSS``.

    Mirrors the ``peak_equity`` test class introduced in issue #199.
    """

    def test_save_uses_tmp_file_then_replace(self, tmp_path):
        """Successful save must publish via ``os.replace`` from a sibling
        tmp file — never leave a half-written primary on disk."""
        acct = _make_tinkoff_account_with_daily_dir(str(tmp_path))
        # Snapshot a value, then save it.
        acct._previous_close_equity = Decimal("987654")
        from datetime import date as _date

        acct._last_trading_day = _date(2026, 8, 24)
        acct._save_daily_pnl_basis()

        primary = Path(acct._daily_pnl_basis_path)
        tmp = Path(str(primary) + ".tmp")

        # Primary exists with the right payload; tmp does NOT linger.
        assert primary.exists(), "primary basis file must exist after save"
        assert not tmp.exists(), (
            "tmp file must be renamed away (os.replace) — leaving a tmp "
            "file behind means the publish step did not run"
        )
        data = json.loads(primary.read_text())
        assert data["previous_close_equity"] == "987654"
        assert data["last_trading_day"] == "2026-08-24"
        assert data["schema_version"] == 1
        assert data["basis_valid"] is True

    def test_save_writes_bak_mirror_of_previous_primary(self, tmp_path):
        """Before each save, the *current* primary content must be mirrored
        to ``.bak`` so a corrupt primary can fall back to the
        last-known-good value rather than zero."""
        from datetime import date as _date

        acct = _make_tinkoff_account_with_daily_dir(str(tmp_path))

        # First save: only the primary is written (no .bak yet).
        acct._previous_close_equity = Decimal("1000000")
        acct._last_trading_day = _date(2026, 8, 23)
        acct._save_daily_pnl_basis()

        bak = Path(acct._daily_pnl_basis_bak_path)
        assert not bak.exists(), "first save: .bak must NOT exist (nothing to mirror yet)"

        # Second save: primary gets updated, .bak mirrors the OLD primary.
        acct._previous_close_equity = Decimal("1050000")
        acct._last_trading_day = _date(2026, 8, 24)
        acct._save_daily_pnl_basis()

        assert bak.exists(), (
            "second save: .bak must contain a snapshot of the previous "
            "primary so a corrupt primary can recover from it"
        )
        bak_data = json.loads(bak.read_text())
        assert (
            bak_data["previous_close_equity"] == "1000000"
        ), f".bak must hold the previous primary value, got {bak_data!r}"
        assert bak_data["last_trading_day"] == "2026-08-23"

    def test_load_falls_back_to_bak_when_primary_corrupt(self, tmp_path):
        """A corrupt primary (simulating SIGKILL mid-write) must NOT
        silently disarm ``RISK_DAILY_LOSS`` — the loader must recover
        from ``.bak`` instead of falling back to cold-start zero."""
        from datetime import date as _date

        # Lay down a valid primary, then run a save to create a .bak of
        # the *previous* value (yesterday's close).
        primary = Path(str(tmp_path)) / "daily_pnl_basis_SB1.json"
        primary.write_text(
            json.dumps(
                {
                    "previous_close_equity": "1000000",
                    "last_trading_day": "2026-08-23",
                    "schema_version": 1,
                    "basis_valid": True,
                }
            )
        )
        bak = Path(str(primary) + ".bak")
        bak.write_text(
            json.dumps(
                {
                    "previous_close_equity": "950000",
                    "last_trading_day": "2026-08-22",
                    "schema_version": 1,
                    "basis_valid": True,
                }
            )
        )
        # Corrupt the primary (simulating SIGKILL between truncate and
        # the rest of json.dump).
        primary.write_text("{" + '"previous_close_e')

        acct = _make_tinkoff_account_with_daily_dir(str(tmp_path))
        assert acct._previous_close_equity == Decimal("950000"), (
            f"loader must recover from .bak (950000), not cold-start (0); " f"got {acct._previous_close_equity}"
        )
        assert acct._last_trading_day == _date(2026, 8, 22)
        assert acct._basis_trusted is True, (
            "recovered .bak has the v1 schema marker + basis_valid=True "
            "→ basis must be trusted so the kill-switch can trip"
        )

    def test_corrupt_primary_with_no_bak_is_true_cold_start(self, tmp_path):
        """If both primary and .bak are corrupt (catastrophic disk loss),
        the loader falls back to cold-start — RISK_DAILY_LOSS is not
        tripped for the first call of the session (documented behaviour,
        see ``_load_daily_pnl_basis`` docstring). This is the ONLY case
        where ``_previous_close_equity`` legitimately stays at 0."""
        primary = Path(str(tmp_path)) / "daily_pnl_basis_SB1.json"
        primary.write_text("garbage")
        bak = Path(str(primary) + ".bak")
        bak.write_text("also garbage")

        acct = _make_tinkoff_account_with_daily_dir(str(tmp_path))
        assert acct._previous_close_equity == Decimal("0")
        assert acct._last_trading_day is None
        assert acct._basis_trusted is False

    def test_save_creates_tmp_only_in_same_directory(self, tmp_path):
        """The atomic-write tmp file MUST be a sibling of the primary
        file — otherwise ``os.replace`` is not atomic across filesystems
        and a kill mid-rename would resurrect the original non-atomic
        failure mode (issue #199)."""
        acct = _make_tinkoff_account_with_daily_dir(str(tmp_path))
        acct._previous_close_equity = Decimal("100")

        # Monkey-patch ``os.replace`` to inspect the src/dst pair.
        captured: dict[str, str] = {}
        real_replace = os.replace

        def spy_replace(src, dst):
            captured["src_dir"] = os.path.dirname(os.fspath(src))
            captured["dst_dir"] = os.path.dirname(os.fspath(dst))
            return real_replace(src, dst)

        with patch("os.replace", side_effect=spy_replace):
            acct._save_daily_pnl_basis()

        assert captured.get("src_dir") == captured.get("dst_dir"), (
            f"os.replace must run within a single filesystem (same dir); "
            f"src_dir={captured.get('src_dir')!r} dst_dir={captured.get('dst_dir')!r}"
        )
        assert captured["src_dir"] == str(tmp_path)
