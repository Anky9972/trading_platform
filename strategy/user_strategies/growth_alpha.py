#!/usr/bin/env python3
"""
================================================================================
STRATEGY: GROWTH ALPHA (REVENUE & EARNINGS MOMENTUM)
================================================================================

[ FEYNMAN EXPLANATION ]
Imagine you're at a farmers market. Some stalls are growing really fast – every
week they have more fruit and more customers. These are the "Growth" stalls.
In the stock market, we look for companies whose price performance shows strong
sustained growth characteristics: consistent upward trends with stable
trajectories. This strategy uses price-derived growth proxies since
fundamental data isn't available in backtests.

[ TECHNICAL DETAILS ]
1. Calculate 60-day CAGR as revenue growth proxy.
2. Calculate 20-day CAGR as earnings momentum proxy.
3. Filter: 60d CAGR > 0.15 annualized AND 20d CAGR > 0.10 annualized.
4. Score = weighted sum of both growth rates.
5. Select Top 3, equal-weight.
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


def _annualized_return(close: pd.Series, lookback: int) -> float:
    """
    Calculate annualized return over the last `lookback` trading days.
    Returns annualized rate (e.g., 0.15 = 15% annual growth).
    """
    if len(close) < lookback + 1:
        return 0.0

    p_end = float(close.iloc[-1])
    p_start = float(close.iloc[-lookback])

    if p_start <= 0 or p_end <= 0:
        return 0.0

    # Raw return
    raw = p_end / p_start

    # Annualize: (1 + r)^(252/lookback) - 1
    years_fraction = lookback / 252.0
    if years_fraction <= 0:
        return 0.0

    return raw ** (1.0 / years_fraction) - 1.0


def _return_consistency(close: pd.Series, window: int = 60) -> float:
    """
    Fraction of rolling 5-day windows with positive returns.
    Higher = more consistent growth (less volatile).
    """
    if len(close) < window + 5:
        return 0.0

    tail = close.iloc[-window:]
    r5 = tail.pct_change(5).dropna()
    if len(r5) == 0:
        return 0.0

    return float((r5 > 0).mean())


# ==============================================================================
# BACKTEST INTERFACE (called by alpha_pipeline.py)
# ==============================================================================

def backtest_signal(data, current_date, fundamentals=None, strategy_history=None):
    """
    Growth Alpha using price-derived growth proxies.

    Since yfinance .info (fundamentals) is not available in backtests,
    we use:
      - 60-day annualized return as "revenue growth" proxy
      - 20-day annualized return as "earnings momentum" proxy
      - Return consistency as a quality filter

    Parameters
    ----------
    data : dict[str, pd.DataFrame]
        {symbol: OHLCV DataFrame} sliced up to current_date
    current_date : datetime-like
        Current backtest date

    Returns
    -------
    dict[str, float]
        {symbol: weight} — top growth picks, equal-weighted
    """
    if not data:
        return {}

    candidates = []

    for sym, df in data.items():
        try:
            close = _s(df['Close'])
            if len(close) < 65:
                continue

            # Growth proxies from price
            growth_60d = _annualized_return(close, 60)  # "revenue growth" proxy
            growth_20d = _annualized_return(close, 20)  # "earnings momentum" proxy
            consistency = _return_consistency(close, 60)

            # Filter: meaningful growth + consistency
            if growth_60d > 0.15 and growth_20d > 0.10 and consistency > 0.45:
                # Score: weighted blend
                score = 0.5 * growth_60d + 0.3 * growth_20d + 0.2 * consistency
                candidates.append({
                    "symbol": sym,
                    "score": score,
                })

        except Exception:
            continue

    if not candidates:
        return {}

    # Sort by score, take top 3
    candidates.sort(key=lambda x: x['score'], reverse=True)
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
        "TITAN.NS", "BAJFINANCE.NS", "ADANIENT.NS", "TATAMOTORS.NS", "HCLTECH.NS"
    ]

    if capital < 10000:
        return

    candidates = []

    for symbol in universe:
        if symbol in positions:
            continue

        try:
            ticker = yf.Ticker(symbol)
            info = ticker.info

            rev_growth = info.get("revenueGrowth", 0)
            earn_growth = info.get("earningsGrowth", 0)

            if rev_growth > 0.15 and earn_growth > 0.10:
                score = rev_growth + earn_growth
                candidates.append({
                    "symbol": symbol,
                    "score": score,
                    "price": info.get("currentPrice") or info.get("regularMarketPrice")
                })

        except Exception as e:
            logging.error(f"Error fetching {symbol}: {e}")
            continue

    if not candidates:
        return

    candidates.sort(key=lambda x: x['score'], reverse=True)
    top_picks = candidates[:3]

    allocation_per_stock = (capital * 0.95) / len(top_picks)

    for pick in top_picks:
        sym = pick['symbol']
        price = pick['price']
        if not price:
            continue

        qty = int(allocation_per_stock // price)
        if qty > 0:
            base_symbol = sym.replace(".NS", "")
            signal = {
                "symbol": base_symbol,
                "action": "BUY",
                "quantity": qty,
                "order_type": "MARKET",
                "reason": f"Growth Alpha: RevGrowth={pick['score']:.1%}"
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