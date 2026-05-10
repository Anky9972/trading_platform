"""Single-scalar fitness function for alpha promotion.

Designed to punish what PMs hate (decay, fragility, crowding) and reward what
they want (clean Sharpe, low drawdown, low correlation, novelty).

See acceptance_engineering.md §5.3 for the formula and §6.2 for calibration.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class AlphaMetrics:
    sharpe_full: float
    sharpe_is: float | None = None
    sharpe_os: float | None = None
    yearly_sharpe: list[float] | None = None
    turnover: float = 0.0           # fraction, e.g., 0.40 = 40%
    max_drawdown: float = 0.0       # fraction, e.g., 0.05 = 5%
    max_corr_to_library: float = 0.0
    theme_novelty_score: float = 0.0  # 0 .. 1


def compute_fitness(m: AlphaMetrics) -> float:
    """Bounded scalar in roughly [-3, +5]. Higher is better.

    Components (tunable; see calibration plan):
      + Sharpe (OS preferred, fall back to full)
      - IS/OS gap penalty
      - Negative-year penalty (heavy)
      - Crowding penalty
      - Turnover penalty above 40%
      - Drawdown penalty above 5%
      + Theme novelty bonus
    """
    sharpe = m.sharpe_os if m.sharpe_os is not None else m.sharpe_full

    is_os_gap = 0.0
    if m.sharpe_is is not None and m.sharpe_os is not None:
        is_os_gap = abs(m.sharpe_is - m.sharpe_os)

    worst_year_penalty = 0.0
    if m.yearly_sharpe:
        worst = min(m.yearly_sharpe)
        if worst < 0:
            worst_year_penalty = abs(worst)

    turnover_excess = max(0.0, m.turnover - 0.40)
    drawdown_excess = max(0.0, m.max_drawdown - 0.05)

    fitness = (
        sharpe
        - 0.5 * is_os_gap
        - 1.0 * worst_year_penalty
        - 0.3 * m.max_corr_to_library
        - 0.2 * turnover_excess
        - 0.1 * drawdown_excess
        + 0.4 * m.theme_novelty_score
    )
    return float(fitness)


def verdict_from_fitness(fitness: float) -> str:
    """Coarse decision label aligned with the pre-registered tree."""
    if fitness >= 1.5:
        return "promote"
    if fitness >= 0.5:
        return "iterate"
    return "kill"
