"""
ExchangeAdapter protocol.

Every exchange integration (Delta, and later CoinSwitch/Shark/Deribit once
their docs are obtained -- see docs/architecture.md Section B) implements this
exact interface. Nothing outside exchange_adapters/ is allowed to call an
exchange-specific method that isn't part of this contract -- that's what keeps
the matching engine, pricing engine, and scanner exchange-agnostic.

Per architecture.md Section A.3, this mirrors (but does not depend on)
Hummingbot's connector abstraction pattern (Apache 2.0) for naming/shape
consistency, in case we ever want to contribute a connector upstream or pull
one from there. See Section K.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Protocol, runtime_checkable

from normalization.schemas import (
    ContractSpec,
    FeeSchedule,
    MarketSnapshot,
    OptionContract,
)


class OrderSide:
    BUY = "buy"
    SELL = "sell"


class OrderRequest:
    """Placeholder request shape -- filled out properly in Phase 8 (execution engine)."""

    def __init__(
        self,
        instrument_id: str,
        side: str,
        quantity: Decimal,
        limit_price: Decimal,
        order_type: str = "limit",
    ) -> None:
        self.instrument_id = instrument_id
        self.side = side
        self.quantity = quantity
        self.limit_price = limit_price
        self.order_type = order_type


class OrderResult:
    def __init__(self, order_id: str, status: str, filled_quantity: Decimal, avg_fill_price: Decimal | None) -> None:
        self.order_id = order_id
        self.status = status
        self.filled_quantity = filled_quantity
        self.avg_fill_price = avg_fill_price


class OrderStatus:
    def __init__(self, order_id: str, status: str, filled_quantity: Decimal, remaining_quantity: Decimal) -> None:
        self.order_id = order_id
        self.status = status
        self.filled_quantity = filled_quantity
        self.remaining_quantity = remaining_quantity


class Position:
    def __init__(self, instrument_id: str, quantity: Decimal, entry_price: Decimal, unrealized_pnl: Decimal) -> None:
        self.instrument_id = instrument_id
        self.quantity = quantity
        self.entry_price = entry_price
        self.unrealized_pnl = unrealized_pnl


class Balance:
    def __init__(self, currency: str, available: Decimal, total: Decimal, margin_used: Decimal) -> None:
        self.currency = currency
        self.available = available
        self.total = total
        self.margin_used = margin_used


class OrderBookSnapshot:
    def __init__(
        self,
        instrument_id: str,
        timestamp: datetime,
        bids: list[tuple[Decimal, Decimal]],
        asks: list[tuple[Decimal, Decimal]],
    ) -> None:
        self.instrument_id = instrument_id
        self.timestamp = timestamp
        self.bids = bids  # [(price, size), ...] best first
        self.asks = asks


class TickerSnapshot:
    def __init__(self, instrument_id: str, timestamp: datetime, snapshot: MarketSnapshot) -> None:
        self.instrument_id = instrument_id
        self.timestamp = timestamp
        self.snapshot = snapshot


@runtime_checkable
class ExchangeAdapter(Protocol):
    """
    Every method here must be implemented by every adapter. Trading methods
    (place_order, cancel_order, modify_order) exist on the interface from day
    one so the shape is consistent, but they must not be *called* by anything
    in Phase 2-7 -- config.settings.LIVE_TRADING gates that, and it defaults
    to False with no environment override (see config/settings.py).
    """

    def get_instruments(self) -> list[OptionContract]: ...

    def get_option_chain(
        self, underlying: str, expiry: datetime | None = None
    ) -> list[OptionContract]: ...

    def get_orderbook(self, instrument_id: str, depth: int = 5) -> OrderBookSnapshot: ...

    def get_ticker(self, instrument_id: str) -> TickerSnapshot: ...

    def get_positions(self) -> list[Position]: ...

    def get_balance(self) -> Balance: ...

    def place_order(self, order: OrderRequest) -> OrderResult: ...

    def cancel_order(self, order_id: str) -> bool: ...

    def modify_order(self, order_id: str, changes: dict) -> OrderResult: ...

    def get_order_status(self, order_id: str) -> OrderStatus: ...

    def get_fees(self) -> FeeSchedule: ...

    def get_contract_specification(self, instrument_id: str) -> ContractSpec: ...
