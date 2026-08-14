"""BrokerAccount ABC — interface for any broker implementation.

All concrete brokers (Tinkoff, BCS, Finam) implement this interface.
The interface is intentionally narrow — only methods that need broker
round-trip live here. Local computation belongs to other agents.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Optional

from src.broker.orders import LimitOrder, MarketOrder, OrderStatus


@dataclass
class Position:
    """Current holding in broker account."""

    ticker: str
    quantity: Decimal
    avg_price: Decimal


@dataclass
class PortfolioSnapshot:
    """Snapshot at request time."""

    account_id: str
    cash: Decimal
    positions: list[Position]
    timestamp: datetime


class BrokerAccount(ABC):
    """Abstract broker interface.

    Concrete: TinkoffAccount. Future: BCSAccount, FinamAccount.
    """

    @abstractmethod
    def get_portfolio(self) -> PortfolioSnapshot:
        """Current portfolio state. Read-only."""

    @abstractmethod
    def get_positions(self) -> list[Position]:
        """Open positions only."""

    @abstractmethod
    def place_order(self, order: MarketOrder | LimitOrder) -> OrderStatus:
        """Submit order. MUST call RiskGate before this method (enforced by
        concrete impl, not interface — caller responsibility).
        Returns SUBMITTED, FILLED, or REJECTED.

        Throws BrokerError on technical failure."""

    @abstractmethod
    def cancel_order(self, order_id: str) -> OrderStatus:
        """Cancel pending order. Returns CANCELLED or current status."""

    @abstractmethod
    def is_sandbox(self) -> bool:
        """True if this account is Tinkoff sandbox."""
