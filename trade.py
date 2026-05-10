#!/usr/bin/env python3
"""
CLI entry point.
Thin client — no broker connections, no database access, no business logic.
Sends HMAC-authenticated commands to running engine daemon via Unix socket.
"""
import sys
import os
import json
import socket
import hashlib
import hmac
import subprocess

from config.settings import SETTINGS


def send_command(cmd: dict) -> dict:
    """Send HMAC-authenticated command to engine daemon."""
    if not os.path.exists(SETTINGS.SOCKET_PATH):
        print("Engine is not running. Start with: python trade.py start")
        sys.exit(1)

    auth_key = hashlib.sha256(SETTINGS.ANGEL_API_KEY.encode()).hexdigest()[:32]
    payload_str = json.dumps(cmd, sort_keys=True)
    mac = hmac.new(auth_key.encode(), payload_str.encode(), hashlib.sha256).hexdigest()
    envelope = {"payload": cmd, "hmac": mac}

    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(10.0)
    try:
        sock.connect(SETTINGS.SOCKET_PATH)
        sock.sendall(json.dumps(envelope).encode())
        response = sock.recv(65536).decode()
        return json.loads(response)
    except Exception as e:
        print(f"Socket error: {e}")
        sys.exit(1)
    finally:
        sock.close()


def main():
    if len(sys.argv) < 2:
        print_usage()
        return

    cmd = sys.argv[1].lower()

    if cmd == "start":
        if os.path.exists(SETTINGS.SOCKET_PATH):
            print("Engine already running.")
            return
        os.makedirs("logs", exist_ok=True)
        print("Starting engine daemon...")
        subprocess.Popen(
            [sys.executable, "engine.py"],
            stdout=open("logs/engine_stdout.log", 'a'),
            stderr=open("logs/engine_stderr.log", 'a'),
            start_new_session=True
        )
        print("Engine started. Check logs/engine_*.log")

    elif cmd == "start-watchdog":
        os.makedirs("logs", exist_ok=True)
        print("Starting watchdog daemon...")
        subprocess.Popen(
            [sys.executable, "monitor/watchdog.py"],
            stdout=open("logs/watchdog_stdout.log", 'a'),
            stderr=open("logs/watchdog_stderr.log", 'a'),
            start_new_session=True
        )
        print("Watchdog started. Check logs/watchdog_*.log")

    elif cmd in ("buy", "sell"):
        if len(sys.argv) < 4:
            print(f"Usage: python trade.py {cmd} SYMBOL QUANTITY [limit PRICE]")
            return

        symbol = sys.argv[2].upper()
        qty = int(sys.argv[3])
        order_type = "MARKET"
        price = None

        if len(sys.argv) >= 6 and sys.argv[4].lower() == "limit":
            order_type = "LIMIT"
            price = float(sys.argv[5])

        result = send_command({
            "action": cmd,
            "symbol": symbol,
            "quantity": qty,
            "order_type": order_type,
            "price": price,
        })

        print(f"\n{'='*50}")
        print(f"  Order:     {result.get('internal_id', 'N/A')}")
        print(f"  State:     {result.get('state', 'N/A')}")
        print(f"  Broker ID: {result.get('broker_id', 'N/A')}")
        if result.get('filled'):
            print(f"  Filled:    {result['filled']} @ ₹{result['avg_price']}")
        if result.get('rejection'):
            print(f"  Reason:    {result['rejection']}")
        print(f"{'='*50}")

    elif cmd == "status":
        result = send_command({"action": "status"})
        print(json.dumps(result, indent=2))

    elif cmd == "positions":
        result = send_command({"action": "positions"})
        positions = result.get('positions', [])
        if positions:
            print(f"\n  {'Symbol':<15} {'Qty':>6} {'Avg Price':>12}")
            print(f"  {'─'*35}")
            for p in positions:
                print(f"  {p['symbol']:<15} {p['quantity']:>6} ₹{p['avg_price']:>10,.2f}")
        else:
            print("  No open positions.")

    elif cmd == "orders":
        # Read directly from DB for order history
        from store.database import Database
        db = Database(SETTINGS.DB_PATH)
        orders = db.get_orders_today()
        if orders:
            for o in orders:
                print(f"  {o['internal_id'][:8]} | {o['action']} {o['quantity']}x "
                      f"{o['symbol']} | {o['state']}")
        else:
            print("  No orders today.")

    elif cmd == "reconcile":
        result = send_command({"action": "reconcile"})
        disc = result.get('discrepancies', [])
        if disc:
            for d in disc:
                print(f"  ⚠  {d}")
        else:
            print("  ✅ No discrepancies found.")

    elif cmd == "kill":
        reason = " ".join(sys.argv[2:]) if len(sys.argv) > 2 else "Manual CLI kill"
        result = send_command({"action": "kill", "reason": reason})
        print(f"🛑 {result.get('result', 'Done')}")
        print(f"   To resume: rm {SETTINGS.KILL_SWITCH_FILE} && python trade.py unkill")

    elif cmd == "unkill":
        if os.path.exists(SETTINGS.KILL_SWITCH_FILE):
            print(f"Remove kill switch file first: rm {SETTINGS.KILL_SWITCH_FILE}")
            return
        from risk.kill_switch import KillSwitch
        ks = KillSwitch()
        if ks.deactivate():
            print("✅ Kill switch deactivated. Start engine to resume trading.")

    elif cmd == "stop":
        result = send_command({"action": "shutdown"})
        print(f"Shutdown: {result.get('result', 'Done')}")

    elif cmd == "tail":
        os.system("tail -f alerts.log")

    elif cmd == "dashboard":
        from monitor.dashboard import main as dash_main
        dash_main()

    elif cmd == "events":
        os.system("tail -f events.jsonl | python3 -m json.tool 2>/dev/null || tail -f events.jsonl")

    elif cmd == "af":
        # Alpha Factory subcommands. Doesn't touch the engine socket -- this
        # is a pure research surface (lint, factor-store, BRAIN submit).
        from research.alpha_factory.cli import main as af_main
        sys.exit(af_main(sys.argv[2:]))

    else:
        print_usage()


def print_usage():
    print("""
TRADING PLATFORM CLI
══════════════════════════════════════════════════
  python trade.py start               Start engine daemon
  python trade.py start-watchdog      Start watchdog daemon
  python trade.py buy SYMBOL QTY      Market buy
  python trade.py sell SYMBOL QTY     Market sell
  python trade.py buy SYM QTY limit P Limit buy
  python trade.py status              System health (JSON)
  python trade.py positions           Current positions
  python trade.py orders              Today's orders
  python trade.py reconcile           Force reconciliation
  python trade.py kill [reason]       Emergency stop
  python trade.py unkill              Resume after kill
  python trade.py stop                Graceful shutdown
  python trade.py tail                Watch alerts in real time
  python trade.py dashboard           Terminal dashboard
  python trade.py events              Watch event log

  ALPHA FACTORY (research):
  python trade.py af lint  "<expr>"   Static lint a BRAIN expression
  python trade.py af list             List alphas in factor store
  python trade.py af submit ...       Lint+register (+--live to submit)
  python trade.py af stats            Factor store summary
  python trade.py af themes           List dead themes
══════════════════════════════════════════════════
  TRADING_MODE=paper  (default — no real orders)
  TRADING_MODE=live   (real money — set explicitly)
    """)


if __name__ == "__main__":
    main()
