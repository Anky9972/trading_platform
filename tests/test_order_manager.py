"""
Tests for core/order_manager.py

Covers:
- Happy path: submit → FILLED
- Timeout → UNKNOWN (not FAILED)
- Broker rejection → FAILED
- Duplicate idempotency key → REJECTED
- Partial fill handling
"""
import pytest
import os
from unittest.mock import MagicMock, patch
from core.models import Signal, Action, OrderType, OrderState
from core.order_manager import OrderManager
from store.event_log import EventLog
from broker.paper import PaperBroker
from broker.base import BrokerTimeoutError, BrokerRejectionError


class TestHappyPath:
    def test_buy_order_fills_and_updates_position(self, tmp_db, tmp_events, paper_broker):
        """Full happy path: BUY → FILLED → position updated."""
        mock_risk = MagicMock()
        mock_risk.check.return_value = {
            "verdict": "APPROVED", "passed": ["ALL"], "failed": [], "reasons": [],
        }
        om = OrderManager(paper_broker, mock_risk, tmp_db, tmp_events)

        signal = Signal(
            symbol="PERSISTENT", action=Action.BUY, quantity=10,
            order_type=OrderType.MARKET, strategy_id="test",
        )
        order = om.submit(signal)

        assert order.state == OrderState.FILLED
        assert order.filled_quantity == 10
        assert order.avg_fill_price > 0
        assert order.broker_order_id is not None

        pos = tmp_db.get_position("PERSISTENT")
        assert pos['quantity'] == 10
        assert pos['avg_price'] > 0


class TestTimeoutProducesUnknown:
    def test_timeout_sets_unknown_not_failed(self, tmp_db, tmp_events):
        """
        CRITICAL TEST: Broker timeout must result in UNKNOWN state.
        If this test fails, you risk duplicate orders.
        """
        mock_broker = MagicMock(spec=PaperBroker)
        mock_broker.get_ltp.return_value = 5200.0
        mock_broker.place_order.side_effect = BrokerTimeoutError("Read timed out")

        mock_risk = MagicMock()
        mock_risk.check.return_value = {
            "verdict": "APPROVED", "passed": ["ALL"], "failed": [], "reasons": [],
        }

        om = OrderManager(mock_broker, mock_risk, tmp_db, tmp_events)
        signal = Signal(
            symbol="PERSISTENT", action=Action.BUY, quantity=10,
            order_type=OrderType.MARKET,
        )
        order = om.submit(signal)

        # Must be UNKNOWN, not FAILED or CREATED
        assert order.state == OrderState.UNKNOWN
        assert "TIMEOUT" in order.rejection_reason.upper()

        # Check DB state matches
        unknowns = tmp_db.get_unknown_orders()
        assert len(unknowns) >= 1
        assert any(u['internal_id'] == order.internal_id for u in unknowns)


class TestBrokerRejection:
    def test_broker_rejection_sets_failed(self, tmp_db, tmp_events):
        mock_broker = MagicMock(spec=PaperBroker)
        mock_broker.get_ltp.return_value = 5200.0
        mock_broker.place_order.side_effect = BrokerRejectionError("Insufficient margin")

        mock_risk = MagicMock()
        mock_risk.check.return_value = {
            "verdict": "APPROVED", "passed": ["ALL"], "failed": [], "reasons": [],
        }

        om = OrderManager(mock_broker, mock_risk, tmp_db, tmp_events)
        signal = Signal(
            symbol="PERSISTENT", action=Action.BUY, quantity=10,
            order_type=OrderType.MARKET,
        )
        order = om.submit(signal)

        assert order.state == OrderState.FAILED
        assert "rejected" in order.rejection_reason.lower()


class TestIdempotency:
    def test_duplicate_idempotency_key_rejected(self, tmp_db, tmp_events, paper_broker):
        mock_risk = MagicMock()
        mock_risk.check.return_value = {
            "verdict": "APPROVED", "passed": ["ALL"], "failed": [], "reasons": [],
        }
        om = OrderManager(paper_broker, mock_risk, tmp_db, tmp_events)

        signal = Signal(
            symbol="PERSISTENT", action=Action.BUY, quantity=10,
            order_type=OrderType.MARKET, idempotency_key="fixed-key-001",
        )
        order1 = om.submit(signal)
        assert order1.state == OrderState.FILLED

        # Second signal with same key — must be rejected
        signal2 = Signal(
            symbol="PERSISTENT", action=Action.BUY, quantity=10,
            order_type=OrderType.MARKET, idempotency_key="fixed-key-001",
        )
        order2 = om.submit(signal2)
        assert order2.state == OrderState.REJECTED
        assert "Duplicate" in order2.rejection_reason


class TestPartialFill:
    def test_partial_fill_detected(self, tmp_db, tmp_events):
        broker = PaperBroker(partial_fill_qty=5)
        broker.connect()
        broker.set_price("PERSISTENT", 5200.0)

        mock_risk = MagicMock()
        mock_risk.check.return_value = {
            "verdict": "APPROVED", "passed": ["ALL"], "failed": [], "reasons": [],
        }

        om = OrderManager(broker, mock_risk, tmp_db, tmp_events)
        signal = Signal(
            symbol="PERSISTENT", action=Action.BUY, quantity=10,
            order_type=OrderType.MARKET,
        )
        order = om.submit(signal)

        assert order.state == OrderState.PARTIALLY_FILLED
        assert order.filled_quantity == 5

        pos = tmp_db.get_position("PERSISTENT")
        assert pos['quantity'] == 5
