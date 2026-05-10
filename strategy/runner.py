"""
Strategy runner — subprocess isolation for user strategies.

Strategies run in isolated subprocesses with:
  - No broker credentials (env stripped)
  - Fake HOME directory (sandbox)
  - Restricted PATH
  - 30-second timeout (configurable)
  - Resource limits on Linux (256MB RAM, no forking, 50 FDs)

Input: JSON context on stdin
Output: JSON signals on stdout (one per line)
"""
import os
import sys
import json
import shutil
import tempfile
import subprocess
import logging
import re
from typing import List
from core.models import Signal, Action, OrderType

logger = logging.getLogger(__name__)

# Files allowed to be copied into strategy sandbox
ALLOWED_DATA_FILES = [
    "upcoming_earnings.json",
    "earnings_calendar.json",
]


class StrategyRunner:
    def __init__(self, strategy_path: str, strategy_id: str,
                 timeout_seconds: int = 30):
        self.strategy_path = os.path.abspath(strategy_path)
        self.strategy_id = strategy_id
        self.timeout = timeout_seconds
        self._sandbox_dir = None

    def run(self, context: dict) -> List[Signal]:
        """
        Execute strategy in subprocess.
        Returns list of validated Signal objects.
        """
        # Create sandbox
        self._sandbox_dir = tempfile.mkdtemp(prefix=f"strat_{self.strategy_id}_")

        try:
            # Copy allowed data files to sandbox
            strategy_dir = os.path.dirname(self.strategy_path)
            for filename in ALLOWED_DATA_FILES:
                src = os.path.join(strategy_dir, filename)
                if os.path.exists(src):
                    shutil.copy2(src, self._sandbox_dir)

            # Build restricted environment
            env = {
                "PATH": "/usr/bin:/usr/local/bin",
                "HOME": self._sandbox_dir,
                "STRATEGY_ID": self.strategy_id,
                "PYTHONPATH": strategy_dir,
            }

            # Add Python path for Windows compatibility
            if sys.platform == 'win32':
                env["PATH"] = os.environ.get("PATH", "")
                env["SYSTEMROOT"] = os.environ.get("SYSTEMROOT", "")

            # Build command with resource limits
            cmd = [sys.executable]

            # On Linux/Mac, add resource limits via preexec_fn
            preexec = None
            if sys.platform != 'win32':
                preexec = self._apply_resource_limits

            cmd.append(self.strategy_path)

            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
                cwd=self._sandbox_dir,
                preexec_fn=preexec,
            )

            # Send context via stdin
            context_json = json.dumps(context) + "\n"
            try:
                stdout, stderr = proc.communicate(
                    input=context_json.encode(),
                    timeout=self.timeout,
                )
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
                logger.warning(f"Strategy {self.strategy_id} timed out "
                             f"({self.timeout}s)")
                return []

            if proc.returncode != 0:
                stderr_text = stderr.decode()[:500] if stderr else ""
                logger.warning(f"Strategy {self.strategy_id} exited with "
                             f"code {proc.returncode}: {stderr_text}")
                return []

            # Parse output signals
            signals = []
            for line in stdout.decode().strip().split("\n"):
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                    signal = self._validate_signal(data)
                    if signal:
                        signals.append(signal)
                except json.JSONDecodeError:
                    logger.debug(f"Non-JSON output from {self.strategy_id}: {line[:100]}")
                    continue

            return signals

        finally:
            # Cleanup sandbox
            try:
                if self._sandbox_dir and os.path.exists(self._sandbox_dir):
                    shutil.rmtree(self._sandbox_dir, ignore_errors=True)
            except Exception:
                pass

    def _validate_signal(self, data: dict) -> Signal:
        """Validate and convert a JSON dict to a Signal object."""
        symbol = data.get("symbol", "")
        action_str = data.get("action", "")
        quantity = data.get("quantity", 0)

        # Symbol validation: uppercase alpha only
        if not symbol or not re.match(r'^[A-Z]+$', symbol):
            logger.warning(f"Invalid symbol: {symbol}")
            return None

        # Action validation
        if action_str.upper() not in ("BUY", "SELL"):
            logger.warning(f"Invalid action: {action_str}")
            return None

        # Quantity validation
        if not isinstance(quantity, int) or quantity <= 0:
            logger.warning(f"Invalid quantity: {quantity}")
            return None

        # Build signal
        try:
            order_type = OrderType.LIMIT if data.get("order_type") == "LIMIT" else OrderType.MARKET
            return Signal(
                symbol=symbol,
                action=Action[action_str.upper()],
                quantity=quantity,
                order_type=order_type,
                price=data.get("price"),
                strategy_id=self.strategy_id,
                reason=data.get("reason", ""),
                idempotency_key=data.get("idempotency_key", ""),
            )
        except Exception as e:
            logger.warning(f"Signal construction failed: {e}")
            return None

    def _apply_resource_limits(self):
        """Apply resource limits in subprocess (Linux/macOS only)."""
        try:
            import resource
            # 256MB RAM
            resource.setrlimit(resource.RLIMIT_AS, (256 * 1024 * 1024, 256 * 1024 * 1024))
            # No forking
            resource.setrlimit(resource.RLIMIT_NPROC, (1, 1))
            # 50 file descriptors
            resource.setrlimit(resource.RLIMIT_NOFILE, (50, 50))
        except (ImportError, ValueError, resource.error):
            pass  # Not available on this platform

    def cleanup(self):
        """Cleanup any remaining sandbox directories."""
        if self._sandbox_dir and os.path.exists(self._sandbox_dir):
            shutil.rmtree(self._sandbox_dir, ignore_errors=True)
