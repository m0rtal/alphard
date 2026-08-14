"""Risk gate logic — Phase 0 skeleton.

CRITICAL INVARIANT: Risk gate NEVER approved any violation.
Even single violation → action DENIED.

NO ML. NO LLM. Pure Python.
"""

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class TradeIntent:
    """Что бот собирается сделать."""
    ticker: str
    side: str  # "BUY" or "SELL"
    quantity: int
    price: float  # expected execution price
    sector: Optional[str] = None  # "energy", "financials", etc.


@dataclass
class PortfolioState:
    """Текущее состояние портфеля."""
    nav: float  # total value in ₽
    positions: dict = field(default_factory=dict)  # {ticker: {qty, avg_price, sector}}
    daily_pnl_pct: float = 0.0  # today's P&L as %
    drawdown_from_peak_pct: float = 0.0  # current DD vs peak (negative)


@dataclass
class RiskDecision:
    """Результат проверки Risk Gate."""
    allowed: bool
    violations: list = field(default_factory=list)  # list[str]
    reason: str = ""

    def __bool__(self):
        return self.allowed


@dataclass
class RiskLimits:
    """Hard limits — immutable without explicit user override."""
    max_position_pct: float = 5.0  # % of NAV per ticker
    max_sector_pct: float = 30.0
    max_daily_loss_pct: float = 3.0
    max_dd_pct: float = 10.0
    # Phase 1+: max_adv_pct, max_spread_pct, leverage_max


class RiskGate:
    """Pre-trade check + continuous monitoring.

    Usage:
        gate = RiskGate(limits=RiskLimits())
        decision = gate.evaluate(intent, state)
        if decision.allowed:
            execute_order(intent)
        else:
            log_blocked(intent, decision.violations)
    """

    def __init__(self, limits: Optional[RiskLimits] = None):
        self.limits = limits or RiskLimits()

    def evaluate(
        self, intent: TradeIntent, state: PortfolioState
    ) -> RiskDecision:
        """Pre-trade check. ANY violation → DENY."""
        violations = []

        # 1. Position size
        position_value = intent.quantity * intent.price
        position_pct = (position_value / state.nav) * 100 if state.nav > 0 else 0
        if position_pct > self.limits.max_position_pct:
            violations.append(
                f"position_pct {position_pct:.2f} > limit {self.limits.max_position_pct}"
            )

        # 2. Sector concentration (basic, only if intent.sector is set)
        if intent.sector:
            sector_value = self._sector_exposure(intent.sector, intent, state)
            sector_pct = (sector_value / state.nav) * 100 if state.nav > 0 else 0
            if sector_pct > self.limits.max_sector_pct:
                violations.append(
                    f"sector_exposure {sector_pct:.2f} > limit {self.limits.max_sector_pct}"
                )

        # 3. Daily loss
        if state.daily_pnl_pct < -self.limits.max_daily_loss_pct:
            violations.append(
                f"daily_loss {state.daily_pnl_pct:.2f}% < -{self.limits.max_daily_loss_pct}%"
            )

        # 4. Drawdown
        if state.drawdown_from_peak_pct < -self.limits.max_dd_pct:
            violations.append(
                f"drawdown {state.drawdown_from_peak_pct:.2f}% < -{self.limits.max_dd_pct}%"
            )

        # 5. NAV safety
        if state.nav <= 0:
            violations.append("nav_invalid_or_zero")

        # DECISION
        allowed = len(violations) == 0
        reason = "ok" if allowed else f"{len(violations)} violation(s)"
        return RiskDecision(allowed=allowed, violations=violations, reason=reason)

    def _sector_exposure(
        self, sector: str, intent: TradeIntent, state: PortfolioState
    ) -> float:
        """Считает exposure в конкретный sector."""
        existing = sum(
            pos["qty"] * pos["avg_price"]
            for pos in state.positions.values()
            if pos.get("sector") == sector
        )
        new = intent.quantity * intent.price
        return existing + new
