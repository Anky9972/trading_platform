#!/usr/bin/env python3
"""
=============================================================================
ALPHA RESEARCH & DEPLOYMENT PIPELINE
=============================================================================
End-to-end flow:
  1. User submits a new alpha → benchmark against all existing alphas
  2. Rate alpha, suggest improvements if underperforming
  3. If outperforming → scan asset universe (WF, MC 10K, metrics)
  4. Generate orders → risk check → execute → persist to DB

Usage:
  python scripts/alpha_pipeline.py --alpha strategy/user_strategies/my_alpha.py
  python scripts/alpha_pipeline.py --alpha strategy/user_strategies/my_alpha.py --dry-run
  python scripts/alpha_pipeline.py --alpha strategy/user_strategies/my_alpha.py --capital 500000
=============================================================================
"""
import os
import sys
import importlib.util
import argparse
import logging
import json
from datetime import datetime

# Add project root to path
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

import numpy as np
import pandas as pd

import warnings
warnings.filterwarnings("ignore")
import yfinance as yf

from core.signals import calculate_volatility_dispersion_scores, calculate_liquidity_imbalance_metrics

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | PIPELINE | %(levelname)-8s | %(message)s'
)
logger = logging.getLogger("alpha_pipeline")

# ── Universe selection ────────────────────────────────────────────────────
# ─── Universe (module-level constant, used as default) ────────────────────────
NIFTY_UNIVERSE = [
    # Financials
    "HDFCBANK.NS", "ICICIBANK.NS", "SBIN.NS", "KOTAKBANK.NS", "AXISBANK.NS",
    "BAJFINANCE.NS", "BAJAJFINSV.NS", "HDFCLIFE.NS", "SBILIFE.NS", "INDUSINDBK.NS",
    # IT
    "TCS.NS", "INFY.NS", "HCLTECH.NS", "WIPRO.NS", "TECHM.NS",
    # Energy & Oil
    "RELIANCE.NS", "ONGC.NS", "BPCL.NS", "IOC.NS", "COALINDIA.NS",
    # Consumer & FMCG
    "HINDUNILVR.NS", "ITC.NS", "NESTLEIND.NS", "BRITANNIA.NS", "TATACONSUM.NS",
    # Auto
    "MARUTI.NS",  "BAJAJ-AUTO.NS", "HEROMOTOCO.NS", "EICHERMOT.NS",
    # Pharma
    "SUNPHARMA.NS", "DRREDDY.NS", "CIPLA.NS", "DIVISLAB.NS",
    # Industrials & Infra
    "LT.NS", "ADANIPORTS.NS", "ULTRACEMCO.NS", "GRASIM.NS", "SHREECEM.NS",
    # Metals
    "TATASTEEL.NS", "JSWSTEEL.NS", "HINDALCO.NS",
    # Telecom
    "BHARTIARTL.NS",
    # Consumer Discretionary
    "ASIANPAINT.NS", "TITAN.NS", 
    # Power
    "NTPC.NS", "POWERGRID.NS",
    # Conglomerate
    "ADANIENT.NS",
    # Chemicals
    # "UPL.NS",
]

# UNIVERSE_ALIASES = {
#     # Nifty 50 aliases
#     "NIFTY":    NIFTY_UNIVERSE,
#     "NIFTY50":  NIFTY_UNIVERSE,
#     "NIFTY-50": NIFTY_UNIVERSE,
#     "N50":      NIFTY_UNIVERSE,
#     "NSE":      NIFTY_UNIVERSE,
#     "INDIA":    NIFTY_UNIVERSE,
#     # US aliases
#     "US":       ["AAPL", "MSFT", "GOOGL", "AMZN", "NVDA",
#                 "TSLA", "META", "BRK-B", "JPM", "V"],
#     "SP500":    ["AAPL", "MSFT", "GOOGL", "AMZN", "NVDA",
#                 "TSLA", "META", "BRK-B", "JPM", "V"],
#     "US_TECH":  ["AAPL", "MSFT", "GOOGL", "NVDA", "META", "TSLA"],
#     # Crypto aliases
#     "CRYPTO":   ["BTC-USD", "ETH-USD", "BNB-USD", "SOL-USD",
#                 "XRP-USD", "DOGE-USD"],
# }
# args = parser.parse_args()
# raw_universe_arg = args.universe.strip().upper().replace(" ", "")

# if raw_universe_arg in UNIVERSE_ALIASES:
#     # Known preset
#     universe = UNIVERSE_ALIASES[raw_universe_arg]
#     print(f"  Universe: {raw_universe_arg} → {len(universe)} symbols")

# else:
#     # Custom comma-separated list  e.g.  "RELIANCE,TCS,INFY"
#     universe = []
#     for s in raw_universe_arg.split(","):
#         s = s.strip()
#         if not s:
#             continue
#         # Already has exchange suffix → use as-is
#         if "." in s or "-" in s:
#             universe.append(s)
#         # Looks like a crypto ticker
#         elif s.endswith("USD") or s.endswith("BTC"):
#             universe.append(f"{s[:3]}-{s[3:]}")   # BTCUSD → BTC-USD
#         # Default: assume NSE
#         else:
#             universe.append(f"{s}.NS")

#     if not universe:
#         print("  ✗ Could not parse universe argument. "
#             "Use: NIFTY, US, CRYPTO, or comma-separated tickers.")
#         sys.exit(1)

#     print(f"  Universe: custom → {len(universe)} symbols: {universe}")

# =============================================================================
# PHASE 1: AUTO-DISCOVER & BENCHMARK
# =============================================================================

# =============================================================================
# AUTO-ADAPTER: wrap run_strategy(context) → backtest_signal(data, date, ...)
# =============================================================================

def _create_backtest_adapter(mod):
    """
    Auto-wraps a run_strategy(context) or main() function into the
    backtest_signal(data, current_date, ...) interface by:
      1. Mocking yf.download / yf.Ticker to return pre-loaded sliced data
      2. Capturing stdout JSON orders
      3. Parsing BUY orders into equal-weight allocations
    """
    # Find entry point
    if hasattr(mod, "run_strategy"):
        entry_fn = mod.run_strategy
        call_mode = "context"        # expects dict argument
    elif hasattr(mod, "main"):
        entry_fn = mod.main
        call_mode = "noarg"          # no arguments
    else:
        return None

    def backtest_signal(data, current_date, fundamentals=None, strategy_history=None):
        import io
        import contextlib

        # ── 1. Slice every symbol's data up to current_date ──
        sliced = {}
        for sym, df in data.items():
            cut = df.loc[:current_date]
            if len(cut) >= 10:
                sliced[sym] = cut

        if not sliced:
            return {}

        # ── 2. Build fast symbol lookup (.NS / .BO / bare) ──
        sym_lookup = {}
        for sym in sliced:
            sym_lookup[sym] = sym
            bare = sym.replace(".NS", "").replace(".BO", "")
            sym_lookup[bare] = sym
            if not sym.endswith(".NS"):
                sym_lookup[sym + ".NS"] = sym

        # ── 3. Mock yfinance.download ──
        orig_download = yf.download

        def _mock_download(symbol, *args, **kwargs):
            key = sym_lookup.get(symbol)
            if key is None:
                # Try adding .NS
                key = sym_lookup.get(symbol + ".NS")
            if key and key in sliced:
                return sliced[key].copy()
            return pd.DataFrame()

        # ── 4. Mock yfinance.Ticker ──
        orig_ticker = getattr(yf, "Ticker", None)

        class _MockTicker:
            def __init__(self, symbol):
                self.ticker = symbol
                key = sym_lookup.get(symbol) or sym_lookup.get(symbol + ".NS")
                self._df = sliced.get(key, pd.DataFrame()) if key else pd.DataFrame()
                last_price = float(self._df["Close"].iloc[-1]) if len(self._df) > 0 else 0
                self.info = {
                    "currentPrice": last_price,
                    "regularMarketPrice": last_price,
                    "regularMarketPreviousClose": last_price,
                }

            def history(self, **kwargs):
                return self._df.copy()

            def download(self, **kwargs):
                return self._df.copy()

        # ── 5. Build mock context for run_strategy ──
        context = {
            "capital": 1_000_000,
            "positions": [],
            "portfolio_value": 1_000_000,
            "available_cash": 1_000_000,
        }

        # ── 6. Run with mocks, capture stdout ──
        captured = io.StringIO()
        try:
            yf.download = _mock_download
            if orig_ticker is not None:
                yf.Ticker = _MockTicker

            with contextlib.redirect_stdout(captured):
                if call_mode == "context":
                    entry_fn(context)
                else:
                    entry_fn()

        except SystemExit:
            pass                     # some main() call sys.exit
        except Exception:
            pass                     # don't crash the pipeline
        finally:
            yf.download = orig_download
            if orig_ticker is not None:
                yf.Ticker = orig_ticker

        # ── 7. Parse JSON lines from stdout into weights ──
        buy_syms = []
        for line in captured.getvalue().strip().split("\n"):
            line = line.strip()
            if not line:
                continue
            try:
                order = json.loads(line)
                if not isinstance(order, dict):
                    continue
                if order.get("error"):
                    continue
                if order.get("action", "").upper() != "BUY":
                    continue

                raw = order.get("symbol", "")
                if not raw:
                    continue

                resolved = sym_lookup.get(raw) or sym_lookup.get(raw + ".NS")
                if resolved:
                    buy_syms.append(resolved)

            except (json.JSONDecodeError, TypeError, ValueError):
                continue

        if not buy_syms:
            return {}

        # De-duplicate and equal-weight
        unique = list(dict.fromkeys(buy_syms))  # preserves order
        w = 1.0 / len(unique)
        return {s: w for s in unique}

    return backtest_signal


# =============================================================================
# DISCOVERY (updated)
# =============================================================================

def discover_alphas(strategy_dir: str, exclude_file: str = None) -> dict:
    """
    Scan user_strategies/ for alpha files.
    Priority 1: native backtest_signal()
    Priority 2: auto-adapt run_strategy() / main()
    """
    alphas = {}
    stats = {"native": 0, "adapted": 0, "failed_import": 0, "no_signal": 0}

    for fname in sorted(os.listdir(strategy_dir)):
        if not fname.endswith(".py") or fname == "__init__.py":
            continue

        fpath = os.path.join(strategy_dir, fname)
        name = fname.replace(".py", "")

        # Skip submitted alpha (loaded separately)
        if exclude_file and os.path.abspath(fpath) == os.path.abspath(exclude_file):
            logger.info(f"  Skipped {fname} (submitted alpha)")
            continue

        # ── Try to import ──
        try:
            spec = importlib.util.spec_from_file_location(name, fpath)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
        except Exception as e:
            logger.warning(f"  IMPORT FAILED: {fname} → {type(e).__name__}: {e}")
            stats["failed_import"] += 1
            continue

        # ── Priority 1: native backtest_signal() ──
        if hasattr(mod, "backtest_signal"):
            alphas[name] = mod.backtest_signal
            logger.info(f"  ✓ Discovered (native):  {fname}")
            stats["native"] += 1
            continue

        # ── Priority 2: auto-adapt run_strategy() / main() ──
        adapter = _create_backtest_adapter(mod)
        if adapter is not None:
            alphas[name] = adapter
            entry = "run_strategy" if hasattr(mod, "run_strategy") else "main"
            logger.info(f"  ✓ Discovered (adapted): {fname}  ← wrapped {entry}()")
            stats["adapted"] += 1
            continue

        # ── Nothing usable ──
        public_funcs = [
            n for n in dir(mod)
            if callable(getattr(mod, n, None)) and not n.startswith("_")
        ]
        logger.warning(
            f"  ✗ NO usable signal in {fname} — "
            f"found: {public_funcs[:10]}"
        )
        stats["no_signal"] += 1

    # Summary
    logger.info(
        f"  Discovery summary: {stats['native']} native, {stats['adapted']} auto-adapted, "
        f"{stats['failed_import']} import failures, {stats['no_signal']} unusable"
    )

    return alphas


def load_submitted_alpha(alpha_path: str):
    """Load the user-submitted alpha file."""
    spec = importlib.util.spec_from_file_location("submitted_alpha", alpha_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    if not hasattr(mod, "backtest_signal"):
        raise ValueError(
            f"{alpha_path} does not have a backtest_signal() function. "
            f"Add one with signature: backtest_signal(data, current_date, fundamentals=None, strategy_history=None) -> dict"
        )
    return mod.backtest_signal


def fetch_universe_data(symbols: list, years: int = 3) -> dict:
    """Fetch historical data for all symbols."""
    from datetime import timedelta
    end = datetime.now()
    start = end - timedelta(days=365 * years)
    data = {}
    for sym in symbols:
        try:
            df = yf.download(sym, start=start, end=end, progress=False)
            if not df.empty:
                if isinstance(df.columns, pd.MultiIndex):
                    df.columns = df.columns.get_level_values(0)
                data[sym] = df
        except Exception as e:
            logger.warning(f"  Failed to fetch {sym}: {e}")
    return data


def run_backtest(data: dict, signal_func, drawdown_limit: float = 0.15) -> dict:
    all_dates = set()
    for df in data.values():
        all_dates.update(df.index)
    dates = sorted(list(all_dates))
    if len(dates) < 60:
        return {"Sharpe": 0, "Sortino": 0, "CAGR": "0%", "MaxDD": "0%",
                "Turnover": 0, "WinRate": 0}

    test_dates = dates[30:]
    portfolio_value = 100000.0
    equity = [portfolio_value]
    returns = []
    prev_weights = {}
    total_turnover = 0
    hits = 0
    exposures = 0

    for i in range(len(test_dates) - 2):  # changed from -1 to -2
        signal_date = test_dates[i]       # date we OBSERVE and compute signal
        entry_date  = test_dates[i + 1]   # date we ENTER the trade (next day)
        exit_date   = test_dates[i + 2]   # date we EXIT (day after entry)

        # Build history up to signal_date ONLY
        # Strategy cannot see entry_date or exit_date prices
        hist = {
            s: df.loc[:signal_date]
            for s, df in data.items()
            if signal_date in df.index
        }
        if not hist:
            returns.append(0)
            equity.append(portfolio_value)
            continue

        # Get signal based on data up to signal_date
        try:
            weights = signal_func(hist, signal_date)
        except Exception:
            weights = {}

        # Turnover
        for s in set(list(weights.keys()) + list(prev_weights.keys())):
            total_turnover += abs(weights.get(s, 0) - prev_weights.get(s, 0))
        prev_weights = dict(weights)

        # PnL: enter at entry_date close, exit at exit_date close
        day_ret = 0.0
        for sym, w in weights.items():
            if w <= 0 or sym not in data:
                continue
            df = data[sym]
            if entry_date not in df.index or exit_date not in df.index:
                continue

            # FIXED: entry is next day after signal, not same day
            p0 = df.loc[entry_date, 'Close']
            p1 = df.loc[exit_date,  'Close']

            # Safe scalar extraction
            if hasattr(p0, 'iloc'):
                p0 = float(p0.iloc[0])
            if hasattr(p1, 'iloc'):
                p1 = float(p1.iloc[0])

            p0 = float(p0)
            p1 = float(p1)

            if p0 <= 0:
                continue

            r = (p1 - p0) / p0
            day_ret += w * float(r)
            exposures += 1
            if r > 0:
                hits += 1

        # Transaction cost
        day_ret -= 0.001 * total_turnover / max(len(test_dates), 1)

        portfolio_value *= (1 + day_ret)
        returns.append(day_ret)
        equity.append(portfolio_value)

        # Circuit breaker
        peak = max(equity)
        dd = (peak - portfolio_value) / peak
        if dd > drawdown_limit:
            break

    # Metrics
    returns = np.array(returns)
    if len(returns) < 2 or returns.std() == 0:
        return {"Sharpe": 0, "Sortino": 0, "CAGR": "0%", "MaxDD": "0%",
                "Turnover": total_turnover, "WinRate": 0}

    sharpe   = (returns.mean() / returns.std()) * np.sqrt(252)
    downside = returns[returns < 0]
    sortino  = (returns.mean() / downside.std()) * np.sqrt(252) if len(downside) > 1 else 0

    total_days  = len(returns)
    years       = total_days / 252
    total_ret   = equity[-1] / equity[0]
    cagr        = (total_ret ** (1 / years) - 1) if years > 0 else 0

    peak_arr = np.maximum.accumulate(equity)
    dd_arr   = (np.array(equity) - peak_arr) / peak_arr
    max_dd   = dd_arr.min()

    win_rate = hits / exposures if exposures > 0 else 0

    # SANITY CHECKS
    if abs(cagr) > 5.0:
        logger.warning(f"SANITY FAIL: CAGR {cagr:.0%} > 500% — likely bug, zeroing")
        sharpe = 0.0
        sortino = 0.0
        cagr = 0.0

    if abs(sharpe) > 5.0:
        logger.warning(f"SANITY FAIL: Sharpe {sharpe:.2f} > 5.0 — likely bug, zeroing")
        sharpe = 0.0

    return {
        "Sharpe":  round(sharpe, 2),
        "Sortino": round(sortino, 2),
        "CAGR":    f"{cagr:.2%}",
        "MaxDD":   f"{max_dd:.2%}",
        "Turnover": round(total_turnover, 1),
        "WinRate":  round(win_rate, 3),
        "returns":  returns,
    }


def print_benchmark_table(results: dict, submitted_name: str):
    """Print a formatted comparison table."""
    print("\n" + "=" * 90)
    print("COMPARATIVE ALPHA BENCHMARK")
    print("=" * 90)
    header = f"{'Strategy':30} | {'Sharpe':>8} | {'Sortino':>8} | {'CAGR':>10} | {'MaxDD':>10} | {'WinRate':>8}"
    print(header)
    print("-" * 90)
    for name, metrics in sorted(results.items(), key=lambda x: x[1].get("Sharpe", 0), reverse=True):
        marker = " ◀ SUBMITTED" if name == submitted_name else ""
        print(f"{name:30} | {metrics['Sharpe']:8.2f} | {metrics['Sortino']:8.2f} | "
              f"{metrics['CAGR']:>10} | {metrics['MaxDD']:>10} | {metrics['WinRate']:8.1%}{marker}")
    print("=" * 90)


def suggest_improvements(submitted: dict, all_results: dict, submitted_name: str):
    """Analyze why alpha underperforms and suggest fixes."""
    other_sharpes = [r["Sharpe"] for n, r in all_results.items() if n != submitted_name]
    median_sharpe = np.median(other_sharpes) if other_sharpes else 0

    other_turnovers = [r["Turnover"] for n, r in all_results.items() if n != submitted_name]
    median_turnover = np.median(other_turnovers) if other_turnovers else 0

    other_maxdd = [abs(float(r["MaxDD"].replace("%", "")) / 100) for n, r in all_results.items() if n != submitted_name]
    median_dd = np.median(other_maxdd) if other_maxdd else 0

    print("\n" + "!" * 70)
    print("ALPHA IMPROVEMENT SUGGESTIONS")
    print("!" * 70)

    suggestions = []

    if submitted["Sharpe"] < median_sharpe * 0.5:
        suggestions.append("  ▸ Sharpe is significantly below median. Consider adding a REGIME FILTER "
                           "(e.g., SMA200 trend) to avoid trading in adverse conditions.")

    if submitted["Turnover"] > median_turnover * 2 and median_turnover > 0:
        suggestions.append("  ▸ Turnover is 2x+ the median. REDUCE TRADE FREQUENCY by increasing "
                           "signal thresholds or adding a holding period filter.")

    sub_dd = abs(float(submitted["MaxDD"].replace("%", "")) / 100)
    if sub_dd > median_dd * 1.5 and median_dd > 0:
        suggestions.append("  ▸ Drawdown is excessive. Add STOP-LOSS logic or reduce per-position "
                           "sizing to cap individual stock exposure.")

    if submitted["WinRate"] < 0.45:
        suggestions.append("  ▸ Win rate is below 45%. INCREASE SIGNAL THRESHOLD to filter only "
                           "high-conviction setups. Quality over quantity.")

    if submitted["Sharpe"] < 0:
        suggestions.append("  ▸ Negative Sharpe. The signal may be INVERTED. Try flipping BUY/SELL "
                           "logic or check for look-ahead bias in feature construction.")

    if not suggestions:
        suggestions.append("  ▸ Alpha is close to median but not beating it. Try ENSEMBLE: "
                           "combine this signal with an uncorrelated alpha for diversification.")

    for s in suggestions:
        print(s)
    print("!" * 70)


# =============================================================================
# PHASE 2: ASSET UNIVERSE SCANNING
# =============================================================================

def walk_forward_per_asset(data_single: dict, signal_func, n_phases: int = 3) -> list:
    """Run walk-forward on a single asset. Returns list of per-phase Sharpes."""
    dates = sorted(data_single[list(data_single.keys())[0]].index)
    if len(dates) < 90:
        return [0.0]
    phase_len = len(dates) // n_phases
    phase_sharpes = []

    for p in range(n_phases):
        start = p * phase_len
        end = min((p + 1) * phase_len, len(dates))
        phase_dates = dates[start:end]
        if len(phase_dates) < 30:
            phase_sharpes.append(0.0)
            continue

        phase_data = {s: df.loc[phase_dates[0]:phase_dates[-1]] for s, df in data_single.items()}
        result = run_backtest(phase_data, signal_func)
        phase_sharpes.append(result["Sharpe"])

    return phase_sharpes


def monte_carlo_asset(returns: np.ndarray, iterations: int = 10000) -> dict:
    """Run MC simulation on raw daily returns."""
    if len(returns) < 10:
        return {"prob_profit": 0, "VaR_95": 0, "expected_pnl": 0, "risk_of_ruin": 1.0}

    sim_results = []
    for _ in range(iterations):
        sim_rets = np.random.choice(returns, size=len(returns), replace=True)
        equity = 100000.0 * np.cumprod(1 + sim_rets)
        final_pnl = (equity[-1] - 100000.0) / 100000.0
        sim_results.append(final_pnl)

    sim_results = np.array(sim_results)
    return {
        "prob_profit": float(np.mean(sim_results > 0)),
        "VaR_95": float(np.percentile(sim_results, 5)),
        "expected_pnl": float(np.mean(sim_results)),
        "risk_of_ruin": float(np.mean(sim_results < -0.20)),
    }


def scan_assets(data: dict, signal_func, capital: float, max_position_pct: float = 0.10):
    """
    Scan each asset individually:
      - Walk-Forward (3 phases)
      - Monte Carlo 10K
      - Score and rank
    Returns list of {symbol, sharpe, sortino, maxdd, mc, score, allocation}.
    """
    print("\n" + "=" * 90)
    print("ASSET UNIVERSE SCAN — Walk-Forward + Monte Carlo 10K")
    print("=" * 90)

    asset_scores = []

    for sym in sorted(data.keys()):
        single = {sym: data[sym]}
        result = run_backtest(single, signal_func)
        raw_returns = result.get("returns", np.array([]))

        # Walk-Forward
        wf_sharpes = walk_forward_per_asset(single, signal_func)
        wf_positive_phases = sum(1 for s in wf_sharpes if s > 0)

        # Gate: must have positive Sharpe in ≥2/3 phases
        if wf_positive_phases < 2:
            print(f"  {sym:15} | WF FAILED ({wf_positive_phases}/3 phases positive) — SKIPPED")
            continue

        # Monte Carlo 10K
        mc = monte_carlo_asset(raw_returns, iterations=10000)

        # Score: 0.4*Sharpe + 0.3*Sortino + 0.2*(1-|MaxDD|) + 0.1*ProbProfit
        maxdd_val = abs(float(result["MaxDD"].replace("%", "")) / 100)
        score = (0.4 * result["Sharpe"] +
                 0.3 * result["Sortino"] +
                 0.2 * (1 - maxdd_val) +
                 0.1 * mc["prob_profit"])

        asset_scores.append({
            "symbol": sym,
            "sharpe": result["Sharpe"],
            "sortino": result["Sortino"],
            "cagr": result["CAGR"],
            "maxdd": result["MaxDD"],
            "wf_phases": f"{wf_positive_phases}/3",
            "mc_prob_profit": mc["prob_profit"],
            "mc_var95": mc["VaR_95"],
            "mc_risk_of_ruin": mc["risk_of_ruin"],
            "score": round(score, 3),
        })

        print(f"  {sym:15} | Sharpe: {result['Sharpe']:6.2f} | Sortino: {result['Sortino']:6.2f} | "
              f"WF: {wf_positive_phases}/3 | MC Prob: {mc['prob_profit']:.1%} | Score: {score:.3f}")

    if not asset_scores:
        print("\n  ⚠ No assets passed Walk-Forward gate.")
        return []

    # Rank by score
    asset_scores.sort(key=lambda x: x["score"], reverse=True)

    # Allocate capital equally, capped at max_position_pct
    max_per_stock = capital * max_position_pct
    n_assets = len(asset_scores)
    equal_alloc = capital / n_assets

    for a in asset_scores:
        a["allocation"] = round(min(equal_alloc, max_per_stock), 2)

    print("\n" + "-" * 90)
    print("SELECTED ASSETS (Ranked by Composite Score)")
    print("-" * 90)
    header = f"{'Rank':>4} | {'Symbol':15} | {'Score':>7} | {'Sharpe':>7} | {'MaxDD':>8} | {'MC Prob':>8} | {'Allocation':>12}"
    print(header)
    print("-" * 90)
    for i, a in enumerate(asset_scores):
        print(f"{i+1:4} | {a['symbol']:15} | {a['score']:7.3f} | {a['sharpe']:7.2f} | "
              f"{a['maxdd']:>8} | {a['mc_prob_profit']:7.1%} | ₹{a['allocation']:>10,.0f}")
    print("=" * 90)

    return asset_scores


# =============================================================================
# PHASE 3: RISK CHECK → EXECUTE → PERSIST
# =============================================================================

def execute_pipeline(asset_scores: list, alpha_name: str, capital: float, dry_run: bool = True):
    """
    For each selected asset:
      1. Build Signal → 2. Risk Check → 3. Place Order → 4. Save to DB
    """
    print("\n" + "=" * 90)
    print(f"EXECUTION PHASE {'(DRY RUN)' if dry_run else '(LIVE)'}")
    print("=" * 90)

    if dry_run:
        print("  Mode: DRY RUN — no orders will be placed.\n")

    from config.settings import SETTINGS
    from store.database import Database
    from store.event_log import EventLog, EventType

    db = Database(SETTINGS.DB_PATH)
    events = EventLog()

    # Risk client (communicates with risk server)
    from risk.client import RiskClient
    risk = RiskClient()

    # Broker
    if SETTINGS.is_paper():
        from broker.paper import PaperBroker
        broker = PaperBroker()
    else:
        from broker.angel import AngelBroker
        broker = AngelBroker()

    from core.order_manager import OrderManager
    order_mgr = OrderManager(broker, risk, db, events)

    from core.models import Signal, Action, OrderType as OT

    results_log = []

    for asset in asset_scores:
        sym = asset["symbol"]
        alloc = asset["allocation"]

        # Get estimated price
        try:
            price = broker.get_ltp(sym.replace(".NS", ""))
        except Exception:
            try:
                tick = yf.Ticker(sym)
                price = tick.info.get("currentPrice", tick.info.get("regularMarketPrice", 0))
            except Exception:
                price = 0

        if price <= 0:
            print(f"  {sym:15} | ⚠ Cannot determine price — SKIPPED")
            continue

        qty = int(alloc // price)
        if qty <= 0:
            print(f"  {sym:15} | ⚠ Allocation too small for 1 share (₹{price:.0f}) — SKIPPED")
            continue

        base_symbol = sym.replace(".NS", "").replace(".BO", "")

        # Build signal
        signal = Signal(
            symbol=base_symbol,
            action=Action.BUY,
            quantity=qty,
            order_type=OT.MARKET,
            strategy_id=alpha_name,
            reason=f"Pipeline: Score={asset['score']}, Sharpe={asset['sharpe']}, MC_Prob={asset['mc_prob_profit']:.1%}",
        )

        if dry_run:
            # Simulate risk check
            risk_payload = {
                "symbol": base_symbol,
                "action": "BUY",
                "quantity": qty,
                "order_type": "MARKET",
                "estimated_price": price,
                "strategy_id": alpha_name,
            }
            verdict = risk.check(risk_payload)
            status = verdict.get("verdict", "UNKNOWN")
            checks_passed = len(verdict.get("passed", []))
            checks_failed = verdict.get("failed", [])

            print(f"  {sym:15} | Qty: {qty:5} | ₹{alloc:>10,.0f} | "
                  f"Risk: {status} ({checks_passed} passed) | "
                  f"{'Failures: ' + ', '.join(checks_failed) if checks_failed else 'All clear'}")

            results_log.append({
                "symbol": sym,
                "quantity": qty,
                "allocation": alloc,
                "price": price,
                "risk_verdict": status,
                "risk_failed": checks_failed,
            })
        else:
            # LIVE: Full order flow
            try:
                broker.connect()
            except Exception:
                pass

            order = order_mgr.submit(signal)
            state = order.state.value if hasattr(order.state, 'value') else str(order.state)

            print(f"  {sym:15} | Qty: {qty:5} | ₹{alloc:>10,.0f} | Order: {state}")

            results_log.append({
                "symbol": sym,
                "quantity": qty,
                "allocation": alloc,
                "price": price,
                "order_state": state,
                "order_id": order.internal_id,
            })

    # Save pipeline run log
    log_path = os.path.join(PROJECT_ROOT, "logs", f"pipeline_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json")
    os.makedirs(os.path.dirname(log_path), exist_ok=True)
    with open(log_path, 'w') as f:
        json.dump({
            "alpha": alpha_name,
            "capital": capital,
            "dry_run": dry_run,
            "timestamp": datetime.now().isoformat(),
            "results": results_log,
        }, f, indent=2, default=str)
    print(f"\n  Pipeline log saved: {log_path}")
    print("=" * 90)

    return results_log


# =============================================================================
# ENTRY POINT
# =============================================================================

# =============================================================================
# ENTRY POINT
# =============================================================================

def main():
    # ── Argument Parser ───────────────────────────────────────────────────────
    parser = argparse.ArgumentParser(
        description="Alpha Research & Deployment Pipeline",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python scripts/alpha_pipeline.py --alpha strategy/user_strategies/my_alpha.py
  python scripts/alpha_pipeline.py --alpha strategy/user_strategies/my_alpha.py --capital 500000
  python scripts/alpha_pipeline.py --alpha strategy/user_strategies/my_alpha.py --universe NIFTY50
  python scripts/alpha_pipeline.py --alpha strategy/user_strategies/my_alpha.py --universe US
  python scripts/alpha_pipeline.py --alpha strategy/user_strategies/my_alpha.py --universe "RELIANCE,TCS,INFY"
  python scripts/alpha_pipeline.py --alpha strategy/user_strategies/my_alpha.py --live
        """
    )

    parser.add_argument(
        "--alpha",
        required=True,
        help="Path to the submitted alpha .py file"
    )
    parser.add_argument(
        "--capital",
        type=float,
        default=500000,
        help="Capital to allocate (default: 500000)"
    )
    parser.add_argument(
        "--years",
        type=int,
        default=3,
        help="Backtest lookback years (default: 3)"
    )
    parser.add_argument(
        "--universe",
        type=str,
        default="NIFTY",
        help=(
            "Stock universe. "
            "Presets: NIFTY (or NIFTY50), US (or SP500), US_TECH, CRYPTO. "
            "Or comma-separated tickers: 'RELIANCE,TCS,INFY' (auto-appends .NS), "
            "'AAPL,MSFT', or 'BTC-USD,ETH-USD'."
        )
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=True,
        help="Simulate execution without placing real orders (default)"
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help="Execute real orders (requires broker connection)"
    )

    # ── Parse ─────────────────────────────────────────────────────────────────
    args    = parser.parse_args()
    dry_run = not args.live

    alpha_path   = os.path.abspath(args.alpha)
    alpha_name   = os.path.basename(alpha_path).replace(".py", "")
    strategy_dir = os.path.join(PROJECT_ROOT, "strategy", "user_strategies")

    # ── Universe presets ──────────────────────────────────────────────────────
    _NIFTY = [
        # Financials
        "HDFCBANK.NS", "ICICIBANK.NS", "SBIN.NS", "KOTAKBANK.NS", "AXISBANK.NS",
        "BAJFINANCE.NS", "BAJAJFINSV.NS", "HDFCLIFE.NS", "SBILIFE.NS", "INDUSINDBK.NS",
        # IT
        "TCS.NS", "INFY.NS", "HCLTECH.NS", "WIPRO.NS", "TECHM.NS",
        # Energy & Oil
        "RELIANCE.NS", "ONGC.NS", "BPCL.NS", "IOC.NS", "COALINDIA.NS",
        # Consumer & FMCG
        "HINDUNILVR.NS", "ITC.NS", "NESTLEIND.NS", "BRITANNIA.NS", "TATACONSUM.NS",
        # Auto
        "MARUTI.NS", "BAJAJ-AUTO.NS", "HEROMOTOCO.NS", "EICHERMOT.NS",
        # Pharma
        "SUNPHARMA.NS", "DRREDDY.NS", "CIPLA.NS", "DIVISLAB.NS",
        # Industrials & Infra
        "LT.NS", "ADANIPORTS.NS", "ULTRACEMCO.NS", "GRASIM.NS", "SHREECEM.NS",
        # Metals & Mining
        "TATASTEEL.NS", "JSWSTEEL.NS", "HINDALCO.NS",
        # Telecom
        "BHARTIARTL.NS",
        # Consumer Discretionary
        "ASIANPAINT.NS", "TITAN.NS", 
        # Power
        "NTPC.NS", "POWERGRID.NS",
        # Conglomerate
        "ADANIENT.NS",
        # Chemicals
        # "UPL.NS",
    ]

    _US = [
        # Mega-cap Tech
        "AAPL", "MSFT", "GOOGL", "AMZN", "NVDA", "META", "TSLA",
        # Financials
        "JPM", "BAC", "WFC", "GS", "MS", "BRK-B", "V", "MA",
        # Healthcare
        "JNJ", "UNH", "PFE", "ABBV", "MRK", "TMO", "ABT",
        # Consumer
        "WMT", "HD", "MCD", "NKE", "SBUX", "TGT", "COST",
        # Industrials
        "BA", "CAT", "GE", "HON", "UPS", "RTX",
        # Energy
        "XOM", "CVX", "COP",
        # Telecom & Media
        "VZ", "T", "NFLX", "DIS",
        # Other
        "PYPL", "CRM", "ADBE", "INTC", "AMD",
    ]

    _US_TECH = [
        "AAPL", "MSFT", "GOOGL", "NVDA", "META", "TSLA",
        "AMZN", "AMD", "INTC", "CRM", "ADBE", "ORCL",
        "QCOM", "TXN", "MU", "AMAT", "LRCX", "KLAC",
        "SNOW", "PLTR", "NET", "DDOG", "ZS", "CRWD",
    ]

    _CRYPTO = [
        "BTC-USD", "ETH-USD", "BNB-USD", "SOL-USD",
        "XRP-USD", "DOGE-USD", "ADA-USD", "AVAX-USD",
        "DOT-USD", "MATIC-USD", "LINK-USD", "LTC-USD",
        "ATOM-USD", "UNI-USD", "ETC-USD", "TRX-USD",
    ]

    UNIVERSE_ALIASES = {
        # NIFTY variants
        "NIFTY":     _NIFTY,
        "NIFTY50":   _NIFTY,
        "NIFTY-50":  _NIFTY,
        "N50":       _NIFTY,
        "NSE":       _NIFTY,
        "INDIA":     _NIFTY,
        # US variants
        "US":        _US,
        "SP500":     _US,
        "S&P500":    _US,
        "S&P":       _US,
        # US Tech
        "US_TECH":   _US_TECH,
        "NASDAQ":    _US_TECH,
        "TECH":      _US_TECH,
        # Crypto
        "CRYPTO":    _CRYPTO,
        "DEFI":      _CRYPTO,
        "WEB3":      _CRYPTO,
    }

    # ── Resolve universe ──────────────────────────────────────────────────────
    raw_arg = args.universe.strip().upper().replace(" ", "")

    if raw_arg in UNIVERSE_ALIASES:
        universe = UNIVERSE_ALIASES[raw_arg]
    else:
        # Custom comma-separated list
        universe = []
        for token in raw_arg.split(","):
            token = token.strip()
            if not token:
                continue
            if "." in token or "-" in token:
                universe.append(token)          # already has suffix
            else:
                universe.append(f"{token}.NS")  # assume NSE

        if not universe:
            print(
                "  ✗ Could not parse --universe argument.\n"
                "    Valid presets : NIFTY, NIFTY50, US, SP500, US_TECH, CRYPTO\n"
                "    Custom example: --universe 'RELIANCE,TCS,INFY'\n"
                "                    --universe 'AAPL,MSFT'\n"
                "                    --universe 'BTC-USD,ETH-USD'"
            )
            sys.exit(1)

    # ── Banner ────────────────────────────────────────────────────────────────
    print("\n" + "#" * 90)
    print(f"  ALPHA RESEARCH & DEPLOYMENT PIPELINE")
    print(f"  Submitted:  {alpha_name}")
    print(f"  Capital:    ₹{args.capital:,.0f}")
    print(f"  Universe:   {raw_arg} ({len(universe)} symbols)")
    print(f"  Lookback:   {args.years}Y")
    print(f"  Mode:       {'DRY RUN' if dry_run else 'LIVE EXECUTION'}")
    print("#" * 90)

    # ── Step 1: Load submitted alpha ──────────────────────────────────────────
    print("\n[1/6] Loading submitted alpha...")
    submitted_func = load_submitted_alpha(alpha_path)
    print(f"  ✓ Loaded: {alpha_name}.backtest_signal()")

    # ── Step 2: Discover existing alphas ──────────────────────────────────────
    print("\n[2/6] Discovering existing alphas...")
    existing = discover_alphas(strategy_dir, exclude_file=alpha_path)
    total_files = len([
        f for f in os.listdir(strategy_dir)
        if f.endswith(".py") and f != "__init__.py"
    ]) - 1  # minus submitted
    skipped = total_files - len(existing)
    print(
        f"  ✓ Found {len(existing)} existing alphas"
        + (f" ({skipped} skipped — see warnings)" if skipped > 0 else " (all discovered)")
    )

    # ── Step 3: Fetch data ────────────────────────────────────────────────────
    print(f"\n[3/6] Fetching {args.years}Y data for {len(universe)} symbols...")
    data = fetch_universe_data(universe, years=args.years)
    print(f"  ✓ Loaded {len(data)} symbols")

    if len(data) < 3:
        print("  ✗ Not enough data. Aborting.")
        sys.exit(1)

    # ── Step 4: Comparative benchmark ────────────────────────────────────────
    print("\n[4/6] Running comparative benchmark...")
    all_results = {}

    submitted_result = run_backtest(data, submitted_func)
    all_results[alpha_name] = submitted_result

    for name, func in existing.items():
        try:
            result = run_backtest(data, func)
            all_results[name] = result
        except Exception as e:
            logger.warning(f"  Benchmark failed for {name}: {e}")

    print_benchmark_table(all_results, alpha_name)

    # ── Step 5: Rate & gate ───────────────────────────────────────────────────
    other_sharpes = [
        r["Sharpe"] for n, r in all_results.items() if n != alpha_name
    ]
    median_sharpe = np.median(other_sharpes) if other_sharpes else 0.0

    if submitted_result["Sharpe"] <= median_sharpe:
        print(
            f"\n  ⚠ Submitted alpha Sharpe ({submitted_result['Sharpe']:.2f}) ≤ "
            f"median ({median_sharpe:.2f})"
        )
        suggest_improvements(submitted_result, all_results, alpha_name)
        print("\n  Pipeline STOPPED: Alpha needs improvement before deployment.\n")
        sys.exit(0)
    else:
        print(
            f"\n  ✓ Submitted alpha OUTPERFORMS median "
            f"(Sharpe {submitted_result['Sharpe']:.2f} > {median_sharpe:.2f})"
        )
        print("  Proceeding to asset universe scanning...")

    # ── Step 6: Asset scan ────────────────────────────────────────────────────
    print("\n[5/6] Scanning asset universe (WF + MC 10K)...")
    selected_assets = scan_assets(data, submitted_func, args.capital)

    if not selected_assets:
        print("\n  ✗ No assets passed the WF/MC gate. Pipeline stopped.\n")
        sys.exit(0)

    # ── Step 7: Execute ───────────────────────────────────────────────────────
    print("\n[6/6] Executing orders...")
    execute_pipeline(
        selected_assets, alpha_name, args.capital, dry_run=dry_run
    )

    print("\n" + "#" * 90)
    print("  PIPELINE COMPLETE")
    print("#" * 90 + "\n")


if __name__ == "__main__":
    main()