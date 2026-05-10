"""
Tests for core/reconciliation.py

Covers:
- Position correction (broker > internal)
- Position correction (broker < internal)
- New positions from broker not in internal state
- UNKNOWN order resolution (FILLED at broker)
- UNKNOWN order resolution (not in broker book → FAILED)
"""
import pytest
from datetime import datetime
from core.reconciliation import Reconciler
from store.database import Database
from store.event_log import EventLog
from broker.paper import PaperBroker


class TestPositionReconciliation:
    def test_adopts_broker_position_on_mismatch(self, tmp_db, tmp_events, paper_broker):
        """If internal says 10, broker says 15, adopt 15."""
        # Internal state: 10 shares
        tmp_db.apply_fill("PERSISTENT", "BUY", 10, 5200.0)

        # Broker state: 15 shares (via direct position injection)
        paper_broker._positions["PERSISTENT"] = {"quantity": 15, "avg_price": 5100.0}

        rec = Reconciler(tmp_db, paper_broker, tmp_events)
        corrections = rec.reconcile_positions()

        assert len(corrections) == 1
        assert "MISMATCH" in corrections[0]

        pos = tmp_db.get_position("PERSISTENT")
        assert pos['quantity'] == 15  # Adopted broker value
        assert pos['source'] == "broker_reconciliation"

    def test_no_corrections_when_in_sync(self, tmp_db, tmp_events, paper_broker):
        """When internal and broker agree, no corrections."""
        from core.models import Signal, Action, OrderType
        signal = Signal(symbol="PERSISTENT", action=Action.BUY, quantity=10,
                        order_type=OrderType.MARKET)
        paper_broker.place_order(signal)
        tmp_db.apply_fill("PERSISTENT", "BUY", 10, 5200.0)

        rec = Reconciler(tmp_db, paper_broker, tmp_events)
        corrections = rec.reconcile_positions()
        assert len(corrections) == 0


class TestUnknownOrderResolution:
    def test_unknown_with_no_broker_id_becomes_failed(self, tmp_db, tmp_events, paper_broker):
        """UNKNOWN order with no broker_order_id → FAILED (never reached broker)."""
        now = datetime.now().isoformat()
        tmp_db.insert_order({
            "internal_id": "unknown-001",
            "idempotency_key": "unk-idem-001",
            "broker_order_id": None,
            "symbol": "PERSISTENT", "action": "BUY", "quantity": 10,
            "order_type": "MARKET", "price": None, "trigger_price": None,
            "state": "UNKNOWN", "filled_quantity": 0, "avg_fill_price": 0,
            "rejection_reason": "Timeout", "strategy_id": "test",
            "reason": "", "created_at": now, "submitted_at": None,
            "last_updated": now,
        })

        rec = Reconciler(tmp_db, paper_broker, tmp_events)
        resolved = rec.resolve_unknown_orders()

        assert len(resolved) == 1
        assert "FAILED" in resolved[0]
        # Verify DB state
        unknowns = tmp_db.get_unknown_orders()
        assert len(unknowns) == 0

    def test_unknown_found_filled_at_broker(self, tmp_db, tmp_events, paper_broker):
        """UNKNOWN order found as complete at broker → FILLED + position updated."""
        now = datetime.now().isoformat()
        # First, actually place an order to create a real broker order
        from core.models import Signal, Action, OrderType
        signal = Signal(symbol="PERSISTENT", action=Action.BUY, quantity=10,
                        order_type=OrderType.MARKET)
        broker_id = paper_broker.place_order(signal)

        # Insert order as UNKNOWN in our DB
        tmp_db.insert_order({
            "internal_id": "unknown-002",
            "idempotency_key": "unk-idem-002",
            "broker_order_id": broker_id,
            "symbol": "PERSISTENT", "action": "BUY", "quantity": 10,
            "order_type": "MARKET", "price": None, "trigger_price": None,
            "state": "UNKNOWN", "filled_quantity": 0, "avg_fill_price": 0,
            "rejection_reason": "Timeout", "strategy_id": "test",
            "reason": "", "created_at": now, "submitted_at": None,
            "last_updated": now,
        })

        rec = Reconciler(tmp_db, paper_broker, tmp_events)
        resolved = rec.resolve_unknown_orders()

        assert len(resolved) == 1
        assert "FILLED" in resolved[0]

        # Position should be updated
        pos = tmp_db.get_position("PERSISTENT")
        assert pos['quantity'] == 10
