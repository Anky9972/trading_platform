#!/usr/bin/env python3
"""
================================================================================
STRATEGY: ADAPTIVE HALF-KELLY WITH TREND FILTER & EXIT LOGIC
================================================================================

[ FEYNMAN EXPLANATION ]
We keep the core idea: buy stocks above their 200-day average (trend) and size bets
using the Kelly Criterion. But now we learn from experience – every time we win or
lose, we update our estimates of win rate and reward/risk. That way the system
adapts to changing market conditions. We also sell when the trend dies or when a
stock drops too much (stop loss). And we make sure we don't bet all our money on
one idea – we spread it evenly among the best signals.

[ TECHNICAL DETAILS ]
- Computes rolling win rate and avg win/loss from recent price data as Kelly inputs.
- For each symbol, computes 200-day SMA and current price.
- Generates BUY weights for symbols above SMA.
- Uses adaptive Kelly fraction to determine total allocation.
- Allocates total risk budget equally across all new buys.

BACKTEST MODE:
- Uses price-derived Kelly estimates (rolling 20d returns as trade proxies).
- No file I/O, no stdin, works purely on pre-loaded data dict.

LIVE MODE:
- Original run_strategy() with file-based KellyTracker, unchanged.
================================================================================
"""

import sys
import json
import logging
import os
from typing import Dict, Any, List, Optional
from collections import deque
import datetime

import yfinance as yf
import pandas as pd
import numpy as np

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')


# ==============================================================================
# HELPER
# ==============================================================================

def _s(x):
    """Guarantee pd.Series from possibly-MultiIndex DataFrame column."""
    return x.iloc[:, 0] if isinstance(x, pd.DataFrame) else x


# ==============================================================================
# KELLY MATH (pure functions, no file I/O)
# ==============================================================================

def _estimate_kelly_from_prices(close: pd.Series, lookback: int = 50) -> float:
    """
    Estimate half-Kelly fraction from recent price returns.
    Uses rolling 5-day returns as "trade" proxies.
    Returns fraction in [0, 0.125] (half of quarter-Kelly cap).
    """
    if len(close) < lookback + 5:
        return 0.02  # conservative default

    tail = close.iloc[-(lookback + 5):]
    rets = tail.pct_change(5).dropna().values

    if len(rets) < 10:
        return 0.02

    wins = rets[rets > 0]
    losses = np.abs(rets[rets < 0])

    if len(wins) == 0 or len(losses) == 0:
        return 0.02

    win_rate = len(wins) / len(rets)
    avg_win = float(np.mean(wins))
    avg_loss = float(np.mean(losses))

    if avg_loss < 1e-8:
        return 0.02

    b = avg_win / avg_loss
    kelly = (win_rate * b - (1 - win_rate)) / b if b > 0 else 0.0

    # Half-Kelly, capped
    half_kelly = kelly * 0.5
    return max(0.0, min(half_kelly, 0.125))


# ==============================================================================
# BACKTEST INTERFACE (called by alpha_pipeline.py)
# ==============================================================================

def backtest_signal(data, current_date, fundamentals=None, strategy_history=None):
    """
    Adaptive Half-Kelly with SMA trend filter.

    Parameters
    ----------
    data : dict[str, pd.DataFrame]
        {symbol: OHLCV DataFrame} sliced up to current_date
    current_date : datetime-like
        Current backtest date

    Returns
    -------
    dict[str, float]
        {symbol: weight}
    """
    if not data:
        return {}

    SMA_PERIOD = 200
    MAX_POSITIONS = 10

    buy_candidates = []

    for sym, df in data.items():
        try:
            close = _s(df['Close'])

            if len(close) < SMA_PERIOD + 1:
                continue

            current_price = float(close.iloc[-1])
            sma = float(close.rolling(window=SMA_PERIOD).mean().iloc[-1])

            if pd.isna(sma) or sma <= 0 or current_price <= 0:
                continue

            # BUY condition: price above 200-day SMA
            if current_price > sma:
                # Signal strength: distance from SMA
                strength = (current_price - sma) / sma
                buy_candidates.append((sym, strength, close))

        except Exception:
            continue

    if not buy_candidates:
        return {}

    # Sort by strength, take top MAX_POSITIONS
    buy_candidates.sort(key=lambda x: x[1], reverse=True)
    top = buy_candidates[:MAX_POSITIONS]

    # Estimate Kelly from the equal-weight portfolio's recent returns
    # Use average Kelly across top candidates
    kelly_estimates = []
    for sym, strength, close in top:
        k = _estimate_kelly_from_prices(close, lookback=50)
        kelly_estimates.append(k)

    avg_kelly = float(np.mean(kelly_estimates)) if kelly_estimates else 0.02

    if avg_kelly <= 0.001:
        return {}

    # Total allocation = kelly fraction, split equally among picks
    # Each position gets kelly / n_positions of total capital
    n = len(top)
    per_position_weight = avg_kelly / n

    # Cap each position at 15%
    per_position_weight = min(per_position_weight, 0.15)

    allocations = {}
    for sym, strength, close in top:
        allocations[sym] = per_position_weight

    # Normalize so total <= 1.0
    total = sum(allocations.values())
    if total > 1.0:
        allocations = {s: w / total for s, w in allocations.items()}

    return allocations


# ==============================================================================
# KELLY TRACKER (file-based, for LIVE execution only)
# ==============================================================================

class KellyTracker:
    def __init__(self, filepath: str = "kelly_tracker.json", maxlen: int = 50):
        self.filepath = filepath
        self.maxlen = maxlen
        self.trades = deque(maxlen=maxlen)
        self.win_rate = 0.55
        self.avg_win = 0.02
        self.avg_loss = 0.015
        self.load()

    def load(self):
        if os.path.exists(self.filepath):
            try:
                with open(self.filepath, 'r') as f:
                    data = json.load(f)
                    self.trades = deque(data.get('trades', []), maxlen=self.maxlen)
                    self.win_rate = data.get('win_rate', 0.55)
                    self.avg_win = data.get('avg_win', 0.02)
                    self.avg_loss = data.get('avg_loss', 0.015)
            except Exception as e:
                logging.warning(f"Could not load Kelly tracker: {e}")

    def save(self):
        try:
            with open(self.filepath, 'w') as f:
                json.dump({
                    'trades': list(self.trades),
                    'win_rate': self.win_rate,
                    'avg_win': self.avg_win,
                    'avg_loss': self.avg_loss,
                }, f, indent=2)
        except Exception as e:
            logging.warning(f"Could not save Kelly tracker: {e}")

    def add_trade(self, pnl_pct: float):
        self.trades.append(pnl_pct)
        self._recompute()
        self.save()

    def _recompute(self):
        if len(self.trades) < 10:
            return
        wins = [t for t in self.trades if t > 0]
        losses = [abs(t) for t in self.trades if t < 0]
        self.win_rate = len(wins) / len(self.trades) if self.trades else 0.5
        self.avg_win = np.mean(wins) if wins else 0.02
        self.avg_loss = np.mean(losses) if losses else 0.015

    def kelly_fraction(self) -> float:
        if self.avg_loss == 0:
            return 0.0
        b = self.avg_win / self.avg_loss
        p = self.win_rate
        q = 1 - p
        kelly = (p * b - q) / b if b != 0 else 0.0
        return max(0.0, min(0.25, kelly))


# ==============================================================================
# UNIVERSE
# ==============================================================================

UNIVERSE = [
    "RELIANCE.NS", "TCS.NS", "HDFCBANK.NS", "INFY.NS", "ICICIBANK.NS",
    "HINDUNILVR.NS", "ITC.NS", "SBIN.NS", "BHARTIARTL.NS", "KOTAKBANK.NS",
    "BAJFINANCE.NS", "LT.NS", "WIPRO.NS", "AXISBANK.NS", "TITAN.NS",
    "ASIANPAINT.NS", "MARUTI.NS", "SUNPHARMA.NS", "HCLTECH.NS", "ONGC.NS",
    "NTPC.NS", "POWERGRID.NS", "ULTRACEMCO.NS", "BAJAJFINSV.NS", "ADANIPORTS.NS",
    "M&M.NS", "TATAMOTORS.NS", "TATASTEEL.NS", "JSWSTEEL.NS", "TECHM.NS",
    "INDUSINDBK.NS", "NESTLEIND.NS", "BRITANNIA.NS", "HEROMOTOCO.NS", "EICHERMOT.NS",
    "COALINDIA.NS", "IOC.NS", "BPCL.NS", "HINDALCO.NS", "DIVISLAB.NS",
    "DRREDDY.NS", "CIPLA.NS", "UPL.NS", "SHREECEM.NS", "GRASIM.NS",
    "ADANIENT.NS", "HDFCLIFE.NS", "SBILIFE.NS", "BAJAJ-AUTO.NS", "TATACONSUM.NS",
]


# ==============================================================================
# LIVE EXECUTION (called by engine.py via subprocess — UNCHANGED)
# ==============================================================================

def run_strategy(context: Dict[str, Any]):
    capital = context.get('capital', 0)
    positions = {p['symbol']: p for p in context.get('positions', [])}
    existing_symbols = set(positions.keys())

    tracker = KellyTracker()

    MAX_POSITIONS = 10
    STOP_LOSS_PCT = 0.05
    SMA_PERIOD = 200

    buy_candidates = []

    for symbol in UNIVERSE:
        try:
            df = yf.download(symbol, period="1y", interval="1d", progress=False)
            if df.empty or len(df) < SMA_PERIOD:
                continue

            current_price = df['Close'].iloc[-1]
            sma = df['Close'].rolling(window=SMA_PERIOD).mean().iloc[-1]

            if symbol in existing_symbols:
                entry = positions[symbol].get('avg_price', current_price)
                pnl_pct = (current_price - entry) / entry

                if current_price < sma or pnl_pct < -STOP_LOSS_PCT:
                    tracker.add_trade(pnl_pct)

                    base_symbol = symbol.replace(".NS", "")
                    sell_signal = {
                        "symbol": base_symbol,
                        "action": "SELL",
                        "quantity": positions[symbol]['quantity'],
                        "order_type": "MARKET",
                        "reason": f"Exit: trend_died" if current_price < sma else f"Stop loss ({pnl_pct*100:.1f}%)"
                    }
                    print(json.dumps(sell_signal))
                continue

            if current_price > sma:
                strength = (current_price - sma) / sma
                buy_candidates.append((symbol, strength, current_price))

        except Exception as e:
            logging.error(f"Error processing {symbol}: {e}")
            continue

    buy_candidates.sort(key=lambda x: x[1], reverse=True)

    current_pos_count = len(existing_symbols)
    slots_available = max(0, MAX_POSITIONS - current_pos_count)
    if slots_available <= 0:
        return

    top_candidates = buy_candidates[:slots_available]
    if not top_candidates:
        return

    kelly = tracker.kelly_fraction()
    half_kelly = kelly * 0.5
    if half_kelly <= 0:
        return

    total_risk_budget = capital * half_kelly
    per_position_allocation = total_risk_budget / len(top_candidates)

    for symbol, strength, price in top_candidates:
        allocation = min(per_position_allocation, capital * 0.15)
        quantity = int(allocation // price)
        if quantity <= 0:
            continue

        base_symbol = symbol.replace(".NS", "")
        buy_signal = {
            "symbol": base_symbol,
            "action": "BUY",
            "quantity": quantity,
            "order_type": "MARKET",
            "reason": f"Trend strength {strength*100:.2f}%, Kelly={half_kelly:.2%}"
        }
        print(json.dumps(buy_signal))


if __name__ == "__main__":
    try:
        raw_context = sys.stdin.readline()
        if not raw_context:
            sys.exit(0)
        context = json.loads(raw_context)
        run_strategy(context)
    except Exception as e:
        print(json.dumps({"error": str(e)}))
        sys.exit(1)