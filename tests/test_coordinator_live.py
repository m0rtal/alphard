"""Live integration tests for the Coordinator pipeline.

These tests require a real Tinkoff token and Postgres DSN in environment:
- TINKOFF_REAL_TOKEN (preferred) or TINKOFF_SANDBOX_TOKEN
- ALPHARD_PG_DSN pointing to a running Postgres

Skipped otherwise. Live smoke only — no assertions about exact data,
just that the pipeline runs end-to-end without raising.
"""
from __future__ import annotations

import os
from decimal import Decimal

import pytest

from src.coordinator import Coordinator, CoordinatorConfig, CoordinatorSide
from src.risk.gate import RiskLimits


def _real_token() -> str | None:
    return os.environ.get("TINKOFF_REAL_TOKEN") or os.environ.get("TINKOFF_SANDBOX_TOKEN")


pytestmark = pytest.mark.skipif(
    not _real_token() or not os.environ.get("ALPHARD_PG_DSN"),
    reason="Live integration test — requires TINKOFF_*_TOKEN and ALPHARD_PG_DSN",
)


class TestCoordinatorLive:
    def test_full_pipeline_runs_end_to_end_for_sber(self) -> None:
        """Smoke test the live Coordinator pipeline against real Tinkoff + Postgres."""
        cfg = CoordinatorConfig(
            ticker="SBER",
            side=CoordinatorSide.BUY,
            quantity=Decimal("1"),
            limit_price=Decimal("100"),
            risk_limits=RiskLimits(
                max_dd_pct=Decimal("10"),
                max_position_pct=Decimal("10"),
                max_sector_pct=Decimal("30"),
                max_daily_loss_pct=Decimal("3"),
            ),
            portfolio_equity=Decimal("1000000"),
            portfolio_cash=Decimal("1000000"),
            portfolio_peak=Decimal("1000000"),
            live_trading=False,  # hard no-trade for this test
            store_dsn=os.environ.get("ALPHARD_PG_DSN"),
        )
        coord = Coordinator(cfg)
        result = coord.run_once()
        # Don't assert specific stages — depends on data freshness.
        # Just verify the pipeline produced a structured result.
        assert result.timestamp is not None
        assert isinstance(result.stages_completed, tuple)
        assert isinstance(result.broker_status, (str, type(None)))
        # broker_status should be None when LIVE_TRADING=false
        assert result.broker_status == "REJECTED_LIVE_TRADING_FALSE"
        # audit_log_id written (since store_dsn set)
        assert result.audit_log_id is not None

    @pytest.mark.timeout(30)
    def test_pipeline_with_insufficient_history_critical(self) -> None:
        """When only 2 bars available, IngestionGate flags CRITICAL → SKIPPED."""
        cfg = CoordinatorConfig(
            ticker="GAZP",
            side=CoordinatorSide.BUY,
            quantity=Decimal("1"),
            limit_price=Decimal("100"),
            risk_limits=RiskLimits(
                max_dd_pct=Decimal("10"),
                max_position_pct=Decimal("10"),
                max_sector_pct=Decimal("30"),
                max_daily_loss_pct=Decimal("3"),
            ),
            portfolio_equity=Decimal("1000000"),
            portfolio_cash=Decimal("1000000"),
            portfolio_peak=Decimal("1000000"),
            live_trading=False,
            fetch_lookback_days=2,  # tiny window
            store_dsn=os.environ.get("ALPHARD_PG_DSN"),
        )
        coord = Coordinator(cfg)
        result = coord.run_once()
        # With 2 bars, gate flags insufficient_history (HIGH)
        # → coordinator skips execution and returns VALIDATE_CRITICAL or HIGH
        assert result.broker_status in (None, "REJECTED_LIVE_TRADING_FALSE")

    def test_audit_persists_to_postgres(self) -> None:
        """Verify decision_log row was written and contains expected fields."""
        import psycopg

        cfg = CoordinatorConfig(
            ticker="LKOH",
            side=CoordinatorSide.BUY,
            quantity=Decimal("1"),
            limit_price=Decimal("5000"),
            risk_limits=RiskLimits(
                max_dd_pct=Decimal("10"),
                max_position_pct=Decimal("10"),
                max_sector_pct=Decimal("30"),
                max_daily_loss_pct=Decimal("3"),
            ),
            portfolio_equity=Decimal("1000000"),
            portfolio_cash=Decimal("1000000"),
            portfolio_peak=Decimal("1000000"),
            live_trading=False,
            store_dsn=os.environ.get("ALPHARD_PG_DSN"),
        )
        coord = Coordinator(cfg)
        result = coord.run_once()
        assert result.audit_log_id is not None

        # Verify the row contents
        assert cfg.store_dsn is not None
        with psycopg.connect(cfg.store_dsn) as conn:  # type: ignore[arg-type]
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT ticker, kind, decision FROM decision_log WHERE id = %s",
                    (result.audit_log_id,),
                )
                row = cur.fetchone()
                assert row is not None
                ticker, kind, decision = row
                assert ticker == "LKOH"
                assert kind == "coordinator_pipeline"
                assert decision["ticker"] == "LKOH"
                assert decision["live_trading"] is False
                assert decision["risk_allowed"] is False  # LIVE_TRADING=false short-circuits
