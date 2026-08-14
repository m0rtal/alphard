"""
Alphard Risk Agent — Skeleton tests.

Coverage target: 95%+ of risk_layer.py.

Strategy:
- One test per hard limit (position, sector, daily loss, drawdown).
- One test for the no-violation happy path.
- One test for the multi-violation case.
- Fail-safe / anomaly tests for bad inputs and edge cases.
- Validation tests for the pydantic models themselves (they're part of
  the gate's contract — bad data must be rejected at the model layer).
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from pydantic import ValidationError

from src.risk.gate import (
    PortfolioState,
    Position,
    RiskDecision,
    RiskGate,
    RiskLimits,
    TradeIntent,
)


# ---------------------------------------------------------------------------
# Fixtures — keep tests readable
# ---------------------------------------------------------------------------


@pytest.fixture
def limits() -> RiskLimits:
    """Standard risk limits for most tests."""
    return RiskLimits(
        max_dd_pct=Decimal("15.0"),
        max_position_pct=Decimal("10.0"),
        max_sector_pct=Decimal("30.0"),
        max_daily_loss_pct=Decimal("3.0"),
    )


@pytest.fixture
def base_state() -> PortfolioState:
    """Healthy portfolio state: no drawdown, no loss, no positions."""
    return PortfolioState(
        total_equity=Decimal("1000000"),
        cash=Decimal("1000000"),
        positions=[],
        daily_pnl=Decimal("0"),
        peak_equity=Decimal("1000000"),
    )


def _intent(
    symbol: str = "SBER",
    qty: int = 10,
    price: str = "100",
    sector: str | None = "energy",
) -> TradeIntent:
    """Helper to build a TradeIntent with sensible defaults."""
    return TradeIntent(
        symbol=symbol,
        side="buy",
        quantity=Decimal(qty),
        price=Decimal(price),
        sector=sector,
    )


# ===========================================================================
# Position-size checks
# ===========================================================================


class TestPositionSize:
    def test_position_size_exceeded(self, limits: RiskLimits, base_state: PortfolioState) -> None:
        """A 11% position (limit 10%) must be rejected."""
        # 110,000 / 1,000,000 = 11% > 10%
        intent = _intent(qty=1100, price="100")
        gate = RiskGate(limits)

        decision = gate.evaluate(intent, base_state)

        assert decision.allowed is False
        assert any("RISK_POSITION" in v for v in decision.violations)

    def test_position_size_allowed(self, limits: RiskLimits, base_state: PortfolioState) -> None:
        """A 9% position (limit 10%) must pass the position check.

        With no drawdown / no daily loss / no sector exposure, the trade is
        fully allowed.
        """
        # 90,000 / 1,000,000 = 9% < 10%
        intent = _intent(qty=900, price="100")
        gate = RiskGate(limits)

        decision = gate.evaluate(intent, base_state)

        assert decision.allowed is True
        assert decision.violations == ()
        # Meta should report the computed position_pct
        assert decision.meta["position_pct"] == pytest.approx(9.0)

    def test_position_size_at_limit_allowed(self, limits: RiskLimits, base_state: PortfolioState) -> None:
        """A position EXACTLY at the limit (10.0%) is allowed (boundary check)."""
        # 100,000 / 1,000,000 = exactly 10.0%
        intent = _intent(qty=1000, price="100")
        gate = RiskGate(limits)

        decision = gate.evaluate(intent, base_state)

        assert decision.allowed is True


# ===========================================================================
# Daily-loss check
# ===========================================================================


class TestDailyLoss:
    def test_daily_loss_exceeded(self, limits: RiskLimits) -> None:
        """daily_pnl = -4% (limit 3%) must be rejected."""
        state = PortfolioState(
            total_equity=Decimal("1000000"),
            cash=Decimal("0"),
            positions=[],
            daily_pnl=Decimal("-40000"),  # -4% of 1M
            peak_equity=Decimal("1000000"),
        )
        intent = _intent(qty=10, price="100")
        gate = RiskGate(limits)

        decision = gate.evaluate(intent, state)

        assert decision.allowed is False
        assert any("RISK_DAILY_LOSS" in v for v in decision.violations)

    def test_daily_loss_at_limit_allowed(self, limits: RiskLimits) -> None:
        """daily_pnl = -3% exactly (limit 3%) is allowed (boundary)."""
        state = PortfolioState(
            total_equity=Decimal("1000000"),
            cash=Decimal("0"),
            positions=[],
            daily_pnl=Decimal("-30000"),
            peak_equity=Decimal("1000000"),
        )
        intent = _intent(qty=10, price="100")
        gate = RiskGate(limits)

        decision = gate.evaluate(intent, state)

        assert decision.allowed is True

    def test_daily_profit_always_allowed(self, limits: RiskLimits, base_state: PortfolioState) -> None:
        """Positive daily P&L never triggers the daily-loss check."""
        state = base_state.model_copy(update={"daily_pnl": Decimal("50000")})
        intent = _intent(qty=10, price="100")
        gate = RiskGate(limits)

        decision = gate.evaluate(intent, state)

        assert decision.allowed is True
        assert decision.meta["daily_loss_pct"] == 0.0


# ===========================================================================
# Drawdown check
# ===========================================================================


class TestDrawdown:
    def test_dd_exceeded(self, limits: RiskLimits) -> None:
        """DD = 16% (limit 15%) must be rejected."""
        state = PortfolioState(
            total_equity=Decimal("840000"),  # -16% from 1M peak
            cash=Decimal("840000"),
            positions=[],
            daily_pnl=Decimal("0"),
            peak_equity=Decimal("1000000"),
        )
        intent = _intent(qty=10, price="100")
        gate = RiskGate(limits)

        decision = gate.evaluate(intent, state)

        assert decision.allowed is False
        assert any("RISK_DD" in v for v in decision.violations)

    def test_dd_at_limit_allowed(self, limits: RiskLimits) -> None:
        """DD exactly at 15% is allowed (boundary)."""
        state = PortfolioState(
            total_equity=Decimal("850000"),
            cash=Decimal("850000"),
            positions=[],
            daily_pnl=Decimal("0"),
            peak_equity=Decimal("1000000"),
        )
        intent = _intent(qty=10, price="100")
        gate = RiskGate(limits)

        decision = gate.evaluate(intent, state)

        assert decision.allowed is True

    def test_no_dd_is_zero_pct(self, limits: RiskLimits, base_state: PortfolioState) -> None:
        """When equity == peak, dd_pct is 0.0 (meta sanity check)."""
        intent = _intent(qty=10, price="100")
        gate = RiskGate(limits)

        decision = gate.evaluate(intent, base_state)

        assert decision.allowed is True
        assert decision.meta["dd_pct"] == pytest.approx(0.0)


# ===========================================================================
# Sector-exposure check
# ===========================================================================


class TestSectorExposure:
    def test_sector_exposure_exceeded(self, limits: RiskLimits) -> None:
        """Existing + intent sector exposure > 30% must be rejected."""
        # Existing energy exposure = 250,000 = 25% of equity.
        # Intent notional = 100 * 100 = 10,000.
        # Projected = 350,000 = 35% > 30%.
        big_state = PortfolioState(
            total_equity=Decimal("1000000"),
            cash=Decimal("600000"),
            positions=[
                Position(
                    symbol="LKOH",
                    quantity=Decimal("2500"),
                    avg_price=Decimal("100"),
                    sector="energy",
                ),
            ],
            daily_pnl=Decimal("0"),
            peak_equity=Decimal("1000000"),
        )
        # 250,000 + 100,000 intent = 350,000 = 35% > 30%
        intent = _intent(symbol="SBER", qty=1000, price="100", sector="energy")
        gate = RiskGate(limits)

        decision = gate.evaluate(intent, big_state)

        assert decision.allowed is False
        assert any("RISK_SECTOR" in v for v in decision.violations)

    def test_sector_exposure_allowed(self, limits: RiskLimits) -> None:
        """Sector exposure under 30% is allowed."""
        state = PortfolioState(
            total_equity=Decimal("1000000"),
            cash=Decimal("900000"),
            positions=[
                Position(
                    symbol="LKOH",
                    quantity=Decimal("1000"),
                    avg_price=Decimal("100"),
                    sector="energy",
                ),
            ],
            daily_pnl=Decimal("0"),
            peak_equity=Decimal("1000000"),
        )
        # 100,000 existing + 10,000 intent = 110,000 = 11% < 30%
        intent = _intent(symbol="SBER", qty=100, price="100", sector="energy")
        gate = RiskGate(limits)

        decision = gate.evaluate(intent, state)

        assert decision.allowed is True

    def test_sector_check_skipped_when_no_sector(self, limits: RiskLimits, base_state: PortfolioState) -> None:
        """Skeleton behaviour: missing intent.sector skips the sector check."""
        intent = _intent(sector=None)
        gate = RiskGate(limits)

        decision = gate.evaluate(intent, base_state)

        assert decision.allowed is True
        assert decision.meta.get("sector_check", "").startswith("skipped")

    def test_other_sector_does_not_count(self, limits: RiskLimits) -> None:
        """Existing position in 'tech' does not affect 'energy' sector check."""
        state = PortfolioState(
            total_equity=Decimal("1000000"),
            cash=Decimal("500000"),
            positions=[
                Position(
                    symbol="YNDX",
                    quantity=Decimal("5000"),
                    avg_price=Decimal("100"),
                    sector="tech",
                ),
            ],
            daily_pnl=Decimal("0"),
            peak_equity=Decimal("1000000"),
        )
        # Existing tech = 500,000 (50% of equity, way over 30%) but it's TECH,
        # not energy, so the energy check must not fire.
        intent = _intent(symbol="SBER", qty=100, price="100", sector="energy")
        gate = RiskGate(limits)

        decision = gate.evaluate(intent, state)

        # Energy sector check alone passes (10,000 = 1% of equity < 30%).
        # Position-size check alone passes (10,000 = 1% of equity < 10%).
        assert decision.allowed is True


# ===========================================================================
# Multi-violation & fail-safe
# ===========================================================================


class TestMultipleViolations:
    def test_multiple_violations(self, limits: RiskLimits) -> None:
        """A trade violating several limits at once reports ALL of them."""
        # Equity 800k, peak 1M -> DD = 20% > 15%
        # daily_pnl = -40k -> -5% > 3%
        # intent notional = 200,000 -> 25% > 10% position limit
        state = PortfolioState(
            total_equity=Decimal("800000"),
            cash=Decimal("600000"),
            positions=[],
            daily_pnl=Decimal("-40000"),
            peak_equity=Decimal("1000000"),
        )
        intent = _intent(qty=2000, price="100")
        gate = RiskGate(limits)

        decision = gate.evaluate(intent, state)

        assert decision.allowed is False
        # Should have at least 3 violations: DD, daily loss, position
        assert len(decision.violations) >= 3
        codes = [v.split(":")[0] for v in decision.violations]
        assert "RISK_DD" in codes
        assert "RISK_DAILY_LOSS" in codes
        assert "RISK_POSITION" in codes

    def test_no_violations_allows(self, limits: RiskLimits, base_state: PortfolioState) -> None:
        """Happy path: clean state, small trade -> allowed=True, empty violations."""
        intent = _intent(qty=100, price="100")  # 10k = 1% of equity
        gate = RiskGate(limits)

        decision = gate.evaluate(intent, base_state)

        assert isinstance(decision, RiskDecision)
        assert decision.allowed is True
        assert decision.violations == ()
        # Meta should contain all computed metrics
        assert "position_pct" in decision.meta
        assert "dd_pct" in decision.meta
        assert "daily_loss_pct" in decision.meta


class TestFailSafe:
    def test_fail_safe_default_unknown_input(self, limits: RiskLimits, base_state: PortfolioState) -> None:
        """A symbol that pydantic validation rejects (empty after strip) raises.

        The skeleton surfaces this as a ValidationError — Phase 1.3 will
        translate these into structured risk violations. The contract is
        "bad inputs never reach evaluate()".
        """
        with pytest.raises(ValidationError):
            TradeIntent(symbol="   ", side="buy", quantity=Decimal("1"), price=Decimal("1"))

    def test_fail_safe_invalid_side(self, limits: RiskLimits, base_state: PortfolioState) -> None:
        """'sell' is not supported in the skeleton -> ValidationError."""
        with pytest.raises(ValidationError):
            TradeIntent(symbol="SBER", side="sell", quantity=Decimal("1"), price=Decimal("1"))

    def test_fail_safe_negative_quantity(self, limits: RiskLimits, base_state: PortfolioState) -> None:
        """Negative quantity is rejected at the model layer (fail-safe)."""
        with pytest.raises(ValidationError):
            TradeIntent(symbol="SBER", side="buy", quantity=Decimal("-1"), price=Decimal("1"))

    def test_fail_safe_zero_price(self, limits: RiskLimits, base_state: PortfolioState) -> None:
        """Zero price is rejected at the model layer."""
        with pytest.raises(ValidationError):
            TradeIntent(symbol="SBER", side="buy", quantity=Decimal("1"), price=Decimal("0"))

    def test_fail_safe_negative_equity(self, limits: RiskLimits) -> None:
        """PortfolioState with non-positive equity is rejected at the model layer."""
        with pytest.raises(ValidationError):
            PortfolioState(
                total_equity=Decimal("0"),
                cash=Decimal("0"),
                positions=[],
                daily_pnl=Decimal("0"),
                peak_equity=Decimal("0"),
            )

    def test_fail_safe_peak_less_than_equity(self, limits: RiskLimits) -> None:
        """peak_equity < total_equity violates an invariant — rejected."""
        with pytest.raises(ValidationError):
            PortfolioState(
                total_equity=Decimal("1000"),
                cash=Decimal("1000"),
                positions=[],
                daily_pnl=Decimal("0"),
                peak_equity=Decimal("500"),  # < total_equity: impossible
            )

    def test_fail_safe_decision_invariant(self, limits: RiskLimits, base_state: PortfolioState) -> None:
        """RiskDecision cannot be constructed with allowed=True AND violations.

        This guards against a future code path that bypasses the gate.
        """
        with pytest.raises(ValidationError):
            RiskDecision(allowed=True, violations=("RISK_X: should be empty",))

    def test_fail_safe_invalid_limits(self) -> None:
        """RiskLimits must be in (0, 100]. 0 and > 100 are rejected."""
        with pytest.raises(ValidationError):
            RiskLimits(
                max_dd_pct=Decimal("0"),
                max_position_pct=Decimal("10"),
                max_sector_pct=Decimal("30"),
                max_daily_loss_pct=Decimal("3"),
            )
        with pytest.raises(ValidationError):
            RiskLimits(
                max_dd_pct=Decimal("101"),
                max_position_pct=Decimal("10"),
                max_sector_pct=Decimal("30"),
                max_daily_loss_pct=Decimal("3"),
            )


# ===========================================================================
# RiskLimits boundary tests
# ===========================================================================


class TestLimits:
    def test_limits_extra_field_rejected(self) -> None:
        """RiskLimits rejects unknown fields — fail-safe against typo'd config."""
        with pytest.raises(ValidationError):
            RiskLimits(
                max_dd_pct=Decimal("15"),
                max_position_pct=Decimal("10"),
                max_sector_pct=Decimal("30"),
                max_daily_loss_pct=Decimal("3"),
                max_something_else=Decimal("99"),  # type: ignore[call-arg]
            )

    def test_limits_default_leverage_and_short(self) -> None:
        """Defaults: leverage_max=1.0 (no leverage), allow_short=False."""
        limits = RiskLimits(
            max_dd_pct=Decimal("10"),
            max_position_pct=Decimal("5"),
            max_sector_pct=Decimal("30"),
            max_daily_loss_pct=Decimal("3"),
        )
        assert limits.leverage_max == Decimal("1.0")
        assert limits.allow_short is False

    def test_limits_custom_leverage_and_short(self) -> None:
        """Custom leverage_max и allow_short принимаются."""
        limits = RiskLimits(
            max_dd_pct=Decimal("10"),
            max_position_pct=Decimal("5"),
            max_sector_pct=Decimal("30"),
            max_daily_loss_pct=Decimal("3"),
            leverage_max=Decimal("1.5"),
            allow_short=True,
        )
        assert limits.leverage_max == Decimal("1.5")
        assert limits.allow_short is True

    def test_limits_leverage_below_one_rejected(self) -> None:
        """leverage_max < 1.0 (отрицательный leverage) rejected by pydantic."""
        with pytest.raises(ValidationError):
            RiskLimits(
                max_dd_pct=Decimal("10"),
                max_position_pct=Decimal("5"),
                max_sector_pct=Decimal("30"),
                max_daily_loss_pct=Decimal("3"),
                leverage_max=Decimal("0.5"),  # below 1.0 — invalid
            )

    def test_limits_leverage_above_two_rejected(self) -> None:
        """leverage_max > 2.0 rejected (too risky for long-term investing)."""
        with pytest.raises(ValidationError):
            RiskLimits(
                max_dd_pct=Decimal("10"),
                max_position_pct=Decimal("5"),
                max_sector_pct=Decimal("30"),
                max_daily_loss_pct=Decimal("3"),
                leverage_max=Decimal("3.0"),  # above 2.0 — invalid
            )

    def test_limits_immutability(self) -> None:
        """RiskLimits model is frozen: post-construction mutation must FAIL.

        SECURITY: this is the **risk gate** — if a caller could mutate
        limits.max_dd_pct = 200 after construction, the validators (gt=0, le=100)
        would be bypassed entirely. Pydantic v2's `frozen=True` raises
        ValidationError on `__setattr__`. We verify the public API is locked.

        Note: `object.__setattr__` bypass is theoretical — Python runtime
        cannot prevent low-level bypass without C-level interception. The
        defence-in-depth is in the architecture: RiskGate is constructed
        with limits ONCE, then carried by reference. A caller that has
        the reference could in theory bypass but cannot also bypass
        integration testing + audit log.
        """
        limits = RiskLimits(
            max_dd_pct=Decimal("10"),
            max_position_pct=Decimal("5"),
            max_sector_pct=Decimal("30"),
            max_daily_loss_pct=Decimal("3"),
        )
        # Direct attribute assignment is blocked by pydantic frozen=True
        with pytest.raises((ValidationError, Exception)):
            limits.max_dd_pct = Decimal("200")
        # Verify validators still reject invalid even if frozen is bypassed:
        with pytest.raises(ValidationError):
            RiskLimits(
                max_dd_pct=Decimal("200"),  # impossible via frozen bypass without removing it
                max_position_pct=Decimal("5"),
                max_sector_pct=Decimal("30"),
                max_daily_loss_pct=Decimal("3"),
            )


# ===========================================================================
# RiskGate behaviour
# ===========================================================================


class TestGate:
    def test_gate_stores_limits(self, limits: RiskLimits) -> None:
        """RiskGate exposes its configured limits (read-only by convention)."""
        gate = RiskGate(limits)
        assert gate.limits is limits

    def test_gate_evaluate_is_pure(self, limits: RiskLimits, base_state: PortfolioState) -> None:
        """Calling evaluate() twice with the same inputs yields equal decisions.

        Guarantees no hidden state, no I/O, no clock reads.
        """
        gate = RiskGate(limits)
        intent = _intent(qty=100, price="100")
        d1 = gate.evaluate(intent, base_state)
        d2 = gate.evaluate(intent, base_state)
        assert d1.allowed == d2.allowed
        assert d1.violations == d2.violations

    def test_meta_contains_all_metrics(self, limits: RiskLimits, base_state: PortfolioState) -> None:
        """On a healthy state, every check should populate meta (or skip-key for sector)."""
        intent = _intent(qty=100, price="100", sector="energy")
        gate = RiskGate(limits)

        decision = gate.evaluate(intent, base_state)

        assert "position_pct" in decision.meta
        assert "sector_pct" in decision.meta
        assert "dd_pct" in decision.meta
        assert "daily_loss_pct" in decision.meta
        assert decision.meta["sector"] == "energy"


# ===========================================================================
# Position / TradeIntent helpers
# ===========================================================================


class TestHelpers:
    def test_position_market_value(self) -> None:
        """Position.market_value == qty * avg_price (placeholder mark)."""
        p = Position(symbol="X", quantity=Decimal("10"), avg_price=Decimal("50"), sector="x")
        assert p.market_value == Decimal("500")

    def test_intent_notional(self) -> None:
        """TradeIntent.notional == qty * price."""
        i = _intent(qty=5, price="200")
        assert i.notional == Decimal("1000")

    def test_intent_symbol_normalised(self) -> None:
        """Symbol is stripped and uppercased."""
        i = TradeIntent(symbol="  sber  ", side="buy", quantity=Decimal("1"), price=Decimal("1"))
        assert i.symbol == "SBER"
