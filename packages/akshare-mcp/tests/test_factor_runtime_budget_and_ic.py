"""Real validation and engine behavior for the first continuous-run defects."""

import asyncio
import copy
import threading
import time
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from akshare_mcp.services._factor_validation_pipeline_support import (
    _aggregate_rank_ic, _build_robustness_report, _cross_section_summary,
)
from akshare_mcp.services.factor_mining_factory.engines.base import FactorCandidate, SearchBudget
from akshare_mcp.services.factor_mining_factory.engines.engine_scheduler import EngineScheduler
from akshare_mcp.services.factor_mining_factory.engines.gp_engine import GeneticProgrammingEngine
from akshare_mcp.services.factor_mining_factory.engines.mcts_engine import MCTSSearchEngine
from akshare_mcp.services.factor_mining_factory.engines.rl_engine import RLAlphaGenEngine


@pytest.mark.parametrize("case,reason", [
    ("constant_factor", "undefined_correlation"),
    ("constant_returns", "undefined_correlation"),
    ("too_few", "insufficient_samples"),
])
def test_aggregate_unmeasurable_ic_is_not_a_zero(case, reason):
    factor = pd.DataFrame([[1., 2., 3.]] * 4)
    returns = pd.DataFrame([[.03, .01, .02]] * 4)
    if case == "constant_factor":
        factor[:] = 1.
    elif case == "constant_returns":
        returns[:] = .02
    else:
        factor = factor.iloc[:, :2]
    result = _aggregate_rank_ic(factor, returns)
    assert result["available"] is False
    assert result["reason"] == "no_measurable_rank_ic"
    assert result["rank_ic_mean"] is None
    assert result["positive_ratio"] is None
    assert result["sample_dates"] == 0
    assert result["unmeasurable_dates"] == 4
    assert result["diagnostic_counts"] == {reason: 4}


def test_partial_constant_dates_keep_only_measured_ic():
    factor = pd.DataFrame([[1., 1., 1.], [1., 2., 3.], [1., 2., 3.]])
    returns = pd.DataFrame([[.01, .02, .03]] * 3)
    result = _aggregate_rank_ic(factor, returns)
    assert result["available"] is True
    assert result["rank_ic_mean"] == pytest.approx(1.)
    assert result["sample_dates"] == 2
    assert result["unmeasurable_dates"] == 1
    assert result["diagnostic_counts"] == {"undefined_correlation": 1}


def test_cross_section_excludes_unmeasurable_dates_from_evidence():
    result = _cross_section_summary({
        "bad": [{"factor_value": 1., "future_return": x} for x in (.1, .2, .3)],
        "good": [{"factor_value": x, "future_return": x} for x in (.1, .2, .3)],
    })
    assert result["summary"]["sample_dates"] == 1
    assert result["summary"]["rank_ic_mean"] == 1.
    assert result["unmeasurable_dates"][0]["rank_ic"] is None
    assert result["attempted_dates"] == 2
    assert [row["date"] for row in result["dates"]] == ["good"]


def test_robustness_unmeasurable_halves_remain_unavailable():
    rng = np.random.default_rng(117)
    prices = pd.DataFrame(50 + np.cumsum(rng.normal(size=(80, 6)), axis=0))
    result = _build_robustness_report(prices * 0, prices, base_horizon=10)
    assert result["available"] is False
    assert result["reason"] == "no_measurable_rank_ic"
    assert result["robustness_score"] is None
    assert result["time_split_consistency"]["score"] is None
    assert result["time_split_consistency"]["detail"]["same_sign"] is None


class SyntheticDB:
    def __init__(self, n_codes=90):
        rng = np.random.default_rng(173)
        dates = pd.bdate_range("2026-01-01", periods=180).strftime("%Y-%m-%d")
        self.frames = {}
        self.saved_ic = []
        for i in range(n_codes):
            close = 20 + i * .1 + np.cumsum(rng.normal(0, .15, 180))
            volume = rng.integers(100000, 200000, 180).astype(float)
            self.frames[f"{i:06d}"] = [
                {"date": str(d), "open": float(c), "high": float(c*1.01),
                 "low": float(c*.99), "close": float(c), "volume": float(v),
                 "amount": float(v*20), "turnover": .02}
                for d, c, v in zip(dates, close, volume)
            ]

    async def get_klines(self, code, limit=None):
        return self.frames[code][-limit:]

    async def save_factor_ic(self, *args):
        self.saved_ic.append(args)


@pytest.mark.parametrize("sign", ["", "-"])
def test_real_quick_pipeline_handles_constant_liquidity_and_never_persists_fake_ic(sign):
    from akshare_mcp.services.factor_validation_pipeline import validate_factor_candidate_pipeline
    from akshare_mcp.services.factor_mining_factory.quick_evidence import QuickEvidenceEvaluator
    db = SyntheticDB()
    candidate = FactorCandidate(name="constant_liquidity", expression_dsl=f"{sign}(ts_rank(amount, 20) - ts_rank(volume, 20))", inputs=["amount", "volume"], hypothesis="test")
    result = asyncio.run(validate_factor_candidate_pipeline(
        db, candidate.to_validation_dict(), codes=list(db.frames), stage="quick",
        lookback_bars=180, max_dates=60, min_cross_section=80,
        persist_outputs=True, persist_ic_history=True,
    ))
    assert result["success"] is True
    assert result["metrics"]["rank_ic_mean"] is None
    assert result["metrics"]["sample_dates"] == 0
    assert result["robustness"]["robustness_score"] is None
    assert result["quality_evidence"]["passed"] is False
    assert result["quality_evidence"]["summary"]["rank_ic_mean"] is None
    assert "no_measurable_rank_ic" in result["quality_evidence"]["reasons"]
    assert result["coverage"]["diagnostic_counts"]["undefined_correlation"] == 60
    assert db.saved_ic == []
    evidence = QuickEvidenceEvaluator(db, codes=list(db.frames))._from_validation_result(result)
    assert evidence["rank_ic_mean"] is None
    assert evidence["evidence_summary"]["rank_ic_mean"] is None
    assert evidence["passed"] is False
    assert "quick_validation_failed" not in evidence["fail_reasons"]


def _candidate(name, engine_id):
    return FactorCandidate(name=name, hypothesis="test", inputs=["close"], expression_dsl=f"ts_mean(close, {5 if name == 'first' else 10})", generation_engine=engine_id, fitness=1.)


def _real_engine(kind, monkeypatch):
    if kind == "gp":
        engine = GeneticProgrammingEngine(population_size=10, max_generations=1)
        monkeypatch.setattr(engine, "_blueprint_candidates", lambda context, count: [_candidate("first", engine.engine_id), _candidate("second", engine.engine_id)])
    elif kind == "mcts":
        engine = MCTSSearchEngine(simulations=30)
        monkeypatch.setattr(engine, "_blueprint_candidates", lambda context, count: [_candidate("first", engine.engine_id), _candidate("second", engine.engine_id)])
    else:
        engine = RLAlphaGenEngine(episodes_per_run=5)
        expressions = iter(["ts_mean(close, 5)", "ts_mean(close, 10)", "ts_mean(close, 20)"])
        monkeypatch.setattr(engine, "_rollout", lambda: (next(expressions), [(["ts_op"], "ts_mean")]))
    return engine


@pytest.mark.parametrize("kind", ["gp", "mcts", "rl"])
def test_real_cpu_engines_preserve_partial_results_and_stop_before_return(kind, monkeypatch):
    engine = _real_engine(kind, monkeypatch)
    calls = []
    async def evaluator(candidate):
        calls.append(candidate.expression_dsl)
        await asyncio.sleep(.005 if len(calls) == 1 else .6)
        calls.append("completed")
        return {"passed": True, "quality_score": 90., "rank_ic_mean": .05}
    context = SimpleNamespace(seed_factors=[], alpha_blueprints=[], quick_evidence_evaluator=evaluator, quick_ic_evaluator=evaluator)
    scheduler = EngineScheduler.__new__(EngineScheduler)
    async def run():
        result = await scheduler._run_engine(engine, context, SearchBudget(candidate_count=3, max_time_sec=.2))
        before = (copy.deepcopy(calls), copy.deepcopy(engine.__dict__))
        assert not any(t.name == f"factor-engine-{engine.engine_id}" for t in threading.enumerate())
        await asyncio.sleep(.05)
        assert calls == before[0]
        assert engine._success_count == before[1]["_success_count"]
        if kind == "rl":
            assert engine._policy._q_table == before[1]["_policy"]._q_table
            assert engine._total_episodes == before[1]["_total_episodes"]
        return result
    result = asyncio.run(run())
    assert result
    assert result[0].expression_dsl == "ts_mean(close, 5)"
    assert calls == ["ts_mean(close, 5)", "completed", "ts_mean(close, 10)"]
    outcome = scheduler._last_runs[engine.engine_id]
    assert outcome["status"] == "partial"
    assert outcome["reason"] == "search_time_budget_exhausted"
    assert result[0].generation_trace["search_budget"]["status"] == "partial"
    assert engine.get_status().last_error == "search_time_budget_exhausted"


def test_blocking_atomic_call_is_joined_instead_of_abandoned():
    class SlowEngine:
        engine_id = "gp_classic"
        async def generate(self, context, budget):
            time.sleep(.12)
            context.completed = True
            return [_candidate("first", self.engine_id)]
    context = SimpleNamespace(completed=False)
    scheduler = EngineScheduler.__new__(EngineScheduler)
    result = asyncio.run(scheduler._run_engine(SlowEngine(), context, SearchBudget(max_time_sec=.03)))
    assert context.completed
    assert result  # An atomic call that completed still has a reviewable result.
    assert scheduler._last_runs["gp_classic"]["budget_overrun_seconds"] >= .08
    assert not any(t.name == "factor-engine-gp_classic" for t in threading.enumerate())


def test_outer_cancellation_joins_worker_before_propagating():
    class CancellableEngine:
        engine_id = "gp_classic"
        async def generate(self, context, budget):
            await asyncio.sleep(10)
            context.completed = True
            return []
    scheduler = EngineScheduler.__new__(EngineScheduler)
    context = SimpleNamespace(completed=False)
    async def run():
        task = asyncio.create_task(scheduler._run_engine(CancellableEngine(), context, SearchBudget(max_time_sec=20)))
        await asyncio.sleep(.02)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not any(t.name == "factor-engine-gp_classic" for t in threading.enumerate())
    asyncio.run(run())
    assert context.completed is False


def test_engine_outcomes_are_in_the_persisted_quality_summary():
    from strategy_factory.application.factor_mining.quality_summary import build_quality_summary
    context = SimpleNamespace(engine_runs={
        "gp_classic": {"status": "partial", "elapsed_seconds": 120.1, "candidate_count": 2},
        "rl_alphagen": {"status": "timed_out", "candidate_count": 0},
    })
    summary = build_quality_summary([], [], [], [], [], context)
    assert summary["engine_runs"] == context.engine_runs
    assert summary["engine_runs"]["gp_classic"] is not context.engine_runs["gp_classic"]


def test_real_quick_pipeline_stops_between_stocks_without_caching_incomplete_evidence(monkeypatch):
    from akshare_mcp.services.factor_mining_factory.quick_evidence import QuickEvidenceEvaluator
    class SlowDB(SyntheticDB):
        calls = 0
        async def get_klines(self, code, limit=None):
            self.calls += 1
            time.sleep(.03)  # Deliberately synchronous, as the production DB bridge can be.
            return await super().get_klines(code, limit)
    db = SlowDB(n_codes=120)
    evaluator = QuickEvidenceEvaluator(db, codes=list(db.frames))
    engine = _real_engine("gp", monkeypatch)
    context = SimpleNamespace(seed_factors=[], quick_evidence_evaluator=evaluator)
    scheduler = EngineScheduler.__new__(EngineScheduler)
    result = asyncio.run(scheduler._run_engine(engine, context, SearchBudget(candidate_count=3, max_time_sec=.15)))
    assert result
    assert 0 < db.calls < 120
    assert evaluator.evaluation_count == 1
    assert evaluator._cache == {}
    assert all(candidate.quick_evidence == {} for candidate in result)
    assert scheduler._last_runs[engine.engine_id]["status"] == "partial"
    assert not any(t.name == f"factor-engine-{engine.engine_id}" for t in threading.enumerate())

