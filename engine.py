"""
PROCESS 1: Trading Engine Daemon.
Runs continuously. Writes PID file. Heartbeats every 60s.
Accepts CLI commands via Unix socket.
Executes strategy cycles during market hours.

Startup sequence:
  1. Write PID file
  2. Register SIGTERM/SIGINT handlers
  3. Check kill switch
  4. Connect broker
  5. Run enforcing reconciliation
  6. Sync non-terminal orders
  7. Write heartbeat
  8. Start socket server (background thread)
  9. Enter main loop

NOT responsible for: price monitoring, stop losses, position reconciliation timing.
Those belong to the Watchdog (Process 4).
"""
import os
import sys
import time
import signal as os_signal
import logging
import threading
from datetime import datetime, time as dtime

from config.settings import SETTINGS
from store.database import Database
from store.event_log import EventLog, EventType
from broker.angel import AngelBroker
from broker.paper import PaperBroker
from risk.kill_switch import KillSwitch
from risk.client import RiskClient
from core.order_manager import OrderManager
from core.reconciliation import Reconciler
from core.models import Signal, Action, OrderType, OrderState
from strategy.runner import StrategyRunner
from ipc.socket_server import SocketServer

os.makedirs("logs", exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | ENGINE | %(levelname)-8s | %(name)-20s | %(message)s',
    handlers=[
        logging.FileHandler(f"logs/engine_{datetime.now().strftime('%Y%m%d')}.log"),
        logging.StreamHandler(),
    ]
)
logger = logging.getLogger("engine")


class TradingEngine:
    def __init__(self):
        self.db = Database(SETTINGS.DB_PATH)
        self.events = EventLog()
        self.kill_switch = KillSwitch(self.db)

        if SETTINGS.is_paper():
            logger.info("MODE: PAPER TRADING")
            self.broker = PaperBroker()
        else:
            logger.warning("MODE: LIVE TRADING — REAL MONEY")
            self.broker = AngelBroker()

        self.risk = RiskClient()
        self.order_mgr = OrderManager(self.broker, self.risk, self.db, self.events)
        self.reconciler = Reconciler(self.db, self.broker, self.events)

        self._strategies = []
        self._shutdown = threading.Event()
        self._socket_server = None

    def startup(self) -> bool:
        logger.info("=" * 60)
        logger.info("TRADING ENGINE STARTUP")
        logger.info(f"Mode: {SETTINGS.TRADING_MODE.upper()}")
        logger.info("=" * 60)

        # Write PID file
        with open(SETTINGS.PID_FILE, 'w') as f:
            f.write(str(os.getpid()))

        # Register signal handlers
        os_signal.signal(os_signal.SIGTERM, self._handle_signal)
        os_signal.signal(os_signal.SIGINT, self._handle_signal)

        # Kill switch check
        if self.kill_switch.is_active():
            logger.critical("KILL SWITCH IS ACTIVE. Cannot start.")
            logger.critical(f"  Status: {self.kill_switch.status['reason']}")
            logger.critical(f"  To deactivate: rm {SETTINGS.KILL_SWITCH_FILE}")
            return False

        # Connect broker
        try:
            self.broker.connect()
            self.events.emit(EventType.BROKER_CONNECTED, {"mode": SETTINGS.TRADING_MODE})
            logger.info("Broker connected")
        except Exception as e:
            logger.error(f"Broker connection failed: {e}")
            self.events.emit(EventType.BROKER_ERROR, {"error": str(e)})
            return False

        # Enforcing reconciliation on startup
        logger.info("Running startup reconciliation...")
        corrections = self.reconciler.reconcile_positions()
        if corrections:
            logger.warning(f"Reconciliation corrected {len(corrections)} position(s)")
        self.reconciler.resolve_unknown_orders()
        self.order_mgr.sync_all_orders()

        # Update margin in Risk Server
        try:
            funds = self.broker.get_funds()
            self.risk.update_margin(funds.get("available_cash", SETTINGS.MAX_CAPITAL))
        except Exception:
            pass

        # Write heartbeat
        self.db.write_heartbeat()
        self.events.emit(EventType.ENGINE_START, {
            "pid": os.getpid(),
            "mode": SETTINGS.TRADING_MODE,
        })

        logger.info("Startup complete")
        return True

    def _handle_signal(self, signum, frame):
        logger.info(f"Received signal {signum}. Shutting down gracefully.")
        self._shutdown.set()

    def register_strategy(self, strategy_path: str, strategy_id: str,
                          timeout: int = 30):
        """Register a strategy file for execution."""
        runner = StrategyRunner(strategy_path, strategy_id, timeout)
        self._strategies.append(runner)
        logger.info(f"Strategy registered: {strategy_id} ({strategy_path})")

    def run_once(self) -> list:
        """Run one strategy cycle. Called by main loop each interval."""
        if self.kill_switch.is_active():
            logger.warning("Kill switch active. Skipping cycle.")
            return []

        results = []

        # Build context for strategy subprocess
        positions = self.db.get_all_positions()
        context = {
            "positions": positions,
            "capital": SETTINGS.MAX_CAPITAL,
            "timestamp": datetime.now().isoformat(),
        }

        for runner in self._strategies:
            try:
                signals = runner.run(context)
                logger.info(f"{runner.strategy_id}: {len(signals)} signal(s)")

                for signal in signals:
                    order = self.order_mgr.submit(signal)
                    results.append(order)

            except Exception as e:
                logger.error(f"Strategy {runner.strategy_id} error: {e}")

        # Sync non-terminal orders
        self.order_mgr.sync_all_orders()

        # Refresh margin for Risk Server
        try:
            funds = self.broker.get_funds()
            self.risk.update_margin(funds.get("available_cash", SETTINGS.MAX_CAPITAL))
        except Exception:
            pass

        # Heartbeat
        self.db.write_heartbeat()
        self.events.emit(EventType.HEARTBEAT, {
            "orders_in_cycle": len(results),
            "positions": len(positions),
        })

        return results

    def submit_manual(self, symbol: str, action: str, qty: int,
                      order_type: str = "MARKET", price: float = None):
        """Manual order — still goes through full risk pipeline."""
        signal = Signal(
            symbol=symbol,
            action=Action[action.upper()],
            quantity=qty,
            order_type=OrderType.LIMIT if order_type == "LIMIT" else OrderType.MARKET,
            price=price,
            strategy_id="manual",
            reason="Manual CLI order",
        )
        return self.order_mgr.submit(signal)

    def run_daemon(self, interval_seconds: int = 60):
        """
        Run as daemon. Execute strategy cycle every interval.
        Accept CLI commands via socket in background thread.
        """
        self._socket_server = SocketServer(self, SETTINGS.SOCKET_PATH)
        socket_thread = threading.Thread(
            target=self._socket_server.serve, daemon=True
        )
        socket_thread.start()
        logger.info(f"Daemon running. Cycle every {interval_seconds}s.")

        while not self._shutdown.is_set():
            try:
                now = datetime.now()
                if self._is_market_hours(now):
                    self.run_once()
                else:
                    # Outside market hours: just heartbeat
                    self.db.write_heartbeat()
                    self.events.emit(EventType.HEARTBEAT, {"market": "closed"})

                self._shutdown.wait(timeout=interval_seconds)

            except Exception as e:
                logger.error(f"Daemon cycle error: {e}", exc_info=True)
                self._shutdown.wait(timeout=30)

        self._cleanup()

    def _is_market_hours(self, now: datetime) -> bool:
        if now.weekday() >= 5:
            return False
        t = now.time()
        return dtime(9, 0) <= t <= dtime(15, 45)

    def _cleanup(self):
        logger.info("Engine shutting down...")
        self.events.emit(EventType.ENGINE_STOP, {"pid": os.getpid()})

        if self._socket_server:
            self._socket_server.stop()

        for runner in self._strategies:
            runner.cleanup()

        if os.path.exists(SETTINGS.PID_FILE):
            os.unlink(SETTINGS.PID_FILE)

        logger.info("Engine stopped")

    def shutdown(self):
        self._shutdown.set()


def main():
    engine = TradingEngine()

    if not engine.startup():
        sys.exit(1)

    # Auto-discover and register all user strategies
    strategy_dir = os.path.join(os.path.dirname(__file__), "strategy", "user_strategies")
    for fname in sorted(os.listdir(strategy_dir)):
        if fname.endswith(".py") and fname != "__init__.py":
            strategy_id = fname.replace(".py", "")
            strategy_path = os.path.join(strategy_dir, fname)
            engine.register_strategy(strategy_path, strategy_id, timeout=30)

    engine.run_daemon(interval_seconds=60)


if __name__ == "__main__":
    main()
