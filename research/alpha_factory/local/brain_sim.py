"""Local BRAIN-test simulator.

Given a panel of OHLCV data and a Python function that maps the panel to
cross-sectional weights, compute the same metrics BRAIN's IS test reports:
Sharpe, Fitness, Turnover, Returns, Drawdown, Sub-universe Sharpe, plus
weight distribution sanity.

API:
    panel = load_universe(tickers, start, end)        -> pd.DataFrame
    metrics = run_alpha(panel, alpha_fn, config)      -> SimMetrics
    walk_forward_metrics = walk_forward(panel, alpha_fn, n_splits=3)
    mc_metrics = monte_carlo_subuniverse(panel, alpha_fn, n_draws=100, k=30)

The alpha_fn signature is::

    def alpha_fn(panel: pd.DataFrame) -> pd.DataFrame:
        # panel has MultiIndex (date, ticker) and columns
        # ['open', 'high', 'low', 'close', 'volume', 'returns']
        # Return a pd.DataFrame with same index/columns producing
        # cross-sectional weights (we'll rank+demean+normalize).
        ...
"""
from __future__ import annotations

import math
import warnings
from dataclasses import dataclass, asdict
from typing import Callable, Iterable, Optional

import numpy as np
import pandas as pd


# Suppress noisy yfinance / pandas warnings during sim
warnings.filterwarnings("ignore", category=FutureWarning)
warnings.filterwarnings("ignore", category=UserWarning)


# ----------------------------------------------------------------------------
# Data loader
# ----------------------------------------------------------------------------

def load_universe(
    tickers: list[str],
    start: str = "2019-01-01",
    end: str = "2023-12-31",
    cache_path: Optional[str] = None,
) -> pd.DataFrame:
    """Download OHLCV via yfinance into a tidy long DataFrame.

    Returns a DataFrame indexed by (date, ticker) with columns
    open, high, low, close, volume, returns.
    """
    import yfinance as yf

    if cache_path:
        from pathlib import Path
        p = Path(cache_path)
        if p.exists():
            return pd.read_parquet(p)

    # Download all in one call, then melt
    raw = yf.download(
        tickers, start=start, end=end, group_by="ticker",
        auto_adjust=True, progress=False, threads=True,
    )
    rows = []
    for t in tickers:
        try:
            df = raw[t].copy() if t in raw else raw.copy()
        except (KeyError, ValueError):
            continue
        if df.empty:
            continue
        df = df.rename(columns=str.lower)
        df["ticker"] = t
        df["returns"] = df["close"].pct_change()
        df = df.reset_index().rename(columns={"Date": "date", "index": "date"})
        rows.append(df[["date", "ticker", "open", "high", "low", "close", "volume", "returns"]])
    if not rows:
        raise RuntimeError("yfinance returned no data")
    panel = pd.concat(rows, ignore_index=True)
    panel = panel.dropna(subset=["close"]).sort_values(["date", "ticker"]).reset_index(drop=True)
    panel = panel.set_index(["date", "ticker"])

    if cache_path:
        from pathlib import Path
        Path(cache_path).parent.mkdir(parents=True, exist_ok=True)
        panel.to_parquet(cache_path)
    return panel


# ----------------------------------------------------------------------------
# Weights -> PnL
# ----------------------------------------------------------------------------

def _crossec_weights(score_panel: pd.DataFrame) -> pd.DataFrame:
    """Cross-sectional rank + demean + L1-normalize per date.

    Returns weights summing to 0 (dollar-neutral) with sum(|w|) = 1 each day.
    """
    # rank per date
    ranked = score_panel.groupby(level="date").rank(pct=True)
    # demean per date so weights sum to 0
    demeaned = ranked - ranked.groupby(level="date").transform("mean")
    # L1-normalize per date
    abs_sum = demeaned.abs().groupby(level="date").transform("sum").replace(0, np.nan)
    weights = demeaned / abs_sum
    return weights.fillna(0.0)


@dataclass
class SimMetrics:
    sharpe: float
    fitness: float
    turnover: float
    returns_annualized: float
    max_drawdown: float
    sub_universe_sharpe_p10: float
    sub_universe_sharpe_median: float
    weight_concentration_top1pct: float
    n_days: int
    n_assets: int

    def passes_brain_is(self) -> tuple[bool, list[str]]:
        """Apply the 5 IS-test cutoffs from the BRAIN screenshot."""
        fails = []
        if self.sharpe < 1.25:
            fails.append(f"Sharpe {self.sharpe:.2f} < 1.25")
        if self.fitness < 1.0:
            fails.append(f"Fitness {self.fitness:.2f} < 1.00")
        if not (0.01 <= self.turnover <= 0.70):
            fails.append(f"Turnover {self.turnover:.2%} outside 1-70%")
        if self.sub_universe_sharpe_p10 < 0.74:
            fails.append(
                f"Sub-universe Sharpe p10 {self.sub_universe_sharpe_p10:.2f} < 0.74"
            )
        if self.weight_concentration_top1pct > 0.05:
            fails.append(
                f"top-1% weight concentration {self.weight_concentration_top1pct:.2%} > 5%"
            )
        return (len(fails) == 0, fails)


def _compute_metrics(
    weights, returns,
    *, n_subsamples: int = 50, sub_k: int = 20, seed: int = 17,
) -> SimMetrics:
    """Given dollar-neutral weights and a returns panel, compute metrics.

    `weights` and `returns` may be either Series with a (date, ticker)
    MultiIndex, or DataFrames (we coerce to Series of the first column).
    """
    # coerce to single-column Series with (date,ticker) index
    if isinstance(weights, pd.DataFrame):
        weights = weights.iloc[:, 0]
    if isinstance(returns, pd.DataFrame):
        returns = returns.iloc[:, 0]

    # Reshape both to wide (date x ticker) with aligned axes.
    w_wide = weights.unstack(level="ticker").sort_index().fillna(0.0)
    r_wide = returns.unstack(level="ticker").sort_index().fillna(0.0)
    common_cols = w_wide.columns.intersection(r_wide.columns)
    common_idx = w_wide.index.intersection(r_wide.index)
    w_wide = w_wide.loc[common_idx, common_cols]
    r_wide = r_wide.loc[common_idx, common_cols]

    # signal at t affects return at t+1
    w_shift = w_wide.shift(1).fillna(0.0)
    daily_pnl = (w_shift * r_wide).sum(axis=1)
    # use w_shift for turnover and concentration too
    w_for_to = w_shift.copy()

    # Sharpe (annualised, 252-day calendar)
    if daily_pnl.std() == 0 or len(daily_pnl) < 30:
        sharpe = 0.0
    else:
        sharpe = float(daily_pnl.mean() / daily_pnl.std() * math.sqrt(252))

    # Returns (annualised)
    total_return = float(daily_pnl.sum())
    n_days = max(len(daily_pnl), 1)
    annual_factor = 252 / n_days
    returns_annualised = total_return * annual_factor

    # Turnover: mean L1 change in weights per day, divided by sum |w| (= 1)
    turnover = float(w_for_to.diff().abs().sum(axis=1).mean())

    # Max drawdown of equity curve
    eq = (1 + daily_pnl).cumprod()
    drawdown = float((eq.cummax() - eq).max() / eq.cummax().max()) if len(eq) else 0.0

    # Fitness = Sharpe * sqrt(|returns| / turnover)
    if turnover > 0:
        fitness = float(sharpe * math.sqrt(abs(returns_annualised) / turnover))
    else:
        fitness = 0.0

    # Sub-universe Sharpe via bootstrap (resample columns)
    rng = np.random.default_rng(seed)
    sub_sharpes = []
    all_tickers = w_shift.columns.tolist()
    if len(all_tickers) >= sub_k:
        for _ in range(n_subsamples):
            pick = rng.choice(all_tickers, size=sub_k, replace=False)
            sub_pnl = (w_shift[pick] * r_wide[pick]).sum(axis=1)
            if sub_pnl.std() > 0:
                sub_sharpes.append(float(sub_pnl.mean() / sub_pnl.std() * math.sqrt(252)))
    sub_sharpes_arr = np.array(sub_sharpes) if sub_sharpes else np.array([sharpe])
    sub_p10 = float(np.percentile(sub_sharpes_arr, 10))
    sub_med = float(np.median(sub_sharpes_arr))

    # Weight concentration: mean of (max single-name |weight|) per day
    max_abs_per_day = w_shift.abs().max(axis=1)
    weight_concentration_top1pct = float(max_abs_per_day.mean())

    return SimMetrics(
        sharpe=sharpe,
        fitness=fitness,
        turnover=turnover,
        returns_annualized=returns_annualised,
        max_drawdown=drawdown,
        sub_universe_sharpe_p10=sub_p10,
        sub_universe_sharpe_median=sub_med,
        weight_concentration_top1pct=weight_concentration_top1pct,
        n_days=n_days,
        n_assets=int(w_shift.shape[1]),
    )


# ----------------------------------------------------------------------------
# Run an alpha
# ----------------------------------------------------------------------------

AlphaFn = Callable[[pd.DataFrame], pd.Series]


def run_alpha(
    panel: pd.DataFrame, alpha_fn: AlphaFn,
    *, n_subsamples: int = 50, sub_k: int = 20,
) -> SimMetrics:
    """Compute a score per (date, ticker), convert to weights, evaluate."""
    score = alpha_fn(panel)
    if isinstance(score, pd.DataFrame) and "score" in score.columns:
        score = score["score"]
    score = score.dropna()
    weights = _crossec_weights(score.to_frame("s"))["s"]
    returns = panel["returns"]
    metrics = _compute_metrics(
        weights.to_frame("w"), returns.to_frame("r"),
        n_subsamples=n_subsamples, sub_k=sub_k,
    )
    return metrics


# ----------------------------------------------------------------------------
# Walk-forward
# ----------------------------------------------------------------------------

def walk_forward(
    panel: pd.DataFrame, alpha_fn: AlphaFn,
    *, train_frac: float = 0.6,
) -> dict:
    """Split the panel chronologically; return IS / OS metrics."""
    dates = panel.index.get_level_values("date").unique().sort_values()
    cutoff = dates[int(len(dates) * train_frac)]
    is_panel = panel.loc[panel.index.get_level_values("date") < cutoff]
    os_panel = panel.loc[panel.index.get_level_values("date") >= cutoff]
    return {
        "is": run_alpha(is_panel, alpha_fn),
        "os": run_alpha(os_panel, alpha_fn),
        "cutoff": str(cutoff),
    }


# ----------------------------------------------------------------------------
# Monte Carlo over universe subsamples
# ----------------------------------------------------------------------------

def monte_carlo_subuniverse(
    panel: pd.DataFrame, alpha_fn: AlphaFn,
    *, n_draws: int = 50, k: int = 25, seed: int = 17,
) -> dict:
    """Bootstrap by drawing k tickers from the full universe; report Sharpe dist."""
    rng = np.random.default_rng(seed)
    all_tickers = panel.index.get_level_values("ticker").unique().tolist()
    if len(all_tickers) <= k:
        return {"note": f"universe too small ({len(all_tickers)}<=k)"}
    sharpes = []
    for _ in range(n_draws):
        pick = rng.choice(all_tickers, size=k, replace=False)
        sub = panel[panel.index.get_level_values("ticker").isin(pick)]
        try:
            m = run_alpha(sub, alpha_fn, n_subsamples=10, sub_k=min(k - 5, 15))
            sharpes.append(m.sharpe)
        except Exception:
            continue
    if not sharpes:
        return {"note": "all draws failed"}
    arr = np.array(sharpes)
    return {
        "n_draws": len(arr),
        "p10": float(np.percentile(arr, 10)),
        "p50": float(np.percentile(arr, 50)),
        "p90": float(np.percentile(arr, 90)),
        "mean": float(arr.mean()),
        "std": float(arr.std()),
        "frac_above_125": float((arr >= 1.25).mean()),
    }
