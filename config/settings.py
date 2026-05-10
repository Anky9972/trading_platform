"""
Configuration management.
ALL credentials from environment variables. NEVER from Python files.
Frozen dataclass — immutable after creation. No runtime mutation.

Setup (add to ~/.bashrc or ~/.zshrc):
    export ANGEL_API_KEY="your_key"
    export ANGEL_CLIENT_ID="your_id"
    export ANGEL_PASSWORD="your_password"
    export ANGEL_TOTP_SECRET="your_totp_secret"
    export TRADING_MODE="paper"
    export MAX_CAPITAL=500000
"""
import os
import sys
from dataclasses import dataclass

@dataclass(frozen=True)
class Settings:
    # Credentials (from env vars)
    ANGEL_API_KEY: str = ""
    ANGEL_CLIENT_ID: str = ""
    ANGEL_PASSWORD: str = ""
    ANGEL_TOTP_SECRET: str = ""

    # Mode
    TRADING_MODE: str = "paper"
    MAX_CAPITAL: int = 500000

    # Paths
    DB_PATH: str = "trading.db"
    KILL_SWITCH_FILE: str = ".kill_switch"
    HEARTBEAT_FILE: str = ".engine_heartbeat"
    PID_FILE: str = ".engine.pid"
    RISK_PID_FILE: str = ".risk.pid"
    WATCHDOG_PID_FILE: str = ".watchdog.pid"
    SOCKET_PATH: str = "/tmp/trading_engine.sock"
    RISK_SOCKET_PATH: str = "/tmp/trading_risk.sock"
    SESSION_FILE: str = ".broker_session"

    # Risk limits — HARDCODED. Change requires code change + review.
    MAX_ORDER_VALUE: int = 100000           # ₹1 lakh per order
    MAX_POSITION_PER_STOCK_PCT: float = 0.10  # 10% of capital per stock
    MAX_TOTAL_EXPOSURE_PCT: float = 0.50    # 50% of capital total
    MAX_DAILY_LOSS_PCT: float = 0.03        # 3% of capital
    MAX_GLOBAL_DRAWDOWN_PCT: float = 0.10   # 10% from peak
    MAX_ORDERS_PER_MINUTE: int = 5
    MAX_ORDERS_PER_DAY: int = 20
    STOP_LOSS_PCT: float = 0.08             # 8% stop loss
    ORDER_TIMEOUT_SECONDS: int = 10
    MAX_RETRIES: int = 2

    # Watchdog / monitoring
    HEARTBEAT_MAX_AGE_SECONDS: int = 120
    PRICE_POLL_INTERVAL_SECONDS: int = 30
    RECONCILIATION_INTERVAL_SECONDS: int = 300
    PRICE_STALENESS_LIMIT_SECONDS: int = 90

    def is_paper(self) -> bool:
        return self.TRADING_MODE == "paper"


def load_settings() -> Settings:
    """Load settings from environment. Fail hard on missing credentials in live mode."""
    mode = os.environ.get("TRADING_MODE", "paper")

    required_for_live = [
        "ANGEL_API_KEY", "ANGEL_CLIENT_ID",
        "ANGEL_PASSWORD", "ANGEL_TOTP_SECRET"
    ]

    if mode != "paper":
        missing = [k for k in required_for_live if not os.environ.get(k)]
        if missing:
            print(f"FATAL: Missing environment variables for live mode: {', '.join(missing)}")
            print(f"Set TRADING_MODE=paper to run without credentials.")
            sys.exit(1)

    return Settings(
        ANGEL_API_KEY=os.environ.get("ANGEL_API_KEY", ""),
        ANGEL_CLIENT_ID=os.environ.get("ANGEL_CLIENT_ID", ""),
        ANGEL_PASSWORD=os.environ.get("ANGEL_PASSWORD", ""),
        ANGEL_TOTP_SECRET=os.environ.get("ANGEL_TOTP_SECRET", ""),
        TRADING_MODE=mode,
        MAX_CAPITAL=int(os.environ.get("MAX_CAPITAL", "500000")),
        DB_PATH=os.environ.get("TRADING_DB", "trading.db"),
        KILL_SWITCH_FILE=os.environ.get("KILL_SWITCH_FILE", ".kill_switch"),
        SESSION_FILE=os.environ.get("SESSION_FILE", ".broker_session"),
    )


# Singleton — loaded once, used everywhere
SETTINGS = load_settings()
