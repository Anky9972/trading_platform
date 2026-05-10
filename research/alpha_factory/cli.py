"""Alpha factory CLI — invoked from trade.py via the `af` subcommand.

Subcommands (all P0 deterministic; no LLM, no network beyond BRAIN):

    trade.py af lint    "<expression>"
    trade.py af lint    --file path.txt
    trade.py af list    [--verdict promote|kill|iterate|pending]
    trade.py af show    <alpha_id>
    trade.py af kill-theme <theme> --rationale "..."
    trade.py af themes
    trade.py af submit  --file path.txt --neutralization sector --decay 0
    trade.py af seed    --json path/to/seeds.json
    trade.py af stats

The `submit` path runs lint -> dedup -> register -> BRAIN submit -> poll ->
attach metrics. It does not yet run the LLM personas (P1+).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Sequence

from .deterministic.lint import lint_expression
from .deterministic.dedup import alpha_id as compute_alpha_id
from .deterministic.fitness import AlphaMetrics, compute_fitness, verdict_from_fitness
from .infra.factor_store import FactorStore, DEFAULT_DB_PATH
from .schemas import SubmissionMetrics, Verdict


# ----- helpers -----

def _read_expression(args: argparse.Namespace) -> str:
    if getattr(args, "file", None):
        return Path(args.file).read_text(encoding="utf-8")
    if getattr(args, "expression", None):
        return args.expression
    if not sys.stdin.isatty():
        return sys.stdin.read()
    raise SystemExit("error: provide expression as positional arg, --file, or stdin")


def _print_report(report) -> None:
    if report.errors:
        print("ERRORS:")
        for e in report.errors:
            print(f"  - {e}")
    if report.warnings:
        print("WARNINGS:")
        for w in report.warnings:
            print(f"  - {w}")
    print(f"\noperators_used ({len(report.operators_used)}): {report.operators_used}")
    print(f"fields_used ({len(report.fields_used)}): {report.fields_used}")
    print(f"OK={report.ok}")


# ----- subcommands -----

def cmd_lint(args: argparse.Namespace) -> int:
    expr = _read_expression(args)
    report = lint_expression(expr)
    _print_report(report)
    return 0 if report.ok else 1


def cmd_list(args: argparse.Namespace) -> int:
    store = FactorStore(args.db)
    rows = store.list_alphas(verdict=args.verdict, limit=args.limit)
    if args.json:
        print(json.dumps(rows, indent=2, default=str))
        return 0
    print(f"{'alpha_id':18s} {'verdict':10s} {'fitness':>8s} {'sharpe':>7s} "
          f"{'turnover':>9s} {'archetype':20s} expression")
    print("-" * 120)
    for r in rows:
        expr = (r.get("expression") or "").replace("\n", " ").strip()
        if len(expr) > 50:
            expr = expr[:47] + "..."
        print(
            f"{r['alpha_id']:18s} "
            f"{(r.get('verdict') or '-'):10s} "
            f"{(r.get('fitness') or 0.0):8.3f} "
            f"{(r.get('sharpe_full') or 0.0):7.3f} "
            f"{(r.get('turnover') or 0.0):9.3f} "
            f"{(r.get('archetype') or '-'):20s} "
            f"{expr}"
        )
    print(f"\n{len(rows)} rows")
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    store = FactorStore(args.db)
    row = store.get(args.alpha_id)
    if not row:
        print(f"alpha_id {args.alpha_id} not found")
        return 1
    print(json.dumps(row, indent=2, default=str))
    return 0


def cmd_themes(args: argparse.Namespace) -> int:
    store = FactorStore(args.db)
    rows = store.list_dead_themes()
    if not rows:
        print("(no dead themes registered)")
        return 0
    print(f"{'theme':30s} {'universe':10s} {'region':6s} {'last_sharpe':>11s}  rationale")
    print("-" * 100)
    for r in rows:
        print(
            f"{r['theme']:30s} {r['universe']:10s} {r['region']:6s} "
            f"{(r.get('last_sharpe') or 0.0):11.3f}  {r.get('rationale','')}"
        )
    return 0


def cmd_kill_theme(args: argparse.Namespace) -> int:
    store = FactorStore(args.db)
    store.kill_theme(
        args.theme,
        universe=args.universe,
        region=args.region,
        last_sharpe=args.last_sharpe,
        last_alpha_id=args.last_alpha_id,
        rationale=args.rationale,
    )
    print(f"theme '{args.theme}' marked dead on {args.universe}/{args.region}")
    return 0


def cmd_submit(args: argparse.Namespace) -> int:
    """Lint -> dedup-register -> (optionally) BRAIN submit -> poll -> attach.

    Without --live, this stops after registration so you can inspect the
    candidate before paying a credit. With --live, it actually contacts BRAIN.
    """
    expr = _read_expression(args)
    report = lint_expression(expr)
    if not report.ok:
        print("LINT FAILED -- not registering")
        _print_report(report)
        return 2
    if report.warnings:
        print("LINT WARNINGS (non-blocking but inspect):")
        for w in report.warnings:
            print(f"  - {w}")

    store = FactorStore(args.db)
    try:
        aid = store.register_alpha(
            expr,
            neutralization=args.neutralization,
            decay=args.decay,
            universe=args.universe,
            region=args.region,
            delay=args.delay,
            archetype=args.archetype,
            anomaly_tag=args.anomaly_tag,
            fields_used=report.fields_used,
            operators_used=report.operators_used,
            notes=args.notes,
        )
    except ValueError as e:
        print(f"DEDUP REJECT: {e}")
        return 3

    print(f"REGISTERED alpha_id={aid}")

    if not args.live:
        print("(--live not set; skipping BRAIN submission)")
        return 0

    # Lazy import to keep `lint` etc. dependency-free
    from .infra.wq_client import BrainClient, BrainSettings, from_env, BrainAuthError

    try:
        client = from_env()
    except BrainAuthError as e:
        print(f"BRAIN auth failed: {e}")
        return 4

    settings = BrainSettings(
        region=args.region, universe=args.universe, delay=args.delay,
        decay=args.decay, neutralization=args.neutralization,
    )
    print(f"submitting to BRAIN ...")
    sim_id = client.submit_simulation(expr, settings=settings)
    print(f"simulation_id={sim_id}, polling ...")
    result = client.poll_simulation(sim_id, timeout_seconds=args.timeout)
    # extract whatever metrics we can; BRAIN response shape varies per env
    is_data = result.get("is", {})
    metrics = SubmissionMetrics(
        alpha_id=aid,
        sharpe_full=float(is_data.get("sharpe", 0.0) or 0.0),
        turnover=float(is_data.get("turnover", 0.0) or 0.0),
        max_drawdown=float(is_data.get("drawdown", 0.0) or 0.0),
        margin_pct=float(is_data.get("margin", 0.0) or 0.0),
        raw=result,
    )
    cmax, cmin = client.get_self_correlation(sim_id)
    if cmax is not None:
        metrics.self_corr_max = cmax
    if cmin is not None:
        metrics.self_corr_min = cmin
    store.attach_metrics(aid, metrics)

    am = AlphaMetrics(
        sharpe_full=metrics.sharpe_full,
        turnover=metrics.turnover,
        max_drawdown=metrics.max_drawdown,
        max_corr_to_library=metrics.self_corr_max,
    )
    fit = compute_fitness(am)
    decision = verdict_from_fitness(fit)
    verdict = Verdict(
        alpha_id=aid, decision=decision, fitness=fit,
        rationale=f"auto-fitness={fit:.3f}",
    )
    store.attach_verdict(verdict)
    print(f"DONE: sharpe={metrics.sharpe_full:.3f} turnover={metrics.turnover:.3f} "
          f"corr_max={metrics.self_corr_max:.3f} fitness={fit:.3f} verdict={decision}")
    return 0


def cmd_seed(args: argparse.Namespace) -> int:
    """Bulk-load existing alphas (your 18 + v1..v4) into the factor store.

    JSON shape (list of objects):
        [
          {"expression": "...", "neutralization": "subindustry", "decay": 0,
           "archetype": "value_quality_blend", "anomaly_tag": "value",
           "metrics": {"sharpe_full": 2.78, "turnover": 0.65, ...},
           "verdict": "promote", "fitness": 2.5, "notes": "Alpha 6"}
        ]
    """
    seeds = json.loads(Path(args.json).read_text(encoding="utf-8"))
    store = FactorStore(args.db)
    inserted = skipped = 0
    for s in seeds:
        try:
            aid = store.register_alpha(
                s["expression"],
                neutralization=s.get("neutralization", ""),
                decay=int(s.get("decay", 0)),
                universe=s.get("universe", "TOP3000"),
                region=s.get("region", "USA"),
                delay=int(s.get("delay", 1)),
                archetype=s.get("archetype"),
                anomaly_tag=s.get("anomaly_tag"),
                notes=s.get("notes"),
            )
        except ValueError:
            skipped += 1
            continue
        inserted += 1
        m = s.get("metrics") or {}
        if m:
            metrics = SubmissionMetrics(alpha_id=aid, **{
                k: v for k, v in m.items()
                if k in SubmissionMetrics.__dataclass_fields__ and k != "alpha_id"
            })
            store.attach_metrics(aid, metrics)
        if s.get("verdict"):
            store.attach_verdict(Verdict(
                alpha_id=aid,
                decision=s["verdict"],
                fitness=float(s.get("fitness", 0.0)),
                rationale=s.get("rationale", "seeded"),
            ))
    print(f"seeded: {inserted} inserted, {skipped} skipped (duplicates)")
    return 0


def cmd_stats(args: argparse.Namespace) -> int:
    store = FactorStore(args.db)
    all_rows = store.list_alphas(limit=10_000)
    by_verdict = {}
    for r in all_rows:
        by_verdict.setdefault(r.get("verdict") or "?", 0)
        by_verdict[r.get("verdict") or "?"] += 1
    print(f"factor store: {args.db}")
    print(f"  total alphas: {len(all_rows)}")
    for v, n in sorted(by_verdict.items(), key=lambda kv: -kv[1]):
        print(f"  {v:10s} {n}")
    print(f"  dead themes: {len(store.list_dead_themes())}")
    return 0


# ----- arg parser -----

def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="trade.py af",
                                description="Alpha Factory CLI")
    p.add_argument("--db", default=str(DEFAULT_DB_PATH),
                   help="path to factor_store.db (default: package data/)")
    sub = p.add_subparsers(dest="cmd", required=True)

    p_lint = sub.add_parser("lint", help="static-lint a BRAIN expression")
    p_lint.add_argument("expression", nargs="?")
    p_lint.add_argument("--file")
    p_lint.set_defaults(func=cmd_lint)

    p_list = sub.add_parser("list", help="list alphas in factor store")
    p_list.add_argument("--verdict", choices=["promote", "iterate", "kill", "pending"])
    p_list.add_argument("--limit", type=int, default=50)
    p_list.add_argument("--json", action="store_true")
    p_list.set_defaults(func=cmd_list)

    p_show = sub.add_parser("show", help="show full row for one alpha_id")
    p_show.add_argument("alpha_id")
    p_show.set_defaults(func=cmd_show)

    p_themes = sub.add_parser("themes", help="list dead themes")
    p_themes.set_defaults(func=cmd_themes)

    p_kt = sub.add_parser("kill-theme", help="register a dead theme")
    p_kt.add_argument("theme")
    p_kt.add_argument("--universe", default="TOP3000")
    p_kt.add_argument("--region", default="USA")
    p_kt.add_argument("--last-sharpe", type=float, default=None)
    p_kt.add_argument("--last-alpha-id", default=None)
    p_kt.add_argument("--rationale", default="")
    p_kt.set_defaults(func=cmd_kill_theme)

    p_sub = sub.add_parser("submit", help="lint + register (+ submit if --live)")
    p_sub.add_argument("expression", nargs="?")
    p_sub.add_argument("--file")
    p_sub.add_argument("--neutralization", default="NONE",
                       help="NONE/sector/industry/subindustry; defaults to NONE because "
                            "you typically embed group_neutralize() inside the expr")
    p_sub.add_argument("--decay", type=int, default=0)
    p_sub.add_argument("--universe", default="TOP3000")
    p_sub.add_argument("--region", default="USA")
    p_sub.add_argument("--delay", type=int, default=1)
    p_sub.add_argument("--archetype", default=None)
    p_sub.add_argument("--anomaly-tag", default=None)
    p_sub.add_argument("--notes", default=None)
    p_sub.add_argument("--live", action="store_true",
                       help="actually contact BRAIN (otherwise stop after register)")
    p_sub.add_argument("--timeout", type=int, default=600)
    p_sub.set_defaults(func=cmd_submit)

    p_seed = sub.add_parser("seed", help="bulk-load alphas from JSON")
    p_seed.add_argument("--json", required=True)
    p_seed.set_defaults(func=cmd_seed)

    p_st = sub.add_parser("stats", help="counts by verdict + dead themes")
    p_st.set_defaults(func=cmd_stats)

    return p


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
