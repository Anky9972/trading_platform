"""End-to-end local validation harness.

Run::

    python -m research.alpha_factory.local.run_validation

It will:
  1. Download a 50-stock liquid US universe (cached on disk).
  2. Run each candidate alpha through:
       - full-sample backtest
       - walk-forward (60% IS / 40% OS)
       - Monte Carlo subuniverse bootstrap
  3. Print a verdict table aligned with the BRAIN IS test cutoffs.
"""
from __future__ import annotations

import json
import sys
import time
from dataclasses import asdict
from pathlib import Path

import pandas as pd

from .brain_sim import (
    load_universe, run_alpha, walk_forward, monte_carlo_subuniverse,
)
from .candidate_alphas import CANDIDATES
from .candidate_alphas_v2 import REFINED_CANDIDATES

# ~150-stock liquid US universe spanning large + mid cap, all 11 GICS sectors.
# Wider universe = stronger cross-sectional dispersion -- closer in spirit to
# TOP3000. Stocks must have continuous coverage 2019-2023 (no IPOs, delistings).
UNIVERSE = [
    # Tech / Communications (40)
    "AAPL", "MSFT", "GOOGL", "GOOG", "META", "NVDA", "AMD", "INTC", "CSCO", "ORCL",
    "ADBE", "CRM", "AVGO", "QCOM", "TXN", "IBM", "INTU", "NOW", "AMAT", "MU",
    "LRCX", "KLAC", "ADI", "MRVL", "PANW", "FTNT", "SNPS", "CDNS", "WDAY", "ANET",
    "VZ", "T", "CMCSA", "DIS", "TMUS", "NFLX", "CHTR", "EA", "TTWO", "ATVI",
    # Finance / Insurance (25)
    "JPM", "BAC", "WFC", "GS", "MS", "C", "USB", "BLK", "SCHW", "AXP",
    "PNC", "TFC", "COF", "BK", "STT", "FITB", "RF", "HBAN", "KEY", "MTB",
    "SPGI", "MCO", "ICE", "CME", "MMC",
    # Health Care (20)
    "JNJ", "UNH", "PFE", "MRK", "ABBV", "LLY", "TMO", "DHR", "BMY", "ABT",
    "AMGN", "GILD", "REGN", "VRTX", "BIIB", "CVS", "CI", "HUM", "ELV", "ISRG",
    # Consumer Discretionary / Staples (25)
    "AMZN", "TSLA", "WMT", "COST", "HD", "PG", "KO", "PEP", "MCD", "NKE",
    "SBUX", "TGT", "LOW", "TJX", "BKNG", "F", "GM", "ROST", "DG", "DLTR",
    "MO", "PM", "CL", "KMB", "GIS",
    # Energy (10)
    "XOM", "CVX", "COP", "EOG", "SLB", "PSX", "VLO", "MPC", "OXY", "PXD",
    # Industrials (15)
    "CAT", "BA", "GE", "HON", "UPS", "FDX", "DE", "RTX", "LMT", "GD",
    "NOC", "MMM", "ETN", "EMR", "ITW",
    # Materials / Real Estate / Utilities (15)
    "LIN", "APD", "ECL", "SHW", "FCX", "NEM",
    "AMT", "PLD", "EQIX", "CCI",
    "NEE", "DUK", "SO", "AEP", "EXC",
]

CACHE = Path(__file__).parent.parent / "data" / "universe_150_2019_2023.parquet"


def main() -> int:
    t0 = time.time()
    print(f"[1/3] loading universe of {len(UNIVERSE)} stocks "
          f"(cached at {CACHE})...")
    panel = load_universe(
        UNIVERSE, start="2019-01-01", end="2023-12-31",
        cache_path=str(CACHE),
    )
    n_dates = panel.index.get_level_values("date").nunique()
    n_tickers = panel.index.get_level_values("ticker").nunique()
    print(f"      panel: {len(panel):,} rows, {n_dates} dates, {n_tickers} tickers "
          f"({time.time() - t0:.1f}s)")

    # Use refined candidates (v2). Set USE_V1=1 in env to reproduce the
    # original 5 for comparison.
    import os
    candidates = CANDIDATES if os.environ.get("USE_V1") else REFINED_CANDIDATES
    print(f"\n[2/3] running {len(candidates)} candidates "
          f"({'v1' if os.environ.get('USE_V1') else 'refined v2'})...")
    rows = []
    for name, fn in candidates.items():
        t1 = time.time()
        full = run_alpha(panel, fn)
        wf = walk_forward(panel, fn, train_frac=0.6)
        mc = monte_carlo_subuniverse(panel, fn, n_draws=30, k=30)
        passes_full, fails_full = full.passes_brain_is()
        passes_os, fails_os = wf["os"].passes_brain_is()
        rows.append({
            "alpha": name,
            "full_sharpe": full.sharpe,
            "full_fitness": full.fitness,
            "full_turnover": full.turnover,
            "full_returns": full.returns_annualized,
            "full_dd": full.max_drawdown,
            "full_subuniv_p10": full.sub_universe_sharpe_p10,
            "full_pass": passes_full,
            "full_fails": "; ".join(fails_full),
            "is_sharpe": wf["is"].sharpe,
            "os_sharpe": wf["os"].sharpe,
            "os_fitness": wf["os"].fitness,
            "os_turnover": wf["os"].turnover,
            "os_pass": passes_os,
            "os_fails": "; ".join(fails_os),
            "mc_p10": mc.get("p10"),
            "mc_p50": mc.get("p50"),
            "mc_p90": mc.get("p90"),
            "mc_frac_pass125": mc.get("frac_above_125"),
        })
        print(f"  {name:35s}  full_sharpe={full.sharpe:5.2f}  "
              f"fit={full.fitness:5.2f}  to={full.turnover:.0%}  "
              f"os_sharpe={wf['os'].sharpe:5.2f}  ({time.time()-t1:.1f}s)")

    df = pd.DataFrame(rows)

    # ---- Print pretty summary ----
    print(f"\n[3/3] verdict table (total runtime {time.time()-t0:.1f}s)\n")
    print("=" * 110)
    print(f"{'Alpha':36s} {'Sharpe':>7s} {'Fit':>5s} {'TO':>6s} {'Ret':>6s} "
          f"{'DD':>5s} {'OSshp':>6s} {'MCp10':>6s} {'PASS?':>6s}")
    print("-" * 110)
    for _, r in df.iterrows():
        ok = "PASS" if r["full_pass"] else "FAIL"
        print(
            f"{r['alpha']:36s} "
            f"{r['full_sharpe']:7.2f} "
            f"{r['full_fitness']:5.2f} "
            f"{r['full_turnover']:5.1%} "
            f"{r['full_returns']:5.1%} "
            f"{r['full_dd']:4.1%} "
            f"{r['os_sharpe']:6.2f} "
            f"{(r['mc_p10'] or 0):6.2f} "
            f"{ok:>6s}"
        )
    print("=" * 110)

    # detailed fail reasons
    print("\nFailure reasons (full-sample):")
    for _, r in df.iterrows():
        if r["full_fails"]:
            print(f"  {r['alpha']}: {r['full_fails']}")
    print("\nFailure reasons (out-of-sample):")
    for _, r in df.iterrows():
        if r["os_fails"]:
            print(f"  {r['alpha']}: {r['os_fails']}")

    # save
    out = Path(__file__).parent.parent / "data" / "validation_results.json"
    df.to_json(out, orient="records", indent=2)
    print(f"\nresults saved to {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
