from __future__ import annotations

import asyncio
import logging

import pytest

from akshare_mcp.services.strategy_autonomy import StrategyAutonomyService


class _CancelDb:
    def __init__(self) -> None:
        self.saved_runs: list[dict] = []
        self.updated_runs: list[dict] = []
        self.events: list[dict] = []

    async def save_strategy_task_run(self, payload: dict) -> dict:
        self.saved_runs.append(dict(payload))
        return {"id": 101, "trace_id": payload.get("trace_id")}

    async def update_strategy_task_run(self, task_run_id: int, **kwargs) -> dict:
        payload = {"id": task_run_id, **kwargs}
        self.updated_runs.append(payload)
        return payload

    async def save_strategy_domain_event(self, payload: dict) -> dict:
        self.events.append(dict(payload))
        return payload


@pytest.mark.asyncio
async def test_autonomy_run_cycle_cancel_logs_without_warning_traceback(caplog) -> None:
    service = StrategyAutonomyService()

    async def _cancel_pipeline(*_args, **_kwargs):
        raise asyncio.CancelledError()

    service._run_cycle_pipeline = _cancel_pipeline
    db = _CancelDb()

    caplog.set_level(logging.DEBUG, logger="akshare_mcp.services.strategy_autonomy")

    with pytest.raises(asyncio.CancelledError):
        await service.run_cycle(
            db,
            snapshot={"date": "2026-05-22"},
            source="strategy_factory:test",
            research_task={"task_key": "task:cancel", "task_source": "test"},
        )

    assert db.updated_runs
    assert db.updated_runs[-1]["id"] == 101
    assert db.updated_runs[-1]["status"] == "failed"
    assert db.updated_runs[-1]["error"] == "cancelled"
    assert db.updated_runs[-1]["result"]["status"] == "failed"
    assert db.updated_runs[-1]["result"]["error"] == "cancelled"
    assert db.events
    assert db.events[-1]["event_type"] == "strategy_ai_cycle.failed"

    warnings = [
        record
        for record in caplog.records
        if record.levelno == logging.WARNING
        and record.getMessage().startswith("StrategyAutonomyService.run_cycle cancelled")
    ]
    assert warnings
    assert warnings[-1].exc_info is None
    assert "task_run_id=101" in warnings[-1].getMessage()
    assert "task_key=task:cancel" in warnings[-1].getMessage()

    debug_tracebacks = [
        record
        for record in caplog.records
        if record.levelno == logging.DEBUG
        and record.getMessage() == "StrategyAutonomyService.run_cycle cancellation traceback"
    ]
    assert debug_tracebacks
    assert debug_tracebacks[-1].exc_info is not None
