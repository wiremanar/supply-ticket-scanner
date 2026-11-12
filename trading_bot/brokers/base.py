"""Broker abstraction and shared trading types."""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum


class Side(str, Enum):
    BUY = "buy"
    SELL = "sell"


@dataclass
class Position:
    symbol: str
    quantity: float
    avg_price: float

    def market_value(self, price: float) -> float:
        return self.quantity * price

    def unrealized_pnl(self, price: float) -> float:
        return (price - self.avg_price) * self.quantity


@dataclass
class Account:
    cash: float
    # Total equity = cash + market value of all positions.
    equity: float


@dataclass
class Order:
    symbol: str
    side: Side
    quantity: float
    # Informational: who/what requested the order and why.
    reason: str = ""


@dataclass
class Fill:
    symbol: str
    side: Side
    quantity: float
    price: float
    notional: float
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    broker_order_id: str | None = None


class BrokerError(RuntimeError):
    """Raised when a broker operation fails."""


class Broker(abc.ABC):
    """Minimal interface the engine relies on.

    Implementations must be safe to call repeatedly; the engine treats
    ``submit_market_order`` as the only state-changing operation.
    """

    name: str = "broker"

    @abc.abstractmethod
    def get_account(self) -> Account:
        ...

    @abc.abstractmethod
    def get_positions(self) -> dict[str, Position]:
        ...

    @abc.abstractmethod
    def get_price(self, symbol: str) -> float:
        ...

    @abc.abstractmethod
    def submit_market_order(self, order: Order) -> Fill:
        ...

    def is_live(self) -> bool:
        return False
