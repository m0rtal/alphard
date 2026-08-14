"""Order and fill primitives shared by the broker ABC and its implementations.

These are *abstract* order shapes — pydantic-validated so the rest of the
system can pass them around without juggling dicts of dicts. Concrete
broker serialization (Tinkoff OrdersState / MoneyValue / Quotation) lives
in :mod:`src.broker.tinkoff_account`.

Design choices
--------------
1. ``pydantic.BaseModel`` (not dataclass) for every model — same rationale as
   :mod:`src.risk.gate`: validation at the boundary is part of the data
   layer's job, and frozen=False on input order shapes lets the broker tag
   them with ``OrderResult`` later in their lifecycle.
2. ``OrderSide`` is ``Literal["buy","sell"]`` because the risk gate already
   rejects ``"short"`` at the model layer. Two values, eight bits, simple.
3. ``OrderStatus`` mirrors Tinkoff's lifecycle (``new`` / ``partially_filled``
   / ``filled`` / ``cancelled`` / ``rejected``) plus a Phase 1.3-specific
   ``"submitted"`` that means "we sent it to the broker, no ack yet".
4. ``OrderResult`` is the canonical return type of all broker place_order /
   cancel_order calls — pydantic, frozen=True, no `model_*` mutation. This
   makes audit logs reproducible.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

# Reuse the ticker's strict validator from src.data.models for consistency.
# We intentionally inline the regex here instead of importing the models
# module — keeps the broker package importable without dragging the data
# agent's Postgres dependency surface into every transaction.
import re

_TICKER_REGEX = re.compile(r"^[A-Z0-9@._-]{1,12}$")

OrderSide = Literal["buy", "sell"]
OrderType = Literal["market", "limit"]
# Lifecycle stages:
#   "submitted"   — we have *not* heard from the exchange yet (Tinkoff ACK pending / network retry).
#   "new"         — exchange has accepted the order, working in book.
#   "partially_filled" — part of the qty has been filled, remainder still working.
#   "filled"      — fully executed.
#   "cancelled"   — cancelled (by us, by the exchange, or by the user).
#   "rejected"    — exchange refused the order (margin, restriction, etc.).
OrderStatus = Literal[
    "submitted",
    "new",
    "partially_filled",
    "filled",
    "cancelled",
    "rejected",
]


class _OrderBase(BaseModel):
    """Common input fields for MarketOrder / LimitOrder.

    Kept private (leading underscore on class name) because abstract —
    concrete inputs extend it via inheritance. ``client_order_id`` is what
    makes the request idempotent on the broker side.
    """

    model_config = ConfigDict(extra="forbid", frozen=False)

    ticker: str = Field(
        ...,
        description="Instrument ticker, e.g. 'SBER'. Uppercased and validated.",
    )
    side: OrderSide
    quantity: Decimal = Field(..., gt=Decimal("0"), description="Number of shares > 0")
    account_id: str = Field(default="default", min_length=1)
    # Idempotency key. If two orders share client_order_id, only one will
    # create a live position on the broker.
    client_order_id: str = Field(default="", max_length=64)

    @field_validator("ticker")
    @classmethod
    def _v_ticker(cls, v: str) -> str:
        v = v.upper().strip()
        if not _TICKER_REGEX.match(v):
            raise ValueError(f"invalid ticker {v!r}: must match {_TICKER_REGEX.pattern}")
        return v

    @field_validator("client_order_id")
    @classmethod
    def _v_client_order_id(cls, v: str) -> str:
        v = v.strip()
        return v[:64]


class MarketOrder(_OrderBase):
    """A market order — fills at the next available price."""

    model_config = ConfigDict(extra="forbid", frozen=False)

    type: Literal["market"] = "market"


class LimitOrder(_OrderBase):
    """A limit order — fills only at ``price`` or better."""

    model_config = ConfigDict(extra="forbid", frozen=False)

    type: Literal["limit"] = "limit"
    # Tinkoff explicitly forbids limit price == 0 (you must set a real bound).
    price: Decimal = Field(..., gt=Decimal("0"))


# Discrimination union — handy in `match` statements and in pydantic's
# discriminated unions. The risk gate receives orders as a TypedDict-like
# view, but the ABC prefers concrete subclasses for broker-call dispatch.
AnyOrder = MarketOrder | LimitOrder


class AccountOrder(BaseModel):
    """An order as it exists on the broker side — the post-submission view.

    Filled quantities / average price are updated by ``poll`` / the
    streaming event hook. Phase 1.3 uses synchronous REST + manual poll;
    streaming is a Phase 2 concern.
    """

    model_config = ConfigDict(extra="forbid", frozen=False)

    broker_order_id: str = Field(..., min_length=1, description="Broker-assigned ID")
    account_id: str = Field(default="default", min_length=1)
    ticker: str
    side: OrderSide
    type: OrderType
    # Original requested quantity (limit/market).
    requested_qty: Decimal = Field(..., gt=Decimal("0"))
    # Quantity that has actually filled so far. <= requested_qty, monotonically non-decreasing.
    filled_qty: Decimal = Field(default=Decimal("0"), ge=Decimal("0"))
    # Volume-weighted average fill price, or None if no fills yet.
    avg_fill_price: Decimal | None = Field(default=None, ge=Decimal("0"))
    # Limit price (only for LimitOrder).
    limit_price: Decimal | None = Field(default=None, ge=Decimal("0"))
    status: OrderStatus = "submitted"
    client_order_id: str = Field(default="", max_length=64)
    submitted_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="UTC submission time",
    )
    updated_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="UTC last-update time",
    )
    # Free-form broker metadata (Tinkoff execution broker, commission, etc.).
    broker_meta: dict[str, Any] = Field(default_factory=dict)


class OrderResult(BaseModel):
    """Return type of every BrokerAccount.place_order / cancel_order call.

    Outcomes:
      - ``ok=True`` AND ``status in {"submitted","new","partially_filled","filled"}`` ⇒ success.
      - ``ok=True`` AND ``status in {"cancelled","rejected"}`` ⇒ terminal failure; ``error_code`` is set.
      - ``ok=False`` ⇒ broker call itself failed (network, auth, rate limit);
        ``error_code`` is a short string (``"rate_limited"``,
        ``"auth_error"``, ``"network"``).

    ``frozen=True`` — audit log integrity. Once you have a receipt, you
    cannot mutate it.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    ok: bool
    status: OrderStatus
    order: AccountOrder | None = Field(
        default=None,
        description=(
            "The AccountOrder as returned (or constructed from) the broker. "
            "None when ok=False (the broker never confirmed the placement)."
        ),
    )
    error_code: str | None = Field(
        default=None,
        description=(
            "Short failure code. Filled only when ok=False, or ok=True "
            "with status rejected."
        ),
    )
    error_message: str | None = None
    received_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="UTC receipt time",
    )


__all__ = [
    "AccountOrder",
    "LimitOrder",
    "MarketOrder",
    "OrderResult",
    "OrderSide",
    "OrderStatus",
    "OrderType",
    "AnyOrder",
]
