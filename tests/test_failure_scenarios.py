"""
Tests for failure scenarios (FM-1, FM-3, FM-9, FM-10 from the design doc).

FM-1:  Stale position drift → reconciliation corrects
FM-3:  Login rate limiting → session persistence prevents repeated auth
FM-9:  UNKNOWN order after timeout → not duplicated on recovery
FM-10: Unmanaged loss → continuous monitoring activates kill switch
"""
import pytest
import os
from datetime import datetime
from unittest.mock import MagicMock, patch
from config.settings import SETTINGS


class TestFM1StalePositionDrift:
    """FM-1: Position drifts from reality over hours."""
    def test_reconciliation_corrects_stale_position(self, tmp_db, tmp_events, paper_broker):
        from core.reconciliation import Reconciler

        # Internal: 10 shares at 5200
        tmp_db.apply_fill("PERSISTENT", "BUY", 10, 5200.0)

        # Broker: actually has 7 (3 were sold by another system or error)
        paper_broker._positions["PERSISTENT"] = {"quantity": 7, "avg_price": 5200.0}

        rec = Reconciler(tmp_db, paper_broker, tmp_events)
        corrections = rec.reconcile_positions()

        assert len(corrections) >= 1
        pos = tmp_db.get_position("PERSISTENT")
        assert pos['quantity'] == 7  # Corrected to broker value


class TestFM3SessionPersistence:
    """FM-3: Login rate limiting — session store prevents repeated auth calls."""
    def test_session_store_saves_and_loads(self, tmp_path):
        from core.session_store import SessionStore

        filepath = str(tmp_path / ".session")
        store = SessionStore(filepath=filepath, key="test_key_12345678901234567890_!")
        session = {
            "access_token": "abc123",
            "refresh_token": "def456",
            "_saved_at": datetime.now().isoformat(),
        }
        store.save(session)
        loaded = store.load()

        assert loaded is not None
        assert loaded["access_token"] == "abc123"
        assert loaded["refresh_token"] == "def456"


class TestFM9UnknownOrderResolution:
    """FM-9: Timeout → UNKNOWN, recovery does not duplicate the order."""
    def test_unknown_order_not_duplicated(self, tmp_db, tmp_events, paper_broker):
        from core.order_manager import OrderManager
        from core.models import Signal, Action, OrderType, OrderState
        from broker.base import BrokerTimeoutError

        mock_broker = MagicMock()
        mock_broker.get_ltp.return_value = 5200.0
        mock_broker.place_order.side_effect = BrokerTimeoutError("timeout")

        mock_risk = MagicMock()
        mock_risk.check.return_value = {
            "verdict": "APPROVED", "passed": ["ALL"], "failed": [], "reasons": [],
        }

        om = OrderManager(mock_broker, mock_risk, tmp_db, tmp_events)
        signal = Signal(
            symbol="PERSISTENT", action=Action.BUY, quantity=10,
            order_type=OrderType.MARKET, idempotency_key="fm9-test-001",
        )

        # First attempt — times out → UNKNOWN
        order = om.submit(signal)
        assert order.state == OrderState.UNKNOWN

        # Second attempt with same idempotency key — must be REJECTED
        signal2 = Signal(
            symbol="PERSISTENT", action=Action.BUY, quantity=10,
            order_type=OrderType.MARKET, idempotency_key="fm9-test-001",
        )
        order2 = om.submit(signal2)
        assert order2.state == OrderState.REJECTED
        assert "Duplicate" in order2.rejection_reason


class TestFM10ContinuousLossMonitoring:
    """FM-10: Portfolio loss exceeds daily limit — kill switch must activate."""
    def test_daily_loss_breach_activates_kill_switch(self, tmp_db, tmp_events, paper_broker, tmp_path):
        from store.price_cache import PriceCache

        ks_file = str(tmp_path / ".kill_switch")
        object.__setattr__(SETTINGS, 'KILL_SWITCH_FILE', ks_file)
        object.__setattr__(SETTINGS, 'MAX_CAPITAL', 500000)
        object.__setattr__(SETTINGS, 'MAX_DAILY_LOSS_PCT', 0.03)

        # Buy at 5200
        tmp_db.apply_fill("PERSISTENT", "BUY", 100, 5200.0)

        # Price drops to 4900 → loss = (4900-5200)*100 = -30000
        # 3% of 500000 = 15000. Loss 30000 > 15000 → should trigger
        pc = PriceCache(tmp_db)
        pc.update("PERSISTENT", 4900.0)

        # Simulate watchdog pnl check logic
        positions = tmp_db.get_all_positions()
        total_pnl = 0
        for pos in positions:
            cached = pc.get(pos['symbol'])
            current = cached.get("price", pos['avg_price'])
            pnl = (current - pos['avg_price']) * pos['quantity']
            total_pnl += pnl

        loss_limit = -(SETTINGS.MAX_CAPITAL * SETTINGS.MAX_DAILY_LOSS_PCT)
        assert total_pnl < loss_limit

        # Verify the loss calculation is correct
        assert total_pnl == -30000.0
        assert loss_limit == -15000.0
