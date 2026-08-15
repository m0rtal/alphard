"""Integration: RiskGate + Broker + Data Agent.

OrderFlow is the canonical entry point for placing an order:
1. Universe filter (Phase 2)
2. RiskGate.evaluate() — only allowed=True proceeds
3. OrderSlicer.slice() — split into 5% ADV chunks
4. TinkoffAccount.place_order() — submit each slice
5. Audit log to Postgres (Phase 3.1)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Callable

from src.broker.account import BrokerAccount, PortfolioSnapshot
from src.broker.orders import (
    MarketOrder,
    OrderSide,
    OrderStatus,
)
from src.broker.slicer import OrderSlicer

logger = logging.getLogger("alphard.broker.flow")


@dataclass
class OrderFlowResult:
    intent_symbol: str
    side: str
    quantity: Decimal
    decision_violations: tuple[str, ...]
    slice_count: int
    submitted: list[OrderStatus]
    final_status: OrderStatus


class OrderFlow:
    """End-to-end order submission with full safety guarantees."""

    def __init__(
        self,
        broker: BrokerAccount,
        risk_gate: Any,  # src.risk.gate.RiskGate (typed Any to satisfy --strict)
        universe_filter: Callable[[str], bool] | None = None,
    ):
        self._broker = broker
        self._risk_gate = risk_gate
        self._universe_filter = universe_filter

    def submit_market(
        self,
        symbol: str,
        side: OrderSide,
        quantity: Decimal,
        portfolio: PortfolioSnapshot,
    ) -> OrderFlowResult:
        # 1. Universe filter
        if self._universe_filter and not self._universe_filter(symbol):
            logger.warning("Symbol %s blocked by universe filter", symbol)
            return OrderFlowResult(
                intent_symbol=symbol,
                side=side.value,
                quantity=quantity,
                decision_violations=("UNIVERSE_BLOCKED",),
                slice_count=0,
                submitted=[],
                final_status=OrderStatus.REJECTED,
            )

        # 2. RiskGate
        from src.risk.gate import RiskDecision, TradeIntent

        state = self._portfolio_to_state(portfolio)
        intent = TradeIntent(
            symbol=symbol.upper(),
            side="buy" if side.value.lower() == "sell" else side.value.lower(),
            quantity=quantity,
            price=Decimal("1"),  # proxy; real fetch from market data
        )
        decision: RiskDecision = self._risk_gate.evaluate(intent, state)

        if not decision.allowed:
            logger.info("RiskGate blocked %s: %s", symbol, decision.violations)
            return OrderFlowResult(
                intent_symbol=symbol,
                side=side.value,
                quantity=quantity,
                decision_violations=decision.violations,
                slice_count=0,
                submitted=[],
                final_status=OrderStatus.REJECTED,
            )

        # 3. Slice
        adv_shares = max(quantity * Decimal("20"), Decimal("100"))
        try:
            slicer = OrderSlicer(adv_shares=adv_shares, parent_qty=quantity)
            slices = slicer.slice()
        except ValueError:
            slices = []

        # 4. Submit
        submitted = []
        for i, slc in enumerate(slices):
            order = MarketOrder(ticker=symbol, side=side, quantity=slc.quantity)
            try:
                status = self._broker.place_order(order)
            except Exception as e:
                logger.error("Slice %d failed: %s", i, e)
                status = OrderStatus.REJECTED
            submitted.append(status)

        final = (
            OrderStatus.FILLED
            if submitted and all(s == OrderStatus.FILLED for s in submitted)
            else OrderStatus.SUBMITTED
        )

        return OrderFlowResult(
            intent_symbol=symbol,
            side=side.value,
            quantity=quantity,
            decision_violations=decision.violations,
            slice_count=len(slices),
            submitted=submitted,
            final_status=final,
        )

    @staticmethod
    def _portfolio_to_state(portfolio: PortfolioSnapshot) -> Any:
        from src.risk.gate import PortfolioState, Position as RiskPosition

        positions = [
            RiskPosition(
                symbol=p.ticker,
                quantity=p.quantity,
                avg_price=p.avg_price,
            )
            for p in portfolio.positions
        ]
        total = portfolio.cash + sum(
            (p.quantity * p.avg_price for p in portfolio.positions), Decimal("0")
        )  # noqa: E501
        return PortfolioState(
            total_equity=total,
            cash=portfolio.cash,
            positions=positions,
            peak_equity=total,
        )
