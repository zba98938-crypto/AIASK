from __future__ import annotations

import asyncio
import sqlite3
from datetime import date


def test_factory_persists_decay_measurements_and_pool_updates():
    from akshare_mcp.services.factor_mining_factory.factory import FactorMiningFactory
    from akshare_mcp.services.factor_mining_factory.pool.storage import (
        ensure_factor_pool_tables,
        load_active_pool_from_db,
    )

    class Db:
        def __init__(self):
            self.conn = sqlite3.connect(":memory:")
            self.conn.row_factory = sqlite3.Row

    async def run():
        db = Db()
        await ensure_factor_pool_tables(db)
        factory = FactorMiningFactory()
        measurement = {
            "factor_id": "factor-1",
            "measured_at": "2026-05-19T00:00:00+00:00",
            "rolling_ic_20d": 0.03,
            "rolling_ic_60d": 0.05,
            "admission_ic": 0.08,
            "current_ic": 0.02,
            "decay_rate": 0.75,
            "estimated_half_life_days": 12.0,
        }
        record = {
            "factor_id": "factor-1",
            "name": "momentum",
            "family": "momentum",
            "expression_dsl": "ts_mean(close, 20)",
            "status": "active",
            "fitness": 1.0,
            "current_ic": 0.02,
            "decay_rate": 0.75,
            "last_evaluated_at": "2026-05-19T00:00:00+00:00",
        }
        await factory._persist_decay_report(db, {"measurements": [measurement]})
        await factory._persist_decay_updates(db, {"updated_records": [record]})
        history = db.conn.execute(
            "SELECT * FROM factor_pool_decay_history WHERE factor_id = ?",
            ("factor-1",),
        ).fetchone()
        pool = await load_active_pool_from_db(db)
        return dict(history), pool

    history, pool = asyncio.run(run())

    assert history["decay_rate"] == 0.75
    assert history["alert_triggered"] == 1
    assert pool[0]["factor_id"] == "factor-1"
    assert pool[0]["decay_rate"] == 0.75
    assert pool[0]["current_ic"] == 0.02


def test_forward_verifier_uses_wilson_lcb_and_holding_period_sharpe():
    from akshare_mcp.services.incubation_factory.forward_verifier import ForwardVerifier

    verifier = ForwardVerifier()

    lcb = verifier._compute_skill_lcb([1.0] * 10)
    assert 0.2 < lcb < 0.5

    returns = [0.01, 0.02, -0.005, 0.015, 0.0]
    daily_scaled = verifier._compute_forward_sharpe(returns, horizon_days=1)
    held_scaled = verifier._compute_forward_sharpe(returns, horizon_days=5)
    assert held_scaled < daily_scaled


def test_metrics_recorder_writes_schema_fields_and_nav_rows():
    from akshare_mcp.services.incubation_factory.metrics_recorder import MetricsRecorder

    class Db:
        def __init__(self):
            self.saved = None

        async def get_strategy_incubation_account(self, strategy_id):
            return {"strategy_id": strategy_id, "account_id": "acct-1"}

        async def get_paper_account(self, account_id):
            return {"id": account_id, "initial_capital": 100000.0}

        async def get_paper_nav_rows(self, account_id, limit=60):
            return [
                {"account_id": account_id, "nav_date": "2026-05-19", "total_value": 99000.0, "cash": 5000.0, "market_value": 94000.0, "daily_return": None},
                {"account_id": account_id, "nav_date": "2026-05-18", "total_value": 100000.0, "cash": 6000.0, "market_value": 94000.0, "daily_return": 0.01},
                {"account_id": account_id, "nav_date": "2026-05-17", "total_value": 105000.0, "cash": 7000.0, "market_value": 98000.0, "daily_return": 0.05},
            ]

        async def save_strategy_incubation_metric(self, strategy_id, metric_date, metric):
            self.saved = dict(metric)
            return self.saved

    db = Db()
    metric = asyncio.run(
        MetricsRecorder().record(
            db,
            {"id": "strategy-1", "status": "incubating"},
            {
                "primary_hit_rate": 0.6,
                "recent_primary_hit_rate": 0.55,
                "primary_skill_lcb": 0.03,
                "recent_primary_skill_lcb": 0.02,
                "stability_gap": 0.01,
                "coverage_ratio": 0.8,
                "forward_ic": 0.04,
                "forward_sharpe": 0.7,
                "primary_effective_n": 20,
                "total_signals": 22,
            },
            metric_date=date(2026, 5, 19),
        )
    )

    assert metric["effective_n_5d"] == 20
    assert metric["recent_hit_rate_5d"] == 0.55
    assert metric["stability_gap_5d"] == 0.01
    assert metric["forward_ic_5d"] == 0.04
    assert metric["total_signals"] == 22
    assert metric["daily_return"] == -0.01
    assert metric["max_drawdown"] > 0


def test_feedback_writer_runtime_control_includes_required_fields():
    from akshare_mcp.services.incubation_factory.feedback_writer import FeedbackWriter

    class Db:
        def __init__(self):
            self.controls = []

        async def save_strategy_domain_event(self, payload):
            return payload

        async def save_strategy_closure_snapshot(self, payload):
            return payload

        async def list_strategies(self, status, limit=500):
            return [{"id": "strategy-1", "strategy_type": "momentum"}]

        async def get_strategy_incubation_account(self, strategy_id):
            return {"strategy_id": strategy_id, "account_id": "acct-1"}

        async def save_strategy_runtime_control(self, payload):
            self.controls.append(dict(payload))
            return payload

    db = Db()
    asyncio.run(
        FeedbackWriter().write(
            db,
            {
                "feedback_actions": {"families_to_freeze": ["momentum"]},
                "hit_rate_dashboard": {"by_family": {}},
            },
        )
    )

    control = db.controls[0]
    assert control["account_id"] == "acct-1"
    assert control["status"] == "active"
    assert control["trigger_event_type"] == "incubation_factory.feedback_control"
    assert control["action_summary"]["action"] == "freeze"


def test_evolutionary_optimizer_blends_ic_feedback():
    from akshare_mcp.services.factor_mining_factory.engines.base import FactorCandidate
    from akshare_mcp.services.factor_mining_factory.evolution.optimizer import EvolutionaryOptimizer

    candidate = FactorCandidate(
        name="factor-a",
        hypothesis="structured factor",
        inputs=["close"],
        expression_dsl="ts_mean(close, 20)",
        fitness=1.0,
    )
    optimizer = EvolutionaryOptimizer()

    asyncio.run(
        optimizer._evaluate_fitness(
            [candidate],
            ic_evaluator=lambda _: 0.1,
        )
    )

    assert round(candidate.fitness, 4) == 1.625
    assert candidate.generation_trace["evolution_ic_value"] == 0.1
    assert candidate.generation_trace["evolution_fitness_blend"] == "0.7_structural_0.3_abs_ic_x25"


def test_factor_llm_payload_truncates_overlong_text_fields():
    from akshare_mcp.services.factor_llm_provider import (
        validate_factor_generation_payload,
    )

    data = validate_factor_generation_payload(
        {
            "candidates": [
                {
                    "name": "long_text_factor",
                    "hypothesis": "h" * 800,
                    "family": "custom",
                    "inputs": ["close"],
                    "expression_dsl": "ts_mean(close, 20)",
                    "novelty_rationale": "n" * 800,
                }
            ]
        }
    )

    candidate = data["candidates"][0]
    assert len(candidate["hypothesis"]) == 400
    assert len(candidate["novelty_rationale"]) == 400


def test_accelerator_triggers_incubation_pipeline(monkeypatch):
    from akshare_mcp.services.incubation_factory.accelerator import IncubationAccelerator
    from akshare_mcp.services import incubation_pipeline

    class Db:
        async def save_strategy_domain_event(self, payload):
            return payload

    class Pipeline:
        def __init__(self):
            self.calls = []

        async def run_strategy(self, db, strategy, **kwargs):
            self.calls.append((strategy, kwargs))
            return {"ok": True}

    pipeline = Pipeline()
    monkeypatch.setattr(
        incubation_pipeline,
        "get_strategy_incubation_pipeline_service",
        lambda: pipeline,
    )

    asyncio.run(
        IncubationAccelerator()._trigger_acceleration(
            Db(),
            {"id": "strategy-1", "name": "S"},
            {"promote_streak": 10},
        )
    )

    assert pipeline.calls[0][1]["source"] == "incubation_factory_accelerator"
    assert pipeline.calls[0][1]["auto_apply_review"] is True


def test_alpha_blueprints_compile_and_include_economic_metadata():
    from akshare_mcp.services.factor_candidate_compiler import compile_factor_candidate
    from akshare_mcp.services.factor_mining_factory.blueprints import AlphaBlueprintLibrary

    blueprints = AlphaBlueprintLibrary().build_context_blueprints()

    assert blueprints
    for blueprint in blueprints:
        assert blueprint["blueprint_id"]
        assert blueprint["economic_hypothesis"]
        assert blueprint["factor_family"]
        assert blueprint["expected_horizon"] > 0
        assert isinstance(blueprint["risk_exposure_hint"], dict)
        compiled = compile_factor_candidate(
            {
                "name": blueprint["blueprint_id"],
                "hypothesis": blueprint["economic_hypothesis"],
                "family": blueprint["factor_family"],
                "inputs": blueprint["inputs"],
                "expression_dsl": blueprint["expression_dsl"],
            }
        )
        assert compiled["valid"], blueprint["blueprint_id"]
        assert compiled["function_calls"], blueprint["blueprint_id"]


def test_rule_engine_starts_from_alpha_blueprints():
    from akshare_mcp.services.factor_mining_factory.engines.context import MiningContext
    from akshare_mcp.services.factor_mining_factory.engines.rule_engine import RuleSeedEngine
    from akshare_mcp.services.factor_mining_factory.engines.base import SearchBudget

    context = MiningContext(
        alpha_blueprints=[
            {
                "blueprint_id": "test_blueprint",
                "factor_family": "momentum",
                "expression_dsl": "zscore(momentum_20d, 20) - zscore(volatility_20d, 20)",
                "inputs": ["momentum_20d", "volatility_20d"],
                "economic_hypothesis": "Risk-adjusted momentum should persist.",
                "expected_horizon": 10,
                "risk_exposure_hint": {"style": ["momentum"]},
            }
        ]
    )

    candidates = asyncio.run(
        RuleSeedEngine().generate(context, SearchBudget(candidate_count=3))
    )

    assert candidates[0].blueprint_id == "test_blueprint"
    assert candidates[0].economic_hypothesis
    assert candidates[0].risk_exposure_hint["style"] == ["momentum"]


def test_quick_evidence_evaluator_uses_quick_stage_and_thresholds(monkeypatch):
    from akshare_mcp.services import factor_validation_pipeline
    from akshare_mcp.services.factor_mining_factory.engines.base import FactorCandidate
    from akshare_mcp.services.factor_mining_factory.quick_evidence import (
        QuickEvidenceEvaluator,
    )

    captured = {}

    async def fake_validate(db, candidate, **kwargs):
        captured.update(kwargs)
        return {
            "success": True,
            "metrics": {
                "rank_ic_mean": 0.03,
                "rank_ic_ir": 0.3,
                "positive_ratio": 0.54,
                "sample_dates": 45,
            },
            "coverage": {"avg_cross_section_n": 100, "eligible_code_count": 120},
            "cross_section": {"summary": {"rank_ic_mean": 0.03, "sample_dates": 45}},
            "lookahead_audit": {"risk_level": "low"},
            "persisted_outputs": {"enabled": False, "ic_history_rows": 0},
        }

    monkeypatch.setattr(
        factor_validation_pipeline,
        "validate_factor_candidate_pipeline",
        fake_validate,
    )

    evaluator = QuickEvidenceEvaluator(object(), codes=[f"000{i:03d}" for i in range(130)])
    candidate = FactorCandidate(
        name="quick_factor",
        hypothesis="quick evidence candidate",
        inputs=["close"],
        expression_dsl="ts_mean(close, 20)",
    )
    evidence = asyncio.run(evaluator.evaluate(candidate))

    assert evidence["passed"] is True
    assert candidate.quick_evidence["stage"] == "quick"
    assert captured["stage"] == "quick"
    assert captured["min_cross_section"] == 80
    assert captured["persist_ic_history"] is False


def test_factory_quick_evidence_budget_is_isolated_by_engine(monkeypatch):
    from akshare_mcp.services import factor_validation_pipeline
    from akshare_mcp.services.factor_mining_factory.engines.base import FactorCandidate
    from akshare_mcp.services.factor_mining_factory.factory import FactorMiningFactory

    calls: list[str] = []

    async def fake_validate(db, candidate, **kwargs):
        calls.append(str(candidate.get("name") or ""))
        return {
            "success": True,
            "metrics": {
                "rank_ic_mean": 0.03,
                "rank_ic_ir": 0.3,
                "positive_ratio": 0.54,
                "sample_dates": 45,
            },
            "coverage": {"avg_cross_section_n": 100, "eligible_code_count": 130},
            "cross_section": {"summary": {"rank_ic_mean": 0.03, "sample_dates": 45}},
            "lookahead_audit": {"risk_level": "low"},
            "persisted_outputs": {"enabled": False, "ic_history_rows": 0},
        }

    monkeypatch.setattr(
        factor_validation_pipeline,
        "validate_factor_candidate_pipeline",
        fake_validate,
    )

    class Context:
        validation_codes = [f"000{i:03d}" for i in range(130)]

    async def run():
        context = Context()
        factory = FactorMiningFactory()
        factory._install_quick_evidence_evaluators(
            object(),
            context,
            max_evaluations=1,
        )
        gp_first = FactorCandidate(
            name="gp_first",
            expression_dsl="ts_mean(close, 20)",
            generation_engine="gp_classic",
        )
        gp_second = FactorCandidate(
            name="gp_second",
            expression_dsl="ts_mean(open, 20)",
            generation_engine="gp_classic",
        )
        mcts_first = FactorCandidate(
            name="mcts_first",
            expression_dsl="ts_mean(volume, 20)",
            generation_engine="mcts_guided",
        )

        gp_first_evidence = await context.quick_evidence_evaluator(gp_first)
        gp_second_evidence = await context.quick_evidence_evaluator(gp_second)
        mcts_first_evidence = await context.quick_evidence_evaluator(mcts_first)
        return context, gp_first_evidence, gp_second_evidence, mcts_first_evidence

    context, gp_first_evidence, gp_second_evidence, mcts_first_evidence = asyncio.run(run())

    assert gp_first_evidence["passed"] is True
    assert gp_second_evidence["passed"] is False
    assert gp_second_evidence["fail_reasons"] == ["quick_evaluation_budget_exhausted"]
    assert mcts_first_evidence["passed"] is True
    assert set(context.quick_evidence_evaluators) == {"gp_classic", "mcts_guided"}
    assert context.quick_evidence_evaluators["gp_classic"].evaluation_count == 1
    assert context.quick_evidence_evaluators["mcts_guided"].evaluation_count == 1
    assert calls == ["gp_first", "mcts_first"]


def test_engine_scheduler_normalizes_budget_for_explicit_single_engine():
    from akshare_mcp.services.factor_mining_factory.engines.engine_scheduler import EngineScheduler

    class Context:
        active_pool_size = 100
        pool_decay_rate = 0.0

    budgets = EngineScheduler()._allocate_budgets(
        Context(),
        ["rule_seed"],
        12,
    )

    assert list(budgets) == ["rule_seed"]
    assert budgets["rule_seed"].candidate_count == 12


def test_engine_scheduler_normalizes_budget_for_explicit_engine_subset():
    from akshare_mcp.services.factor_mining_factory.engines.engine_scheduler import EngineScheduler

    class Context:
        active_pool_size = 100
        pool_decay_rate = 0.0

    budgets = EngineScheduler()._allocate_budgets(
        Context(),
        ["llm_primary", "rule_seed"],
        20,
    )

    assert set(budgets) == {"llm_primary", "rule_seed"}
    assert budgets["llm_primary"].candidate_count + budgets["rule_seed"].candidate_count <= 20
    assert budgets["rule_seed"].candidate_count > 3


def test_active_pool_redundancy_uses_replacement_decision_metadata():
    from akshare_mcp.services.factor_mining_factory.engines.base import FactorCandidate
    from akshare_mcp.services.factor_mining_factory.pool.active_pool import ActiveFactorPool

    validation_result = {
        "metrics": {
            "rank_ic_mean": 0.05,
            "rank_ic_ir": 0.35,
            "positive_ratio": 0.56,
            "sample_dates": 60,
        },
        "coverage": {"avg_cross_section_n": 100},
        "lookahead_audit": {"risk_level": "low"},
        "rating": {"grade": "A"},
        "persisted_outputs": {"enabled": True, "ic_history_rows": 60},
    }

    pool = ActiveFactorPool()
    pool.hydrate(
        [
            {
                "factor_id": "existing-1",
                "name": "existing",
                "family": "momentum",
                "inputs": ["close"],
                "hypothesis": "existing factor",
                "expression_dsl": "ts_mean(close, 20)",
                "status": "active",
                "fitness": 95.0,
                "validation_summary": {
                    "quality_status": "promoted",
                    "quality_score": 95.0,
                },
            }
        ]
    )
    rejected = asyncio.run(
        pool.admit(
            FactorCandidate(
                name="redundant_factor",
                hypothesis="same expression",
                inputs=["close"],
                expression_dsl="ts_mean(close, 20)",
                validation_result=validation_result,
            )
        )
    )

    assert rejected["admitted"] is False
    assert rejected["reason"] == "redundant"
    assert rejected["replacement_decision"]["action"] == "reject"
    assert rejected["incremental_quality_score"] < 10.0


def test_active_pool_replacement_returns_retired_record_for_persistence():
    from akshare_mcp.services.factor_mining_factory.engines.base import FactorCandidate
    from akshare_mcp.services.factor_mining_factory.factory import FactorMiningFactory
    from akshare_mcp.services.factor_mining_factory.pool.active_pool import ActiveFactorPool

    class Db:
        def __init__(self):
            self.saved: list[dict] = []

        async def acquire(self):
            raise AssertionError("raw connection path is not used in this test")

    validation_result = {
        "metrics": {
            "rank_ic_mean": 0.09,
            "rank_ic_ir": 0.45,
            "positive_ratio": 0.62,
            "sample_dates": 80,
        },
        "coverage": {"avg_cross_section_n": 120},
        "lookahead_audit": {"risk_level": "low"},
        "rating": {"grade": "A"},
        "persisted_outputs": {"enabled": True, "ic_history_rows": 80},
    }

    pool = ActiveFactorPool()
    pool.hydrate(
        [
            {
                "factor_id": "existing-1",
                "name": "existing",
                "family": "momentum",
                "inputs": ["close"],
                "hypothesis": "existing factor",
                "expression_dsl": "ts_mean(close, 20)",
                "status": "active",
                "fitness": 70.0,
                "validation_summary": {
                    "quality_status": "promoted",
                    "quality_score": 70.0,
                },
            }
        ]
    )

    admitted = asyncio.run(
        pool.admit(
            FactorCandidate(
                name="better_factor",
                hypothesis="same expression but materially better",
                inputs=["close"],
                expression_dsl="ts_mean(close, 20)",
                validation_result=validation_result,
            )
        )
    )

    retired = admitted["retired_records"][0]
    assert admitted["admitted"] is True
    assert retired["factor_id"] == "existing-1"
    assert retired["status"] == "retired"
    assert retired["retired_reason"] == "quality_replacement"
    assert retired["validation_summary"]["quality_status"] == "retired"

    async def fake_save_factor_to_pool(db, record):
        db.saved.append(dict(record))
        return {"saved": True, "factor_id": record.get("factor_id")}

    import akshare_mcp.services.factor_mining_factory.pool.storage as storage

    original = storage.save_factor_to_pool
    storage.save_factor_to_pool = fake_save_factor_to_pool
    try:
        db = Db()
        asyncio.run(FactorMiningFactory()._persist_admitted_factors(db, [admitted]))
    finally:
        storage.save_factor_to_pool = original

    saved_by_id = {item["factor_id"]: item for item in db.saved}
    assert saved_by_id["existing-1"]["status"] == "retired"
    assert saved_by_id[admitted["factor_id"]]["status"] == "active"


def test_factory_quality_summary_includes_quick_funnel_and_blueprints():
    from akshare_mcp.services.factor_mining_factory.engines.base import FactorCandidate
    from akshare_mcp.services.factor_mining_factory.factory import FactorMiningFactory

    raw = [
        FactorCandidate(
            name="raw",
            expression_dsl="ts_mean(close, 20)",
            generation_engine="rule_seed",
            blueprint_id="bp1",
        )
    ]
    evolved = [
        FactorCandidate(
            name="evolved",
            expression_dsl="ts_mean(close, 20)",
            generation_engine="rule_seed",
            blueprint_id="bp1",
            quick_evidence={"passed": False, "fail_reasons": ["rank_ic_ir_below_min"]},
        )
    ]

    class Context:
        validation_universe_health = {"eligible_count": 300}

    summary = FactorMiningFactory()._build_quality_summary(
        raw,
        evolved,
        [],
        [],
        Context(),
    )

    assert summary["quality_funnel"]["generated"] == 1
    assert summary["quality_funnel"]["quick_evaluated"] == 1
    assert summary["reject_reasons"]["rank_ic_ir_below_min"] == 1
    assert summary["by_blueprint"]["bp1"]["raw"] == 1


def test_llm_engine_uses_blueprint_fallback_prompt(monkeypatch):
    from akshare_mcp import storage
    from akshare_mcp.services import factor_llm_provider, factor_prompt_builder
    from akshare_mcp.services.factor_mining_factory.engines.base import SearchBudget
    from akshare_mcp.services.factor_mining_factory.engines.context import MiningContext
    from akshare_mcp.services.factor_mining_factory.engines.llm_engine import LLMSearchEngine

    async def empty_prompt_builder(*args, **kwargs):
        raise ValueError("codes cannot be empty")

    class Provider:
        def __init__(self):
            self.prompt = None

        def is_enabled(self):
            return True

        async def generate_candidates(self, prompt, *, candidate_count):
            self.prompt = prompt
            assert getattr(prompt, "system_prompt", "")
            assert getattr(prompt, "user_prompt", "")
            assert "alpha_blueprints" in prompt.request_payload
            return {
                "provider": "fake",
                "model": "fake-model",
                "candidates": [
                    {
                        "name": "llm_blueprint_factor",
                        "hypothesis": "Mutated blueprint candidate.",
                        "economic_hypothesis": "Risk-adjusted momentum may persist.",
                        "family": "momentum",
                        "factor_family": "vol_adjusted_momentum",
                        "inputs": ["momentum_20d", "volatility_20d"],
                        "expression_dsl": "zscore(momentum_20d, 20) - zscore(volatility_20d, 20)",
                        "expected_holding_period": 10,
                        "expected_horizon": 10,
                        "risk_exposure_hint": {"style": ["momentum"]},
                        "blueprint_id": "bp_mom",
                    }
                ],
            }

    provider = Provider()
    monkeypatch.setattr(factor_prompt_builder, "build_factor_mining_prompt", empty_prompt_builder)
    monkeypatch.setattr(factor_llm_provider, "get_factor_llm_provider", lambda: provider)
    monkeypatch.setattr(storage, "get_db", lambda: object())

    context = MiningContext(
        validation_codes=["000001"],
        alpha_blueprints=[
            {
                "blueprint_id": "bp_mom",
                "factor_family": "vol_adjusted_momentum",
                "expression_dsl": "zscore(momentum_20d, 20) - zscore(volatility_20d, 20)",
                "inputs": ["momentum_20d", "volatility_20d"],
                "economic_hypothesis": "Risk-adjusted momentum may persist.",
                "expected_horizon": 10,
                "risk_exposure_hint": {"style": ["momentum"]},
            }
        ],
    )

    candidates = asyncio.run(
        LLMSearchEngine()._generation_chain(context, SearchBudget(candidate_count=2))
    )

    assert candidates[0].generation_engine == "llm_primary"
    assert candidates[0].blueprint_id == "bp_mom"
    assert provider.prompt.source_chain == ["factor_mining_factory.llm_blueprint_fallback", "factor_mining_factory.alpha_blueprints"]


def test_quality_summary_persists_strict_candidate_validation_result():
    from akshare_mcp.services.factor_mining_factory.engines.base import FactorCandidate
    from akshare_mcp.services.factor_mining_factory.factory import FactorMiningFactory

    candidate = FactorCandidate(
        name="strict_blocked_factor",
        expression_dsl="ts_mean(close, 20)",
        generation_engine="gp_classic",
        quick_evidence={"passed": True},
        generation_trace={"strict_validation_attempted": True},
        validation_result={
            "success": True,
            "stage": "validated",
            "metrics": {
                "rank_ic_mean": 0.05,
                "rank_ic_ir": 0.35,
                "positive_ratio": 0.56,
                "sample_dates": 60,
            },
            "coverage": {"avg_cross_section_n": 100, "diagnostic_counts": {}},
            "lookahead_audit": {"risk_level": "low"},
            "persisted_outputs": {"enabled": True, "ic_history_rows": 60},
            "rating": {"grade": "C", "recommendation": "watch"},
        },
    )

    class Context:
        validation_universe_health = {"eligible_count": 300}

    summary = FactorMiningFactory()._build_quality_summary(
        [candidate],
        [candidate],
        [],
        [],
        Context(),
    )

    strict_rows = summary["strict_candidate_results"]
    assert strict_rows[0]["name"] == "strict_blocked_factor"
    assert strict_rows[0]["validation_result"]["metrics"]["rank_ic_mean"] == 0.05
    assert strict_rows[0]["admission_decision"]["evidence_passed"] is True
    assert "rating_grade_not_admissible:C" in strict_rows[0]["admission_decision"]["blockers"]
    quick_rows = summary["quick_candidate_results"]
    assert quick_rows[0]["name"] == "strict_blocked_factor"
    assert quick_rows[0]["passed"] is True
    assert quick_rows[0]["expression_dsl"] == "ts_mean(close, 20)"


def test_factor_meta_learner_uses_strict_outcomes_for_pattern_memory():
    from akshare_mcp.services.factor_mining_factory.engines.base import FactorCandidate
    from akshare_mcp.services.factor_mining_factory.feedback.meta_learner import FactorMetaLearner

    quick_only = FactorCandidate(
        name="quick_only_factor",
        expression_dsl="ts_mean(close, 20)",
        generation_engine="llm_primary",
        factor_family="momentum",
        quick_evidence={"passed": True},
    )
    quick_failed = FactorCandidate(
        name="quick_failed_factor",
        expression_dsl="zscore(momentum_20d, 20)",
        generation_engine="llm_primary",
        factor_family="momentum",
        quick_evidence={"passed": False, "fail_reasons": ["rank_ic_mean_below_min"]},
    )
    strict_blocked = FactorCandidate(
        name="strict_blocked_factor",
        expression_dsl="ts_mean(close, 20)",
        generation_engine="llm_primary",
        factor_family="volatility_reversal",
        blueprint_id="bp_pbo_high",
        quick_evidence={"passed": True},
        generation_trace={"strict_validation_attempted": True},
        validation_result={
            "success": True,
            "metrics": {
                "sample_dates": 60,
                "rank_ic_mean": 0.03,
                "rank_ic_ir": 0.35,
                "positive_ratio": 0.6,
            },
            "coverage": {"avg_cross_section_n": 120},
            "persisted_outputs": {"ic_history_rows": 60},
            "lookahead_audit": {"risk_level": "low"},
            "rating": {
                "grade": "D",
                "governance": {
                    "admission_blocked": True,
                    "admission_block_reasons": ["multiple_testing_risk_high"],
                },
            },
        },
    )
    strict_passed = FactorCandidate(
        name="strict_passed_factor",
        expression_dsl="ts_mean(close, 20)",
        generation_engine="llm_primary",
        factor_family="quality",
        blueprint_id="bp_quality",
        generation_trace={"strict_validation_attempted": True},
        validation_result={
            "success": True,
            "metrics": {
                "sample_dates": 60,
                "rank_ic_mean": 0.04,
                "rank_ic_ir": 0.45,
                "positive_ratio": 0.62,
            },
            "coverage": {"avg_cross_section_n": 140},
            "persisted_outputs": {"ic_history_rows": 60},
            "lookahead_audit": {"risk_level": "low"},
            "rating": {"grade": "A", "governance": {"admission_blocked": False}},
        },
    )

    learner = FactorMetaLearner()
    asyncio.run(
        learner.record_cycle(
            run_id="run_meta",
            raw_count=3,
            evolved_count=3,
            validated_count=1,
            admitted_count=0,
            candidates=[quick_only, quick_failed, strict_blocked, strict_passed],
        )
    )

    memory = learner.get_pattern_memory()
    assert {"pattern": "bp_quality", "count": 1} in memory["successful_pattern_memory"]
    assert {"pattern": "bp_pbo_high", "count": 1} in memory["failed_pattern_memory"]
    assert {"pattern": "momentum", "count": 1} in memory["failed_pattern_memory"]
    assert all(row["pattern"] != "llm_primary:momentum" for row in memory["successful_pattern_memory"])


def test_rule_seed_filters_failed_memory_patterns(monkeypatch):
    from akshare_mcp.services.factor_mining_factory.engines.base import FactorCandidate
    from akshare_mcp.services.factor_mining_factory.engines.rule_engine import RuleSeedEngine

    class Context:
        failed_pattern_memory = [
            {"pattern": "momentum", "count": 3},
            {"pattern": "volume_ratio", "count": 3},
        ]

    candidates = [
        FactorCandidate(
            name="bad_family",
            family="momentum",
            expression_dsl="momentum_20d",
        ),
        FactorCandidate(
            name="bad_parent",
            family="custom",
            expression_dsl="momentum_20d + volume_ratio_5_20",
            generation_trace={"parent_a": "volume_ratio"},
        ),
        FactorCandidate(
            name="kept",
            family="quality",
            expression_dsl="ts_mean(close, 20)",
        ),
    ]

    monkeypatch.setenv("FACTOR_MINING_PATTERN_FILTER_MIN_COUNT", "3")
    kept = RuleSeedEngine()._filter_failed_patterns(candidates, Context())

    assert [candidate.name for candidate in kept] == ["kept"]


def test_rule_seed_memory_fallback_skips_failed_seed_prefix(monkeypatch):
    from akshare_mcp.services.factor_mining_factory.engines.rule_engine import RuleSeedEngine

    class Context:
        failed_pattern_memory = [
            {"pattern": "momentum", "count": 3},
            {"pattern": "trend", "count": 3},
        ]

    monkeypatch.setenv("FACTOR_MINING_PATTERN_FILTER_MIN_COUNT", "3")
    candidates = RuleSeedEngine()._from_seed_library_excluding(2, Context())

    assert candidates
    assert all(candidate.family not in {"momentum", "trend"} for candidate in candidates)


def test_rule_seed_expands_and_ranks_without_running_quick_validation(monkeypatch):
    from akshare_mcp.services.factor_mining_factory.engines.base import SearchBudget
    from akshare_mcp.services.factor_mining_factory.engines.rule_engine import RuleSeedEngine

    class Context:
        alpha_blueprints = []
        failed_pattern_memory = []

        async def quick_evidence_evaluator(self, candidate):
            raise AssertionError(f"quick validation must not run inside rule_seed: {candidate.name}")

    # ATR-inverse 短窗族是可选探索族，生产默认 OFF（见 rule_engine._family_rank：
    # 稳定族优先，volatility 排最末）。本测试要验的是「rule_seed 内不跑 quick
    # validation」以及该族的候选形态，因此显式开启开关，且不断言它排第几名
    # —— 位次由排序策略决定，属于实现细节。
    monkeypatch.setenv("FACTOR_MINING_ALLOW_ATR_INVERSE_VARIANTS", "1")

    engine = RuleSeedEngine()
    monkeypatch.setattr(engine, "_generate_variants", lambda _count: [])

    atr_variants = engine._targeted_low_volatility_variants()

    # 关键行为一：rule_seed 生成阶段绝不触发 quick validation。
    # Context.quick_evidence_evaluator 一旦被调用就会抛 AssertionError，
    # 因此 generate 能正常返回即证明该行为成立。
    candidates = asyncio.run(engine.generate(Context(), SearchBudget(candidate_count=2)))
    assert candidates

    # 关键行为二：候选未被预填 quick_evidence。
    assert all(candidate.quick_evidence == {} for candidate in candidates)

    # 关键行为三：ATR-inverse 族的候选形态正确（含日内区间输入）。
    assert atr_variants
    range_based = [
        candidate
        for candidate in atr_variants
        if candidate.generation_trace.get("mode") == "targeted_low_volatility_variant"
        and "high" in candidate.inputs
    ]
    assert range_based
    assert all(candidate.quick_evidence == {} for candidate in atr_variants)


def test_rule_seed_generates_directional_inversion_candidates():
    from akshare_mcp.services.factor_mining_factory.engines.base import FactorCandidate
    from akshare_mcp.services.factor_mining_factory.engines.rule_engine import RuleSeedEngine

    base = FactorCandidate(
        name="seed_atr_14",
        family="atr",
        inputs=["high", "low", "close"],
        expression_dsl="(high - low) / close",
        generation_trace={"seed_name": "atr_14"},
    )

    inverted = RuleSeedEngine()._directional_inversions([base])

    assert inverted[0].name == "seed_atr_14_inverse"
    assert inverted[0].expression_dsl == "-((high - low) / close)"
    assert inverted[0].generation_trace["mode"] == "directional_inversion"
    assert inverted[0].generation_trace["parent"] == "seed_atr_14"


def test_rule_seed_keeps_targeted_variants_despite_broad_family_memory():
    from akshare_mcp.services.factor_mining_factory.engines.base import FactorCandidate
    from akshare_mcp.services.factor_mining_factory.engines.rule_engine import RuleSeedEngine

    class Context:
        failed_pattern_memory = [{"pattern": "volatility", "count": 4}]

    engine = RuleSeedEngine()
    generic = FactorCandidate(
        name="generic_vol",
        family="volatility",
        factor_family="volatility",
        expression_dsl="-zscore(volatility_20d, 20)",
    )
    targeted = engine._targeted_low_volatility_variants()[0]

    kept = engine._filter_failed_patterns([generic, targeted], Context())

    assert kept == [targeted]


def test_evolution_novelty_filter_keeps_one_candidate_for_small_population():
    from akshare_mcp.services.factor_mining_factory.engines.base import FactorCandidate
    from akshare_mcp.services.factor_mining_factory.evolution.optimizer import EvolutionaryOptimizer

    class Context:
        seed_factors = [
            {"expression_dsl": "zscore(volume_ratio_5_20, 10)"},
            {"expression_dsl": "ts_mean(returns_1d, 20)"},
            {"expression_dsl": "-return_5d"},
        ]

    candidates = [
        FactorCandidate(
            name="a",
            expression_dsl="zscore(volume_ratio_5_20, 10)",
            fitness=0.1,
        ),
        FactorCandidate(
            name="b",
            expression_dsl="ts_mean(returns_1d, 20)",
            fitness=0.3,
        ),
        FactorCandidate(
            name="c",
            expression_dsl="-return_5d",
            fitness=0.2,
        ),
    ]

    filtered = EvolutionaryOptimizer()._novelty_filter(candidates, Context())

    assert [candidate.name for candidate in filtered] == ["b"]


def test_llm_blueprint_fallback_prompt_includes_strict_admission_policy():
    from akshare_mcp.services.factor_mining_factory.engines.base import SearchBudget
    from akshare_mcp.services.factor_mining_factory.engines.llm_engine import LLMSearchEngine

    prompt = LLMSearchEngine()._build_blueprint_fallback_prompt(
        context=object(),
        budget=SearchBudget(candidate_count=2),
        codes=["000001", "000002"],
        blueprints=[],
    )

    policy = prompt.request_payload["dsl_contract"]["strict_admission_policy"]
    assert policy["minimum_evidence"]["abs_rank_ic_mean"] == 0.025
    assert "multiple_testing risk must be low" in policy["governance"]
    assert "low multiple-testing risk" in prompt.system_prompt
    assert "failed memory patterns" in prompt.user_prompt


def test_factory_loads_persistent_strict_pattern_memory_from_mining_runs():
    from akshare_mcp.services.factor_mining_factory.factory import FactorMiningFactory
    from akshare_mcp.services.factor_mining_factory.pool.storage import (
        ensure_factor_pool_tables,
        save_mining_run,
    )

    class Db:
        def __init__(self):
            self.conn = sqlite3.connect(":memory:")
            self.conn.row_factory = sqlite3.Row

    async def run():
        db = Db()
        await ensure_factor_pool_tables(db)
        await save_mining_run(
            db,
            {
                "success": True,
                "run_id": "run_strict_memory",
                "quality_summary": {
                    "quick_candidate_results": [
                        {
                            "generation_engine": "llm_primary",
                            "family": "momentum",
                            "passed": False,
                            "fail_reasons": ["rank_ic_mean_below_min"],
                        }
                    ],
                    "strict_candidate_results": [
                        {
                            "blueprint_id": "bp_pbo_high",
                            "generation_engine": "llm_primary",
                            "family": "volatility_reversal",
                            "admission_decision": {
                                "strict_gate_passed": False,
                                "blockers": ["multiple_testing_risk_high"],
                                "rating_grade": "D",
                            },
                        },
                        {
                            "blueprint_id": "bp_quality",
                            "generation_engine": "llm_primary",
                            "family": "quality",
                            "admission_decision": {
                                "strict_gate_passed": True,
                                "blockers": [],
                                "rating_grade": "A",
                            },
                        },
                    ]
                },
            },
        )
        return await FactorMiningFactory()._load_persistent_pattern_memory(db)

    memory = asyncio.run(run())
    assert {"pattern": "bp_pbo_high", "count": 1} in memory["failed_pattern_memory"]
    assert {"pattern": "momentum", "count": 1} in memory["failed_pattern_memory"]
    assert {"pattern": "bp_quality", "count": 1} in memory["successful_pattern_memory"]


# ALPHA-WIRING-V1 (P-B)：放宽 factor_pool 消费门槛的 toggle。
# 已挖出 status=active + 有 DSL 但 quality_status 缺失(null) 的强 IC 因子，
# 默认仍被 promoted 过滤挡住；toggle ON 时放行（quarantine 仍挡）。
def _seed_pool_for_admission():
    from akshare_mcp.services.factor_mining_factory.pool.active_pool import ActiveFactorPool

    pool = ActiveFactorPool()
    pool.hydrate(
        [
            {
                "factor_id": "f_active_null_qs",
                "name": "rl_factor_1",
                "family": "custom",
                "expression_dsl": "ts_rank(open, 5)",
                "status": "active",
                "fitness": 2.0,
                "current_ic": 0.091,
                "validation_summary": {},  # quality_status 缺失
            },
            {
                "factor_id": "f_active_promoted",
                "name": "gp_factor_1",
                "family": "momentum",
                "expression_dsl": "log1p(abs(volume))",
                "status": "active",
                "fitness": 3.0,
                "current_ic": 0.089,
                "validation_summary": {"quality_status": "promoted"},
            },
            {
                "factor_id": "f_active_quarantine",
                "name": "gp_factor_q",
                "family": "momentum",
                "expression_dsl": "ts_mean(close, 5)",
                "status": "active",
                "fitness": 1.0,
                "current_ic": 0.05,
                "validation_summary": {"quality_status": "quarantine"},
            },
        ]
    )
    return pool


def test_factor_pool_admit_default_only_promoted(monkeypatch):
    monkeypatch.delenv("STRATEGY_FACTORY_FACTOR_POOL_ADMIT_ACTIVE_WITHOUT_PROMOTION", raising=False)
    pool = _seed_pool_for_admission()
    factors = asyncio.run(pool.get_active_factors(limit=10))
    names = {item["name"] for item in factors}
    # 默认零变化：只放行 promoted
    assert names == {"gp_factor_1"}


def test_factor_pool_admit_active_without_promotion_toggle(monkeypatch):
    monkeypatch.setenv("STRATEGY_FACTORY_FACTOR_POOL_ADMIT_ACTIVE_WITHOUT_PROMOTION", "1")
    pool = _seed_pool_for_admission()
    factors = asyncio.run(pool.get_active_factors(limit=10))
    names = {item["name"] for item in factors}
    # 放行 promoted + active 且 DSL 非空且非 quarantine 的 null-qs 因子
    assert "gp_factor_1" in names
    assert "rl_factor_1" in names
    # quarantine 显式淘汰态仍被挡
    assert "gp_factor_q" not in names


def test_reappraise_promotes_active_quarantine_stale_records(monkeypatch):
    import asyncio
    import sqlite3

    from akshare_mcp.services.factor_mining_factory import reappraise as reappraise_mod
    from akshare_mcp.services.factor_mining_factory.pool.storage import (
        ensure_factor_pool_tables,
        save_factor_to_pool,
    )

    class _Db:
        def __init__(self):
            self.conn = sqlite3.connect(":memory:")
            self.conn.row_factory = sqlite3.Row

    async def _load_factor_ic_rows(_db, factor_name: str):
        values = [0.05, 0.055, 0.06, 0.065, 0.07] * 12
        return [
            {"rank_ic": value, "ic_value": value, "stock_count": 120 + (idx % 5)}
            for idx, value in enumerate(values)
        ]

    monkeypatch.setattr(reappraise_mod, "_load_factor_ic_rows", _load_factor_ic_rows)

    db = _Db()

    async def _run():
        await ensure_factor_pool_tables(db)
        await save_factor_to_pool(
            db,
            {
                "factor_id": "factor_active_quarantine",
                "name": "active_quarantine_alpha",
                "family": "momentum",
                "expression_dsl": "close / ma(close, 20) - 1",
                "status": "active",
                "validation_summary": {"quality_status": "quarantine"},
                "admission_grade": "B",
                "fitness": 1.0,
            },
        )
        await save_factor_to_pool(
            db,
            {
                "factor_id": "factor_quarantine",
                "name": "quarantine_alpha",
                "family": "momentum",
                "expression_dsl": "close / ma(close, 10) - 1",
                "status": "quarantine",
                "validation_summary": {"quality_status": "quarantine"},
                "admission_grade": "B",
                "fitness": 1.0,
            },
        )
        result = await reappraise_mod.reappraise_quarantine_factors(db, limit=20)
        rows = db.conn.execute(
            "SELECT factor_id, status, validation_summary FROM factor_pool_active ORDER BY factor_id"
        ).fetchall()
        return result, rows

    result, rows = asyncio.run(_run())
    assert result["promoted"] == 2
    assert [row["status"] for row in rows] == ["active", "active"]
    assert all('"quality_status": "promoted"' in row["validation_summary"] for row in rows)


def test_run_mining_cycle_reports_reappraisal_promoted_count(monkeypatch):
    import asyncio
    import sqlite3
    from types import SimpleNamespace

    from akshare_mcp.services.factor_mining_factory.factory import FactorMiningFactory
    from akshare_mcp.services.factor_mining_factory import reappraise as reappraise_module

    class _Db:
        def __init__(self):
            self.conn = sqlite3.connect(":memory:")
            self.conn.row_factory = sqlite3.Row

    class _Pool:
        size = 0

        async def admit_batch(self, validated):
            return []

    class _Scheduler:
        last_engines_used = []

        async def search(self, *args, **kwargs):
            return []

        def record_quality_feedback(self, *args, **kwargs):
            return None

    class _Optimizer:
        async def evolve(self, *args, **kwargs):
            return []

    class _Context:
        validation_codes = list(range(200))
        validation_universe_health = {"eligible_count": 200}

    async def _run():
        factory = FactorMiningFactory()
        factory._initialized = True
        factory._engine_scheduler = _Scheduler()
        factory._evolutionary_optimizer = _Optimizer()
        factory._active_pool = _Pool()
        factory._decay_monitor = SimpleNamespace(daily_check=None)
        factory._meta_learner = SimpleNamespace()

        db = _Db()

        async def _ensure_persistent_pool(_db):
            return None

        async def _build_mining_context(*args, **kwargs):
            return _Context()

        async def _quick_filter_candidates(*args, **kwargs):
            return []

        async def _validate_batch(*args, **kwargs):
            return []

        async def _persist_admitted_factors(*args, **kwargs):
            return None

        async def _persist_mining_run(*args, **kwargs):
            return None

        async def _record_feedback(*args, **kwargs):
            return None

        async def _reappraise(_db, limit=200):
            return {"scanned": 1, "promoted": 2, "kept_quarantine": 0}

        async def _get_db():
            return db

        monkeypatch.setattr(factory, "_get_db", _get_db)
        monkeypatch.setattr(factory, "_ensure_persistent_pool", _ensure_persistent_pool)
        monkeypatch.setattr(factory, "_build_mining_context", _build_mining_context)
        monkeypatch.setattr(factory, "_quick_filter_candidates", _quick_filter_candidates)
        monkeypatch.setattr(factory, "_validate_batch", _validate_batch)
        monkeypatch.setattr(factory, "_persist_admitted_factors", _persist_admitted_factors)
        monkeypatch.setattr(factory, "_persist_mining_run", _persist_mining_run)
        monkeypatch.setattr(factory, "_record_feedback", _record_feedback)
        monkeypatch.setattr(factory, "_install_quick_evidence_evaluators", lambda *a, **k: None)
        monkeypatch.setattr(reappraise_module, "reappraise_quarantine_factors", _reappraise)
        result = await factory.run_mining_cycle(trigger="quality_session")
        return result

    result = asyncio.run(_run())
    assert result["active_promoted_count"] == 2
    assert result["cycle_active_promoted_count"] == 0
    assert result["reappraisal_promoted_count"] == 2
    assert result["quality_summary"]["active_promoted_count"] == 2
    assert result["quality_summary"]["quality_funnel"]["promoted"] == 2
    assert result["quality_summary"]["quality_funnel"]["reappraisal_promoted"] == 2
