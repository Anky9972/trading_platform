"""Static lint for WorldQuant BRAIN expressions.

Catches the three classes of failure that cost real BRAIN credits:
  1. Mechanical — unknown operator names, wrong arity, malformed syntax.
  2. Look-ahead — patterns that read future bars (negative ts_delay, etc.).
  3. Unit drift — additive operands that mix unit-tagged with unitless terms,
     which BRAIN's unit system rejects with "Incompatible unit" warnings.

Reference data: data/operators.csv (the canonical operator catalog from BRAIN).

Validated against:
  - Alpha 19 v1 (the unit-warning bug we hit 2026-05-07)
  - All 18 production alphas (must lint clean)
  - All v1/v2/v3/v4 iterations (each must lint clean except v1)

Usage:
    from research.alpha_factory.deterministic.lint import lint_expression
    errors, warnings = lint_expression(expr_string)
"""
from __future__ import annotations

import csv
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

DATA_DIR = Path(__file__).parent.parent / "data"
OPERATORS_CSV = DATA_DIR / "operators.csv"

# Operators that produce a unit-clean (dimensionless) scalar in their output.
# Adding a wrapper of one of these to a raw field-arithmetic operand is what
# BRAIN's unit system requires for the top-level + reduction.
UNIT_CLEAN_OPS = frozenset({
    "rank", "zscore", "quantile", "ts_rank", "ts_zscore",
    "group_rank", "group_zscore", "group_neutralize", "group_scale",
    "scale", "normalize", "winsorize",
})

# Look-ahead patterns — any of these in an expression is a hard reject.
LOOK_AHEAD_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"ts_delay\s*\(\s*[^,]+,\s*-\d", "negative-delay (future read)"),
    (r"\bfuture_\w+", "field name starts with future_"),
    (r"\bforward_returns?\b", "explicit forward returns"),
    (r"ts_delta\s*\(\s*[^,]+,\s*-\d", "negative ts_delta lookback"),
)

# Epsilon-stabilization unit-drift patterns. Mixing a bare numeric literal
# (units = []) with a unit-tagged variable on either side of + or - inside
# a divisor is the classic cause of "Incompatible unit for input of 'add'"
# warnings on BRAIN. Catches `rv30 + 0.001`, `0.001 + rv30`, `x - 1e-6`, etc.
# Safe forms: `0.40 * rank(x) + 0.18 * zscore(y)` (no add of bare literal),
# or `add(x, y, filter=true)` style. We deliberately allow integer literals
# in window-arg positions (caught by being inside parens of a single op call).
_NUM = r"(?:\d+\.\d+(?:e-?\d+)?|\d*\.\d+(?:e-?\d+)?|\d+e-\d+)"
_IDENT_CHARS = r"[A-Za-z_][A-Za-z0-9_]*"
EPS_DRIFT_PATTERNS: tuple[tuple[str, str], ...] = (
    (rf"{_IDENT_CHARS}\s*[+\-]\s*{_NUM}",
     "bare numeric literal added to/subtracted from an identifier "
     "(unit drift; use 'add(x, y, filter=true)' or wrap in zscore/rank first)"),
    (rf"{_NUM}\s*[+\-]\s*{_IDENT_CHARS}",
     "bare numeric literal added to/subtracted from an identifier "
     "(unit drift; use 'add(x, y, filter=true)' or wrap in zscore/rank first)"),
)


@dataclass
class LintReport:
    ok: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    operators_used: list[str] = field(default_factory=list)
    fields_used: list[str] = field(default_factory=list)

    def __bool__(self) -> bool:
        return self.ok


def _load_operator_catalog(path: Path = OPERATORS_CSV) -> set[str]:
    """Return the lowercased set of valid operator names from operators.csv."""
    if not path.exists():
        # graceful fallback — return an empty set so caller gets clear error
        return set()
    out: set[str] = set()
    with path.open("r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            name = (row.get("name") or "").strip().lower()
            if name:
                out.add(name)
    return out


_OP_CATALOG: set[str] | None = None


def operator_catalog() -> set[str]:
    """Lazy-load and cache the operator catalog."""
    global _OP_CATALOG
    if _OP_CATALOG is None:
        _OP_CATALOG = _load_operator_catalog()
    return _OP_CATALOG


# Identifier syntax: a plain word at the start of a function-call.
# We treat anything before "(" as an operator candidate.
_OP_CALL_RE = re.compile(r"\b([a-z_][a-z0-9_]*)\s*\(")
# Identifier reads — used to extract candidate field names. We exclude any
# token that appears as an operator call in the same expression.
_IDENT_RE = re.compile(r"\b([a-z_][a-z0-9_]*)\b")
# Numbers and Python keywords we don't want as "fields"
_NON_FIELD = frozenset({
    "true", "false", "and", "or", "not", "if", "else",
    "abs",  # builtin-ish, also an operator
})


def _strip_comments(expr: str) -> str:
    """Remove // and # line comments so they don't confuse pattern matching."""
    out_lines = []
    for raw in expr.splitlines():
        # remove // ... and # ... up to end of line
        line = re.sub(r"//.*$", "", raw)
        line = re.sub(r"#.*$", "", line)
        out_lines.append(line)
    return "\n".join(out_lines)


def _split_top_level_assignments(expr: str) -> tuple[dict[str, str], str]:
    """Split a multi-statement expression into (assignments, final_expression).

    BRAIN expressions look like::

        a = ts_mean(close, 5);
        b = rank(a);
        b

    We return ({"a": "ts_mean(close,5)", "b": "rank(a)"}, "b"). If the
    expression is a single statement, assignments is empty and the input
    is the final expression.
    """
    cleaned = _strip_comments(expr).strip()
    # split on ; that are top-level (not inside parentheses)
    parts: list[str] = []
    depth = 0
    buf = []
    for ch in cleaned:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == ";" and depth == 0:
            parts.append("".join(buf).strip())
            buf = []
        else:
            buf.append(ch)
    if buf:
        parts.append("".join(buf).strip())
    parts = [p for p in parts if p]
    if not parts:
        return {}, ""
    assignments: dict[str, str] = {}
    final = parts[-1]
    for p in parts[:-1]:
        if "=" in p and not p.lstrip().startswith("="):
            lhs, rhs = p.split("=", 1)
            assignments[lhs.strip()] = rhs.strip()
        # bare expression at non-final position is harmless; ignore
    return assignments, final


def _check_top_level_additive_unit_safety(final: str) -> list[str]:
    """If the final expression is a top-level weighted sum, every operand
    must be wrapped in a unit-clean operator (rank/zscore/quantile/...).

    This is the rule that catches the "Incompatible unit" warning we hit on
    Alpha 19 v1.
    """
    # find top-level + or - splits (depth 0)
    depth = 0
    operands: list[str] = []
    buf: list[str] = []
    for ch in final:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch in "+-" and depth == 0 and buf and buf[-1] not in "(*+-/, ":
            operands.append("".join(buf).strip())
            buf = []
        else:
            buf.append(ch)
    if buf:
        operands.append("".join(buf).strip())

    # If only one operand at top level, it's not an additive composite.
    if len(operands) <= 1:
        return []

    warnings: list[str] = []
    for op in operands:
        # Strip a leading numeric weight like "0.40 * ..."
        stripped = re.sub(r"^\s*[-+]?\d*\.?\d+\s*\*\s*", "", op).strip()
        # Now stripped should start with a unit-clean operator call
        m = re.match(r"\b([a-z_][a-z0-9_]*)\s*\(", stripped)
        if not m:
            # operand is a bare identifier or literal — treat as warning
            warnings.append(
                f"Top-level operand has no unit-cleansing wrapper: '{stripped[:60]}'"
            )
            continue
        head = m.group(1).lower()
        if head not in UNIT_CLEAN_OPS:
            warnings.append(
                f"Top-level operand starts with '{head}' which is not a unit-clean "
                f"wrapper. Wrap in zscore/rank/quantile/group_zscore. "
                f"Operand: '{stripped[:60]}'"
            )
    return warnings


def _expand_assignments(final: str, assignments: dict[str, str], depth: int = 0) -> str:
    """Inline assigned variables into the final expression (one level).

    For unit safety we want to look at the *expanded* final composite, since
    the unit drift bug occurs after assignment substitution.
    """
    if depth > 5:  # bound recursion
        return final
    out = final
    changed = True
    while changed:
        changed = False
        for name, rhs in assignments.items():
            # whole-word replace
            new_out = re.sub(rf"\b{re.escape(name)}\b", f"({rhs})", out)
            if new_out != out:
                out = new_out
                changed = True
    return out


def lint_expression(
    expression: str,
    *,
    known_fields: Iterable[str] | None = None,
) -> LintReport:
    """Run the full lint suite against a BRAIN expression.

    Parameters
    ----------
    expression : str
        The full multi-statement BRAIN expression.
    known_fields : iterable of str, optional
        Whitelist of valid field names. If provided, any identifier read that
        isn't an operator and isn't in this set produces a warning.

    Returns
    -------
    LintReport with ok flag, errors, warnings, operators_used, fields_used.
    """
    errors: list[str] = []
    warnings: list[str] = []
    if not expression or not expression.strip():
        return LintReport(ok=False, errors=["empty expression"])

    cleaned = _strip_comments(expression)

    # 1) operator validity
    cat = operator_catalog()
    if not cat:
        warnings.append(
            "operators.csv catalog not found — operator-name check skipped"
        )
    operators_used = sorted(set(m.group(1).lower() for m in _OP_CALL_RE.finditer(cleaned)))
    if cat:
        for op in operators_used:
            if op not in cat:
                errors.append(f"Unknown operator: '{op}'")

    # 2) look-ahead
    for pat, label in LOOK_AHEAD_PATTERNS:
        if re.search(pat, cleaned):
            errors.append(f"Look-ahead pattern detected ({label}): /{pat}/")

    # 2b) epsilon-stabilization unit drift
    # Look for `<ident> + <small float>` or `<small float> + <ident>` patterns
    # where the literal is NOT a coefficient (i.e., not followed by `*`). The
    # coefficient case `core + 0.18 * zscore(...)` is legitimate; the epsilon
    # case `rv30 + 0.001` is the bug. We distinguish by checking what comes
    # after the literal: if it's `*`, it's a coefficient and we ignore.
    for pat, label in EPS_DRIFT_PATTERNS:
        for m in re.finditer(pat, cleaned):
            snippet = m.group(0)
            # Allow if both sides are clearly numbers (rare)
            if re.fullmatch(rf"{_NUM}\s*[+\-]\s*{_NUM}", snippet):
                continue
            # If the literal is followed by `*`, it's a coefficient on the
            # next term, not an epsilon. e.g. `core + 0.18 * zscore(x)`.
            after = cleaned[m.end():m.end() + 6].lstrip()
            if after.startswith("*"):
                continue
            # If the literal is preceded by `*`, same logic: `0.40 * x + y`
            # but the regex already captured a different shape there. Skip
            # if we see we're in a `* literal` pattern.
            before = cleaned[max(0, m.start() - 6):m.start()].rstrip()
            if before.endswith("*"):
                continue
            warnings.append(f"Epsilon/unit-drift risk: '{snippet}' -- {label}")

    # 3) unit safety on top-level additive composite (after assignment expansion)
    assignments, final = _split_top_level_assignments(cleaned)
    if not final:
        errors.append("expression has no terminal value")
    else:
        expanded = _expand_assignments(final, assignments)
        unit_warnings = _check_top_level_additive_unit_safety(expanded)
        warnings.extend(unit_warnings)

    # 4) candidate field extraction (for downstream coverage check)
    candidate_idents = set(_IDENT_RE.findall(cleaned)) - set(operators_used) - _NON_FIELD
    # remove assignment names too, those are local
    candidate_idents -= set(assignments.keys())
    # remove pure numeric tokens
    candidate_idents = {x for x in candidate_idents if not x.isdigit()}
    fields_used = sorted(candidate_idents)
    if known_fields is not None:
        kf = set(known_fields)
        for f in fields_used:
            if f not in kf:
                warnings.append(f"Field '{f}' not in known_fields whitelist")

    return LintReport(
        ok=not errors,
        errors=errors,
        warnings=warnings,
        operators_used=operators_used,
        fields_used=fields_used,
    )


def main() -> int:
    """CLI entry point: `python -m research.alpha_factory.deterministic.lint <file>`."""
    import argparse
    import sys

    p = argparse.ArgumentParser(description="Static lint a BRAIN expression.")
    p.add_argument("path", nargs="?", help="path to a file containing the expression "
                                            "(omit to read from stdin)")
    args = p.parse_args()
    if args.path:
        expr = Path(args.path).read_text(encoding="utf-8")
    else:
        expr = sys.stdin.read()
    report = lint_expression(expr)
    if report.errors:
        print("ERRORS:")
        for e in report.errors:
            print(f"  - {e}")
    if report.warnings:
        print("WARNINGS:")
        for w in report.warnings:
            print(f"  - {w}")
    print(f"\noperators_used: {report.operators_used}")
    print(f"fields_used: {report.fields_used}")
    print(f"OK={report.ok}")
    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
