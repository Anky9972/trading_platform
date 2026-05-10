#!/usr/bin/env python3
"""
================================================================================
STRATEGY: VOLATILITY DISPERSION (VARIANCE RISK PREMIUM)
================================================================================

[ THE FEYNMAN EXPLANATION ]
Imagine an insurance company selling hurricane insurance.
If the news says a Category 5 hurricane is coming tomorrow, the insurance
company panics and charges $10,000 for a policy just to be safe.
But if you look at 100 years of weather data, a hurricane hitting that exact
town is incredibly rare (maybe worth $500).
In the stock market, "Options" are insurance policies. Sometimes, the market
panics and prices options astronomically high (High Implied Volatility).
If we check the actual math of how the stock normally moves (Historical Volatility)
and see the panic is unjustified, we can bet against the fear. When the fear
vanishes, the stock usually rallies.

[ THE TECHNICAL EXPLANATION ]
1. Historical Volatility (HV): Annualized std of log returns (trailing 126 days).
2. Implied Volatility (IV): In backtest mode, approximated via VIX proxy or
   Parkinson estimator from OHLC data (since options chains are unavailable).
3. Variance Risk Premium (VRP): The spread between IV-proxy and HV.

BACKTEST MODE:
  - No options chain available, so we use the Parkinson High-Low volatility
    estimator as an IV proxy. It captures the "expected range" the market
    is implicitly pricing via bid/ask spreads and OHLC extremes.
  - Signal: Enter when Parkinson vol < Historical vol AND Parkinson vol < 20%
    (market is complacent — volatility compression precedes breakouts)

LIVE MODE:
  - Uses yfinance options chain for real ATM implied volatility (unchanged).
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


def _historical_vol(close: pd.Series, window: int = 126) -> float:
    """
    Annualized historical volatility from log returns.
    Uses trailing `window` days.
    """
    if len(close) < window + 1:
        return np.nan

    log_rets = np.log(close / close.shift(1)).iloc[-window:]
    hv = log_rets.std() * np.sqrt(252)
    return float(hv)


def _parkinson_vol(high: pd.Series, low: pd.Series, window: int = 21) -> float:
    """
    Parkinson estimator — uses daily High/Low range to estimate
    'implied' range volatility. More efficient than close-to-close HV.

    Formula: sqrt( 1/(4*ln2) * mean((ln(H/L))^2) ) * sqrt(252)

    In backtest, this serves as our IV proxy because:
    - It captures the OHLC range the market "priced in" each day
    - It tends to be higher than close-to-close HV (like IV > HV)
    - When Parkinson vol < HV, the market is unusually complacent
    """
    if len(high) < window or len(low) < window:
        return np.nan

    h = high.iloc[-window:]
    l = low.iloc[-window:]

    # Avoid log of zero or negative
    ratio = h / l
    if (ratio <= 0).any():
        return np.nan

    log_hl = np.log(ratio)
    parkinson = np.sqrt((1 / (4 * np.log(2))) * (log_hl ** 2).mean()) * np.sqrt(252)
    return float(parkinson)


def _vol_percentile_rank(close: pd.Series,
                          current_hv: float,
                          lookback: int = 126) -> float:
    """
    Where does current HV sit in its own historical distribution?
    Returns percentile [0, 1]. Low value = volatility compression.
    """
    if len(close) < lookback + 21:
        return 0.5

    # Rolling 21-day HV over the lookback window
    log_rets = np.log(close / close.shift(1))
    rolling_hv = log_rets.rolling(21).std() * np.sqrt(252)
    hist_hvs = rolling_hv.iloc[-lookback:].dropna()

    if len(hist_hvs) < 10:
        return 0.5

    pct = float((hist_hvs < current_hv).mean())
    return pct


# ==============================================================================
# BACKTEST INTERFACE (called by alpha_pipeline.py)
# ==============================================================================

def backtest_signal(data, current_date, fundamentals=None, strategy_history=None):
    """
    Volatility Dispersion / VRP signal using price-derived vol estimates.

    Entry condition (matches live logic intent):
      - Parkinson vol (IV proxy) < Historical vol (HV)   ← complacency
      - Parkinson vol < 25% annualized                   ← low absolute vol
      - HV percentile rank < 40% vs 126-day history      ← vol compression
        (confirms we're in a low-vol environment, not just a quiet day)

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

    candidates = []

    for sym, df in data.items():
        try:
            if len(df) < 50:
                continue

            close = _s(df['Close'])
            high  = _s(df['High'])
            low   = _s(df['Low'])

            # Historical volatility (126-day)
            hv = _historical_vol(close, window=126)
            if np.isnan(hv) or hv <= 0:
                # Fall back to 60-day if not enough data
                hv = _historical_vol(close, window=60)
                if np.isnan(hv) or hv <= 0:
                    continue

            # IV proxy (Parkinson, 21-day)
            iv_proxy = _parkinson_vol(high, low, window=21)
            if np.isnan(iv_proxy) or iv_proxy <= 0:
                continue

            # Vol percentile rank (how compressed is current vol?)
            vol_pct = _vol_percentile_rank(close, hv, lookback=126)

            # Entry conditions (mirroring live: IV < HV and IV < 20%)
            # We use 25% for backtest since Parkinson < close-to-close HV often
            iv_low_absolute = iv_proxy < 0.25
            iv_below_hv = iv_proxy < hv
            vol_compressed = vol_pct < 0.40

            if iv_low_absolute and iv_below_hv and vol_compressed:
                # Signal strength: how much cheaper is IV vs HV?
                vrp_spread = hv - iv_proxy
                candidates.append({
                    "symbol": sym,
                    "strength": vrp_spread,
                })

        except Exception:
            continue

    if not candidates:
        return {}

    # Rank by VRP spread (bigger discount = stronger signal)
    candidates.sort(key=lambda x: x['strength'], reverse=True)

    # Take top 4, equal-weight (matches live universe size)
    top = candidates[:4]
    w = 1.0 / len(top)
    return {c['symbol']: w for c in top}


# ==============================================================================
# LIVE EXECUTION (called by engine.py via subprocess — UNCHANGED)
# ==============================================================================

def run_strategy(context: Dict[str, Any]):
    capital = context.get('capital', 0)
    positions = {p['symbol']: p for p in context.get('positions', [])}

    universe = ["RELIANCE.NS", "HDFCBANK.NS", "INFY.NS", "TCS.NS"]

    if capital < 10000:
        return

    for symbol in universe:
        if symbol in positions:
            continue

        try:
            ticker = yf.Ticker(symbol)
            hist = ticker.history(period="6mo")
            if hist.empty or len(hist) < 30:
                continue

            daily_returns = np.log(hist['Close'] / hist['Close'].shift(1))
            hv_annualized = daily_returns.std() * np.sqrt(252)

            try:
                options_dates = ticker.options
                if not options_dates:
                    logging.warning(f"No options chain found via yfinance for {symbol}")
                    continue

                near_expiry = options_dates[0]
                opt_chain = ticker.option_chain(near_expiry)

                calls = opt_chain.calls
                current_price = hist['Close'].iloc[-1]
                calls['strike_diff'] = abs(calls['strike'] - current_price)
                atm_call = calls.loc[calls['strike_diff'].idxmin()]

                iv_annualized = atm_call['impliedVolatility']

            except Exception as e:
                logging.warning(f"Could not fetch IV for {symbol}: {e}. Skipping.")
                continue

            if pd.isna(iv_annualized) or iv_annualized == 0:
                continue

            if iv_annualized < hv_annualized and iv_annualized < 0.20:
                base_symbol = symbol.replace(".NS", "")
                allocation = min(capital * 0.10, 50000)
                quantity = int(allocation // hist['Close'].iloc[-1])

                if quantity > 0:
                    signal = {
                        "symbol": base_symbol,
                        "action": "BUY",
                        "quantity": quantity,
                        "order_type": "MARKET",
                        "reason": f"Vol Dispersion: IV ({iv_annualized:.2f}) < HV ({hv_annualized:.2f})"
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