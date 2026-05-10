#!/usr/bin/env python3
"""
================================================================================
STRATEGY: CUSTOM BETA ALPHA (BETA-ADJUSTED MEAN REVERSION)
================================================================================

[ FEYNMAN EXPLANATION ]
Some stocks are wild (High Beta) and jump around a lot. Others are calm (Low Beta).
This strategy looks for a specific "rubber band" effect. When a stock price
stretches too far away from its recent 5-day average, we expect it to snap back.
We use the stock's "Beta" to decide how much to bet – if it's a wild stock,
we size our bet more carefully to avoid getting shaken out.

[ TECHNICAL DETAILS ]
1. Calculate 5-day SMA.
2. Signal: Price < 5-day SMA * 0.98 (Oversold).
3. Sizing: Base weight (10%) scaled by (1/Beta) for high-beta stocks.
   Beta is estimated from 60-day rolling correlation with equal-weight index.
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


def _estimate_beta(close: pd.Series, index_returns: pd.Series, window: int = 60) -> float:
    """
    Estimate beta against the equal-weight index using trailing returns.
    Falls back to 1.0 if insufficient data.
    """
    if len(close) < window + 2:
        return 1.0

    stock_ret = close.pct_change().iloc[-window:]
    idx_ret = index_returns.iloc[-window:]

    # Align
    aligned = pd.concat([stock_ret, idx_ret], axis=1).dropna()
    if len(aligned) < 20:
        return 1.0

    cov = aligned.iloc[:, 0].cov(aligned.iloc[:, 1])
    var = aligned.iloc[:, 1].var()

    if var < 1e-10:
        return 1.0

    return max(0.2, min(cov / var, 3.0))  # clamp to [0.2, 3.0]


# ==============================================================================
# BACKTEST INTERFACE (called by alpha_pipeline.py)
# ==============================================================================

def backtest_signal(data, current_date, fundamentals=None, strategy_history=None):
    """
    Beta-Adjusted Mean Reversion signal using pre-loaded data.

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
    if not data or len(data) < 2:
        return {}

    # --- Build equal-weight index returns for beta estimation ---
    all_returns = {}
    close_map = {}
    for sym, df in data.items():
        try:
            close = _s(df['Close'])
            if len(close) < 10:
                continue
            close_map[sym] = close
            all_returns[sym] = close.pct_change()
        except Exception:
            continue

    if len(all_returns) < 2:
        return {}

    ret_df = pd.DataFrame(all_returns)
    index_returns = ret_df.mean(axis=1)  # equal-weight index

    # --- Scan for oversold stocks with beta-adjusted sizing ---
    candidates = {}

    for sym, close in close_map.items():
        if len(close) < 6:
            continue

        p_now = float(close.iloc[-2])
        p_avg = float(close.rolling(5).mean().iloc[-2])

        if pd.isna(p_avg) or p_avg <= 0 or p_now <= 0:
            continue

        # Oversold condition: price < 5-day SMA * 0.98
        if p_now < p_avg * 0.98:
            beta = _estimate_beta(close, index_returns, window=60)

            # Sizing: base 10% scaled by 1/beta, capped at 15%
            weight = 0.10
            if beta > 0.5:
                weight *= (1.0 / beta)
            weight = min(weight, 0.15)

            candidates[sym] = weight

    if not candidates:
        return {}

    # Normalize weights to sum to 1.0
    total = sum(candidates.values())
    if total <= 0:
        return {}

    return {sym: w / total for sym, w in candidates.items()}


# ==============================================================================
# LIVE EXECUTION (called by engine.py via subprocess — UNCHANGED)
# ==============================================================================

def run_strategy(context: Dict[str, Any]):
    capital = context.get('capital', 0)
    positions = {p['symbol']: p for p in context.get('positions', [])}

    universe = ["RELIANCE.NS", "TCS.NS", "INFY.NS", "HDFCBANK.NS", "ICICIBANK.NS"]

    if capital < 10000:
        return

    for symbol in universe:
        if symbol in positions:
            continue

        try:
            ticker = yf.Ticker(symbol)
            df = ticker.history(period="10d")
            if len(df) < 5:
                continue

            p_now = df['Close'].iloc[-1]
            p_avg = df['Close'].rolling(5).mean().iloc[-1]

            if p_now < p_avg * 0.98:
                info = ticker.info
                beta = info.get("beta", 1.0)

                weight = 0.10
                if beta > 0.5:
                    weight *= (1.0 / beta)
                weight = min(weight, 0.15)

                qty = int((capital * weight) // p_now)
                if qty > 0:
                    base_symbol = symbol.replace(".NS", "")
                    signal = {
                        "symbol": base_symbol,
                        "action": "BUY",
                        "quantity": qty,
                        "order_type": "MARKET",
                        "reason": f"Custom Beta MR: Beta={beta:.2f}, Size={weight:.1%}"
                    }
                    print(json.dumps(signal))

        except Exception as e:
            logging.error(f"Error processing {symbol}: {e}")
            continue


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