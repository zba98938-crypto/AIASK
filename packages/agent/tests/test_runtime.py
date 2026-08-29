from __future__ import annotations

import asyncio
import json
from typing import Any

from aiask_agent.model_client import ModelResponse
from aiask_agent.runtime import AgentRuntime
from aiask_agent.session_store import AgentSessionStore
from aiask_agent.tool_registry import AgentToolRegistry, aiask_envelope


class ToolCallingModel:
    def __init__(self) -> None:
        self.calls = 0
        self.saw_tool_result = False

    async def complete(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        model: str,
    ) -> ModelResponse:
        self.calls += 1
        if self.calls == 1:
            return ModelResponse(
                tool_calls=[
                    {
                        "id": "call_test",
                        "type": "function",
                        "function": {
                            "name": "agent_test_tool",
                            "arguments": json.dumps({"value": 3}),
                        },
                    }
                ],
                usage={"total_tokens": 1},
            )
        self.saw_tool_result = any(msg.get("role") == "tool" for msg in messages)
        return ModelResponse(content="done", usage={"total_tokens": 2})


class ClosableModel:
    def __init__(self) -> None:
        self.closed = False

    async def complete(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        model: str,
    ) -> ModelResponse:
        return ModelResponse(content="ok")

    async def aclose(self) -> None:
        self.closed = True


class FailingModel:
    async def complete(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        model: str,
    ) -> ModelResponse:
        raise RuntimeError(
            "model failed broker_token=runtime-failure-secret "
            "Authorization: Bearer runtime-failure-bearer-secret"
        )


def test_runtime_executes_tool_call_and_feeds_result_back(tmp_path) -> None:
    registry = AgentToolRegistry()

    async def handler(arguments: dict[str, Any]) -> dict[str, Any]:
        return aiask_envelope(
            True,
            data={"value": arguments["value"] + 1},
            error=None,
            tool_name="agent_test_tool",
            source_chain=["test"],
        )

    registry.register(
        "agent_test_tool",
        description="test tool",
        parameters={"type": "object", "properties": {"value": {"type": "integer"}}},
        handler=handler,
    )
    model = ToolCallingModel()
    runtime = AgentRuntime(
        model_client=model,
        tool_registry=registry,
        session_store=AgentSessionStore(tmp_path / "state.sqlite3"),
        max_iterations=3,
    )

    result = asyncio.run(runtime.run([{"role": "user", "content": "call tool"}], user_id="u1"))
    assert result.content == "done"
    assert model.saw_tool_result is True
    assert result.tool_calls[0]["name"] == "agent_test_tool"
    assert result.tool_calls[0]["result"]["data"]["value"] == 4
    saved = runtime.session_store.get_response(result.response_id)
    assert saved is not None
    assert saved["session_id"] == result.session_id


def test_runtime_failure_redacts_audit_events_and_saved_payload(tmp_path) -> None:
    runtime = AgentRuntime(
        model_client=FailingModel(),
        tool_registry=AgentToolRegistry(),
        session_store=AgentSessionStore(tmp_path / "state.sqlite3"),
        max_iterations=1,
        retry_attempts=1,
    )

    result = asyncio.run(runtime.run([{"role": "user", "content": "fail safely"}], user_id="u1"))

    saved = runtime.session_store.get_response(result.response_id)
    run = runtime.session_store.get_run(result.run_id)
    events = runtime.session_store.list_run_events(result.run_id)
    encoded = json.dumps(
        {
            "content": result.content,
            "audit_events": result.audit_events,
            "events": result.events,
            "saved": saved,
            "run": run,
            "listed_events": events,
        },
        ensure_ascii=False,
    )

    assert result.status == "failed"
    assert "runtime-failure-secret" not in encoded
    assert "runtime-failure-bearer-secret" not in encoded
    assert result.content == "Agent run failed: model failed broker_token=[redacted] Authorization: Bearer [redacted]"
    assert any(event["event"] == "operation_retry_classified" for event in result.audit_events)
    assert any(event["event"] == "run.failed" for event in result.audit_events)
    assert next(event for event in result.audit_events if event["event"] == "run.failed")["error"] == (
        "model failed broker_token=[redacted] Authorization: Bearer [redacted]"
    )


def test_runtime_close_releases_model_client(tmp_path) -> None:
    model = ClosableModel()
    runtime = AgentRuntime(
        model_client=model,
        tool_registry=AgentToolRegistry(),
        session_store=AgentSessionStore(tmp_path / "state.sqlite3"),
    )

    runtime.close()
    runtime.close()
    assert model.closed is True


def test_runtime_numeric_config_rejects_non_finite_values(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("AIASK_AGENT_MAX_ITERATIONS", "not-an-int")
    monkeypatch.setenv("AIASK_AGENT_MODEL_TIMEOUT", "nan")
    monkeypatch.setenv("AIASK_AGENT_TOOL_TIMEOUT", "inf")
    monkeypatch.setenv("AIASK_AGENT_RETRY_ATTEMPTS", "0")

    runtime = AgentRuntime(
        model_client=ClosableModel(),
        tool_registry=AgentToolRegistry(),
        session_store=AgentSessionStore(tmp_path / "state.sqlite3"),
    )

    assert runtime.max_iterations == 8
    assert runtime.model_timeout_seconds == 120.0
    assert runtime.tool_timeout_seconds == 120.0
    assert runtime.retry_attempts == 1
    runtime.close()


def test_runtime_explicit_numeric_config_is_bounded(tmp_path) -> None:
    runtime = AgentRuntime(
        model_client=ClosableModel(),
        tool_registry=AgentToolRegistry(),
        session_store=AgentSessionStore(tmp_path / "state.sqlite3"),
        max_iterations=0,
        model_timeout_seconds=float("nan"),
        tool_timeout_seconds=-5,
        retry_attempts=0,
    )

    assert runtime.max_iterations == 1
    assert runtime.model_timeout_seconds == 120.0
    assert runtime.tool_timeout_seconds == 1.0
    assert runtime.retry_attempts == 1
    runtime.close()
