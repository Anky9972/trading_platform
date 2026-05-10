"""Refined candidates after first-pass local sim.

Lessons from v1 (run on 147 stocks 2019-2023):
  - C5 Amihud reversal: IS=0.59 / OS=0.43 / TO=27%  -> the one robust survivor
  - C2 micro decay:     IS=0.90 / OS=-0.18         -> overfit pre-2022, broken post
  - C1 / C3:            high TO 60-69%, kill Fitness regardless of Sharpe
  - C4 momentum:        IS=-0.15 / OS=+0.92        -> regime-flipped

Refinements:
  1. KILL C1, C2, C3 in their current form (high-TO MR doesn't work on this universe).
  2. KEEP C5 as is (R1) and add an EBITDA-yield-tilted variant (R2).
  3. KEEP C4's idea but pair it with a low-vol filter to stabilize IS (R3).
  4. ADD an explicit low-turnover compound: 21-day reversal x 252-day vol-rank (R4).
  5. ADD a risk-scaled cross-sectional dispersion alpha (R5).

Each refinement targets:  Sharpe>=1.0,  Turnover<=30%,  Returns>=5%  on local sim
(BRAIN should beat that by ~1.5-2x because TOP3000 is 20x wider than our 147).
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from .candidate_alphas import (
    ts_mean, ts_std, ts_rank, ts_delay, ts_decay_linear, cross_rank,
)


# R1 -- Amihud illiquidity-weighted 5-day reversal (the one that worked)
def alpha_R1_amihud_reversal(panel: pd.DataFrame) -> pd.Series:
    abs_ret = panel["returns"].abs()
    dollar_vol = panel["close"] * panel["volume"]
    amihud = abs_ret / dollar_vol.replace(0, np.nan)
    illiq = ts_mean(amihud, 21)
    rev_5d = -(panel["close"] / ts_delay(panel["close"], 5) - 1.0)
    score = 0.5 * cross_rank(illiq) + 0.5 * cross_rank(rev_5d)
    return ts_decay_linear(score, 3)


# R2 -- Amihud x short-rev x 21-day momentum control
# Bet: reversal works BEST when it's not in a strong trend regime.
# We long oversold names UNLESS they're in a strong long-term down-trend
# (in which case selling is informed, not noise).
def alpha_R2_amihud_with_trend_filter(panel: pd.DataFrame) -> pd.Series:
    abs_ret = panel["returns"].abs()
    dollar_vol = panel["close"] * panel["volume"]
    illiq = ts_mean(abs_ret / dollar_vol.replace(0, np.nan), 21)
    rev_5d = -(panel["close"] / ts_delay(panel["close"], 5) - 1.0)
    # 21-day trend (sign only) -- we DOWNWEIGHT reversal for stocks in strong
    # multi-week downtrends
    trend_21 = panel["close"] / ts_delay(panel["close"], 21) - 1.0
    trend_strength = ts_rank(trend_21.abs(), 252)
    # gate: reversal scaled by (1 - trend_strength)
    score = (cross_rank(illiq) + cross_rank(rev_5d)) * (1.0 - 0.5 * trend_strength)
    return ts_decay_linear(score, 5)


# R3 -- 12-1 momentum with low-vol filter (Frazzini-Pedersen-style "QMJ")
# C4 had OS Sharpe 0.92 with TO 21% but IS -0.15 (regime).
# We stabilise by REQUIRING the momentum stock to also be in the low-vol bucket.
# This is a quality-momentum hybrid known to work cross-sectionally.
def alpha_R3_low_vol_momentum(panel: pd.DataFrame) -> pd.Series:
    price = panel["close"]
    mom_12_1 = (price / ts_delay(price, 252) - 1.0) - (price / ts_delay(price, 21) - 1.0)
    vol_252 = ts_std(panel["returns"], 252)
    # invert vol -- low vol bucket is positive
    inv_vol = -ts_rank(vol_252, 252)
    score = 0.7 * cross_rank(mom_12_1) + 0.3 * cross_rank(inv_vol)
    return ts_decay_linear(score, 10)


# R4 -- Cross-sectional dispersion-weighted reversal
# When the cross-sectional dispersion of returns is HIGH (volatile day),
# 5-day reversal is the strongest. We size up the reversal in those regimes.
def alpha_R4_dispersion_weighted_reversal(panel: pd.DataFrame) -> pd.Series:
    rets = panel["returns"]
    # cross-sectional std per day -- broadcast back
    cs_std = rets.groupby(level="date").transform("std")
    rev_5d = -(panel["close"] / ts_delay(panel["close"], 5) - 1.0)
    # weight by cross-sectional dispersion rank (over time)
    cs_rank = _by_ticker_safe_rank(cs_std, 63)
    score = cross_rank(rev_5d) * cs_rank
    return ts_decay_linear(score, 5)


def _by_ticker_safe_rank(s: pd.Series, d: int) -> pd.Series:
    """ts_rank that handles the case where many stocks have the same
    cross-sectional value (which is common for cs_std)."""
    return s.groupby(level="ticker").transform(
        lambda x: x.rolling(d, min_periods=max(2, d // 2)).rank(pct=True)
    )


# R5 -- Range compression breakout (Bollinger-style with volume confirm)
# Bet: stocks with the tightest 21-day price range AND elevated volume are
# coiling. Long when 5-day return is negative (mean-reversion bias inside
# the squeeze).
def alpha_R5_squeeze_reversion(panel: pd.DataFrame) -> pd.Series:
    rng = panel["high"] - panel["low"]
    pct_range = rng / panel["close"]
    range_compress = -ts_rank(ts_mean(pct_range, 21), 252)   # tight range = high
    rel_vol = panel["volume"] / ts_mean(panel["volume"], 63)
    rev_5d = -(panel["close"] / ts_delay(panel["close"], 5) - 1.0)
    score = (
        0.45 * cross_rank(range_compress)
      + 0.30 * cross_rank(rel_vol)
      + 0.25 * cross_rank(rev_5d)
    )
    return ts_decay_linear(score, 5)


REFINED_CANDIDATES = {
    "R1_amihud_reversal":               alpha_R1_amihud_reversal,
    "R2_amihud_trend_filtered":         alpha_R2_amihud_with_trend_filter,
    "R3_low_vol_momentum":              alpha_R3_low_vol_momentum,
    "R4_dispersion_weighted_reversal":  alpha_R4_dispersion_weighted_reversal,
    "R5_squeeze_reversion":             alpha_R5_squeeze_reversion,
}
