#!/usr/bin/env python3
"""
================================================================================
ALPHA BENCHMARKER — MULTI-STRATEGY COMPARATIVE RESEARCH ENGINE
================================================================================
Usage:
  python strategy/alpha_benchmarker.py --years 3
  python strategy/alpha_benchmarker.py --years 5 --matrix
  python strategy/alpha_benchmarker.py --oos --wf --mc
================================================================================
"""
import os
import sys

# Add project root to path so we can import 'core'
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import logging
import argparse
from datetime import datetime, timedelta
from typing import List, Dict, Any
import pandas as pd
import numpy as np

# Suppress yfinance verbose printing
import warnings
warnings.filterwarnings("ignore")
import yfinance as yf

# ── Core signals (optional — graceful degradation if unavailable) ─────────────
try:
    from core.signals import (
        calculate_volatility_dispersion_scores,
        calculate_liquidity_imbalance_metrics,
    )
    HAS_CORE_SIGNALS = True
except ImportError:
    HAS_CORE_SIGNALS = False

# ── User strategies (imported AFTER logger is created) ───────────────────────
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger("AlphaBenchmarker")

# Dynamically import user strategies — warn instead of crash if missing
_user_strategies: Dict[str, Any] = {}

def _try_import(module_path: str, func_name: str, alias: str):
    """Attempt to import a backtest_signal function; store in _user_strategies."""
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location(alias, module_path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        if hasattr(mod, func_name):
            _user_strategies[alias] = getattr(mod, func_name)
            logger.info(f"  ✓ Loaded strategy: {alias}")
        else:
            logger.warning(f"  ✗ {module_path} has no {func_name}()")
    except Exception as e:
        logger.warning(f"  ✗ Could not import {alias}: {e}")

# Resolve paths relative to this file
_strat_dir = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "strategy", "user_strategies"
)

for _fname, _alias in [
    ("volatility_dispersion_alpha.py", "vol_dispersion_alpha"),
    ("liquidity_imbalance_alpha.py",   "liq_imbalance_alpha"),
    ("multifactor_alpha.py",           "multifactor_alpha"),
]:
    _try_import(os.path.join(_strat_dir, _fname), "backtest_signal", _alias)

# Convenience references (fall back to None if import failed)
strategy_volatility_dispersion_alpha = _user_strategies.get("vol_dispersion_alpha")
strategy_liquidity_imbalance_alpha   = _user_strategies.get("liq_imbalance_alpha")
strategy_multifactor_alpha           = _user_strategies.get("multifactor_alpha")


# ─── Universe ─────────────────────────────────────────────────────────────────
NIFTY_UNIVERSE = [
    "RELIANCE.NS", "TCS.NS", "HDFCBANK.NS", "INFY.NS", "ICICIBANK.NS",
    "SBIN.NS", "BHARTIARTL.NS", "ITC.NS", "KOTAKBANK.NS",
    "AXISBANK.NS", "ASIANPAINT.NS", "MARUTI.NS", "SUNPHARMA.NS", "BAJFINANCE.NS"
]

# Sector mapping
SECTOR_MAP = {
    "RELIANCE.NS": "Energy",    "TCS.NS": "IT",
    "INFY.NS": "IT",            "HDFCBANK.NS": "Financials",
    "ICICIBANK.NS": "Financials","SBIN.NS": "Financials",
    "BHARTIARTL.NS": "Telecom", "ITC.NS": "FMCG",
    "KOTAKBANK.NS": "Financials","AXISBANK.NS": "Financials",
    "ASIANPAINT.NS": "Consumer","MARUTI.NS": "Auto",
    "SUNPHARMA.NS": "Pharma",   "BAJFINANCE.NS": "Financials",
}


# ==============================================================================
# HELPERS
# ==============================================================================

def _s(x):
    """Guarantee pd.Series from possibly-MultiIndex DataFrame column."""
    return x.iloc[:, 0] if isinstance(x, pd.DataFrame) else x


def _safe_f(fundamentals, symbol, key, default=0):
    """Safely extract fundamental value, handling None."""
    if not fundamentals or symbol not in fundamentals:
        return default
    val = fundamentals[symbol].get(key, default)
    return val if val is not None else default


def _safe_price(df, date, col='Close'):
    """Extract a scalar price safely from both Series and DataFrame columns."""
    try:
        raw = df.loc[date, col]
        return float(_s(raw) if isinstance(raw, pd.DataFrame) else raw)
    except Exception:
        return None


# ==============================================================================
# BACKTEST ENGINE
# ==============================================================================

class BacktestEngine:
    def __init__(self, symbols, lookback_years=5, output_dir="results"):
        self.symbols = symbols
        self.lookback_years = lookback_years
        self.output_dir = output_dir
        self.data: Dict[str, pd.DataFrame] = {}
        self.fundamentals: Dict[str, dict] = {}
        self.results: Dict[str, dict] = {}
        self.equity_curves: Dict[str, pd.Series] = {}
        self.daily_returns: Dict[str, pd.Series] = {}
        self.daily_weights: Dict[str, dict] = {}
        self.validation_results: dict = {}

        os.makedirs(self.output_dir, exist_ok=True)

        # File handler — added only once
        log_file = os.path.join(self.output_dir, "backtest.log")
        if not any(isinstance(h, logging.FileHandler) and
                   getattr(h, 'baseFilename', '') == os.path.abspath(log_file)
                   for h in logger.handlers):
            fh = logging.FileHandler(log_file)
            fh.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
            logger.addHandler(fh)
        logger.info(f"Output directory: {self.output_dir}")

    # ── Data ──────────────────────────────────────────────────────────────────

    def fetch_data(self) -> bool:
        end_date = datetime.now()
        start_date = end_date - timedelta(days=365 * self.lookback_years)

        logger.info(
            f"Fetching {self.lookback_years}Y data for {len(self.symbols)} symbols..."
        )

        for symbol in self.symbols:
            try:
                df = yf.download(symbol, start=start_date, end=end_date,
                                 progress=False)
                if not df.empty:
                    if isinstance(df.columns, pd.MultiIndex):
                        df.columns = df.columns.get_level_values(0)
                    self.data[symbol] = df

                # Fundamentals
                try:
                    info = yf.Ticker(symbol).info
                except Exception:
                    info = {}

                self.fundamentals[symbol] = {
                    "beta":             info.get("beta", 1.0),
                    "marketCap":        info.get("marketCap", 0),
                    "trailingPE":       info.get("trailingPE", 0),
                    "forwardPE":        info.get("forwardPE", 0),
                    "priceToBook":      info.get("priceToBook", 0),
                    "debtToEquity":     info.get("debtToEquity", 0),
                    "revenueGrowth":    info.get("revenueGrowth", 0),
                    "earningsGrowth":   info.get("earningsGrowth", 0),
                    "returnOnEquity":   info.get("returnOnEquity", 0),
                    "operatingMargins": info.get("operatingMargins", 0),
                    "sector":           info.get("sector", "Unknown"),
                    "industry":         info.get("industry", "Unknown"),
                }

                beta = self.fundamentals[symbol]['beta'] or 0
                pe   = self.fundamentals[symbol]['trailingPE'] or 0
                logger.info(f"  Loaded {symbol} (Beta: {beta:.2f}, PE: {pe:.1f})")

            except Exception as e:
                logger.warning(f"  Failed to fetch {symbol}: {e}")

        logger.info(f"Loaded {len(self.data)} symbols.")
        return len(self.data) > 0

    # ── Core backtest loop ────────────────────────────────────────────────────

    def calculate_drawdown(self, equity_curve: pd.Series) -> float:
        peak = equity_curve.cummax()
        return float(((equity_curve - peak) / peak).min())

    def run_strategy(self, strategy_name: str, signal_func,
                     drawdown_limit: float = None,
                     custom_dates: list = None):
        """
        signal_func(data_dict, current_date, fundamentals, strategy_history) -> {symbol: weight}
        """
        logger.info(f"--- Running Backtest: {strategy_name} ---")

        if not self.data:
            logger.error("No data loaded. Call fetch_data() first.")
            return

        # Build common date index
        all_dates = sorted(set().union(*(df.index for df in self.data.values())))
        if len(all_dates) < 30:
            return

        test_dates = custom_dates if custom_dates else all_dates[30:]

        portfolio_value = 100_000.0
        peak_equity     = 100_000.0
        equity_curve    = []
        strategy_returns = []
        weight_history: Dict = {}

        is_circuit_broken = False
        breach_date = None
        actual_dd   = 0.0

        COST_PCT     = 0.001
        total_costs  = 0.0
        daily_turnover = []
        prev_weights: Dict = {}
        hits = 0
        exposures = 0

        for i in range(len(test_dates) - 1):
            current_date = test_dates[i]
            next_date    = test_dates[i + 1]

            # Update peak / check circuit breaker
            if portfolio_value > peak_equity:
                peak_equity = portfolio_value

            current_dd = (peak_equity - portfolio_value) / peak_equity
            if drawdown_limit and current_dd >= drawdown_limit and not is_circuit_broken:
                is_circuit_broken = True
                breach_date = current_date
                actual_dd   = current_dd

            # Build history slice
            history_slice = {
                sym: df.loc[:current_date]
                for sym, df in self.data.items()
                if current_date in df.index
            }

            # Get weights
            if is_circuit_broken:
                weights = {sym: 0.0 for sym in history_slice}
            else:
                try:
                    weights = signal_func(
                        history_slice, current_date,
                        self.fundamentals,
                        strategy_history=strategy_returns[-50:] if strategy_returns else []
                    )
                    if not isinstance(weights, dict):
                        weights = {}
                except Exception as e:
                    logger.error(f"  Strategy error [{strategy_name}] @ {current_date}: {e}")
                    weights = {}

            weight_history[current_date] = weights

            # Turnover & costs
            all_syms = set(weights) | set(prev_weights)
            turnover = sum(
                abs(weights.get(s, 0.0) - prev_weights.get(s, 0.0))
                for s in all_syms
            )
            daily_turnover.append(turnover)
            cost = portfolio_value * turnover * COST_PCT
            total_costs    += cost
            portfolio_value -= cost
            prev_weights    = dict(weights)

            if any(w > 0 for w in weights.values()):
                exposures += 1

            # PnL step
            day_return = 0.0
            for sym, weight in weights.items():
                if weight == 0:
                    continue
                df = self.data.get(sym)
                if df is None:
                    continue
                if current_date not in df.index or next_date not in df.index:
                    continue

                p1 = _safe_price(df, current_date)
                p2 = _safe_price(df, next_date)

                if p1 is None or p2 is None or p1 == 0:
                    continue

                next_ret = (p2 - p1) / p1
                day_return += weight * next_ret

            portfolio_value *= (1.0 + day_return)
            equity_curve.append(portfolio_value)
            strategy_returns.append(day_return)

            if day_return > 0:
                hits += 1

        # ── Metrics ──────────────────────────────────────────────────────────
        if not equity_curve:
            return

        eq_series  = pd.Series(equity_curve,      index=test_dates[:len(equity_curve)])
        ret_series = pd.Series(strategy_returns,   index=test_dates[:len(strategy_returns)])

        self.equity_curves[strategy_name]  = eq_series
        self.daily_returns[strategy_name]  = ret_series
        self.daily_weights[strategy_name]  = weight_history

        if is_circuit_broken:
            logger.warning(
                f"  CIRCUIT BREAKER: {strategy_name} @ {actual_dd:.2%} "
                f"(limit {drawdown_limit:.1%}) on {breach_date}"
            )

        total_return = (portfolio_value - 100_000.0) / 100_000.0
        days_tested  = (test_dates[-1] - test_dates[0]).days
        years        = max(days_tested / 365.25, 0.01)
        cagr         = (portfolio_value / 100_000.0) ** (1 / years) - 1

        daily_ret_clean = eq_series.pct_change().dropna()
        sharpe = (
            (daily_ret_clean.mean() / daily_ret_clean.std()) * np.sqrt(252)
            if len(daily_ret_clean) > 1 and daily_ret_clean.std() > 0 else 0.0
        )

        max_dd = self.calculate_drawdown(eq_series)

        downside = ret_series[ret_series < 0]
        down_std = downside.std() * np.sqrt(252) if len(downside) > 1 else 0.001
        sortino  = cagr / down_std if down_std > 0 else 0.0
        calmar   = cagr / abs(max_dd) if max_dd != 0 else 0.0

        n = len(test_dates)
        hit_ratio    = hits / n if n > 0 else 0.0
        avg_turnover = np.mean(daily_turnover) if daily_turnover else 0.0
        avg_exposure = exposures / n if n > 0 else 0.0

        self.results[strategy_name] = {
            "Total Return":  f"{total_return:.2%}",
            "CAGR":          f"{cagr:.2%}",
            "Sharpe Ratio":  f"{sharpe:.2f}",
            "Sortino":       f"{sortino:.2f}",
            "Calmar":        f"{calmar:.2f}",
            "Max Drawdown":  f"{max_dd:.2%}",
            "Turnover":      f"{avg_turnover:.2%}",
            "Hit Ratio":     f"{hit_ratio:.2%}",
            "Exposure":      f"{avg_exposure:.2%}",
            "Costs":         f"INR {total_costs:,.2f}",
            "Final Equity":  f"INR {portfolio_value:,.2f}",
        }

        logger.info(
            f"  {strategy_name} → CAGR: {cagr:.2%} | "
            f"Sharpe: {sharpe:.2f} | Sortino: {sortino:.2f} | MaxDD: {max_dd:.2%}"
        )

    # ── Visualisation ─────────────────────────────────────────────────────────

    def plot_results(self):
        try:
            import matplotlib.pyplot as plt
            plt.style.use('dark_background')
            fig, ax = plt.subplots(figsize=(14, 7))
            for name, curve in self.equity_curves.items():
                if not curve.empty:
                    ax.plot(curve.index, curve.values, label=name, linewidth=1.5)
            ax.set_title(
                f"Strategy Comparison ({self.lookback_years}Y) | Start: ₹1,00,000"
            )
            ax.set_xlabel("Date")
            ax.set_ylabel("Portfolio Value (INR)")
            ax.legend(fontsize=7)
            ax.grid(alpha=0.2)
            plot_path = os.path.join(self.output_dir, "equity_curves.png")
            plt.tight_layout()
            plt.savefig(plot_path, dpi=150)
            logger.info(f"  Chart saved: {plot_path}")
            plt.close()
        except ImportError:
            logger.warning("matplotlib not installed — skipping chart.")

    def _print_ascii_plot(self):
        print("\n" + "~" * 70)
        print("ASCII EQUITY CURVES")
        print("~" * 70)

        height, width = 12, 80
        all_vals = [v for c in self.equity_curves.values()
                    if not c.empty for v in c.values]
        if not all_vals:
            return

        v_min, v_max = min(all_vals), max(all_vals)
        v_range = v_max - v_min if v_max != v_min else 1.0
        grid = [[" "] * width for _ in range(height + 1)]

        chars   = ['#', '*', '@', '+', 'x', '.', 'o', 'v', '^', '~', '=', '!']
        legend  = {name: chars[i % len(chars)]
                   for i, name in enumerate(self.results)}

        for name, curve in self.equity_curves.items():
            if curve.empty:
                continue
            ch = legend.get(name, '?')
            for w in range(width):
                idx = int((w / (width - 1)) * (len(curve) - 1)) if len(curve) > 1 else 0
                val = curve.iloc[idx]
                h_i = int(((val - v_min) / v_range) * height)
                h_i = max(0, min(height, h_i))
                grid[h_i][w] = ch

        for name, ch in legend.items():
            print(f" {ch} : {name}")
        print("-" * (width + 14))
        for h in range(height, -1, -1):
            thr = v_min + (h / height) * v_range
            print(f" {thr:12,.0f} | {''.join(grid[h])}")
        print("-" * (width + 14))
        print(f"{' ' * 15} Timeline (Start → End)\n")

    def _print_drawdown_plot(self, strategy_name: str):
        print(f"\nUNDERWATER DRAWDOWN: {strategy_name}")
        print("-" * 70)
        curve = self.equity_curves.get(strategy_name)
        if curve is None or curve.empty:
            return
        dd = (curve - curve.cummax()) / curve.cummax()
        v_min = dd.min()
        if v_min >= 0:
            print("  No drawdown recorded.")
            return

        height, width = 8, 70
        for h in range(height, -1, -1):
            thr = (h / height) * v_min
            line = "".join(
                "v" if dd.iloc[int((w / (width - 1)) * (len(dd) - 1))] <= thr else " "
                for w in range(width)
            )
            print(f"{thr:6.1%} | {line}")
        print("-" * 70)

    def _print_monthly_heatmap(self, strategy_name: str):
        print(f"\nMONTHLY RETURNS: {strategy_name}")
        returns = self.daily_returns.get(strategy_name)
        if returns is None or returns.empty:
            return

        monthly = returns.groupby(
            [returns.index.year, returns.index.month]
        ).apply(lambda x: (1 + x).prod() - 1)

        years  = sorted(returns.index.year.unique())
        months = ["Jan","Feb","Mar","Apr","May","Jun",
                  "Jul","Aug","Sep","Oct","Nov","Dec"]
        print("Year | " + "  ".join(months))
        print("-" * 65)
        for y in years:
            row = f"{y} | "
            for m in range(1, 13):
                val = monthly.get((y, m), None)
                if val is None:
                    row += "  .  "
                else:
                    sym = "++" if val > 0.05 else "+" if val > 0 else "-" if val > -0.05 else "--"
                    row += f"{val:5.1%}" if abs(val) > 0.01 else f"  {sym}  "
            print(row)
        print("-" * 65)

    def _print_pnl_distribution(self, strategy_name: str):
        print(f"\nPNL DISTRIBUTION: {strategy_name}")
        returns = self.daily_returns.get(strategy_name)
        if returns is None or returns.empty:
            return
        buckets = pd.cut(returns * 100, bins=np.arange(-5, 5.5, 0.5))
        counts  = buckets.value_counts().sort_index()
        mx      = counts.max()
        scale   = 50 / mx if mx > 0 else 1
        for interval, count in counts.items():
            bar = "#" * int(count * scale)
            print(f"{interval.left:4.1f}% to {interval.right:4.1f}% | {bar} ({count})")

    def _print_rolling_sharpe(self, strategy_name: str, window: int = 60):
        print(f"\nROLLING SHARPE ({window}d): {strategy_name}")
        returns = self.daily_returns.get(strategy_name)
        if returns is None or returns.empty:
            return
        rolling = returns.rolling(window).apply(
            lambda x: (x.mean() / x.std() * np.sqrt(252)) if x.std() != 0 else 0
        ).dropna()
        if rolling.empty:
            return

        height, width = 8, 70
        v_min, v_max = rolling.min(), rolling.max()
        v_range = v_max - v_min if v_max != v_min else 1
        for h in range(height, -1, -1):
            thr  = v_min + (h / height) * v_range
            line = "".join(
                "~" if rolling.iloc[int((w / (width - 1)) * (len(rolling) - 1))] >= thr else " "
                for w in range(width)
            )
            print(f"{thr:6.2f} | {line}")
        print("-" * 70)

    def _print_alpha_spread(self, strategy_name: str, baseline_name: str):
        print(f"\nALPHA SPREAD: {strategy_name} vs {baseline_name}")
        s1 = self.equity_curves.get(strategy_name)
        s2 = self.equity_curves.get(baseline_name)
        if s1 is None or s2 is None:
            return
        spread = (s1 - s2) / 100_000.0 * 100

        height, width = 8, 70
        v_min, v_max = spread.min(), spread.max()
        v_range = v_max - v_min if v_max != v_min else 1
        for h in range(height, -1, -1):
            thr  = v_min + (h / height) * v_range
            line = "".join(
                "^" if spread.iloc[int((w / (width - 1)) * (len(spread) - 1))] >= thr else " "
                for w in range(width)
            )
            print(f"{thr:6.1f}% | {line}")
        print("-" * 70)

    def _print_exposure_bar(self, strategy_name: str):
        print(f"\nAVERAGE EXPOSURE: {strategy_name}")
        wh = self.daily_weights.get(strategy_name)
        if not wh:
            return
        df_w = pd.DataFrame.from_dict(wh, orient='index')
        avg  = df_w.mean().sort_values(ascending=False)
        avg  = avg[avg > 0.001]
        if avg.empty:
            print("  No active exposure.")
            return
        for sym, w in avg.items():
            print(f"  {sym:15} | {'#' * int(w * 100)} {w:.1%}")
        print("-" * 70)

    # ── Comparison output ─────────────────────────────────────────────────────

    def output_comparison(self):
        print("\n" + "=" * 80)
        print(
            f"BACKTEST COMPARISON ({self.lookback_years}Y) | "
            f"Universe: {len(self.symbols)} symbols"
        )
        print("=" * 80)

        if not self.results:
            print("  No results to display.")
            return

        df = pd.DataFrame(self.results).T
        df['_sharpe'] = pd.to_numeric(df['Sharpe Ratio'], errors='coerce').fillna(0)
        df = df.sort_values('_sharpe', ascending=False).drop(columns=['_sharpe'])
        print(df.to_string())
        print("=" * 80 + "\n")

        self.plot_results()
        self._print_ascii_plot()

        # Deep-dive on first user strategy if present
        user_strat = next(
            (n for n in self.results if "Custom" in n or "User" in n), None
        )
        baseline = next(
            (n for n in self.results if "Baseline" in n or "Rank" in n), None
        )

        if user_strat:
            print("\n" + "#" * 70)
            print(f"DEEP DIVE: {user_strat}")
            print("#" * 70)
            self._print_drawdown_plot(user_strat)
            self._print_monthly_heatmap(user_strat)
            self._print_pnl_distribution(user_strat)
            self._print_rolling_sharpe(user_strat)
            self._print_exposure_bar(user_strat)
            if baseline:
                self._print_alpha_spread(user_strat, baseline)

    # ── Advanced validation ───────────────────────────────────────────────────

    def run_ticker_by_strategy_matrix(self, strategies_map: dict):
        print("\n" + "=" * 110)
        print("TICKER × STRATEGY MATRIX (Sharpe Ratio)")
        print("=" * 110)

        original_data    = self.data
        original_symbols = self.symbols

        matrix: Dict[str, Dict] = {}

        for symbol in original_symbols:
            if symbol not in original_data:
                continue
            logger.info(f"  Matrix: {symbol}")
            matrix[symbol] = {}
            self.data    = {symbol: original_data[symbol]}
            self.symbols = [symbol]

            for name, func in strategies_map.items():
                prev_level = logger.level
                logger.setLevel(logging.WARNING)
                self.results = {}
                self.run_strategy(name, func)
                if name in self.results:
                    matrix[symbol][name] = self.results[name]
                logger.setLevel(prev_level)

        self.data    = original_data
        self.symbols = original_symbols

        names  = list(strategies_map.keys())
        header = f"{'Ticker':15} | " + " | ".join(f"{n[:14]:14}" for n in names)
        print(header)
        print("-" * len(header))
        for sym, reports in matrix.items():
            row = f"{sym:15} | "
            for name in names:
                try:
                    sharpe = float(reports.get(name, {}).get("Sharpe Ratio", 0))
                except Exception:
                    sharpe = 0.0
                row += f"{sharpe:14.2f} | "
            print(row)

        print("\n" + "=" * 130)
        print(f"{'BEST STRATEGY PER TICKER':40} | {'CAGR':>8} | {'Sharpe':>7} | "
              f"{'Sortino':>7} | {'MaxDD':>8} | {'Costs':>14} | Final Equity")
        print("=" * 130)
        for sym, reports in matrix.items():
            if not reports:
                continue
            best = max(reports, key=lambda n: float(reports[n].get("Sharpe Ratio", 0)))
            r = reports[best]
            print(
                f"{sym:15} → {best:22} | {r.get('CAGR','0%'):>8} | "
                f"{r.get('Sharpe Ratio','0'):>7} | {r.get('Sortino','0'):>7} | "
                f"{r.get('Max Drawdown','0%'):>8} | {r.get('Costs','INR 0'):>14} | "
                f"{r.get('Final Equity','0')}"
            )
        print("=" * 130 + "\n")

    def run_walk_forward_validation(self, strategies_map: dict, windows: int = 3):
        all_dates  = sorted(set().union(*(df.index for df in self.data.values())))
        chunk_size = len(all_dates) // (windows + 1)

        print("\n" + "=" * 70)
        print(f"WALK-FORWARD VALIDATION ({windows} phases)")
        print("=" * 70)

        wf_results: Dict[str, list] = {}

        for i in range(windows):
            end_in  = (i + 1) * chunk_size
            end_out = min(end_in + chunk_size // 2, len(all_dates))
            test_dates = all_dates[end_in:end_out]
            if not test_dates:
                break
            print(f"\nPhase {i+1}: {test_dates[0].date()} → {test_dates[-1].date()}")
            for name, func in strategies_map.items():
                tag = f"{name}_WF{i+1}"
                self.run_strategy(tag, func, custom_dates=test_dates)
                wf_results.setdefault(name, []).append(self.results.get(tag, {}))

        print("\n" + "-" * 60)
        print(f"{'Strategy':25} | {'Avg Sharpe':>10} | {'Std Sharpe':>10}")
        print("-" * 60)
        for name, phases in wf_results.items():
            sharpes = [float(p.get("Sharpe Ratio", 0)) for p in phases]
            print(f"{name:25} | {np.mean(sharpes):10.2f} | {np.std(sharpes):10.2f}")
        print("-" * 60 + "\n")

        return wf_results

    def run_oos_validation(self, strategies_map: dict):
        all_dates = sorted(set().union(*(df.index for df in self.data.values())))
        split_idx = int(len(all_dates) * 0.7)
        oos_dates = all_dates[split_idx:]

        print("\n" + "#" * 70)
        print("OUT-OF-SAMPLE VALIDATION")
        print(f"IS:  {all_dates[0].date()} → {all_dates[split_idx].date()}")
        print(f"OOS: {oos_dates[0].date()}  → {oos_dates[-1].date()}")
        print("#" * 70)

        oos_results = {}
        for name, func in strategies_map.items():
            tag = f"{name} (OOS)"
            self.run_strategy(tag, func, custom_dates=oos_dates)
            oos_results[name] = self.results.get(tag, {})

        return oos_results

    def run_cross_asset_test(self, strategies_map: dict):
        universes = {
            "NIFTY":   ["RELIANCE.NS", "TCS.NS", "INFY.NS", "HDFCBANK.NS"],
            "US_TECH": ["AAPL", "MSFT", "GOOGL", "NVDA"],
            "CRYPTO":  ["BTC-USD", "ETH-USD"],
        }

        print("\n" + "=" * 70)
        print("CROSS-ASSET ROBUSTNESS PROFILER")
        print("=" * 70)

        scores: Dict[str, Dict] = {}

        for u_name, u_syms in universes.items():
            print(f"\n  Universe: {u_name}")
            sub = BacktestEngine(
                u_syms,
                lookback_years=self.lookback_years,
                output_dir=os.path.join(self.output_dir, u_name)
            )
            if not sub.fetch_data():
                continue
            for name, func in strategies_map.items():
                sub.run_strategy(name, func)
                try:
                    sc = float(sub.results.get(name, {}).get("Sharpe Ratio", 0))
                except Exception:
                    sc = 0.0
                scores.setdefault(name, {})[u_name] = sc

        header = f"{'Strategy':25} | {'NIFTY':>10} | {'US_TECH':>10} | {'CRYPTO':>10} | {'AVG':>8}"
        print("\n" + header)
        print("-" * len(header))
        for name, s in scores.items():
            n = s.get("NIFTY", 0)
            u = s.get("US_TECH", 0)
            c = s.get("CRYPTO", 0)
            print(f"{name:25} | {n:10.2f} | {u:10.2f} | {c:10.2f} | {(n+u+c)/3:8.2f}")

    def run_monte_carlo(self, strategy_name: str, iterations: int = 1000):
        print(f"\nMONTE CARLO: {strategy_name} ({iterations:,} iterations)")
        print("-" * 70)
        returns = self.daily_returns.get(strategy_name)
        if returns is None or returns.empty:
            print("  No data.")
            return {}

        arr = returns.values
        sim = np.array([
            (100_000 * np.cumprod(1 + np.random.choice(arr, len(arr), replace=True)))[-1]
            / 100_000 - 1
            for _ in range(iterations)
        ])

        result = {
            "prob_profit":    float(np.mean(sim > 0)),
            "VaR_95":         float(np.percentile(sim, 5)),
            "Expected_Return": float(np.mean(sim)),
            "Risk_of_Ruin":   float(np.mean(sim < -0.20)),
        }

        print(f"  Prob of Profit     : {result['prob_profit']:.1%}")
        print(f"  Expected PnL       : {result['Expected_Return']:.1%}")
        print(f"  95% VaR            : {result['VaR_95']:.1%}")
        print(f"  Risk of Ruin (>20%): {result['Risk_of_Ruin']:.1%}")
        print("-" * 70)
        return result

    def run_correlation_analysis(self):
        if len(self.daily_returns) < 2:
            print("  Need ≥ 2 strategies for correlation.")
            return

        corr = pd.DataFrame(self.daily_returns).corr()
        print("\n" + "=" * 80)
        print("SIGNAL CORRELATION MATRIX")
        print("=" * 80)
        print(corr.round(3).to_string())

        for i in range(len(corr.columns)):
            for j in range(i + 1, len(corr.columns)):
                c = corr.iloc[i, j]
                if abs(c) > 0.8:
                    print(
                        f"  ⚠  HIGH REDUNDANCY: {corr.columns[i]} vs "
                        f"{corr.columns[j]} (r={c:.2f})"
                    )

    def run_parameter_stability(self, strategy_func, param_name: str,
                                values: List[int]):
        print(f"\nPARAMETER STABILITY: {param_name}")
        print("-" * 50)
        results = []
        for val in values:
            name = f"Stability_{param_name}_{val}"

            def _make_wrapper(v):
                def wrapped(data, current_date, fundamentals=None,
                            strategy_history=None):
                    return strategy_func(
                        data, current_date,
                        fundamentals=fundamentals,
                        strategy_history=strategy_history,
                        **{param_name: v}
                    )
                return wrapped

            self.run_strategy(name, _make_wrapper(val))
            stats = self.results.get(name, {})
            results.append({
                "value": val,
                "Sharpe": float(stats.get("Sharpe Ratio", 0)),
                "CAGR":   stats.get("CAGR", "0%"),
            })
            print(f"  {param_name}={val:<4} | "
                  f"Sharpe: {stats.get('Sharpe Ratio','?'):>6} | "
                  f"CAGR: {stats.get('CAGR','?'):>8}")
        return results

    def generate_current_suggestions(self, signal_func):
        logger.info("Generating current suggestions...")
        if not self.data:
            return
        current_date = list(self.data.values())[0].index[-1]
        history_slice = {sym: df.loc[:current_date] for sym, df in self.data.items()}

        try:
            weights = signal_func(history_slice, current_date, self.fundamentals)
        except Exception as e:
            logger.error(f"  Suggestion error: {e}")
            weights = {}

        name = getattr(signal_func, '__name__', 'strategy')
        print("\n" + "*" * 60)
        print(f"CURRENT SUGGESTIONS | {name}")
        print("*" * 60)

        for sym in sorted(self.symbols,
                          key=lambda s: weights.get(s, 0), reverse=True):
            w = weights.get(sym, 0.0)
            tag = "[PICKED]" if w > 0 else "[FILTERED]"
            print(f"  {sym:15} | {w:6.1%} | {tag}")
        print("*" * 60 + "\n")


# ==============================================================================
# BUILT-IN STRATEGIES
# ==============================================================================

def baseline_rank7_strategy(data, current_date, fundamentals=None,
                             strategy_history=None):
    """Rank #7 Baseline: Momentum + SMA20 Filter"""
    eligible = []
    for sym, df in data.items():
        try:
            close = _s(df['Close'])
            if len(close) < 21:
                continue
            p_now = float(close.iloc[-1])
            p_old = float(close.iloc[-20])
            if p_old <= 0:
                continue
            mom   = (p_now - p_old) / p_old
            sma20 = float(close.rolling(20).mean().iloc[-1])
            if p_now > sma20:
                eligible.append((sym, mom))
        except Exception:
            continue
    eligible.sort(key=lambda x: x[1], reverse=True)
    top = eligible[:3]
    if not top:
        return {}
    w = 1.0 / len(top)
    return {sym: w for sym, _ in top}


def user_custom_alpha(data, current_date, fundamentals=None,
                      strategy_history=None):
    """Beta-Adjusted Mean Reversion"""
    allocations = {}
    for sym, df in data.items():
        try:
            close = _s(df['Close'])
            if len(close) < 5:
                continue
            p_now = float(close.iloc[-1])
            p_avg = float(close.rolling(5).mean().iloc[-1])
            if p_now < p_avg * 0.98:
                beta   = _safe_f(fundamentals, sym, "beta", 1.0) or 1.0
                weight = 0.10 * (1.0 / beta if beta > 0.5 else 1.0)
                allocations[sym] = min(weight, 0.15)
        except Exception:
            continue
    return allocations


def strategy_cross_regime_mr(data, current_date, fundamentals=None,
                              strategy_history=None):
    """Mean Reversion + PE filter"""
    allocations = {}
    for sym, df in data.items():
        try:
            close = _s(df['Close'])
            if len(close) < 30:
                continue
            pe = _safe_f(fundamentals, sym, "trailingPE", 0)
            if pe and pe > 50:
                continue
            vol = close.pct_change().std() * np.sqrt(252)
            if vol > 0.35:
                sma = close.rolling(20).mean().iloc[-1]
                std = close.rolling(20).std().iloc[-1]
                if std > 0:
                    z = (float(close.iloc[-1]) - float(sma)) / float(std)
                    if z < -2.0:
                        allocations[sym] = 0.15
        except Exception:
            continue
    return allocations


def strategy_kelly_sma_filter(data, current_date, fundamentals=None,
                               strategy_history=None):
    """Trend + Quality (D/E & MarketCap)"""
    allocations = {}
    for sym, df in data.items():
        try:
            close = _s(df['Close'])
            if len(close) < 50:
                continue
            de   = _safe_f(fundamentals, sym, "debtToEquity", 0)
            mcap = _safe_f(fundamentals, sym, "marketCap", 0)
            if de and de > 200:
                continue
            if mcap and mcap < 1e9:
                continue
            p_now  = float(close.iloc[-1])
            sma50  = float(close.rolling(50).mean().iloc[-1])
            if p_now > sma50:
                allocations[sym] = 0.125
        except Exception:
            continue
    return allocations


def strategy_crypto_momentum_alpha(data, current_date, fundamentals=None,
                                   strategy_history=None):
    """7-Day Momentum (no minimum bar)"""
    momentum = {}
    for sym, df in data.items():
        try:
            close = _s(df['Close'])
            if len(close) < 8:
                continue
            momentum[sym] = (float(close.iloc[-1]) - float(close.iloc[-8])) / float(close.iloc[-8])
        except Exception:
            continue
    if not momentum:
        return {}
    top = sorted(momentum, key=momentum.get, reverse=True)[:3]
    w   = 1.0 / len(top)
    return {sym: w for sym in top}


def strategy_growth_alpha(data, current_date, fundamentals=None,
                          strategy_history=None):
    """Growth: Revenue + Earnings growth"""
    scores = {}
    for sym in data:
        rg = _safe_f(fundamentals, sym, "revenueGrowth",  0)
        eg = _safe_f(fundamentals, sym, "earningsGrowth", 0)
        scores[sym] = (rg + eg) if (rg + eg) > 0 else rg
    if not scores:
        return {}
    top = sorted(scores, key=scores.get, reverse=True)[:3]
    w   = 1.0 / len(top)
    return {sym: w for sym in top}


def strategy_quality_alpha(data, current_date, fundamentals=None,
                           strategy_history=None):
    """Quality: ROE + Operating Margins"""
    scores = {}
    for sym in data:
        roe    = _safe_f(fundamentals, sym, "returnOnEquity",   0)
        margin = _safe_f(fundamentals, sym, "operatingMargins", 0)
        scores[sym] = (roe + margin) if (roe + margin) > 0 else roe
    if not scores:
        return {}
    top = sorted(scores, key=scores.get, reverse=True)[:3]
    w   = 1.0 / len(top)
    return {sym: w for sym in top}


def strategy_adaptive_kelly(data, current_date, fundamentals=None,
                             strategy_history=None):
    """Adaptive Half-Kelly: learns from backtest history"""
    win_rate = 0.55
    avg_win  = 0.02
    avg_loss = 0.015

    if strategy_history:
        nz = [r for r in strategy_history if abs(r) > 1e-6]
        if len(nz) >= 5:
            wins   = [r for r in nz if r > 0]
            losses = [abs(r) for r in nz if r < 0]
            win_rate = len(wins) / len(nz)
            avg_win  = float(np.mean(wins))  if wins   else avg_win
            avg_loss = float(np.mean(losses)) if losses else avg_loss

    b         = avg_win / avg_loss if avg_loss > 0 else 1.0
    kelly     = max(0.0, (win_rate * b - (1 - win_rate)) / b) if b > 0 else 0.0
    half_kelly = min(kelly * 0.5, 0.25)

    if half_kelly <= 0:
        half_kelly = 0.10  # floor

    eligible = []
    for sym, df in data.items():
        try:
            close = _s(df['Close'])
            if len(close) < 50:
                continue
            p_now  = float(close.iloc[-1])
            sma50  = float(close.rolling(50).mean().iloc[-1])
            strength = (p_now - sma50) / (sma50 if sma50 != 0 else 1.0)
            eligible.append((sym, strength))
        except Exception:
            continue

    if not eligible:
        return {}

    eligible.sort(key=lambda x: x[1], reverse=True)
    top = eligible[:5]
    w   = half_kelly / len(top)
    return {sym: w for sym, _ in top}


# ==============================================================================
# ENTRY POINT
# ==============================================================================

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Alpha Benchmarker")

    parser.add_argument("--years",          type=int,   default=3)
    parser.add_argument("--universe",       type=str,   default="NIFTY")
    parser.add_argument("--drawdown-limit", type=float, default=0.10)
    parser.add_argument("--output-dir",     type=str,   default="backtest_results")
    parser.add_argument("--matrix",         action="store_true")
    parser.add_argument("--oos",            action="store_true")
    parser.add_argument("--wf",             action="store_true")
    parser.add_argument("--mc",             action="store_true")
    parser.add_argument("--cross-asset",    action="store_true")
    parser.add_argument("--stability",      type=str)
    parser.add_argument("--correlation",    action="store_true")
    parser.add_argument("--research-suite", action="store_true")
    parser.add_argument("--guide",          action="store_true")

    args = parser.parse_args()

    if args.guide:
        print("""
===========================================================================
INSTITUTIONAL QUANTITATIVE RESEARCH GUIDE
===========================================================================
--oos            Out-of-Sample: 70/30 split to detect overfitting.
--wf             Walk-Forward:  3-phase rolling validation.
--mc             Monte Carlo:   1,000 resamples → Risk of Ruin.
--cross-asset    Cross-Asset:   NIFTY / US Tech / Crypto robustness.
--matrix         Ticker×Strategy Sharpe matrix.
--correlation    Return correlation matrix (redundancy check).
--stability p:v  Parameter sensitivity (e.g. --stability sma:20,50,100).
--research-suite Full audit (matrix + MC).
===========================================================================
""")
        sys.exit(0)

    # ── Universe selection ────────────────────────────────────────────────────
    u = args.universe.upper()
    if u == "NIFTY":
        universe = NIFTY_UNIVERSE
    elif u == "US":
        universe = ["AAPL","MSFT","GOOGL","AMZN","NVDA","TSLA","META","JPM","V"]
    else:
        universe = []
        for s in [x.strip().upper() for x in args.universe.split(",")]:
            universe.append(s if ("." in s or len(s) > 6) else f"{s}.NS")

    # ── Strategy registry ─────────────────────────────────────────────────────
    strategies: Dict[str, Any] = {
        "Rank #7 Baseline":      baseline_rank7_strategy,
        "User Custom Alpha":     user_custom_alpha,
        "Cross-Regime MR":       strategy_cross_regime_mr,
        "Half-Kelly SMA":        strategy_kelly_sma_filter,
        "Crypto Momentum":       strategy_crypto_momentum_alpha,
        "Growth Alpha":          strategy_growth_alpha,
        "Quality Alpha":         strategy_quality_alpha,
        "Adaptive Kelly":        strategy_adaptive_kelly,
    }

    # Append dynamically loaded user strategies if available
    if strategy_volatility_dispersion_alpha:
        strategies["Vol Dispersion"] = strategy_volatility_dispersion_alpha
    if strategy_liquidity_imbalance_alpha:
        strategies["Liq Imbalance"]  = strategy_liquidity_imbalance_alpha
    if strategy_multifactor_alpha:
        strategies["MFA Composite"]  = strategy_multifactor_alpha

    # ── Run engine ────────────────────────────────────────────────────────────
    engine = BacktestEngine(
        universe,
        lookback_years=args.years,
        output_dir=args.output_dir
    )

    if not engine.fetch_data():
        logger.error("No data loaded. Exiting.")
        sys.exit(1)

    limit = args.drawdown_limit

    if args.research_suite:
        for name, func in strategies.items():
            engine.run_strategy(name, func, drawdown_limit=limit)
            engine.run_monte_carlo(name)
        engine.output_comparison()
        engine.run_ticker_by_strategy_matrix(strategies)

    elif args.matrix:
        for name, func in strategies.items():
            engine.run_strategy(name, func, drawdown_limit=limit)
        engine.output_comparison()
        engine.run_ticker_by_strategy_matrix(strategies)

    elif args.oos:
        engine.run_oos_validation(strategies)
        engine.output_comparison()

    elif args.wf:
        engine.run_walk_forward_validation(strategies)
        engine.output_comparison()

    elif args.mc:
        for name, func in strategies.items():
            engine.run_strategy(name, func, drawdown_limit=limit)
            engine.run_monte_carlo(name)

    elif args.cross_asset:
        engine.run_cross_asset_test(strategies)

    elif args.correlation:
        for name, func in strategies.items():
            engine.run_strategy(name, func, drawdown_limit=limit)
        engine.run_correlation_analysis()

    elif args.stability:
        p_name, p_vals_str = args.stability.split(":")
        vals = [int(v) for v in p_vals_str.split(",")]
        engine.run_parameter_stability(strategy_adaptive_kelly, p_name, vals)

    else:
        for name, func in strategies.items():
            engine.run_strategy(name, func, drawdown_limit=limit)
        engine.output_comparison()
        engine.generate_current_suggestions(strategy_kelly_sma_filter)