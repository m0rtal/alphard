"""BrokerAccount ABC — the interface every broker adapter implements.

Why an ABC at all?
------------------
Phase 1.4+ needs to swap broker backends (Tinkoff now, Finam / IBKR / a
fake broker in tests later). The ABC keeps the rest of the code (RiskGate
wiring, the executors, the audit log) decoupled from which concrete broker
we have today.

Hard invariants encoded in the ABC
-----------------------------------
1. ``submit_intent`` is the only public path from a ``TradeIntent`` to a
   live order. It MUST call :meth:`RiskGate.evaluate` first; rejecting an
   intent at the gate raises :class:`OrderRejectedByRisk` and never reaches
   ``place_order``. Subclasses cannot override this without re-implementing
   submit_intent (they cannot easily bypass it from place_order because
   ``place_order`` does not accept a ``TradeIntent`` at all).
2. ``place_order`` accepts *only* :class:`MarketOrder` or :class:`LimitOrder`
   shapes. It is the primitive the slicer talks to, and the slicer never
   has a ``TradeIntent`` handy — so the only way to bypass the risk gate
   via this method would be to construct an order outside submit_intent,
   which is forbidden by project rules.
3. ``cancel_order`` does NOT call the risk gate (cancellation is always
   safe — it unwinds exposure). It is allowed to fail, in which case the
   order remains live.
4. Broker adapters must be cheap to construct and MUST NOT do network I/O
   in ``__init__``. Connection establishment happens in an explicit
   :meth:`connect` coroutine (sync in Phase 1.3 — async is Phase 2).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from decimal import Decimal
from typing import cast

from pydantic import BaseModel, ConfigDict, Field, field_validator

from src.risk.gate import PortfolioState, RiskGate, TradeIntent

from .orders import AccountOrder, LimitOrder, MarketOrder, OrderResult, OrderSide

import re

_TICKER_REGEX = re.compile(r"^[A-Z0-9@._-]{1,12}$")


class OrderRejectedByRisk(Exception):
    """Raised by :meth:`BrokerAccount.submit_intent` when the risk gate denies.

    Carries the intent + a snapshot of the gate's decision so audit code can
    record the rejection without re-running evaluate().
    """

    def __init__(self, intent: TradeIntent, decision_msg: str, violations: tuple[str, ...]) -> None:
        self.intent = intent
        self.decision_msg = decision_msg
        self.violations = violations
        super().__init__(f"risk gate rejected {intent.symbol} {intent.side} {intent.quantity}: {decision_msg}")


class Balance(BaseModel):
    """Account balance snapshot.

    Phase 1.3 only uses ``cash`` and ``currency`` — the rest is forward-compatible
    storage that arrives in Phase 1.4 (multi-currency, accrued interest,
    margin debt, etc.).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    cash: Decimal = Field(..., ge=Decimal("0"))
    currency: str = Field(default="RUB", min_length=3, max_length=3)
    # Net liquidation value (positions + cash), best-effort from broker.
    net_liquidation: Decimal | None = Field(default=None, ge=Decimal("0"))


class AccountPosition(BaseModel):
    """A single open position on a broker account.

    Sign convention: positive = long, negative = short. ``quantity`` is in
    shares (NOT lots) — lot handling is the broker adapter's job when we
    sell.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    ticker: str
    quantity: Decimal
    avg_price: Decimal = Field(..., ge=Decimal("0"))
    market_value: Decimal | None = Field(default=None, ge=Decimal("0"))

    @field_validator("ticker")
    @classmethod
    def _v_ticker(cls, v: str) -> str:
        v = v.upper().strip()
        if not _TICKER_REGEX.match(v):
            raise ValueError(f"invalid ticker {v!r}: must match {_TICKER_REGEX.pattern}")
        return v


class BrokerAccount(ABC):
    """Abstract broker account.

    Concrete subclasses (``TinkoffAccount``, test fakes) implement the four
    primitive operations; the higher-level :meth:`submit_intent` is shared
    here and enforces the pre-trade risk gate uniformly.

    Constructor contract
    --------------------
    Subclasses may accept configuration (token, endpoint URL) but MUST NOT
    perform network I/O before :meth:`connect` is called.
    """

    # ----- introspection (always available) -------------------------------

    @property
    @abstractmethod
    def account_id(self) -> str:
        """Stable account id (used for routing and audit logs)."""

    @property
    @abstractmethod
    def is_sandbox(self) -> bool:
        """True if this account is the sandbox — refuses to connect to a non-sandbox token."""

    # ----- lifecycle ------------------------------------------------------

    @abstractmethod
    def connect(self) -> None:
        """Establish the session. Raises on auth/network failure."""

    @abstractmethod
    def close(self) -> None:
        """Tear down. Idempotent. No errors after the first call."""

    # ----- read-only views ------------------------------------------------

    @abstractmethod
    def get_balance(self) -> Balance:
        """Snapshot the account balance. Raises on broker failure."""

    @abstractmethod
    def get_positions(self) -> list[AccountPosition]:
        """Snapshot open positions. Sorted by ticker."""

    @abstractmethod
    def get_orders(self) -> list[AccountOrder]:
        """Snapshot working and recently filled orders."""

    # ----- write paths ----------------------------------------------------

    @abstractmethod
    def place_order(self, order: MarketOrder | LimitOrder) -> OrderResult:
        """Submit a single MarketOrder or LimitOrder. The risk gate has already run.

        Implementations must:
        * consume one token from the shared rate limiter *before* the broker call;
        * map broker "rejected for risk" responses to ``OrderResult(ok=True, status="rejected", ...)``
          (NOT ``ok=False``) — that is the broker's voice, not ours;
        * map network/auth/rate-limit failures to ``OrderResult(ok=False, ...)``.

        Implementations must NOT (cannot, because the type signature is
        MarketOrder|LimitOrder) receive a TradeIntent here.
        """

    @abstractmethod
    def cancel_order(self, broker_order_id: str) -> OrderResult:
        """Cancel a working order. Idempotent on already-cancelled IDs.

        Does NOT invoke the risk gate — cancellation is always safe.
        """

    # ----- high-level: the only path from a TradeIntent to a live order --

    def submit_intent(
        self,
        intent: TradeIntent,
        portfolio_state: PortfolioState,
        risk_gate: RiskGate,
    ) -> OrderResult:
        """Run the risk gate, then translate the allowed intent into a MarketOrder.

        This is the SOLE public method that turns a TradeIntent into a live
        order. Concurrency-wise, this is single-threaded in Phase 1.3; multi-
        threaded execution becomes Phase 2.

        Rejection modes
        ---------------
        * ``intent.sector`` is ``None`` —> rejected (Phase 1.3 policy:
          unknown-sector instruments are not tradeable).
        * gate returns ``allowed=False`` —> raises ``OrderRejectedByRisk``.
        * gate allows but the broker denies the resulting market order —
          the call still returns ``OrderResult`` with ``status="rejected"``
          (an exchange-level rejection, not a gate rejection).
        """
        if intent.sector is None:
            raise OrderRejectedByRisk(
                intent,
                "intent.sector is None; Phase 1.3 requires non-null sector",
                tuple(),
            )

        decision = risk_gate.evaluate(intent, portfolio_state)
        if not decision.allowed:
            raise OrderRejectedByRisk(
                intent,
                f"risk gate denied with {len(decision.violations)} violation(s)",
                decision.violations,
            )

        # The risk gate has spoken. Translate into a MarketOrder. We use market
        # orders rather than limits because Phase 1.3's strategy is momentum/
        # mean-reversion alpha; price precision beyond the gate's risk-cleared
        # notional is a Phase 2 concern.
        order = MarketOrder(
            ticker=intent.symbol,
            side=cast(OrderSide, intent.side),
            quantity=intent.quantity,
            account_id=intent.account_id,
            client_order_id=intent.client_order_id or "",
        )
        return self.place_order(order)


__all__ = [
    "AccountPosition",
    "Balance",
    "BrokerAccount",
    "OrderRejectedByRisk",
]
