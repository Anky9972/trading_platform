"""
Unix domain socket server for CLI → Engine communication.
Commands authenticated with HMAC-SHA256.
Prevents unauthorized local processes from injecting trade commands.
Socket file has 600 permissions (owner-only access).
"""
import os
import socket
import json
import hmac
import hashlib
import threading
import logging
from config.settings import SETTINGS

logger = logging.getLogger(__name__)


def compute_hmac(payload_str: str, key: str) -> str:
    return hmac.new(key.encode(), payload_str.encode(), hashlib.sha256).hexdigest()


def verify_hmac(payload_str: str, received_mac: str, key: str) -> bool:
    expected = compute_hmac(payload_str, key)
    return hmac.compare_digest(expected, received_mac)


class SocketServer:
    def __init__(self, engine, socket_path: str):
        self.engine = engine
        self.socket_path = socket_path
        # HMAC key derived from ANGEL_API_KEY (already in memory on both ends)
        self._auth_key = hashlib.sha256(
            SETTINGS.ANGEL_API_KEY.encode()
        ).hexdigest()[:32]
        self._running = False
        self._server = None

    def serve(self):
        if os.path.exists(self.socket_path):
            os.unlink(self.socket_path)

        self._server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._server.bind(self.socket_path)
        try:
            os.chmod(self.socket_path, 0o600)
        except (OSError, AttributeError):
            pass  # Windows
        self._server.listen(5)
        self._server.settimeout(1.0)
        self._running = True

        logger.info(f"Socket server listening on {self.socket_path}")

        while self._running:
            try:
                conn, _ = self._server.accept()
                threading.Thread(target=self._handle, args=(conn,), daemon=True).start()
            except socket.timeout:
                continue
            except Exception as e:
                if self._running:
                    logger.error(f"Socket error: {e}")

    def _handle(self, conn):
        try:
            raw = conn.recv(16384).decode()
            if not raw:
                return

            envelope = json.loads(raw)
            payload = envelope.get("payload", {})
            payload_str = json.dumps(payload, sort_keys=True)
            received_mac = envelope.get("hmac", "")

            if not verify_hmac(payload_str, received_mac, self._auth_key):
                conn.sendall(json.dumps({"error": "Authentication failed"}).encode())
                logger.warning("Unauthorized socket command rejected")
                return

            response = self._process(payload)
            conn.sendall(json.dumps(response).encode())

        except Exception as e:
            try:
                conn.sendall(json.dumps({"error": str(e)}).encode())
            except Exception:
                pass
        finally:
            conn.close()

    def _process(self, cmd: dict) -> dict:
        action = cmd.get("action", "")

        if action in ("buy", "sell"):
            order = self.engine.submit_manual(
                cmd["symbol"], action.upper(), cmd["quantity"],
                cmd.get("order_type", "MARKET"), cmd.get("price")
            )
            return {
                "internal_id": order.internal_id,
                "state": order.state.value,
                "broker_id": order.broker_order_id,
                "filled": order.filled_quantity,
                "avg_price": order.avg_fill_price,
                "rejection": order.rejection_reason,
            }

        elif action == "status":
            return {
                "kill_switch": os.path.exists(SETTINGS.KILL_SWITCH_FILE),
                "positions": self.engine.db.get_all_positions(),
                "orders_today": self.engine.db.count_orders_today(),
                "live_orders": len(self.engine.db.get_live_orders()),
                "mode": SETTINGS.TRADING_MODE,
            }

        elif action == "kill":
            self.engine.kill_switch.activate(cmd.get("reason", "CLI kill"))
            return {"result": "kill switch activated"}

        elif action == "positions":
            return {"positions": self.engine.db.get_all_positions()}

        elif action == "reconcile":
            d = self.engine.reconciler.reconcile_positions()
            return {"discrepancies": d}

        elif action == "shutdown":
            self.engine.shutdown()
            return {"result": "shutdown initiated"}

        elif action == "health":
            return {
                "status": "alive",
                "mode": SETTINGS.TRADING_MODE,
                "orders_today": self.engine.db.count_orders_today(),
            }

        return {"error": f"Unknown action: {action}"}

    def stop(self):
        self._running = False
        if self._server:
            self._server.close()
        if os.path.exists(self.socket_path):
            os.unlink(self.socket_path)
