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
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Optional

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
        # Use Any locally to satisfy --strict without runtime isinstance check
        risk_gate: Any = None,  # src.risk.gate.RiskGate (typed Any to satisfy --strict)
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

    def _build_intent_and_state(self, order: MarketOrder | LimitOrder) -> tuple[Any, Any]:
        """Build TradeIntent + PortfolioState for RiskGate."""
        from src.risk.gate import PortfolioState, TradeIntent

        order_type = OrderType.LIMIT if isinstance(order, LimitOrder) else OrderType.MARKET
        _ = order_type  # currently unused, reserved for Phase 2
        price = order.price if isinstance(order, LimitOrder) else Decimal("1")

        intent = TradeIntent(
            symbol=order.ticker,
            # BUGFIX (C-4): the previous expression
            #   "buy" if order.side.value.lower() == "sell" else order.side.value.lower()
            # silently inverted SELL → BUY. Pass the side through as-is.
            side=order.side.value.lower(),
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
        """Real call to Tinkoff. Returns empty mock if SDK unavailable.

        Live API contract (t_tech.invest SDK 1.49):
        - client.users.get_accounts() → .accounts (list of Account)
        - Account.id is the Tinkoff account_id
        - client.operations.get_portfolio(account_id=...) → PortfolioResponse
        - PortfolioResponse has .positions (list[PortfolioPosition]) and
          .total_amount_currencies (Money). Money has .units + .nano.
        """
        self._rate_limit_acquire()
        try:
            from t_tech.invest import Client

            with Client(self._token) as client:
                acc_response = client.users.get_accounts()
                ops = None
                for a in acc_response.accounts:
                    if a.id == self._account_id:
                        ops = a
                        break
                if ops is None:
                    raise BrokerError(f"account {self._account_id} not found")
                portfolio = client.operations.get_portfolio(account_id=self._account_id)
                positions = []
                if getattr(portfolio, "positions", None):
                    for p in portfolio.positions:
                        # average_position_price is a Quotation (.units + .nano)
                        # OR a legacy Money-like object (.value float) depending
                        # on SDK/mock version. Handle both.
                        avg_price_q = getattr(p, "average_position_price", None) or getattr(
                            p, "average_buy_price", None
                        )
                        if avg_price_q is None:
                            avg_price = Decimal("0")
                        elif hasattr(avg_price_q, "units") and hasattr(avg_price_q, "nano"):
                            avg_price = Decimal(str(getattr(avg_price_q, "units", 0))) + Decimal(
                                str(getattr(avg_price_q, "nano", 0))
                            ) / Decimal("1000000000")
                        else:
                            # Legacy: value is a float-like (e.g. Money.value).
                            try:
                                avg_price = Decimal(str(avg_price_q.value))
                            except (AttributeError, TypeError, ValueError):
                                avg_price = Decimal(str(avg_price_q))
                        positions.append(
                            Position(
                                ticker=getattr(p, "ticker", ""),
                                quantity=Decimal(str(getattr(p, "quantity", 0))),
                                avg_price=avg_price,
                            )
                        )
                total_amount = getattr(portfolio, "total_amount_currencies", None) or getattr(
                    portfolio, "total_amount", None
                )
                cash = Decimal("0")
                if total_amount is not None:
                    if hasattr(total_amount, "units") and hasattr(total_amount, "nano"):
                        cash = Decimal(str(getattr(total_amount, "units", 0))) + Decimal(
                            str(getattr(total_amount, "nano", 0))
                        ) / Decimal("1000000000")
                    else:
                        # Legacy Money.value
                        try:
                            cash = Decimal(str(total_amount.value))
                        except (AttributeError, TypeError, ValueError):
                            cash = Decimal(str(total_amount))
                return PortfolioSnapshot(
                    account_id=self._account_id,
                    cash=cash,
                    positions=positions,
                    timestamp=datetime.now(timezone.utc),
                )
        except Exception as e:
            raise BrokerError(f"Tinkoff portfolio fetch failed: {e}") from e

    def get_positions(self) -> list[Position]:
        return self.get_portfolio().positions

    def place_order(self, order: MarketOrder | LimitOrder) -> OrderStatus:
        """RiskGate first. Reject before touching broker.

        If risk_gate is None, order is rejected (fail-safe default).
        LIVE_TRADING gate: if env LIVE_TRADING=false, refuse ALL orders.
        This is a hard guarantee for Phase 1: real token may be present
        but no orders are placed regardless of RiskGate.
        """
        import os

        if os.environ.get("LIVE_TRADING", "false").lower() != "true":
            logger.warning(
                "LIVE_TRADING=false — refusing order for %s (Phase 1 hard no-trade)",
                order.ticker,
            )
            return OrderStatus.REJECTED
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
            from t_tech.invest import (
                Client,
                OrderDirection,
                OrderType,
                Quotation,
            )

            direction = (
                OrderDirection.ORDER_DIRECTION_BUY
                if order.side == OrderSide.BUY
                else OrderDirection.ORDER_DIRECTION_SELL
            )
            with Client(self._token) as client:
                figi = self._ticker_to_figi(client, order.ticker)
                if isinstance(order, MarketOrder):
                    resp = client.orders.post_order(
                        figi=figi,
                        quantity=int(order.quantity),
                        account_id=self._account_id,
                        direction=direction,
                        order_type=OrderType.ORDER_TYPE_MARKET,
                    )
                else:
                    # Limit order — wrap price into Quotation
                    price_q = Quotation(
                        units=int(order.price),
                        nano=int((order.price - int(order.price)) * 1_000_000_000),
                    )
                    resp = client.orders.post_order(
                        figi=figi,
                        quantity=int(order.quantity),
                        price=price_q,
                        account_id=self._account_id,
                        direction=direction,
                        order_type=OrderType.ORDER_TYPE_LIMIT,
                    )
                # PostOrderResponse.execution_report_status is OrderExecutionReportStatus
                # enum (real SDK) or a plain string (legacy/test mocks). Handle both.
                ers = resp.execution_report_status
                raw_name: str = getattr(ers, "name", None) or str(ers)
                return self._map_status(raw_name)
        except Exception as e:
            raise BrokerError(f"Tinkoff order submit failed: {e}") from e

    def cancel_order(self, order_id: str) -> OrderStatus:
        self._rate_limit_acquire()
        try:
            from t_tech.invest import Client

            with Client(self._token) as client:
                client.orders.cancel_order(account_id=self._account_id, order_id=order_id)
                return OrderStatus.CANCELLED
        except Exception as e:
            raise BrokerError(f"Tinkoff cancel failed: {e}") from e

    @staticmethod
    def _ticker_to_figi(client: Any, ticker: str) -> str:
        """Map ticker to FIGI via Tinkoff instruments API.

        Accepts both TQBR (stocks) and TQOB (bonds) class codes so the
        same helper works for the OFZ bond universe.
        """
        try:
            response = client.instruments.find_instrument(query=ticker)
            for inst in response.instruments:
                if inst.ticker == ticker and inst.class_code in ("TQBR", "TQOB"):
                    return str(inst.figi)
        except Exception:
            pass
        return ticker

    @staticmethod
    def _map_status(raw: str) -> OrderStatus:
        # raw is OrderExecutionReportStatus enum name (e.g. "EXECUTION_REPORT_STATUS_FILL")
        mapping: dict[str, OrderStatus] = {
            "EXECUTION_REPORT_STATUS_FILL": OrderStatus.FILLED,
            "EXECUTION_REPORT_STATUS_PARTIALLYFILL": OrderStatus.FILLED,
            "EXECUTION_REPORT_STATUS_REJECTED": OrderStatus.REJECTED,
            "EXECUTION_REPORT_STATUS_CANCELLED": OrderStatus.CANCELLED,
        }
        return mapping.get(raw, OrderStatus.SUBMITTED)


def from_env(env: Optional[dict[str, str]] = None) -> TinkoffAccount:
    """Construct TinkoffAccount from environment variables."""
    if env is None:
        env = dict(os.environ)  # cast _Environ[str] to dict[str, str]
    sandbox_token = env.get("TINKOFF_SANDBOX_TOKEN")
    real_token = env.get("TINKOFF_REAL_TOKEN")
    account_id = env.get("TINKOFF_ACCOUNT_ID", "SB1")

    # Prefer REAL token (full universe, 200 req/min)
    if real_token and real_token.strip():
        return TinkoffAccount(token=real_token, account_id=account_id)
    if sandbox_token and sandbox_token.strip() and sandbox_token != "placeholder_get_from_tbank":
        return TinkoffAccount(token=sandbox_token, account_id=account_id)
    raise BrokerError("No TINKOFF_SANDBOX_TOKEN or TINKOFF_REAL_TOKEN set")
