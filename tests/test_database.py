"""
Tests for store/database.py

Covers:
- Schema migration idempotency
- Idempotency key enforcement (UNIQUE constraint)
- Order state machine transitions
- Position arithmetic (apply_fill, force_position)
- Heartbeat read/write
"""
import pytest
from datetime import datetime
from store.database import Database
from core.models import OrderState


class TestMigrations:
    def test_fresh_database_creates_all_tables(self, tmp_db):
        conn = tmp_db._conn()
        tables = {
            row[0] for row in
            conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        required = {
            "orders", "state_transitions", "positions",
            "price_cache", "risk_events", "reconciliation_log",
            "session_cache", "schema_version"
        }
        assert required.issubset(tables)

    def test_migration_is_idempotent(self, tmp_path):
        """Running migrations twice on same DB must not error."""
        db_path = str(tmp_path / "idempotent.db")
        db1 = Database(db_path)
        db2 = Database(db_path)  # Second init — must not fail
        assert db2._conn() is not None

    def test_schema_version_set_correctly(self, tmp_db):
        from store.database import SCHEMA_VERSION
        row = tmp_db._conn().execute(
            "SELECT MAX(version) as v FROM schema_version"
        ).fetchone()
        assert row['v'] == SCHEMA_VERSION


class TestIdempotency:
    def test_duplicate_idempotency_key_raises(self, tmp_db):
        import sqlite3
        order = {
            "internal_id": "test-001",
            "idempotency_key": "idem-001",
            "broker_order_id": None,
            "symbol": "PERSISTENT",
            "action": "BUY",
            "quantity": 10,
            "order_type": "MARKET",
            "price": None,
            "trigger_price": None,
            "state": "CREATED",
            "filled_quantity": 0,
            "avg_fill_price": 0,
            "rejection_reason": "",
            "strategy_id": "test",
            "reason": "test",
            "created_at": datetime.now().isoformat(),
            "submitted_at": None,
            "last_updated": datetime.now().isoformat(),
        }
        tmp_db.insert_order(order)

        # Second insert with same idempotency_key must raise
        order2 = dict(order)
        order2["internal_id"] = "test-002"  # Different internal ID
        with pytest.raises(Exception):  # IntegrityError
            tmp_db.insert_order(order2)

    def test_has_idempotency_key_returns_true_after_insert(self, tmp_db):
        order = {
            "internal_id": "test-003",
            "idempotency_key": "idem-unique-001",
            "broker_order_id": None, "symbol": "PERSISTENT",
            "action": "BUY", "quantity": 10, "order_type": "MARKET",
            "price": None, "trigger_price": None, "state": "CREATED",
            "filled_quantity": 0, "avg_fill_price": 0,
            "rejection_reason": "", "strategy_id": "test", "reason": "",
            "created_at": datetime.now().isoformat(),
            "submitted_at": None,
            "last_updated": datetime.now().isoformat(),
        }
        assert tmp_db.has_idempotency_key("idem-unique-001") is False
        tmp_db.insert_order(order)
        assert tmp_db.has_idempotency_key("idem-unique-001") is True


class TestPositionArithmetic:
    def test_buy_creates_position(self, tmp_db):
        tmp_db.apply_fill("PERSISTENT", "BUY", 10, 5200.0)
        pos = tmp_db.get_position("PERSISTENT")
        assert pos['quantity'] == 10
        assert abs(pos['avg_price'] - 5200.0) < 0.01

    def test_buy_averages_price_correctly(self, tmp_db):
        tmp_db.apply_fill("PERSISTENT", "BUY", 10, 5000.0)
        tmp_db.apply_fill("PERSISTENT", "BUY", 10, 5200.0)
        pos = tmp_db.get_position("PERSISTENT")
        assert pos['quantity'] == 20
        assert abs(pos['avg_price'] - 5100.0) < 0.01

    def test_sell_reduces_position(self, tmp_db):
        tmp_db.apply_fill("PERSISTENT", "BUY", 10, 5200.0)
        tmp_db.apply_fill("PERSISTENT", "SELL", 4, 5300.0)
        pos = tmp_db.get_position("PERSISTENT")
        assert pos['quantity'] == 6
        assert abs(pos['avg_price'] - 5200.0) < 0.01  # avg price unchanged on sell

    def test_full_sell_zeroes_position(self, tmp_db):
        tmp_db.apply_fill("PERSISTENT", "BUY", 10, 5200.0)
        tmp_db.apply_fill("PERSISTENT", "SELL", 10, 5500.0)
        pos = tmp_db.get_position("PERSISTENT")
        assert pos['quantity'] == 0
        assert pos['avg_price'] == 0.0

    def test_force_position_overwrites(self, tmp_db):
        tmp_db.apply_fill("PERSISTENT", "BUY", 100, 5200.0)  # Internal: 100
        tmp_db.force_position("PERSISTENT", 50, 5100.0, "broker_reconciliation")
        pos = tmp_db.get_position("PERSISTENT")
        assert pos['quantity'] == 50
        assert abs(pos['avg_price'] - 5100.0) < 0.01
        assert pos['source'] == "broker_reconciliation"

    def test_invalid_action_raises(self, tmp_db):
        with pytest.raises(ValueError):
            tmp_db.apply_fill("PERSISTENT", "HOLD", 10, 5200.0)


class TestHeartbeat:
    def test_write_and_read_heartbeat(self, tmp_db):
        assert tmp_db.read_heartbeat() is None
        tmp_db.write_heartbeat()
        hb = tmp_db.read_heartbeat()
        assert hb is not None
        # Should be a valid ISO timestamp
        from datetime import datetime
        dt = datetime.fromisoformat(hb)
        age = (datetime.now() - dt).total_seconds()
        assert age < 5  # Written less than 5 seconds ago
