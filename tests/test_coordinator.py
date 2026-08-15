"""Tests for the Coordinator stub (Phase 1.5) — mocked pipeline paths."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest

from src.coordinator import (
    Coordinator,
    CoordinatorConfig,
    CoordinatorSide,
    PipelineStage,
)


# -----------------------------------------------------------------------------
# Fixtures
# -----------------------------------------------------------------------------


def _limits() -> MagicMock:
    return MagicMock(
        max_dd_pct=Decimal("10"),
        max_position_pct=Decimal("10"),
        max_sector_pct=Decimal("30"),
        max_daily_loss_pct=Decimal("3"),
    )


def _config(**overrides: object) -> CoordinatorConfig:
    """Build CoordinatorConfig; kwargs override defaults."""
    kwargs: dict[str, object] = {
        "ticker": "SBER",
        "side": CoordinatorSide.BUY,
        "quantity": Decimal("1"),
        "limit_price": Decimal("100"),
        "risk_limits": _limits(),
        "portfolio_equity": Decimal("1000000"),
        "portfolio_cash": Decimal("1000000"),
        "portfolio_peak": Decimal("1000000"),
        "live_trading": False,
        "store_dsn": None,
    }
    kwargs.update(overrides)
    return CoordinatorConfig(**kwargs)  # type: ignore[arg-type]


# -----------------------------------------------------------------------------
# Config dataclass
# -----------------------------------------------------------------------------


class TestCoordinatorConfig:
    def test_config_is_immutable(self) -> None:
        cfg = _config()
        with pytest.raises(Exception):
            cfg.ticker = "GAZP"  # type: ignore[misc]

    def test_config_holds_all_required_fields(self) -> None:
        cfg = _config()
        assert cfg.ticker == "SBER"
        assert cfg.side == CoordinatorSide.BUY
        assert cfg.quantity == Decimal("1")
        assert cfg.live_trading is False


# -----------------------------------------------------------------------------
# Coordinator.run_once() — full mocked paths
# -----------------------------------------------------------------------------


def _bar() -> MagicMock:
    """Minimal OHLCVRow stand-in."""
    today = date.today()
    bar = MagicMock()
    bar.ts = today - timedelta(days=1)
    bar.open = Decimal("100")
    bar.high = Decimal("101")
    bar.low = Decimal("99")
    bar.close = Decimal("100.5")
    bar.volume = Decimal("1000")
    return bar


class TestCoordinatorFullPipeline:
    """Mock the entire pipeline to test internal coordination."""

    def test_fetch_failure_short_circuits_with_FETCH_ERROR(self) -> None:
        """FETCH fails → pipeline returns immediately with FETCH_ERROR violation."""
        with patch.object(Coordinator, "_fetch", side_effect=RuntimeError("Tinkoff unreachable")):
            result = Coordinator(_config()).run_once()
        assert result.stages_completed == ()
        assert result.bars_loaded == 0
        assert "FETCH_ERROR" in result.risk_violations
        assert result.broker_status is None

    def test_fetch_returns_empty_bars_skips_validation(self) -> None:
        """Empty fetch → VALIDATE returns False → SKIPPED stage."""
        with patch.object(Coordinator, "_fetch", return_value=[]):
            result = Coordinator(_config()).run_once()
        assert result.bars_loaded == 0

    def test_validate_critical_returns_skipped(self) -> None:
        """VALIDATE returns False → SKIPPED, no risk check, no broker call."""
        with (
            patch.object(Coordinator, "_fetch", return_value=[_bar()]),
            patch.object(Coordinator, "_validate", return_value=False),
        ):
            result = Coordinator(_config()).run_once()
        assert PipelineStage.SKIPPED in result.stages_completed
        assert "VALIDATE_CRITICAL" in result.risk_violations
        assert result.risk_allowed is False
        assert result.broker_status is None

    def test_validate_raises_does_not_block_risk_check(self) -> None:
        """If _validate raises, pipeline continues to RISK (conservative)."""
        with (
            patch.object(Coordinator, "_fetch", return_value=[_bar()]),
            patch.object(Coordinator, "_validate", side_effect=RuntimeError("gate fail")),
            patch.object(Coordinator, "_risk_check", return_value=(True, ())),
            patch.object(Coordinator, "_execute", return_value="REJECTED_LIVE_TRADING_FALSE"),
        ):
            result = Coordinator(_config()).run_once()
        assert PipelineStage.RISK in result.stages_completed
        assert result.risk_allowed is True

    def test_risk_allowed_live_trading_false_blocks_at_broker(self) -> None:
        """Risk passes but LIVE_TRADING=false → broker_status REJECTED_LIVE_TRADING_FALSE."""
        with (
            patch.object(Coordinator, "_fetch", return_value=[_bar()]),
            patch.object(Coordinator, "_validate", return_value=True),
            patch.object(Coordinator, "_risk_check", return_value=(True, ())),
        ):
            result = Coordinator(_config(live_trading=False)).run_once()
        assert result.risk_allowed is True
        assert result.broker_status == "REJECTED_LIVE_TRADING_FALSE"

    def test_risk_denied_blocks_broker_live_trading_true(self) -> None:
        """Risk fails AND LIVE_TRADING=true → still blocks at risk gate."""
        with (
            patch.object(Coordinator, "_fetch", return_value=[_bar()]),
            patch.object(Coordinator, "_validate", return_value=True),
            patch.object(Coordinator, "_risk_check", return_value=(False, ("RISK_DD: 12% > 10%",))),
            patch.object(Coordinator, "_execute", return_value="REJECTED_RISK_GATE"),
        ):
            result = Coordinator(_config(live_trading=True)).run_once()
        assert result.risk_allowed is False
        assert "RISK_DD" in result.risk_violations[0]
        assert result.broker_status == "REJECTED_RISK_GATE"

    def test_risk_allowed_live_trading_true_calls_execute(self) -> None:
        """Risk passes AND LIVE_TRADING=true → _execute called, result returned."""
        with (
            patch.object(Coordinator, "_fetch", return_value=[_bar()]),
            patch.object(Coordinator, "_validate", return_value=True),
            patch.object(Coordinator, "_risk_check", return_value=(True, ())),
            patch.object(Coordinator, "_execute", return_value="FILLED"),
        ):
            result = Coordinator(_config(live_trading=True)).run_once()
        assert result.broker_status == "FILLED"

    def test_broker_raises_returns_error_status(self) -> None:
        """If _execute raises (broker unavailable), main loop catches and returns ERROR:..."""
        with (
            patch.object(Coordinator, "_fetch", return_value=[_bar()]),
            patch.object(Coordinator, "_validate", return_value=True),
            patch.object(Coordinator, "_risk_check", return_value=(True, ())),
            patch.object(Coordinator, "_execute", return_value="ERROR:RuntimeError"),
        ):
            result = Coordinator(_config(live_trading=True)).run_once()
        assert result.broker_status == "ERROR:RuntimeError"


# -----------------------------------------------------------------------------
# Coordinator._audit()
# -----------------------------------------------------------------------------


class TestCoordinatorAudit:
    def test_audit_skips_when_no_dsn(self) -> None:
        """No store_dsn → audit returns None silently."""
        coord = Coordinator(_config(store_dsn=None))
        result = coord._audit(
            stages=[PipelineStage.FETCH, PipelineStage.DONE],
            bars_loaded=10,
            risk_allowed=False,
            risk_violations=(),
            broker_status=None,
        )
        assert result is None

    def test_audit_persists_to_postgres(self) -> None:
        """Audit inserts JSONB row, returns id."""
        from unittest.mock import MagicMock

        mock_cursor = MagicMock()
        mock_cursor.fetchone.return_value = (42,)
        mock_conn = MagicMock()
        mock_conn.cursor.return_value.__enter__.return_value = mock_cursor
        mock_psycopg = MagicMock()
        mock_psycopg.connect.return_value.__enter__.return_value = mock_conn

        with patch.dict("sys.modules", {"psycopg": mock_psycopg}):
            coord = Coordinator(_config(store_dsn="postgresql://fake"))
            result = coord._audit(
                stages=[PipelineStage.FETCH, PipelineStage.DONE],
                bars_loaded=10,
                risk_allowed=True,
                risk_violations=(),
                broker_status="FILLED",
            )
        assert result == 42
        mock_psycopg.connect.assert_called_once()


# -----------------------------------------------------------------------------
# Coordinator._fetch()
# -----------------------------------------------------------------------------


class TestCoordinatorFetch:
    def test_fetch_calls_loader_with_short_window(self) -> None:
        """_fetch uses 2-day window to minimise API call payload."""
        with patch("src.data.tinkoff_loader.TinkoffInvestDataLoader") as mock_loader:
            mock_instance = MagicMock()
            mock_instance.fetch_ohlcv.return_value = [_bar()]
            mock_loader.return_value = mock_instance

            coord = Coordinator(_config())
            bars = coord._fetch()

        assert len(bars) == 1
        # Verify 2-day window (not 1825-day default)
        call_args = mock_instance.fetch_ohlcv.call_args
        args = call_args.args  # positional (ticker, start, end)
        assert args[2] - args[1] == timedelta(days=2)


# -----------------------------------------------------------------------------
# Coordinator._validate()
# -----------------------------------------------------------------------------


class TestCoordinatorValidate:
    def test_validate_empty_bars_returns_false(self) -> None:
        """No bars → validation fails (skips pipeline)."""
        coord = Coordinator(_config())
        assert coord._validate([]) is False

    def test_validate_quality_gate_exception_returns_true(self) -> None:
        """If quality gate itself fails, default to allow (conservative)."""
        with patch(
            "src.data.quality.ingestion_gate.check_ingestion",
            side_effect=RuntimeError("gate crash"),
        ):
            coord = Coordinator(_config())
            assert coord._validate([_bar()]) is True


# -----------------------------------------------------------------------------
# Coordinator._risk_check()
# -----------------------------------------------------------------------------


class TestCoordinatorRiskCheck:
    def test_risk_check_returns_decision(self) -> None:
        """Risk check returns (allowed, violations) tuple from gate."""
        with patch("src.risk.gate.RiskGate") as mock_gate_class:
            mock_gate = MagicMock()
            mock_gate.evaluate.return_value = MagicMock(allowed=True, violations=())
            mock_gate_class.return_value = mock_gate

            coord = Coordinator(_config())
            allowed, violations = coord._risk_check()

        assert allowed is True
        assert violations == ()
