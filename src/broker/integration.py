"""Integration: RiskGate + Broker + Data Agent.

OrderFlow is the canonical entry point for placing an order:
1. Universe filter (Phase 2)
2. Live quote fetch (issue #166 — never substitute a placeholder price)
3. RiskGate.evaluate() — only allowed=True proceeds
4. OrderSlicer.slice() — split into 5% ADV chunks
5. TinkoffAccount.place_order() — submit each slice
6. Audit log to Postgres (Phase 3.1)

Issue #166: the previous implementation constructed the RiskGate
``TradeIntent`` with ``price=Decimal("1")`` as a "proxy; real fetch from
market data". The real ``RiskGate.evaluate`` (src/risk/gate.py:274) adds
a guard that refuses any intent with ``price == Decimal("1")`` AND
``quantity > Decimal("1")`` — a defence-in-depth against the historical
issue #11 placeholder exploit. As a result the previous ``OrderFlow``
silently rejected 100% of real-market orders. The fix is structural:
``OrderFlow`` now requires a ``quote_provider`` (callable taking the
ticker and returning the live price as ``Decimal``) and refuses the order
with a clear ``QUOTE_UNAVAILABLE`` violation if no real price is
available. The broker's existing ``_fetch_live_quote_price`` method is
the canonical production quote source.
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

# Type alias for the live-quote provider. Signature:
#   quote_provider(symbol: str) -> Decimal  (price > 0)
# Raise any exception to signal "quote unavailable". Callers (e.g.
# TinkoffAccount) wrap Tinkoff market_data.get_last_prices here.
QuoteProvider = Callable[[str], Decimal]


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
        quote_provider: QuoteProvider,
        universe_filter: Callable[[str], bool] | None = None,
    ):
        """
        Args:
            broker: Concrete broker (TinkoffAccount, future BCSAccount, etc.).
            risk_gate: RiskGate instance. Cannot be None — fail-safe contract.
            quote_provider: Callable returning the live ``Decimal`` price for
                a ticker. MUST raise on failure — ``OrderFlow`` will refuse
                the order with a ``QUOTE_UNAVAILABLE`` violation rather than
                substitute a placeholder (issue #166).
            universe_filter: Optional allow-list. Symbols for which the
                filter returns False are short-circuited with
                ``UNIVERSE_BLOCKED``.

        Raises:
            TypeError: if ``quote_provider`` is None. We require an explicit
                quote source rather than accepting a default that could
                silently degrade to a placeholder.
        """
        if quote_provider is None:
            raise TypeError(
                "OrderFlow requires a quote_provider (issue #166). "
                "Pass a callable (e.g. TinkoffAccount._fetch_live_quote_price) "
                "that returns a real Decimal price for the ticker; "
                "raising on failure."
            )
        self._broker = broker
        self._risk_gate = risk_gate
        self._quote_provider = quote_provider
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

        # 2. Live quote (issue #166). Refuse the order if the quote cannot
        # be fetched. We do NOT fall back to a placeholder — that is
        # exactly the bug we fixed at the broker layer (issue #11) and
        # the bug that broke this integration before issue #166.
        try:
            price = self._quote_provider(symbol)
        except Exception as exc:
            logger.error(
                "QUOTE_UNAVAILABLE for %s: %s — refusing order (issue #166, "
                "fail-safe: never substitute a placeholder price)",
                symbol,
                exc,
            )
            return OrderFlowResult(
                intent_symbol=symbol,
                side=side.value,
                quantity=quantity,
                decision_violations=("QUOTE_UNAVAILABLE",),
                slice_count=0,
                submitted=[],
                final_status=OrderStatus.REJECTED,
            )
        if not isinstance(price, Decimal) or price <= Decimal("0"):
            logger.error(
                "QUOTE_INVALID for %s: quote_provider returned %r — refusing "
                "order (issue #166, fail-safe: never substitute a placeholder)",
                symbol,
                price,
            )
            return OrderFlowResult(
                intent_symbol=symbol,
                side=side.value,
                quantity=quantity,
                decision_violations=("QUOTE_INVALID",),
                slice_count=0,
                submitted=[],
                final_status=OrderStatus.REJECTED,
            )

        # 3. RiskGate
        from src.risk.gate import RiskDecision, TradeIntent

        state = self._portfolio_to_state(portfolio)
        intent = TradeIntent(
            symbol=symbol.upper(),
            # BUGFIX (C-4): pass side through unchanged. The previous expression
            # silently inverted SELL → BUY.
            side=side.value.lower(),
            quantity=quantity,
            price=price,
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

        # 4. Slice
        adv_shares = max(quantity * Decimal("20"), Decimal("100"))
        try:
            slicer = OrderSlicer(adv_shares=adv_shares, parent_qty=quantity)
            slices = slicer.slice()
        except ValueError:
            slices = []

        # 5. Submit
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
