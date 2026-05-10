"""Tests for the factor store DAO."""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from research.alpha_factory.infra.factor_store import FactorStore
from research.alpha_factory.schemas import SubmissionMetrics, Verdict


@pytest.fixture
def store(tmp_path: Path) -> FactorStore:
    return FactorStore(db_path=tmp_path / "test_factor_store.db")


def test_register_alpha_returns_stable_id(store: FactorStore) -> None:
    aid = store.register_alpha(
        "rank(close)", neutralization="sector", decay=0,
    )
    assert aid
    row = store.get(aid)
    assert row is not None
    assert row["expression"] == "rank(close)"
    assert row["verdict"] == "pending"


def test_register_duplicate_raises(store: FactorStore) -> None:
    store.register_alpha("rank(close)", neutralization="sector", decay=0)
    with pytest.raises(ValueError):
        store.register_alpha("rank(close)", neutralization="sector", decay=0)


def test_attach_metrics_and_verdict(store: FactorStore) -> None:
    aid = store.register_alpha("rank(close)")
    metrics = SubmissionMetrics(
        alpha_id=aid, sharpe_full=1.5, turnover=0.30, max_drawdown=0.05,
        self_corr_max=0.4,
    )
    store.attach_metrics(aid, metrics)
    row = store.get(aid)
    assert row["sharpe_full"] == pytest.approx(1.5)
    assert row["turnover"] == pytest.approx(0.30)

    verdict = Verdict(
        alpha_id=aid, decision="promote", fitness=2.1,
        rationale="clean, low corr, no decay",
    )
    store.attach_verdict(verdict)
    row = store.get(aid)
    assert row["verdict"] == "promote"
    assert row["fitness"] == pytest.approx(2.1)


def test_dead_themes_registry(store: FactorStore) -> None:
    assert not store.is_dead_theme("pead", universe="TOP3000", region="USA")
    store.kill_theme(
        "pead", universe="TOP3000", region="USA",
        last_sharpe=0.45, last_alpha_id="abc123",
        rationale="contrarian-PEAD on TOP3000 2019-2023 caps at 0.6 Sharpe",
    )
    assert store.is_dead_theme("pead", universe="TOP3000", region="USA")
    assert not store.is_dead_theme("pead", universe="TOP1000", region="USA")
    rows = store.list_dead_themes()
    assert len(rows) == 1
    assert rows[0]["theme"] == "pead"


def test_list_alphas_filter_by_verdict(store: FactorStore) -> None:
    a1 = store.register_alpha("rank(close)")
    a2 = store.register_alpha("rank(volume)")
    store.attach_verdict(Verdict(
        alpha_id=a1, decision="promote", fitness=2.0, rationale="ok",
    ))
    store.attach_verdict(Verdict(
        alpha_id=a2, decision="kill", fitness=-0.5, rationale="dead",
    ))
    promoted = store.list_alphas(verdict="promote")
    killed = store.list_alphas(verdict="kill")
    assert len(promoted) == 1 and promoted[0]["alpha_id"] == a1
    assert len(killed) == 1 and killed[0]["alpha_id"] == a2


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
