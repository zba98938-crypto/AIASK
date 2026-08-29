from __future__ import annotations

import asyncio

import pytest

from strategy_factory.application._autonomy_task_executor import (
    AutonomyTaskExecutionContext,
    execute_autonomy_task,
)


class _FakeSemaphore:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _FakeDb:
    pass


@pytest.mark.asyncio
async def test_execute_autonomy_task_marks_outer_task_run_failed_on_cancel() -> None:
    updates: list[dict] = []

    async def _call_optional_async(_db, method_name: str, *args, default=None, **kwargs):
        if method_name == "save_strategy_task_run":
            return {"id": 42, "trace_id": "trace-cancel"}
        if method_name == "update_strategy_task_run":
            updates.append({"args": args, "kwargs": kwargs})
            return {"id": args[0], **kwargs}
        return default

    async def _generate_for_research_task(*_args, **_kwargs):
        raise asyncio.CancelledError()

    context = AutonomyTaskExecutionContext(
        task={"task_id": "task_cancel", "task_key": "scope:key", "task_source": "test"},
        task_semaphore=_FakeSemaphore(),
        db=_FakeDb(),
        snapshot={"date": "2026-05-22"},
        autonomy_gateway=object(),
        persist_task_evidence=lambda *_args, **_kwargs: asyncio.sleep(0, result=[]),
        extract_event_context=lambda task: {"task_id": task.get("task_id")},
        call_optional_async=_call_optional_async,
        record_persistence_failure=lambda *_args, **_kwargs: None,
        generate_for_research_task=_generate_for_research_task,
        extract_cycle_llm_generation=lambda _cycle: {},
        extract_cycle_lifecycle=lambda _cycle: {},
        extract_cycle_generated_count=lambda _cycle: 0,
        extract_cycle_reviewed_count=lambda _cycle: 0,
        extract_cycle_candidates=lambda _cycle: [],
        extract_cycle_experiments=lambda _cycle: [],
        enrich_candidate_targeting=lambda candidate, _task: dict(candidate),
        build_research_task_run_result_summary=lambda result: dict(result),
        summarize_request_status_counts=lambda _requests: {},
        count_network_requests=lambda _requests: 0,
        count_real_requests=lambda _requests: 0,
        request_is_compatibility_failure=lambda _request: False,
        request_is_empty_200_response=lambda _request: False,
        normalize_external_request_status=lambda value: str(value or ""),
        summarize_autonomy_lifecycle=lambda lifecycle: dict(lifecycle or {}),
        autonomy_phase_order=("prepared", "generating", "failed"),
    )

    with pytest.raises(asyncio.CancelledError):
        await execute_autonomy_task(context)

    assert updates
    assert updates[-1]["args"] == (42,)
    assert updates[-1]["kwargs"]["status"] == "failed"
    assert updates[-1]["kwargs"]["error"] == "cancelled"
    assert updates[-1]["kwargs"]["result"]["status"] == "failed"
    assert updates[-1]["kwargs"]["result"]["error"] == "cancelled"
