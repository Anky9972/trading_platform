#!/usr/bin/env python3
"""
================================================================================
STRATEGY: CROSS-REGIME MEAN REVERSION
================================================================================

[ THE FEYNMAN EXPLANATION (Like I'm 5) ]
Imagine a crazy dog on a leash bouncing around its owner in a park. 
1. If the owner is standing still (Ranging Market), the dog eventually bounces 
   to the end of the leash and gets yanked right back to the owner (Mean Reversion).
2. If the owner is running full sprints (Trending Market), the dog isn't coming back;
   the dog is being dragged along!
This strategy checks if the owner is standing still (ADX < 20). If they are, 
AND the dog is extremely far away (Z-Score < -2.0), we bet that the dog will 
snap back to the middle. If the owner is running, we do nothing.

[ THE TECHNICAL EXPLANATION ]
1. Regime Filter: Average Directional Index (ADX)
   Formula: ADX = Smoothed Moving Average of Directional Movement (DX).
   We calculate the 14-period ADX to measure trend strength. 
   - ADX > 25 means a strong trend exists (avoid Mean Reversion!).
   - ADX < 20 means there is no trend (ranging/consolidation) -> Safe to execute.
   
2. Execution Trigger: Z-Score (Standard Deviations from Mean)
   Formula: Z = (Current Price - 20_SMA) / 20_Day_Standard_Deviation
   We calculate the 20-day Simple Moving Average. If the current price drops 
   2 full standard deviations below the mean (Z < -2.0) WHILE in a ranging regime,
   it represents a statistical anomaly with a high probability of reverting.

[ EXECUTION FLOW ]
1. Engine feeds Capital & Positions.
2. Fetch 60 days of historical data via yfinance.
3. Drop stocks we already own.
4. Calculate ADX and Z-score for the last candlestick.
5. If ADX < 20 AND Z-score < -2: Generate "BUY" signal for 10% of portfolio cap.
================================================================================
"""
import sys
import json
import logging
from typing import Dict, Any

# Ensure yfinance and pandas are available for regime calculations
try:
    import yfinance as yf
    import pandas as pd
    import numpy as np
except ImportError:
    print(json.dumps({"error": "Missing dependencies: pip install yfinance pandas numpy"}))
    sys.exit(1)

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

def calculate_adx(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Calculate the Average Directional Index (ADX) to determine regime."""
    high = df['High']
    low = df['Low']
    close = df['Close']

    plus_dm = high.diff()
    minus_dm = low.diff()

    plus_dm[plus_dm < 0] = 0
    minus_dm[minus_dm > 0] = 0

    tr1 = pd.DataFrame(high - low)
    tr2 = pd.DataFrame(abs(high - close.shift(1)))
    tr3 = pd.DataFrame(abs(low - close.shift(1)))
    frames = [tr1, tr2, tr3]
    tr = pd.concat(frames, axis=1, join='inner').max(axis=1)
    atr = tr.rolling(period).mean()

    plus_di = 100 * (plus_dm.ewm(alpha=1/period).mean() / atr)
    minus_di = abs(100 * (minus_dm.ewm(alpha=1/period).mean() / atr))

    dx = (abs(plus_di - minus_di) / abs(plus_di + minus_di)) * 100
    adx = ((dx.shift(1) * (period - 1)) + dx) / period
    adx.smooth = dx.ewm(alpha=1/period).mean()
    return adx.smooth

def run_strategy(context: Dict[str, Any]):
    capital = context.get('capital', 0)
    positions = {p['symbol']: p for p in context.get('positions', [])}
    
    # Example Universe
    universe = ["RELIANCE.NS", "TCS.NS", "HDFCBANK.NS", "INFY.NS", "ITC.NS"]
    
    # We only trade if we have capital
    if capital < 10000:
        return

    # Fetch 60 days of historical data for our universe
    data = yf.download(universe, period="60d", interval="1d", group_by="ticker", progress=False)

    for symbol in universe:
        if symbol in positions:
            continue # Skip if already holding

        try:
            # Handle single vs multi ticker yfinance output
            if len(universe) == 1:
                df = data
            else:
                df = data[symbol]

            df = df.dropna()
            if len(df) < 30:
                continue

            # 1. Calculate Regime Constraints
            adx = calculate_adx(df)
            current_adx = adx.iloc[-1]

            # 2. Mean Reversion Constraints
            sma_20 = df['Close'].rolling(window=20).mean()
            std_20 = df['Close'].rolling(window=20).std()
            z_score = (df['Close'] - sma_20) / std_20
            current_z = z_score.iloc[-1]

            # 3. Execution Logic
            # ADX < 20 means chopped/ranging market (Perfect for Mean Reversion)
            # Z-Score < -2.0 means 2 standard deviations oversold
            if current_adx < 20 and current_z < -2.0:
                base_symbol = symbol.replace(".NS", "")
                
                # We allocate 10% of capital or max 50,000 INR
                allocation = min(capital * 0.10, 50000)
                price = df['Close'].iloc[-1]
                quantity = int(allocation // price)

                if quantity > 0:
                    signal = {
                        "symbol": base_symbol,
                        "action": "BUY",
                        "quantity": quantity,
                        "order_type": "MARKET",
                        "reason": f"Cross-Regime MR: ADX={current_adx:.2f} (Ranging), Z-Score={current_z:.2f} (Oversold)"
                    }
                    print(json.dumps(signal))

        except Exception as e:
            logging.error(f"Error processing {symbol}: {e}")

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
