"""Deterministic dedup — sha256 of a normalized expression.

Two expressions that differ only in whitespace/comments must hash identically,
so we can never resubmit a duplicate to BRAIN.
"""
from __future__ import annotations

import hashlib
import re


def normalize_expression(expr: str) -> str:
    """Strip comments, collapse whitespace, lowercase. Preserves semantics."""
    # remove // and # comments
    expr = re.sub(r"//[^\n]*", "", expr)
    expr = re.sub(r"#[^\n]*", "", expr)
    # collapse whitespace
    expr = re.sub(r"\s+", " ", expr).strip()
    # remove spaces around punctuation that don't matter
    expr = re.sub(r"\s*([,;()*/+\-=<>])\s*", r"\1", expr)
    return expr.lower()


def alpha_id(
    expression: str,
    neutralization: str = "",
    decay: int = 0,
    universe: str = "TOP3000",
    region: str = "USA",
    delay: int = 1,
) -> str:
    """Deterministic alpha id: sha256 of (normalized_expr | settings).

    Equal expressions with equal settings always produce the same id, so the
    factor store will reject a duplicate submission.
    """
    norm = normalize_expression(expression)
    payload = f"{norm}|{neutralization}|{decay}|{universe}|{region}|{delay}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
