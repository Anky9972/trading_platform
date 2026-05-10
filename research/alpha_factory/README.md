# Alpha Factory

Open-source research pipeline for WorldQuant BRAIN, embedded inside the
`trading_platform` so it can reuse the existing spine (SQLite WAL, subprocess
sandbox, kill switches, event log, HMAC IPC, alerting).

See `research/alpha_factory_integration_plan.md` for the phased plan.

## Quick start

```bash
# Lint a candidate expression
python trade.py af lint "group_neutralize(rank(zscore(returns)), sector)"
python trade.py af lint --file path/to/alpha.txt

# Seed factor store with existing alphas
python trade.py af seed --json research/alpha_factory/data/seeds_existing_alphas.json

# Inspect the factor store
python trade.py af stats
python trade.py af list --verdict promote
python trade.py af list --verdict kill
python trade.py af show <alpha_id>

# Register a new candidate (lint + dedup + store; no BRAIN call)
python trade.py af submit "rank(zscore(close - open))" --archetype experimental

# Submit live to BRAIN (requires BRAIN_EMAIL / BRAIN_PASSWORD env vars)
python trade.py af submit --file my_alpha.txt --live --neutralization NONE

# Mark a theme dead so the theme sampler avoids it for 6 months
python trade.py af kill-theme "contrarian_pead_analyst_revisions" \
    --universe TOP3000 --region USA \
    --last-sharpe 0.45 \
    --rationale "Family capped sub-1.0 Sharpe across v1-v4"

# View dead themes
python trade.py af themes
```

## What's implemented (P0 — done)

| Module | Status | Notes |
|---|---|---|
| `deterministic/lint.py` | ✅ | operator validity, look-ahead, unit drift, top-level additive composite check. **Catches the Alpha 19 v1 epsilon bug.** |
| `deterministic/dedup.py` | ✅ | sha256 over normalized expression + settings |
| `deterministic/fitness.py` | ✅ | scalar fitness function, verdict mapping |
| `infra/factor_store.py` | ✅ | SQLite WAL, dedup-on-insert, dead_themes registry |
| `infra/wq_client.py` | ✅ | BRAIN submit/poll/correlation wrapper |
| `cli.py` | ✅ | wired into `trade.py af ...` |
| `tests/test_lint.py` | ✅ | 8/8 passing — includes regression for v1 unit bug |
| `tests/test_factor_store.py` | ✅ | 5/5 passing |

## What's not yet implemented (P1+)

| Module | Phase | Notes |
|---|---|---|
| `local/brain_sim.py` | P1 | local IS-test simulator using yfinance OHLCV |
| `personas/expression_compiler.py` | P1 | hybrid Jinja archetype + LLM compiler |
| `personas/performance_surgeon.py` | P1 | sign-error / regime / decay diagnostician |
| `deterministic/regime_tagger.py` | P1 | VIX/trend/rate/style regimes |
| `deterministic/theme_sampler.py` | P1 | gap analysis from datasets.csv |
| `templates/*.j2` | P1 | 5 archetypes from your top alphas (6, 15, 8, 11, 1) |
| `infra/llm_client.py` | P2 | vLLM/Ollama with `outlines` JSON guard |
| `infra/rag.py` | P2 | ChromaDB + arXiv ingest |
| `personas/hypothesis_hunter.py` | P2 | Microfish (1.5B) ideation |
| `personas/crowd_scout.py` | P2 | Mediumfish corr + thematic novelty |
| `orchestration/pipeline.py` | P2 | smolagents DAG |
| `personas/gatekeeper.py` | P3 | Bigfish production memo |

## Design principles

1. **Pure-function-first.** Only `infra/factor_store.py` and `infra/wq_client.py`
   carry persistent state.
2. **Deterministic before LLM.** Every LLM gate has a deterministic baseline.
3. **Pydantic at every boundary** (P1+).
4. **Kill switches.** Daily submission caps, consecutive-fail caps.
5. **Reuse the trading_platform spine.** No new IPC, no new alerting, no new
   sandbox — those already exist in the parent platform.

## Reference data

- `data/operators.csv` — the BRAIN operator catalog (synced from
  `e:\worldquant\operators.csv` 2026-05-07).
- `data/datasets.csv` — accessible datasets for USA/TOP3000/D1.
- `data/seeds_existing_alphas.json` — 11 production alphas + Alpha 19 v1-v4
  (15 entries total) for seeding the factor store.
- `data/factor_store.db` — created on first use.

## Known limitations (P0)

1. The lint cannot validate field-coverage requirements without the per-field
   coverage table. Coverage check ships in P1.
2. The BRAIN client's response-shape extraction is a best-effort wrapper —
   exact field names may need tuning against your account's API responses
   the first time you `--live` submit.
3. The lint's unit-drift heuristic is regex-based, not AST-based. False
   negatives possible on novel patterns; PRs welcome.

## Running the tests

```bash
cd "e:/HFT infra/trading_platform"
python -m research.alpha_factory.tests.test_lint
python -m pytest research/alpha_factory/tests/ -v
```
