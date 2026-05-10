"""5 candidate alphas designed for high BRAIN acceptance probability.

Each maps to a proven archetype from your existing 18-alpha library, with
minor field/window variations. Each is constructed to target the design
envelope:

    Sharpe >= 1.5  AND  Turnover <= 30%  AND  Returns >= 6-8%

so that Fitness (= Sharpe * sqrt(|R|/TO)) clears 1.0 with margin.

Only price+volume fields are used (yfinance limitation). Each function
takes the long panel (date, ticker)x(open,high,low,close,volume,returns)
and returns a Series of cross-sectional scores.
"""
from __future__ import annotations

import numpy as np
import pandas as pd


# ---------- helpers (mimic WQ ts_* / rank operators on a panel) -------------

def _by_ticker(s: pd.Series) -> pd.core.groupby.SeriesGroupBy:
    return s.groupby(level="ticker")


def ts_mean(s: pd.Series, d: int) -> pd.Series:
    return _by_ticker(s).transform(lambda x: x.rolling(d, min_periods=max(2, d // 2)).mean())


def ts_std(s: pd.Series, d: int) -> pd.Series:
    return _by_ticker(s).transform(lambda x: x.rolling(d, min_periods=max(2, d // 2)).std())


def ts_rank(s: pd.Series, d: int) -> pd.Series:
    """Rank of current value vs the past d days, in [0,1]."""
    return _by_ticker(s).transform(
        lambda x: x.rolling(d, min_periods=max(2, d // 4)).rank(pct=True)
    )


def ts_delay(s: pd.Series, d: int) -> pd.Series:
    return _by_ticker(s).shift(d)


def ts_decay_linear(s: pd.Series, d: int) -> pd.Series:
    """Linearly-weighted moving average over the past d days."""
    weights = np.arange(1, d + 1, dtype=float)
    weights /= weights.sum()
    return _by_ticker(s).transform(
        lambda x: x.rolling(d, min_periods=max(2, d // 2))
                   .apply(lambda v: float(np.dot(v, weights[-len(v):] / weights[-len(v):].sum())), raw=True)
    )


def cross_rank(s: pd.Series) -> pd.Series:
    """Rank cross-section per date in [0,1]."""
    return s.groupby(level="date").rank(pct=True)


# ---------- the 5 candidates -------------------------------------------------

def alpha_C1_long_horizon_intraday_mr(panel: pd.DataFrame) -> pd.Series:
    """C1 — Long-horizon intraday mean reversion.

    Archetype: Alpha 15 (Sharpe 2.76 in your library).
    Bet: stocks where the day's midpoint sits well above today's close
    (closing on the lows, deep oversold) tend to bounce. Long-horizon
    (252-day) ranking removes regime drift.
    """
    intraday_mr = (panel["high"] + panel["low"]) / 2.0 - panel["close"]
    score = ts_rank(intraday_mr, 252)        # high rank = oversold close
    # smooth modestly to drop turnover
    score = ts_decay_linear(score, 5)
    return score


def alpha_C2_pure_micro_decay(panel: pd.DataFrame) -> pd.Series:
    """C2 — Pure microstructure decay (Alpha 8 archetype, Sharpe 2.27).

    vwap_proxy = (H+L+C)/3 since yfinance has no true VWAP.
    Bet: stocks closing at the bottom of the day's range with high relative
    volume tend to mean-revert short-term.
    """
    vwap_proxy = (panel["high"] + panel["low"] + panel["close"]) / 3.0
    vwap_gap = (vwap_proxy - panel["close"]) / panel["close"]
    rng = panel["high"] - panel["low"]
    range_pos = (panel["close"] - panel["low"]) / rng.replace(0, np.nan)
    rel_vol = panel["volume"] / ts_mean(panel["volume"], 125)
    micro = cross_rank(vwap_gap) * cross_rank(-(range_pos - 0.5)) * cross_rank(rel_vol)
    score = ts_decay_linear(micro, 3)
    return score


def alpha_C3_vol_scaled_shock(panel: pd.DataFrame) -> pd.Series:
    """C3 — Vol-scaled shock with linear decay (Alpha 11 archetype, Sharpe 1.80).

    Bet: stocks with extreme negative one-day returns and elevated volume
    are oversold; we long them. The 252-day |returns| rank measures shock
    magnitude in stock-specific vol units.
    """
    rel_vol = panel["volume"] / ts_mean(panel["volume"], 20)
    rng_returns = panel["returns"].abs()
    shock = np.sign(-panel["returns"]) * ts_rank(rng_returns, 252) * rel_vol
    score = ts_decay_linear(shock, 5)
    return score


def alpha_C4_risk_scaled_long_momentum(panel: pd.DataFrame) -> pd.Series:
    """C4 — Risk-scaled 12-1 momentum (NEW, classic Jegadeesh-Titman 1993).

    12-month price momentum, skipping the most recent month (avoids 1-month
    reversal contamination), then divided by trailing volatility so we're
    longing high-Sharpe trending names rather than just high-return ones.
    Low turnover (monthly drift only).
    """
    price = panel["close"]
    mom_12m = (price - ts_delay(price, 252)) / ts_delay(price, 252)
    mom_1m = (price - ts_delay(price, 21)) / ts_delay(price, 21)
    mom_12_1 = mom_12m - mom_1m       # drop 1m reversal
    vol = ts_std(panel["returns"], 252)
    risk_scaled = mom_12_1 / vol.replace(0, np.nan)
    score = ts_rank(risk_scaled, 252)
    return score


def alpha_C5_amihud_weighted_reversal(panel: pd.DataFrame) -> pd.Series:
    """C5 — Amihud illiquidity-weighted 5-day reversal (NEW, Avramov-Chordia-
    Goyal 2006). Reversal works strongest in illiquid names.
    """
    abs_ret = panel["returns"].abs()
    # daily Amihud illiquidity ~ |return| / dollar_volume
    dollar_vol = panel["close"] * panel["volume"]
    amihud = abs_ret / dollar_vol.replace(0, np.nan)
    illiq = ts_mean(amihud, 21)
    rev_5d = -(panel["close"] / ts_delay(panel["close"], 5) - 1.0)
    # cross-rank both legs and combine
    score = 0.5 * cross_rank(illiq) + 0.5 * cross_rank(rev_5d)
    score = ts_decay_linear(score, 3)
    return score


CANDIDATES = {
    "C1_long_horizon_intraday_mr": alpha_C1_long_horizon_intraday_mr,
    "C2_pure_micro_decay":          alpha_C2_pure_micro_decay,
    "C3_vol_scaled_shock":          alpha_C3_vol_scaled_shock,
    "C4_risk_scaled_long_momentum": alpha_C4_risk_scaled_long_momentum,
    "C5_amihud_weighted_reversal":  alpha_C5_amihud_weighted_reversal,
}
