"""
Kill switch — the last line of defense.
Two enforcement mechanisms:

1. COOPERATIVE (file check):
   Engine checks kill switch before every order cycle.
   If active, engine stops placing new orders.
   This is "cooperative" — the engine chooses to obey.

2. NON-COOPERATIVE (OS signal):
   Watchdog sends SIGTERM (graceful) then SIGKILL (force) via PID file.
   This works even if the engine is hung in an infinite loop.
   Engine does not need to cooperate.

File-based persistence:
   Kill switch state survives process restarts.
   A file (.kill_switch) is the source of truth.
   This means: restarting the engine does NOT clear the kill switch.
   Manual intervention required: rm .kill_switch && python trade.py unkill
"""
import os
import json
import signal
import logging
from datetime import datetime
from config.settings import SETTINGS

logger = logging.getLogger(__name__)


class KillSwitch:
    def __init__(self, db=None):
        self.db = db
        self._ks_file = SETTINGS.KILL_SWITCH_FILE

    def is_active(self) -> bool:
        """Check if kill switch is currently active."""
        return os.path.exists(self._ks_file)

    @property
    def status(self) -> dict:
        if not self.is_active():
            return {"active": False, "reason": None, "activated_at": None}
        try:
            with open(self._ks_file) as f:
                data = json.load(f)
            return {"active": True, **data}
        except (json.JSONDecodeError, IOError):
            return {"active": True, "reason": "Unknown (file exists)",
                    "activated_at": None}

    def activate(self, reason: str):
        """
        Activate kill switch. Creates persistent file.
        This is the ONLY write operation. Must be atomic.
        """
        data = {
            "reason": reason,
            "activated_at": datetime.now().isoformat(),
            "pid": os.getpid(),
        }

        # Write atomically via temp file
        tmp = self._ks_file + ".tmp"
        with open(tmp, 'w') as f:
            json.dump(data, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self._ks_file)

        logger.critical(f"KILL SWITCH ACTIVATED: {reason}")

        # Log to DB if available
        if self.db:
            self.db.log_risk_event("KILL_SWITCH_ON", reason)

        # Log to event log
        try:
            from store.event_log import EventLog, EventType
            events = EventLog()
            events.emit(EventType.KILL_SWITCH_ON, {"reason": reason})
        except Exception:
            pass

    def deactivate(self) -> bool:
        """
        Deactivate kill switch.
        REQUIRES: kill switch file must be manually removed first.
        This prevents accidental deactivation.
        """
        if os.path.exists(self._ks_file):
            logger.warning(f"Cannot deactivate: file {self._ks_file} still exists. "
                          f"Remove manually first: rm {self._ks_file}")
            return False

        logger.info("Kill switch deactivated")

        if self.db:
            self.db.log_risk_event("KILL_SWITCH_OFF", "Manual deactivation")

        try:
            from store.event_log import EventLog, EventType
            events = EventLog()
            events.emit(EventType.KILL_SWITCH_OFF, {})
        except Exception:
            pass

        return True

    def kill_engine_process(self):
        """
        Non-cooperative kill: send SIGTERM then SIGKILL to engine process.
        Called by Watchdog when engine is hung.
        """
        pid_file = SETTINGS.PID_FILE
        if not os.path.exists(pid_file):
            logger.warning("No PID file found — cannot kill engine")
            return

        try:
            with open(pid_file) as f:
                pid = int(f.read().strip())

            logger.warning(f"Sending SIGTERM to engine PID {pid}")
            os.kill(pid, signal.SIGTERM)

            # Wait 5 seconds for graceful shutdown
            import time
            time.sleep(5)

            # Force kill if still running
            try:
                os.kill(pid, 0)  # Check if still alive
                logger.critical(f"Engine still alive. Sending SIGKILL to PID {pid}")
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                logger.info("Engine terminated gracefully")

        except (ValueError, ProcessLookupError) as e:
            logger.warning(f"Kill attempt failed: {e}")
        except Exception as e:
            logger.error(f"Unexpected error killing engine: {e}")
