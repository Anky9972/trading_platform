"""
Order Manager — full order lifecycle management.

Signal → Risk Check → Place Order → Verify → Update Position

Key design decisions:
  - TIMEOUT produces UNKNOWN state, never FAILED
  - UNKNOWN orders are tracked separately and resolved by the Reconciler
  - Idempotency key is consumed even on rejection (prevents replay)
  - Position is only updated AFTER verified fill from broker
"""
import logging
from datetime import datetime
from core.models import Signal, Order, OrderState, Action
from store.database import Database
from store.event_log import EventLog, EventType
from broker.base import (
    BrokerAdapter, BrokerError, BrokerTimeoutError,
    BrokerRateLimitError, BrokerRejectionError,
)

logger = logging.getLogger(__name__)


class OrderManager:
    def __init__(self, broker: BrokerAdapter, risk, db: Database,
                 events: EventLog):
        self.broker = broker
        self.risk = risk
        self.db = db
        self.events = events

    def submit(self, signal: Signal) -> Order:
        """
        Full order flow: signal → risk → broker → verify.
        Returns the completed Order object.
        """
        order = Order.from_signal(signal)

        # 0. Check idempotency locally first
        if self.db.has_idempotency_key(signal.idempotency_key):
            order.state = OrderState.REJECTED
            order.rejection_reason = f"Duplicate idempotency key: {signal.idempotency_key}"
            return order

        # 1. Save order as CREATED (consumes idempotency key)
        self._save_order(order)

        # 2. Get estimated price for risk check
        try:
            est_price = self.broker.get_ltp(signal.symbol)
        except Exception:
            est_price = signal.price or 0

        # 3. Risk check
        risk_result = self.risk.check({
            "symbol": signal.symbol,
            "action": signal.action.value,
            "quantity": signal.quantity,
            "order_type": signal.order_type.value,
            "estimated_price": est_price,
            "idempotency_key": signal.idempotency_key,
            "strategy_id": signal.strategy_id,
        })

        if risk_result.get("verdict") != "APPROVED":
            order.state = OrderState.REJECTED
            order.rejection_reason = "; ".join(risk_result.get("reasons", []))
            self._update_state(order, OrderState.REJECTED,
                              rejection_reason=order.rejection_reason)
            self.events.emit(EventType.RISK_REJECTED, {
                "internal_id": order.internal_id,
                "symbol": signal.symbol,
                "reasons": risk_result.get("reasons", []),
            })
            logger.info(f"Order {order.internal_id[:8]} REJECTED by risk: "
                       f"{order.rejection_reason}")
            return order

        # 4. Place order with broker
        try:
            order.submitted_at = datetime.now().isoformat()
            self._update_state(order, OrderState.SUBMITTED)

            broker_order_id = self.broker.place_order(signal)
            order.broker_order_id = broker_order_id

            self.events.emit(EventType.ORDER_SUBMITTED, {
                "internal_id": order.internal_id,
                "broker_order_id": broker_order_id,
                "symbol": signal.symbol,
                "action": signal.action.value,
                "quantity": signal.quantity,
            })

            logger.info(f"Order {order.internal_id[:8]} submitted → "
                       f"broker_id={broker_order_id}")

        except BrokerTimeoutError as e:
            # CRITICAL: Timeout → UNKNOWN, not FAILED
            order.state = OrderState.UNKNOWN
            order.rejection_reason = f"TIMEOUT: {e} — order may be live at broker"
            self._update_state(order, OrderState.UNKNOWN,
                              rejection_reason=order.rejection_reason)
            self.events.emit(EventType.ORDER_UNKNOWN, {
                "internal_id": order.internal_id,
                "symbol": signal.symbol,
                "error": str(e),
            })
            logger.warning(f"Order {order.internal_id[:8]} TIMEOUT → UNKNOWN")
            return order

        except BrokerRejectionError as e:
            order.state = OrderState.FAILED
            order.rejection_reason = f"Broker rejected: {e}"
            self._update_state(order, OrderState.FAILED,
                              rejection_reason=order.rejection_reason)
            self.events.emit(EventType.ORDER_FAILED, {
                "internal_id": order.internal_id,
                "symbol": signal.symbol,
                "error": str(e),
            })
            logger.info(f"Order {order.internal_id[:8]} FAILED: {e}")
            return order

        except BrokerRateLimitError as e:
            order.state = OrderState.FAILED
            order.rejection_reason = f"Rate limited: {e}"
            self._update_state(order, OrderState.FAILED,
                              rejection_reason=order.rejection_reason)
            logger.warning(f"Order {order.internal_id[:8]} rate limited")
            return order

        except Exception as e:
            order.state = OrderState.UNKNOWN
            order.rejection_reason = f"UNKNOWN ERROR: {e}"
            self._update_state(order, OrderState.UNKNOWN,
                              rejection_reason=order.rejection_reason)
            logger.error(f"Order {order.internal_id[:8]} unexpected error: {e}")
            return order

        # 5. Verify and update
        self._verify_and_update(order)
        return order

    def _verify_and_update(self, order: Order):
        """Check broker for fill status and update position."""
        if not order.broker_order_id:
            return

        try:
            status = self.broker.get_order_status(order.broker_order_id)
        except Exception as e:
            logger.warning(f"Cannot verify order {order.internal_id[:8]}: {e}")
            return

        broker_status = status.get("status", "").lower()
        filled = int(status.get("filledshares", "0") or "0")
        avg_price = float(status.get("averageprice", "0") or "0")
        text = status.get("text", "")

        if broker_status == "complete":
            order.state = OrderState.FILLED
            order.filled_quantity = filled
            order.avg_fill_price = avg_price

            self._update_state(order, OrderState.FILLED,
                              filled_quantity=filled,
                              avg_fill_price=avg_price)

            # Update position
            self.db.apply_fill(order.symbol, order.action.value,
                              filled, avg_price)

            self.events.emit(EventType.ORDER_FILLED, {
                "internal_id": order.internal_id,
                "broker_order_id": order.broker_order_id,
                "symbol": order.symbol,
                "action": order.action.value,
                "filled": filled,
                "avg_price": avg_price,
            })
            logger.info(f"Order {order.internal_id[:8]} FILLED: "
                       f"{filled}x {order.symbol} @ ₹{avg_price}")

        elif broker_status in ("partial", "partially_filled"):
            order.state = OrderState.PARTIALLY_FILLED
            order.filled_quantity = filled
            order.avg_fill_price = avg_price

            self._update_state(order, OrderState.PARTIALLY_FILLED,
                              filled_quantity=filled,
                              avg_fill_price=avg_price)

            # Update position with partial fill
            if filled > 0:
                self.db.apply_fill(order.symbol, order.action.value,
                                  filled, avg_price)

            self.events.emit(EventType.ORDER_PARTIAL, {
                "internal_id": order.internal_id,
                "filled": filled,
                "quantity": order.quantity,
                "symbol": order.symbol,
            })
            logger.info(f"Order {order.internal_id[:8]} PARTIAL: "
                       f"{filled}/{order.quantity}")

        elif broker_status in ("rejected", "cancelled"):
            order.state = OrderState.FAILED
            order.rejection_reason = text or f"Broker status: {broker_status}"
            self._update_state(order, OrderState.FAILED,
                              rejection_reason=order.rejection_reason)
            self.events.emit(EventType.ORDER_FAILED, {
                "internal_id": order.internal_id,
                "symbol": order.symbol,
                "reason": text,
            })

        elif broker_status in ("open", "pending", "trigger pending"):
            order.state = OrderState.OPEN
            self._update_state(order, OrderState.OPEN)

    def sync_all_orders(self):
        """Sync all non-terminal orders with broker. Called on startup and each cycle."""
        live = self.db.get_live_orders()
        for order_dict in live:
            if not order_dict.get('broker_order_id'):
                continue

            order = Order(
                internal_id=order_dict['internal_id'],
                idempotency_key=order_dict['idempotency_key'],
                broker_order_id=order_dict['broker_order_id'],
                symbol=order_dict['symbol'],
                action=Action[order_dict['action']],
                quantity=order_dict['quantity'],
                order_type=order_dict['order_type'],
                price=order_dict.get('price'),
                trigger_price=order_dict.get('trigger_price'),
                state=OrderState[order_dict['state']],
                filled_quantity=order_dict.get('filled_quantity', 0),
                avg_fill_price=order_dict.get('avg_fill_price', 0),
                rejection_reason=order_dict.get('rejection_reason', ''),
                strategy_id=order_dict.get('strategy_id', ''),
            )
            self._verify_and_update(order)

    def _save_order(self, order: Order):
        """Save order to database."""
        self.db.insert_order({
            "internal_id": order.internal_id,
            "idempotency_key": order.idempotency_key,
            "broker_order_id": order.broker_order_id,
            "symbol": order.symbol,
            "action": order.action.value,
            "quantity": order.quantity,
            "order_type": order.order_type.value if hasattr(order.order_type, 'value') else order.order_type,
            "price": order.price,
            "trigger_price": order.trigger_price,
            "state": order.state.value,
            "filled_quantity": order.filled_quantity,
            "avg_fill_price": order.avg_fill_price,
            "rejection_reason": order.rejection_reason,
            "strategy_id": order.strategy_id,
            "reason": order.reason,
            "created_at": order.created_at,
            "submitted_at": order.submitted_at,
            "last_updated": order.last_updated,
        })

    def _update_state(self, order: Order, new_state: OrderState,
                      filled_quantity: int = None,
                      avg_fill_price: float = None,
                      rejection_reason: str = None):
        """Update order state in database."""
        self.db.update_order_state(
            order.internal_id,
            new_state.value,
            broker_order_id=order.broker_order_id,
            filled_quantity=filled_quantity,
            avg_fill_price=avg_fill_price,
            rejection_reason=rejection_reason,
        )
        order.state = new_state
