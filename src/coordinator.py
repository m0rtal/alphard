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
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Any

from src.risk.gate import PortfolioState, RiskGate, TradeIntent

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

    # Maximum wall-clock seconds between the RISK check and the broker
    # call. Beyond this window, the portfolio state captured at risk
    # time is considered stale and the trade is blocked. Tunable so
    # tests can use a generous value while production uses a tight one.
    toctou_max_seconds: float = 0.100


# Issue #99: alphard-internal refusal markers returned by _execute() when
# the pipeline blocks before/at the broker. These do NOT count as a
# "decided" trade — we never put an order on the wire. Real broker
# outcomes (FILLED, NEW, REJECTED_BY_EXCHANGE, ...) are NOT in this set
# because the broker itself answered.
_LOCAL_REJECTIONS: frozenset[str] = frozenset(
    {
        "REJECTED_LIVE_TRADING_FALSE",
        "REJECTED_RISK_GATE",
    }
)


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
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def decided(self) -> bool:
        """True iff the pipeline produced a real broker response.

        Issue #99: previously this property returned True for any run that
        reached the DONE stage, including paths where the broker rejected
        (LIVE_TRADING=false, RISK_GATE, broker ERROR). The audit log then
        conflates "we tried to trade" with "we chose to trade", inflating
        downstream "trades decided" metrics by every rejected run.

        Semantics: a real decision means the broker responded with an
        outcome that is not an alphard-internal refusal. Local refusals
        (``REJECTED_LIVE_TRADING_FALSE``, ``REJECTED_RISK_GATE``) and
        broker submit errors (``ERROR:<Exc>``) all mean "we did not
        actually trade" — the audit row should record ``decided=False``.

        Real broker outcomes include FILLED, NEW, PARTIALLY_FILLED, and
        exchange-level rejections (``REJECTED_BY_EXCHANGE``) — those are
        decisions: we put an order on the wire and got an answer back.
        """
        bs = self.broker_status
        if bs is None:
            return False
        if bs.startswith("ERROR"):
            return False
        # Local refusal markers (alphard-internal, not the broker):
        if bs in _LOCAL_REJECTIONS:
            return False
        return True

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
        # Wall-clock captured at the end of the RISK stage. The TOCTOU
        # guard in run_once() compares this against `time.monotonic()` to
        # decide whether the gap since risk assessment is too wide.
        self._risk_completed_at: float | None = None
        # Issue #26: build a single RiskGate instance and share it with
        # the broker so order placement doesn't fail with `risk_gate is
        # None`. The gate is stateless (RiskLimits are deterministic), so
        # constructing it once per Coordinator is safe.
        self._gate = RiskGate(limits=self.config.risk_limits)

    def _validate_state_for_execute(self) -> bool:
        """TOCTOU guard: confirm elapsed time since the RISK stage is
        within `toctou_max_seconds`. Beyond that window, portfolio
        state captured by the risk check is stale and the trade is
        blocked. We use `time.monotonic()` (not `time.time()`) so that
        clock skew / NTP corrections cannot widen the apparent window.
        """
        if self._risk_completed_at is None:
            # No risk stage was completed (defensive — should not happen
            # because run_once only calls this after a successful RISK).
            return False
        elapsed = time.monotonic() - self._risk_completed_at
        return elapsed <= self.config.toctou_max_seconds

    def run_once(self) -> PipelineResult:
        """Execute the pipeline once. Returns a structured result.

        Fail-safe contract (issue #15):
          * If VALIDATE raises → block (do NOT proceed with unvalidated data).
          * If RISK raises → block (do NOT call broker).
          * If TOCTOU detected between RISK and EXECUTE → block.

        Any blocked run returns a fully-formed PipelineResult with the
        appropriate `risk_violations` marker so downstream consumers
        (AUDIT log, alerts) can distinguish "blocked" from "no decision".
        """
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

        # Stage 2: VALIDATE — quality gate.
        # Fail-safe: if gate RAISES, block (do NOT trust unvalidated data).
        # If gate returns False (HIGH/CRITICAL), block. Either way,
        # risk_allowed stays False.
        try:
            validation = self._validate(bars)
        except Exception as exc:
            logger.error("VALIDATE failed for %s: %s", self.config.ticker, exc)
            stages.append(PipelineStage.VALIDATE)
            stages.append(PipelineStage.SKIPPED)
            return PipelineResult(
                config=self.config,
                stages_completed=tuple(stages),
                bars_loaded=bars_loaded,
                risk_allowed=False,
                risk_violations=("VALIDATE_EXCEPTION",),
                broker_status=None,
                audit_log_id=self._audit(stages, bars_loaded, False, ("VALIDATE_EXCEPTION",), None),
            )

        stages.append(PipelineStage.VALIDATE)
        if not validation:
            logger.warning("VALIDATE skipped %s: HIGH/CRITICAL", self.config.ticker)
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

        # Stage 3: RISK — gate check.
        # Fail-safe: if gate RAISES, block the broker call. The previous
        # implementation let the exception propagate past the try/except
        # and then continued to EXECUTE — that's a fail-open bug. We now
        # record `RISK_EXCEPTION` and short-circuit before _execute().
        try:
            risk_allowed, risk_violations = self._risk_check()
            stages.append(PipelineStage.RISK)
            # Stamp the wall-clock at the end of the RISK stage. The TOCTOU
            # guard below compares against this value to decide whether the
            # portfolio state captured by the risk check is still fresh.
            self._risk_completed_at = time.monotonic()
        except Exception as exc:
            logger.error("RISK failed for %s: %s", self.config.ticker, exc)
            stages.append(PipelineStage.SKIPPED)
            return PipelineResult(
                config=self.config,
                stages_completed=tuple(stages),
                bars_loaded=bars_loaded,
                risk_allowed=False,
                risk_violations=("RISK_EXCEPTION",),
                broker_status=None,
                audit_log_id=self._audit(stages, bars_loaded, False, ("RISK_EXCEPTION",), None),
            )

        # Stage 3.5: TOCTOU re-validation.
        # The risk verdict was computed against the portfolio state
        # captured `toctou_max_seconds` (or less) ago. If more time has
        # elapsed, a fill could have arrived in the meantime, making the
        # verdict stale. Block the trade.
        if not self._validate_state_for_execute():
            logger.error(
                "TOCTOU: state stale between RISK and EXECUTE for %s (window=%ss)",
                self.config.ticker,
                self.config.toctou_max_seconds,
            )
            stages.append(PipelineStage.SKIPPED)
            return PipelineResult(
                config=self.config,
                stages_completed=tuple(stages),
                bars_loaded=bars_loaded,
                risk_allowed=False,
                risk_violations=("TOCTOU_STATE_STALE",),
                broker_status=None,
                audit_log_id=self._audit(stages, bars_loaded, False, ("TOCTOU_STATE_STALE",), None),
            )

        # Stage 4: EXECUTE — broker (only if LIVE_TRADING && risk_allowed).
        broker_status = self._execute(risk_allowed)
        if broker_status is not None:
            stages.append(PipelineStage.EXECUTE)

        # Stage 5: AUDIT
        audit_log_id = self._audit(
            stages,
            bars_loaded,
            risk_allowed,
            risk_violations,
            broker_status,
        )
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
        from datetime import timedelta

        loader = TinkoffInvestDataLoader()
        end = date.today()
        # Issue #26: respect CoordinatorConfig.fetch_lookback_days (default 5*365).
        # Hardcoded timedelta(days=2) previously left only ~3 bars reaching VALIDATE,
        # which made the gate block on insufficient_history_rows.
        start = end - timedelta(days=self.config.fetch_lookback_days)
        return loader.fetch_ohlcv(self.config.ticker, start, end)

    def _validate(self, bars: list[Any]) -> bool:
        """Return True if data passes quality gate (HIGH/CRITICAL → False).

        Note: this method itself does NOT swallow exceptions — those
        are caught in run_once() at the call site and converted into a
        fail-safe `VALIDATE_EXCEPTION` block. The historical
        implementation had `except Exception: return True` here, which
        is fail-OPEN: a broken gate silently allowed unvalidated data
        downstream. The check is intentionally fragile here so that
        any gate crash becomes a loud, blocking failure upstream.
        """
        if not bars:
            return False
        from src.data.quality.ingestion_gate import Bar, check_ingestion, IngestionParams

        # Issue #102: ``bars`` may now carry multiple rows per (ticker, ts)
        # because the Phase 2.6 step 2 multi-source schema allows one
        # OHLCVRow per (ticker, ts, source). The ingestion gate's
        # gap/coverage/outlier analysis treats every Bar as an independent
        # observation, so two Bars with the same primary_key (ts) would
        # inflate the row count, double-count volume, and let a ticker with
        # only 126 unique dates squeak past min_history_rows=252 because
        # two sources each contribute a copy of the same date.
        # Dedup by primary_key, keeping the FIRST row encountered (the
        # ordering from query_ohlcv is ``ORDER BY ts, source`` so the
        # canonical ``tkf`` source arrives first when present). This
        # preserves the single-source pre-Phase-2.6 contract for the
        # gate's row-counting invariants.
        seen_ts: set[date] = set()
        bar_list: list[Bar] = []
        for b in bars:
            if b.ts in seen_ts:
                continue
            seen_ts.add(b.ts)
            bar_list.append(
                Bar(
                    primary_key=b.ts,
                    open=float(b.open),
                    high=float(b.high),
                    low=float(b.low),
                    close=float(b.close),
                    volume=int(b.volume),
                )
            )
        report = check_ingestion(self.config.ticker, bar_list, params=IngestionParams())
        worst = report.worst_severity()
        if worst is None:
            return True
        return worst.value not in ("CRITICAL", "HIGH")

    def _risk_check(self) -> tuple[bool, tuple[str, ...]]:
        # Issue #26: reuse the gate created in __init__ so the same
        # RiskLimits are applied to both the gate stage and the broker
        # stage. Previously each stage constructed its own gate, which
        # caused drift if the config changed between stages.
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
        decision = self._gate.evaluate(intent, state)
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

            # Issue #26: pass the already-built RiskGate into TinkoffAccount
            # so the broker-level fail-safe check has a real gate, not None.
            account = TinkoffAccount.from_env(risk_gate=self._gate)  # type: ignore[attr-defined]
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
