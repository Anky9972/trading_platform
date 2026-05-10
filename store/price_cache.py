"""
Shared price cache in SQLite.
Written by: Watchdog (has broker access, polls prices for stop losses anyway)
Read by: Risk Server (for daily loss check), Engine, Dashboard

This is the architectural fix for the zero-PnL daily loss check bug.
The Risk Server needs live prices but cannot have broker credentials.
The Watchdog already fetches prices. It writes them here. Risk Server reads.

Staleness policy:
  > 90 seconds during market hours → Risk Server rejects all orders (fail-closed)
  > 90 seconds outside market hours → accepted (expected, market is closed)
"""
from datetime import datetime
from store.database import Database


class PriceCache:
    def __init__(self, db: Database):
        self.db = db

    def update(self, symbol: str, price: float):
        """Called by Watchdog after fetching LTP."""
        with self.db.tx() as conn:
            conn.execute("""
                INSERT INTO price_cache (symbol, price, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(symbol) DO UPDATE SET
                    price = excluded.price,
                    updated_at = excluded.updated_at
            """, (symbol, price, datetime.now().isoformat()))

    def update_batch(self, prices: dict):
        """Update multiple prices atomically. Called by Watchdog after LTP batch fetch."""
        now = datetime.now().isoformat()
        with self.db.tx() as conn:
            for symbol, price in prices.items():
                if price and price > 0:
                    conn.execute("""
                        INSERT INTO price_cache (symbol, price, updated_at)
                        VALUES (?, ?, ?)
                        ON CONFLICT(symbol) DO UPDATE SET
                            price = excluded.price,
                            updated_at = excluded.updated_at
                    """, (symbol, price, now))

    def get(self, symbol: str) -> dict:
        """Returns price info including staleness."""
        row = self.db._conn().execute(
            "SELECT price, updated_at FROM price_cache WHERE symbol = ?",
            (symbol,)
        ).fetchone()

        if not row:
            return {"price": 0.0, "updated_at": None, "age_seconds": float('inf')}

        updated = datetime.fromisoformat(row['updated_at'])
        age = (datetime.now() - updated).total_seconds()
        return {"price": row['price'], "updated_at": row['updated_at'], "age_seconds": age}

    def get_all(self) -> dict:
        """Returns {symbol: {"price": float, "age_seconds": float}}"""
        rows = self.db._conn().execute(
            "SELECT symbol, price, updated_at FROM price_cache"
        ).fetchall()

        result = {}
        now = datetime.now()
        for row in rows:
            updated = datetime.fromisoformat(row['updated_at'])
            result[row['symbol']] = {
                "price": row['price'],
                "age_seconds": (now - updated).total_seconds(),
            }
        return result

    def max_age_seconds(self) -> float:
        """Age of the stalest price. Uses index on updated_at."""
        row = self.db._conn().execute(
            "SELECT MIN(updated_at) as oldest FROM price_cache"
        ).fetchone()
        if not row or not row['oldest']:
            return float('inf')
        oldest = datetime.fromisoformat(row['oldest'])
        return (datetime.now() - oldest).total_seconds()
