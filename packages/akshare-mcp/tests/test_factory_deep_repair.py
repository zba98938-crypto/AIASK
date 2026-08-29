from __future__ import annotations

import asyncio
import sqlite3
from datetime import date


def test_decay_monitor_measures_ic_history_and_updates_pool():
    from akshare_mcp.services.factor_mining_factory.feedback.decay_monitor import DecayMonitor
    from akshare_mcp.services.factor_mining_factory.pool.active_pool import ActiveFactorPool

    class Db:
        def __init__(self):
            self.calls = []

        async def get_factor_ic_history(self, factor_name: str, period: str, limit: int):
            self.calls.append((factor_name, period, limit))
            return [
                {"factor_name": factor_name, "period": period, "ic_date": "2026-05-18", "rank_ic": 0.04},
                {"factor_name": factor_name, "period": period, "ic_date": "2026-04-18", "rank_ic": 0.10},
            ]

    pool = ActiveFactorPool()
    pool.hydrate(
        [
            {
                "factor_id": "factor-1",
                "name": "momentum",
                "family": "momentum",
                "expression_dsl": "ts_mean(close, 20)",
                "status": "active",
                "fitness": 1.0,
                "admission_ic": 0.10,
                "admission_date": "2026-04-18",
            }
        ]
    )

    report = asyncio.run(DecayMonitor().daily_check(pool, db=Db()))

    assert report["measurements"]
    assert report["measurements"][0]["period"] == "10"
    assert report["updated_records"][0]["factor_id"] == "factor-1"
    assert report["updated_records"][0]["decay_rate"] > 0
    assert report["measurements"][0]["rolling_ic_20d"] == 0.07


def test_factor_pool_gateway_default_filters_retired_and_quarantine_quality():
    from akshare_mcp.services.factor_mining_factory.api import FactorPoolGateway

    class Pool:
        async def get_active_factors(self, **kwargs):
            return [
                {
                    "factor_id": "promoted-1",
                    "name": "promoted",
                    "family": "momentum",
                    "expression_dsl": "rank(close)",
                    "status": "active",
                    "validation_summary": {"quality_status": "promoted"},
                },
                {
                    "factor_id": "retire-1",
                    "name": "retire",
                    "family": "momentum",
                    "expression_dsl": "rank(volume)",
                    "status": "active",
                    "validation_summary": {
                        "quality_status": "promoted",
                        "qc_shelf_decision": {"decision": "retire"},
                    },
                },
                {
                    "factor_id": "stale-advisory-1",
                    "name": "stale_advisory",
                    "family": "momentum",
                    "expression_dsl": "rank(open)",
                    "status": "active",
                    "validation_summary": {
                        "quality_status": "promoted",
                        "qc_shelf_decision": {"decision": "retire"},
                        "qc_labels": {
                            "rank_ic_ir": 0.0,
                            "bootstrap_ci_lower": 0.0,
                            "oos_pass": False,
                            "oos_grade": "unknown",
                            "monotonicity": 0.0,
                            "long_short_return": 0.0,
                            "window_stability": 0.0,
                            "param_sensitivity": 0.0,
                            "dsr": 0.0,
                            "pbo": 0.0,
                        },
                    },
                },
                {
                    "factor_id": "quarantine-1",
                    "name": "quarantine",
                    "family": "momentum",
                    "expression_dsl": "rank(amount)",
                    "status": "active",
                    "validation_summary": {"quality_status": "quarantine"},
                },
            ]

    class Factory:
        def __init__(self):
            self._active_pool = Pool()

        def _ensure_initialized(self):
            return None

        async def _get_db(self):
            raise RuntimeError("force in-memory pool fallback")

        async def _ensure_persistent_pool(self, db):
            return None

    gateway = FactorPoolGateway()
    gateway._factory = Factory()

    factors = asyncio.run(gateway.get_active_factors(limit=10))

    assert [item["factor_id"] for item in factors] == ["promoted-1", "stale-advisory-1"]


def test_active_pool_rejects_too_simple_expression_and_records_admission_ic():
    from akshare_mcp.services.factor_mining_factory.engines.base import FactorCandidate
    from akshare_mcp.services.factor_mining_factory.pool.active_pool import ActiveFactorPool

    pool = ActiveFactorPool()

    rejected = asyncio.run(
        pool.admit(
            FactorCandidate(
                name="simple_factor",
                hypothesis="single field should not enter the active pool",
                inputs=["volatility_20d"],
                expression_dsl="volatility_20d",
                fitness=3.0,
            )
        )
    )

    assert rejected["admitted"] is False
    assert rejected["reason"] == "expression_too_simple"

    invalid = asyncio.run(
        pool.admit(
            FactorCandidate(
                name="invalid_factor",
                hypothesis="unknown field should fail the compile gate",
                inputs=["close"],
                expression_dsl="ts_mean(unknown_field, 20)",
                fitness=3.0,
            )
        )
    )

    assert invalid["admitted"] is False
    assert invalid["reason"] == "compile_invalid"

    no_evidence = asyncio.run(
        pool.admit(
            FactorCandidate(
                name="no_evidence_factor",
                hypothesis="A/B grade without cross-section IC should not enter",
                inputs=["close"],
                expression_dsl="ts_mean(close, 20)",
                fitness=3.0,
                validation_result={
                    "metrics": {"rank_ic_mean": 0.0, "sample_dates": 0},
                    "rating": {"grade": "A"},
                    "persisted_outputs": {"enabled": True, "ic_history_rows": 0},
                },
            )
        )
    )

    assert no_evidence["admitted"] is False
    assert no_evidence["reason"] == "insufficient_ic_evidence"

    no_history = asyncio.run(
        pool.admit(
            FactorCandidate(
                name="no_history_factor",
                hypothesis="A/B grade without persisted IC history should not enter",
                inputs=["close"],
                expression_dsl="ts_mean(close, 20)",
                fitness=3.0,
                validation_result={
                    "metrics": {
                        "rank_ic_mean": 0.05,
                        "rank_ic_ir": 0.3,
                        "positive_ratio": 0.55,
                        "sample_dates": 60,
                    },
                    "coverage": {"avg_cross_section_n": 80},
                    "lookahead_audit": {"risk_level": "low"},
                    "rating": {"grade": "A"},
                },
            )
        )
    )

    assert no_history["admitted"] is False
    assert "ic_history_rows_below_min" in no_history["reasons"]

    admitted = asyncio.run(
        pool.admit(
            FactorCandidate(
                name="structured_factor",
                hypothesis="structured factor should preserve validation IC",
                inputs=["close"],
                expression_dsl="ts_mean(close, 20)",
                fitness=1.0,
                validation_result={
                    "metrics": {
                        "rank_ic_mean": 0.052,
                        "rank_ic_ir": 0.3,
                        "positive_ratio": 0.55,
                        "sample_dates": 60,
                    },
                    "coverage": {"avg_cross_section_n": 80},
                    "lookahead_audit": {"risk_level": "low"},
                    "rating": {"grade": "A"},
                    "persisted_outputs": {"enabled": True, "ic_history_rows": 60},
                },
            )
        )
    )

    record = admitted["record"]
    assert admitted["admitted"] is True
    assert admitted["quarantined"] is True
    assert record["status"] == "quarantine"
    assert record["admission_ic"] == 0.052
    assert record["current_ic"] == 0.052
    assert record["validation_summary"]["quality_status"] == "quarantine"
    assert record["validation_summary"]["persisted_outputs"]["ic_history_rows"] == 60


def test_active_pool_hydrate_filters_persisted_simple_factors():
    from akshare_mcp.services.factor_mining_factory.pool.active_pool import ActiveFactorPool

    pool = ActiveFactorPool()
    pool.hydrate(
        [
            {
                "factor_id": "bad-1",
                "name": "bad_factor",
                "family": "volatility",
                "inputs": ["volatility_20d"],
                "expression_dsl": "volatility_20d",
                "status": "active",
                "fitness": 9.0,
            },
            {
                "factor_id": "good-1",
                "name": "good_factor",
                "family": "momentum",
                "inputs": ["close"],
                "hypothesis": "persisted structured factor",
                "expression_dsl": "ts_mean(close, 20)",
                "status": "active",
                "fitness": 1.0,
                "validation_summary": {"quality_status": "promoted"},
            },
        ]
    )

    factors = asyncio.run(pool.get_active_factors(limit=10))

    assert [item["factor_id"] for item in factors] == ["good-1"]


def test_factory_validation_persists_factor_ic_history(monkeypatch):
    from akshare_mcp.services import factor_validation_pipeline
    from akshare_mcp.services.factor_mining_factory.factory import FactorMiningFactory

    captured = {}

    class Candidate:
        name = "factor_x"
        validation_result = None

        def to_validation_dict(self):
            return {
                "name": self.name,
                "hypothesis": "candidate",
                "family": "custom",
                "inputs": ["close"],
                "expression_dsl": "ts_mean(close, 20)",
            }

    class Context:
        validation_codes = ["000001"]

    async def fake_validate(db, candidate, **kwargs):
        captured.update(kwargs)
        return {
            "success": True,
            "metrics": {
                "rank_ic_mean": 0.04,
                "rank_ic_ir": 0.3,
                "positive_ratio": 0.55,
                "sample_dates": 60,
            },
            "coverage": {"avg_cross_section_n": 80},
            "lookahead_audit": {"risk_level": "low"},
            "cross_section": {"summary": {"rank_ic_mean": 0.04, "sample_dates": 60}},
            "rating": {"grade": "A"},
            "persisted_outputs": {"enabled": True, "ic_history_rows": 60},
        }

    monkeypatch.setattr(
        factor_validation_pipeline,
        "validate_factor_candidate_pipeline",
        fake_validate,
    )

    candidate = Candidate()
    validated = asyncio.run(
        FactorMiningFactory()._validate_batch(object(), [candidate], Context())
    )

    assert validated == [candidate]
    assert captured["persist_outputs"] is True
    assert captured["persist_ic_history"] is True
    assert captured["factor_key"] == "factor_x"
    assert captured["min_cross_section"] == 30


def test_factory_validation_rejects_a_grade_without_ic_evidence(monkeypatch):
    from akshare_mcp.services import factor_validation_pipeline
    from akshare_mcp.services.factor_mining_factory.factory import FactorMiningFactory

    class Candidate:
        name = "factor_x"
        validation_result = None

        def to_validation_dict(self):
            return {
                "name": self.name,
                "hypothesis": "candidate",
                "family": "custom",
                "inputs": ["close"],
                "expression_dsl": "ts_mean(close, 20)",
            }

    class Context:
        validation_codes = ["000001"]

    async def fake_validate(db, candidate, **kwargs):
        return {
            "success": True,
            "metrics": {"rank_ic_mean": 0.0, "sample_dates": 0},
            "cross_section": {"summary": {"rank_ic_mean": 0.0, "sample_dates": 0}},
            "rating": {"grade": "A"},
            "persisted_outputs": {"enabled": True, "ic_history_rows": 0},
        }

    monkeypatch.setattr(
        factor_validation_pipeline,
        "validate_factor_candidate_pipeline",
        fake_validate,
    )

    candidate = Candidate()
    validated = asyncio.run(
        FactorMiningFactory()._validate_batch(object(), [candidate], Context())
    )

    assert validated == []


def test_factory_ic_evaluator_requires_stable_sample(monkeypatch):
    from akshare_mcp.services import factor_validation_pipeline
    from akshare_mcp.services.factor_mining_factory.engines.base import FactorCandidate
    from akshare_mcp.services.factor_mining_factory.factory import FactorMiningFactory

    captured = {}

    class Context:
        validation_codes = ["000001", "000002"]

    async def fake_validate(db, candidate, **kwargs):
        captured.update(kwargs)
        return {
            "success": True,
            "metrics": {"rank_ic_mean": 0.5, "sample_dates": 5},
            "cross_section": {"summary": {"rank_ic_mean": 0.5, "sample_dates": 5}},
        }

    monkeypatch.setattr(
        factor_validation_pipeline,
        "validate_factor_candidate_pipeline",
        fake_validate,
    )

    evaluator = FactorMiningFactory()._build_ic_evaluator(object(), Context())
    value = asyncio.run(
        evaluator(
            FactorCandidate(
                name="factor_x",
                hypothesis="candidate",
                inputs=["close"],
                expression_dsl="ts_mean(close, 20)",
            )
        )
    )

    assert value == 0.0
    assert captured["lookback_bars"] == 180
    assert captured["min_cross_section"] == 30


def test_rl_engine_has_deeper_expression_templates():
    from akshare_mcp.services.factor_mining_factory.engines.rl_engine import (
        _EXPRESSION_TEMPLATES,
    )

    assert any("rank({field1})" in template for template in _EXPRESSION_TEMPLATES)
    assert any("ts_rank({field3}" in template for template in _EXPRESSION_TEMPLATES)


def test_mining_context_builds_healthy_validation_universe():
    from akshare_mcp.services.factor_mining_factory.engines.context import MiningContext

    class Db:
        def __init__(self):
            self.conn = sqlite3.connect(":memory:")
            self.conn.row_factory = sqlite3.Row
            self.conn.executescript(
                """
                CREATE TABLE stocks (
                    stock_code TEXT PRIMARY KEY,
                    stock_name TEXT,
                    industry TEXT,
                    market TEXT,
                    market_cap REAL
                );
                CREATE TABLE kline_1d (
                    time TEXT,
                    code TEXT,
                    open REAL,
                    high REAL,
                    low REAL,
                    close REAL,
                    volume INTEGER,
                    amount REAL
                );
                """
            )
            for i in range(125):
                code = f"000{i:03d}"
                self.conn.execute(
                    "INSERT INTO stocks VALUES (?, ?, ?, ?, ?)",
                    (code, f"S{i}", f"industry-{i % 5}", "SZ", 1000.0 - i),
                )
                for day in range(500):
                    self.conn.execute(
                        "INSERT INTO kline_1d VALUES (?, ?, 1, 2, 1, 1.5, 100, 1000)",
                        (f"2026-05-{(day % 15) + 1:02d}T15:00:00+08:00", code),
                    )
            self.conn.execute(
                "INSERT INTO stocks VALUES ('300001', 'bad', 'bad', 'SZ', 1)"
            )
            for day in range(100):
                self.conn.execute(
                    "INSERT INTO kline_1d VALUES (?, '300001', 1, 2, 1, 1.5, 100, 1000)",
                    (f"2026-05-{(day % 15) + 1:02d}T15:00:00+08:00",),
                )
            self.conn.commit()

    context = asyncio.run(MiningContext.build(db=Db()))

    assert context.validation_universe_health["eligible_count"] == 125
    assert context.validation_universe_health["latest_date_coverage"] == 1.0
    assert context.validation_universe_health["avg_field_coverage"] == 1.0
    assert len(context.validation_codes) == 125
    assert "300001" not in context.validation_codes


def test_mining_context_does_not_fallback_to_unvalidated_stock_list():
    from akshare_mcp.services.factor_mining_factory.engines.context import MiningContext

    class Db:
        async def list_stock_universe(self, limit=80, offset=0):
            return [{"code": f"000{i:03d}"} for i in range(200)]

    context = asyncio.run(MiningContext.build(db=Db()))

    assert context.validation_codes == []
    assert context.validation_universe_health["eligible_count"] == 0
    assert "healthy_validation_universe_empty" in context.data_warnings


def test_factor_pool_status_includes_quality_health_summary():
    from akshare_mcp.services.factor_mining_factory.api import FactorPoolGateway

    rows = [
        {
            "factor_id": "active-1",
            "status": "active",
            "expression_dsl": "rank(ts_mean(close, 20))",
            "generation_engine": "gp_classic",
            "fitness": 88.0,
            "validation_summary": {
                "quality_status": "promoted",
                "quality_score": 88.0,
                "evidence_summary": {
                    "ic_history_rows": 60,
                    "rank_ic_ir": 0.4,
                },
            },
        },
        {
            "factor_id": "active-retire-1",
            "status": "active",
            "expression_dsl": "rank(ts_mean(volume, 20))",
            "generation_engine": "gp_classic",
            "fitness": 77.0,
            "validation_summary": {
                "quality_status": "promoted",
                "qc_shelf_decision": {"decision": "retire"},
                "evidence_summary": {"ic_history_rows": 60, "rank_ic_ir": 0.1},
            },
        },
        {
            "factor_id": "active-stale-advisory-1",
            "status": "active",
            "expression_dsl": "rank(ts_mean(open, 20))",
            "generation_engine": "gp_classic",
            "fitness": 79.0,
            "validation_summary": {
                "quality_status": "promoted",
                "qc_shelf_decision": {"decision": "retire"},
                "qc_labels": {
                    "rank_ic_ir": 0.0,
                    "bootstrap_ci_lower": 0.0,
                    "oos_pass": False,
                    "oos_grade": "unknown",
                    "monotonicity": 0.0,
                    "long_short_return": 0.0,
                    "window_stability": 0.0,
                    "param_sensitivity": 0.0,
                    "dsr": 0.0,
                    "pbo": 0.0,
                },
                "evidence_summary": {"ic_history_rows": 60, "rank_ic_ir": 0.2},
            },
        },
        {
            "factor_id": "q-1",
            "status": "quarantine",
            "generation_engine": "rl_alphagen",
            "fitness": 10.0,
            "validation_summary": {
                "quality_status": "quarantine",
                "evidence_summary": {"ic_history_rows": 12},
            },
        },
    ]

    summary = FactorPoolGateway._summarize_pool_health(rows)

    assert summary["active_promoted_count"] == 3
    assert summary["active_status_count"] == 3
    assert summary["research_consumable_count"] == 2
    assert summary["active_retire_recommended_count"] == 1
    assert summary["active_unconsumable_reason_counts"] == {"qc_retire": 1}
    assert summary["quarantine_count"] == 1
    assert summary["evidence_insufficient_count"] == 1
    assert summary["recent_60d_icir"] == 0.233333


def test_engine_scheduler_downweights_low_quality_rl_and_mcts():
    from akshare_mcp.services.factor_mining_factory.engines.base import FactorCandidate
    from akshare_mcp.services.factor_mining_factory.engines.engine_scheduler import (
        EngineScheduler,
    )

    scheduler = EngineScheduler()
    raw = [
        FactorCandidate(
            name=f"rl-{i}",
            hypothesis="h",
            expression_dsl="ts_mean(close, 20)",
            generation_engine="rl_alphagen",
        )
        for i in range(10)
    ]
    scheduler.record_quality_feedback(raw, [], [])

    class Context:
        active_pool_size = 5
        pool_decay_rate = 0.0

    budgets = scheduler._allocate_budgets(Context(), None, 30)

    assert budgets["rl_alphagen"].candidate_count <= 3


def test_engine_scheduler_runs_cpu_bound_engines_off_event_loop_thread():
    import threading

    from akshare_mcp.services.factor_mining_factory.engines.base import (
        EngineStatus,
        FactorCandidate,
        SearchBudget,
    )
    from akshare_mcp.services.factor_mining_factory.engines.engine_scheduler import (
        EngineScheduler,
    )

    class RecordingCpuEngine:
        engine_id = "gp_classic"
        engine_type = "test_cpu"

        def __init__(self):
            self.thread_ids: list[int] = []

        async def generate(self, context, budget: SearchBudget):
            self.thread_ids.append(threading.get_ident())
            return [
                FactorCandidate(
                    name="threaded-factor",
                    hypothesis="h",
                    expression_dsl="ts_mean(close, 20)",
                    generation_engine=self.engine_id,
                )
            ]

        def get_status(self):
            return EngineStatus(engine_id=self.engine_id, engine_type=self.engine_type)

    class Context:
        active_pool_size = 5
        pool_decay_rate = 0.0

    engine = RecordingCpuEngine()
    scheduler = EngineScheduler()
    scheduler.register_engine(engine)

    async def _run():
        loop_thread_id = threading.get_ident()
        candidates = await scheduler.search(context=Context(), engines=["gp_classic"], candidate_count=3)
        return loop_thread_id, candidates

    loop_thread_id, candidates = asyncio.run(_run())

    assert [candidate.generation_engine for candidate in candidates] == ["gp_classic"]
    assert len(engine.thread_ids) == 1
    assert engine.thread_ids[0] != loop_thread_id


def test_rl_reward_requires_quick_ic_evidence():
    from akshare_mcp.services.factor_mining_factory.engines.rl_engine import (
        RLAlphaGenEngine,
    )

    class NoEvidence:
        pass

    class WithEvidence:
        async def quick_ic_evaluator(self, candidate):
            return 0.08

    engine = RLAlphaGenEngine()
    no_evidence = asyncio.run(
        engine._compute_reward("ts_mean(close, 20)", NoEvidence())
    )
    with_evidence = asyncio.run(
        engine._compute_reward("ts_mean(close, 20)", WithEvidence())
    )

    assert no_evidence <= 0.25
    assert with_evidence > 0.3
