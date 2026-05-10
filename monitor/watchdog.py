"""
PROCESS 4: Watchdog — independent monitoring daemon.

Monitors:
  1. Engine heartbeat (stale = engine dead)
  2. Price updates via broker LTP
  3. Stop loss per position
  4. Continuous portfolio PnL vs daily loss limit
  5. Price validation (secondary source)

Emergency actions:
  - Kill switch activation on daily loss breach
  - Emergency sell when engine dead + stop loss breached
  - SIGTERM/SIGKILL engine via PID file

Independent: runs even when engine is dead.
Must ALWAYS restart (systemd Restart=always, no burst limit).
"""
import os
import sys
import time
import logging
from datetime import datetime, time as dtime

from config.settings import SETTINGS
from store.database import Database
from store.event_log import EventLog, EventType
from store.price_cache import PriceCache
from store.price_validator import PriceValidator
from risk.kill_switch import KillSwitch
from monitor.alerting import alert
from monitor.emergency_executor import EmergencyExecutor

os.makedirs("logs", exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | WATCHDOG | %(levelname)-8s | %(name)-20s | %(message)s',
    handlers=[
        logging.FileHandler(f"logs/watchdog_{datetime.now().strftime('%Y%m%d')}.log"),
        logging.StreamHandler(),
    ]
)
logger = logging.getLogger("watchdog")


class Watchdog:
    def __init__(self):
        self.db = Database(SETTINGS.DB_PATH)
        self.events = EventLog()
        self.price_cache = PriceCache(self.db)
        self.price_validator = PriceValidator(self.events)

        # Broker for LTP and emergency sells
        if SETTINGS.is_paper():
            from broker.paper import PaperBroker
            self.broker = PaperBroker()
        else:
            from broker.angel import AngelBroker
            self.broker = AngelBroker()

        self.emergency = EmergencyExecutor(self.db, self.broker, self.events)

    def run(self):
        """Main watchdog loop. Runs independently of engine."""
        logger.info("Watchdog starting")
        self.events.emit(EventType.WATCHDOG_START, {"pid": os.getpid()})

        # Connect broker
        try:
            self.broker.connect()
            logger.info("Watchdog broker connected")
        except Exception as e:
            logger.error(f"Watchdog broker connection failed: {e}")
            # Continue anyway — will retry each cycle

        while True:
            try:
                now = datetime.now()
                if self._is_market_hours(now):
                    self._check_cycle()
                else:
                    # Minimal check outside market hours
                    self._check_heartbeat()

                time.sleep(SETTINGS.PRICE_POLL_INTERVAL_SECONDS)

            except KeyboardInterrupt:
                logger.info("Watchdog stopped")
                break
            except Exception as e:
                logger.error(f"Watchdog cycle error: {e}", exc_info=True)
                time.sleep(30)  # Back off on error

    def _check_cycle(self):
        """Full monitoring cycle during market hours."""
        # 1. Check engine heartbeat
        engine_alive = self._check_heartbeat()

        # 2. Fetch and cache prices
        prices = self._update_prices()

        # 3. Check stop losses per position
        self._check_stop_losses(prices, engine_alive)

        # 4. Continuous portfolio PnL check
        self._check_portfolio_pnl(prices)

    def _check_heartbeat(self) -> bool:
        """Check engine heartbeat. Returns True if engine is alive."""
        heartbeat = self.db.read_heartbeat()
        if not heartbeat:
            return False

        try:
            last = datetime.fromisoformat(heartbeat)
            age = (datetime.now() - last).total_seconds()

            if age > SETTINGS.HEARTBEAT_MAX_AGE_SECONDS:
                logger.warning(f"Engine heartbeat stale: {age:.0f}s old")
                alert(f"Engine heartbeat stale: {age:.0f}s. Engine may be dead.",
                      level="WARNING")
                return False

            return True

        except Exception:
            return False

    def _update_prices(self) -> dict:
        """Fetch LTP for all positions and update price cache."""
        positions = self.db.get_all_positions()
        if not positions:
            return {}

        symbols = [p['symbol'] for p in positions]
        prices = {}

        for symbol in symbols:
            try:
                ltp = self.broker.get_ltp(symbol)
                if ltp and ltp > 0:
                    validated = self.price_validator.validate(symbol, ltp)
                    if validated > 0:
                        prices[symbol] = validated
            except Exception as e:
                logger.warning(f"LTP fetch failed for {symbol}: {e}")

        if prices:
            self.price_cache.update_batch(prices)

        return prices

    def _check_stop_losses(self, prices: dict, engine_alive: bool):
        """Check stop loss for each position."""
        positions = self.db.get_all_positions()

        for pos in positions:
            symbol = pos['symbol']
            qty = pos['quantity']
            entry = pos['avg_price']

            if qty <= 0 or entry <= 0:
                continue

            current = prices.get(symbol, 0)
            if current <= 0:
                continue

            pnl_pct = (current - entry) / entry

            if pnl_pct <= -SETTINGS.STOP_LOSS_PCT:
                logger.warning(
                    f"STOP LOSS BREACH: {symbol} entry=₹{entry:.2f} "
                    f"current=₹{current:.2f} pnl={pnl_pct:.1%}"
                )

                self.events.emit(EventType.STOP_LOSS_BREACH, {
                    "symbol": symbol,
                    "entry_price": entry,
                    "current_price": current,
                    "pnl_pct": round(pnl_pct * 100, 2),
                    "engine_alive": engine_alive,
                })

                alert(
                    f"🔴 STOP LOSS: {symbol} at {pnl_pct:.1%} "
                    f"(entry ₹{entry:.0f} → ₹{current:.0f})",
                    level="CRITICAL"
                )

                # Emergency sell if engine is dead
                if self.emergency.should_execute(symbol, pnl_pct,
                                                  engine_alive=engine_alive):
                    self.emergency.execute_emergency_sell(
                        symbol, qty, current, entry, pnl_pct
                    )

    def _check_portfolio_pnl(self, prices: dict):
        """
        Continuous portfolio PnL check.
        Activates kill switch if total daily loss exceeds limit.
        This runs EVERY cycle — not just on new orders.
        """
        positions = self.db.get_all_positions()
        if not positions:
            return

        total_pnl = 0
        for pos in positions:
            symbol = pos['symbol']
            qty = pos['quantity']
            entry = pos['avg_price']

            current = prices.get(symbol, 0)
            if current <= 0:
                cached = self.price_cache.get(symbol)
                current = cached.get("price", entry)

            pnl = (current - entry) * qty
            total_pnl += pnl

        loss_limit = -(SETTINGS.MAX_CAPITAL * SETTINGS.MAX_DAILY_LOSS_PCT)

        if total_pnl < loss_limit:
            logger.critical(
                f"DAILY LOSS BREACH: ₹{total_pnl:,.0f} exceeds limit ₹{loss_limit:,.0f}"
            )

            self.events.emit(EventType.CONTINUOUS_LOSS_BREACH, {
                "total_pnl": round(total_pnl, 2),
                "loss_limit": round(loss_limit, 2),
                "positions": len(positions),
            })

            alert(
                f"🔴🔴 DAILY LOSS BREACH: ₹{total_pnl:,.0f} "
                f"(limit ₹{loss_limit:,.0f}). KILL SWITCH ACTIVATED.",
                level="CRITICAL"
            )

            ks = KillSwitch(self.db)
            ks.activate(
                f"CONTINUOUS MONITORING: daily loss ₹{total_pnl:,.0f} "
                f"exceeds limit ₹{loss_limit:,.0f}"
            )

    def _is_market_hours(self, now: datetime) -> bool:
        if now.weekday() >= 5:
            return False
        t = now.time()
        return dtime(9, 0) <= t <= dtime(15, 45)


def main():
    watchdog = Watchdog()
    watchdog.run()


if __name__ == "__main__":
    main()
