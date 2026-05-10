#!/usr/bin/env python3
"""
================================================================================
STRATEGY: LIQUIDITY IMBALANCE (LIQ-IMB)
================================================================================

[ FEYNMAN EXPLANATION ]
Sometimes prices move because everyone is trading (high volume), which is normal.
But sometimes prices move a lot while almost no one is trading (low volume). 
This is "shaky ground." If a stock price drops significantly but the volume 
is very low, it might be a fake-out because there wasn't enough "weight" behind 
the move. We look for these imbalances to bet on a quick bounce back.

[ TECHNICAL DETAILS ]
1. Calculate 1-day returns.
2. Calculate Volume Z-Score (vs 20d SMA).
3. If Price Return < -2% and Volume is < 0.5x SMA -> BUY (Mean Reversion).
4. Sizing: Constant weight for simplicity.
================================================================================
"""

import sys
import json
import logging
from typing import Dict, Any

try:
    import yfinance as yf
    import pandas as pd
except ImportError:
    print(json.dumps({"error": "Missing dependencies: pip install yfinance pandas"}))
    sys.exit(1)

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

def run_strategy(context: Dict[str, Any]):
    capital = context.get('capital', 0)
    positions = {p['symbol']: p for p in context.get('positions', [])}
    
    universe = [
        "RELIANCE.NS", "TCS.NS", "INFY.NS", "HDFCBANK.NS", "ICICIBANK.NS",
        "SBIN.NS", "BHARTIARTL.NS", "ITC.NS", "L&T.NS", "AXISBANK.NS"
    ]
    
    if capital < 10000:
        return

    candidates = []

    for symbol in universe:
        if symbol in positions:
            continue
            
        try:
            df = yf.download(symbol, period="30d", interval="1d", progress=False)
            if len(df) < 20: continue
            
            # Use unified signal logic
            from core.signals import calculate_liquidity_imbalance_metrics
            metrics = calculate_liquidity_imbalance_metrics(df)
            
            if metrics and metrics["price_return"] < -0.015 and metrics["volume_ratio"] < 0.6:
                candidates.append({
                    "symbol": symbol,
                    "ret": metrics["price_return"],
                    "vol_ratio": metrics["volume_ratio"],
                    "price": metrics["current_price"]
                })
                
        except Exception as e:
            logging.error(f"Error processing {symbol}: {e}")
            continue

    if not candidates:
        return

    # Pick most extreme imbalance
    candidates.sort(key=lambda x: x['vol_ratio'])
    top_picks = candidates[:2]
    
    allocation_per_stock = (capital * 0.90) / len(top_picks)

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
                "reason": f"Liq Imbalance: Ret={pick['ret']:.1%}, VolRatio={pick['vol_ratio']:.2f}"
            }
            print(json.dumps(signal))

def backtest_signal(data, current_date, fundamentals=None, strategy_history=None):
    """Vectorized logic for backtester parity."""
    from core.signals import calculate_liquidity_imbalance_metrics
    allocations = {}
    candidates = []
    
    for symbol, df in data.items():
        metrics = calculate_liquidity_imbalance_metrics(df)
        if metrics and metrics["price_return"] < -0.015 and metrics["volume_ratio"] < 0.6:
            candidates.append({"symbol": symbol, "ratio": metrics["volume_ratio"]})
            
    if not candidates: return {}
    
    candidates.sort(key=lambda x: x['ratio'])
    top_picks = candidates[:2]
    
    for pick in top_picks:
        allocations[pick['symbol']] = 1.0 / len(top_picks)
    return allocations

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
