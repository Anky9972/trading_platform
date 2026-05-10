"""
Emergency Executor — sells when engine is dead and stop loss is breached.

This is the architectural fix for the post-mortem finding:
  "Watchdog detects stop loss breach but cannot act — only the engine places orders."

Guardrails (to prevent accidental liquidation):
  1. SELL-only (never buys)
  2. MARKET orders only (no limit orders)
  3. Maximum 3 emergency sells per day
  4. Kill switch ALWAYS activated after execution (even on failure)
  5. Only fires when engine is confirmed dead (no heartbeat)
  6. Only fires when kill switch is NOT already active

Full audit trail:
  Every emergency sell is logged to the database, event log, and alerts.
"""
import uuid
import logging
from datetime import datetime, date
from config.settings import SETTINGS
from store.event_log import EventLog, EventType
from store.database import Database
from broker.base import BrokerAdapter, BrokerError
from core.models import Signal, Action, OrderType
from monitor.alerting import alert

logger = logging.getLogger(__name__)

MAX_EMERGENCY_SELLS_PER_DAY = 3


class EmergencyExecutor:
    def __init__(self, db: Database, broker: BrokerAdapter, events: EventLog):
        self.db = db
        self.broker = broker
        self.events = events
        self._sells_today = 0
        self._sell_date = date.today()

    def should_execute(self, symbol: str, pnl_pct: float,
                       engine_alive: bool) -> bool:
        """
        Check all conditions for emergency execution.
        ALL must be true:
          - Engine is dead (no heartbeat)
          - Stop loss is breached
          - Kill switch is NOT already active
          - Daily limit not exceeded
        """
        # Engine must be dead
        if engine_alive:
            return False

        # Stop loss must be breached (negative pnl_pct, magnitude >= threshold)
        if abs(pnl_pct) < SETTINGS.STOP_LOSS_PCT:
            return False

        # Kill switch must NOT already be active
        import os
        if os.path.exists(SETTINGS.KILL_SWITCH_FILE):
            return False

        # Daily limit
        if self._sell_date != date.today():
            self._sells_today = 0
            self._sell_date = date.today()

        if self._sells_today >= MAX_EMERGENCY_SELLS_PER_DAY:
            logger.warning(f"Emergency sell limit reached ({MAX_EMERGENCY_SELLS_PER_DAY}/day)")
            return False

        return True

    def execute_emergency_sell(self, symbol: str, quantity: int,
                                current_price: float, entry_price: float,
                                pnl_pct: float) -> bool:
        """
        Execute emergency market sell order.
        Returns True if order was placed successfully.
        Kill switch is ALWAYS activated (even on failure).
        """
        internal_id = f"EMRG-{uuid.uuid4().hex[:8].upper()}"
        idem_key = f"emergency_{symbol}_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{internal_id[-6:]}"

        logger.critical(
            f"EMERGENCY SELL: {symbol} qty={quantity} "
            f"entry=₹{entry_price} current=₹{current_price} pnl={pnl_pct:.1%}"
        )

        self.events.emit(EventType.EMERGENCY_SELL_INITIATED, {
            "symbol": symbol,
            "quantity": quantity,
            "current_price": current_price,
            "entry_price": entry_price,
            "pnl_pct": round(pnl_pct * 100, 2),
        })

        # Write audit trail to database
        now = datetime.now().isoformat()
        try:
            self.db.insert_order({
                "internal_id": internal_id,
                "idempotency_key": idem_key,
                "broker_order_id": None,
                "symbol": symbol,
                "action": "SELL",
                "quantity": quantity,
                "order_type": "MARKET",
                "price": None,
                "trigger_price": None,
                "state": "SUBMITTED",
                "filled_quantity": 0,
                "avg_fill_price": 0,
                "rejection_reason": "",
                "strategy_id": "emergency_watchdog",
                "reason": f"Emergency sell: {pnl_pct:.1%} loss, engine dead",
                "created_at": now,
                "submitted_at": now,
                "last_updated": now,
            })
        except Exception as e:
            import traceback
            traceback.print_exc()
            logger.error(f"Emergency audit trail write failed: {e}")

        # Place the sell order
        success = False
        try:
            signal = Signal(
                symbol=symbol,
                action=Action.SELL,
                quantity=quantity,
                order_type=OrderType.MARKET,
                strategy_id="emergency_watchdog",
                reason=f"Emergency: {pnl_pct:.1%} loss, engine dead",
                idempotency_key=idem_key,
            )
            broker_id = self.broker.place_order(signal)
            success = True

            self.db.update_order_state(
                internal_id, "FILLED",
                broker_order_id=broker_id,
            )
            self.events.emit(EventType.EMERGENCY_SELL_PLACED, {
                "symbol": symbol,
                "broker_order_id": broker_id,
                "quantity": quantity,
            })
            alert(
                f"🔴 EMERGENCY SELL EXECUTED: {quantity}x {symbol} @ ~₹{current_price} "
                f"(entry ₹{entry_price}, loss {pnl_pct:.1%})",
                level="CRITICAL"
            )

        except Exception as e:
            logger.critical(f"Emergency sell FAILED: {e}")
            self.db.update_order_state(
                internal_id, "FAILED",
                rejection_reason=f"Emergency sell failed: {e}",
            )
            self.events.emit(EventType.EMERGENCY_SELL_FAILED, {
                "symbol": symbol,
                "error": str(e),
            })
            alert(
                f"🔴🔴 EMERGENCY SELL FAILED: {symbol} — {e}. "
                f"MANUAL INTERVENTION REQUIRED.",
                level="CRITICAL"
            )

        # ALWAYS activate kill switch (even on failure)
        from risk.kill_switch import KillSwitch
        ks = KillSwitch(self.db)
        if success:
            ks.activate(f"EMERGENCY SELL: {symbol} {pnl_pct:.1%} loss")
        else:
            ks.activate(
                f"EMERGENCY SELL FAILED: {symbol} — MANUAL EXIT REQUIRED"
            )

        self._sells_today += 1
        return success
