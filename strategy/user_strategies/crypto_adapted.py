#!/usr/bin/env python3
"""
CRYPTO PERPETUAL FUTURES STRATEGY — PORTED TO PLATFORM
Adapted from the user's Rank #7 configuration

This strategy incorporates:
1. Alpha Symbol Filter (Empirically selected symbols based on backtesting)
2. Temporal Alpha Filter (Trading allowed only in specific UTC hours)
3. Drawdown Manager (Reduces risk leverage based on peak equity drops)
4. Momentum/7D Return calculation (Using yfinance for continuous crypto pricing)
"""
import sys
import json
import hashlib
import logging
from datetime import datetime, timedelta

try:
    import yfinance as yf
    HAS_YFINANCE = True
except ImportError:
    HAS_YFINANCE = False

import pandas as pd
import numpy as np

logger = logging.getLogger("crypto_strategy")

# ==============================================================================
# CONFIGURATION & FILTERS (from demo.py)
# ==============================================================================

MAX_DRAWDOWN = 0.10
MAX_LEVERAGE = 1.50


class AlphaSymbolFilter:
    ALPHA_SYMBOLS = {
        'DOT-USD':  {'bias': 'short'},
        'DOGE-USD': {'bias': 'long'},
        'BTC-USD':  {'bias': 'neutral'},
        'ETC-USD':  {'bias': 'neutral'},
        'LTC-USD':  {'bias': 'short'},
        'LINK-USD': {'bias': 'long'},
        'TRX-USD':  {'bias': 'long'},
        'BNB-USD':  {'bias': 'neutral'},
        'ATOM-USD': {'bias': 'neutral'},
        'XRP-USD':  {'bias': 'neutral'},
        'ETH-USD':  {'bias': 'neutral'},
        'SOL-USD':  {'bias': 'neutral'}
    }

    BLACKLIST = {'UNI-USD', 'BCH-USD', 'AVAX-USD'}

    @classmethod
    def get_symbols(cls):
        return [s for s in cls.ALPHA_SYMBOLS.keys() if s not in cls.BLACKLIST]


class TemporalAlphaFilter:
    ALPHA_HOURS_UTC = [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12]
    BLACKOUT_HOURS_UTC = list(range(17, 24))

    @classmethod
    def is_trading_allowed(cls, current_time: datetime) -> bool:
        hour_utc = current_time.hour
        if hour_utc in cls.BLACKOUT_HOURS_UTC:
            return False
        if hour_utc in cls.ALPHA_HOURS_UTC:
            return True
        return False


class DrawdownManager:
    def __init__(self, initial_capital: float, current_equity: float):
        self.initial_capital = initial_capital
        self.peak_equity = max(initial_capital, current_equity)

        self.LEVEL_1_WARNING = 0.02
        self.LEVEL_2_REDUCE = 0.04
        self.LEVEL_3_CRITICAL = 0.05
        self.LEVEL_4_EMERGENCY = 0.06
        self.LEVEL_5_ELIMINATION = 0.10

    def get_risk_scale(self, current_equity: float) -> float:
        dd = (self.peak_equity - current_equity) / self.peak_equity if self.peak_equity > 0 else 0.0

        if dd >= self.LEVEL_5_ELIMINATION:
            return 0.0
        elif dd >= self.LEVEL_4_EMERGENCY:
            return 0.0
        elif dd >= self.LEVEL_3_CRITICAL:
            return 0.2
        elif dd >= self.LEVEL_2_REDUCE:
            return 0.5
        elif dd >= self.LEVEL_1_WARNING:
            return 0.8
        return 1.0


# ==============================================================================
# HELPER: safe column access for yfinance MultiIndex
# ==============================================================================

def _s(x):
    """Guarantee pd.Series from a possibly-MultiIndex DataFrame column."""
    return x.iloc[:, 0] if isinstance(x, pd.DataFrame) else x


# ==============================================================================
# BACKTEST INTERFACE  (called by alpha_pipeline.py)
# ==============================================================================

def backtest_signal(data, current_date, fundamentals=None, strategy_history=None):
    """
    Pure-data backtest entry point.

    Works with ANY universe (NSE stocks, crypto, etc.) by computing
    7-day momentum cross-sectionally on whatever data dict is passed in.

    Parameters
    ----------
    data : dict[str, pd.DataFrame]
        {symbol: OHLCV DataFrame} sliced up to current_date
    current_date : datetime-like
        The current backtest date

    Returns
    -------
    dict[str, float]
        {symbol: weight}  — long-only top-momentum picks, equal-weighted
    """
    if not data:
        return {}

    # --- Temporal filter: skip if current_date hour is in blackout ---
    if isinstance(current_date, datetime):
        if not TemporalAlphaFilter.is_trading_allowed(current_date):
            return {}

    # --- Compute 7-day momentum for each symbol in the data ---
    momentum = {}
    for sym, df in data.items():
        try:
            close = _s(df['Close'])
            if len(close) < 7:
                continue

            # 7-day return
            tail = close.iloc[-7:]
            if len(tail) >= 2 and tail.iloc[0] > 0:
                ret_7d = (tail.iloc[-1] - tail.iloc[0]) / tail.iloc[0]
                momentum[sym] = float(ret_7d)
        except Exception:
            continue

    if len(momentum) < 2:
        return {}

    # --- Rank by momentum, go long top half ---
    sorted_mom = sorted(momentum.items(), key=lambda x: x[1], reverse=True)

    # Top third (more selective than top half)
    n_long = max(1, len(sorted_mom) // 3)
    long_picks = sorted_mom[:n_long]

    # Only take positive momentum
    long_picks = [(s, m) for s, m in long_picks if m > 0]

    if not long_picks:
        return {}

    # Equal-weight allocation
    w = 1.0 / len(long_picks)
    return {sym: w for sym, _ in long_picks}


# ==============================================================================
# LIVE EXECUTION (original main — unchanged for engine.py subprocess)
# ==============================================================================

def fetch_momentum(symbols: list) -> dict:
    """Fetch 7D returns using yfinance."""
    if not HAS_YFINANCE:
        return {s: 0.0 for s in symbols}

    returns = {}
    try:
        data = yf.download(symbols, period="7d", progress=False)
        if data.empty or 'Close' not in data:
            return {s: 0.0 for s in symbols}

        close_prices = data['Close']
        for symbol in symbols:
            try:
                prices = close_prices[symbol].dropna()
                if len(prices) >= 2:
                    returns[symbol] = (prices.iloc[-1] - prices.iloc[0]) / prices.iloc[0]
                else:
                    returns[symbol] = 0.0
            except Exception:
                returns[symbol] = 0.0
    except Exception:
        pass
    return returns


def main():
    try:
        raw_input = sys.stdin.readline()
        if not raw_input:
            return
        context = json.loads(raw_input)
    except Exception:
        return

    positions = context.get("positions", [])
    capital = float(context.get("capital", 500000))
    timestamp_str = context.get("timestamp", datetime.now().isoformat())
    current_time = datetime.fromisoformat(timestamp_str)

    if not TemporalAlphaFilter.is_trading_allowed(current_time):
        return

    current_equity = capital
    for p in positions:
        current_equity += (p['quantity'] * p['avg_price'])

    dd_manager = DrawdownManager(initial_capital=capital, current_equity=current_equity)
    risk_factor = dd_manager.get_risk_scale(current_equity)

    if risk_factor <= 0.0:
        return

    symbols = AlphaSymbolFilter.get_symbols()
    momentum = fetch_momentum(symbols)

    if not momentum:
        return

    sorted_momentum = sorted(momentum.items(), key=lambda x: x[1], reverse=True)
    position_switch_count = min(6, len(sorted_momentum) // 2)

    long_symbols = [s for s, _ in sorted_momentum[:position_switch_count]]
    short_symbols = [s for s, _ in sorted_momentum[-position_switch_count:]]

    target_value_per_position = (current_equity * risk_factor * 0.10) / max(position_switch_count, 1)

    signals = []

    for sym in long_symbols:
        idem = hashlib.sha256(
            f"long_{sym}_{current_time.strftime('%Y%m%d%H')}".encode()
        ).hexdigest()[:12]
        signals.append({
            "symbol": sym,
            "action": "BUY",
            "quantity": 1,
            "order_type": "MARKET",
            "reason": f"Crypto Alpha: Long Momentum | Risk: {risk_factor}",
            "idempotency_key": idem
        })

    for sym in short_symbols:
        idem = hashlib.sha256(
            f"short_{sym}_{current_time.strftime('%Y%m%d%H')}".encode()
        ).hexdigest()[:12]
        signals.append({
            "symbol": sym,
            "action": "SELL",
            "quantity": 1,
            "order_type": "MARKET",
            "reason": f"Crypto Alpha: Short Momentum | Risk: {risk_factor}",
            "idempotency_key": idem
        })

    for sig in signals:
        print(json.dumps(sig))


if __name__ == "__main__":
    main()