"""
Database layer.
SQLite with WAL mode and synchronous=FULL (no data loss on power failure).
Schema versioning with numbered migrations.
All state lives here. Zero JSON files for critical state.
"""
import sqlite3
import threading
import os
from datetime import datetime
from contextlib import contextmanager

SCHEMA_VERSION = 4

MIGRATIONS = {
    1: """
        CREATE TABLE IF NOT EXISTS schema_version (version INTEGER PRIMARY KEY);
        INSERT OR IGNORE INTO schema_version VALUES (1);

        CREATE TABLE IF NOT EXISTS orders (
            internal_id TEXT PRIMARY KEY,
            idempotency_key TEXT UNIQUE NOT NULL,
            broker_order_id TEXT,
            symbol TEXT NOT NULL,
            action TEXT NOT NULL CHECK(action IN ('BUY','SELL')),
            quantity INTEGER NOT NULL CHECK(quantity > 0),
            order_type TEXT NOT NULL,
            price REAL,
            trigger_price REAL,
            state TEXT NOT NULL DEFAULT 'CREATED',
            filled_quantity INTEGER DEFAULT 0 CHECK(filled_quantity >= 0),
            avg_fill_price REAL DEFAULT 0,
            rejection_reason TEXT DEFAULT '',
            strategy_id TEXT DEFAULT 'manual',
            reason TEXT DEFAULT '',
            created_at TEXT NOT NULL,
            submitted_at TEXT,
            last_updated TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS state_transitions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_internal_id TEXT NOT NULL REFERENCES orders(internal_id),
            from_state TEXT,
            to_state TEXT NOT NULL,
            reason TEXT DEFAULT '',
            ts TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS positions (
            symbol TEXT PRIMARY KEY,
            quantity INTEGER NOT NULL DEFAULT 0,
            avg_price REAL NOT NULL DEFAULT 0 CHECK(avg_price >= 0),
            last_fill_at TEXT,
            source TEXT DEFAULT 'internal'
        );

        CREATE TABLE IF NOT EXISTS risk_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            event_type TEXT NOT NULL,
            details TEXT NOT NULL,
            idempotency_key TEXT DEFAULT '',
            action_taken TEXT DEFAULT ''
        );

        CREATE INDEX IF NOT EXISTS idx_orders_state ON orders(state);
        CREATE INDEX IF NOT EXISTS idx_orders_idem ON orders(idempotency_key);
        CREATE INDEX IF NOT EXISTS idx_orders_created ON orders(created_at);
    """,
    2: """
        CREATE TABLE IF NOT EXISTS reconciliation_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            rec_type TEXT NOT NULL,
            symbol TEXT,
            internal_qty INTEGER,
            broker_qty INTEGER,
            resolution TEXT NOT NULL,
            details TEXT DEFAULT ''
        );
        UPDATE schema_version SET version = 2;
    """,
    3: """
        CREATE TABLE IF NOT EXISTS session_cache (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        UPDATE schema_version SET version = 3;
    """,
    4: """
        CREATE TABLE IF NOT EXISTS price_cache (
            symbol TEXT PRIMARY KEY,
            price REAL NOT NULL CHECK(price > 0),
            updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_price_cache_updated ON price_cache(updated_at);
        UPDATE schema_version SET version = 4;
    """,
}


class Database:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self._local = threading.local()
        self._migrate()

    def _conn(self) -> sqlite3.Connection:
        if not hasattr(self._local, 'conn') or self._local.conn is None:
            conn = sqlite3.connect(self.db_path, timeout=30)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=FULL")     # No data loss on power failure
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA busy_timeout=10000")
            conn.row_factory = sqlite3.Row
            self._local.conn = conn
        return self._local.conn

    @contextmanager
    def tx(self):
        """Transaction context. Rollback on any exception."""
        conn = self._conn()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    def _migrate(self):
        """Run pending migrations in order. Idempotent."""
        conn = self._conn()
        tables = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_version'"
        ).fetchone()

        current = 0
        if tables:
            row = conn.execute("SELECT MAX(version) as v FROM schema_version").fetchone()
            current = row['v'] if row and row['v'] else 0

        for version in range(current + 1, SCHEMA_VERSION + 1):
            if version in MIGRATIONS:
                conn.executescript(MIGRATIONS[version])
                conn.commit()

        if current == 0:
            conn.execute("INSERT OR REPLACE INTO schema_version VALUES (?)", (SCHEMA_VERSION,))
            conn.commit()

    # === ORDER OPERATIONS ===

    def has_idempotency_key(self, key: str) -> bool:
        row = self._conn().execute(
            "SELECT 1 FROM orders WHERE idempotency_key = ?", (key,)
        ).fetchone()
        return row is not None

    def insert_order(self, data: dict):
        """Insert new order. Raises IntegrityError on duplicate idempotency key."""
        with self.tx() as conn:
            conn.execute("""
                INSERT INTO orders
                (internal_id, idempotency_key, broker_order_id, symbol, action,
                 quantity, order_type, price, trigger_price, state, filled_quantity,
                 avg_fill_price, rejection_reason, strategy_id, reason,
                 created_at, submitted_at, last_updated)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (
                data['internal_id'], data['idempotency_key'],
                data.get('broker_order_id'), data['symbol'], data['action'],
                data['quantity'], data['order_type'],
                data.get('price'), data.get('trigger_price'),
                data['state'], data.get('filled_quantity', 0),
                data.get('avg_fill_price', 0), data.get('rejection_reason', ''),
                data.get('strategy_id', 'manual'), data.get('reason', ''),
                data['created_at'], data.get('submitted_at'), data['last_updated'],
            ))

    def update_order_state(self, internal_id: str, new_state: str,
                           broker_order_id: str = None,
                           filled_quantity: int = None,
                           avg_fill_price: float = None,
                           rejection_reason: str = None):
        """Atomic order state update with automatic transition log."""
        now = datetime.now().isoformat()
        with self.tx() as conn:
            old = conn.execute(
                "SELECT state FROM orders WHERE internal_id = ?", (internal_id,)
            ).fetchone()
            old_state = old['state'] if old else None

            updates = ["state = ?", "last_updated = ?"]
            params = [new_state, now]

            if broker_order_id is not None:
                updates.append("broker_order_id = ?")
                params.append(broker_order_id)
            if filled_quantity is not None:
                updates.append("filled_quantity = ?")
                params.append(filled_quantity)
            if avg_fill_price is not None:
                updates.append("avg_fill_price = ?")
                params.append(avg_fill_price)
            if rejection_reason is not None:
                updates.append("rejection_reason = ?")
                params.append(rejection_reason)

            params.append(internal_id)
            conn.execute(
                f"UPDATE orders SET {', '.join(updates)} WHERE internal_id = ?",
                params
            )

            conn.execute("""
                INSERT INTO state_transitions
                (order_internal_id, from_state, to_state, reason, ts)
                VALUES (?, ?, ?, ?, ?)
            """, (internal_id, old_state, new_state,
                  rejection_reason or '', now))

    def get_live_orders(self) -> list:
        rows = self._conn().execute("""
            SELECT * FROM orders
            WHERE state IN ('CREATED','SUBMITTED','OPEN','PARTIAL','UNKNOWN')
        """).fetchall()
        return [dict(r) for r in rows]

    def get_unknown_orders(self) -> list:
        rows = self._conn().execute(
            "SELECT * FROM orders WHERE state = 'UNKNOWN'"
        ).fetchall()
        return [dict(r) for r in rows]

    def get_orders_today(self) -> list:
        today = datetime.now().strftime("%Y-%m-%d")
        rows = self._conn().execute(
            "SELECT * FROM orders WHERE created_at >= ?", (today,)
        ).fetchall()
        return [dict(r) for r in rows]

    def count_orders_today(self) -> int:
        today = datetime.now().strftime("%Y-%m-%d")
        row = self._conn().execute("""
            SELECT COUNT(*) as c FROM orders
            WHERE created_at >= ? AND state NOT IN ('FAILED','REJECTED')
        """, (today,)).fetchone()
        return row['c']

    # === POSITION OPERATIONS ===

    def get_position(self, symbol: str) -> dict:
        row = self._conn().execute(
            "SELECT * FROM positions WHERE symbol = ?", (symbol,)
        ).fetchone()
        if row:
            return dict(row)
        return {"symbol": symbol, "quantity": 0, "avg_price": 0.0, "source": "none"}

    def get_all_positions(self) -> list:
        rows = self._conn().execute(
            "SELECT * FROM positions WHERE quantity != 0"
        ).fetchall()
        return [dict(r) for r in rows]

    def apply_fill(self, symbol: str, action: str, fill_qty: int, fill_price: float):
        """
        Atomic position update after a fill.
        This is the ONLY way positions change except for force_position.
        """
        with self.tx() as conn:
            row = conn.execute(
                "SELECT quantity, avg_price FROM positions WHERE symbol = ?",
                (symbol,)
            ).fetchone()

            old_qty = row['quantity'] if row else 0
            old_avg = row['avg_price'] if row else 0.0

            if action == "BUY":
                new_qty = old_qty + fill_qty
                new_avg = ((old_qty * old_avg) + (fill_qty * fill_price)) / new_qty if new_qty > 0 else 0.0
            elif action == "SELL":
                new_qty = old_qty - fill_qty
                new_avg = old_avg if new_qty > 0 else 0.0
            else:
                raise ValueError(f"Invalid action: {action}")

            now = datetime.now().isoformat()
            conn.execute("""
                INSERT INTO positions (symbol, quantity, avg_price, last_fill_at, source)
                VALUES (?, ?, ?, ?, 'fill')
                ON CONFLICT(symbol) DO UPDATE SET
                    quantity = ?, avg_price = ?, last_fill_at = ?, source = 'fill'
            """, (symbol, new_qty, new_avg, now,
                  new_qty, new_avg, now))

    def force_position(self, symbol: str, quantity: int, avg_price: float, source: str):
        """
        Force-set position from broker reconciliation.
        Overwrites internal state with broker truth.
        Used ONLY by the reconciler.
        """
        now = datetime.now().isoformat()
        with self.tx() as conn:
            conn.execute("""
                INSERT INTO positions (symbol, quantity, avg_price, last_fill_at, source)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(symbol) DO UPDATE SET
                    quantity = ?, avg_price = ?, last_fill_at = ?, source = ?
            """, (symbol, quantity, avg_price, now, source,
                  quantity, avg_price, now, source))

    # === RISK EVENTS ===

    def log_risk_event(self, event_type: str, details: str,
                       idem_key: str = "", action: str = ""):
        with self.tx() as conn:
            conn.execute("""
                INSERT INTO risk_events (ts, event_type, details, idempotency_key, action_taken)
                VALUES (?, ?, ?, ?, ?)
            """, (datetime.now().isoformat(), event_type, details, idem_key, action))

    def log_reconciliation(self, rec_type: str, symbol: str,
                           internal_qty: int, broker_qty: int,
                           resolution: str, details: str = ""):
        with self.tx() as conn:
            conn.execute("""
                INSERT INTO reconciliation_log
                (ts, rec_type, symbol, internal_qty, broker_qty, resolution, details)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (datetime.now().isoformat(), rec_type, symbol,
                  internal_qty, broker_qty, resolution, details))

    # === HEARTBEAT ===

    def write_heartbeat(self):
        now = datetime.now().isoformat()
        with self.tx() as conn:
            conn.execute("""
                INSERT INTO session_cache (key, value, updated_at)
                VALUES ('heartbeat', ?, ?)
                ON CONFLICT(key) DO UPDATE SET value = ?, updated_at = ?
            """, (now, now, now, now))

    def read_heartbeat(self) -> str:
        row = self._conn().execute(
            "SELECT value FROM session_cache WHERE key = 'heartbeat'"
        ).fetchone()
        return row['value'] if row else None

    # === METRICS / PEAK EQUITY ===

    def get_peak_equity(self) -> float:
        row = self._conn().execute(
            "SELECT value FROM session_cache WHERE key = 'peak_equity'"
        ).fetchone()
        return float(row['value']) if row else 0.0

    def update_peak_equity(self, value: float):
        ts = datetime.now().isoformat()
        with self.tx() as conn:
            conn.execute("""
                INSERT INTO session_cache (key, value, updated_at)
                VALUES ('peak_equity', ?, ?)
                ON CONFLICT(key) DO UPDATE SET value = ?, updated_at = ?
            """, (str(value), ts, str(value), ts))
