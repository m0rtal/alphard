"""Order models for Broker Connector.

Pure pydantic, no broker SDK dependency. Used by both RiskGate and TinkoffAccount.
"""
from __future__ import annotations

from decimal import Decimal
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field, field_validator


class OrderSide(str, Enum):
    """Buy or sell. Shorts require risk.allow_short=true."""

    BUY = "BUY"
    SELL = "SELL"


class OrderType(str, Enum):
    """Market = immediate fill at best price. Limit = only at specified price."""

    MARKET = "MARKET"
    LIMIT = "LIMIT"


class OrderStatus(str, Enum):
    """Lifecycle states."""

    PENDING = "PENDING"  # Pre-RiskGate
    SUBMITTED = "SUBMITTED"  # Sent to broker
    FILLED = "FILLED"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"


class MarketOrder(BaseModel):
    """Immediate execution at best available price.

    Honours frozen=True — once placed, the price is locked in.
    """

    model_config = {"frozen": True}

    ticker: str = Field(..., min_length=1, max_length=12)
    side: OrderSide
    quantity: Decimal = Field(..., gt=Decimal("0"))
    order_id: Optional[str] = None
    status: OrderStatus = OrderStatus.PENDING

    @field_validator("ticker")
    @classmethod
    def _upper(cls, v: str) -> str:
        return v.upper().strip()


class LimitOrder(BaseModel):
    """Execution only at specified price or better.

    Time-in-force: DAY (until market close). For GTC add tif='GTC' later.
    """

    model_config = {"frozen": True}

    ticker: str = Field(..., min_length=1, max_length=12)
    side: OrderSide
    quantity: Decimal = Field(..., gt=Decimal("0"))
    price: Decimal = Field(..., gt=Decimal("0"))
    order_id: Optional[str] = None
    status: OrderStatus = OrderStatus.PENDING

    @field_validator("ticker")
    @classmethod
    def _upper(cls, v: str) -> str:
        return v.upper().strip()
    @field_validator("price")
    @classmethod
    def _positive(cls, v: Decimal) -> Decimal:
        if v <= Decimal("0"):
            raise ValueError("price must be > 0")
        return v
