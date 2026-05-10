"""
Abstract broker interface.
All broker interactions go through this interface.

If you add a new broker (Zerodha, IBKR, etc.):
  1. Create broker/zerodha.py
  2. Inherit from BrokerAdapter
  3. Implement all abstract methods
  4. Add to engine.py constructor

Exception hierarchy:
  BrokerError (base class)
    ├── BrokerAuthError — 401/403, session expired
    ├── BrokerTimeoutError — network timeout, no response
    ├── BrokerRateLimitError — 429, too many requests
    └── BrokerRejectionError — broker rejected the order (permanent)
"""
from abc import ABC, abstractmethod
from core.models import Signal
from typing import Optional


class BrokerError(Exception):
    """Base class for all broker errors."""
    pass

class BrokerAuthError(BrokerError):
    """Authentication/session error."""
    pass

class BrokerTimeoutError(BrokerError):
    """Network timeout — order state is UNKNOWN (not FAILED)."""
    pass

class BrokerRateLimitError(BrokerError):
    """Rate limited by broker API."""
    pass

class BrokerRejectionError(BrokerError):
    """Broker permanently rejected the order."""
    pass


class BrokerAdapter(ABC):
    @abstractmethod
    def connect(self) -> None:
        """Establish broker connection. Raises BrokerAuthError on failure."""
        ...

    @abstractmethod
    def is_connected(self) -> bool:
        ...

    @abstractmethod
    def place_order(self, signal: Signal) -> str:
        """
        Place order. Returns broker_order_id on success.
        Raises:
            BrokerTimeoutError — if timeout (state unknown)
            BrokerRejectionError — if permanently rejected
            BrokerRateLimitError — if rate limited
            BrokerError — any other error
        """
        ...

    @abstractmethod
    def get_order_status(self, broker_order_id: str) -> dict:
        """
        Get order status from broker.
        Returns dict with keys:
            status: str — 'complete', 'open', 'rejected', 'cancelled', 'pending'
            filledshares: str — number of filled shares
            averageprice: str — average fill price
            text: str — rejection/cancellation reason
        """
        ...

    @abstractmethod
    def get_order_book(self) -> list:
        """Get all orders for today."""
        ...

    @abstractmethod
    def get_holdings(self) -> list:
        """Get current portfolio holdings from broker."""
        ...

    @abstractmethod
    def get_ltp(self, symbol: str) -> float:
        """Get last traded price for a symbol."""
        ...

    @abstractmethod
    def get_funds(self) -> dict:
        """Get available margin/funds."""
        ...

    def get_positions(self) -> dict:
        """Optional: Get intraday positions."""
        return {}
