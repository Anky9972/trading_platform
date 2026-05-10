"""Pydantic schemas at every cross-module boundary.

We avoid the heavy pydantic dependency for the P0 modules where dataclasses
suffice; pydantic enters once we add the LLM personas (P1+).
"""
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Blueprint:
    """A factor blueprint emitted by the Hypothesis Hunter (or a Jinja archetype)."""
    theme: str
    archetype: str
    components: list[dict]
    neutralization: str = "sector"
    decay: int = 0
    novelty_claim: str = ""
    academic_anchor: str = ""


@dataclass
class CompiledExpression:
    """Output of the Expression Compiler (Jinja or LLM)."""
    expression: str
    archetype: str
    fields_used: list[str] = field(default_factory=list)
    operators_used: list[str] = field(default_factory=list)


@dataclass
class SubmissionMetrics:
    """Metrics returned by the BRAIN simulator harvester."""
    alpha_id: str
    sharpe_full: float
    sharpe_is: Optional[float] = None
    sharpe_os: Optional[float] = None
    yearly_sharpe: list[float] = field(default_factory=list)
    yearly_returns: list[float] = field(default_factory=list)
    yearly_drawdown: list[float] = field(default_factory=list)
    turnover: float = 0.0
    max_drawdown: float = 0.0
    margin_pct: float = 0.0
    self_corr_max: float = 0.0
    self_corr_min: float = 0.0
    is_pass_count: int = 0
    is_fail_count: int = 0
    raw: dict = field(default_factory=dict)


@dataclass
class Verdict:
    """Output of the Performance Surgeon."""
    alpha_id: str
    decision: str             # "promote" | "iterate" | "kill"
    fitness: float
    rationale: str
    sign_error_suspected: bool = False
    regime_dependent: bool = False
    decay_detected: bool = False
    iteration_suggestion: str = ""
