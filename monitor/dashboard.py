"""
Terminal dashboard.
curses-based real-time monitoring display.

Sections:
  - System status (kill switch, heartbeat, risk server)
  - Positions with live PnL
  - Recent events

Controls:
  q = quit
  k = activate kill switch
"""
import os
import sys
import time
import json
import curses
import threading
import logging
from datetime import datetime

from config.settings import SETTINGS
from store.database import Database
from store.event_log import EventLog
from store.price_cache import PriceCache

logger = logging.getLogger(__name__)


class DashboardPoller:
    """Background poller — avoids blocking the curses UI."""
    def __init__(self):
        self.db = Database(SETTINGS.DB_PATH)
        self.events = EventLog()
        self.price_cache = PriceCache(self.db)
        self._data = {}
        self._lock = threading.Lock()
        self._running = False

    def start(self):
        self._running = True
        threading.Thread(target=self._poll, daemon=True).start()

    def stop(self):
        self._running = False

    def _poll(self):
        while self._running:
            try:
                data = {
                    "kill_switch": os.path.exists(SETTINGS.KILL_SWITCH_FILE),
                    "heartbeat": self.db.read_heartbeat(),
                    "positions": self.db.get_all_positions(),
                    "orders_today": self.db.count_orders_today(),
                    "live_orders": len(self.db.get_live_orders()),
                    "prices": self.price_cache.get_all(),
                    "events": self.events.tail(20),
                }
                with self._lock:
                    self._data = data
            except Exception as e:
                pass
            time.sleep(2)

    def get(self) -> dict:
        with self._lock:
            return dict(self._data)


def run_dashboard(stdscr):
    curses.curs_set(0)
    stdscr.nodelay(True)
    stdscr.timeout(1000)

    # Colors
    curses.start_color()
    curses.use_default_colors()
    curses.init_pair(1, curses.COLOR_GREEN, -1)
    curses.init_pair(2, curses.COLOR_RED, -1)
    curses.init_pair(3, curses.COLOR_YELLOW, -1)
    curses.init_pair(4, curses.COLOR_CYAN, -1)

    poller = DashboardPoller()
    poller.start()

    try:
        while True:
            try:
                stdscr.clear()
                height, width = stdscr.getmaxyx()
                data = poller.get()

                row = 0

                # Header
                header = f" TRADING PLATFORM — {SETTINGS.TRADING_MODE.upper()} MODE "
                stdscr.addstr(row, 0, f"{'═' * width}", curses.color_pair(4))
                row += 1
                stdscr.addstr(row, max(0, (width - len(header)) // 2),
                             header, curses.A_BOLD | curses.color_pair(4))
                row += 1
                stdscr.addstr(row, 0, f"{'═' * width}", curses.color_pair(4))
                row += 2

                # Kill switch
                ks = data.get("kill_switch", False)
                ks_color = curses.color_pair(2) if ks else curses.color_pair(1)
                ks_text = "🔴 ACTIVE" if ks else "✅ OFF"
                stdscr.addstr(row, 2, f"Kill Switch: {ks_text}", ks_color | curses.A_BOLD)
                row += 1

                # Heartbeat
                hb = data.get("heartbeat")
                if hb:
                    try:
                        age = (datetime.now() - datetime.fromisoformat(hb)).total_seconds()
                        hb_color = curses.color_pair(1) if age < 120 else curses.color_pair(2)
                        stdscr.addstr(row, 2, f"Heartbeat:   {age:.0f}s ago", hb_color)
                    except Exception:
                        stdscr.addstr(row, 2, "Heartbeat:   unknown", curses.color_pair(3))
                else:
                    stdscr.addstr(row, 2, "Heartbeat:   NO DATA", curses.color_pair(2))
                row += 1

                # Orders
                orders = data.get("orders_today", 0)
                live = data.get("live_orders", 0)
                stdscr.addstr(row, 2, f"Orders:      {orders} today, {live} live",
                             curses.color_pair(0))
                row += 2

                # Positions
                positions = data.get("positions", [])
                prices = data.get("prices", {})

                stdscr.addstr(row, 2,
                    f"{'Symbol':<15} {'Qty':>6} {'Entry':>10} {'Current':>10} {'PnL':>12} {'%':>8}",
                    curses.A_BOLD | curses.A_UNDERLINE)
                row += 1

                total_pnl = 0
                for pos in positions:
                    symbol = pos['symbol']
                    qty = pos['quantity']
                    entry = pos['avg_price']

                    p_info = prices.get(symbol, {})
                    current = p_info.get("price", entry)

                    pnl = (current - entry) * qty
                    total_pnl += pnl
                    pnl_pct = ((current - entry) / entry * 100) if entry > 0 else 0

                    color = curses.color_pair(1) if pnl >= 0 else curses.color_pair(2)
                    line = (f"  {symbol:<15} {qty:>6} ₹{entry:>9,.2f} ₹{current:>9,.2f} "
                           f"₹{pnl:>10,.0f} {pnl_pct:>7.1f}%")
                    if row < height - 5:
                        stdscr.addstr(row, 0, line[:width-1], color)
                        row += 1

                if positions:
                    row += 1
                    total_color = curses.color_pair(1) if total_pnl >= 0 else curses.color_pair(2)
                    stdscr.addstr(row, 2, f"Total PnL: ₹{total_pnl:,.0f}",
                                 total_color | curses.A_BOLD)
                    row += 2

                # Events
                events = data.get("events", [])
                if events and row < height - 3:
                    stdscr.addstr(row, 2, "Recent Events:", curses.A_BOLD)
                    row += 1

                max_events = min(height - row - 3, 12)
                for evt in events[-max_events:]:
                    ts = evt.get("ts", "")[-8:]
                    etype = evt.get("type", "")
                    evt_data = evt.get("data", {})

                    color = curses.color_pair(0)
                    if any(w in etype for w in ("reject", "breach", "fail", "kill")):
                        color = curses.color_pair(2)
                    elif any(w in etype for w in ("filled", "approved")):
                        color = curses.color_pair(1)
                    elif "unknown" in etype or "warning" in etype:
                        color = curses.color_pair(3)

                    sym = evt_data.get("symbol", "")
                    summary = f"  {ts} {etype:<28} {sym}"
                    if row < height - 2:
                        stdscr.addstr(row, 0, summary[:width-1], color)
                        row += 1

                if height > 2:
                    stdscr.addstr(height-1, 1, " q=quit  k=kill-switch ", curses.A_REVERSE)

                stdscr.refresh()

                ch = stdscr.getch()
                if ch == ord('q'):
                    break
                elif ch == ord('k'):
                    from risk.kill_switch import KillSwitch
                    KillSwitch().activate("Dashboard manual kill")

                time.sleep(1)

            except curses.error:
                pass
            except KeyboardInterrupt:
                break
    finally:
        poller.stop()


def main():
    curses.wrapper(run_dashboard)


if __name__ == "__main__":
    main()
