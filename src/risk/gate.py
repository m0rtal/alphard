"""
Alphard Risk Agent — Skeleton (Phase 1.0)

PURPOSE
-------
Pure-Python risk gate. Hard limits only. NO ML, NO LLM, NO statistical inference.
This is an architectural skeleton — it compiles, imports, and passes tests,
but contains no real exchange / market / portfolio integrations yet.

DESIGN DECISIONS (documented for Phase 1.3+ extensions)
--------------------------------------------------------
1. Pure Python stdlib + pydantic. No numpy / pandas / scipy / sklearn / torch.
   Reason: risk decisions are the last line of defence before money moves.
   Minimal dependency surface = fewer supply-chain failure modes.

2. Hard limits only. No "soft" / ML-based sizing. Reason: every limit must be
   human-auditable. If you cannot explain a rejection to a regulator in
   one sentence, the limit does not belong here.

3. Fail-safe default: ANY violation => allowed=False. Reason: uncertainty
   must always bias toward NOT trading. There is no cost to skipping a
   trade; there is potentially infinite cost to taking a bad one.

4. Violations are accumulated, not short-circuited. Reason: an operator
   inspecting a rejected trade wants to see EVERY limit that fired, not
   just the first. Phase 1.3 will add structured violation codes (RISK_001,
   RISK_002, ...) — for now plain strings are fine.

5. Pydantic models (not bare dataclasses) for TradeIntent / PortfolioState.
   Reason: validation of incoming data is part of the risk gate's job.
   If a TradeIntent arrives with negative qty or NaN-equivalent, it must
   be rejected before we even run the checks.

6. Sector mapping is a placeholder dict on PortfolioState. Phase 1.3 will
   pull this from a sector registry; Phase 2 from the data agent.

WHAT IS NOT HERE (intentional gaps)
-----------------------------------
- No Tinkoff / MOEX / broker integration.
- No persistence (audit log, state file, DB).
- No async / concurrent evaluation (gate is synchronous and pure).
- No sector registry / instrument metadata service.
- No correlation / VaR / drawdown-trajectory modelling (out of scope: this
  is hard limits only).
- No time-of-day / liquidity / spread checks (Phase 1.3+).
- No kill-switch / circuit-breaker state machine (Phase 2).
- No config loader / env-var wiring (Phase 1.3 will use pydantic-settings).

If a Phase 1.3 feature is needed but missing, the gate's behaviour is
defined as "reject" — i.e. fail-safe.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


class TradeIntent(BaseModel):
    """A single proposed trade to be risk-checked before execution.

    Placeholder schema — fields match the Phase 1.3 contract but no broker
    is wired up yet. `side` is "buy" only for the skeleton; "sell" / "short"
    will be added in Phase 1.3 once Portfolio bookkeeping is real.
    """

    model_config = ConfigDict(extra="forbid", frozen=False)

    symbol: str = Field(..., min_length=1, description="Instrument ticker, e.g. 'SBER'")
    side: str = Field(..., description="'buy' (placeholder: only 'buy' supported in skeleton)")
    quantity: Decimal = Field(..., ge=Decimal("0"), description="Number of shares/lots; >= 0")
    price: Decimal = Field(..., gt=Decimal("0"), description="Limit/market price; > 0")
    sector: str | None = Field(default=None, description="Sector tag, e.g. 'energy'. Placeholder.")

    @field_validator("symbol")
    @classmethod
    def _strip_symbol(cls, v: str) -> str:
        v = v.strip().upper()
        if not v:
            raise ValueError("symbol must be non-empty after stripping")
        return v

    @field_validator("side")
    @classmethod
    def _validate_side(cls, v: str) -> str:
        v_norm = v.strip().lower()
        # Skeleton: only 'buy' is meaningfully wired. Anything else is rejected
        # at the validation layer (fail-safe).
        if v_norm not in {"buy"}:
            raise ValueError(f"side must be 'buy' in skeleton (got {v!r})")
        return v_norm

    @property
    def notional(self) -> Decimal:
        """Gross notional value of the intent (qty * price)."""
        return self.quantity * self.price


class Position(BaseModel):
    """A single open position in the portfolio. Placeholder."""

    model_config = ConfigDict(extra="forbid")

    symbol: str
    quantity: Decimal = Field(..., ge=Decimal("0"))
    avg_price: Decimal = Field(..., gt=Decimal("0"))
    sector: str | None = None

    @property
    def market_value(self) -> Decimal:
        """Marked at avg_price — Phase 1.3 will use live marks from Data agent."""
        return self.quantity * self.avg_price


class PortfolioState(BaseModel):
    """Snapshot of portfolio state used for risk checks. Placeholder.

    `sector_exposure_pct` is intentionally a dict, not a derived property —
    Phase 1.3 will compute it from positions + live marks. The skeleton
    accepts whatever the caller passes (validation is on types, not values).
    """

    model_config = ConfigDict(extra="forbid")

    total_equity: Decimal = Field(..., gt=Decimal("0"), description="Total equity (NAV); > 0")
    cash: Decimal = Field(..., ge=Decimal("0"))
    positions: list[Position] = Field(default_factory=list)
    # Today realised + unrealised P&L. Phase 1.3 will derive from trade blotter.
    daily_pnl: Decimal = Field(default=Decimal("0"))
    # Peak equity observed to date — used for max-drawdown computation.
    peak_equity: Decimal = Field(..., gt=Decimal("0"))

    @model_validator(mode="after")
    def _peak_at_least_equity(self) -> "PortfolioState":
        # Invariant: peak_equity >= total_equity always. If a caller passes a
        # state where this doesn't hold, it's an upstream bug — reject here.
        if self.peak_equity < self.total_equity:
            raise ValueError(
                f"peak_equity ({self.peak_equity}) must be >= total_equity ({self.total_equity})"
            )
        return self


class RiskLimits(BaseModel):
    """Hard limits enforced by the gate. All percentages are in % units (e.g. 5.0 = 5%)."""

    model_config = ConfigDict(extra="forbid")

    # Maximum drawdown from peak equity, in percent. e.g. 15.0 == 15% DD.
    max_dd_pct: Decimal = Field(..., gt=Decimal("0"), le=Decimal("100"))
    # Maximum single-position size as % of equity. e.g. 10.0 == 10% of NAV.
    max_position_pct: Decimal = Field(..., gt=Decimal("0"), le=Decimal("100"))
    # Maximum aggregate sector exposure as % of equity.
    max_sector_pct: Decimal = Field(..., gt=Decimal("0"), le=Decimal("100"))
    # Maximum daily loss as % of equity. e.g. 3.0 == -3% kills trading for the day.
    max_daily_loss_pct: Decimal = Field(..., gt=Decimal("0"), le=Decimal("100"))


class RiskDecision(BaseModel):
    """Outcome of a risk evaluation.

    `allowed` is True ONLY if `violations` is empty. Fail-safe default:
    if the gate ever returns a decision with both `allowed=True` and a
    non-empty `violations`, treat it as a bug.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    allowed: bool
    violations: tuple[str, ...] = Field(default_factory=tuple)
    # Free-form metadata for the audit log (Phase 1.3). Empty dict in skeleton.
    meta: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _allowed_implies_no_violations(self) -> "RiskDecision":
        if self.allowed and self.violations:
            # Defensive: should be unreachable given gate logic, but enforce
            # the invariant at the data layer too.
            raise ValueError("allowed=True requires violations to be empty")
        return self


# ---------------------------------------------------------------------------
# Risk gate
# ---------------------------------------------------------------------------


class RiskGate:
    """Pure-Python hard-limit risk gate.

    Usage (Phase 1.3+):
        gate = RiskGate(limits=RiskLimits(max_dd_pct=15, ...))
        decision = gate.evaluate(intent, state)
        if decision.allowed:
            execution.submit(intent)
        else:
            audit.log(decision)

    Architectural guarantees:
    - No side effects (no I/O, no logging, no clock reads, no randomness).
    - Deterministic given identical inputs.
    - Stateless aside from `self.limits`.
    - Synchronous (no async).
    """

    def __init__(self, limits: RiskLimits) -> None:
        # Snapshot the limits at construction time. Mutating `limits` after
        # construction is unsupported and undefined behaviour.
        self.limits: RiskLimits = limits

    # ---- public API -----------------------------------------------------

    def evaluate(
        self, intent: TradeIntent, state: PortfolioState
    ) -> RiskDecision:
        """Evaluate a TradeIntent against current PortfolioState.

        Returns a RiskDecision. ALWAYS returns a decision — never raises for
        ordinary risk-rejection cases. May raise only on programmer error
        (bad input types, violating PortfolioState invariants).
        """
        violations: list[str] = []
        meta: dict[str, Any] = {}

        self._check_position_size(intent, state, violations, meta)
        self._check_sector_exposure(intent, state, violations, meta)
        self._check_daily_loss(state, violations, meta)
        self._check_drawdown(state, violations, meta)

        return RiskDecision(
            allowed=not violations,
            violations=tuple(violations),
            meta=meta,
        )

    # ---- individual checks ----------------------------------------------

    def _check_position_size(
        self,
        intent: TradeIntent,
        state: PortfolioState,
        violations: list[str],
        meta: dict[str, Any],
    ) -> None:
        """Single-position size must not exceed max_position_pct of equity.

        Skeleton assumes the entire intent is a NEW position (no averaging
        into an existing one). Phase 1.3 will project the post-trade
        position size including the increment.
        """
        if state.total_equity <= 0:
            # Defence-in-depth: PortfolioState validator already enforces
            # total_equity > 0, but if we ever get here, fail safe.
            violations.append("RISK_POSITION: invalid portfolio state (total_equity <= 0)")
            return

        notional = intent.notional
        position_pct = (notional / state.total_equity) * Decimal("100")
        meta["position_pct"] = float(position_pct)

        if position_pct > self.limits.max_position_pct:
            violations.append(
                f"RISK_POSITION: intent notional {notional} = {position_pct:.4f}% of equity "
                f"exceeds limit {self.limits.max_position_pct}%"
            )

    def _check_sector_exposure(
        self,
        intent: TradeIntent,
        state: PortfolioState,
        violations: list[str],
        meta: dict[str, Any],
    ) -> None:
        """Aggregate sector exposure (existing + this intent) must not exceed max_sector_pct.

        Skeleton caveat: if `intent.sector` is None, we skip sector check.
        Phase 1.3 will require every instrument to have a sector.
        """
        if intent.sector is None:
            # Skeleton: silent skip. Phase 1.3 will treat missing sector as
            # an error (unknown instrument => reject).
            meta["sector_check"] = "skipped: intent.sector is None"
            return

        # Sum current sector exposure in equity %
        sector_value = Decimal("0")
        for pos in state.positions:
            if pos.sector == intent.sector:
                sector_value += pos.market_value

        # Add the proposed intent notional (assuming same sector)
        projected_sector_value = sector_value + intent.notional
        sector_pct = (projected_sector_value / state.total_equity) * Decimal("100")
        meta["sector_pct"] = float(sector_pct)
        meta["sector"] = intent.sector

        if sector_pct > self.limits.max_sector_pct:
            violations.append(
                f"RISK_SECTOR: projected {intent.sector} exposure "
                f"{projected_sector_value} = {sector_pct:.4f}% of equity "
                f"exceeds limit {self.limits.max_sector_pct}%"
            )

    def _check_daily_loss(
        self,
        state: PortfolioState,
        violations: list[str],
        meta: dict[str, Any],
    ) -> None:
        """Daily loss (negative P&L) must not exceed max_daily_loss_pct.

        daily_pnl < 0 means a loss. The magnitude as % of equity is
        compared to the limit. daily_pnl >= 0 is always allowed.
        """
        if state.daily_pnl >= 0:
            meta["daily_loss_pct"] = 0.0
            return

        loss_pct = (-state.daily_pnl / state.total_equity) * Decimal("100")
        meta["daily_loss_pct"] = float(loss_pct)

        if loss_pct > self.limits.max_daily_loss_pct:
            violations.append(
                f"RISK_DAILY_LOSS: daily P&L {state.daily_pnl} = {loss_pct:.4f}% loss "
                f"exceeds limit {self.limits.max_daily_loss_pct}%"
            )

    def _check_drawdown(
        self,
        state: PortfolioState,
        violations: list[str],
        meta: dict[str, Any],
    ) -> None:
        """Current drawdown from peak equity must not exceed max_dd_pct.

        drawdown_pct = (peak_equity - total_equity) / peak_equity * 100
        """
        if state.peak_equity <= 0:
            # Defence-in-depth: PortfolioState validator enforces peak_equity > 0.
            violations.append("RISK_DD: invalid portfolio state (peak_equity <= 0)")
            return

        dd_pct = ((state.peak_equity - state.total_equity) / state.peak_equity) * Decimal("100")
        meta["dd_pct"] = float(dd_pct)

        # dd_pct < 0 means equity is ABOVE peak — unusual but allowed
        # (PortfolioState validator already enforces peak_equity >= total_equity,
        # so dd_pct is always >= 0 in practice).
        if dd_pct > self.limits.max_dd_pct:
            violations.append(
                f"RISK_DD: drawdown {dd_pct:.4f}% exceeds limit {self.limits.max_dd_pct}%"
            )


__all__ = [
    "TradeIntent",
    "Position",
    "PortfolioState",
    "RiskLimits",
    "RiskDecision",
    "RiskGate",
]