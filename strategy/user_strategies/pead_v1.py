#!/usr/bin/env python3
"""
================================================================================
STRATEGY: PEAD — POST-EARNINGS ANNOUNCEMENT DRIFT
================================================================================

[ FEYNMAN EXPLANATION ]
When a company reports earnings much better than expected, the stock price
jumps — but research shows it keeps drifting up for weeks after. The market
underreacts to good news. We buy after big positive surprises and ride
the drift for ~42 trading days.

[ TECHNICAL DETAILS ]
LIVE MODE:
  - Reads upcoming_earnings.json for actual vs expected EPS
  - Buys on >10% positive surprise, sells after 42 days

BACKTEST MODE:
  - No earnings data file available, so we simulate "earnings surprise"
    using price gaps: a day with >3% gap-up on >1.5x average volume
    suggests a positive fundamental event (earnings, guidance, etc.)
  - Holds for 42 trading days (tracked via strategy_history)
  - This is an approximation — real PEAD needs actual earnings data
================================================================================
"""
import sys
import json
import hashlib
import logging
from datetime import datetime

import pandas as pd
import numpy as np

try:
    import yfinance as yf
except ImportError:
    pass

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
    PEAD approximation using price-gap events as earnings surprise proxies.

    A "positive surprise" is detected when:
      - Overnight gap-up > 3%  (open >> previous close)
      - Volume on gap day > 1.5x 20-day average volume
      - The gap happened within the last 5 trading days (recent event)

    We buy these stocks and hold for ~42 days. Since the pipeline backtester
    manages positions externally, we simply emit buy signals for stocks
    showing recent surprise-like events.

    Parameters
    ----------
    data : dict[str, pd.DataFrame]
        {symbol: OHLCV DataFrame} sliced up to current_date
    current_date : datetime-like
        Current backtest date
    strategy_history : dict, optional
        Not used here (pipeline manages holding periods)

    Returns
    -------
    dict[str, float]
        {symbol: weight} — stocks with recent positive surprise events
    """
    if not data:
        return {}

    GAP_THRESHOLD = 0.03        # 3% overnight gap-up
    VOLUME_MULTIPLIER = 1.5     # volume > 1.5x average
    LOOKBACK_DAYS = 5           # event must be within last 5 days
    MIN_HISTORY = 30            # need at least 30 days of data

    candidates = []

    for sym, df in data.items():
        try:
            close = _s(df['Close'])
            open_ = _s(df['Open'])

            if len(close) < MIN_HISTORY:
                continue

            # Check if volume data exists and is usable
            has_volume = 'Volume' in df.columns
            if has_volume:
                volume = _s(df['Volume'])
            else:
                volume = None

            # Scan last LOOKBACK_DAYS for a gap-up event
            found_event = False
            event_strength = 0.0

            for offset in range(1, min(LOOKBACK_DAYS + 1, len(close) - 1)):
                idx = -offset

                prev_close = float(close.iloc[idx - 1])
                day_open = float(open_.iloc[idx])
                day_close = float(close.iloc[idx])

                if prev_close <= 0 or day_open <= 0:
                    continue

                # Overnight gap
                gap = (day_open - prev_close) / prev_close

                if gap < GAP_THRESHOLD:
                    continue

                # Volume confirmation (if available)
                if volume is not None and len(volume) > 25:
                    day_vol = float(volume.iloc[idx])
                    avg_vol = float(volume.iloc[idx - 20:idx].mean())

                    if avg_vol > 0 and day_vol < avg_vol * VOLUME_MULTIPLIER:
                        continue  # gap without volume = less convincing

                # Close held above open (didn't reverse intraday)
                if day_close >= day_open * 0.995:
                    found_event = True
                    event_strength = gap
                    break

            if found_event:
                # Also check that stock hasn't already drifted too far
                # (avoid chasing if >10% above the gap price)
                post_gap_return = (float(close.iloc[-1]) - float(close.iloc[-LOOKBACK_DAYS])) / float(close.iloc[-LOOKBACK_DAYS])
                if post_gap_return < 0.10:  # still room to drift
                    candidates.append({
                        "symbol": sym,
                        "strength": event_strength,
                    })

        except Exception:
            continue

    if not candidates:
        return {}

    # Rank by event strength (bigger surprise = more drift expected)
    candidates.sort(key=lambda x: x['strength'], reverse=True)

    # Take top 5 (PEAD is a moderate-concentration strategy)
    top = candidates[:5]

    # Equal-weight
    w = 1.0 / len(top)
    return {c['symbol']: w for c in top}


# ==============================================================================
# LIVE EXECUTION (called by engine.py via subprocess — UNCHANGED)
# ==============================================================================

def main():
    context = json.loads(sys.stdin.readline())

    positions = context.get("positions", [])
    capital = context.get("capital", 500000)
    timestamp = context.get("timestamp", "")

    signals = []

    # === ENTRY LOGIC ===
    try:
        with open("upcoming_earnings.json") as f:
            earnings = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        earnings = []

    held_symbols = {p['symbol'] for p in positions}

    for e in earnings:
        symbol = e.get("symbol", "")
        actual_eps = e.get("actual_eps", 0)
        expected_eps = e.get("expected_eps", 0)
        quarter = e.get("quarter", "")

        if not symbol or not expected_eps:
            continue

        if symbol in held_symbols:
            continue

        surprise = (actual_eps - expected_eps) / abs(expected_eps)
        if surprise > 0.10:
            idem_key = hashlib.sha256(
                f"pead_{symbol}_{quarter}".encode()
            ).hexdigest()[:12]

            signals.append({
                "symbol": symbol,
                "action": "BUY",
                "quantity": min(10, int(capital * 0.02 / max(actual_eps * 20, 100))),
                "order_type": "MARKET",
                "reason": f"PEAD: {surprise:.0%} surprise {quarter}",
                "idempotency_key": idem_key,
            })

    # === EXIT LOGIC ===
    for pos in positions:
        entry_str = pos.get("last_fill_at", "")
        if not entry_str:
            continue

        try:
            entry = datetime.fromisoformat(entry_str)
            days = (datetime.now() - entry).days
        except Exception:
            continue

        if days >= 42:
            idem_key = hashlib.sha256(
                f"pead_exit_{pos['symbol']}_{entry_str[:10]}".encode()
            ).hexdigest()[:12]

            signals.append({
                "symbol": pos['symbol'],
                "action": "SELL",
                "quantity": pos['quantity'],
                "order_type": "MARKET",
                "reason": f"PEAD exit: held {days} days",
                "idempotency_key": idem_key,
            })

    for sig in signals:
        print(json.dumps(sig))


if __name__ == "__main__":
    main()