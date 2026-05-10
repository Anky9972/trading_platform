"""
Risk client — used by the Trading Engine to communicate with the Risk Server.

FAIL-CLOSED: If the Risk Server is unreachable, ALL orders are rejected.
This is a hard guarantee. The engine cannot bypass this.

The client sends signals to the Risk Server via Unix socket.
The Risk Server returns APPROVED or REJECTED with reasons.
"""
import socket
import json
import logging
from config.settings import SETTINGS

logger = logging.getLogger(__name__)


class RiskClient:
    def __init__(self, socket_path: str = None):
        self.socket_path = socket_path or SETTINGS.RISK_SOCKET_PATH
        self._timeout = 5.0  # 5 second timeout for risk check

    def check(self, signal_data: dict) -> dict:
        """
        Send signal to Risk Server for approval.
        Returns dict with verdict, passed, failed, reasons.

        FAIL-CLOSED: Returns REJECTED if Risk Server is unreachable.
        """
        try:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.settimeout(self._timeout)
            sock.connect(self.socket_path)

            request = json.dumps({"action": "check", "signal": signal_data})
            sock.sendall(request.encode())
            response = sock.recv(65536).decode()
            sock.close()

            return json.loads(response)

        except (ConnectionRefusedError, FileNotFoundError) as e:
            logger.critical(f"FAIL-CLOSED: Risk Server unreachable: {e}")
            return {
                "verdict": "REJECTED",
                "passed": [],
                "failed": ["RISK_SERVER_UNAVAILABLE"],
                "reasons": [f"RISK_SERVER_UNAVAILABLE: Cannot reach Risk Server — {e}"],
            }

        except socket.timeout:
            logger.critical("FAIL-CLOSED: Risk Server timed out")
            return {
                "verdict": "REJECTED",
                "passed": [],
                "failed": ["RISK_SERVER_TIMEOUT"],
                "reasons": ["RISK_SERVER_TIMEOUT: Risk check timed out"],
            }

        except Exception as e:
            logger.critical(f"FAIL-CLOSED: Risk check error: {e}")
            return {
                "verdict": "REJECTED",
                "passed": [],
                "failed": ["RISK_ERROR"],
                "reasons": [f"RISK_ERROR: {e}"],
            }

    def update_margin(self, margin: float):
        """Update available margin in Risk Server."""
        try:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.settimeout(2.0)
            sock.connect(self.socket_path)
            request = json.dumps({"action": "update_margin", "margin": margin})
            sock.sendall(request.encode())
            sock.recv(1024)
            sock.close()
        except Exception as e:
            logger.warning(f"Could not update margin in Risk Server: {e}")
