"""Behavior regressions for the 2026-11-04 factory audit."""

from __future__ import annotations

from datetime import date, timedelta
import json
import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock

import numpy as np
import pandas as pd
import pytest

from factor_identity_fixtures import bind_candidate_fixture, bind_record_fixture

from akshare_mcp.services.factor_mining_factory.api import FactorPoolGateway
from akshare_mcp.services.factor_mining_factory.engines.base import FactorCandidate
from akshare_mcp.services.factor_mining_factory.engines.engine_scheduler import EngineScheduler
from akshare_mcp.services.factor_mining_factory.factory import FactorMiningFactory
from akshare_mcp.services.factor_mining_factory.pool.active_pool import ActiveFactorPool
from akshare_mcp.services.factor_mining_factory.pool.numeric_orthogonality import FactorPoolCorrelationEvaluator
from akshare_mcp.services.factor_mining_factory.pool.orthogonalizer import FactorOrthogonalizer
from akshare_mcp.services.factor_mining_factory.pool.storage import ensure_factor_pool_tables, save_factor_to_pool, save_mining_run
from akshare_mcp.services.market_event_sources import persist_normalized_events, bridge_normalized_events_to_strategy_factory
from strategy_factory.application.factor_mining.orchestrator import FactorMiningOrchestrator
from strategy_factory.runtime.provider_adapters import LegacyFactorMiningProvider


EXPRESSION = "ts_mean(close, 5) / ts_mean(close, 20) - 1"


@pytest.fixture
def memory_db():
    conn = sqlite3.connect(":memory:")
    yield SimpleNamespace(conn=conn)
    conn.close()


def pool_record(factor_id="old", **extra):
    return bind_record_fixture({
        "factor_id": factor_id, "name": factor_id, "family": "momentum",
        "expression_dsl": EXPRESSION, "inputs": ["close"], "status": "active",
        "fitness": 100.0, "generation_engine": "rule_seed",
        "validation_summary": {"quality_status": "promoted", "quality_score": 100.0},
        **extra,
    })


def factory_for(db):
    factory = FactorMiningFactory()
    factory._ensure_initialized()
    factory._get_db = AsyncMock(return_value=db)
    return factory


@pytest.mark.asyncio
async def test_two_consumers_refresh_replacements_and_empty_pool(memory_db):
    await ensure_factor_pool_tables(memory_db)
    await save_factor_to_pool(memory_db, pool_record())
    gateways = []
    for _ in range(2):
        gateway = FactorPoolGateway()
        gateway._factory = factory_for(memory_db)
        gateways.append(gateway)
        assert [r["factor_id"] for r in await gateway.get_active_factors()] == ["old"]
    await save_factor_to_pool(memory_db, pool_record(status="retired"))
    await save_factor_to_pool(memory_db, pool_record("new"))
    for gateway in gateways:
        assert [r["factor_id"] for r in await gateway.get_active_factors()] == ["new"]
    await save_factor_to_pool(memory_db, pool_record("new", status="retired"))
    assert await gateways[0].get_active_factors() == []


@pytest.mark.asyncio
async def test_gateway_does_not_resurrect_cache_on_store_failure(memory_db):
    gateway = FactorPoolGateway()
    gateway._factory = factory_for(memory_db)
    gateway._factory._active_pool.hydrate([pool_record()])
    gateway._factory._get_db.side_effect = OSError("store offline")
    with pytest.raises(RuntimeError, match="factor_pool_refresh_failed"):
        await gateway.get_active_factors()


class MarketDb:
    async def get_klines(self, code, limit):
        offset = int(code[-1])
        return [{
            "date": str(date(2026, 1, 1) + timedelta(days=i)),
            "close": 100 + i * .2 + np.sin(i / 5) * 3 + offset,
            "open": 100 + i * .2 + offset, "high": 105 + i * .2 + offset,
            "low": 95 + i * .2 + offset, "volume": 1000 + np.sin(i * 1.7 + offset) * 200,
        } for i in range(100)]


def validated_candidate(name="new", engine="rule_seed"):
    return bind_candidate_fixture(FactorCandidate(
        name=name, generation_engine=engine, hypothesis="numeric admission regression",
        inputs=["close"], expression_dsl="ts_mean(close, 5) * (1 / ts_mean(close, 20)) - 1",
        validation_result={
            "success": True,
            "metrics": {"rank_ic_mean": .05, "rank_ic_ir": .35, "positive_ratio": .56, "sample_dates": 60},
            "coverage": {"avg_cross_section_n": 100}, "lookahead_audit": {"risk_level": "low"},
            "rating": {"grade": "A"}, "persisted_outputs": {"enabled": True, "ic_history_rows": 60},
        },
    ))


@pytest.mark.asyncio
async def test_equivalent_expressions_use_measured_values_in_admission(monkeypatch):
    factory = factory_for(MarketDb())
    factory._active_pool.hydrate([pool_record()])
    context = SimpleNamespace(validation_codes=["000001", "000002", "000003"])
    monkeypatch.setattr(
        "akshare_mcp.services.factor_mining_factory.engines.context.MiningContext.build",
        AsyncMock(return_value=context),
    )
    factory._load_persistent_pattern_memory = AsyncMock(return_value={})
    provider = LegacyFactorMiningProvider(factory)
    await provider.build_mining_context(MarketDb())
    candidate = validated_candidate()
    # Exercise the same active-pool callback installed by production context.
    measured = await factory._active_pool.correlation_evaluator(candidate, [pool_record()])
    assert measured["measurable"] is True, measured
    assert measured["max_correlation"] == pytest.approx(1.0)
    assert measured["common_observations"]["old"] > 200
    assert measured["observation_key"] == ["trade_date", "stock_code"]
    rejected = await factory._active_pool.admit(candidate)
    assert rejected["reason"] == "redundant"
    assert rejected["max_correlation"] == pytest.approx(1.0)
    assert await provider.admit_batch([candidate]) == []


@pytest.mark.asyncio
async def test_missing_numeric_measurement_cannot_pass_as_zero_correlation():
    pool = ActiveFactorPool()
    pool.hydrate([pool_record()])
    assert (await pool.admit(validated_candidate()))["reason"] == "orthogonality_unavailable"
    db = SimpleNamespace(get_klines=AsyncMock(return_value=[]))
    pool.correlation_evaluator = FactorPoolCorrelationEvaluator(db, ["000001"]).evaluate
    result = await pool.admit(validated_candidate())
    assert result["reason"] == "orthogonality_unmeasurable"
    assert result["orthogonality"]["max_correlation"] is None


@pytest.mark.asyncio
async def test_numerically_independent_factor_is_admitted_with_measurement_provenance():
    pool = ActiveFactorPool()
    pool.hydrate([pool_record()])
    pool.correlation_evaluator = FactorPoolCorrelationEvaluator(MarketDb(), ["000001", "000002", "000003"]).evaluate
    candidate = validated_candidate("volume_alpha")
    candidate.inputs = ["volume"]
    candidate.expression_dsl = "zscore(volume, 5)"
    bind_candidate_fixture(candidate)
    result = await pool.admit(candidate)
    assert result["admitted"] is True, result
    measured = result["record"]["validation_summary"]["orthogonality"]
    assert measured["measurable"] is True
    assert measured["max_correlation"] < pool.ORTHOGONALITY_THRESHOLD
    assert measured["independent_ratio"] is not None
    assert measured["residual_ic"] is not None


@pytest.mark.asyncio
async def test_nonoverlapping_dates_and_constant_factors_are_unmeasurable():
    orthogonalizer = FactorOrthogonalizer()
    x = pd.Series(range(60), index=pd.date_range("2025-01-01", periods=60))
    y = pd.Series(range(60), index=pd.date_range("2026-01-01", periods=60))
    result = await orthogonalizer.check_orthogonality(x, {"old": y})
    assert not result.measurable
    assert result.max_correlation is None
    constant = await orthogonalizer.check_orthogonality(x, {"old": pd.Series(1.0, index=x.index)})
    assert constant.reason == "constant_factor_values"


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["reappraisal", "maintenance"])
async def test_quarantine_promotions_cannot_bypass_numeric_orthogonality(memory_db, monkeypatch, route):
    from akshare_mcp.services.factor_mining_factory import reappraise

    await ensure_factor_pool_tables(memory_db)
    memory_db.get_klines = MarketDb().get_klines
    monkeypatch.setattr(
        "akshare_mcp.services.factor_mining_factory.engines.context.MiningContext.build",
        AsyncMock(return_value=SimpleNamespace(validation_codes=["000001", "000002", "000003"])),
    )
    monkeypatch.setattr(reappraise, "_load_factor_ic_rows", AsyncMock(return_value=[
        {"rank_ic": .05 + i % 5 * .001, "stock_count": 120} for i in range(60)
    ]))
    await save_factor_to_pool(memory_db, pool_record())
    await save_factor_to_pool(memory_db, pool_record(
        "duplicate", status="quarantine", expression_dsl=validated_candidate().expression_dsl,
        validation_summary={"quality_status": "quarantine"},
    ))
    await save_factor_to_pool(memory_db, pool_record(
        "independent", status="quarantine", expression_dsl="zscore(volume, 5)", inputs=["volume"],
        validation_summary={"quality_status": "quarantine"},
    ))
    if route == "reappraisal":
        report = await reappraise.reappraise_quarantine_factors(memory_db)
        assert report["promoted"] == 1, report
        duplicate = next(item for item in report["items"] if item["factor_id"] == "duplicate")
        assert duplicate["reasons"] == ["orthogonality_redundant"]
    else:
        factory = factory_for(memory_db)
        factory._build_quarantine_evidence_result = AsyncMock(return_value=validated_candidate().validation_result)
        report = await factory._promote_quarantine_factors(memory_db)
        assert report["promoted"] == ["independent"], report
        assert report["rejected_reasons"] == {"orthogonality_redundant": 1}
    stored = {
        row[0]: (row[1], json.loads(row[2]))
        for row in memory_db.conn.execute("SELECT factor_id, status, validation_summary FROM factor_pool_active")
    }
    assert stored["duplicate"][0] == "quarantine"
    assert stored["independent"][0] == "active"
    assert stored["independent"][1]["orthogonality"]["basis"] == "factor_values_pooled_spearman"


@pytest.mark.asyncio
@pytest.mark.parametrize("dry_run", [False, True])
async def test_reappraisal_checks_newly_promoted_peers_within_same_batch(memory_db, monkeypatch, dry_run):
    from akshare_mcp.services.factor_mining_factory import reappraise

    await ensure_factor_pool_tables(memory_db)
    memory_db.get_klines = MarketDb().get_klines
    monkeypatch.setattr(reappraise, "_load_factor_ic_rows", AsyncMock(return_value=[
        {"rank_ic": .05 + i % 5 * .001, "stock_count": 120} for i in range(60)
    ]))
    for factor_id in ("q1", "q2"):
        await save_factor_to_pool(memory_db, pool_record(factor_id, status="quarantine"))
    report = await reappraise.reappraise_quarantine_factors(
        memory_db, dry_run=dry_run,
        correlation_evaluator=FactorPoolCorrelationEvaluator(memory_db, ["000001"]).evaluate,
    )
    assert report["promoted"] == report["kept_quarantine"] == 1, report
    assert report["items"][1]["orthogonality"]["max_correlation"] == pytest.approx(1.0)
    active_count = memory_db.conn.execute("SELECT COUNT(*) FROM factor_pool_active WHERE status='active'").fetchone()[0]
    assert active_count == (0 if dry_run else 1)


@pytest.mark.asyncio
async def test_canonical_cycle_persists_correct_funnel_and_pattern_details(memory_db):
    await ensure_factor_pool_tables(memory_db)
    candidates = [validated_candidate(f"f{i}") for i in range(4)]
    for i, candidate in enumerate(candidates):
        candidate.blueprint_id = f"blueprint{i}"
        candidate.quick_evidence = {"passed": i < 3, "fail_reasons": [] if i < 3 else ["weak_quick_ic"]}
        if i >= 2:
            candidate.validation_result = {"success": False, "error": "invalid"} if i == 2 else None
        candidate.generation_trace = {"strict_validation_attempted": i < 3}
    admitted = [
        {"admitted": True, "record": pool_record("f0", generation_trace={"blueprint_id": "blueprint0"})},
        {"admitted": True, "record": pool_record("f1", status="quarantine", generation_trace={"blueprint_id": "blueprint1"})},
    ]
    provider = SimpleNamespace(
        get_db=AsyncMock(return_value=memory_db), ensure_persistent_pool=AsyncMock(),
        build_mining_context=AsyncMock(return_value=SimpleNamespace(validation_codes=list(range(120)))),
        install_quick_evidence_evaluators=lambda *_: None,
        search_candidates=AsyncMock(return_value=candidates), evolve_candidates=AsyncMock(return_value=candidates),
        quick_filter_candidates=AsyncMock(return_value=candidates[:3]),
        validate_batch=AsyncMock(return_value=candidates[:2]), admit_batch=AsyncMock(return_value=admitted),
        persist_admitted_factors=AsyncMock(), record_feedback=AsyncMock(),
        reappraise_quarantine_factors=AsyncMock(return_value={}),
        get_active_pool_size=lambda: 1, get_last_engines_used=lambda: ["rule_seed"],
        persist_mining_run=save_mining_run,
    )
    report = await FactorMiningOrchestrator(provider).run_cycle()
    summary = report["quality_summary"]
    assert summary["quality_funnel"]["quick_passed"] == 3
    assert summary["quality_funnel"]["validated"] == 2
    assert summary["active_promoted_count"] == summary["quarantine_count"] == 1
    assert summary["rejection_counts"] == {"evolution": 0, "quick_evidence": 1, "validation": 1, "admission": 0}
    assert len(summary["strict_candidate_results"]) == 3
    assert len(summary["quick_candidate_results"]) == 4
    memory = await FactorMiningFactory()._load_persistent_pattern_memory(memory_db)
    assert {row["pattern"] for row in memory["successful_pattern_memory"]} == {"blueprint0", "blueprint1"}
    assert {row["pattern"] for row in memory["failed_pattern_memory"]} == {"blueprint2", "blueprint3"}


@pytest.mark.asyncio
async def test_production_feedback_changes_budget_and_restores_after_restart(memory_db, monkeypatch):
    await ensure_factor_pool_tables(memory_db)
    factory = factory_for(memory_db)
    raw = [validated_candidate(f"m{i}", "mcts_guided") for i in range(10)] + [validated_candidate(f"r{i}") for i in range(10)]
    validated = raw[10:12]
    admitted = [{"admitted": True, "record": pool_record("r0")}]
    context = SimpleNamespace(active_pool_size=0, pool_decay_rate=0, validation_codes=[])
    before = factory._engine_scheduler._allocate_budgets(context, ["mcts_guided", "rule_seed"], 100)
    await LegacyFactorMiningProvider(factory).record_feedback("feedback-run", raw, raw, validated, admitted)
    after = factory._engine_scheduler._allocate_budgets(context, ["mcts_guided", "rule_seed"], 100)
    assert after["mcts_guided"].candidate_count < before["mcts_guided"].candidate_count
    summary = FactorMiningOrchestrator(None)._build_quality_summary(raw, raw, validated, admitted, context, quick_passed=raw)
    await save_mining_run(memory_db, {"run_id": "feedback-run", "success": True, "quality_summary": summary})
    restarted = factory_for(memory_db)
    monkeypatch.setattr("akshare_mcp.services.factor_mining_factory.engines.context.MiningContext.build", AsyncMock(return_value=context))
    await restarted._build_mining_context(db=memory_db)
    restored = restarted._engine_scheduler._allocate_budgets(context, ["mcts_guided", "rule_seed"], 100)
    assert {k: v.candidate_count for k, v in restored.items()} == {k: v.candidate_count for k, v in after.items()}
    await restarted._build_mining_context(db=memory_db)
    assert restarted._engine_scheduler._feedback_totals("mcts_guided")["raw"] == 10


class EventDb:
    def __init__(self):
        self.events, self.signals = [], []

    async def list_market_events_normalized(self, status=None, event_signature=None, limit=50):
        return [r for r in self.events if (status is None or r["status"] == status) and (event_signature is None or r["metadata"]["event_signature"] == event_signature)][:limit]

    async def upsert_market_event_normalized(self, row):
        self.events.append(row)
        return row

    async def save_factory_event_cluster(self, row):
        return row

    async def save_factory_event_signal(self, row):
        self.signals.append(row)
        return row


@pytest.mark.asyncio
@pytest.mark.parametrize("title, expected", [
    ("三季度业绩预减公告：预计净利润同比下降80%", "down"),
    ("三季度业绩预告：净利润同比下降80%，营业收入增长10%", "down"),
    ("三季度业绩预亏公告", "down"),
    ("三季度业绩预增公告", "up"),
    ("三季度业绩预告：扭亏为盈", "up"),
    ("三季度业绩预告：亏损收窄", "up"),
    ("三季度业绩快报", "neutral"),
    ("关于终止重大资产重组的公告", "down"),
    ("关于终止重大合同的公告", "down"),
])
async def test_official_events_keep_direction_through_persistence_and_bridge(title, expected):
    db = EventDb()
    await persist_normalized_events(db, "600519", "notice", [{
        "title": title, "date": date.today().isoformat(), "source_tier": "tier_a", "provider": "cninfo", "code": "600519",
    }])
    assert db.events[0]["direction"] == expected
    assert db.events[0]["status"] == "verified"
    await bridge_normalized_events_to_strategy_factory(db)
    if db.signals:
        assert db.signals[0]["direction"] == {"up": "positive", "down": "negative", "neutral": "neutral"}[expected]
    elif expected != "neutral":
        pytest.fail("Directional verified announcement was not bridged")
