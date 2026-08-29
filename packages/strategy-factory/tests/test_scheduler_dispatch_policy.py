from __future__ import annotations

import asyncio
from types import MethodType


def test_shadow_readonly_skips_paper_trading_cycle(monkeypatch) -> None:
    from strategy_factory.application.factory_execution import FactoryExecutionMode
    from strategy_factory.application.factory_scheduler import StrategyFactoryScheduler
    from strategy_factory.application.research import paper_trading_scheduler

    paper_calls = {"count": 0}

    async def _paper_cycle(_db):
        paper_calls["count"] += 1
        return {"status": "success"}

    monkeypatch.setattr(paper_trading_scheduler, "run_paper_trading_cycle", _paper_cycle)

    scheduler = StrategyFactoryScheduler()
    cycle_calls: list[dict] = []

    async def _cycle_once(
        self,
        resolved_db,
        *,
        previous_result,
        execution_mode,
        parity_role="primary",
        read_only=False,
        run_id=None,
        trace_id=None,
        target_codes=None,
    ):
        cycle_calls.append({"parity_role": parity_role, "read_only": read_only})
        return (
            {
                "run_id": run_id or "factory_run_primary",
                "trace_id": trace_id or "trace-primary",
                "status": "success",
                "summary": {},
                "artifacts": [],
            },
            [],
        )

    scheduler._execute_factory_cycle_once = MethodType(_cycle_once, scheduler)

    result, failures = asyncio.run(
        scheduler._execute_factory_run_once_mode(
            object(),
            previous_result=None,
            execution_mode=FactoryExecutionMode.SHADOW_READONLY,
        )
    )

    assert failures == []
    assert paper_calls["count"] == 0
    assert result["paper_trading_cycle"]["status"] == "skipped"
    assert result["paper_trading_cycle"]["reason"] == "shadow_readonly"
    assert result["summary"]["paper_trading_cycle"]["reason"] == "shadow_readonly"
    assert cycle_calls == [
        {"parity_role": "primary", "read_only": False},
        {"parity_role": "shadow", "read_only": True},
    ]


def test_gate3_record_only_skips_paper_trading_cycle(monkeypatch) -> None:
    from strategy_factory.application.factory_execution import FactoryExecutionMode
    from strategy_factory.application.factory_scheduler import StrategyFactoryScheduler
    from strategy_factory.application.research import paper_trading_scheduler

    monkeypatch.setenv("STRATEGY_FACTORY_GATE3_RECORD_ONLY_ENABLED", "1")
    paper_calls = {"count": 0}

    async def _paper_cycle(_db):
        paper_calls["count"] += 1
        return {"status": "success"}

    monkeypatch.setattr(paper_trading_scheduler, "run_paper_trading_cycle", _paper_cycle)

    scheduler = StrategyFactoryScheduler()

    async def _cycle_once(
        self,
        resolved_db,
        *,
        previous_result,
        execution_mode,
        parity_role="primary",
        read_only=False,
        run_id=None,
        trace_id=None,
        target_codes=None,
    ):
        return (
            {
                "run_id": run_id or "factory_run_primary",
                "trace_id": trace_id or "trace-primary",
                "status": "success",
                "summary": {},
                "artifacts": [],
            },
            [],
        )

    scheduler._execute_factory_cycle_once = MethodType(_cycle_once, scheduler)

    result, failures = asyncio.run(
        scheduler._execute_factory_run_once_mode(
            object(),
            previous_result=None,
            execution_mode=FactoryExecutionMode.STOCK_FIRST_OBSERVE_PRIMARY,
        )
    )

    assert failures == []
    assert paper_calls["count"] == 0
    assert result["paper_trading_cycle"]["status"] == "skipped"
    assert result["paper_trading_cycle"]["reason"] == "gate3_record_only"
    assert result["summary"]["paper_trading_cycle"]["reason"] == "gate3_record_only"


class _DispatchDb:
    def __init__(self):
        self.rows: dict[str, dict] = {}

    async def create_strategy_factory_dispatch(self, payload: dict) -> dict:
        row = dict(payload)
        self.rows[row["dispatch_id"]] = row
        return dict(row)

    async def update_strategy_factory_dispatch(self, dispatch_id: str, **kwargs):
        if dispatch_id not in self.rows:
            return None
        self.rows[dispatch_id].update(kwargs)
        return dict(self.rows[dispatch_id])

    async def get_strategy_factory_dispatch(self, dispatch_id: str):
        row = self.rows.get(dispatch_id)
        return dict(row) if row else None


def test_dispatch_run_persists_partial_status(monkeypatch) -> None:
    from strategy_factory.application.factory_scheduler import StrategyFactoryScheduler

    monkeypatch.setenv("STRATEGY_FACTORY_ALLOW_PARALLEL_FULL_CYCLES", "0")
    monkeypatch.setenv("STRATEGY_FACTORY_MAX_CONCURRENT_DISPATCHES", "5")

    scheduler = StrategyFactoryScheduler()
    db = _DispatchDb()
    seen_target_codes: list[str] = []

    async def _partial_run_once(self, db=None, *, execution_mode=None, dispatch_id=None, target_codes=None):
        seen_target_codes.extend(list(target_codes or []))
        return {
            "run_id": "factory_run_partial",
            "status": "partial",
            "parity_result": {"status": "matched"},
            "artifact_refs": [{"artifact_type": "research_plane"}],
        }

    scheduler.run_once = MethodType(_partial_run_once, scheduler)

    async def _run():
        accepted = await scheduler.dispatch_run(db=db, target_codes=["600000", "000001"])
        dispatch_id = accepted["dispatch_id"]
        task = scheduler._dispatch_tasks[dispatch_id]
        await task
        return await scheduler.get_dispatch_status(dispatch_id, db=db)

    status = asyncio.run(_run())

    assert status is not None
    assert status["status"] == "partial"
    assert status["run_id"] == "factory_run_partial"
    assert status["metadata"]["result_status"] == "partial"
    assert status["metadata"]["degraded"] is True
    assert status["metadata"]["target_codes"] == ["600000", "000001"]
    assert seen_target_codes == ["600000", "000001"]


def test_dispatch_run_accepts_multiple_queued_requests_by_default(monkeypatch) -> None:
    from strategy_factory.application.factory_scheduler import StrategyFactoryScheduler

    monkeypatch.setenv("STRATEGY_FACTORY_ALLOW_PARALLEL_FULL_CYCLES", "0")
    monkeypatch.setenv("STRATEGY_FACTORY_MAX_CONCURRENT_DISPATCHES", "5")

    scheduler = StrategyFactoryScheduler()
    db = _DispatchDb()
    release_first = asyncio.Event()
    started: list[str] = []

    async def _run_once(self, db=None, *, execution_mode=None, dispatch_id=None, target_codes=None):
        started.append(str(dispatch_id))
        if len(started) == 1:
            await release_first.wait()
        return {
            "run_id": f"factory_run_{len(started)}",
            "status": "success",
            "summary": {"dispatch_id": dispatch_id},
            "artifact_refs": [],
        }

    scheduler.run_once = MethodType(_run_once, scheduler)

    async def _run():
        first = await scheduler.dispatch_run(db=db)
        second = await scheduler.dispatch_run(db=db)
        await asyncio.sleep(0)

        assert first["dispatch_id"] != second["dispatch_id"]
        assert first["already_running"] is False
        assert second["already_running"] is False
        assert first["dispatch_concurrency_limit"] == 1
        assert second["dispatch_concurrency_limit"] == 1

        first_status = await scheduler.get_dispatch_status(first["dispatch_id"], db=db)
        second_status = await scheduler.get_dispatch_status(second["dispatch_id"], db=db)
        assert first_status is not None
        assert second_status is not None
        assert first_status["status"] == "running"
        assert second_status["status"] == "queued"

        release_first.set()
        await asyncio.gather(*list(scheduler._dispatch_tasks.values()))
        return first, second

    first, second = asyncio.run(_run())

    assert started == [first["dispatch_id"], second["dispatch_id"]]
    assert db.rows[first["dispatch_id"]]["status"] == "success"
    assert db.rows[second["dispatch_id"]]["status"] == "success"


def test_dispatch_run_can_execute_parallel_full_cycles_when_enabled(monkeypatch) -> None:
    from strategy_factory.application.factory_scheduler import StrategyFactoryScheduler

    monkeypatch.setenv("STRATEGY_FACTORY_ALLOW_PARALLEL_FULL_CYCLES", "1")
    monkeypatch.setenv("STRATEGY_FACTORY_MAX_CONCURRENT_DISPATCHES", "2")

    scheduler = StrategyFactoryScheduler()
    db = _DispatchDb()
    active = 0
    max_active = 0
    started: list[str] = []

    async def _execute_mode(self, resolved_db, **kwargs):
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        started.append(str(kwargs.get("execution_mode")))
        await asyncio.sleep(0.05)
        active -= 1
        return (
            {
                "run_id": f"factory_run_parallel_{len(started)}",
                "status": "success",
                "summary": {},
                "artifact_refs": [],
            },
            [],
        )

    async def _persist_run_result(self, db, results, *, persistence_failures):
        return None

    scheduler._execute_factory_run_once_mode = MethodType(_execute_mode, scheduler)
    scheduler._persist_run_result = MethodType(_persist_run_result, scheduler)

    async def _run():
        first = await scheduler.dispatch_run(db=db)
        second = await scheduler.dispatch_run(db=db)
        await asyncio.gather(*list(scheduler._dispatch_tasks.values()))
        return first, second

    first, second = asyncio.run(_run())

    assert first["dispatch_concurrency_limit"] == 2
    assert second["dispatch_concurrency_limit"] == 2
    assert first["parallel_full_cycles"] is True
    assert second["parallel_full_cycles"] is True
    assert max_active == 2
    assert db.rows[first["dispatch_id"]]["status"] == "success"
    assert db.rows[second["dispatch_id"]]["status"] == "success"
    assert db.rows[first["dispatch_id"]]["metadata"]["parallel_full_cycles"] is True
    assert db.rows[second["dispatch_id"]]["metadata"]["parallel_full_cycles"] is True
