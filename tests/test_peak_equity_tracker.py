"""Tests for issue #32: peak-equity high-water mark tracker.

These tests cover the regression where ``_fetch_real_portfolio_state`` set
``peak_equity = current_equity`` on every call, which made the
``_check_drawdown`` guard in ``src/risk/gate.py`` always evaluate to
drawdown = 0 — i.e., the RISK_DD guard never tripped in production,
even after a 20-30% drawdown.

The fix: persist a peak-equity high-water mark on disk, read on
construction, updated (monotonically) and re-saved on every successful
snapshot. ``PortfolioState.peak_equity`` is now the running max, not
the current value.
"""

from __future__ import annotations

import json
import os
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock, patch

from src.risk.gate import RiskGate, RiskLimits


def _make_risk_limits(**overrides) -> RiskLimits:
    """Helper: build a RiskLimits with sensible defaults for DD-focused tests.

    The defaults are deliberately permissive (high position limits, no
    shorting) so the only blocking factor under test is the DD guard.
    """
    # Cast each value to its declared type so Pyright does not infer
    # ``bool`` from the literal ``False`` and reject a Decimal kwarg.
    defaults: dict[str, object] = {
        "max_position_pct": Decimal("100"),
        "max_dd_pct": Decimal("15"),
        "max_sector_pct": Decimal("100"),
        "max_daily_loss_pct": Decimal("100"),
        "leverage_max": Decimal("1.0"),
        "allow_short": False,
    }
    for key, value in overrides.items():
        defaults[key] = value
    return RiskLimits(**defaults)  # type: ignore[arg-type]


def _make_tinkoff_account_with_peak_dir(peak_dir: str, account_id: str = "SB1", risk_gate=None):
    """Helper: build a TinkoffAccount pointing at ``peak_dir`` for peak
    state, and stub out the SDK so it can be constructed without
    network access.
    """
    from src.broker.tinkoff_account import TinkoffAccount

    with patch.dict(os.environ, {"ALPHARD_PEAK_STORE_DIR": peak_dir}):
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


class TestPeakEquityTracker:
    """The peak-equity tracker is a per-account monotonic high-water
    mark persisted to disk.
    """

    def test_cold_start_peak_is_zero(self, tmp_path):
        """A fresh TinkoffAccount (no existing peak file) starts at 0."""
        acct = _make_tinkoff_account_with_peak_dir(str(tmp_path))
        assert acct._peak_equity == Decimal("0")

    def test_first_snapshot_sets_peak_to_current(self, tmp_path):
        """After the first snapshot, peak == current. Persisted to disk."""
        acct = _make_tinkoff_account_with_peak_dir(str(tmp_path))
        with patch.object(acct, "get_portfolio") as mock_gp:
            mock_snap = MagicMock()
            mock_snap.cash = Decimal("100000")
            mock_snap.positions = []
            mock_gp.return_value = mock_snap
            state = acct._fetch_real_portfolio_state()
        assert state.peak_equity == Decimal("100000")
        assert state.total_equity == Decimal("100000")
        peak_file = Path(str(tmp_path)) / "peak_equity_SB1.json"
        assert peak_file.exists()
        data = json.loads(peak_file.read_text())
        assert data["peak_equity"] == "100000"

    def test_drawdown_does_not_lower_peak(self, tmp_path):
        """A drawdown must NOT reduce the stored peak — only growth does.

        This is the regression that issue #32 fixed: previously
        peak = current on every snapshot, so a drawdown never registered.
        """
        acct = _make_tinkoff_account_with_peak_dir(str(tmp_path))
        with patch.object(acct, "get_portfolio") as mock_gp:
            # Snapshot 1: equity 100_000, peak becomes 100_000
            mock_gp.return_value = MagicMock(cash=Decimal("100000"), positions=[])
            state1 = acct._fetch_real_portfolio_state()
            assert state1.peak_equity == Decimal("100000")

            # Snapshot 2: equity drops to 80_000. Peak must STAY at 100_000.
            mock_gp.return_value = MagicMock(cash=Decimal("80000"), positions=[])
            state2 = acct._fetch_real_portfolio_state()
            assert state2.total_equity == Decimal("80000")
            assert state2.peak_equity == Decimal("100000")  # the fix

    def test_growth_raises_peak(self, tmp_path):
        """When equity exceeds stored peak, peak is updated."""
        acct = _make_tinkoff_account_with_peak_dir(str(tmp_path))
        with patch.object(acct, "get_portfolio") as mock_gp:
            mock_gp.return_value = MagicMock(cash=Decimal("100000"), positions=[])
            state1 = acct._fetch_real_portfolio_state()
            assert state1.peak_equity == Decimal("100000")

            mock_gp.return_value = MagicMock(cash=Decimal("150000"), positions=[])
            state2 = acct._fetch_real_portfolio_state()
            assert state2.total_equity == Decimal("150000")
            assert state2.peak_equity == Decimal("150000")

    def test_peak_persists_across_restarts(self, tmp_path):
        """A second TinkoffAccount instance constructed against the same
        peak-store directory loads the previously-saved peak.
        """
        acct1 = _make_tinkoff_account_with_peak_dir(str(tmp_path))
        with patch.object(acct1, "get_portfolio") as mock_gp:
            mock_gp.return_value = MagicMock(cash=Decimal("100000"), positions=[])
            acct1._fetch_real_portfolio_state()

        acct2 = _make_tinkoff_account_with_peak_dir(str(tmp_path))
        assert acct2._peak_equity == Decimal("100000")

    def test_corrupt_peak_file_starts_at_zero(self, tmp_path):
        """A corrupt / unparseable peak file should not crash the
        constructor — it falls back to 0 and logs a warning.
        """
        peak_file = Path(str(tmp_path)) / "peak_equity_SB1.json"
        peak_file.write_text("not valid json {{")
        # Should not raise
        acct = _make_tinkoff_account_with_peak_dir(str(tmp_path))
        assert acct._peak_equity == Decimal("0")

    def test_negative_peak_value_is_treated_as_zero(self, tmp_path):
        """Defence-in-depth: a negative value (shouldn't happen) is
        clamped to 0 so PortfolioState validation doesn't blow up.
        """
        peak_file = Path(str(tmp_path)) / "peak_equity_SB1.json"
        peak_file.write_text(json.dumps({"peak_equity": "-1000"}))
        acct = _make_tinkoff_account_with_peak_dir(str(tmp_path))
        assert acct._peak_equity == Decimal("0")

    def test_per_account_separation(self, tmp_path):
        """Two different account_ids use two different peak files."""
        acct1 = _make_tinkoff_account_with_peak_dir(str(tmp_path), account_id="ACC1")
        acct2 = _make_tinkoff_account_with_peak_dir(str(tmp_path), account_id="ACC2")
        # Each has its own file path
        assert acct1._peak_equity_path != acct2._peak_equity_path
        # Set peaks
        with patch.object(acct1, "get_portfolio") as mock_gp1:
            mock_gp1.return_value = MagicMock(cash=Decimal("100000"), positions=[])
            acct1._fetch_real_portfolio_state()
        with patch.object(acct2, "get_portfolio") as mock_gp2:
            mock_gp2.return_value = MagicMock(cash=Decimal("200000"), positions=[])
            acct2._fetch_real_portfolio_state()
        # Both files exist
        assert Path(acct1._peak_equity_path).exists()
        assert Path(acct2._peak_equity_path).exists()
        # Each holds its own value
        data1 = json.loads(Path(acct1._peak_equity_path).read_text())
        data2 = json.loads(Path(acct2._peak_equity_path).read_text())
        assert data1["peak_equity"] == "100000"
        assert data2["peak_equity"] == "200000"


class TestDrawdownTriggersRiskDD:
    """End-to-end: the drawdown guard in src/risk/gate.py now actually
    fires when current_equity < stored_peak.
    """

    def test_risk_dd_fires_on_drawdown(self, tmp_path):
        """Given a stored peak of 100_000 and a current equity of 80_000
        with max_dd_pct=15, the order must be rejected with RISK_DD.
        """
        from src.broker.tinkoff_account import TinkoffAccount
        from src.risk.gate import TradeIntent

        # Pre-populate the peak file at 100_000
        peak_file = Path(str(tmp_path)) / "peak_equity_SB1.json"
        peak_file.write_text(json.dumps({"peak_equity": "100000"}))

        with patch.dict(
            os.environ,
            {
                "TINKOFF_SANDBOX_TOKEN": "t.test_token_aaaaaaaaaaaaaaaaaaa",
                "ALPHARD_PEAK_STORE_DIR": str(tmp_path),
            },
        ):
            gate = RiskGate(limits=_make_risk_limits(max_dd_pct=Decimal("15")))
            acct = TinkoffAccount(
                token="t.test_token_aaaaaaaaaaaaaaaaaaa",
                account_id="SB1",
                risk_gate=gate,
            )

        assert acct._peak_equity == Decimal("100000")

        with patch.object(acct, "get_portfolio") as mock_gp:
            mock_gp.return_value = MagicMock(
                cash=Decimal("80000"),  # 20% below 100_000 peak
                positions=[],
            )
            state = acct._fetch_real_portfolio_state()

        intent = TradeIntent(
            symbol="SBER",
            side="buy",
            quantity=Decimal("10"),
            price=Decimal("300"),
        )
        decision = gate.evaluate(intent, state)
        assert decision.allowed is False, (
            f"RISK_DD must fire on 20% drawdown but got allowed=True. " f"Violations: {decision.violations}"
        )
        assert any(
            "RISK_DD" in v for v in decision.violations
        ), f"Expected RISK_DD violation, got: {decision.violations}"

    def test_risk_dd_does_not_fire_within_threshold(self, tmp_path):
        """If current_equity is within max_dd_pct of peak, no RISK_DD."""
        from src.broker.tinkoff_account import TinkoffAccount
        from src.risk.gate import TradeIntent

        peak_file = Path(str(tmp_path)) / "peak_equity_SB1.json"
        peak_file.write_text(json.dumps({"peak_equity": "100000"}))

        with patch.dict(
            os.environ,
            {
                "TINKOFF_SANDBOX_TOKEN": "t.test_token_aaaaaaaaaaaaaaaaaaa",
                "ALPHARD_PEAK_STORE_DIR": str(tmp_path),
            },
        ):
            gate = RiskGate(limits=_make_risk_limits(max_dd_pct=Decimal("20")))
            acct = TinkoffAccount(
                token="t.test_token_aaaaaaaaaaaaaaaaaaa",
                account_id="SB1",
                risk_gate=gate,
            )

        # 10% drawdown (within 20% threshold)
        with patch.object(acct, "get_portfolio") as mock_gp:
            mock_gp.return_value = MagicMock(
                cash=Decimal("90000"),
                positions=[],
            )
            state = acct._fetch_real_portfolio_state()
        intent = TradeIntent(
            symbol="SBER",
            side="buy",
            quantity=Decimal("10"),
            price=Decimal("300"),
        )
        decision = gate.evaluate(intent, state)
        # 10% drawdown is within 20% threshold — DD must not fire
        assert not any(
            "RISK_DD" in v for v in decision.violations
        ), f"10% drawdown should not trip 20% DD limit; got: {decision.violations}"

    def test_cold_start_peak_zero_does_not_trip_dd(self, tmp_path):
        """A cold-start (no prior peak) must not artificially trip DD."""
        from src.broker.tinkoff_account import TinkoffAccount
        from src.risk.gate import TradeIntent

        with patch.dict(
            os.environ,
            {
                "TINKOFF_SANDBOX_TOKEN": "t.test_token_aaaaaaaaaaaaaaaaaaa",
                "ALPHARD_PEAK_STORE_DIR": str(tmp_path),
            },
        ):
            gate = RiskGate(limits=_make_risk_limits(max_dd_pct=Decimal("10")))
            acct = TinkoffAccount(
                token="t.test_token_aaaaaaaaaaaaaaaaaaa",
                account_id="SB1",
                risk_gate=gate,
            )
        # First snapshot: peak=0, then peaks to current
        with patch.object(acct, "get_portfolio") as mock_gp:
            mock_gp.return_value = MagicMock(
                cash=Decimal("100000"),
                positions=[],
            )
            state = acct._fetch_real_portfolio_state()
        assert state.peak_equity == Decimal("100000")
        assert state.total_equity == Decimal("100000")
        # No drawdown at first snapshot — no DD
        decision = gate.evaluate(
            TradeIntent(
                symbol="SBER",
                side="buy",
                quantity=Decimal("1"),
                price=Decimal("300"),
            ),
            state,
        )
        assert not any("RISK_DD" in v for v in decision.violations)


class TestZeroNavPortfolioState:
    """Issue #42 regression tests.

    ``_fetch_real_portfolio_state`` must raise ``BrokerError`` (not bare
    ``pydantic.ValidationError``) when ``get_portfolio`` reports a zero or
    negative NAV, because ``PortfolioState`` requires ``gt=0`` on both
    ``total_equity`` and ``peak_equity``. Without the guard, cold-start
    sandboxes raise a ValidationError that callers catching ``BrokerError``
    do not handle.
    """

    def test_zero_cash_on_cold_start_raises_broker_error(self, tmp_path, caplog):
        """Cold start + cash=0 must raise BrokerError, never ValidationError."""
        import logging

        from src.broker.tinkoff_account import BrokerError, TinkoffAccount

        with patch.dict(
            os.environ,
            {
                "TINKOFF_SANDBOX_TOKEN": "t.test_token_aaaaaaaaaaaaaaaaaaa",
                "ALPHARD_PEAK_STORE_DIR": str(tmp_path),
            },
        ):
            acct = TinkoffAccount(
                token="t.test_token_aaaaaaaaaaaaaaaaaaa",
                account_id="SB1",
                risk_gate=None,
            )
        assert acct._peak_equity == Decimal("0"), "cold start should seed peak=0"

        with patch.object(acct, "get_portfolio") as mock_gp:
            mock_gp.return_value = MagicMock(
                cash=Decimal("0"),
                positions=[],
            )
            with caplog.at_level(logging.WARNING):
                with __import__("pytest").raises(BrokerError) as exc_info:
                    acct._fetch_real_portfolio_state()

        msg = str(exc_info.value)
        assert "SB1" in msg, f"error must name the account id; got: {msg}"
        assert "total_amount_currencies" in msg, f"error must hint at likely cause; got: {msg}"

    def test_missing_total_amount_currencies_logs_warning(self, tmp_path, caplog):
        """A gRPC response with no NAV field must be distinguishable in logs.

        Issue #42 secondary defect: silently defaulting to ``Decimal("0")``
        at the parse site conflates "gRPC contract mismatch" with a real
        zero balance. The parse path must log at WARNING so operators can
        distinguish the two.
        """
        import logging

        from src.broker.tinkoff_account import TinkoffAccount

        with patch.dict(
            os.environ,
            {
                "TINKOFF_SANDBOX_TOKEN": "t.test_token_aaaaaaaaaaaaaaaaaaa",
                "ALPHARD_PEAK_STORE_DIR": str(tmp_path),
            },
        ):
            acct = TinkoffAccount(
                token="t.test_token_aaaaaaaaaaaaaaaaaaa",
                account_id="SB1",
                risk_gate=None,
            )

        fake_portfolio = MagicMock(spec=["positions"])
        fake_portfolio.positions = []
        fake_portfolio.total_amount_currencies = None
        fake_portfolio.total_amount = None

        with patch("t_tech.invest.Client") as mock_client_cls:
            mock_client = MagicMock()
            mock_client.__enter__.return_value = mock_client
            mock_client.users.get_accounts.return_value = MagicMock(accounts=[MagicMock(id="SB1")])
            mock_client.operations.get_portfolio.return_value = fake_portfolio
            mock_client_cls.return_value = mock_client

            with caplog.at_level(logging.WARNING, logger="alphard"):
                result = acct.get_portfolio()

        assert result.cash == Decimal("0")
        warning_msgs = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
        assert any(
            "total_amount_currencies" in m for m in warning_msgs
        ), f"expected WARNING about missing NAV field; got: {warning_msgs}"

    def test_zero_cash_with_persisted_peak_raises_broker_error(self, tmp_path):
        """Persisted peak=100000 + cash=0 must still raise BrokerError.

        Guards against a regression where the cold-start guard is bypassed
        once a peak has been written to disk (peak > 0 would mask the
        zero cash but still violate total_equity > 0).
        """
        from src.broker.tinkoff_account import BrokerError, TinkoffAccount

        peak_dir = tmp_path
        peak_path = peak_dir / "peak_equity_SB1.json"
        peak_path.write_text(json.dumps({"peak_equity": "100000"}), encoding="utf-8")

        with patch.dict(
            os.environ,
            {
                "TINKOFF_SANDBOX_TOKEN": "t.test_token_aaaaaaaaaaaaaaaaaaa",
                "ALPHARD_PEAK_STORE_DIR": str(tmp_path),
            },
        ):
            acct = TinkoffAccount(
                token="t.test_token_aaaaaaaaaaaaaaaaaaa",
                account_id="SB1",
                risk_gate=None,
            )
        assert acct._peak_equity == Decimal("100000"), "peak should be loaded from disk"

        with patch.object(acct, "get_portfolio") as mock_gp:
            mock_gp.return_value = MagicMock(
                cash=Decimal("0"),
                positions=[],
            )
            with __import__("pytest").raises(BrokerError) as exc_info:
                acct._fetch_real_portfolio_state()

        assert "SB1" in str(exc_info.value)


# ────────────────────────────────────────────
# Issue #199 — atomic write + non-destructive corruption recovery
# ────────────────────────────────────────────


class TestPeakEquityAtomicWrite:
    """Issue #199 — atomic _save_peak_equity + .bak fallback + non-destructive
    corruption recovery. The peak file is the single source of truth for the
    RISK_DD guard across process restarts; a SIGKILL, Docker healthcheck kill,
    or disk-full mid-write must NOT silently disarm the guard.
    """

    def test_save_uses_atomic_rename(self, tmp_path):
        """The primary write must go through tmp + os.replace, not a
        raw truncate-then-write. After the save, no ``.tmp`` file may
        remain in the peak store dir.
        """
        peak_dir = tmp_path
        acct = _make_tinkoff_account_with_peak_dir(str(peak_dir))
        with patch.object(acct, "get_portfolio") as mock_gp:
            mock_gp.return_value = MagicMock(cash=Decimal("100000"), positions=[])
            acct._fetch_real_portfolio_state()
        # Primary file exists
        peak_file = Path(str(peak_dir)) / "peak_equity_SB1.json"
        assert peak_file.exists()
        # No leftover .tmp from the atomic rename
        leftover = list(Path(str(peak_dir)).glob("*.tmp"))
        assert leftover == [], f"leftover tmp files: {leftover}"

    def test_save_writes_bak_before_overwriting_primary(self, tmp_path):
        """Each save must mirror the previous-good primary into ``.bak``
        before overwriting. After two saves (100k then 150k), ``.bak``
        should hold 100k (the value before the second save) and the
        primary should hold 150k.
        """
        peak_dir = tmp_path
        acct = _make_tinkoff_account_with_peak_dir(str(peak_dir))
        with patch.object(acct, "get_portfolio") as mock_gp:
            mock_gp.return_value = MagicMock(cash=Decimal("100000"), positions=[])
            acct._fetch_real_portfolio_state()
            mock_gp.return_value = MagicMock(cash=Decimal("150000"), positions=[])
            acct._fetch_real_portfolio_state()
        primary = Path(str(peak_dir)) / "peak_equity_SB1.json"
        bak = Path(str(peak_dir)) / "peak_equity_SB1.json.bak"
        assert primary.exists()
        assert bak.exists(), "second save must have mirrored primary into .bak"
        primary_data = json.loads(primary.read_text())
        bak_data = json.loads(bak.read_text())
        assert primary_data["peak_equity"] == "150000"
        # .bak mirrors the value BEFORE the second save, i.e. 100000.
        assert bak_data["peak_equity"] == "100000"

    def test_save_does_not_create_bak_on_cold_start(self, tmp_path):
        """On the very first save (cold start, no prior primary file)
        there is no previous-good to mirror — ``.bak`` should not be
        created (no spurious empty file).
        """
        peak_dir = tmp_path
        acct = _make_tinkoff_account_with_peak_dir(str(peak_dir))
        with patch.object(acct, "get_portfolio") as mock_gp:
            mock_gp.return_value = MagicMock(cash=Decimal("100000"), positions=[])
            acct._fetch_real_portfolio_state()
        bak = Path(str(peak_dir)) / "peak_equity_SB1.json.bak"
        assert not bak.exists(), "cold-start must not create .bak"

    def test_corrupt_primary_recovers_from_bak(self, tmp_path):
        """If the primary is corrupt, the loader must fall back to
        the .bak file (last-known-good) rather than silently reset to 0.
        """
        peak_dir = tmp_path
        primary = Path(str(peak_dir)) / "peak_equity_SB1.json"
        bak = Path(str(peak_dir)) / "peak_equity_SB1.json.bak"
        primary.write_text("not valid json {{", encoding="utf-8")
        bak.write_text(json.dumps({"peak_equity": "175000"}), encoding="utf-8")

        acct = _make_tinkoff_account_with_peak_dir(str(peak_dir))
        assert acct._peak_equity == Decimal("175000"), "loader must recover from .bak when primary is corrupt"
        # Corrupt primary was renamed aside, not silently overwritten
        corrupt_files = list(Path(str(peak_dir)).glob("peak_equity_SB1.json.corrupt-*"))
        assert len(corrupt_files) == 1, f"corrupt primary must be renamed aside, found {corrupt_files}"
        # Original corrupt file no longer at primary path
        assert not primary.exists()

    def test_corrupt_primary_with_no_bak_returns_zero_with_forensic(self, tmp_path):
        """When BOTH primary and .bak are corrupt, fall back to 0 with
        both files renamed aside for forensics.
        """
        peak_dir = tmp_path
        primary = Path(str(peak_dir)) / "peak_equity_SB1.json"
        bak = Path(str(peak_dir)) / "peak_equity_SB1.json.bak"
        primary.write_text("garbage", encoding="utf-8")
        bak.write_text("also garbage", encoding="utf-8")

        acct = _make_tinkoff_account_with_peak_dir(str(peak_dir))
        assert acct._peak_equity == Decimal("0")
        # Both files moved aside (primary forensics + .bak forensics)
        corrupt_files = sorted(Path(str(peak_dir)).glob("peak_equity_SB1.json*.corrupt-*"))
        # One for primary + one for .bak
        assert len(corrupt_files) >= 2, f"expected 2+ corrupt-forensic files, got {corrupt_files}"

    def test_good_primary_does_not_leave_corrupt_files(self, tmp_path):
        """A successful primary load must prune any stale
        ``.corrupt-*`` forensic file from a previous recovery.
        """
        peak_dir = tmp_path
        # Simulate leftover forensic from a previous recovery
        leftover = Path(str(peak_dir)) / "peak_equity_SB1.json.corrupt-1700000000"
        leftover.write_text("stale evidence", encoding="utf-8")
        primary = Path(str(peak_dir)) / "peak_equity_SB1.json"
        primary.write_text(json.dumps({"peak_equity": "100000"}), encoding="utf-8")

        acct = _make_tinkoff_account_with_peak_dir(str(peak_dir))
        assert acct._peak_equity == Decimal("100000")
        # Forensics file pruned after a clean load
        assert not leftover.exists(), "successful load must prune stale forensic"

    def test_save_peak_equity_survives_disk_full(self, tmp_path):
        """Issue #199 acceptance: simulate a write failure mid-write
        (e.g. disk-full, which raises OSError, NOT KeyboardInterrupt)
        by monkeypatching ``json.dump`` to raise OSError after the
        file is opened. The best-effort outer try/except must catch
        it, the primary file must remain unchanged (no truncation),
        and no .tmp file may be left behind.
        """
        peak_dir = tmp_path
        acct = _make_tinkoff_account_with_peak_dir(str(peak_dir))
        with patch.object(acct, "get_portfolio") as mock_gp:
            mock_gp.return_value = MagicMock(cash=Decimal("100000"), positions=[])
            acct._fetch_real_portfolio_state()
        # Pre-crash state: primary has 100000
        primary = Path(str(peak_dir)) / "peak_equity_SB1.json"
        pre_crash = primary.read_text()

        # Simulate disk-full mid-write (OSError, not KI)
        with patch.object(acct, "_peak_equity", Decimal("200000")):

            def crashing_dump(obj, fh, *args, **kwargs):  # noqa: ANN001
                # Write a few bytes to simulate partial write, then
                # fail with disk-full (OSError, caught by best-effort).
                fh.write('{"peak_equity": "200000"')  # incomplete
                raise OSError(28, "No space left on device")

            with patch("src.broker.tinkoff_account.json.dump", side_effect=crashing_dump):
                # Must NOT propagate — best-effort catch
                acct._save_peak_equity()

        # Primary must be unchanged from the pre-crash state because
        # the atomic-rename never completed (tmp + os.replace never
        # reached). Old "open('w')" code would have truncated to 0
        # here and then crashed on the partial-write.
        post_crash = primary.read_text()
        assert post_crash == pre_crash, "atomic write must leave primary intact when json.dump " "raises mid-write"
        # And no stale .tmp file is left behind
        leftover = list(Path(str(peak_dir)).glob("*.tmp"))
        assert leftover == [], f"failure must not leave .tmp behind: {leftover}"

    def test_load_peak_equity_recovers_from_simulated_crash(self, tmp_path):
        """Issue #199 acceptance: simulate a SIGKILL mid-write by
        leaving the primary at 0 bytes (the classic failure mode of
        the old ``open('w') + json.dump`` code). Recovery on next
        load must NOT silently reset to 0 — must use .bak if present,
        or fall back to 0 only if both primary and .bak are missing.
        """
        peak_dir = tmp_path
        primary = Path(str(peak_dir)) / "peak_equity_SB1.json"
        bak = Path(str(peak_dir)) / "peak_equity_SB1.json.bak"
        # Mid-write crash left primary at 0 bytes; .bak survived.
        primary.write_bytes(b"")
        bak.write_text(json.dumps({"peak_equity": "210000"}), encoding="utf-8")

        acct = _make_tinkoff_account_with_peak_dir(str(peak_dir))
        assert acct._peak_equity == Decimal("210000"), (
            "loader must recover last-known-good from .bak after a " "zero-byte primary caused by mid-write crash"
        )
