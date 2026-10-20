"""Failures must not be reported as successes.

Each case here reproduces a specific way the agent used to turn a failure into a
success-shaped result, and asserts the behaviour instead of the source text.
"""

from __future__ import annotations

import asyncio
import types

from aiask_agent.factory_worker import _outcome, _failure_kind
from aiask_agent.response_payloads import responses_payload
from aiask_agent.routes.ws import _ws_authorized


class _FakeResult:
    response_id = "resp_1"
    content = "Agent run failed: boom"
    usage = {}
    session_id = "sess_1"
    run_id = "run_1"
    tool_calls = []
    audit_events = []
    events = []
    context_summary_id = None
    planner_steps = []
    subruns = []

    def __init__(self, status: str) -> None:
        self.status = status


def test_factory_outcome_maps_a_reported_failure_to_failed() -> None:
    """A runner that reports failure but exits 0 must not become "succeeded"."""
    target, raw_status, _flags, error = _outcome({"status": "failed", "error": "cycle failed"}, 0)
    assert target == "failed"
    assert raw_status == "failed"
    assert error == "cycle failed"
    # _failure_kind already knew how to classify this; _outcome could not reach it.
    assert _failure_kind(target, 0) == "runner_reported_failure"


def test_factory_outcome_still_succeeds_when_nothing_reports_failure() -> None:
    target, _raw, _flags, _error = _outcome({"status": "completed"}, 0)
    assert target == "succeeded"


def test_factory_outcome_keeps_nonzero_exit_as_failure() -> None:
    target, _raw, _flags, _error = _outcome({"status": "completed"}, 2)
    assert target == "failed"


def test_responses_payload_reports_a_failed_run_as_failed() -> None:
    payload = responses_payload(_FakeResult("failed"), model="gpt-test")
    assert payload["status"] == "failed"
    assert payload["error"]["code"] == "agent_run_failed"
    assert "boom" in payload["error"]["message"]


def test_responses_payload_reports_a_completed_run_as_completed() -> None:
    payload = responses_payload(_FakeResult("completed"), model="gpt-test")
    assert payload["status"] == "completed"
    assert "error" not in payload


def _ws(host: str) -> object:
    return types.SimpleNamespace(client=types.SimpleNamespace(host=host))


def test_websocket_authorization_fails_closed_for_remote_clients(monkeypatch) -> None:
    """Loopback is allowed; a remote client with no token configured is not."""
    monkeypatch.delenv("AIASK_AGENT_API_TOKEN", raising=False)
    assert _ws_authorized(_ws("127.0.0.1"), None) is True
    assert _ws_authorized(_ws("10.0.0.5"), None) is False
    assert _ws_authorized(_ws("10.0.0.5"), "anything") is False


def test_websocket_authorization_accepts_a_matching_remote_token(monkeypatch) -> None:
    monkeypatch.setenv("AIASK_AGENT_API_TOKEN", "s3cret")
    assert _ws_authorized(_ws("10.0.0.5"), "s3cret") is True
    assert _ws_authorized(_ws("10.0.0.5"), "wrong") is False


def test_cancelled_worker_task_propagates_cancellation() -> None:
    """Cancelling the listener must not be swallowed (shutdown used to hang)."""

    async def scenario() -> bool:
        heartbeat_started = asyncio.Event()

        async def heartbeat() -> None:
            heartbeat_started.set()
            await asyncio.sleep(3600)

        async def listener() -> None:
            hb_task = asyncio.create_task(heartbeat())
            await heartbeat_started.wait()
            try:
                await asyncio.sleep(3600)
            finally:
                hb_task.cancel()
                try:
                    await hb_task
                except asyncio.CancelledError:
                    if not hb_task.cancelled():
                        raise
                except Exception:
                    pass

        task = asyncio.create_task(listener())
        await heartbeat_started.wait()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            return True
        return False

    assert asyncio.run(scenario()) is True
