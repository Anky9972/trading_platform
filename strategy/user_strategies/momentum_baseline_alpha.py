#!/usr/bin/env python3
"""
================================================================================
STRATEGY: MOMENTUM BASELINE (SMA FILTER)
================================================================================

[ FEYNMAN EXPLANATION ]
This is the "Old Reliable" strategy. It follows the trend. If a stock is moving
up faster than it was a month ago (Momentum) AND it's staying above its average
price (SMA), we buy it. We assume that things in motion tend to stay in motion.

[ TECHNICAL DETAILS ]
1. Calculate 20-day returns (Momentum).
2. Filter for stocks trading above their 20-day SMA.
3. Select Top 3 by momentum and distribute capital equally.
================================================================================
"""

import sys
import json
import logging
from typing import Dict, Any

try:
    import yfinance as yf
    import pandas as pd
    import numpy as np
except ImportError:
    print(json.dumps({"error": "Missing dependencies: pip install yfinance pandas numpy"}))
    sys.exit(1)

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')


# ==============================================================================
# HELPER
# ==============================================================================

def _s(x):
    """Guarantee pd.Series from possibly-MultiIndex DataFrame column."""
    return x.iloc[:, 0] if isinstance(x, pd.DataFrame) else x


# ==============================================================================
# BACKTEST INTERFACE (called by alpha_pipeline.py)
# ==============================================================================

def backtest_signal(data, current_date, fundamentals=None, strategy_history=None):
    """
    Momentum Baseline: buy top-3 stocks by 20-day momentum that are
    trading above their 20-day SMA.

    Parameters
    ----------
    data : dict[str, pd.DataFrame]
        {symbol: OHLCV DataFrame} sliced up to current_date
    current_date : datetime-like
        Current backtest date

    Returns
    -------
    dict[str, float]
        {symbol: weight} — top-3 momentum picks, equal-weighted
    """
    if not data:
        return {}

    candidates = []

    for sym, df in data.items():
        try:
            close = _s(df['Close'])
            if len(close) < 21:
                continue

            p_now = float(close.iloc[-2])
            p_old = float(close.iloc[-20])

            if p_old <= 0 or p_now <= 0:
                continue

            mom = (p_now - p_old) / p_old
            sma20 = float(close.rolling(20).mean().iloc[-2])

            if pd.isna(sma20) or sma20 <= 0:
                continue

            # Filter: price above 20-day SMA
            if p_now > sma20:
                candidates.append({
                    "symbol": sym,
                    "mom": mom,
                })

        except Exception:
            continue

    if not candidates:
        return {}

    # Sort by momentum descending, take top 3
    candidates.sort(key=lambda x: x['mom'], reverse=True)
    top_picks = candidates[:3]

    # Equal-weight
    w = 1.0 / len(top_picks)
    return {p['symbol']: w for p in top_picks}


# ==============================================================================
# LIVE EXECUTION (called by engine.py via subprocess — UNCHANGED)
# ==============================================================================

def run_strategy(context: Dict[str, Any]):
    capital = context.get('capital', 0)
    positions = {p['symbol']: p for p in context.get('positions', [])}

    universe = [
        "RELIANCE.NS", "TCS.NS", "INFY.NS", "HDFCBANK.NS", "ICICIBANK.NS",
        "TATASTEEL.NS", "ITC.NS", "LT.NS", "SBIN.NS", "BHARTIARTL.NS"
    ]

    if capital < 10000:
        return

    candidates = []

    for symbol in universe:
        if symbol in positions:
            continue

        try:
            df = yf.download(symbol, period="60d", interval="1d", progress=False)
            if len(df) < 20:
                continue

            p_now = df['Close'].iloc[-1]
            p_old = df['Close'].iloc[-20]
            mom = (p_now - p_old) / p_old
            sma20 = df['Close'].rolling(20).mean().iloc[-1]

            if p_now > sma20:
                candidates.append({
                    "symbol": symbol,
                    "mom": mom,
                    "price": p_now
                })

        except Exception as e:
            logging.error(f"Error processing {symbol}: {e}")
            continue

    if not candidates:
        return

    candidates.sort(key=lambda x: x['mom'], reverse=True)
    top_picks = candidates[:3]

    allocation_per_stock = (capital * 0.95) / len(top_picks)

    for pick in top_picks:
        sym = pick['symbol']
        price = pick['price']
        qty = int(allocation_per_stock // price)
        if qty > 0:
            base_symbol = sym.replace(".NS", "")
            signal = {
                "symbol": base_symbol,
                "action": "BUY",
                "quantity": qty,
                "order_type": "MARKET",
                "reason": f"Momentum Baseline: 20D Mom={pick['mom']:.1%}"
            }
            print(json.dumps(signal))


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