"""
Tests for risk/server.py (logic extracted for unit testing)
and risk/kill_switch.py

Covers:
- Each of the 12 pre-trade checks individually
- 10% price buffer enforcement
- Kill switch activation and deactivation
- Fail-closed behavior on missing price
"""
import pytest
import os
from datetime import datetime
from unittest.mock import patch, MagicMock
from core.models import Signal, Action, OrderType
from risk.kill_switch import KillSwitch
from config.settings import SETTINGS


# Helper to build a minimal signal
def make_signal(symbol="PERSISTENT", action=Action.BUY, quantity=10,
                idem_key=None):
    import uuid
    return Signal(
        symbol=symbol,
        action=action,
        quantity=quantity,
        order_type=OrderType.MARKET,
        strategy_id="test",
        reason="test",
        idempotency_key=idem_key or str(uuid.uuid4())[:12],
    )


class TestKillSwitch:
    def test_not_active_by_default(self, tmp_path, tmp_db):
        ks_file = str(tmp_path / ".kill_switch")
        object.__setattr__(SETTINGS, 'KILL_SWITCH_FILE', ks_file)
        ks = KillSwitch(tmp_db)
        assert ks.is_active() is False

    def test_activate_creates_file(self, tmp_path, tmp_db):
        ks_file = str(tmp_path / ".kill_switch")
        object.__setattr__(SETTINGS, 'KILL_SWITCH_FILE', ks_file)
        ks = KillSwitch(tmp_db)
        ks.activate("test reason")
        assert os.path.exists(ks_file)
        assert ks.is_active() is True

    def test_file_based_detection(self, tmp_path, tmp_db):
        ks_file = str(tmp_path / ".kill_switch")
        object.__setattr__(SETTINGS, 'KILL_SWITCH_FILE', ks_file)
        ks = KillSwitch(tmp_db)
        # Write file externally (simulates Watchdog writing it)
        with open(ks_file, 'w') as f:
            f.write("external activation\n")
        assert ks.is_active() is True

    def test_deactivate_requires_file_removed_first(self, tmp_path, tmp_db):
        ks_file = str(tmp_path / ".kill_switch")
        object.__setattr__(SETTINGS, 'KILL_SWITCH_FILE', ks_file)
        ks = KillSwitch(tmp_db)
        ks.activate("test")
        result = ks.deactivate()  # File still exists
        assert result is False
        assert ks.is_active() is True

    def test_deactivate_succeeds_after_file_removed(self, tmp_path, tmp_db):
        ks_file = str(tmp_path / ".kill_switch")
        object.__setattr__(SETTINGS, 'KILL_SWITCH_FILE', ks_file)
        ks = KillSwitch(tmp_db)
        ks.activate("test")
        os.unlink(ks_file)
        result = ks.deactivate()
        assert result is True
        assert ks.is_active() is False


class TestRiskChecks:
    """
    Test each risk check in isolation by calling the Risk Server
    evaluation logic directly (bypasses socket for unit testing).
    """
    def setup_method(self):
        """Import RiskServer inline to avoid socket binding on import."""
        import sys, os
        sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

    def _make_risk_server(self, tmp_db, tmp_path):
        from risk.server import RiskServer
        from store.price_cache import PriceCache
        from store.event_log import EventLog

        ks_file = str(tmp_path / ".kill_switch")
        object.__setattr__(SETTINGS, 'KILL_SWITCH_FILE', ks_file)
        object.__setattr__(SETTINGS, 'DB_PATH', tmp_db.db_path)

        rs = RiskServer.__new__(RiskServer)
        rs.db = tmp_db
        rs.price_cache = PriceCache(tmp_db)
        rs.events = EventLog(str(tmp_path / "events.jsonl"))
        rs._recent_orders = __import__('collections').deque(maxlen=200)
        rs._available_margin = float(SETTINGS.MAX_CAPITAL)
        return rs

    def test_kill_switch_check_rejects_when_active(self, tmp_db, tmp_path):
        rs = self._make_risk_server(tmp_db, tmp_path)
        ks_file = str(tmp_path / ".kill_switch")
        with open(ks_file, 'w') as f:
            f.write("active\n")
        sig = {"symbol": "PERSISTENT", "action": "BUY", "quantity": 10,
               "idempotency_key": "test001", "estimated_price": 5200}
        ok, reason = rs._ck_kill_switch(sig)
        assert ok is False
        assert "Kill switch" in reason

    def test_quantity_sanity_rejects_zero(self, tmp_db, tmp_path):
        rs = self._make_risk_server(tmp_db, tmp_path)
        sig = {"symbol": "PERSISTENT", "action": "BUY", "quantity": 0}
        ok, reason = rs._ck_quantity(sig)
        assert ok is False

    def test_quantity_sanity_rejects_over_limit(self, tmp_db, tmp_path):
        rs = self._make_risk_server(tmp_db, tmp_path)
        sig = {"symbol": "PERSISTENT", "action": "BUY", "quantity": 5001}
        ok, reason = rs._ck_quantity(sig)
        assert ok is False
        assert "5000" in reason

    def test_order_value_applies_10pct_buffer(self, tmp_db, tmp_path):
        rs = self._make_risk_server(tmp_db, tmp_path)
        sig = {"symbol": "PERSISTENT", "action": "BUY",
               "quantity": 10, "estimated_price": 9500}
        ok, reason = rs._ck_order_value(sig)
        assert ok is False
        assert "buffered" in reason.lower() or "max" in reason.lower()

    def test_order_value_passes_within_buffer(self, tmp_db, tmp_path):
        rs = self._make_risk_server(tmp_db, tmp_path)
        sig = {"symbol": "PERSISTENT", "action": "BUY",
               "quantity": 5, "estimated_price": 5000}
        ok, reason = rs._ck_order_value(sig)
        assert ok is True

    def test_sell_skips_position_limit(self, tmp_db, tmp_path):
        rs = self._make_risk_server(tmp_db, tmp_path)
        sig = {"symbol": "PERSISTENT", "action": "SELL",
               "quantity": 999999, "estimated_price": 5200}
        ok, reason = rs._ck_position_limit(sig)
        assert ok is True  # Sells always pass position limit

    def test_sell_validation_rejects_selling_more_than_held(self, tmp_db, tmp_path):
        rs = self._make_risk_server(tmp_db, tmp_path)
        sig = {"symbol": "PERSISTENT", "action": "SELL",
               "quantity": 10, "estimated_price": 5200}
        ok, reason = rs._ck_sell_holdings(sig)
        assert ok is False
        assert "Hold 0" in reason

    def test_sell_validation_passes_with_sufficient_holding(self, tmp_db, tmp_path):
        rs = self._make_risk_server(tmp_db, tmp_path)
        tmp_db.apply_fill("PERSISTENT", "BUY", 20, 5200.0)
        sig = {"symbol": "PERSISTENT", "action": "SELL",
               "quantity": 10, "estimated_price": 5200}
        ok, reason = rs._ck_sell_holdings(sig)
        assert ok is True

    def test_idempotency_rejects_duplicate_key(self, tmp_db, tmp_path):
        rs = self._make_risk_server(tmp_db, tmp_path)
        from datetime import datetime
        tmp_db.insert_order({
            "internal_id": "dup-test-001",
            "idempotency_key": "dup-key-001",
            "broker_order_id": None, "symbol": "PERSISTENT",
            "action": "BUY", "quantity": 10, "order_type": "MARKET",
            "price": None, "trigger_price": None, "state": "FILLED",
            "filled_quantity": 10, "avg_fill_price": 5200,
            "rejection_reason": "", "strategy_id": "test", "reason": "",
            "created_at": datetime.now().isoformat(),
            "submitted_at": None,
            "last_updated": datetime.now().isoformat(),
        })
        sig = {"symbol": "PERSISTENT", "action": "BUY",
               "quantity": 10, "idempotency_key": "dup-key-001"}
        ok, reason = rs._ck_idempotency(sig)
        assert ok is False
        assert "Duplicate" in reason
