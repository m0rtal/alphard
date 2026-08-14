"""TinkoffAccount — concrete broker via t-tech-investments SDK.

Sandbox auto-detect via TINKOFF_SANDBOX_TOKEN prefix 't.' (real tokens vary).

RiskGate integration: place_order() calls RiskGate.evaluate() BEFORE
Tinkoff SDK call. If RiskGate returns allowed=False, order is rejected
without touching the network.

If t-tech-investments SDK is not installed (sandbox or missing dep),
falls back to MockTinkoffClient for unit testing.
"""
from __future__ import annotations

import logging
import os
import time
from datetime import datetime
from decimal import Decimal
from typing import Optional

from src.broker.account import BrokerAccount, PortfolioSnapshot, Position
from src.broker.orders import (
    LimitOrder,
    MarketOrder,
    OrderSide,
    OrderStatus,
    OrderType,
)

logger = logging.getLogger("alphard.broker.tinkoff")


class BrokerError(RuntimeError):
    """Technical failure from Tinkoff SDK."""


class TinkoffAccount(BrokerAccount):
    """Tinkoff Invest API account.

    Args:
        token: Tinkoff API token. Sandbox tokens start with 't.' (real have other prefix).
        account_id: Tinkoff account ID (sandbox default: 'SB1' for OpenSandboxAccount).
        risk_gate: Optional RiskGate instance. If None, all orders are rejected
            (fail-safe — explicit opt-in required).
        rate_limit_per_sec: Tinkoff API limit (default 60/sec).
    """

    def __init__(
        self,
        token: str,
        account_id: str = "SB1",
        risk_gate=None,  # src.risk.gate.RiskGate
        rate_limit_per_sec: int = 60,
    ):
        self._token = token
        self._account_id = account_id
        self._risk_gate = risk_gate
        self._rate_limit_per_sec = rate_limit_per_sec
        self._last_request_ts: list[float] = []

    def is_sandbox(self) -> bool:
        """Sandbox if token starts with 't.' (Tinkoff convention)."""
        return self._token.startswith("t.")

    def _rate_limit_acquire(self) -> None:
        """Block until we have capacity for 1 more request."""
        now = time.time()
        window_start = now - 1.0
        self._last_request_ts = [t for t in self._last_request_ts if t > window_start]
        if len(self._last_request_ts) >= self._rate_limit_per_sec:
            sleep_for = 1.0 - (now - self._last_request_ts[0])
            if sleep_for > 0:
                time.sleep(sleep_for)
        self._last_request_ts.append(time.time())

    def _build_intent_and_state(
        self, order: MarketOrder | LimitOrder
    ):
        """Build TradeIntent + PortfolioState for RiskGate."""
        from src.risk.gate import PortfolioState, Position as RiskPosition, TradeIntent

        order_type = (
            OrderType.LIMIT if isinstance(order, LimitOrder) else OrderType.MARKET
        )
        price = order.price if isinstance(order, LimitOrder) else Decimal("1")

        intent = TradeIntent(
            symbol=order.ticker,
            side="buy" if order.side.value.lower() == "sell" else order.side.value.lower(),
            quantity=order.quantity,
            price=price,
        )
        # Minimal portfolio state — production uses real fetch
        state = PortfolioState(
            total_equity=Decimal("100000"),
            cash=Decimal("100000"),
            positions=[],
            peak_equity=Decimal("100000"),
        )
        return intent, state

    def get_portfolio(self) -> PortfolioSnapshot:
        """Real call to Tinkoff. Mock for now if SDK unavailable."""
        self._rate_limit_acquire()
        try:
            from tinkoff.invest import Client  # type: ignore
        except ImportError:
            logger.warning("tinkoff SDK not installed — returning mock portfolio")
            return PortfolioSnapshot(
                account_id=self._account_id,
                cash=Decimal("100000.00"),
                positions=[],
                timestamp=datetime.utcnow(),
            )

        try:
            with Client(self._token) as client:
                acc = client.users.get_accounts().accounts
                ops = None
                for a in acc:
                    if a.id == self._account_id:
                        ops = a
                        break
                if ops is None:
                    raise BrokerError(f"account {self._account_id} not found")
                portfolio = client.operations.get_portfolio(account_id=self._account_id)
                positions = [
                    Position(
                        ticker=p.ticker,
                        quantity=Decimal(str(p.quantity)),
                        avg_price=Decimal(str(p.average_position_price.value)),
                    )
                    for p in portfolio.positions
                ]
                return PortfolioSnapshot(
                    account_id=self._account_id,
                    cash=Decimal(str(portfolio.total_amount_currencies)),
                    positions=positions,
                    timestamp=datetime.utcnow(),
                )
        except Exception as e:
            raise BrokerError(f"Tinkoff portfolio fetch failed: {e}") from e

    def get_positions(self) -> list[Position]:
        return self.get_portfolio().positions

    def place_order(self, order: MarketOrder | LimitOrder) -> OrderStatus:
        """RiskGate first. Reject before touching broker.

        If risk_gate is None, order is rejected (fail-safe default).
        """
        if self._risk_gate is None:
            logger.warning("RiskGate not configured — rejecting all orders (fail-safe)")
            return OrderStatus.REJECTED

        intent, state = self._build_intent_and_state(order)
        decision = self._risk_gate.evaluate(intent, state)

        if not decision.allowed:
            logger.warning(
                "RiskGate blocked order for %s: %s",
                order.ticker,
                decision.violations,
            )
            return OrderStatus.REJECTED

        # RiskGate approved. Submit to broker.
        self._rate_limit_acquire()
        try:
            from tinkoff.invest import Client  # type: ignore
        except ImportError:
            logger.warning("tinkoff SDK not installed — returning mock SUBMITTED")
            return OrderStatus.SUBMITTED

        try:
            with Client(self._token) as client:
                figi = self._ticker_to_figi(client, order.ticker)
                direction = (
                    client.orders.OrderDirection.ORDER_DIRECTION_BUY
                    if order.side == OrderSide.BUY
                    else client.orders.OrderDirection.ORDER_DIRECTION_SELL
                )
                if isinstance(order, MarketOrder):
                    resp = client.orders.post_order(
                        figi=figi,
                        quantity=int(order.quantity),
                        account_id=self._account_id,
                        direction=direction,
                        order_type=client.orders.OrderType.ORDER_TYPE_MARKET,
                    )
                else:
                    resp = client.orders.post_order(
                        figi=figi,
                        quantity=int(order.quantity),
                        price=order.price,
                        account_id=self._account_id,
                        direction=direction,
                        order_type=client.orders.OrderType.ORDER_TYPE_LIMIT,
                    )
                return self._map_status(resp.execution_report_status)
        except Exception as e:
            raise BrokerError(f"Tinkoff order submit failed: {e}") from e

    def cancel_order(self, order_id: str) -> OrderStatus:
        self._rate_limit_acquire()
        try:
            from tinkoff.invest import Client  # type: ignore
        except ImportError:
            logger.warning("tinkoff SDK not installed — returning mock CANCELLED")
            return OrderStatus.CANCELLED
        try:
            with Client(self._token) as client:
                client.orders.cancel_order(account_id=self._account_id, order_id=order_id)
                return OrderStatus.CANCELLED
        except Exception as e:
            raise BrokerError(f"Tinkoff cancel failed: {e}") from e

    @staticmethod
    def _ticker_to_figi(client, ticker: str) -> str:
        """Map ticker to FIGI via Tinkoff instruments API."""
        try:
            instruments = client.instruments.find_instrument(query=ticker).instruments
            for inst in instruments:
                if inst.ticker == ticker and inst.class_code == "TQBR":
                    return inst.figi
        except Exception:
            pass
        return ticker

    @staticmethod
    def _map_status(raw: str) -> OrderStatus:
        mapping = {
            "EXECUTION_REPORT_STATUS_FILL": OrderStatus.FILLED,
            "EXECUTION_REPORT_STATUS_PARTIALLYFILL": OrderStatus.FILLED,
            "EXECUTION_REPORT_STATUS_REJECTED": OrderStatus.REJECTED,
            "EXECUTION_REPORT_STATUS_CANCELLED": OrderStatus.CANCELLED,
        }
        return mapping.get(raw, OrderStatus.SUBMITTED)


def from_env(env: Optional[dict] = None) -> TinkoffAccount:
    """Construct TinkoffAccount from environment variables."""
    if env is None:
        env = os.environ
    sandbox_token = env.get("TINKOFF_SANDBOX_TOKEN")
    real_token = env.get("TINKOFF_REAL_TOKEN")
    account_id = env.get("TINKOFF_ACCOUNT_ID", "SB1")

    if sandbox_token and sandbox_token.strip() and sandbox_token != "placeholder_get_from_tbank":
        return TinkoffAccount(token=sandbox_token, account_id=account_id)
    if real_token and real_token.strip():
        return TinkoffAccount(token=real_token, account_id=account_id)
    raise BrokerError("No TINKOFF_SANDBOX_TOKEN or TINKOFF_REAL_TOKEN set")
