# Alpha Factory — Integration Plan inside `trading_platform`

**Source design:** `e:\worldquant\alpha_factory_product_doc.md` (the 7-persona pipeline)
**Target host:** `e:\HFT infra\trading_platform\` (existing live-execution platform)
**Date:** 2026-05-07
**Premise:** the trading platform already has 70% of the boring infra (SQLite WAL, subprocess sandbox, kill switches, event logging, alerting, HMAC-IPC). Reuse those. Don't rebuild.

---

## 1. What to reuse, what to build, what to skip

### 1.1 Reuse (already in `trading_platform/`, no new code)

| Existing module | Original purpose | Reuse for alpha factory |
|-----------------|------------------|-------------------------|
| `store/database.py` (SQLite WAL) | order/state persistence | factor_store: extend schema with `alphas` table |
| `store/event_log.py` (JSONL) | engine event audit | LLM call tracing (replaces Langfuse for MVP) |
| `strategy/runner.py` (subprocess sandbox) | runs user strategies | runs compiled WQ expressions in Python sim — same pattern |
| `risk/server.py` (fail-closed AF_UNIX) | trade-time risk checks | pattern for the static lint server |
| `risk/kill_switch.py` (file-based) | emergency stop | budget kill switches (daily submissions, consecutive lint fails) |
| `monitor/alerting.py` | OS toast + log | "pipeline stuck" notifications |
| `core/event_log` (existing patterns) | structured audit | every persona emit logged to events.jsonl |
| `ipc/socket_server.py` (HMAC) | CLI ↔ engine | CLI ↔ alpha factory daemon |
| `trade.py` (CLI) | user entry point | extend with `trade.py af ...` subcommand |

### 1.2 Build (new code in `research/alpha_factory/`)

| Module | Purpose | Priority | LOC est |
|--------|---------|----------|---------|
| `deterministic/lint.py` | Layer-2 static lint (operator validity, look-ahead, unit safety) | **P0** | ~200 |
| `deterministic/dedup.py` | Hash-based dedup against factor store | P0 | ~50 |
| `deterministic/fitness.py` | Single-scalar fitness function | P0 | ~80 |
| `deterministic/regime_tagger.py` | VIX/trend/rate/style regime tags | P1 | ~150 |
| `deterministic/theme_sampler.py` | Gap analysis from coverage tables | P1 | ~120 |
| `infra/factor_store.py` | Schema + DAO over existing SQLite | **P0** | ~150 |
| `infra/wq_client.py` | Wrap existing `e:\worldquant\file.py` for submit/harvest | **P0** | ~200 |
| `infra/llm_client.py` | vLLM/Ollama with `outlines` JSON guard | P1 | ~100 |
| `infra/rag.py` | ChromaDB + arXiv loader | P2 | ~150 |
| `personas/hypothesis_hunter.py` | Microfish: blueprint generator | P2 | ~120 |
| `personas/expression_compiler.py` | Hybrid Jinja + Tinyfish | P1 | ~150 |
| `personas/crowd_scout.py` | Mediumfish: corr + thematic novelty | P2 | ~100 |
| `personas/performance_surgeon.py` | Mediumfish: regime/decay/sign-error detector | P1 | ~150 |
| `personas/gatekeeper.py` | Bigfish: production memo | P3 | ~100 |
| `local/brain_sim.py` | Layer-4: local pre-flight using yfinance/polars | **P0** | ~400 |
| `orchestration/pipeline.py` | smolagents DAG | P2 | ~250 |
| `cli.py` | `trade.py af generate / submit / list / kill` | P1 | ~150 |
| `templates/*.j2` | Jinja archetype library (5 from your top alphas) | P1 | ~200 |
| `prompts/*.j2` | persona system prompts | P2 | ~300 |
| `tests/test_lint.py` | unit tests against your v1/v2/v3/v4 alphas | **P0** | ~100 |
| `tests/test_factor_store.py` | DAO tests | P0 | ~80 |
| `tests/test_brain_sim.py` | local sim sanity | P1 | ~100 |

**P0 total ≈ 1,200 LOC (ship in week 1).** Everything else is layered on after the P0 core proves the loop.

### 1.3 Skip (out of scope for now)

| Skipped | Why |
|---------|-----|
| Langfuse tracing | reuse `events.jsonl` + grep for MVP |
| Redis queueing | asyncio + sqlite is fine for ≤ 200 alphas/day |
| Qdrant / vector DB | ChromaDB over SQLite is enough |
| Bigfish 70B production gate | only 5 alphas/week reach it; defer to P3 |
| QLoRA fine-tune of Qwen-Coder-7B | optional, $6 one-time; not blocking MVP |
| Prometheus / Grafana | events.jsonl tail is enough for one operator |

---

## 2. Directory layout (additive — no existing files moved)

```
trading_platform/
├── ... (existing stays untouched)
└── research/
    ├── features.py                         # (existing)
    ├── alpha_factory_integration_plan.md   # (this file)
    └── alpha_factory/                      # NEW package
        ├── __init__.py
        ├── README.md
        ├── cli.py                          # invoked from trade.py af ...
        ├── config.py                       # pydantic settings, kill switches
        ├── deterministic/
        │   ├── __init__.py
        │   ├── lint.py                     # P0
        │   ├── dedup.py                    # P0
        │   ├── fitness.py                  # P0
        │   ├── regime_tagger.py            # P1
        │   └── theme_sampler.py            # P1
        ├── infra/
        │   ├── __init__.py
        │   ├── factor_store.py             # P0  (extends store/database.py)
        │   ├── wq_client.py                # P0  (wraps e:\worldquant\file.py)
        │   ├── llm_client.py               # P1
        │   └── rag.py                      # P2
        ├── personas/
        │   ├── __init__.py
        │   ├── hypothesis_hunter.py        # P2
        │   ├── expression_compiler.py      # P1
        │   ├── crowd_scout.py              # P2
        │   ├── performance_surgeon.py      # P1
        │   └── gatekeeper.py               # P3
        ├── local/
        │   ├── __init__.py
        │   └── brain_sim.py                # P0  layer-4 pre-flight
        ├── orchestration/
        │   ├── __init__.py
        │   └── pipeline.py                 # P2
        ├── schemas/
        │   ├── __init__.py
        │   ├── blueprint.py                # pydantic
        │   ├── expression.py
        │   └── verdict.py
        ├── templates/                      # Jinja2
        │   ├── archetype_value_quality.j2
        │   ├── archetype_intraday_mr_decay.j2
        │   ├── archetype_vol_scaled_shock.j2
        │   ├── archetype_pead_revisions.j2
        │   └── archetype_news_drift.j2
        ├── prompts/                        # persona system prompts
        │   ├── hypothesis_hunter.txt
        │   ├── expression_compiler.txt
        │   ├── crowd_scout.txt
        │   ├── performance_surgeon.txt
        │   └── gatekeeper.txt
        └── tests/
            ├── __init__.py
            ├── test_lint.py
            ├── test_factor_store.py
            └── test_brain_sim.py
```

---

## 3. CLI integration with `trade.py`

Add a single new subcommand `af` (alpha factory) to the existing `trade.py`:

```bash
# already existing
python trade.py status
python trade.py orders
python trade.py kill

# NEW — alpha factory
python trade.py af lint    "<expression>"           # static lint only
python trade.py af sim     "<expression>"           # local BRAIN sim
python trade.py af submit  "<expression>" [--name X] # full pipeline through submission
python trade.py af list                              # factor store rows
python trade.py af kill <alpha_id>                   # mark as dead in store
python trade.py af themes                            # gap analysis
python trade.py af generate --theme news_drift       # spawn hypothesis hunter
```

`trade.py af` dispatches to `research/alpha_factory/cli.py:main(argv)`. No daemon needed for the CLI commands; `submit` may spawn a background worker if running through the full pipeline.

---

## 4. Dependency additions to `requirements.txt`

```diff
 smartapi-python>=1.3.0
 pyotp>=2.8.0
 pandas>=2.0.0
 cryptography>=41.0.0
 yfinance>=0.2.0
+# alpha factory additions
+polars>=0.20.0           # local BRAIN sim
+pydantic>=2.5.0          # schemas across the pipeline
+jinja2>=3.1.0            # archetype templates
+aiohttp>=3.9.0           # async BRAIN client
+requests>=2.31.0         # already a dep of others; pin
+# optional (P1+)
+# outlines>=0.0.40       # JSON-constrained LLM decoding
+# chromadb>=0.4.0        # vector store for arXiv RAG
+# arxiv>=2.1.0           # paper ingest
```

---

## 5. Phased delivery — MVP in 1 week, full system in 4 weeks

### Week 1 — P0 core (deterministic, no LLM)
- [x] Plan written (this file)
- [ ] `deterministic/lint.py` — operator validity + look-ahead + unit safety. **Validates against my own v1 alpha unit-warning bug.**
- [ ] `deterministic/dedup.py` — sha256 hash check
- [ ] `deterministic/fitness.py` — fitness scalar function
- [ ] `infra/factor_store.py` — DDL + DAO; seed with your 18 alphas + Alpha 19 v1-v4
- [ ] `infra/wq_client.py` — wraps `e:\worldquant\file.py` for `submit_simulation()` + `get_alpha_metrics()`
- [ ] `local/brain_sim.py` — local IS-test simulator using cached yfinance OHLCV
- [ ] `cli.py` + `trade.py af ...` wiring
- [ ] `tests/test_lint.py` validates the lint catches the v1 unit bug

**Exit criterion:** `python trade.py af lint "<v4 expression>"` returns 0 errors, `python trade.py af sim "<v4 expression>"` returns Sharpe estimate within ±0.2 of BRAIN's 0.45.

### Week 2 — P1 (Jinja templates + Performance Surgeon + regime)
- [ ] 5 Jinja archetype templates from your top alphas (6, 15, 8, 11, 1)
- [ ] `personas/expression_compiler.py` (hybrid)
- [ ] `personas/performance_surgeon.py` (regime + decay + sign-error detection)
- [ ] `deterministic/regime_tagger.py` (VIX/trend/rate/style)
- [ ] `deterministic/theme_sampler.py` (gap analysis from coverage CSVs)
- [ ] First end-to-end Alpha 20 (news-drift) generated by template, linted, sim'd, submitted

**Exit criterion:** Alpha 20 ships through the Week-1 deterministic path + Week-2 surgeon, and posts ≥ Sharpe 1.0 on BRAIN.

### Week 3 — P2 (LLM personas + RAG)
- [ ] vLLM/Ollama serving Qwen2.5-1.5B + 7B locally
- [ ] `infra/llm_client.py` with `outlines` JSON-schema enforcement
- [ ] `personas/hypothesis_hunter.py` — Microfish ideation
- [ ] `personas/crowd_scout.py` — corr + thematic novelty with anomaly tagging
- [ ] `infra/rag.py` — ChromaDB + 5-year arXiv `q-fin` ingest
- [ ] `orchestration/pipeline.py` — smolagents DAG

**Exit criterion:** an autonomous run produces 50 candidate alphas/day; ≥ 5 reach BRAIN submission; ≥ 1 passes IS tests.

### Week 4 — P3 (gatekeeper + calibration + dead-themes registry)
- [ ] `personas/gatekeeper.py` — Bigfish memo for promote candidates
- [ ] Hand-rank 20 generated alphas → calibrate fitness coefficients
- [ ] Dead-themes registry (per the Acceptance Engineering doc)
- [ ] CI: pytest matrix on Linux for the alpha factory package
- [ ] Documentation pass

**Exit criterion:** ≥ 3 production-ready alphas in the factor store with ≥ 1.25 Sharpe + corr < 0.65 to existing library.

---

## 6. How the alpha factory plugs into the trading platform's *live* path

Out of scope for the immediate MVP, but the long-term picture:

```
Alpha factory (research)       trading_platform (live)
─────────────────────────      ───────────────────────
factor_store.alphas    ─────►  strategy/user_strategies/<id>.py
   ↓ (best K)                       ↓
RAG / hand-curate                Engine cycle
   ↓                                ↓
WQ BRAIN submission              Risk server / OMS
   ↓                                ↓
IS tests pass                    PaperBroker / Angel
   ↓                                ↓
Promote to live                  monitor / kill switch
```

Concretely: every promoted alpha in the factor store gets transpiled (or hand-translated) into a `strategy/user_strategies/<alpha_id>.py` file that follows the existing strategy contract (stdin OHLCV → stdout signal JSON). That gives you a **research-to-production path with a single canonical artifact (the WQ expression) and a single canonical store (factor_store)**. Out of scope for week 1, but the directory layout and schemas above are designed to make it trivial when it's time.

---

## 7. Defensive-engineering checklist — adopted from `acceptance_engineering.md`

Every code module added must satisfy:

1. **Pure-function-first.** No global state outside `infra/factor_store.py` and `infra/wq_client.py`.
2. **Pydantic schemas at every boundary.** Blueprint, Expression, Verdict, Metrics — all typed.
3. **Determinism over magic.** Anything an LLM does, a deterministic baseline must exist (Jinja templates, hash dedup, AST lint).
4. **Kill switches.** Daily submission cap, consecutive-lint-fail cap, daily-LLM-token cap. File-based, like the existing `risk/kill_switch.py`.
5. **Tests for every P0 module.** No P0 ships without a test that runs in `pytest research/alpha_factory/tests/`.
6. **No new external services.** Local everything. SQLite, ChromaDB, Ollama, all on one host.

---

## 8. Open questions for you (the operator)

1. **Where do I store cached yfinance OHLCV** for local BRAIN sim? Suggest `trading_platform/data/cache/yfinance/` with parquet partitioned by year. Confirm or override.
2. **Which existing alpha archetypes do you want as the first templates?** Default proposal: Alpha 6, 15, 8, 11, 1 (your top 5 by Sharpe). Confirm.
3. **Should the factor store live in the existing `trading.db`** (additive tables) **or in a separate** `factor_store.db`? Default proposal: separate DB to keep the trading state clean. Confirm.
4. **Do you want the LLM persona stack from week 3** or can we ship the deterministic pipeline only, on the basis that 80% of acceptance engineering value lives in deterministic gates (lint + sim + dedup) without needing any LLM at all? Default proposal: ship deterministic-only first, layer LLMs on after we have a working harness.

I'll proceed with the Week-1 P0 work next, default answers to the above unless you tell me otherwise.

*End of integration plan.*
