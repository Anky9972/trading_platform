#!/usr/bin/env python3
"""
================================================================================
STRATEGY: VOLATILITY DISPERSION (SECTOR-RELATIVE)
================================================================================

[ FEYNMAN EXPLANATION ]
Imagine a group of friends (a sector) who usually walk at the same pace. 
If suddenly one friend starts running or jumping around (high volatility) 
while the others stayed calm, something unique is happening to that one friend. 
We look for these "restless" stocks. If they are restlessness (high volatility) 
relative to their peers, it often signals a significant move is coming.

[ TECHNICAL DETAILS ]
1. Group stocks by Sector (mapping included).
2. Calculate 20-day annualized realized volatility for each stock.
3. Calculate Median Volatility for the sector.
4. Signal = Realized Vol / Sector Median Vol.
5. Long top 3 stocks with Signal > 1.5.
================================================================================
"""

import sys
import json
import logging
from typing import Dict, Any, List
import numpy as np

try:
    import yfinance as yf
    import pandas as pd
    from core.signals import calculate_volatility_dispersion_scores
except ImportError:
    print(json.dumps({"error": "Missing dependencies: pip install yfinance pandas numpy"}))
    sys.exit(1)

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# Sector mapping for Nifty Universe
SECTOR_MAP = {
    "RELIANCE.NS": "Energy",
    "TCS.NS": "IT",
    "INFY.NS": "IT",
    "HDFCBANK.NS": "Financials",
    "ICICIBANK.NS": "Financials",
    "SBIN.NS": "Financials",
    "BHARTIARTL.NS": "Telecom",
    "ITC.NS": "FMCG",
    "L&T.NS": "Industrial",
    "AXISBANK.NS": "Financials",
    "ASIANPAINT.NS": "Consumer",
    "MARUTI.NS": "Auto",
    "SUNPHARMA.NS": "Pharma",
    "BAJFINANCE.NS": "Financials"
}

def run_strategy(context: Dict[str, Any]):
    capital = context.get('capital', 0)
    positions = {p['symbol']: p for p in context.get('positions', [])}
    
    if capital < 10000:
        return

    universe = list(SECTOR_MAP.keys())
    vol_data = []

    # Use unified signal logic
    df_vol = calculate_volatility_dispersion_scores({s: yf.download(s, period="40d", progress=False) for s in universe}, SECTOR_MAP)
    
    if df_vol.empty:
        return

    # 3. Filter and Rank
    # We want high dispersion (> 1.2x sector median)
    candidates = df_vol[df_vol['dispersion_score'] > 1.2].sort_values(by='dispersion_score', ascending=False)
    
    # Add price for order sizing
    def get_latest_price(sym):
        return yf.download(sym, period="1d", progress=False)['Close'].iloc[-1]
    
    top_picks = candidates.head(3).copy()
    if top_picks.empty:
        return
    
    top_picks['price'] = top_picks['symbol'].apply(get_latest_price)

    allocation_per_stock = (capital * 0.95) / len(top_picks)

    for _, pick in top_picks.iterrows():
        sym = pick['symbol']
        if sym in positions: continue # Simple: don't double up
        
        price = pick['price']
        qty = int(allocation_per_stock // price)
        if qty > 0:
            base_symbol = sym.replace(".NS", "")
            signal = {
                "symbol": base_symbol,
                "action": "BUY",
                "quantity": qty,
                "order_type": "MARKET",
                "reason": f"Vol Dispersion: Score={pick['dispersion_score']:.2f} (Sector: {pick['sector']})"
            }
            print(json.dumps(signal))

def backtest_signal(data, current_date, fundamentals=None, strategy_history=None):
    """Vectorized logic for backtester parity."""
    allocations = {}
    df_vol = calculate_volatility_dispersion_scores(data, SECTOR_MAP)
    if df_vol.empty: return {}
    
    candidates = df_vol[df_vol['dispersion_score'] > 1.2].sort_values(by='dispersion_score', ascending=False)
    top_picks = candidates.head(3)
    
    for _, row in top_picks.iterrows():
        allocations[row['symbol']] = 1.0 / len(top_picks)
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
