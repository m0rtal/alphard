"""Tests for the Coordinator stub (Phase 1.5)."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest

from src.coordinator import (
    Coordinator,
    CoordinatorConfig,
    CoordinatorSide,
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
    """Build CoordinatorConfig; kwargs override defaults. Type is Any because
    each field is heterogeneous (str / CoordinatorSide / Decimal / MagicMock / bool)."""
    kwargs: dict[str, object] = {
        "ticker": "SBER",
        "side": CoordinatorSide.BUY,
        "quantity": Decimal("1"),
        "limit_price": Decimal("100"),
        "risk_limits": _limits(),
        "portfolio_equity": Decimal("1000000"),
        "portfolio_cash": Decimal("1000000"),
        "portfolio_peak": Decimal("1000000"),
        "live_trading": False,  # hard default
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
        assert cfg.live_trading is False  # hard default


# -----------------------------------------------------------------------------
# Coordinator.run_once() — fetch stage
# -----------------------------------------------------------------------------


class TestCoordinatorFetchStage:
    def test_fetch_failure_short_circuits_to_done(self) -> None:
        """If Tinkoff raises, pipeline stops immediately and returns FETCH_ERROR.

        Stages list stays empty (FETCH didn't succeed) but the error is recorded.
        """
        with patch.object(
            Coordinator,
            "_fetch",
            side_effect=RuntimeError("Tinkoff unreachable"),
        ):
            coord = Coordinator(_config())
            result = coord.run_once()
        # FETCH itself failed — not recorded as "completed"
        assert result.stages_completed == ()
        assert result.bars_loaded == 0
        assert "FETCH_ERROR" in result.risk_violations
        assert result.audit_log_id is None

    def test_fetch_empty_returns_zero_bars(self) -> None:
        with patch.object(Coordinator, "_fetch", return_value=[]):
            coord = Coordinator(_config())
            result = coord.run_once()
        assert result.bars_loaded == 0


# -----------------------------------------------------------------------------
# Coordinator.run_once() — risk stage
# -----------------------------------------------------------------------------


class TestCoordinatorRiskStage:
    def test_risk_allowed_continues_to_execute(self) -> None:
        with (
            patch.object(Coordinator, "_fetch", return_value=[_bar()]),
            patch.object(Coordinator, "_validate", return_value=True),
            patch.object(Coordinator, "_risk_check", return_value=(True, ())),
            patch.object(Coordinator, "_execute", return_value="REJECTED_LIVE_TRADING_FALSE"),
            patch.object(Coordinator, "_audit", return_value=None),
        ):
            coord = Coordinator(_config())
            result = coord.run_once()
        assert result.risk_allowed is True
        assert result.broker_status == "REJECTED_LIVE_TRADING_FALSE"

    def test_risk_denied_blocks_execute(self) -> None:
        with (
            patch.object(Coordinator, "_fetch", return_value=[_bar()]),
            patch.object(Coordinator, "_validate", return_value=True),
            patch.object(Coordinator, "_risk_check", return_value=(False, ("RISK_DD: 12% > 10%",))),
            patch.object(Coordinator, "_execute", return_value="REJECTED_RISK_GATE"),
            patch.object(Coordinator, "_audit", return_value=None),
        ):
            coord = Coordinator(_config())
            result = coord.run_once()
        assert result.risk_allowed is False
        assert "RISK_DD" in result.risk_violations[0]
        assert result.broker_status == "REJECTED_RISK_GATE"


# -----------------------------------------------------------------------------
# Coordinator.run_once() — execute stage (LIVE_TRADING gate)
# -----------------------------------------------------------------------------


class TestCoordinatorExecuteStage:
    def test_live_trading_false_refuses_every_order(self) -> None:
        """The hard Phase 1 guarantee — even with risk approval, no order if LIVE_TRADING=false."""
        cfg = _config(live_trading=False)
        with (
            patch.object(Coordinator, "_fetch", return_value=[_bar()]),
            patch.object(Coordinator, "_validate", return_value=True),
            patch.object(Coordinator, "_risk_check", return_value=(True, ())),
            patch.object(Coordinator, "_audit", return_value=None),
        ):
            coord = Coordinator(cfg)
            result = coord.run_once()
        assert result.broker_status == "REJECTED_LIVE_TRADING_FALSE"
        assert result.decided is True  # stage reached, but rejected at gate

    def test_live_trading_true_with_risk_calls_execute(self) -> None:
        """If LIVE_TRADING=true and risk=allowed, _execute() is called and returned."""
        cfg = _config(live_trading=True)
        with (
            patch.object(Coordinator, "_fetch", return_value=[_bar()]),
            patch.object(Coordinator, "_validate", return_value=True),
            patch.object(Coordinator, "_risk_check", return_value=(True, ())),
            patch.object(Coordinator, "_execute", return_value="FILLED"),
            patch.object(Coordinator, "_audit", return_value=None),
        ):
            coord = Coordinator(cfg)
            result = coord.run_once()
        assert result.broker_status == "FILLED"

    def test_live_trading_true_with_risk_denied_blocks(self) -> None:
        cfg = _config(live_trading=True)
        with (
            patch.object(Coordinator, "_fetch", return_value=[_bar()]),
            patch.object(Coordinator, "_validate", return_value=True),
            patch.object(Coordinator, "_risk_check", return_value=(False, ("RISK_DD: x",))),
            patch.object(Coordinator, "_execute", return_value="REJECTED_RISK_GATE"),
            patch.object(Coordinator, "_audit", return_value=None),
        ):
            coord = Coordinator(cfg)
            result = coord.run_once()
        assert result.broker_status == "REJECTED_RISK_GATE"


# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------


def _bar() -> MagicMock:
    """Minimal OHLCVRow stand-in (MagicMock) for fetch stage."""
    today = date.today()
    bar = MagicMock()
    bar.ts = today - timedelta(days=1)
    bar.open = Decimal("100")
    bar.high = Decimal("101")
    bar.low = Decimal("99")
    bar.close = Decimal("100.5")
    bar.volume = Decimal("1000")
    return bar
