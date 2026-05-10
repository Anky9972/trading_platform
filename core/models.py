"""
Data models for the trading platform.
Everything is a dataclass — no loose dicts for critical state.
Enums for finite states — no string typos.
"""
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional
import uuid


class Action(Enum):
    BUY = "BUY"
    SELL = "SELL"


class OrderType(Enum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"


class OrderState(Enum):
    CREATED = "CREATED"       # Internal — not yet submitted to broker
    SUBMITTED = "SUBMITTED"   # Sent to broker, awaiting response
    OPEN = "OPEN"             # Accepted by broker, in order book
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"         # Terminal state — success
    CANCELLED = "CANCELLED"   # Terminal state — broker cancelled
    REJECTED = "REJECTED"     # Terminal state — risk engine rejected
    FAILED = "FAILED"         # Terminal state — broker permanently rejected
    UNKNOWN = "UNKNOWN"       # NOT terminal — timeout, state unclear

    def is_terminal(self) -> bool:
        return self in (
            OrderState.FILLED,
            OrderState.CANCELLED,
            OrderState.REJECTED,
            OrderState.FAILED,
        )


@dataclass
class Signal:
    """Output of a strategy — input to order manager."""
    symbol: str
    action: Action
    quantity: int
    order_type: OrderType = OrderType.MARKET
    price: Optional[float] = None
    trigger_price: Optional[float] = None
    strategy_id: str = "manual"
    reason: str = ""
    idempotency_key: str = field(default_factory=lambda: str(uuid.uuid4())[:12])


@dataclass
class Order:
    """Full order state. Created by OrderManager, persisted in DB."""
    internal_id: str
    idempotency_key: str
    broker_order_id: Optional[str]
    symbol: str
    action: Action
    quantity: int
    order_type: OrderType
    price: Optional[float]
    trigger_price: Optional[float]
    state: OrderState
    filled_quantity: int = 0
    avg_fill_price: float = 0.0
    rejection_reason: str = ""
    strategy_id: str = "manual"
    reason: str = ""
    created_at: str = ""
    submitted_at: Optional[str] = None
    last_updated: str = ""

    @classmethod
    def from_signal(cls, signal: Signal) -> 'Order':
        from datetime import datetime
        now = datetime.now().isoformat()
        return cls(
            internal_id=str(uuid.uuid4()),
            idempotency_key=signal.idempotency_key,
            broker_order_id=None,
            symbol=signal.symbol,
            action=signal.action,
            quantity=signal.quantity,
            order_type=signal.order_type,
            price=signal.price,
            trigger_price=signal.trigger_price,
            state=OrderState.CREATED,
            strategy_id=signal.strategy_id,
            reason=signal.reason,
            created_at=now,
            last_updated=now,
        )
