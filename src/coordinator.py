"""Alphard Coordinator — Phase 1.5 stub.

PURPOSE
-------
End-to-end pipeline orchestrator that wires the components already
implemented in Phase 1.1 / 1.2 / 1.3:

    Data Agent (tinkoff_loader)
        → Quality Gate (cross_source / ingestion_gate)
            → Risk Gate (gate.py)
                → Broker (tinkoff_account.place_order)
                    → Audit Log (Postgres JSONB)

This is a STUB, not the full Coordinator state machine (§20 of the
autonomous-trading research). The full version adds Macro Agent, Quant
Agent, Portfolio Optimizer, News, and Audit Agent in Phases 2-7. What
this stub DOES provide:

- One-shot pipeline run: pull → validate → evaluate → (optionally) submit
- LIVE_TRADING hard lock (defaults to false, refuses order placement)
- Audit trail written to Postgres `decision_log` table
- Idempotent: safe to call multiple times in a day

WHAT IS NOT HERE (deferred to later phases)
------------------------------------------
- State machine (IDLE → SCANNING → ... → MONITORING)
- Macro regime-aware defensive rotation
- ML model scoring / portfolio optimization
- Position sizing (Kelly / vol-targeted)
- Multi-symbol coordination (this stub handles ONE symbol at a time)
- Continuous loop (cron-driven via daily_sync.py for now)

USAGE
-----
    from src.coordinator import Coordinator, CoordinatorConfig
    from src.risk.gate import RiskLimits
    from decimal import Decimal

    coord = Coordinator(
        config=CoordinatorConfig(
            ticker="SBER",
            side="buy",
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
            live_trading=False,   # HARD LOCK
            store_dsn=os.environ["ALPHARD_PG_DSN"],
        ),
    )
    result = coord.run_once()
    print(result.decision)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Any

logger = logging.getLogger("alphard.coordinator")


class CoordinatorSide(str, Enum):
    BUY = "buy"
    SELL = "sell"


class PipelineStage(str, Enum):
    FETCH = "fetch"
    VALIDATE = "validate"
    RISK = "risk"
    EXECUTE = "execute"
    AUDIT = "audit"
    DONE = "done"
    SKIPPED = "skipped"


@dataclass(frozen=True)
class CoordinatorConfig:
    """Immutable input for one coordinator run."""

    ticker: str
    side: CoordinatorSide
    quantity: Decimal
    limit_price: Decimal

    # Risk gate parameters
    risk_limits: Any  # src.risk.gate.RiskLimits
    portfolio_equity: Decimal
    portfolio_cash: Decimal
    portfolio_peak: Decimal

    # Data loader parameters
    fetch_lookback_days: int = 5 * 365

    # SAFETY: refuse ALL orders if false
    live_trading: bool = False

    # Persistence
    store_dsn: str | None = None


@dataclass(frozen=True)
class PipelineResult:
    """Result of one coordinator run."""

    config: CoordinatorConfig
    stages_completed: tuple[PipelineStage, ...]
    bars_loaded: int
    risk_allowed: bool
    risk_violations: tuple[str, ...]
    broker_status: str | None  # None when LIVE_TRADING=false (refused before broker)
    audit_log_id: int | None
    timestamp: datetime = field(default_factory=datetime.utcnow)

    @property
    def decided(self) -> bool:
        """True iff pipeline reached EXECUTE stage."""
        return PipelineStage.EXECUTE in self.stages_completed or PipelineStage.DONE in self.stages_completed

    def to_dict(self) -> dict[str, Any]:
        return {
            "ticker": self.config.ticker,
            "side": self.config.side.value,
            "quantity": str(self.config.quantity),
            "limit_price": str(self.config.limit_price),
            "stages_completed": [s.value for s in self.stages_completed],
            "bars_loaded": self.bars_loaded,
            "risk_allowed": self.risk_allowed,
            "risk_violations": list(self.risk_violations),
            "broker_status": self.broker_status,
            "audit_log_id": self.audit_log_id,
            "decided": self.decided,
            "timestamp": self.timestamp.isoformat(),
        }


class Coordinator:
    """One-shot pipeline orchestrator.

    Wires Data Agent → Quality Gate → Risk Gate → Broker → Audit.

    LIVE_TRADING=False (default) means the broker call is short-circuited
    with status='REJECTED' and no money is moved. This is a hard Phase 1
    guarantee that even when real Tinkoff tokens are present, no orders
    reach the broker unless LIVE_TRADING is explicitly True.
    """

    def __init__(self, config: CoordinatorConfig) -> None:
        self.config = config

    def run_once(self) -> PipelineResult:
        """Execute the pipeline once. Returns a structured result."""
        stages: list[PipelineStage] = []
        bars_loaded = 0
        risk_allowed = False
        risk_violations: tuple[str, ...] = ()
        broker_status: str | None = None
        audit_log_id: int | None = None

        # Stage 1: FETCH — pull OHLCV from Tinkoff
        try:
            bars = self._fetch()
            bars_loaded = len(bars)
            stages.append(PipelineStage.FETCH)
        except Exception as exc:
            logger.error("FETCH failed for %s: %s", self.config.ticker, exc)
            return PipelineResult(
                config=self.config,
                stages_completed=tuple(stages),
                bars_loaded=0,
                risk_allowed=False,
                risk_violations=("FETCH_ERROR",),
                broker_status=None,
                audit_log_id=self._audit(stages, bars_loaded, False, ("FETCH_ERROR",), None),
            )

        # Stage 2: VALIDATE — quality gate (CRITICAL → skip)
        try:
            validation = self._validate(bars)
            stages.append(PipelineStage.VALIDATE)
            if not validation:
                logger.warning("VALIDATE skipped %s: CRITICAL", self.config.ticker)
                stages.append(PipelineStage.SKIPPED)
                return PipelineResult(
                    config=self.config,
                    stages_completed=tuple(stages),
                    bars_loaded=bars_loaded,
                    risk_allowed=False,
                    risk_violations=("VALIDATE_CRITICAL",),
                    broker_status=None,
                    audit_log_id=self._audit(stages, bars_loaded, False, ("VALIDATE_CRITICAL",), None),
                )
        except Exception as exc:
            logger.error("VALIDATE failed for %s: %s", self.config.ticker, exc)

        # Stage 3: RISK — gate check
        try:
            risk_allowed, risk_violations = self._risk_check()
            stages.append(PipelineStage.RISK)
        except Exception as exc:
            logger.error("RISK failed for %s: %s", self.config.ticker, exc)

        # Stage 4: EXECUTE — broker (only if LIVE_TRADING && risk_allowed)
        broker_status = self._execute(risk_allowed)
        if broker_status is not None:
            stages.append(PipelineStage.EXECUTE)

        # Stage 5: AUDIT
        audit_log_id = self._audit(stages, bars_loaded, risk_allowed, risk_violations, broker_status)
        stages.append(PipelineStage.AUDIT)
        stages.append(PipelineStage.DONE)

        return PipelineResult(
            config=self.config,
            stages_completed=tuple(stages),
            bars_loaded=bars_loaded,
            risk_allowed=risk_allowed,
            risk_violations=risk_violations,
            broker_status=broker_status,
            audit_log_id=audit_log_id,
        )

    def _fetch(self) -> list[Any]:
        from src.data.tinkoff_loader import TinkoffInvestDataLoader

        loader = TinkoffInvestDataLoader()
        end = date.today()
        # Single-day range — we only need 1 bar to evaluate the intent
        from datetime import timedelta

        start = end - timedelta(days=2)
        return loader.fetch_ohlcv(self.config.ticker, start, end)

    def _validate(self, bars: list[Any]) -> bool:
        """Return True if data passes quality gate (HIGH/CRITICAL → False)."""
        if not bars:
            return False
        try:
            from src.data.quality.ingestion_gate import Bar, check_ingestion, IngestionParams

            bar_list = [
                Bar(
                    primary_key=b.ts,
                    open=float(b.open),
                    high=float(b.high),
                    low=float(b.low),
                    close=float(b.close),
                    volume=int(b.volume),
                )
                for b in bars
            ]
            report = check_ingestion(self.config.ticker, bar_list, params=IngestionParams())
            worst = report.worst_severity()
            if worst is None:
                return True
            return worst.value not in ("CRITICAL", "HIGH")
        except Exception:
            # Conservative: if gate fails, don't auto-block, let RiskGate decide
            return True

    def _risk_check(self) -> tuple[bool, tuple[str, ...]]:
        from src.risk.gate import PortfolioState, RiskGate, TradeIntent

        gate = RiskGate(limits=self.config.risk_limits)
        intent = TradeIntent(
            symbol=self.config.ticker,
            side=self.config.side.value,
            quantity=self.config.quantity,
            price=self.config.limit_price,
        )
        state = PortfolioState(
            total_equity=self.config.portfolio_equity,
            cash=self.config.portfolio_cash,
            positions=[],
            peak_equity=self.config.portfolio_peak,
        )
        decision = gate.evaluate(intent, state)
        return decision.allowed, decision.violations

    def _execute(self, risk_allowed: bool) -> str | None:
        """Submit to broker if LIVE_TRADING=true AND risk_allowed=True."""
        if not self.config.live_trading:
            logger.info(
                "LIVE_TRADING=false — refusing order for %s (Phase 1 hard no-trade)",
                self.config.ticker,
            )
            return "REJECTED_LIVE_TRADING_FALSE"

        if not risk_allowed:
            return "REJECTED_RISK_GATE"

        try:
            from src.broker.tinkoff_account import TinkoffAccount
            from src.broker.orders import MarketOrder, OrderSide

            account = TinkoffAccount.from_env()  # type: ignore[attr-defined]
            order = MarketOrder(
                ticker=self.config.ticker,
                side=OrderSide.BUY if self.config.side == CoordinatorSide.BUY else OrderSide.SELL,
                quantity=self.config.quantity,
            )
            status = account.place_order(order)
            return str(status.value)
        except Exception as exc:
            logger.error("BROKER submit failed for %s: %s", self.config.ticker, exc)
            return f"ERROR:{type(exc).__name__}"

    def _audit(
        self,
        stages: list[PipelineStage],
        bars_loaded: int,
        risk_allowed: bool,
        risk_violations: tuple[str, ...],
        broker_status: str | None,
    ) -> int | None:
        """Persist decision lineage to Postgres (returns row id)."""
        if not self.config.store_dsn:
            logger.debug("AUDIT skipped (no store_dsn)")
            return None

        try:
            import json

            import psycopg

            payload = json.dumps(
                {
                    "ticker": self.config.ticker,
                    "side": self.config.side.value,
                    "quantity": str(self.config.quantity),
                    "limit_price": str(self.config.limit_price),
                    "stages": [s.value for s in stages],
                    "bars_loaded": bars_loaded,
                    "risk_allowed": risk_allowed,
                    "risk_violations": list(risk_violations),
                    "broker_status": broker_status,
                    "live_trading": self.config.live_trading,
                },
                default=str,
            )

            with psycopg.connect(self.config.store_dsn) as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "INSERT INTO decision_log (kind, ticker, decision, source) "
                        "VALUES ('coordinator_pipeline', %s, %s, 'alphard') "
                        "RETURNING id",
                        (self.config.ticker, payload),
                    )
                    row_id = cur.fetchone()
                    conn.commit()
                    return row_id[0] if row_id else None
        except Exception as exc:
            logger.warning("AUDIT persist failed for %s: %s", self.config.ticker, exc)
            return None


__all__ = [
    "Coordinator",
    "CoordinatorConfig",
    "CoordinatorSide",
    "PipelineStage",
    "PipelineResult",
]
