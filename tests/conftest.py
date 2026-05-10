"""
Shared fixtures for all tests.
Every test uses the paper broker and an in-memory (temp file) database.
No test ever touches a real broker or the production database.
"""
import pytest
import tempfile
import os
import sys

# Ensure project root is in path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Set paper mode before any imports that load settings
os.environ.setdefault("TRADING_MODE", "paper")
os.environ.setdefault("MAX_CAPITAL", "500000")
os.environ.setdefault("ANGEL_API_KEY", "test_key")
os.environ.setdefault("ANGEL_CLIENT_ID", "test_client")
os.environ.setdefault("ANGEL_PASSWORD", "test_pass")
os.environ.setdefault("ANGEL_TOTP_SECRET", "JBSWY3DPEHPK3PXP")  # Valid base32

from store.database import Database
from store.event_log import EventLog
from store.price_cache import PriceCache
from broker.paper import PaperBroker
from risk.kill_switch import KillSwitch
from risk.client import RiskClient
from core.reconciliation import Reconciler
from core.order_manager import OrderManager
from core.models import Signal, Action, OrderType


@pytest.fixture
def tmp_db(tmp_path):
    """Fresh database in a temp directory for each test."""
    db_path = str(tmp_path / "test_trading.db")
    db = Database(db_path)
    return db


@pytest.fixture
def tmp_events(tmp_path):
    """Fresh event log in a temp directory for each test."""
    log_path = str(tmp_path / "test_events.jsonl")
    return EventLog(filepath=log_path)


@pytest.fixture
def paper_broker():
    """Connected paper broker with known prices."""
    broker = PaperBroker()
    broker.connect()
    broker.set_price("PERSISTENT", 5200.0)
    broker.set_price("DIXON", 4800.0)
    broker.set_price("COFORGE", 6000.0)
    broker.set_price("YESBANK", 22.0)
    return broker


@pytest.fixture
def kill_switch(tmp_path, tmp_db):
    """Kill switch wired to temp DB with temp kill switch file."""
    ks_file = str(tmp_path / ".kill_switch")
    # Temporarily override the setting
    from config.settings import SETTINGS
    object.__setattr__(SETTINGS, 'KILL_SWITCH_FILE', ks_file)
    ks = KillSwitch(tmp_db)
    yield ks
    # Cleanup
    if os.path.exists(ks_file):
        os.unlink(ks_file)


@pytest.fixture
def buy_signal():
    return Signal(
        symbol="PERSISTENT",
        action=Action.BUY,
        quantity=10,
        order_type=OrderType.MARKET,
        strategy_id="test",
        reason="test buy",
    )


@pytest.fixture
def sell_signal(tmp_db):
    """A sell signal — requires a position to exist first."""
    return Signal(
        symbol="PERSISTENT",
        action=Action.SELL,
        quantity=10,
        order_type=OrderType.MARKET,
        strategy_id="test",
        reason="test sell",
    )
