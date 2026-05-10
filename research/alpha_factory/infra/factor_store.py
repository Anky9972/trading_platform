"""Factor store — single source of truth for every alpha submitted to BRAIN.

Schema mirrors the design in alpha_factory_product_doc.md §6.4. Backed by
SQLite (consistent with the trading_platform's existing store/database.py).
We use a *separate* DB file (factor_store.db by default) so research state
doesn't pollute the live trading state.
"""
from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Iterator, Optional

from ..schemas import SubmissionMetrics, Verdict
from ..deterministic.dedup import alpha_id as compute_alpha_id

DEFAULT_DB_PATH = Path(__file__).parent.parent / "data" / "factor_store.db"

DDL = """
CREATE TABLE IF NOT EXISTS alphas (
    alpha_id            TEXT PRIMARY KEY,
    submitted_at        REAL NOT NULL,        -- unix epoch seconds
    expression          TEXT NOT NULL,
    neutralization      TEXT NOT NULL DEFAULT '',
    decay               INTEGER NOT NULL DEFAULT 0,
    universe            TEXT NOT NULL DEFAULT 'TOP3000',
    region              TEXT NOT NULL DEFAULT 'USA',
    delay               INTEGER NOT NULL DEFAULT 1,
    archetype           TEXT,
    anomaly_tag         TEXT,
    fields_used_json    TEXT,                 -- JSON array
    operators_used_json TEXT,                 -- JSON array
    metrics_json        TEXT,                 -- full SubmissionMetrics JSON blob
    sharpe_full         REAL,
    sharpe_is           REAL,
    sharpe_os           REAL,
    turnover            REAL,
    max_drawdown        REAL,
    self_corr_max       REAL,
    fitness             REAL,
    verdict             TEXT,                 -- 'promote'|'iterate'|'kill'|'pending'
    verdict_rationale   TEXT,
    gatekeeper_memo     TEXT,
    notes               TEXT
);

CREATE INDEX IF NOT EXISTS idx_alphas_archetype ON alphas(archetype);
CREATE INDEX IF NOT EXISTS idx_alphas_anomaly ON alphas(anomaly_tag);
CREATE INDEX IF NOT EXISTS idx_alphas_verdict ON alphas(verdict);
CREATE INDEX IF NOT EXISTS idx_alphas_fitness ON alphas(fitness DESC);

CREATE TABLE IF NOT EXISTS dead_themes (
    theme           TEXT NOT NULL,
    universe        TEXT NOT NULL,
    region          TEXT NOT NULL,
    date_killed     REAL NOT NULL,
    last_sharpe     REAL,
    last_alpha_id   TEXT,
    rationale       TEXT,
    PRIMARY KEY (theme, universe, region)
);
"""


class FactorStore:
    """Thin DAO over the alphas + dead_themes tables."""

    def __init__(self, db_path: str | Path = DEFAULT_DB_PATH):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _init_db(self) -> None:
        with self._conn() as c:
            c.executescript(DDL)
            c.execute("PRAGMA journal_mode=WAL;")

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        c = sqlite3.connect(self.db_path, isolation_level=None)
        c.row_factory = sqlite3.Row
        try:
            yield c
        finally:
            c.close()

    # ---------- writes ----------

    def register_alpha(
        self,
        expression: str,
        *,
        neutralization: str = "",
        decay: int = 0,
        universe: str = "TOP3000",
        region: str = "USA",
        delay: int = 1,
        archetype: Optional[str] = None,
        anomaly_tag: Optional[str] = None,
        fields_used: Optional[list[str]] = None,
        operators_used: Optional[list[str]] = None,
        notes: Optional[str] = None,
    ) -> str:
        """Insert a new alpha row in the 'pending' state. Returns alpha_id.

        Raises ValueError if duplicate (same expression + neutralization + decay).
        """
        aid = compute_alpha_id(
            expression, neutralization=neutralization, decay=decay,
            universe=universe, region=region, delay=delay,
        )
        with self._conn() as c:
            row = c.execute("SELECT 1 FROM alphas WHERE alpha_id=?", (aid,)).fetchone()
            if row:
                raise ValueError(f"Duplicate alpha_id {aid} (expression already submitted)")
            c.execute(
                """
                INSERT INTO alphas (
                    alpha_id, submitted_at, expression, neutralization, decay,
                    universe, region, delay, archetype, anomaly_tag,
                    fields_used_json, operators_used_json, verdict, notes
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    aid, time.time(), expression, neutralization, decay,
                    universe, region, delay, archetype, anomaly_tag,
                    json.dumps(fields_used or []), json.dumps(operators_used or []),
                    "pending", notes,
                ),
            )
        return aid

    def attach_metrics(self, alpha_id: str, metrics: SubmissionMetrics) -> None:
        with self._conn() as c:
            c.execute(
                """
                UPDATE alphas SET
                    metrics_json=?, sharpe_full=?, sharpe_is=?, sharpe_os=?,
                    turnover=?, max_drawdown=?, self_corr_max=?
                WHERE alpha_id=?
                """,
                (
                    json.dumps(asdict(metrics)),
                    metrics.sharpe_full, metrics.sharpe_is, metrics.sharpe_os,
                    metrics.turnover, metrics.max_drawdown, metrics.self_corr_max,
                    alpha_id,
                ),
            )

    def attach_verdict(self, verdict: Verdict, gatekeeper_memo: str = "") -> None:
        with self._conn() as c:
            c.execute(
                "UPDATE alphas SET fitness=?, verdict=?, verdict_rationale=?, "
                "gatekeeper_memo=? WHERE alpha_id=?",
                (
                    verdict.fitness, verdict.decision, verdict.rationale,
                    gatekeeper_memo, verdict.alpha_id,
                ),
            )

    def kill_theme(
        self, theme: str, *, universe: str, region: str,
        last_sharpe: float | None = None, last_alpha_id: str | None = None,
        rationale: str = "",
    ) -> None:
        with self._conn() as c:
            c.execute(
                """
                INSERT OR REPLACE INTO dead_themes
                    (theme, universe, region, date_killed,
                     last_sharpe, last_alpha_id, rationale)
                VALUES (?,?,?,?,?,?,?)
                """,
                (theme, universe, region, time.time(),
                 last_sharpe, last_alpha_id, rationale),
            )

    # ---------- reads ----------

    def get(self, alpha_id: str) -> dict | None:
        with self._conn() as c:
            row = c.execute("SELECT * FROM alphas WHERE alpha_id=?", (alpha_id,)).fetchone()
            return dict(row) if row else None

    def list_alphas(
        self, *, verdict: str | None = None, limit: int = 200,
    ) -> list[dict]:
        with self._conn() as c:
            if verdict:
                rows = c.execute(
                    "SELECT * FROM alphas WHERE verdict=? "
                    "ORDER BY fitness DESC NULLS LAST LIMIT ?",
                    (verdict, limit),
                ).fetchall()
            else:
                rows = c.execute(
                    "SELECT * FROM alphas ORDER BY submitted_at DESC LIMIT ?",
                    (limit,),
                ).fetchall()
            return [dict(r) for r in rows]

    def is_dead_theme(self, theme: str, *, universe: str, region: str) -> bool:
        with self._conn() as c:
            row = c.execute(
                "SELECT 1 FROM dead_themes WHERE theme=? AND universe=? AND region=?",
                (theme, universe, region),
            ).fetchone()
            return row is not None

    def list_dead_themes(self) -> list[dict]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM dead_themes ORDER BY date_killed DESC"
            ).fetchall()
            return [dict(r) for r in rows]
