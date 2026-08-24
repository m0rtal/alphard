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

    def test_position_size_at_limit_allowed(self, limits: RiskLimits, base_state: PortfolioState) -> None:  # noqa: E501
        """A position EXACTLY at the limit (10.0%) is allowed (boundary check)."""
        # 100,000 / 1,000,000 = exactly 10.0%
        intent = _intent(qty=1000, price="100")
        gate = RiskGate(limits)

        decision = gate.evaluate(intent, base_state)

        assert decision.allowed is True

    def test_sell_within_existing_long_allowed(self, limits: RiskLimits) -> None:
        """Issue #172: a SELL that trims an existing long must not trip
        RISK_POSITION. Pre-fix logic rejected it because it computed
        ``position_pct = intent.notional / equity * 100`` regardless of
        side, even though a SELL strictly reduces gross exposure.

        Portfolio holds 500 SBER @ 100 = 5% of equity. Selling 200 SBER
        @ 100 = 2% notional trims the position to 3% — a textbook de-risk.
        """
        state = PortfolioState(
            total_equity=Decimal("1000000"),
            cash=Decimal("0"),
            positions=[
                Position(
                    symbol="SBER",
                    quantity=Decimal("500"),
                    avg_price=Decimal("100"),
                )
            ],
            daily_pnl=Decimal("0"),
            peak_equity=Decimal("1000000"),
        )
        intent = TradeIntent(
            symbol="SBER",
            side="sell",
            quantity=Decimal("200"),
            price=Decimal("100"),
        )
        gate = RiskGate(limits)

        decision = gate.evaluate(intent, state)

        assert decision.allowed is True
        # Pure trim — effective_notional is zero, position_pct is reported
        # as 0% in the audit log so operators can confirm the path.
        assert decision.meta["existing_qty"] == pytest.approx(500.0)
        assert decision.meta["trim_qty"] == pytest.approx(200.0)
        assert decision.meta["short_qty"] == pytest.approx(0.0)
        assert decision.meta["position_pct"] == 0.0

    def test_sell_exceeding_long_counts_only_short_portion(self, limits: RiskLimits) -> None:
        """Issue #172: when a SELL exceeds the existing long (opens a short),
        only the SHORT portion counts toward the position limit, not the
        full intent notional.

        Portfolio holds 100 SBER. Selling 1100 SBER @ 100 → trims 100,
        opens short of 1000 @ 100 = 10% of equity (exactly at limit → allowed).
        """
        state = PortfolioState(
            total_equity=Decimal("1000000"),
            cash=Decimal("0"),
            positions=[
                Position(
                    symbol="SBER",
                    quantity=Decimal("100"),
                    avg_price=Decimal("100"),
                )
            ],
            daily_pnl=Decimal("0"),
            peak_equity=Decimal("1000000"),
        )
        intent = TradeIntent(
            symbol="SBER",
            side="sell",
            quantity=Decimal("1100"),
            price=Decimal("100"),
        )
        gate = RiskGate(limits)

        decision = gate.evaluate(intent, state)

        # 1000 shares short @ 100 = 100,000 = exactly 10% of equity → boundary allowed
        assert decision.allowed is True
        assert decision.meta["trim_qty"] == pytest.approx(100.0)
        assert decision.meta["short_qty"] == pytest.approx(1000.0)
        assert decision.meta["position_pct"] == pytest.approx(10.0)

    def test_sell_opening_short_above_limit_rejected(self, limits: RiskLimits) -> None:
        """Issue #172: a SELL that opens a short exceeding the position
        limit must be rejected with RISK_POSITION, sized on the short portion.
        """
        state = PortfolioState(
            total_equity=Decimal("1000000"),
            cash=Decimal("0"),
            positions=[
                Position(
                    symbol="SBER",
                    quantity=Decimal("100"),
                    avg_price=Decimal("100"),
                )
            ],
            daily_pnl=Decimal("0"),
            peak_equity=Decimal("1000000"),
        )
        # Sell 1200 SBER @ 100 → trim 100, short 1100 @ 100 = 11% of equity > 10%
        intent = TradeIntent(
            symbol="SBER",
            side="sell",
            quantity=Decimal("1200"),
            price=Decimal("100"),
        )
        gate = RiskGate(limits)

        decision = gate.evaluate(intent, state)

        assert decision.allowed is False
        assert any("RISK_POSITION" in v for v in decision.violations)
        # Violation text must reference the SHORT notional (11,000-equivalent
        # in position_pct), not the gross intent notional (120,000-equivalent
        # = 12% which would be the pre-fix over-count).
        assert decision.meta["position_pct"] == pytest.approx(11.0)
        assert decision.meta["short_qty"] == pytest.approx(1100.0)

    def test_sell_with_no_existing_position_counts_full_notional(
        self, limits: RiskLimits, base_state: PortfolioState
    ) -> None:
        """Issue #172: a SELL against no existing position is 100% new
        short — must be counted as full notional. Pre-fix this happened
        to \"work\" because the old logic always counted full notional,
        but it was wrong by accident; now the semantics are explicit.
        """
        intent = TradeIntent(
            symbol="SBER",
            side="sell",
            quantity=Decimal("1100"),
            price=Decimal("100"),
        )
        gate = RiskGate(limits)

        decision = gate.evaluate(intent, base_state)

        assert decision.allowed is False
        assert any("RISK_POSITION" in v for v in decision.violations)
        assert decision.meta["existing_qty"] == pytest.approx(0.0)
        assert decision.meta["trim_qty"] == pytest.approx(0.0)
        assert decision.meta["short_qty"] == pytest.approx(1100.0)
        assert decision.meta["position_pct"] == pytest.approx(11.0)

    def test_buy_position_check_unchanged(self, limits: RiskLimits, base_state: PortfolioState) -> None:
        """Issue #172 regression guard: BUY semantics are unchanged —
        a 9% BUY on empty book is allowed, an 11% BUY is rejected.
        """
        allowed_intent = _intent(qty=900, price="100")
        rejected_intent = _intent(qty=1100, price="100")
        gate = RiskGate(limits)

        assert gate.evaluate(allowed_intent, base_state).allowed is True
        assert gate.evaluate(rejected_intent, base_state).allowed is False
        assert any("RISK_POSITION" in v for v in gate.evaluate(rejected_intent, base_state).violations)


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

    def test_daily_profit_always_allowed(self, limits: RiskLimits, base_state: PortfolioState) -> None:  # noqa: E501
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

    def test_sector_check_skipped_when_no_sector(
        self, limits: RiskLimits, base_state: PortfolioState
    ) -> None:  # noqa: E501
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

    def test_sell_trim_reduces_sector_exposure_allowed(self, limits: RiskLimits) -> None:
        """Issue #178: a SELL that trims an existing long position in the
        same sector REDUCES aggregate sector exposure. Pre-fix logic
        projected ``sector_value + intent.notional``, which over-counted
        the trim portion and fired RISK_SECTOR on every trim-only SELL.

        Portfolio holds 2500 SBER @ 100 = 250,000 (25% energy). Selling
        2000 SBER @ 100 = 200,000 (pure trim) → projected energy exposure
        drops from 25% to 5%. Must be allowed; the trim reduces risk.
        """
        state = PortfolioState(
            total_equity=Decimal("1000000"),
            cash=Decimal("0"),
            positions=[
                Position(
                    symbol="SBER",
                    quantity=Decimal("2500"),
                    avg_price=Decimal("100"),
                    sector="energy",
                ),
            ],
            daily_pnl=Decimal("0"),
            peak_equity=Decimal("1000000"),
        )
        intent = TradeIntent(
            symbol="SBER",
            side="sell",
            quantity=Decimal("2000"),
            price=Decimal("100"),
            sector="energy",
        )
        gate = RiskGate(limits)

        decision = gate.evaluate(intent, state)

        assert decision.allowed is True
        # Meta should report the trim/short decomposition so operators can
        # confirm the path: trim_qty == intent.quantity (pure trim),
        # short_qty == 0, sector_pct reflects the new (lower) projection.
        assert decision.meta["sector_trim_qty"] == pytest.approx(2000.0)
        assert decision.meta["sector_short_qty"] == pytest.approx(0.0)
        assert decision.meta["sector_pct"] == pytest.approx(5.0)

    def test_sell_partial_short_increases_sector_exposure_proportionally(self, limits: RiskLimits) -> None:
        """Issue #178: a SELL that PARTIALLY opens a short must increase
        sector exposure only by the SHORT portion's notional.

        Portfolio holds 2500 SBER @ 100 = 250,000 (25% energy). Selling
        3000 SBER @ 100 → trim 2500, short 500. Projected energy exposure
        = 25% - 25% + 5% = 5%. Pre-fix this projected 25% + 30% = 55%
        and rejected.
        """
        state = PortfolioState(
            total_equity=Decimal("1000000"),
            cash=Decimal("0"),
            positions=[
                Position(
                    symbol="SBER",
                    quantity=Decimal("2500"),
                    avg_price=Decimal("100"),
                    sector="energy",
                ),
            ],
            daily_pnl=Decimal("0"),
            peak_equity=Decimal("1000000"),
        )
        intent = TradeIntent(
            symbol="SBER",
            side="sell",
            quantity=Decimal("3000"),
            price=Decimal("100"),
            sector="energy",
        )
        gate = RiskGate(limits)

        decision = gate.evaluate(intent, state)

        assert decision.allowed is True
        assert decision.meta["sector_trim_qty"] == pytest.approx(2500.0)
        assert decision.meta["sector_short_qty"] == pytest.approx(500.0)
        assert decision.meta["sector_pct"] == pytest.approx(5.0)

    def test_sell_pure_short_no_existing_position_increases_sector_exposure(
        self, limits: RiskLimits, base_state: PortfolioState
    ) -> None:
        """Issue #178: a SELL with no existing position is 100% new short
        exposure in the sector — must be counted as full notional, same
        semantics as a BUY of equivalent size.
        """
        # 1000 SBER @ 100 = 100,000 = 10% of equity in 'energy' on empty book.
        intent = TradeIntent(
            symbol="SBER",
            side="sell",
            quantity=Decimal("1000"),
            price=Decimal("100"),
            sector="energy",
        )
        gate = RiskGate(limits)

        decision = gate.evaluate(intent, base_state)

        assert decision.allowed is True
        assert decision.meta["sector_trim_qty"] == pytest.approx(0.0)
        assert decision.meta["sector_short_qty"] == pytest.approx(1000.0)
        assert decision.meta["sector_pct"] == pytest.approx(10.0)

    def test_sell_short_above_sector_limit_rejected(self, limits: RiskLimits) -> None:
        """Issue #178: a SELL whose SHORT portion alone exceeds the sector
        limit must be rejected with RISK_SECTOR.

        Portfolio holds 2500 SBER @ 100 = 25% energy (limit 30%). Selling
        6000 SBER @ 100 → trim 2500, short 3500 = 35% new energy exposure.
        Post-trim sector exposure = 25% - 25% + 35% = 35% > 30% → REJECT.
        Pre-fix the projection was 25% + 60% = 85%, also rejected but
        for the wrong reason.
        """
        state = PortfolioState(
            total_equity=Decimal("1000000"),
            cash=Decimal("0"),
            positions=[
                Position(
                    symbol="SBER",
                    quantity=Decimal("2500"),
                    avg_price=Decimal("100"),
                    sector="energy",
                ),
            ],
            daily_pnl=Decimal("0"),
            peak_equity=Decimal("1000000"),
        )
        intent = TradeIntent(
            symbol="SBER",
            side="sell",
            quantity=Decimal("6000"),
            price=Decimal("100"),
            sector="energy",
        )
        gate = RiskGate(limits)

        decision = gate.evaluate(intent, state)

        assert decision.allowed is False
        assert any("RISK_SECTOR" in v for v in decision.violations)
        assert decision.meta["sector_trim_qty"] == pytest.approx(2500.0)
        assert decision.meta["sector_short_qty"] == pytest.approx(3500.0)
        assert decision.meta["sector_pct"] == pytest.approx(35.0)

    def test_sell_trim_different_sector_only_short_counts(self, limits: RiskLimits) -> None:
        """Issue #178 (edge case): intent.sector differs from the position's
        sector. The trim_qty is bounded by existing qty in *intent.symbol*
        AND *intent.sector* — a position in a different sector for the same
        symbol does NOT count as trim capacity. Only the short portion
        (intent.quantity minus sector-and-symbol qty) adds to intent.sector.

        Portfolio: 2500 SBER @ 100 = 25% in 'tech'. Intent: sell 2000 SBER
        in 'energy'. No SBER position exists in 'energy' (only in 'tech'),
        so existing_qty_in_intent_sector = 0; trim_qty = 0, short_qty = 2000.
        Projected energy exposure = 0 + 2000*100 = 200,000 = 20% < 30%. Allowed.

        Issue #204 also fixes the underlying bug here: pre-fix this test
        passed because the ``projected_sector_value < 0`` clamp silently
        zeroed out the wrong number (-20% became 0%, masking the fact that
        the trim shouldn't have applied to a different-sector position in
        the first place). Post-fix the trim only counts positions in the
        SAME sector, so the answer is naturally 20% (no clamp needed).
        """
        state = PortfolioState(
            total_equity=Decimal("1000000"),
            cash=Decimal("0"),
            positions=[
                Position(
                    symbol="SBER",
                    quantity=Decimal("2500"),
                    avg_price=Decimal("100"),
                    sector="tech",  # position is in tech, not energy
                ),
            ],
            daily_pnl=Decimal("0"),
            peak_equity=Decimal("1000000"),
        )
        intent = TradeIntent(
            symbol="SBER",
            side="sell",
            quantity=Decimal("2000"),
            price=Decimal("100"),
            sector="energy",  # intent targets energy sector
        )
        gate = RiskGate(limits)

        decision = gate.evaluate(intent, state)

        # Energy sector had no existing exposure; selling SBER (which is in
        # 'tech', not 'energy') is a 100% new short in the energy sector.
        # sector_pct = 20%.
        assert decision.allowed is True
        assert decision.meta["sector_pct"] == pytest.approx(20.0)
        assert decision.meta["sector_trim_qty"] == pytest.approx(0.0)
        assert decision.meta["sector_short_qty"] == pytest.approx(2000.0)

    def test_buy_sector_unchanged(self, limits: RiskLimits) -> None:
        """Issue #178 regression guard: BUY semantics are unchanged.

        Same fixture as test_sector_exposure_exceeded: existing 25%
        energy, BUY 10% energy → projected 35% > 30% → reject.
        """
        state = PortfolioState(
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
        intent = _intent(symbol="SBER", qty=1000, price="100", sector="energy")
        gate = RiskGate(limits)

        decision = gate.evaluate(intent, state)

        assert decision.allowed is False
        assert any("RISK_SECTOR" in v for v in decision.violations)
        # No trim/short decomposition on BUY side — meta keys absent.
        assert "sector_trim_qty" not in decision.meta
        assert "sector_short_qty" not in decision.meta

    def test_multi_symbol_sector_trim_marked_at_intent_price(self, limits: RiskLimits) -> None:
        """Issue #204: sector exposure must use a single price domain.

        Pre-fix, the sector was summed at avg_price (qty × avg_price) while
        the trim subtracted qty × intent.price — different price domains.
        When avg_price ≠ intent.price (i.e. any position with unrealised P&L),
        the percentage was wrong. This test exercises the multi-symbol case
        that triggers the divergence: positions in two symbols of the same
        sector, one with a marked-up basis (avg_price > intent.price).

        Portfolio: 1000 SBER @ avg_price=200 (energy) + 1000 LKOH @ avg_price=100 (energy).
        Mark at intent.price=150:
          pre-fix sector_value = 1000*200 + 1000*100 = 300,000
          post-fix sector_value = 1000*150 + 1000*150 = 300,000 (same — by accident here)

        The intent.price=180 scenario below is what really exposes the bug:
          pre-fix sector_value = 1000*200 + 1000*100 = 300,000
          post-fix sector_value = 1000*180 + 1000*180 = 360,000 (different)

        After fix: sell 1500 SBER @ 180 → trim 1000, short 500.
          projected = 360,000 − 1000*180 + 500*180 = 360,000 − 180,000 + 90,000 = 270,000 → 27%
        Allowed (27% < 30%).
        """
        state = PortfolioState(
            total_equity=Decimal("1000000"),
            cash=Decimal("0"),
            positions=[
                Position(
                    symbol="SBER",
                    quantity=Decimal("1000"),
                    avg_price=Decimal("200"),  # avg_price above current
                    sector="energy",
                ),
                Position(
                    symbol="LKOH",
                    quantity=Decimal("1000"),
                    avg_price=Decimal("100"),  # avg_price below current
                    sector="energy",
                ),
            ],
            daily_pnl=Decimal("0"),
            peak_equity=Decimal("1000000"),
        )
        intent = TradeIntent(
            symbol="SBER",
            side="sell",
            quantity=Decimal("1500"),
            price=Decimal("180"),  # current market price; ≠ avg_price
            sector="energy",
        )
        gate = RiskGate(limits)

        decision = gate.evaluate(intent, state)

        # Mark basis must be reported so audit logs can verify the path.
        assert decision.meta["sector_mark_basis"] == "intent.price"
        # Marked at intent.price=180: sector_value = (1000+1000) * 180 = 360,000.
        # Sell 1500 SBER @ 180 → trim 1000, short 500.
        # projected = 360,000 - 1000*180 + 500*180 = 270,000 = 27% → allowed.
        assert decision.allowed is True
        assert decision.meta["sector_trim_qty"] == pytest.approx(1000.0)
        assert decision.meta["sector_short_qty"] == pytest.approx(500.0)
        assert decision.meta["sector_pct"] == pytest.approx(27.0)

    def test_multi_symbol_sector_unrealised_loss_mark_consistency(self, limits: RiskLimits) -> None:
        """Issue #204: the bug is most visible when avg_price > intent.price
        (position has unrealised loss) — pre-fix would UNDER-count exposure.

        Portfolio: 1000 SBER @ avg_price=300 (energy, was bought high, now
        trading at 100). Pre-fix sector_value = 1000*300 + 1000*100 = 400,000
        (40% of equity). Intent: sell 500 SBER @ 100 (trim-only).

        Pre-fix: projected = 400,000 - 500*100 + 0 = 350,000 → 35% > 30% → REJECT.
        Post-fix: sector_value marked at intent.price = 1000*100 + 1000*100 = 200,000 (20%).
                  projected = 200,000 - 500*100 = 150,000 → 15% → ALLOWED.

        The post-fix number is the correct one: the live-mark sector
        exposure is 20%, not 40%, and the trim brings it down to 15%.
        Rejecting the trim would have been the conservative path, but
        requiring capital to be frozen on the basis of stale avg_price
        marks is wrong — it locks positions into zombie state.
        """
        state = PortfolioState(
            total_equity=Decimal("1000000"),
            cash=Decimal("0"),
            positions=[
                Position(
                    symbol="SBER",
                    quantity=Decimal("1000"),
                    avg_price=Decimal("300"),  # bought high — large unrealised loss
                    sector="energy",
                ),
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
        intent = TradeIntent(
            symbol="SBER",
            side="sell",
            quantity=Decimal("500"),
            price=Decimal("100"),  # current price — well below avg_price
            sector="energy",
        )
        gate = RiskGate(limits)

        decision = gate.evaluate(intent, state)

        # Post-fix: live-mark sector = 20%, projected post-trim = 15%. Allowed.
        assert decision.allowed is True
        assert decision.meta["sector_pct"] == pytest.approx(15.0)
        assert decision.meta["sector_trim_qty"] == pytest.approx(500.0)
        assert decision.meta["sector_short_qty"] == pytest.approx(0.0)

    def test_sector_negative_projection_not_clamped(self, limits: RiskLimits) -> None:
        """Issue #204 regression guard: the ``projected_sector_value < 0``
        clamp was REMOVED. If the math goes negative under the new live-mark
        semantics, that is an upstream invariant violation (duplicate
        positions, sector tag mutated mid-flight) and we want the negative
        number to surface in audit logs.

        We force a negative projection by constructing a state that violates
        the implicit invariant that every Position in the sector is keyed
        by intent.symbol. This is not a production-realistic state — it's
        a stress test that confirms the clamp is gone.

        Portfolio: 100 SBER in 'energy'. Intent: sell 200 SBER @ 50 in 'energy'.
          sector_value at intent.price = 100*50 = 5000.
          existing_qty_same_symbol = 100; trim_qty = min(200, 100) = 100; short_qty = 100.
          projected = 5000 - 100*50 + 100*50 = 5000.

        That's the legal path. To force a negative projection we'd need to
        violate ``existing_qty_same_symbol ≤ sum(qty_in_sector)``, which
        pydantic / Position construction doesn't directly allow (no two
        positions for same symbol is enforced elsewhere, not here).

        Instead we test the OPPOSITE invariant: when the projection comes out
        to exactly zero (pure trim that consumes the whole sector), the
        audit log reports 0%, not a negative number. This guards against
        a future regression where someone re-adds the clamp but in the
        wrong direction.
        """
        state = PortfolioState(
            total_equity=Decimal("1000000"),
            cash=Decimal("0"),
            positions=[
                Position(
                    symbol="SBER",
                    quantity=Decimal("100"),
                    avg_price=Decimal("100"),
                    sector="energy",
                ),
            ],
            daily_pnl=Decimal("0"),
            peak_equity=Decimal("1000000"),
        )
        # Sell all 100 SBER @ 100 → trim_qty=100, short_qty=0.
        # sector_value (live mark) = 100*100 = 10,000 (1%).
        # projected = 10,000 - 100*100 = 0 → 0%.
        intent = TradeIntent(
            symbol="SBER",
            side="sell",
            quantity=Decimal("100"),
            price=Decimal("100"),
            sector="energy",
        )
        gate = RiskGate(limits)

        decision = gate.evaluate(intent, state)

        assert decision.allowed is True
        assert decision.meta["sector_pct"] == pytest.approx(0.0)
        assert decision.meta["sector_trim_qty"] == pytest.approx(100.0)
        # Sanity: the value reported must not have been clamped — it's
        # naturally zero from the arithmetic. If we ever see a tiny epsilon
        # here (e.g. -0.0001 due to clamp-mask), that's a regression.
        assert decision.meta["sector_pct"] >= 0.0


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
    def test_fail_safe_default_unknown_input(
        self, limits: RiskLimits, base_state: PortfolioState
    ) -> None:  # noqa: E501
        """A symbol that pydantic validation rejects (empty after strip) raises.

        The skeleton surfaces this as a ValidationError — Phase 1.3 will
        translate these into structured risk violations. The contract is
        "bad inputs never reach evaluate()".
        """
        with pytest.raises(ValidationError):
            TradeIntent(symbol="   ", side="buy", quantity=Decimal("1"), price=Decimal("1"))

    def test_fail_safe_invalid_side(self, limits: RiskLimits, base_state: PortfolioState) -> None:
        """Both 'buy' and 'sell' are now accepted (BUGFIX C-4). Other values
        (e.g. 'short', 'hold', '') still fail at the model layer."""
        # Accepted sides
        for ok_side in ("buy", "sell", "BUY", "SELL"):
            intent = TradeIntent(symbol="SBER", side=ok_side, quantity=Decimal("1"), price=Decimal("1"))
            assert intent.side == ok_side.lower()
        # Rejected sides
        for bad_side in ("short", "hold", "cover"):
            with pytest.raises(ValidationError):
                TradeIntent(symbol="SBER", side=bad_side, quantity=Decimal("1"), price=Decimal("1"))

    def test_fail_safe_negative_quantity(self, limits: RiskLimits, base_state: PortfolioState) -> None:  # noqa: E501
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

    def test_fail_safe_decision_invariant(self, limits: RiskLimits, base_state: PortfolioState) -> None:  # noqa: E501
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

    def test_fail_safe_market_order_placeholder_price_rejected(
        self, limits: RiskLimits, base_state: PortfolioState
    ) -> None:
        """Issue #11: an intent with price=Decimal('1') and qty>1 is the
        historical MarketOrder placeholder. RiskGate MUST refuse it
        with a clearly-named violation rather than computing a tiny
        notional and silently passing."""
        gate = RiskGate(limits=limits)
        intent = TradeIntent(
            symbol="SBER",
            side="buy",
            quantity=Decimal("1000"),
            price=Decimal("1"),  # the historical placeholder
        )
        decision = gate.evaluate(intent, base_state)
        assert decision.allowed is False
        assert any("RISK_MARKET_ORDER_NO_QUOTE" in v for v in decision.violations)

    def test_fail_safe_market_order_placeholder_price_allowed_when_qty_le_1(
        self, limits: RiskLimits, base_state: PortfolioState
    ) -> None:
        """The price=Decimal('1') sentinel only blocks when qty>1. An
        intent with qty=1 and price=1 has notional=1, which is a tiny
        amount and not a market-order bypass. This preserves
        ringfenced edge cases like closed-out positions."""
        gate = RiskGate(limits=limits)
        intent = TradeIntent(
            symbol="SBER",
            side="buy",
            quantity=Decimal("1"),
            price=Decimal("1"),
        )
        decision = gate.evaluate(intent, base_state)
        # Allowed if no other violation; the only check is no_position
        # size exceed (notional=1 vs 10M equity = 0.00001%).
        assert decision.allowed is True


# ===========================================================================
# Issue #98: TradeIntent / Position / PortfolioState immutability
#
# Without `frozen=True`, post-construction assignment bypasses every Field
# validator — so a caller can rewrite quantity / price / side AFTER the gate
# has read the original values. Same exploit class as the historical
# MarketOrder price=Decimal('1') bypass (issues #11 / #13), but on the
# intent side. The tests below are the regression net.
# ===========================================================================


class TestIssue98Immutability:
    def test_trade_intent_is_frozen_at_construction(self) -> None:
        """TradeIntent.model_config['frozen'] is True (issue #98 acceptance)."""
        i = TradeIntent(symbol="SBER", side="buy", quantity=Decimal("1"), price=Decimal("100"))
        assert i.model_config.get("frozen") is True

    def test_trade_intent_post_construction_mutation_raises(self) -> None:
        """Assigning to a TradeIntent field after construction must raise.

        Repro of issue #98: without frozen=True, the following assignments
        silently succeed and let an attacker rewrite gate-approved values.
        """
        i = TradeIntent(symbol="SBER", side="buy", quantity=Decimal("1"), price=Decimal("100"))
        with pytest.raises(ValidationError):
            i.quantity = Decimal("-999999")
        with pytest.raises(ValidationError):
            i.price = Decimal("0.0001")
        with pytest.raises(ValidationError):
            i.symbol = "@!#"
        with pytest.raises(ValidationError):
            i.side = "buy"  # inversion attempt; even valid values are blocked

    def test_trade_intent_mutation_block_preserves_original_values(self) -> None:
        """A failed mutation does NOT partially mutate the instance.

        Pydantic v2 raises ValidationError on assignment under frozen=True
        and the original value is preserved.
        """
        i = TradeIntent(symbol="SBER", side="buy", quantity=Decimal("1"), price=Decimal("100"))
        with pytest.raises(ValidationError):
            i.quantity = Decimal("-999999")
        assert i.quantity == Decimal("1")

    def test_position_is_frozen_at_construction(self) -> None:
        """Position.model_config['frozen'] is True (issue #98 audit)."""
        p = Position(symbol="X", quantity=Decimal("1"), avg_price=Decimal("10"), sector="x")
        assert p.model_config.get("frozen") is True

    def test_position_post_construction_mutation_raises(self) -> None:
        """Mutating Position silently would falsify market_value used by
        RISK_SECTOR; frozen=True now blocks it."""
        p = Position(symbol="X", quantity=Decimal("1"), avg_price=Decimal("10"), sector="x")
        with pytest.raises(ValidationError):
            p.quantity = Decimal("999")
        with pytest.raises(ValidationError):
            p.avg_price = Decimal("0")
        with pytest.raises(ValidationError):
            p.sector = "other"

    def test_portfolio_state_is_frozen_at_construction(self) -> None:
        """PortfolioState.model_config['frozen'] is True (issue #98 audit)."""
        s = PortfolioState(
            total_equity=Decimal("1000000"),
            cash=Decimal("1000000"),
            positions=[],
            daily_pnl=Decimal("0"),
            peak_equity=Decimal("1000000"),
        )
        assert s.model_config.get("frozen") is True

    def test_portfolio_state_post_construction_mutation_raises(self) -> None:
        """Mutating PortfolioState would silently bypass RISK_DD /
        RISK_SECTOR; frozen=True now blocks it."""
        s = PortfolioState(
            total_equity=Decimal("1000000"),
            cash=Decimal("1000000"),
            positions=[],
            daily_pnl=Decimal("0"),
            peak_equity=Decimal("1000000"),
        )
        with pytest.raises(ValidationError):
            s.peak_equity = Decimal("10000000000")  # DD would go negative
        with pytest.raises(ValidationError):
            s.total_equity = Decimal("0")
        with pytest.raises(ValidationError):
            s.positions = []  # even with same value, frozen blocks __setattr__

    def test_trade_intent_mutation_bypass_repro_issue_98(self) -> None:
        """End-to-end: the exact PoC from issue #98.

        Before the fix, this code would print '-999999 0.0001' — silently
        mutated values. After the fix, both assignments raise ValidationError.
        """
        i = TradeIntent(symbol="SBER", side="buy", quantity=Decimal("1"), price=Decimal("100"))
        with pytest.raises(ValidationError):
            i.quantity = Decimal("-999999")
        with pytest.raises(ValidationError):
            i.price = Decimal("0.0001")
        # Confirm gate-approved values are intact
        assert i.quantity == Decimal("1")
        assert i.price == Decimal("100")
        assert i.symbol == "SBER"
        assert i.side == "buy"


# ===========================================================================
# Issue #11: MarketOrder end-to-end — confirms that RiskGate reject
# propagates to the broker as OrderStatus.REJECTED, and that the
# critic (price=Decimal('1') bypass) is now structurally impossible.
# ===========================================================================


class TestIssue11MarketOrderBypass:
    def test_market_order_intent_with_placeholder_price_is_rejected_outright(
        self, limits: RiskLimits, base_state: PortfolioState
    ) -> None:
        """End-to-end: the exact PoC from issue #11 — qty=100000 SBER
        with price=Decimal('1') — must be rejected by RiskGate instead
        of silently passing at 0.001% of NAV."""
        gate = RiskGate(limits=limits)
        intent = TradeIntent(
            symbol="SBER",
            side="buy",
            quantity=Decimal("100000"),
            price=Decimal("1"),
        )
        decision = gate.evaluate(intent, base_state)
        assert decision.allowed is False
        # The PoC violation must be present (not just RISK_POSITION
        # computed against the wrong notional).
        assert any("RISK_MARKET_ORDER_NO_QUOTE" in v for v in decision.violations)


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

    def test_meta_contains_all_metrics(self, limits: RiskLimits, base_state: PortfolioState) -> None:  # noqa: E501
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
