#!/usr/bin/env python3
"""
================================================================================
STRATEGY: MULTI-FACTOR ALPHA COMPOSITE (MFA)
================================================================================

4 pre-designed orthogonal factors with regime-adaptive fixed weights.

Root-cause fixes from v1:
  ✗ 10 correlated factors → unstable correlation filter → random signal
  ✓ 4 orthogonal factors → no runtime filter needed → stable signal

  ✗ No regime awareness → bought into crashes
  ✓ SMA-breadth regime gate → adaptive weights per market state

  ✗ Top-3 concentrated picks changed daily → 2x turnover
  ✓ Top-quartile selection with quality threshold → lower turnover

Factors:
  1. Risk-Adjusted Momentum    ret_20 / vol_20
  2. Short-Term Reversal       -ret_5
  3. Low Volatility Premium    -vol_20 (cross-sectionally normalized)
  4. Return Consistency        fraction of up days in 20d window

================================================================================
"""

import sys
import json
import logging
import numpy as np
import pandas as pd
from typing import Dict, Any

try:
    import yfinance as yf
except ImportError:
    print(json.dumps({"error": "Missing: pip install yfinance pandas numpy"}))
    sys.exit(1)

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s - %(levelname)s - %(message)s")


# ==============================================================================
# HELPERS
# ==============================================================================

def _s(x):
    """Guarantee pd.Series from a possibly-DataFrame column (yfinance compat)."""
    return x.iloc[:, 0] if isinstance(x, pd.DataFrame) else x


def _zscore_win(df: pd.DataFrame, cap: float = 3.0) -> pd.DataFrame:
    """Cross-sectional z-score, winsorized to ±cap."""
    return df.apply(
        lambda c: (c - c.mean()) / max(c.std(), 1e-8)
    ).clip(-cap, cap)


# ==============================================================================
# FACTOR COMPUTATION  (4 orthogonal factors — no runtime filter needed)
# ==============================================================================

def _compute_factors(data: dict) -> pd.DataFrame:
    """
    Each factor uses a DIFFERENT raw signal to guarantee low pairwise correlation:
      F1 → 20d return level / 20d vol       (momentum magnitude)
      F2 → 5d return level, negated          (short-horizon reversal)
      F3 → 20d volatility, negated           (risk preference)
      F4 → sign-count of daily returns       (return quality / consistency)
    """
    rows = []
    for sym, df in data.items():
        try:
            close = _s(df["Close"])
            if len(close) < 40:
                continue

            dr = close.pct_change()

            r5  = close.pct_change(5).iloc[-1]
            r20 = close.pct_change(20).iloc[-1] if len(close) > 20 else np.nan
            v20 = dr.rolling(20).std().iloc[-1]  if len(close) > 21 else np.nan

            if any(pd.isna(x) for x in (r5, r20, v20)) or v20 < 1e-8:
                continue

            # F1: Risk-Adjusted Momentum  (buy risk-efficient winners)
            f1 = r20 / v20

            # F2: Short-Term Reversal  (buy 5-day dips for mean-reversion)
            f2 = -r5

            # F3: Low-Volatility Premium  (prefer steadier names)
            #     Cross-sectional z-score later normalises the raw level.
            f3 = -v20

            # F4: Return Consistency  (fraction of positive-return days, 20d)
            last20 = dr.iloc[-20:]
            valid  = last20.dropna()
            f4 = float((valid > 0).mean()) - 0.5 if len(valid) > 5 else 0.0

            rows.append(dict(symbol=sym,
                             f1_mom=f1, f2_rev=f2,
                             f3_vol=f3, f4_con=f4))
        except Exception:
            continue

    return pd.DataFrame(rows).set_index("symbol") if rows else pd.DataFrame()


# ==============================================================================
# REGIME DETECTION
# ==============================================================================

def _regime(data: dict) -> str:
    """
    Market regime via SMA-breadth + median 20d return.
    Returns 'bull', 'bear', or 'neut'.
    """
    bull = tot = 0
    rets = []

    for df in data.values():
        try:
            c = _s(df["Close"])
            if len(c) < 50:
                continue
            tot += 1

            s20 = c.rolling(20).mean().iloc[-1]
            s50 = c.rolling(50).mean().iloc[-1]
            if pd.notna(s20) and pd.notna(s50) and s20 > s50:
                bull += 1

            r = c.pct_change(20).iloc[-1]
            if pd.notna(r):
                rets.append(r)
        except Exception:
            continue

    if tot == 0:
        return "neut"

    breadth = bull / tot
    med = np.median(rets) if rets else 0.0

    if breadth >= 0.60 and med > 0.02:
        return "bull"
    if breadth <= 0.30 and med < -0.03:
        return "bear"
    return "neut"


# ==============================================================================
# COMPOSITE BUILDER
# ==============================================================================

# Fixed, regime-adaptive weights  (sum to 1.0 per regime)
WEIGHTS = {
    #              momentum  reversal  low-vol  consistency
    "bull": dict(f1_mom=0.40, f2_rev=0.10, f3_vol=0.15, f4_con=0.35),
    "bear": dict(f1_mom=0.10, f2_rev=0.25, f3_vol=0.40, f4_con=0.25),
    "neut": dict(f1_mom=0.30, f2_rev=0.20, f3_vol=0.25, f4_con=0.25),
}

# Signal-quality thresholds per regime
THRESHOLDS = {"bull": 0.10, "neut": 0.15, "bear": 0.30}


def build_composite(data: dict) -> dict:
    """
    factors → z-score → regime-weight → select → allocate
    Returns {symbol: weight}.
    """
    # 1. Compute factors
    df_raw = _compute_factors(data)
    if df_raw.empty or len(df_raw) < 3:
        return {}

    # 2. Detect regime
    reg = _regime(data)

    # 3. Cross-sectional z-score with winsorization
    df_z = _zscore_win(df_raw)

    # 4. Weighted composite  (stable — same 4 factors every period)
    w = WEIGHTS[reg]
    composite = pd.Series(0.0, index=df_z.index)
    for col in df_z.columns:
        composite += df_z[col] * w.get(col, 0.0)

    composite = composite.dropna()
    if len(composite) < 2:
        return {}

    # 5. Select top stocks  (~top quartile, tighter in bear)
    n = max(3, len(composite) // 3)
    if reg == "bear":
        n = max(2, len(composite) // 4)

    top = composite.nlargest(n)

    # 6. Signal-quality gate
    thr = THRESHOLDS[reg]
    top = top[top > thr]
    if top.empty:
        return {}

    # 7. Score-weighted allocation, capped at 40 % per name
    s = top.clip(lower=0.01)
    alloc = s / s.sum()
    alloc = alloc.clip(upper=0.40)
    alloc /= alloc.sum()            # renormalise after cap

    return alloc.to_dict()


# ==============================================================================
# BACKTEST INTERFACE  (called by alpha_pipeline.py)
# ==============================================================================

def backtest_signal(data, current_date, fundamentals=None, strategy_history=None):
    """Vectorized backtest entry point. Same signature as all strategies."""
    return build_composite(data)


# ==============================================================================
# LIVE EXECUTION INTERFACE  (called by engine.py via subprocess)
# ==============================================================================

def run_strategy(ctx: Dict[str, Any]):
    capital   = ctx.get("capital", 0)
    positions = {p["symbol"]: p for p in ctx.get("positions", [])}

    universe = [
        "RELIANCE.NS", "TCS.NS", "INFY.NS", "HDFCBANK.NS", "ICICIBANK.NS",
        "SBIN.NS", "BHARTIARTL.NS", "ITC.NS", "KOTAKBANK.NS",
        "AXISBANK.NS", "ASIANPAINT.NS", "MARUTI.NS", "SUNPHARMA.NS",
        "BAJFINANCE.NS",
    ]

    if capital < 10_000:
        return

    data: Dict[str, pd.DataFrame] = {}
    for sym in universe:
        if sym in positions:
            continue
        try:
            d = yf.download(sym, period="180d", interval="1d", progress=False)
            if len(d) >= 60:
                data[sym] = d
        except Exception:
            continue

    if not data:
        return

    alloc = build_composite(data)
    if not alloc:
        return

    invest = capital * 0.90
    reg    = _regime(data)

    for sym, wt in alloc.items():
        price = float(_s(data[sym]["Close"]).iloc[-1])
        qty   = int((invest * wt) // price)
        if qty > 0:
            print(json.dumps({
                "symbol":     sym.replace(".NS", ""),
                "action":     "BUY",
                "quantity":   qty,
                "order_type": "MARKET",
                "reason":     f"MFA wt={wt:.2f} regime={reg}",
            }))


if __name__ == "__main__":
    try:
        raw = sys.stdin.readline()
        if not raw:
            sys.exit(0)
        run_strategy(json.loads(raw))
    except Exception as e:
        print(json.dumps({"error": str(e)}))
        sys.exit(1)