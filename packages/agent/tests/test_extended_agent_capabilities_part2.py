from __future__ import annotations

import asyncio
import base64
import json
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from aiask_agent.context import ContextManager
from aiask_agent.control_context import internal_control_context
from aiask_agent.intents import ActionIntentStore
from aiask_agent.memory_providers import HERMES_EXTERNAL_MEMORY_PROVIDERS, MemoryProviderManager
from aiask_agent.mcp_client import MCPAggregator, MCPTokenStore, MCPOAuthRequired
from aiask_agent.model_client import ModelResponse, MockModelClient
from aiask_agent.native_capabilities import media_provider_catalog
from aiask_agent.process_registry import ProcessRegistry
from aiask_agent.runtime import AgentRuntime
from aiask_agent.session_store import AgentSessionStore
from aiask_agent.tool_registry import AgentToolRegistry, build_default_tool_registry
from aiask_agent.tools.policy import ToolPolicy, ToolPolicyEngine


def _request(method: str, url: str, payload: dict[str, Any] | None = None, headers: dict[str, str] | None = None):
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    req = Request(url, data=data, method=method, headers={"Content-Type": "application/json", **dict(headers or {})})
    with urlopen(req, timeout=5) as response:
        body = response.read().decode("utf-8")
        return response.status, dict(response.headers), body


def _request_maybe_error(
    method: str,
    url: str,
    payload: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
):
    try:
        return _request(method, url, payload, headers)
    except HTTPError as exc:
        return exc.code, dict(exc.headers), exc.read().decode("utf-8")


class FlakyModel:
    def __init__(self) -> None:
        self.calls = 0

    async def complete(self, *, messages: list[dict[str, Any]], tools: list[dict[str, Any]], model: str) -> ModelResponse:
        self.calls += 1
        if self.calls == 1:
            raise TimeoutError("temporary timeout")
        return ModelResponse(content="ok after retry", usage={"total_tokens": 1})


class CaptureMessagesModel:
    def __init__(self) -> None:
        self.messages: list[dict[str, Any]] = []

    async def complete(self, *, messages: list[dict[str, Any]], tools: list[dict[str, Any]], model: str) -> ModelResponse:
        self.messages = [dict(item) for item in messages]
        return ModelResponse(content="context captured", usage={"total_tokens": 1})


class SequenceCaptureModel:
    def __init__(self, responses: list[ModelResponse] | None = None) -> None:
        self.responses = list(responses or [])
        self.calls = 0
        self.messages_by_call: list[list[dict[str, Any]]] = []
        self.tools_by_call: list[list[str]] = []

    async def complete(self, *, messages: list[dict[str, Any]], tools: list[dict[str, Any]], model: str) -> ModelResponse:
        self.calls += 1
        self.messages_by_call.append([dict(item) for item in messages])
        self.tools_by_call.append([str((dict(item.get("function") or {})).get("name") or "") for item in tools])
        if self.responses:
            return self.responses.pop(0)
        return ModelResponse(content=f"captured {self.calls}", usage={"total_tokens": 1})


class ExecutePythonRpcModel:
    def __init__(self) -> None:
        self.calls = 0

    async def complete(self, *, messages: list[dict[str, Any]], tools: list[dict[str, Any]], model: str) -> ModelResponse:
        self.calls += 1
        if self.calls == 1:
            return ModelResponse(
                tool_calls=[
                    {
                        "id": "call_python_rpc",
                        "type": "function",
                        "function": {
                            "name": "agent_execute_python",
                            "arguments": json.dumps(
                                {
                                    "code": (
                                        "from aiask_tools import agent_file_write, agent_file_read\n"
                                        "agent_file_write(path='rpc-audit.txt', content='rpc audit ok')\n"
                                        "result = agent_file_read(path='rpc-audit.txt')\n"
                                        "print(result['data']['content'])\n"
                                    ),
                                    "cwd": ".",
                                }
                            ),
                        },
                    }
                ]
            )
        return ModelResponse(content="done", usage={"total_tokens": 1})


class DelegateTaskModel:
    def __init__(self) -> None:
        self.calls = 0

    async def complete(self, *, messages: list[dict[str, Any]], tools: list[dict[str, Any]], model: str) -> ModelResponse:
        self.calls += 1
        if self.calls == 1:
            return ModelResponse(
                tool_calls=[
                    {
                        "id": "call_delegate",
                        "type": "function",
                        "function": {
                            "name": "agent_delegate_task",
                            "arguments": json.dumps(
                                {
                                    "task": "summarize delegated context",
                                    "toolset": "finance_safe",
                                    "max_iterations": 1,
                                }
                            ),
                        },
                    }
                ]
            )
        if self.calls == 2:
            return ModelResponse(content="child delegated answer", usage={"total_tokens": 1})
        return ModelResponse(content="parent final answer", usage={"total_tokens": 1})


class SessionHandoffModel:
    def __init__(self) -> None:
        self.calls = 0

    async def complete(self, *, messages: list[dict[str, Any]], tools: list[dict[str, Any]], model: str) -> ModelResponse:
        self.calls += 1
        if self.calls == 1:
            return ModelResponse(
                tool_calls=[
                    {
                        "id": "call_handoff",
                        "type": "function",
                        "function": {
                            "name": "agent_session_handoff",
                            "arguments": json.dumps(
                                {
                                    "action": "request",
                                    "target": "risk_specialist",
                                    "reason": "needs risk review",
                                    "summary": "handoff with context snapshot",
                                    "metadata": {"requested_from": "test"},
                                }
                            ),
                        },
                    }
                ]
            )
        return ModelResponse(content="handoff requested", usage={"total_tokens": 1})


class CrossSessionHandoffModel:
    def __init__(self) -> None:
        self.calls = 0

    async def complete(self, *, messages: list[dict[str, Any]], tools: list[dict[str, Any]], model: str) -> ModelResponse:
        self.calls += 1
        if self.calls == 1:
            return ModelResponse(
                tool_calls=[
                    {
                        "id": "call_cross_handoff",
                        "type": "function",
                        "function": {
                            "name": "agent_session_handoff",
                            "arguments": json.dumps(
                                {
                                    "action": "request",
                                    "session_id": "victim-session",
                                    "user_id": "u2",
                                    "target": "risk_specialist",
                                    "reason": "attempted cross-session handoff",
                                }
                            ),
                        },
                    }
                ]
            )
        return ModelResponse(content="cross-session handoff blocked", usage={"total_tokens": 1})


class CrossSessionPlanningModel:
    def __init__(self) -> None:
        self.calls = 0

    async def complete(self, *, messages: list[dict[str, Any]], tools: list[dict[str, Any]], model: str) -> ModelResponse:
        self.calls += 1
        if self.calls == 1:
            return ModelResponse(
                tool_calls=[
                    {
                        "id": "call_cross_todo",
                        "type": "function",
                        "function": {
                            "name": "agent_todo_set",
                            "arguments": json.dumps(
                                {
                                    "session_id": "victim-session",
                                    "user_id": "u2",
                                    "items": [
                                        {
                                            "id": "leak",
                                            "content": "overwrite another session",
                                            "status": "completed",
                                        }
                                    ],
                                }
                            ),
                        },
                    },
                    {
                        "id": "call_cross_subgoal",
                        "type": "function",
                        "function": {
                            "name": "agent_subgoal",
                            "arguments": json.dumps(
                                {
                                    "action": "add",
                                    "session_id": "victim-session",
                                    "user_id": "u2",
                                    "title": "mutate another session",
                                    "criteria": ["should be blocked"],
                                }
                            ),
                        },
                    },
                ]
            )
        return ModelResponse(content="cross-session planning blocked", usage={"total_tokens": 1})


class RuntimeScopedPlanningModel:
    def __init__(self) -> None:
        self.calls = 0

    async def complete(self, *, messages: list[dict[str, Any]], tools: list[dict[str, Any]], model: str) -> ModelResponse:
        self.calls += 1
        if self.calls == 1:
            return ModelResponse(
                tool_calls=[
                    {
                        "id": "call_runtime_todo",
                        "type": "function",
                        "function": {
                            "name": "agent_todo_set",
                            "arguments": json.dumps(
                                {
                                    "items": [
                                        {
                                            "id": "current",
                                            "content": "write only current session plan",
                                            "status": "pending",
                                        }
                                    ],
                                }
                            ),
                        },
                    },
                    {
                        "id": "call_runtime_subgoal",
                        "type": "function",
                        "function": {
                            "name": "agent_subgoal",
                            "arguments": json.dumps(
                                {
                                    "action": "add",
                                    "title": "current session subgoal",
                                    "criteria": ["current session only"],
                                }
                            ),
                        },
                    },
                ]
            )
        return ModelResponse(content="current-session planning saved", usage={"total_tokens": 1})


class RuntimeScopedProcessModel:
    def __init__(self) -> None:
        self.calls = 0

    async def complete(self, *, messages: list[dict[str, Any]], tools: list[dict[str, Any]], model: str) -> ModelResponse:
        self.calls += 1
        if self.calls == 1:
            return ModelResponse(
                tool_calls=[
                    {
                        "id": "call_terminal_current",
                        "type": "function",
                        "function": {
                            "name": "agent_terminal",
                            "arguments": json.dumps({"command": "python -c \"print('current session')\"", "cwd": "."}),
                        },
                    },
                    {
                        "id": "call_process_list_current",
                        "type": "function",
                        "function": {
                            "name": "agent_process",
                            "arguments": json.dumps({"action": "list"}),
                        },
                    },
                ]
            )
        return ModelResponse(content="current-session process inspected", usage={"total_tokens": 1})


class CrossSessionProcessModel:
    def __init__(self, *, process_id: str) -> None:
        self.process_id = process_id
        self.calls = 0

    async def complete(self, *, messages: list[dict[str, Any]], tools: list[dict[str, Any]], model: str) -> ModelResponse:
        self.calls += 1
        if self.calls == 1:
            return ModelResponse(
                tool_calls=[
                    {
                        "id": "call_cross_process_list",
                        "type": "function",
                        "function": {
                            "name": "agent_process",
                            "arguments": json.dumps({"action": "list", "session_id": "victim-session"}),
                        },
                    },
                    {
                        "id": "call_cross_process_read",
                        "type": "function",
                        "function": {
                            "name": "agent_process",
                            "arguments": json.dumps({"action": "read", "process_id": self.process_id}),
                        },
                    },
                    {
                        "id": "call_cross_terminal",
                        "type": "function",
                        "function": {
                            "name": "agent_terminal",
                            "arguments": json.dumps({"command": "python -c \"print('wrong session')\"", "cwd": ".", "session_id": "victim-session"}),
                        },
                    },
                ]
            )
        return ModelResponse(content="cross-session process blocked", usage={"total_tokens": 1})


def test_session_handoff_request_links_runtime_context_snapshot(tmp_path) -> None:
    policy = ToolPolicyEngine(ToolPolicy("general_full", True, (str(tmp_path),)))
    store = AgentSessionStore(tmp_path / "state.sqlite3")
    registry = build_default_tool_registry(session_store=store, policy_engine=policy)
    runtime = AgentRuntime(
        model_client=SessionHandoffModel(),
        tool_registry=registry,
        session_store=store,
        max_iterations=2,
    )

    result = asyncio.run(runtime.run([{"role": "user", "content": "handoff to risk"}], user_id="u1"))

    assert result.context_snapshot_id
    handoffs = store.list_handoffs(session_id=result.session_id)
    assert len(handoffs) == 1
    handoff = handoffs[0]
    metadata = handoff["metadata"]
    assert handoff["user_id"] == "u1"
    assert metadata["handoff_kind"] == "ownership_transfer"
    assert metadata["source_session_id"] == result.session_id
    assert metadata["source_run_id"] == result.run_id
    assert metadata["source_tool_call_id"] == "call_handoff"
    assert metadata["context_snapshot_id"] == result.context_snapshot_id
    session = store.get_session(result.session_id)
    handoff_state = session["metadata"]["handoff_state"]
    assert handoff_state["status"] == "pending"
    assert handoff_state["handoff_id"] == handoff["handoff_id"]
    assert handoff_state["target"] == "risk_specialist"
    assert handoff_state["context_snapshot_id"] == result.context_snapshot_id


def test_session_handoff_cannot_target_other_session_from_runtime(tmp_path) -> None:
    policy = ToolPolicyEngine(ToolPolicy("general_full", True, (str(tmp_path),)))
    store = AgentSessionStore(tmp_path / "state.sqlite3")
    store.create_session(session_id="victim-session", user_id="u2")
    registry = build_default_tool_registry(session_store=store, policy_engine=policy)
    runtime = AgentRuntime(
        model_client=CrossSessionHandoffModel(),
        tool_registry=registry,
        session_store=store,
        max_iterations=2,
    )

    result = asyncio.run(
        runtime.run(
            [{"role": "user", "content": "handoff but stay in scope"}],
            session_id="active-session",
            user_id="u1",
        )
    )

    assert result.tool_calls[0]["name"] == "agent_session_handoff"
    tool_result = result.tool_calls[0]["result"]
    assert tool_result["success"] is False
    assert tool_result["error_code"] == "SESSION_HANDOFF_SCOPE_VIOLATION"
    assert store.list_handoffs(session_id="victim-session") == []
    victim = store.get_session("victim-session")
    assert "handoff_state" not in dict(victim.get("metadata") or {})


def test_planning_tools_cannot_target_other_session_from_runtime(tmp_path) -> None:
    policy = ToolPolicyEngine(ToolPolicy("general_full", True, (str(tmp_path),)))
    store = AgentSessionStore(tmp_path / "state.sqlite3")
    store.create_session(session_id="victim-session", user_id="u2")
    registry = build_default_tool_registry(session_store=store, policy_engine=policy)
    runtime = AgentRuntime(
        model_client=CrossSessionPlanningModel(),
        tool_registry=registry,
        session_store=store,
        max_iterations=2,
    )

    result = asyncio.run(
        runtime.run(
            [{"role": "user", "content": "plan but stay in scope"}],
            session_id="active-session",
            user_id="u1",
        )
    )

    tool_results = {item["name"]: item["result"] for item in result.tool_calls}
    assert tool_results["agent_todo_set"]["success"] is False
    assert tool_results["agent_todo_set"]["error_code"] == "PLANNING_SESSION_SCOPE_VIOLATION"
    assert tool_results["agent_subgoal"]["success"] is False
    assert tool_results["agent_subgoal"]["error_code"] == "PLANNING_SESSION_SCOPE_VIOLATION"
    victim_todos = asyncio.run(registry.call_tool("agent_todo_list", {"session_id": "victim-session"}))
    assert victim_todos["data"]["items"] == []
    assert store.list_subgoals(session_id="victim-session") == []


def test_planning_tools_default_to_active_runtime_session(tmp_path) -> None:
    policy = ToolPolicyEngine(ToolPolicy("general_full", True, (str(tmp_path),)))
    store = AgentSessionStore(tmp_path / "state.sqlite3")
    registry = build_default_tool_registry(session_store=store, policy_engine=policy)
    runtime = AgentRuntime(
        model_client=RuntimeScopedPlanningModel(),
        tool_registry=registry,
        session_store=store,
        max_iterations=2,
    )

    result = asyncio.run(
        runtime.run(
            [{"role": "user", "content": "save this plan"}],
            session_id="active-session",
            user_id="u1",
        )
    )

    tool_results = {item["name"]: item["result"] for item in result.tool_calls}
    assert tool_results["agent_todo_set"]["success"] is True
    assert tool_results["agent_todo_set"]["data"]["session_id"] == "active-session"
    assert tool_results["agent_subgoal"]["success"] is True
    assert tool_results["agent_subgoal"]["data"]["session_id"] == "active-session"
    current_todos = asyncio.run(registry.call_tool("agent_todo_list", {"session_id": "active-session"}))
    assert current_todos["data"]["items"][0]["content"] == "write only current session plan"
    current_subgoals = store.list_subgoals(session_id="active-session")
    assert current_subgoals[0]["title"] == "current session subgoal"


def test_process_tools_default_to_active_runtime_session(tmp_path) -> None:
    policy = ToolPolicyEngine(ToolPolicy("general_full", True, (str(tmp_path),)))
    store = AgentSessionStore(tmp_path / "state.sqlite3")
    intent_store = ActionIntentStore(tmp_path / "intents.sqlite3")
    registry = build_default_tool_registry(intent_store, session_store=store, policy_engine=policy)
    runtime = AgentRuntime(
        model_client=RuntimeScopedProcessModel(),
        tool_registry=registry,
        session_store=store,
        max_iterations=2,
    )

    result = asyncio.run(
        runtime.run(
            [{"role": "user", "content": "run a scoped command"}],
            session_id="active-session",
            user_id="u1",
        )
    )

    tool_results = {item["name"]: item["result"] for item in result.tool_calls}
    assert tool_results["agent_terminal"]["success"] is False
    assert tool_results["agent_terminal"]["error_code"] == "ACTION_INTENT_REQUIRED"
    terminal_intent = tool_results["agent_terminal"]["data"]["intent"]
    assert terminal_intent["action"] == "terminal.run"
    stored_intent = intent_store.get(terminal_intent["intent_id"])
    assert stored_intent is not None
    assert stored_intent["params"]["session_id"] == "active-session"
    assert tool_results["agent_process"]["success"] is True
    assert all(item["session_id"] == "active-session" for item in tool_results["agent_process"]["data"]["items"])


def test_process_tools_cannot_target_other_session_from_runtime(tmp_path) -> None:
    policy = ToolPolicyEngine(ToolPolicy("general_full", True, (str(tmp_path),)))
    store = AgentSessionStore(tmp_path / "state.sqlite3")
    process = ProcessRegistry(store.path).record(
        command="python -c \"print('victim')\"",
        cwd=str(tmp_path),
        status="completed",
        returncode=0,
        stdout="victim\n",
        stderr="",
        session_id="victim-session",
        metadata={"backend": "local", "aiask_managed": True},
    )
    registry = build_default_tool_registry(session_store=store, policy_engine=policy)
    runtime = AgentRuntime(
        model_client=CrossSessionProcessModel(process_id=str(process["process_id"])),
        tool_registry=registry,
        session_store=store,
        max_iterations=2,
    )

    result = asyncio.run(
        runtime.run(
            [{"role": "user", "content": "inspect process but stay in scope"}],
            session_id="active-session",
            user_id="u1",
        )
    )

    tool_results = {item["id"]: item["result"] for item in result.tool_calls}
    assert tool_results["call_cross_process_list"]["success"] is False
    assert tool_results["call_cross_process_list"]["error_code"] == "PROCESS_SESSION_SCOPE_VIOLATION"
    assert tool_results["call_cross_process_read"]["success"] is False
    assert tool_results["call_cross_process_read"]["error_code"] == "PROCESS_SESSION_SCOPE_VIOLATION"
    assert tool_results["call_cross_terminal"]["success"] is False
    assert tool_results["call_cross_terminal"]["error_code"] == "TERMINAL_SESSION_SCOPE_VIOLATION"
    assert ProcessRegistry(store.path).get(str(process["process_id"]))["status"] == "completed"


def test_runtime_consumes_pending_session_handoff_state_on_next_turn(tmp_path) -> None:
    store = AgentSessionStore(tmp_path / "state.sqlite3")
    session_id = store.create_session(session_id="handoff-session", user_id="u1")
    pending = store.set_session_handoff_state(
        session_id,
        status="pending",
        handoff_id="handoff_test",
        target="risk_specialist",
        source_run_id="run_source",
        source_tool_call_id="call_source",
        context_snapshot_id="ctxsnap_source",
        summary="Continue with risk review.",
        reason="risk escalation",
    )
    assert pending["status"] == "pending"
    model = SequenceCaptureModel()
    runtime = AgentRuntime(
        model_client=model,
        tool_registry=AgentToolRegistry(),
        session_store=store,
        max_iterations=1,
    )

    result = asyncio.run(
        runtime.run(
            [{"role": "user", "content": "continue after handoff"}],
            session_id=session_id,
            user_id="u1",
        )
    )

    events = store.list_run_events(result.run_id)
    activated = [item for item in events if item["event"] == "handoff.activated"]
    assert len(activated) == 1
    assert activated[0]["data"]["target"] == "risk_specialist"
    assert activated[0]["data"]["context_snapshot_id"] == "ctxsnap_source"
    policy_events = [item for item in events if item["event"] == "handoff.policy_applied"]
    assert len(policy_events) == 1
    assert policy_events[0]["data"]["policy_id"] == "risk_specialist"
    assert policy_events[0]["data"]["effective_toolset"] == "finance_safe"
    session = store.get_session(session_id)
    state = session["metadata"]["handoff_state"]
    assert state["status"] == "active"
    assert state["active_run_id"] == result.run_id
    assert session["metadata"]["active_agent"] == "risk_specialist"
    assert session["metadata"]["active_context_snapshot_id"] == "ctxsnap_source"
    handoff_messages = [
        item
        for item in model.messages_by_call[0]
        if item.get("name") == "handoff_state"
    ]
    assert handoff_messages
    assert "target=risk_specialist" in handoff_messages[0]["content"]
    assert "context_snapshot_id=ctxsnap_source" in handoff_messages[0]["content"]
    policy_messages = [
        item
        for item in model.messages_by_call[0]
        if item.get("name") == "handoff_specialist_policy"
    ]
    assert policy_messages
    assert "policy_id=risk_specialist" in policy_messages[0]["content"]
    assert "role=Risk specialist" in policy_messages[0]["content"]


def test_handoff_specialist_policy_filters_advertised_tools(tmp_path) -> None:
    policy = ToolPolicyEngine(ToolPolicy("general_full", True, (str(tmp_path),)))
    store = AgentSessionStore(tmp_path / "state.sqlite3")
    registry = build_default_tool_registry(session_store=store, policy_engine=policy)
    session_id = store.create_session(session_id="handoff-filter-session", user_id="u1")
    store.set_session_handoff_state(
        session_id,
        status="pending",
        handoff_id="handoff_filter",
        target="risk_specialist",
        context_snapshot_id="ctxsnap_filter",
        summary="Risk review only.",
    )
    model = SequenceCaptureModel()
    runtime = AgentRuntime(
        model_client=model,
        tool_registry=registry,
        session_store=store,
        max_iterations=1,
    )

    result = asyncio.run(
        runtime.run(
            [{"role": "user", "content": "continue handoff"}],
            session_id=session_id,
            user_id="u1",
        )
    )

    advertised = model.tools_by_call[0]
    assert "agent_portfolio_risk" in advertised
    assert "agent_file_read" not in advertised
    events = store.list_run_events(result.run_id)
    policy_event = next(item for item in events if item["event"] == "handoff.policy_applied")
    assert policy_event["data"]["filtered"] is True
    assert "agent_portfolio_risk" in policy_event["data"]["advertised_tools"]
    snapshot = store.get_context_snapshot(result.context_snapshot_id)
    assert snapshot["metadata"]["handoff_policy"]["policy_id"] == "risk_specialist"
    assert snapshot["metadata"]["handoff_policy"]["advertised_tool_count"] == len(advertised)


def test_runtime_resumes_active_handoff_state_on_later_turn(tmp_path) -> None:
    store = AgentSessionStore(tmp_path / "state.sqlite3")
    session_id = store.create_session(session_id="handoff-resume-session", user_id="u1")
    store.set_session_handoff_state(
        session_id,
        status="pending",
        handoff_id="handoff_resume",
        target="risk_specialist",
        context_snapshot_id="ctxsnap_resume",
        summary="Resume risk review.",
    )
    model = SequenceCaptureModel()
    runtime = AgentRuntime(
        model_client=model,
        tool_registry=AgentToolRegistry(),
        session_store=store,
        max_iterations=1,
    )
    first = asyncio.run(
        runtime.run(
            [{"role": "user", "content": "activate handoff"}],
            session_id=session_id,
            user_id="u1",
        )
    )
    second = asyncio.run(
        runtime.run(
            [{"role": "user", "content": "continue later"}],
            session_id=session_id,
            user_id="u1",
        )
    )

    assert any(item["event"] == "handoff.activated" for item in store.list_run_events(first.run_id))
    second_events = store.list_run_events(second.run_id)
    assert any(item["event"] == "handoff.resumed" for item in second_events)
    assert not any(item["event"] == "handoff.activated" for item in second_events)
    resumed_messages = [
        item
        for item in model.messages_by_call[1]
        if item.get("name") == "handoff_state"
    ]
    assert resumed_messages
    assert "handoff is resumed" in resumed_messages[0]["content"]
    assert "context_snapshot_id=ctxsnap_resume" in resumed_messages[0]["content"]
    assert any(item.get("name") == "handoff_specialist_policy" for item in model.messages_by_call[1])
    snapshot = store.get_context_snapshot(second.context_snapshot_id)
    assert snapshot["metadata"]["handoff_policy"]["context_snapshot_id"] == "ctxsnap_resume"


def test_native_hermes_class_tools_are_aiask_owned(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("AIASK_AGENT_HOME", str(tmp_path / "home"))
    policy = ToolPolicyEngine(ToolPolicy("general_full", True, (str(tmp_path),)))
    intent_store = ActionIntentStore(tmp_path / "intents.sqlite3")
    registry = build_default_tool_registry(
        intent_store,
        session_store=AgentSessionStore(tmp_path / "state.sqlite3"),
        policy_engine=policy,
    )

    clarified = asyncio.run(
        registry.call_tool(
            "agent_clarify",
            {"question": "Which universe?", "options": ["A-shares", "HK"]},
        )
    )
    assert clarified["success"] is True
    assert clarified["data"]["requires_user_input"] is True

    saved_skill = asyncio.run(
        registry.call_tool(
            "agent_skill_save",
            {"name": "risk-review", "description": "Risk review workflow", "content": "# Risk Review\nCheck drawdown."},
        )
    )
    assert saved_skill["success"] is False
    assert saved_skill["error_code"] == "ACTION_INTENT_REQUIRED"
    assert saved_skill["data"]["intent"]["action"] == "skill.save"
    listed_skills = asyncio.run(registry.call_tool("agent_skill_list", {}))
    assert listed_skills["data"]["skills"] == []

    plugin = asyncio.run(
        registry.call_tool(
            "agent_plugin_set_enabled",
            {"name": "audit-export", "enabled": True, "description": "Audit export plugin"},
        )
    )
    assert plugin["success"] is False
    assert plugin["error_code"] == "ACTION_INTENT_REQUIRED"
    assert plugin["data"]["intent"]["action"] == "plugin.set_enabled"
    plugins = asyncio.run(registry.call_tool("agent_plugin_list", {}))
    assert plugins["data"]["plugins"] == []

    todos = asyncio.run(
        registry.call_tool(
            "agent_todo_set",
            {"session_id": "s1", "items": [{"id": "1", "content": "collect evidence", "status": "pending"}]},
        )
    )
    assert todos["success"] is True
    listed_todos = asyncio.run(registry.call_tool("agent_todo_list", {"session_id": "s1"}))
    assert listed_todos["data"]["items"][0]["content"] == "collect evidence"


def test_search_and_mcp_financial_allowlist(tmp_path) -> None:
    state = AgentSessionStore(tmp_path / "state.sqlite3")
    sid = state.create_session(user_id="u1")
    state.append_message(sid, {"role": "user", "content": "maotai margin quality"})
    assert state.search(query="maotai", user_id="u1")

    config_path = tmp_path / "mcp_servers.json"
    config_path.write_text(
        json.dumps(
            {
                "servers": [
                    {
                        "name": "finance-demo",
                        "domain": "financial",
                        "command": "python",
                        "tools": [
                            {"name": "quote", "description": "quote tool", "parameters": {"type": "object"}},
                            {"name": "terminal", "description": "shell tool"},
                            {"name": "available_tools", "description": "legacy catalog"},
                            {"name": "get_tool_contract", "description": "legacy contract lookup"},
                            {"name": "strategy_manager", "description": "direct strategy manager"},
                        ],
                    },
                    {
                        "name": "general-demo",
                        "domain": "general",
                        "command": "python",
                        "tools": [{"name": "terminal", "description": "terminal"}],
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    aggregator = MCPAggregator(config_path)
    tools = aggregator.financial_tools()
    assert [item["wrapped_name"] for item in tools] == ["agent_mcp_finance_demo_quote"]
    assert {item["name"] for item in tools}.isdisjoint({"available_tools", "get_tool_contract", "strategy_manager"})
    assert aggregator.servers_summary()[0]["resources_enabled"] is False


def test_mcp_remote_transports_and_oauth_token_store(tmp_path, monkeypatch) -> None:
    config_path = tmp_path / "mcp_servers.json"
    config_path.write_text(
        json.dumps(
            {
                "servers": [
                    {
                        "name": "sse-fin",
                        "domain": "financial",
                        "transport": "sse",
                        "url": "https://mcp.example.test/sse",
                        "auth": "oauth",
                        "oauth": {"authorization_url": "https://auth.example.test/authorize", "client_id": "client-1", "scope": "quotes"},
                        "tools": [{"name": "quote", "description": "quote", "parameters": {"type": "object"}}],
                    },
                    {
                        "name": "http-fin",
                        "domain": "financial",
                        "transport": "streamable_http",
                        "url": "https://mcp.example.test/mcp",
                        "tools": [{"name": "risk", "description": "risk", "parameters": {"type": "object"}}],
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("AIASK_AGENT_MCP_TOKEN_DIR", str(tmp_path / "tokens"))

    calls: list[dict[str, Any]] = []

    class FakeSession:
        def __init__(self, *_: Any) -> None:
            pass

        async def __aenter__(self) -> "FakeSession":
            return self

        async def __aexit__(self, *_: Any) -> None:
            return None

        async def initialize(self) -> None:
            return None

        async def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
            return {"called": name, "arguments": arguments or {}}

        async def list_tools(self) -> SimpleNamespace:
            return SimpleNamespace(tools=[{"name": "risk"}])

        async def list_resources(self) -> SimpleNamespace:
            return SimpleNamespace(resources=[{"uri": "aiask://risk"}])

        async def read_resource(self, uri: str) -> dict[str, Any]:
            return {"uri": uri, "text": "resource text"}

        async def list_prompts(self) -> SimpleNamespace:
            return SimpleNamespace(prompts=[{"name": "review"}])

        async def get_prompt(self, name: str, arguments: dict[str, str] | None = None) -> dict[str, Any]:
            return {"name": name, "arguments": arguments or {}, "messages": []}

    @asynccontextmanager
    async def fake_sse_client(url: str, **kwargs: Any):
        calls.append({"transport": "sse", "url": url, "auth": kwargs.get("auth")})
        yield object(), object()

    @asynccontextmanager
    async def fake_streamable_client(url: str, **kwargs: Any):
        calls.append({"transport": "streamable_http", "url": url, "auth": kwargs.get("auth")})
        yield object(), object(), lambda: "session-1"

    import mcp
    import mcp.client.sse
    import mcp.client.streamable_http

    monkeypatch.setattr(mcp, "ClientSession", FakeSession)
    monkeypatch.setattr(mcp.client.sse, "sse_client", fake_sse_client)
    monkeypatch.setattr(mcp.client.streamable_http, "streamablehttp_client", fake_streamable_client)

    aggregator = MCPAggregator(config_path)
    start = aggregator.oauth_start("sse-fin")
    assert start["status"] == "oauth_required"
    assert "client_id=client-1" in start["authorization_url"]
    assert MCPTokenStore("sse-fin").summary(configured=True)["token_available"] is False

    with pytest.raises(MCPOAuthRequired):
        asyncio.run(aggregator.call("agent_mcp_sse_fin_quote", {"symbol": "600519"}))

    stored = aggregator.oauth_callback("sse-fin", {"access_token": "token-1", "scope": "quotes"})
    assert stored["token_available"] is True
    called = asyncio.run(aggregator.call("agent_mcp_sse_fin_quote", {"symbol": "600519"}))
    assert called["called"] == "quote"
    assert calls[-1]["transport"] == "sse"
    assert calls[-1]["auth"] is not None

    discovered = asyncio.run(aggregator.discover("http-fin"))
    assert discovered["transport"] == "streamable_http"
    assert discovered["resources"][0]["uri"] == "aiask://risk"
    resource = asyncio.run(aggregator.read_resource("http-fin", "aiask://risk"))
    assert resource["result"]["text"] == "resource text"
    prompt = asyncio.run(aggregator.get_prompt("http-fin", "review", {"ticker": "600519"}))
    assert prompt["result"]["arguments"] == {"ticker": "600519"}


def test_provider_backed_vision_tts_and_stt_are_real_calls(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("AIASK_AGENT_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("AIASK_AGENT_VISION_MODEL", "vision-model")
    calls: list[dict[str, Any]] = []

    class FakeResponses:
        async def create(self, **kwargs: Any) -> SimpleNamespace:
            calls.append({"api": "responses.create", **kwargs})
            return SimpleNamespace(output_text="visible chart evidence")

    class FakeSpeech:
        async def create(self, **kwargs: Any) -> SimpleNamespace:
            calls.append({"api": "audio.speech.create", **kwargs})
            return SimpleNamespace(content=b"audio-bytes")

    class FakeTranscriptions:
        async def create(self, **kwargs: Any) -> SimpleNamespace:
            calls.append({"api": "audio.transcriptions.create", **{k: v for k, v in kwargs.items() if k != "file"}})
            return SimpleNamespace(text="transcribed words")

    class FakeAudio:
        def __init__(self) -> None:
            self.speech = FakeSpeech()
            self.transcriptions = FakeTranscriptions()

    class FakeOpenAI:
        def __init__(self, **_: Any) -> None:
            self.responses = FakeResponses()
            self.audio = FakeAudio()

    import openai

    monkeypatch.setattr(openai, "AsyncOpenAI", FakeOpenAI)
    registry = build_default_tool_registry(
        session_store=AgentSessionStore(tmp_path / "state.sqlite3"),
        policy_engine=ToolPolicyEngine(ToolPolicy("general_full", True, (str(tmp_path),))),
    )
    image_path = tmp_path / "chart.png"
    image_path.write_bytes(base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/p9sAAAAASUVORK5CYII="))
    audio_path = tmp_path / "speech.wav"
    audio_path.write_bytes(b"RIFF0000WAVEfmt ")

    async def scenario() -> None:
        vision = await registry.call_tool("agent_vision_analyze", {"image_path": str(image_path), "prompt": "read chart"})
        assert vision["success"] is True
        assert vision["data"]["analysis"] == "visible chart evidence"
        vision_input = calls[-1]["input"][0]["content"]
        assert vision_input[1]["image_url"].startswith("data:image/png;base64,")

        tts = await registry.call_tool(
            "agent_text_to_speech",
            {"text": "hello", "voice": "alloy", "format": "mp3", "speed": 1.2},
        )
        assert tts["success"] is True
        assert Path(tts["data"]["path"]).read_bytes() == b"audio-bytes"
        assert calls[-1]["api"] == "audio.speech.create"
        assert calls[-1]["speed"] == 1.2

        stt = await registry.call_tool(
            "agent_transcribe_audio",
            {"audio_path": str(audio_path), "language": "en", "prompt": "finance call"},
        )
        assert stt["success"] is True
        assert stt["data"]["text"] == "transcribed words"
        assert calls[-1]["api"] == "audio.transcriptions.create"
        assert calls[-1]["language"] == "en"

    asyncio.run(scenario())


def test_media_provider_catalog_reports_multimodal_readiness(tmp_path) -> None:
    catalog = media_provider_catalog(
        {
            "OPENAI_API_KEY": "secret-openai",
            "AIASK_AGENT_VISION_MODEL": "vision-model",
            "AIASK_VIDEO_API_URL": "https://video.example/v1",
            "AIASK_VIDEO_API_KEY": "secret-video",
            "AIASK_VIDEO_MODEL": "video-model",
        }
    )
    providers = {item["name"]: item for item in catalog["providers"]}
    assert {"openai_vision", "openai_image", "aiask_video_endpoint", "openai_tts", "openai_stt", "edge_tts", "iflytek_voice", "local_whisper"} <= set(providers)
    assert providers["openai_vision"]["configured"] is True
    assert providers["aiask_video_endpoint"]["configured"] is True
    assert providers["iflytek_voice"]["configured"] is False
    assert catalog["by_modality"]["tts"] >= 2
    assert "secret-openai" not in json.dumps(catalog)
    assert "secret-video" not in json.dumps(catalog)

    registry = build_default_tool_registry(
        session_store=AgentSessionStore(tmp_path / "state.sqlite3"),
        policy_engine=ToolPolicyEngine(ToolPolicy("general_full", True, (str(tmp_path),))),
    )
    assert "agent_media_provider_catalog" in registry.names()
    tool_payload = asyncio.run(registry.call_tool("agent_media_provider_catalog", {}))
    assert tool_payload["success"] is True
    assert tool_payload["data"]["object"] == "aiask.media_provider_catalog"


def test_provider_tools_report_unconfigured_without_credentials(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("AIASK_AGENT_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("AIASK_AGENT_VISION_MODEL", raising=False)
    registry = build_default_tool_registry(
        session_store=AgentSessionStore(tmp_path / "state.sqlite3"),
        policy_engine=ToolPolicyEngine(ToolPolicy("general_full", True, (str(tmp_path),))),
    )
    image_path = tmp_path / "chart.png"
    image_path.write_bytes(b"not really an image")

    async def scenario() -> None:
        vision = await registry.call_tool("agent_vision_analyze", {"image_path": str(image_path)})
        assert vision["success"] is False
        assert vision["data"]["configured"] is False
        tts = await registry.call_tool("agent_text_to_speech", {"text": "hello"})
        assert tts["success"] is False
        assert tts["data"]["configured"] is False
        stt = await registry.call_tool("agent_transcribe_audio", {"audio_path": str(tmp_path / "missing.wav")})
        assert stt["success"] is False
        assert stt["data"]["configured"] is False

    asyncio.run(scenario())


def test_http_sse_run_events_toolsets_and_jobs(tmp_path, monkeypatch) -> None:
    from aiask_agent.server import build_server

    monkeypatch.setenv("AIASK_AGENT_CONTROL_TOKEN", "secret")
    store = AgentSessionStore(tmp_path / "state.sqlite3")
    runtime = AgentRuntime(
        model_client=MockModelClient(),
        tool_registry=AgentToolRegistry(),
        session_store=store,
        max_iterations=1,
    )
    server = build_server("127.0.0.1", 0, runtime=runtime)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        status, _, body = _request("GET", f"{base_url}/v1/toolsets")
        assert status == 200
        assert json.loads(body)["active"] == "finance_safe"

        status, headers, body = _request(
            "POST",
            f"{base_url}/v1/chat/completions",
            {"stream": True, "messages": [{"role": "user", "content": "hello"}]},
        )
        assert status == 200
        assert headers["Content-Type"].startswith("text/event-stream")
        assert "[DONE]" in body

        first_json_line = next(line for line in body.splitlines() if line.startswith("data: {"))
        response_id = json.loads(first_json_line.removeprefix("data: "))["id"]
        run_id = store.get_response(response_id)["run_id"]
        status, headers, events_body = _request("GET", f"{base_url}/v1/runs/{run_id}/events")
        assert status == 200
        assert headers["Content-Type"].startswith("text/event-stream")
        assert "run.completed" in events_body

        status, _, denied_body = _request_maybe_error(
            "POST",
            f"{base_url}/v1/jobs",
            {"name": "blocked", "prompt": "hello job", "interval_seconds": 3600},
        )
        assert status == 401
        assert "control" in denied_body.lower()

        status, _, job_body = _request(
            "POST",
            f"{base_url}/v1/jobs",
            {"name": "smoke", "prompt": "hello job", "interval_seconds": 3600},
            headers={"Authorization": "Bearer secret"},
        )
        assert status == 201
        job_id = json.loads(job_body)["job_id"]
        status, _, list_body = _request("GET", f"{base_url}/v1/jobs")
        assert status == 200
        assert job_id in list_body

        status, _, patch_denied_body = _request_maybe_error(
            "PATCH",
            f"{base_url}/v1/jobs/{job_id}",
            {"enabled": False},
        )
        assert status == 401
        assert "control" in patch_denied_body.lower()

        status, _, patch_body = _request(
            "PATCH",
            f"{base_url}/v1/jobs/{job_id}",
            {"enabled": True},
            headers={"Authorization": "Bearer secret"},
        )
        assert status == 200
        assert json.loads(patch_body)["enabled"] is True

        status, _, run_denied_body = _request_maybe_error("POST", f"{base_url}/v1/jobs/{job_id}/run", {})
        assert status == 401
        assert "control" in run_denied_body.lower()

        status, _, run_body = _request(
            "POST",
            f"{base_url}/v1/jobs/{job_id}/run",
            {},
            headers={"Authorization": "Bearer secret"},
        )
        assert status == 200
        assert json.loads(run_body)["success"] is True

        status, _, runs_body = _request("GET", f"{base_url}/v1/jobs/{job_id}/runs?limit=20")
        assert status == 200
        assert json.loads(runs_body)["job_id"] == job_id

        status, _, delete_denied_body = _request_maybe_error("DELETE", f"{base_url}/v1/jobs/{job_id}")
        assert status == 401
        assert "control" in delete_denied_body.lower()

        status, _, delete_body = _request(
            "DELETE",
            f"{base_url}/v1/jobs/{job_id}",
            headers={"Authorization": "Bearer secret"},
        )
        assert status == 200
        assert json.loads(delete_body)["deleted"] is True
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
