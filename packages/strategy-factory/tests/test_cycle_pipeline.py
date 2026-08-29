from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace


class CompleteRepository:
    def __getattr__(self, name):
        from strategy_factory.application.runtime_boundary import REQUIRED_REPOSITORY_METHODS

        if name not in REQUIRED_REPOSITORY_METHODS:
            raise AttributeError(name)

        async def _method(*_args, **_kwargs):
            return []

        return _method


class FakeScheduler:
    def _now(self):
        return datetime(2026, 5, 20, 12, 0, tzinfo=timezone.utc)

    def _with_stage_meta(self, stage, trace_id, payload, **kwargs):
        data = dict(payload or {})
        data.update({"stage": stage, "trace_id": trace_id, **kwargs})
        return data

    def _build_layered_run_summary(self, summary, _submit=None):
        return dict(summary or {})

    async def _run_startup_warmup(self):
        return {"status": "skipped", "ok": True, "failed": 0}

    def _apply_run_audit(self, results, persistence_failures=None):
        return results


def _runtime_adapters(db):
    return SimpleNamespace(
        repository=SimpleNamespace(raw=db),
        vector_search=object(),
        autonomy=object(),
        factor_research=object(),
        incubation=object(),
        validation=object(),
        risk=object(),
    )


def test_cycle_runner_delegates_through_explicit_pipeline():
    from strategy_factory.application.cycle_pipeline import (
        CYCLE_PIPELINE_STAGE_ALIASES,
        CYCLE_PIPELINE_STAGE_ORDER,
    )
    from strategy_factory.application.cycle_runner import (
        FactoryCycleOutcome,
        FactoryCycleRunner,
        FactoryRunContext,
    )

    db = CompleteRepository()
    context = FactoryRunContext(
        db=db,
        factory_pkg=SimpleNamespace(),
        runtime_adapters=_runtime_adapters(db),
        start=datetime(2026, 5, 20, 11, 59, tzinfo=timezone.utc),
        trace_id="trace-1",
        run_id="run-1",
    )
    runner = FactoryCycleRunner(FakeScheduler(), context)

    async def fake_legacy_run(self):
        return FactoryCycleOutcome(
            {
                "run_id": context.run_id,
                "trace_id": context.trace_id,
                "status": "success",
                "summary": {},
                "stages": {},
            },
            [],
        )

    runner._run_legacy_cycle = fake_legacy_run

    import asyncio

    outcome = asyncio.run(runner.run())

    assert outcome.result["cycle_pipeline"]["stage_order"] == list(CYCLE_PIPELINE_STAGE_ORDER)
    assert outcome.result["cycle_pipeline"]["stage_aliases"] == {
        key: list(value) for key, value in CYCLE_PIPELINE_STAGE_ALIASES.items()
    }
    assert [
        item["name"] for item in outcome.result["cycle_pipeline"]["stage_results"]
    ] == list(CYCLE_PIPELINE_STAGE_ORDER)
    assert outcome.result["summary"]["cycle_pipeline_stage_order"] == list(CYCLE_PIPELINE_STAGE_ORDER)
    assert outcome.result["summary"]["cycle_pipeline_stage_aliases"] == {
        key: list(value) for key, value in CYCLE_PIPELINE_STAGE_ALIASES.items()
    }
    assert "candidate_governance" not in outcome.result["summary"]["cycle_pipeline_stage_order"]


def test_cycle_pipeline_returns_runtime_boundary_stage_on_missing_contract():
    from strategy_factory.application.cycle_runner import FactoryCycleRunner, FactoryRunContext

    context = FactoryRunContext(
        db=object(),
        factory_pkg=SimpleNamespace(),
        runtime_adapters=None,
        start=datetime(2026, 5, 20, 11, 59, tzinfo=timezone.utc),
        trace_id="trace-2",
        run_id="run-2",
    )
    runner = FactoryCycleRunner(FakeScheduler(), context)

    import asyncio

    outcome = asyncio.run(runner.run())

    result = outcome.result
    assert result["status"] == "failed"
    assert "runtime_boundary_failed" in result["stages"]
    assert result["runtime_boundary"]["status"] == "runtime_boundary_failed"
    for key in ("summary", "stages", "quality_gate", "backtest_report", "submit_result", "artifact_refs"):
        assert key in result


def test_cycle_pipeline_reports_each_runner_stage_under_its_own_name():
    from strategy_factory.application.cycle_runner import (
        FactoryCycleOutcome,
        FactoryCycleRunner,
        FactoryRunContext,
    )

    db = CompleteRepository()
    context = FactoryRunContext(
        db=db,
        factory_pkg=SimpleNamespace(),
        runtime_adapters=_runtime_adapters(db),
        start=datetime(2026, 5, 20, 11, 59, tzinfo=timezone.utc),
        trace_id="trace-3",
        run_id="run-3",
    )
    runner = FactoryCycleRunner(FakeScheduler(), context)

    async def fake_legacy_run(self):
        return FactoryCycleOutcome(
            {
                "run_id": context.run_id,
                "trace_id": context.trace_id,
                "status": "success",
                "summary": {},
                "stages": {
                    "spawn": {"status": "completed"},
                    "autonomy": {"status": "partial"},
                    "quality_gate": {"status": "completed"},
                    "backtest": {"status": "completed"},
                    "deduplicate": {"status": "completed"},
                    "submit": {"status": "completed"},
                },
            },
            [],
        )

    runner._run_legacy_cycle = fake_legacy_run

    import asyncio

    outcome = asyncio.run(runner.run())
    canonical = {
        item["name"]: item
        for item in outcome.result["cycle_pipeline"]["stage_results"]
    }

    # Each pipeline stage now reports under the name the runner actually emits,
    # so a stage's status maps straight through instead of being folded into a
    # coarser label. A partial leg stays visible as that leg.
    assert canonical["autonomy"]["status"] == "partial"
    assert canonical["autonomy"]["observed_stage_names"] == ["autonomy"]
    assert canonical["spawn"]["status"] == "completed"
    assert canonical["spawn"]["observed_stage_names"] == ["spawn"]
    assert "candidate_governance" not in canonical
    for stage in ("quality_gate", "backtest", "deduplicate", "submit"):
        assert canonical[stage]["status"] == "completed"
        assert canonical[stage]["observed_stage_names"] == [stage]
