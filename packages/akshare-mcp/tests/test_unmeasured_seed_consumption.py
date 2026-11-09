"""Legacy daily-sync seeds cannot masquerade as measured active factors."""
from __future__ import annotations

import asyncio
import copy
import importlib.util
import json
from pathlib import Path

import pytest

from akshare_mcp.services.factor_mining_factory.api import FactorPoolGateway
from akshare_mcp.services.factor_mining_factory.pool.active_pool import ActiveFactorPool
from akshare_mcp.services.factor_mining_factory.quality import unmeasured_seed_reason


def seed(**changes):
    return {
        "factor_id": "rule_seed_momentum", "name": "rule_seed_momentum",
        "status": "active", "expression_dsl": "ts_mean(close, 20)",
        "inputs": ["close"], "family": "momentum", "fitness": 0.6,
        "admission_ic": 0.02, "current_ic": 0.02, "admission_grade": "B",
        "generation_trace": json.dumps(json.dumps({"source": "free_data_daily_sync"})),
        "validation_summary": {"quality_status": "promoted", "evidence_summary": {}},
        **changes,
    }


def test_daily_sync_defers_seed_admission_without_database_initialization(monkeypatch):
    import akshare_mcp.storage as storage

    def forbidden():
        raise AssertionError("daily sync must not initialize or write the factor pool")

    monkeypatch.setattr(storage, "get_db", forbidden)
    path = Path(__file__).resolve().parents[3] / "scripts/free_data_daily_sync.py"
    spec = importlib.util.spec_from_file_location("daily_sync_seed_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    report = asyncio.run(module.seed_active_factor_pool())
    assert report["written"] == 0
    assert report["saved"] == []
    assert report["reason"] == "seed_validation_evidence_missing"
    assert report["candidate_count"] > 0
    assert report["active_pool_size"] is None


def test_hydration_excludes_unmeasured_seed_and_preserves_original_record(monkeypatch):
    original = seed()
    before = copy.deepcopy(original)
    pool = ActiveFactorPool()
    monkeypatch.setattr(pool, "_compile_gate", lambda **kwargs: {"passed": True})
    pool.hydrate([original])
    assert pool._factors == {}
    assert original == before


@pytest.mark.parametrize("bypass", ["0", "1"])
def test_direct_pool_consumption_cannot_bypass_missing_seed_evidence(monkeypatch, bypass):
    monkeypatch.setenv("STRATEGY_FACTORY_FACTOR_POOL_ADMIT_ACTIVE_WITHOUT_PROMOTION", bypass)
    pool = ActiveFactorPool()
    pool._factors = {"legacy": seed()}
    assert asyncio.run(pool.get_active_factors()) == []
    assert FactorPoolGateway._is_research_consumable_factor(seed()) is False


def test_quarantine_fallback_does_not_resurrect_seed(monkeypatch):
    from akshare_mcp.services.factor_mining_factory.pool import storage

    async def load(*args, **kwargs):
        return [seed(), {"factor_id": "reviewable", "status": "quarantine"}]

    class Factory:
        def _ensure_initialized(self):
            pass

        async def _get_db(self):
            return object()

        async def _ensure_persistent_pool(self, db):
            pass

    monkeypatch.setattr(storage, "load_factor_pool_from_db", load)
    gateway = FactorPoolGateway()
    gateway._factory = Factory()
    assert asyncio.run(gateway.get_active_factors(include_quarantine=True)) == [
        {"factor_id": "reviewable", "status": "quarantine"}
    ]


def test_seed_evidence_requires_real_sample_counts_and_finite_metrics():
    measured = seed(validation_summary={"quality_status": "promoted", "evidence_summary": {
        "sample_dates": 60, "avg_cross_section_n": 120, "ic_history_rows": 60,
        "rank_ic_mean": -0.04, "rank_ic_ir": 0.4,
    }})
    assert unmeasured_seed_reason(measured) is None
    assert FactorPoolGateway._is_research_consumable_factor(measured) is True
    for field, value in [("sample_dates", 0), ("ic_history_rows", None), ("rank_ic_mean", float("nan"))]:
        bad = copy.deepcopy(measured)
        bad["validation_summary"]["evidence_summary"][field] = value
        assert unmeasured_seed_reason(bad) == "seed_validation_evidence_missing"


def test_health_counts_raw_seed_status_but_does_not_claim_consumability():
    row = seed(generation_trace={"source": "free_data_daily_sync"})
    health = FactorPoolGateway._summarize_pool_health([row])
    assert health["active_status_count"] == 1
    assert health["research_consumable_count"] == 0
    assert health["active_unconsumable_reason_counts"]["seed_validation_evidence_missing"] == 1
