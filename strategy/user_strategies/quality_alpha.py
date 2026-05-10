#!/usr/bin/env python3
"""
================================================================================
STRATEGY: QUALITY ALPHA (OPERATIONAL EFFICIENCY & ROE)
================================================================================

[ FEYNMAN EXPLANATION ]
Imagine two companies selling the exact same thing (like soap).
Company A spends $1 to make a bar of soap and sells it for $1.10.
Company B spends $0.60 to make the same bar and sells it for $1.10.
Company B is a "Quality" company – it is much more efficient at turning money
into more money. We look for companies with high Return on Equity (ROE) and
high Profit Margins. These companies are well-run machines that usually
weather storms better and grow more reliably.

[ TECHNICAL DETAILS ]
LIVE MODE:
  - Uses yfinance .info for ROE, operating margins, debt/equity
  - Filters ROE > 15%, margin > 10%, D/E < 150

BACKTEST MODE:
  - Fundamental data not available from price history, so we use
    price-derived quality proxies:
    * Sharpe ratio (60d) → proxy for ROE (efficient returns)
    * Drawdown resilience → proxy for margin (pricing power in stress)
    * Trend smoothness → proxy for clean balance sheet (low volatility)
  - These capture the SAME economic intuition: quality = stable, efficient returns
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


def _rolling_sharpe(close: pd.Series, window: int = 60) -> float:
    """
    Annualized Sharpe ratio over trailing window.
    Proxy for ROE: how efficiently does this stock generate returns per unit risk?
    """
    if len(close) < window + 1:
        return 0.0

    rets = close.pct_change().iloc[-window:]
    mu = rets.mean()
    sigma = rets.std()

    if sigma < 1e-8:
        return 0.0

    return float(mu / sigma * np.sqrt(252))


def _max_drawdown(close: pd.Series, window: int = 60) -> float:
    """
    Maximum drawdown over trailing window.
    Proxy for margin/pricing power: quality companies drawdown less.
    Returns a positive number (e.g., 0.15 = 15% max drawdown).
    """
    if len(close) < window:
        return 1.0

    tail = close.iloc[-window:]
    peak = tail.cummax()
    dd = (tail - peak) / peak
    return float(abs(dd.min()))


def _trend_smoothness(close: pd.Series, window: int = 60) -> float:
    """
    R-squared of linear fit to log-prices.
    Proxy for balance sheet quality: high R² = smooth trend = well-managed.
    Returns value in [0, 1].
    """
    if len(close) < window:
        return 0.0

    tail = close.iloc[-window:]

    # Handle non-positive prices
    if (tail <= 0).any():
        return 0.0

    log_p = np.log(tail.values)
    x = np.arange(len(log_p))

    # Linear regression
    slope, intercept = np.polyfit(x, log_p, 1)
    predicted = slope * x + intercept
    ss_res = np.sum((log_p - predicted) ** 2)
    ss_tot = np.sum((log_p - log_p.mean()) ** 2)

    if ss_tot < 1e-12:
        return 0.0

    r_squared = 1.0 - ss_res / ss_tot
    return float(max(0.0, r_squared))


# ==============================================================================
# BACKTEST INTERFACE (called by alpha_pipeline.py)
# ==============================================================================

def backtest_signal(data, current_date, fundamentals=None, strategy_history=None):
    """
    Quality Alpha using price-derived quality proxies.

    Scoring:
      - Sharpe (60d)          → proxy for ROE           (weight 0.40)
      - Drawdown resilience   → proxy for margin        (weight 0.35)
      - Trend smoothness      → proxy for balance sheet  (weight 0.25)

    Filters:
      - Sharpe > 0.5 (positive risk-adjusted return)
      - Max drawdown < 20% (resilient)
      - Trend R² > 0.3 (reasonably smooth)

    Parameters
    ----------
    data : dict[str, pd.DataFrame]
        {symbol: OHLCV DataFrame} sliced up to current_date
    current_date : datetime-like
        Current backtest date

    Returns
    -------
    dict[str, float]
        {symbol: weight} — top-3 quality picks, equal-weighted
    """
    if not data:
        return {}

    candidates = []

    for sym, df in data.items():
        try:
            close = _s(df['Close'])

            if len(close) < 65:
                continue

            close_lagged = close.iloc[:-1]
            sharpe = _rolling_sharpe(close_lagged, 60)
            max_dd = _max_drawdown(close_lagged, 60)
            smoothness = _trend_smoothness(close_lagged, 60)

            # Quality filters
            if sharpe < 0.5:
                continue
            if max_dd > 0.20:
                continue
            if smoothness < 0.30:
                continue

            # Composite quality score
            # Drawdown resilience = 1 - max_dd (higher is better)
            dd_resilience = 1.0 - max_dd

            score = (0.40 * sharpe +
                     0.35 * dd_resilience +
                     0.25 * smoothness)

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
        "HINDUNILVR.NS", "ITC.NS", "TCS.NS", "INFY.NS", "NESTLEIND.NS",
        "TITAN.NS", "ASIANPAINT.NS", "BRITANNIA.NS", "BAJAJ-AUTO.NS", "SUNPHARMA.NS"
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

            roe = info.get("returnOnEquity", 0)
            margin = info.get("operatingMargins", 0)
            debt = info.get("debtToEquity", 0)

            if roe > 0.15 and margin > 0.10 and debt < 150:
                score = roe + margin
                candidates.append({
                    "symbol": symbol,
                    "score": score,
                    "roe": roe,
                    "debt": debt,
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
                "reason": f"Quality Alpha: ROE={pick['roe']:.1%}, Debt={pick['debt']:.0f}"
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