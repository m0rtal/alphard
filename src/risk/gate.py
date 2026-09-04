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

    SECURITY: model is `frozen=True` (issue #98). Without `frozen`, post-
    construction assignment would bypass every `Field` validator — e.g.
    `intent.quantity = Decimal('-1')` would NOT raise ValidationError, so
    a caller holding a reference between construction and gate-evaluate could
    rewrite price/qty/side AFTER the gate had read the original values. This
    is the same exploit class as the historical MarketOrder price=1 bypass
    (issues #11 / #13) but on the intent side. Mutate via `model_copy` if
    a new value is genuinely needed.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

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
        # BUGFIX (C-4): allow both "buy" and "sell". The previous whitelist of
        # only {"buy"} masked the broker-side SELL→BUY inversion — by the time
        # an inverted "buy" hit this validator, the damage was done. With the
        # broker fix in place, we now let both sides through to RiskGate.
        if v_norm not in {"buy", "sell"}:
            raise ValueError(f"side must be 'buy' or 'sell' (got {v!r})")
        return v_norm

    @property
    def notional(self) -> Decimal:
        """Gross notional value of the intent (qty * price)."""
        return self.quantity * self.price


class Position(BaseModel):
    """A single open position in the portfolio. Placeholder.

    SECURITY: model is `frozen=True` (issue #98, audit follow-up). Position is
    a building block of PortfolioState; if a caller could mutate e.g.
    `pos.quantity = Decimal('0')` after construction, the sector-exposure
    check in RiskGate would compute against falsified market_value, letting
    an oversized position slip through RISK_SECTOR. Same defence-in-depth
    reasoning as TradeIntent / RiskLimits.

    Issue #240: normalise ``symbol`` to UPPERCASE on construction, mirroring
    ``TradeIntent._strip_symbol`` (line 90-96). Without this, a
    ``PortfolioState(positions=[Position(symbol="sber", ...)])`` built from
    mixed-case broker output (e.g. Tinkoff) would silently miss the lookup
    against ``TradeIntent(symbol="SBER")`` in
    ``RiskGate._check_position_size`` (line 326) — the existing_qty would
    be reported as 0, and the existing position would be invisible to the
    RISK_POSITION check. Sister fix of the ticker-normalisation series
    (issues #183, #185, #224, #234, #236, #238).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    symbol: str = Field(..., min_length=1, description="Instrument ticker, e.g. 'SBER'")
    quantity: Decimal = Field(..., ge=Decimal("0"))
    avg_price: Decimal = Field(..., gt=Decimal("0"))
    sector: str | None = None

    @field_validator("symbol")
    @classmethod
    def _strip_symbol(cls, v: str) -> str:
        # Mirrors TradeIntent._strip_symbol (line 90-96) so that
        # ``Position(symbol="sber")`` and ``Position(symbol="SBER")`` are
        # canonicalised to the same key for the lookup at line 326.
        v = v.strip().upper()
        if not v:
            raise ValueError("symbol must be non-empty after stripping")
        return v

    @property
    def market_value(self) -> Decimal:
        """Marked at avg_price — Phase 1.3 will use live marks from Data agent."""
        return self.quantity * self.avg_price


class PortfolioState(BaseModel):
    """Snapshot of portfolio state used for risk checks. Placeholder.

    `sector_exposure_pct` is intentionally a dict, not a derived property —
    Phase 1.3 will compute it from positions + live marks. The skeleton
    accepts whatever the caller passes (validation is on types, not values).

    SECURITY: model is `frozen=True` (issue #98, audit follow-up). Without
    `frozen`, a caller could rewrite `state.peak_equity` or `state.positions`
    between construction and `RiskGate.evaluate()` and silently bypass the
    drawdown / sector-exposure checks. Mutate via `model_copy` if a new
    snapshot is genuinely needed.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

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
            )  # noqa: E501
        return self


class RiskLimits(BaseModel):
    """Hard limits enforced by the gate. All percentages are in % units (e.g. 5.0 = 5%).

    SECURITY: model is `frozen=True` — once constructed, fields cannot be mutated.
    Without `frozen`, post-construction assignment would bypass all `Field` validators
    (e.g., `limits.max_dd_pct = 200` would not raise ValidationError). This is
    the **risk gate** — any code path that could mutate limits after validation is
    an exploit vector. See SECURITY.md: "Risk gate immutability".
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    # Maximum drawdown from peak equity, in percent. e.g. 15.0 == 15% DD.
    max_dd_pct: Decimal = Field(..., gt=Decimal("0"), le=Decimal("100"))
    # Maximum single-position size as % of equity. e.g. 10.0 == 10% of NAV.
    max_position_pct: Decimal = Field(..., gt=Decimal("0"), le=Decimal("100"))
    # Maximum aggregate sector exposure as % of equity.
    max_sector_pct: Decimal = Field(..., gt=Decimal("0"), le=Decimal("100"))
    # Maximum daily loss as % of equity. e.g. 3.0 == -3% kills trading for the day.
    max_daily_loss_pct: Decimal = Field(..., gt=Decimal("0"), le=Decimal("100"))
    # Maximum leverage multiplier (1.0 = no leverage, 1.15 = 15% leverage).
    leverage_max: Decimal = Field(default=Decimal("1.0"), ge=Decimal("1.0"), le=Decimal("2.0"))
    # Allow short selling (requires qualified investor + margin account).
    allow_short: bool = Field(default=False)


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

    def evaluate(self, intent: TradeIntent, state: PortfolioState) -> RiskDecision:
        """Evaluate a TradeIntent against current PortfolioState.

        Returns a RiskDecision. ALWAYS returns a decision — never raises for
        ordinary risk-rejection cases. May raise only on programmer error
        (bad input types, violating PortfolioState invariants).
        """
        violations: list[str] = []
        meta: dict[str, Any] = {}

        # Hard pre-check (issue #11): the historical ``MarketOrder`` path
        # in TinkoffAccount._build_intent_and_state used ``Decimal("1")``
        # as a placeholder price when no live quote was available. That
        # caused position_pct to be 1/100..1/300 of the real value and
        # silently bypassed RISK_POSITION / RISK_SECTOR. Here we catch
        # the sentinel at the gate boundary: if ``price`` is exactly
        # ``Decimal("1")`` AND ``quantity > 1``, the intent is almost
        # certainly a market-order-without-live-quote, which is unsafe.
        # We refuse rather than guessing.
        if intent.price == Decimal("1") and intent.quantity > Decimal("1"):
            violations.append(
                "RISK_MARKET_ORDER_NO_QUOTE: intent.price=Decimal('1') is the "
                "historical placeholder for an unresolved market-order quote; "
                "RiskGate cannot evaluate an intent without a real price. "
                "Caller must fetch a live quote and resubmit."
            )

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

        Issue #172: previously this check treated every intent as a BUY,
        computing ``position_pct = intent.notional / equity * 100``. A SELL
        intent that trims an existing long position was rejected as if it
        had ADDED exposure — a sell of 20% of equity on a 30% position
        would fail RISK_POSITION even though it DE-risks the book.

        Corrected semantics:
          * BUY  → projects the full post-trade position level, marking both
                   the existing and incoming quantity at ``intent.price``.
          * SELL → only the portion that would OPEN a new short counts
                   (qty exceeding the existing long position). The trim
                   portion is allowed because it strictly reduces risk.
        The BUY projection uses the same live ``intent.price`` domain as the
        sector check, so position and sector limits evaluate a consistent
        post-trade exposure level.
        Sector-exposure / drawdown / daily-loss checks are unaffected.
        """
        if state.total_equity <= 0:
            # Defence-in-depth: PortfolioState validator already enforces
            # total_equity > 0, but if we ever get here, fail safe.
            violations.append("RISK_POSITION: invalid portfolio state (total_equity <= 0)")
            return

        # Existing long quantity in this symbol. For SELL this is the
        # amount we trim before we start opening a short.
        existing_qty = sum(
            (p.quantity for p in state.positions if p.symbol == intent.symbol),
            Decimal("0"),
        )
        meta["existing_qty"] = float(existing_qty)

        if intent.side == "sell":
            trim_qty = min(intent.quantity, existing_qty)
            short_qty = intent.quantity - trim_qty  # never < 0 here
            # Only the short portion creates new exposure. The trim
            # portion strictly reduces risk.
            effective_notional = short_qty * intent.price
            meta["trim_qty"] = float(trim_qty)
            meta["short_qty"] = float(short_qty)
        else:
            # BUY (or anything else — fail-safe: treat as exposure-additive)
            projected_qty = existing_qty + intent.quantity
            effective_notional = projected_qty * intent.price
            meta["projected_qty"] = float(projected_qty)

        if effective_notional <= Decimal("0"):
            # Pure trim — never trips position limit. Record zero.
            meta["position_pct"] = 0.0
            return

        position_pct = (effective_notional / state.total_equity) * Decimal("100")
        meta["position_pct"] = float(position_pct)

        if position_pct > self.limits.max_position_pct:
            violations.append(
                f"RISK_POSITION: projected position notional {effective_notional} = {position_pct:.4f}% of equity "
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

        Issue #178: the projection must mirror the side-aware decomposition
        used in `_check_position_size`. A SELL that trims an existing long
        position in the same sector REDUCES aggregate sector exposure — it
        must not be counted as if it ADDED `intent.notional`. The trim
        portion (qty up to existing long) lowers exposure; the short
        portion (qty beyond existing long) raises exposure.

        Issue #204 (mark consistency): the sector value must be computed at
        the SAME price domain as the trim/short projection. Pre-fix, existing
        exposure was summed at `pos.market_value` (qty × avg_price) while
        the trim subtracted `trim_qty × intent.price`. In multi-symbol sectors
        — or any time a position has unrealised P&L so avg_price ≠ current
        price — this mix produced wrong percentages (over- or under-counted)
        and the ``projected_sector_value < 0`` clamp silently hid the
        arithmetic violation. The fix marks the sector at `intent.price`
        (the live mark the trade is being evaluated at) so both sides of
        the equation use the same price domain. This matches
        `_check_position_size`, which already evaluates the per-symbol
        notional at `intent.price`.
        """
        if intent.sector is None:
            # Skeleton: silent skip. Phase 1.3 will treat missing sector as
            # an error (unknown instrument => reject).
            meta["sector_check"] = "skipped: intent.sector is None"
            return

        # Mark the entire sector at the intent's live price. This is the
        # same price domain the trim/short projection uses, so the math
        # stays self-consistent regardless of how many symbols are in the
        # sector or how far avg_price has drifted from current price.
        sector_value = sum(
            (p.quantity * intent.price for p in state.positions if p.sector == intent.sector),
            Decimal("0"),
        )
        meta["sector_mark_basis"] = "intent.price"

        if intent.side == "sell":
            # Symmetric to _check_position_size but SECTOR-AWARE: only
            # positions in *both* intent.symbol AND intent.sector count as
            # trim capacity. A position in the same symbol but a different
            # sector does NOT reduce intent.sector exposure (the trim
            # affects a different risk bucket), so it cannot be subtracted
            # from the sector projection. Issue #204: pre-fix this used
            # `_check_position_size`'s symbol-only filter, which let the
            # same-sector trim math interbreed with cross-sector positions.
            existing_qty_in_intent_sector = sum(
                (p.quantity for p in state.positions if p.symbol == intent.symbol and p.sector == intent.sector),
                Decimal("0"),
            )
            trim_qty = min(intent.quantity, existing_qty_in_intent_sector)
            short_qty = intent.quantity - trim_qty  # never < 0 here
            # trim_qty * price reduces sector_value; short_qty * price adds.
            # With sector_value already marked at intent.price, this stays
            # self-consistent across multi-symbol sectors and unrealised
            # P&L — see issue #204.
            projected_sector_value = sector_value - trim_qty * intent.price + short_qty * intent.price
            meta["sector_trim_qty"] = float(trim_qty)
            meta["sector_short_qty"] = float(short_qty)
        else:
            # BUY (or anything else — fail-safe: treat as exposure-additive)
            projected_sector_value = sector_value + intent.notional

        # Note: issue #204 removed the ``projected_sector_value < 0`` clamp.
        # With sector_value marked at intent.price, ``trim_qty * intent.price``
        # is bounded by ``sum(qty_in_intent_symbol) * intent.price ≤
        # sum(qty_in_sector) * intent.price = sector_value`` — so the
        # projected value can never go negative for a valid SELL. If it ever
        # does, that is an upstream invariant violation (a position's sector
        # tag was changed mid-flight, or a duplicate Position was added for
        # the same symbol) and we want the negative number to surface in
        # audit logs, not be silently zeroed out.

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
            violations.append(f"RISK_DD: drawdown {dd_pct:.4f}% exceeds limit {self.limits.max_dd_pct}%")  # noqa: E501


__all__ = [
    "TradeIntent",
    "Position",
    "PortfolioState",
    "RiskLimits",
    "RiskDecision",
    "RiskGate",
]
