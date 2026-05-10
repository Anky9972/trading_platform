"""
PROCESS 2: Risk Server.
Independent process from the trading engine.
Listens on a Unix domain socket.
Evaluates every signal against 12 pre-trade checks.

If you can't reach this process: NO TRADES.
This is the "fail-closed" guarantee.

Checks (in order):
  1. Kill switch
  2. Market hours
  3. Quantity sanity
  4. Order value (with 10% price buffer)
  5. Position limit per stock
  6. Portfolio exposure
  7. Available margin
  8. Sell holdings check
  9. Order rate limit (per minute)
  10. Daily order count
  11. Idempotency key
  12. Daily loss limit (with live price + staleness check)
"""
import os
import sys
import socket
import json
import threading
import logging
import collections
import time
from datetime import datetime, time as dtime

from config.settings import SETTINGS
from store.database import Database
from store.price_cache import PriceCache
from store.event_log import EventLog, EventType

os.makedirs("logs", exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | RISK | %(levelname)-8s | %(name)-20s | %(message)s',
    handlers=[
        logging.FileHandler(f"logs/risk_{datetime.now().strftime('%Y%m%d')}.log"),
        logging.StreamHandler(),
    ]
)
logger = logging.getLogger("risk.server")


class RiskServer:
    def __init__(self):
        self.db = Database(SETTINGS.DB_PATH)
        self.price_cache = PriceCache(self.db)
        self.events = EventLog()
        self._recent_orders = collections.deque(maxlen=200)
        self._available_margin = float(SETTINGS.MAX_CAPITAL)
        self._socket_path = SETTINGS.RISK_SOCKET_PATH
        
        # Initialize Peak Equity if not set
        if self.db.get_peak_equity() == 0.0:
            self.db.update_peak_equity(float(SETTINGS.MAX_CAPITAL))

    def serve(self):
        """Start Unix socket server."""
        if os.path.exists(self._socket_path):
            os.unlink(self._socket_path)

        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(self._socket_path)
        try:
            os.chmod(self._socket_path, 0o600)
        except (OSError, AttributeError):
            pass  # Windows doesn't support chmod on sockets
        server.listen(5)
        server.settimeout(1.0)

        self.events.emit(EventType.RISK_SERVER_START, {
            "socket": self._socket_path,
            "pid": os.getpid(),
        })
        logger.info(f"Risk server listening on {self._socket_path}")

        try:
            while True:
                try:
                    conn, _ = server.accept()
                    threading.Thread(
                        target=self._handle, args=(conn,), daemon=True
                    ).start()
                except socket.timeout:
                    continue
                except Exception as e:
                    logger.error(f"Socket accept error: {e}")
        except KeyboardInterrupt:
            logger.info("Risk server shutting down")
        finally:
            server.close()
            if os.path.exists(self._socket_path):
                os.unlink(self._socket_path)

    def _handle(self, conn):
        try:
            raw = conn.recv(16384).decode()
            if not raw:
                return

            request = json.loads(raw)
            action = request.get("action", "")

            if action == "check":
                signal = request.get("signal", {})
                result = self._evaluate(signal)
                conn.sendall(json.dumps(result).encode())

            elif action == "update_margin":
                self._available_margin = float(request.get("margin", 0))
                conn.sendall(json.dumps({"ok": True}).encode())

            elif action == "health":
                conn.sendall(json.dumps({"status": "alive"}).encode())

            else:
                conn.sendall(json.dumps({"error": f"Unknown: {action}"}).encode())

        except Exception as e:
            try:
                conn.sendall(json.dumps({"error": str(e)}).encode())
            except Exception:
                pass
        finally:
            conn.close()

    def _evaluate(self, signal: dict) -> dict:
        """Run all 12 checks. Any failure → REJECTED."""
        checks = [
            ("KILL_SWITCH", self._ck_kill_switch),
            ("MARKET_HOURS", self._ck_market_hours),
            ("QUANTITY", self._ck_quantity),
            ("ORDER_VALUE", self._ck_order_value),
            ("POSITION_LIMIT", self._ck_position_limit),
            ("EXPOSURE", self._ck_exposure),
            ("MARGIN", self._ck_margin),
            ("SELL_HOLDINGS", self._ck_sell_holdings),
            ("RATE_LIMIT", self._ck_rate_limit),
            ("DAILY_COUNT", self._ck_daily_count),
            ("IDEMPOTENCY", self._ck_idempotency),
            ("DAILY_LOSS", self._ck_daily_loss),
            ("GLOBAL_DRAWDOWN", self._ck_global_drawdown),
        ]

        passed = []
        failed = []
        reasons = []

        for name, check_fn in checks:
            try:
                ok, reason = check_fn(signal)
                if ok:
                    passed.append(name)
                else:
                    failed.append(name)
                    reasons.append(f"{name}: {reason}")
            except Exception as e:
                failed.append(name)
                reasons.append(f"{name}: EXCEPTION — {e}")

        verdict = "APPROVED" if not failed else "REJECTED"

        if verdict == "APPROVED":
            self._recent_orders.append(time.time())
            self.events.emit(EventType.RISK_APPROVED, {
                "symbol": signal.get("symbol"),
                "action": signal.get("action"),
                "quantity": signal.get("quantity"),
                "checks_passed": len(passed),
            })
        else:
            self.events.emit(EventType.RISK_REJECTED, {
                "symbol": signal.get("symbol"),
                "action": signal.get("action"),
                "quantity": signal.get("quantity"),
                "failed_checks": failed,
                "reasons": reasons,
            })

        return {
            "verdict": verdict,
            "passed": passed,
            "failed": failed,
            "reasons": reasons,
        }

    # === INDIVIDUAL CHECKS ===

    def _ck_kill_switch(self, sig) -> tuple:
        if os.path.exists(SETTINGS.KILL_SWITCH_FILE):
            return False, "Kill switch is active"
        return True, ""

    def _ck_market_hours(self, sig) -> tuple:
        now = datetime.now()
        if now.weekday() >= 5:
            return False, "Market closed (weekend)"
        t = now.time()
        if not (dtime(9, 0) <= t <= dtime(15, 45)):
            return False, f"Outside market hours: {t}"
        return True, ""

    def _ck_quantity(self, sig) -> tuple:
        qty = sig.get("quantity", 0)
        if qty <= 0:
            return False, f"Invalid quantity: {qty}"
        if qty > 5000:
            return False, f"Quantity {qty} exceeds max 5000"
        return True, ""

    def _ck_order_value(self, sig) -> tuple:
        price = sig.get("estimated_price", 0)
        if not price:
            price_info = self.price_cache.get(sig.get("symbol", ""))
            price = price_info.get("price", 0)
        qty = sig.get("quantity", 0)
        value = qty * price
        buffered = value * 1.10  # 10% buffer for market orders
        if buffered > SETTINGS.MAX_ORDER_VALUE:
            return False, f"Buffered order value ₹{buffered:,.0f} > max ₹{SETTINGS.MAX_ORDER_VALUE:,.0f}"
        return True, ""

    def _ck_position_limit(self, sig) -> tuple:
        if sig.get("action") == "SELL":
            return True, ""  # Sells always pass
        price = sig.get("estimated_price", 0)
        if not price:
            price_info = self.price_cache.get(sig.get("symbol", ""))
            price = price_info.get("price", 0)
        qty = sig.get("quantity", 0)
        pos = self.db.get_position(sig.get("symbol", ""))
        existing_qty = pos.get("quantity", 0)
        total_value = (existing_qty + qty) * price
        limit = SETTINGS.MAX_CAPITAL * SETTINGS.MAX_POSITION_PER_STOCK_PCT
        if total_value > limit:
            return False, f"Position value ₹{total_value:,.0f} > limit ₹{limit:,.0f}"
        return True, ""

    def _ck_exposure(self, sig) -> tuple:
        if sig.get("action") == "SELL":
            return True, ""
        positions = self.db.get_all_positions()
        total_exposure = 0
        for pos in positions:
            p_info = self.price_cache.get(pos['symbol'])
            price = p_info.get("price", pos.get("avg_price", 0))
            total_exposure += pos['quantity'] * price

        new_price = sig.get("estimated_price", 0)
        if not new_price:
            p_info = self.price_cache.get(sig.get("symbol", ""))
            new_price = p_info.get("price", 0)
        total_exposure += sig.get("quantity", 0) * new_price

        limit = SETTINGS.MAX_CAPITAL * SETTINGS.MAX_TOTAL_EXPOSURE_PCT
        if total_exposure > limit:
            return False, f"Total exposure ₹{total_exposure:,.0f} > limit ₹{limit:,.0f}"
        return True, ""

    def _ck_margin(self, sig) -> tuple:
        if sig.get("action") == "SELL":
            return True, ""
        price = sig.get("estimated_price", 0)
        if not price:
            p_info = self.price_cache.get(sig.get("symbol", ""))
            price = p_info.get("price", 0)
        required = sig.get("quantity", 0) * price * 1.10
        if required > self._available_margin:
            return False, f"Insufficient margin: need ₹{required:,.0f}, have ₹{self._available_margin:,.0f}"
        return True, ""

    def _ck_sell_holdings(self, sig) -> tuple:
        if sig.get("action") != "SELL":
            return True, ""
        pos = self.db.get_position(sig.get("symbol", ""))
        held = pos.get("quantity", 0)
        want_sell = sig.get("quantity", 0)
        if want_sell > held:
            return False, f"Hold {held}, trying to sell {want_sell}"
        return True, ""

    def _ck_rate_limit(self, sig) -> tuple:
        now = time.time()
        recent = [t for t in self._recent_orders if now - t < 60]
        if len(recent) >= SETTINGS.MAX_ORDERS_PER_MINUTE:
            return False, f"{len(recent)} orders in last minute (max {SETTINGS.MAX_ORDERS_PER_MINUTE})"
        return True, ""

    def _ck_daily_count(self, sig) -> tuple:
        count = self.db.count_orders_today()
        if count >= SETTINGS.MAX_ORDERS_PER_DAY:
            return False, f"{count} orders today (max {SETTINGS.MAX_ORDERS_PER_DAY})"
        return True, ""

    def _ck_idempotency(self, sig) -> tuple:
        key = sig.get("idempotency_key", "")
        if not key:
            return True, ""
        if self.db.has_idempotency_key(key):
            return False, f"Duplicate idempotency key: {key}"
        return True, ""

    def _ck_daily_loss(self, sig) -> tuple:
        """
        Check daily PnL using live prices from price cache.
        During market hours: reject if prices are stale (>90s).
        """
        positions = self.db.get_all_positions()
        if not positions:
            return True, ""

        now = datetime.now()
        is_market = (now.weekday() < 5 and
                     dtime(9, 0) <= now.time() <= dtime(15, 45))

        if is_market:
            max_age = self.price_cache.max_age_seconds()
            if max_age > SETTINGS.PRICE_STALENESS_LIMIT_SECONDS:
                return False, (f"FAIL CLOSED: prices are stale ({max_age:.0f}s, "
                             f"limit {SETTINGS.PRICE_STALENESS_LIMIT_SECONDS}s)")

        total_pnl = 0
        for pos in positions:
            cached = self.price_cache.get(pos['symbol'])
            live_price = cached.get("price", 0)
            if live_price <= 0:
                live_price = pos['avg_price']
            pnl = (live_price - pos['avg_price']) * pos['quantity']
            total_pnl += pnl

        loss_limit = -(SETTINGS.MAX_CAPITAL * SETTINGS.MAX_DAILY_LOSS_PCT)
        if total_pnl < loss_limit:
            return False, f"Daily loss ₹{total_pnl:,.0f} exceeds limit ₹{loss_limit:,.0f}"

        return True, ""

    def _ck_global_drawdown(self, sig) -> tuple:
        """
        Check peak-to-current drawdown across the entire account history.
        Formula: Drawdown = (Peak Equity - Current Equity) / Peak Equity
        """
        positions = self.db.get_all_positions()
        
        # Calculate Current Equity = Capital + Unrealized PnL
        current_equity = self._available_margin
        
        for pos in positions:
            cached = self.price_cache.get(pos['symbol'])
            live_price = cached.get("price", pos['avg_price'])
            if live_price <= 0:
                live_price = pos['avg_price']
            current_equity += pos['quantity'] * live_price

        # Update Peak if we reached a new high
        peak = self.db.get_peak_equity()
        if current_equity > peak:
            self.db.update_peak_equity(current_equity)
            peak = current_equity
            
        if peak <= 0:
            return True, ""

        dd = (peak - current_equity) / peak
        if dd > SETTINGS.MAX_GLOBAL_DRAWDOWN_PCT:
            return False, f"Global Drawdown {dd:.1%} > limit {SETTINGS.MAX_GLOBAL_DRAWDOWN_PCT:.1%} (Peak: ₹{peak:,.0f}, Current: ₹{current_equity:,.0f})"

        return True, ""


def main():
    server = RiskServer()
    server.serve()


if __name__ == "__main__":
    main()
