import pandas as pd
import numpy as np
from typing import Dict, List, Optional

"""
================================================================================
QUANTITATIVE RESEARCH: ATOMIC FEATURE LIBRARY
================================================================================
Purpose: Provide stationary and clean market features for alpha research.
"""

def rolling_z_score(series: pd.Series, window: int = 20) -> pd.Series:
    """Calculates the rolling Z-score (Standardized Score) for a series."""
    mean = series.rolling(window=window).mean()
    std = series.rolling(window=window).std()
    return (series - mean) / std

def realized_volatility(returns: pd.Series, window: int = 20) -> pd.Series:
    """Calculates annualized realized volatility."""
    return returns.rolling(window=window).std() * np.sqrt(252)

def liquidity_imbalance(df: pd.DataFrame, window: int = 20) -> pd.Series:
    """
    Measures Volume-Weighted Relative Price Distance.
    Intuition: Large volume with little price movement suggests absorption/liquidity.
    """
    # Relative spread proxy
    price_range = (df['High'] - df['Low']) / df['Close']
    imbalance = df['Volume'] / (price_range.replace(0, 1e-6))
    return rolling_z_score(imbalance, window=window)

def amihud_illiquidity(df: pd.DataFrame, window: int = 20) -> pd.Series:
    """
    Amihud (2002) Illiquidity Measure: Avg(|Return| / Volume).
    High values = low liquidity (price moves easily on low volume).
    """
    returns = df['Close'].pct_change().abs()
    illiq = returns / df['Volume']
    return illiq.rolling(window=window).mean()

def volatility_cluster_score(returns: pd.Series, window: int = 50) -> pd.Series:
    """
    Checks if current volatility is significantly higher/lower than historical.
    Intuition: Volatility clusters (quiet leads to quiet, loud leads to loud).
    """
    short_vol = returns.rolling(window=10).std()
    long_vol = returns.rolling(window=window).std()
    return short_vol / long_vol

def price_momentum_score(df: pd.DataFrame, lookback: int = 252) -> float:
    """Returns the risk-adjusted momentum (Sharpe of price trend)."""
    returns = df['Close'].pct_change().dropna()
    if len(returns) < lookback:
        return 0.0
    recent = returns.tail(lookback)
    return (recent.mean() / recent.std()) * np.sqrt(252)
