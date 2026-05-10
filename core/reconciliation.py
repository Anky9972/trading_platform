"""
Reconciliation — broker is source of truth.

reconcile_positions():
  Compare internal positions with broker holdings.
  If different → adopt broker state (force_position).
  Log the discrepancy.

resolve_unknown_orders():
  Check broker's order book for UNKNOWN orders.
  If found complete → FILLED + update position.
  If no broker_order_id → FAILED (never reached broker).
"""
import logging
from datetime import datetime
from store.database import Database
from store.event_log import EventLog, EventType
from broker.base import BrokerAdapter

logger = logging.getLogger(__name__)


class Reconciler:
    def __init__(self, db: Database, broker: BrokerAdapter, events: EventLog):
        self.db = db
        self.broker = broker
        self.events = events

    def reconcile_positions(self) -> list:
        """
        Compare internal positions with broker holdings.
        Broker wins on any discrepancy.
        Returns list of correction descriptions.
        """
        corrections = []

        try:
            holdings = self.broker.get_holdings()
        except Exception as e:
            msg = f"Cannot reconcile: broker fetch failed: {e}"
            logger.error(msg)
            return [msg]

        # Build broker position map
        broker_positions = {}
        for h in holdings:
            symbol = h.get('tradingsymbol', '').replace('-EQ', '')
            qty = int(h.get('quantity', 0))
            avg = float(h.get('averageprice', 0))
            if qty > 0:
                broker_positions[symbol] = {"quantity": qty, "avg_price": avg}

        # Check all internal positions
        internal_positions = self.db.get_all_positions()
        checked = set()

        for pos in internal_positions:
            symbol = pos['symbol']
            checked.add(symbol)
            int_qty = pos['quantity']
            broker = broker_positions.get(symbol, {"quantity": 0, "avg_price": 0})
            brk_qty = broker['quantity']

            if int_qty != brk_qty:
                msg = (f"MISMATCH {symbol}: internal={int_qty} broker={brk_qty}")
                corrections.append(msg)
                logger.warning(msg)

                self.db.force_position(
                    symbol, brk_qty, broker['avg_price'],
                    "broker_reconciliation"
                )
                self.db.log_reconciliation(
                    "POSITION_MISMATCH", symbol,
                    int_qty, brk_qty,
                    f"Adopted broker: {brk_qty} @ {broker['avg_price']}"
                )
                self.events.emit(EventType.POSITION_CORRECTED, {
                    "symbol": symbol,
                    "internal_qty": int_qty,
                    "broker_qty": brk_qty,
                    "action": "adopted_broker",
                })

        # Check for positions in broker but not internal
        for symbol, brk in broker_positions.items():
            if symbol not in checked:
                msg = f"NEW from broker: {symbol} qty={brk['quantity']}"
                corrections.append(msg)
                logger.warning(msg)

                self.db.force_position(
                    symbol, brk['quantity'], brk['avg_price'],
                    "broker_reconciliation"
                )
                self.db.log_reconciliation(
                    "POSITION_NEW", symbol,
                    0, brk['quantity'],
                    f"Adopted from broker: {brk['quantity']} @ {brk['avg_price']}"
                )

        if not corrections:
            logger.info("Reconciliation: no discrepancies")

        return corrections

    def resolve_unknown_orders(self) -> list:
        """
        Resolve UNKNOWN orders by checking broker order book.
        Returns list of resolution descriptions.
        """
        unknowns = self.db.get_unknown_orders()
        if not unknowns:
            return []

        resolved = []
        try:
            order_book = self.broker.get_order_book()
        except Exception as e:
            logger.error(f"Cannot resolve unknowns: {e}")
            return [f"Cannot resolve: {e}"]

        broker_orders = {}
        for o in order_book:
            oid = o.get('orderid', '')
            if oid:
                broker_orders[oid] = o

        for unknown in unknowns:
            internal_id = unknown['internal_id']
            broker_id = unknown.get('broker_order_id')

            if not broker_id:
                # No broker_order_id → never reached broker → FAILED
                self.db.update_order_state(internal_id, "FAILED",
                    rejection_reason="Resolved: no broker_order_id — never submitted")
                resolved.append(f"{internal_id[:8]}: → FAILED (no broker ID)")
                logger.info(f"Unknown {internal_id[:8]} → FAILED (no broker ID)")
                continue

            broker_order = broker_orders.get(broker_id)
            if not broker_order:
                # Not in broker order book — might have been rejected
                self.db.update_order_state(internal_id, "FAILED",
                    rejection_reason="Resolved: not found in broker order book")
                resolved.append(f"{internal_id[:8]}: → FAILED (not in book)")
                continue

            status = broker_order.get('status', '').lower()
            filled = int(broker_order.get('filledshares', '0') or '0')
            avg_price = float(broker_order.get('averageprice', '0') or '0')

            if status == 'complete' and filled > 0:
                self.db.update_order_state(
                    internal_id, "FILLED",
                    broker_order_id=broker_id,
                    filled_quantity=filled,
                    avg_fill_price=avg_price,
                )
                self.db.apply_fill(
                    unknown['symbol'], unknown['action'],
                    filled, avg_price
                )
                resolved.append(
                    f"{internal_id[:8]}: → FILLED {filled}x @ ₹{avg_price}"
                )
                self.events.emit(EventType.UNKNOWN_RESOLVED, {
                    "internal_id": internal_id,
                    "resolution": "FILLED",
                    "filled": filled,
                    "avg_price": avg_price,
                })
                logger.info(f"Unknown {internal_id[:8]} → FILLED "
                           f"{filled}x @ ₹{avg_price}")

            elif status in ('rejected', 'cancelled'):
                text = broker_order.get('text', '')
                self.db.update_order_state(
                    internal_id, "FAILED",
                    rejection_reason=f"Resolved: broker {status}: {text}",
                )
                resolved.append(f"{internal_id[:8]}: → FAILED ({status})")

            else:
                # Still open/pending — leave as UNKNOWN for next check
                logger.info(f"Unknown {internal_id[:8]} still {status} at broker")

        return resolved
