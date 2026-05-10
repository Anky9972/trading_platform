"""
Paper broker — deterministic simulation for testing.
No real orders. No real money. No broker API calls.

Simulates:
  - Immediate fills at last set price
  - Optional slippage (default: 0.05%)
  - Optional partial fills (for testing partial fill handling)

Deterministic: same inputs → same outputs. Required for reproducible tests.
"""
import uuid
import logging
from typing import Optional, Dict
from core.models import Signal, Action
from broker.base import BrokerAdapter, BrokerError

logger = logging.getLogger(__name__)


class PaperBroker(BrokerAdapter):
    def __init__(self, slippage_pct: float = 0.0005,
                 partial_fill_qty: int = None):
        self._connected = False
        self._prices: Dict[str, float] = {}
        self._positions: Dict[str, dict] = {}
        self._orders: Dict[str, dict] = {}   # broker_order_id → order_data
        self._slippage = slippage_pct
        self._partial_fill_qty = partial_fill_qty   # If set, all fills are partial

    def connect(self) -> None:
        self._connected = True
        logger.info("Paper broker connected")

    def is_connected(self) -> bool:
        return self._connected

    def set_price(self, symbol: str, price: float):
        """Set simulated price for a symbol."""
        self._prices[symbol] = price

    def place_order(self, signal: Signal) -> str:
        """
        Simulate order placement.
        Returns broker_order_id immediately.
        """
        if not self._connected:
            raise BrokerError("Paper broker not connected")

        broker_id = f"PAPER-{uuid.uuid4().hex[:8].upper()}"
        price = self._prices.get(signal.symbol, 0)

        if price <= 0:
            raise BrokerError(f"No price set for {signal.symbol}")

        # Apply slippage
        if signal.action == Action.BUY:
            fill_price = price * (1 + self._slippage)
        else:
            fill_price = price * (1 - self._slippage)

        # Determine fill quantity
        if self._partial_fill_qty and self._partial_fill_qty < signal.quantity:
            fill_qty = self._partial_fill_qty
            status = "partial"
        else:
            fill_qty = signal.quantity
            status = "complete"

        self._orders[broker_id] = {
            "orderid": broker_id,
            "status": status,
            "symbol": signal.symbol,
            "action": signal.action.value,
            "quantity": signal.quantity,
            "filledshares": str(fill_qty),
            "averageprice": f"{fill_price:.2f}",
            "text": "",
        }

        # Update internal positions
        pos = self._positions.get(signal.symbol, {"quantity": 0, "avg_price": 0.0})
        if signal.action == Action.BUY:
            old_qty = pos["quantity"]
            new_qty = old_qty + fill_qty
            if new_qty > 0:
                pos["avg_price"] = ((old_qty * pos["avg_price"]) + (fill_qty * fill_price)) / new_qty
            pos["quantity"] = new_qty
        else:
            pos["quantity"] = max(0, pos["quantity"] - fill_qty)
            if pos["quantity"] == 0:
                pos["avg_price"] = 0.0
        self._positions[signal.symbol] = pos

        logger.info(f"Paper order {broker_id}: {signal.action.value} "
                    f"{fill_qty}x {signal.symbol} @ {fill_price:.2f} [{status}]")

        return broker_id

    def get_order_status(self, broker_order_id: str) -> dict:
        order = self._orders.get(broker_order_id)
        if not order:
            return {"status": "not_found", "filledshares": "0",
                    "averageprice": "0", "text": "Order not found"}
        return order

    def get_order_book(self) -> list:
        return list(self._orders.values())

    def get_holdings(self) -> list:
        holdings = []
        for symbol, pos in self._positions.items():
            if pos["quantity"] > 0:
                holdings.append({
                    "tradingsymbol": f"{symbol}-EQ",
                    "quantity": str(pos["quantity"]),
                    "averageprice": f"{pos['avg_price']:.2f}",
                })
        return holdings

    def get_ltp(self, symbol: str) -> float:
        return self._prices.get(symbol, 0.0)

    def get_ltp_batch(self, symbols: list) -> dict:
        return {s: self._prices.get(s, 0.0) for s in symbols}

    def get_funds(self) -> dict:
        from config.settings import SETTINGS
        return {"available_cash": float(SETTINGS.MAX_CAPITAL)}
