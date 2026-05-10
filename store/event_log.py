"""
Append-only event log in JSONL format.
Independent of SQLite — survives database corruption.
Cross-process safe via fcntl file locking + fsync.
"""
import json
import os
import threading
from datetime import datetime
from enum import Enum

try:
    import fcntl
    HAS_FCNTL = True
except ImportError:
    HAS_FCNTL = False  # Windows


class EventType(Enum):
    # Lifecycle
    ENGINE_START = "engine.start"
    ENGINE_STOP = "engine.stop"
    RISK_SERVER_START = "risk.start"
    WATCHDOG_START = "watchdog.start"

    # Orders
    SIGNAL_GENERATED = "signal.generated"
    RISK_APPROVED = "risk.approved"
    RISK_REJECTED = "risk.rejected"
    ORDER_SUBMITTED = "order.submitted"
    ORDER_FILLED = "order.filled"
    ORDER_PARTIAL = "order.partial_fill"
    ORDER_REJECTED = "order.rejected"
    ORDER_FAILED = "order.failed"
    ORDER_UNKNOWN = "order.unknown"
    ORDER_CANCELLED = "order.cancelled"

    # Risk / Safety
    KILL_SWITCH_ON = "kill_switch.activated"
    KILL_SWITCH_OFF = "kill_switch.deactivated"
    STOP_LOSS_BREACH = "stop_loss.breach"
    DAILY_LOSS_BREACH = "daily_loss.breach"

    # Reconciliation
    POSITION_CORRECTED = "reconciliation.position_corrected"
    UNKNOWN_RESOLVED = "reconciliation.unknown_resolved"

    # System
    HEARTBEAT = "heartbeat"
    BROKER_CONNECTED = "broker.connected"
    BROKER_ERROR = "broker.error"
    CIRCUIT_BREAKER_OPEN = "broker.circuit_breaker_open"
    CIRCUIT_BREAKER_CLOSE = "broker.circuit_breaker_close"

    # Emergency execution (new — post-mortem fix)
    EMERGENCY_SELL_INITIATED = "emergency.sell.initiated"
    EMERGENCY_SELL_PLACED = "emergency.sell.placed"
    EMERGENCY_SELL_FAILED = "emergency.sell.failed"

    # Continuous monitoring (new — post-mortem fix)
    CONTINUOUS_LOSS_BREACH = "continuous.loss.breach"

    # Price validation (new — post-mortem fix)
    PRICE_VALIDATION_FAILED = "price.validation.failed"


class EventLog:
    """Thread-safe, cross-process-safe append-only event log."""

    def __init__(self, filepath: str = "events.jsonl"):
        self.filepath = filepath
        self._lock = threading.Lock()

    def emit(self, event_type: EventType, data: dict = None):
        """Write one event. Atomic via file lock + fsync."""
        event = {
            "ts": datetime.now().isoformat(),
            "type": event_type.value,
            "data": data or {},
        }
        line = json.dumps(event, default=str) + "\n"

        with self._lock:
            with open(self.filepath, 'a') as f:
                if HAS_FCNTL:
                    fcntl.flock(f.fileno(), fcntl.LOCK_EX)
                f.write(line)
                f.flush()
                os.fsync(f.fileno())
                if HAS_FCNTL:
                    fcntl.flock(f.fileno(), fcntl.LOCK_UN)

    def tail(self, n: int = 50) -> list:
        if not os.path.exists(self.filepath):
            return []
        with open(self.filepath, 'r') as f:
            lines = f.readlines()
        events = []
        for line in lines[-n:]:
            try:
                events.append(json.loads(line.strip()))
            except json.JSONDecodeError:
                continue
        return events

    def query(self, event_type: str = None, since: str = None,
              symbol: str = None) -> list:
        if not os.path.exists(self.filepath):
            return []
        results = []
        with open(self.filepath, 'r') as f:
            for line in f:
                try:
                    event = json.loads(line.strip())
                except json.JSONDecodeError:
                    continue
                if event_type and event.get("type") != event_type:
                    continue
                if since and event.get("ts", "") < since:
                    continue
                if symbol and event.get("data", {}).get("symbol") != symbol:
                    continue
                results.append(event)
        return results

    def count_today(self, event_type: str) -> int:
        today = datetime.now().strftime("%Y-%m-%d")
        count = 0
        if not os.path.exists(self.filepath):
            return 0
        with open(self.filepath, 'r') as f:
            for line in f:
                try:
                    event = json.loads(line.strip())
                    if (event.get("type") == event_type and
                            event.get("ts", "").startswith(today)):
                        count += 1
                except json.JSONDecodeError:
                    continue
        return count
