from __future__ import annotations

import asyncio
import base64
import json
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.request import Request, urlopen

import pytest

from aiask_agent.context import ContextManager
from aiask_agent.control_context import internal_control_context
from aiask_agent.memory_providers import HERMES_EXTERNAL_MEMORY_PROVIDERS, MemoryProviderManager
from aiask_agent.mcp_client import MCPAggregator, MCPTokenStore, MCPOAuthRequired
from aiask_agent.model_client import ModelResponse, MockModelClient
from aiask_agent.native_capabilities import media_provider_catalog
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
                                        "from aiask_tools import agent_file_write\n"
                                        "result = agent_file_write(path='rpc-audit.txt', content='rpc audit ok')\n"
                                        "print(result['error_code'])\n"
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


class CrossUserDelegateTaskModel:
    def __init__(self) -> None:
        self.calls = 0

    async def complete(self, *, messages: list[dict[str, Any]], tools: list[dict[str, Any]], model: str) -> ModelResponse:
        self.calls += 1
        if self.calls == 1:
            return ModelResponse(
                tool_calls=[
                    {
                        "id": "call_cross_user_delegate",
                        "type": "function",
                        "function": {
                            "name": "agent_delegate_task",
                            "arguments": json.dumps(
                                {
                                    "task": "run delegated work as another user",
                                    "toolset": "finance_safe",
                                    "max_iterations": 1,
                                    "user_id": "u2",
                                }
                            ),
                        },
                    }
                ]
            )
        return ModelResponse(content="cross-user delegate blocked", usage={"total_tokens": 1})


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


def test_toolsets_gate_general_tools(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("AIASK_AGENT_ENABLE_GENERAL_TOOLS", raising=False)
    monkeypatch.setenv("AIASK_AGENT_WORKSPACE_ROOTS", str(tmp_path))
    finance = build_default_tool_registry(
        session_store=AgentSessionStore(tmp_path / "finance.sqlite3"),
        policy_engine=ToolPolicyEngine(ToolPolicy("finance_safe", False, (str(tmp_path),))),
    )
    assert "agent_terminal" not in finance.names()
    assert "agent_file_read" not in finance.names()

    general = build_default_tool_registry(
        session_store=AgentSessionStore(tmp_path / "general.sqlite3"),
        policy_engine=ToolPolicyEngine(ToolPolicy("general_full", True, (str(tmp_path),))),
    )
    names = set(general.names())
    assert {
        "agent_terminal",
        "agent_file_read",
        "agent_execute_python",
        "agent_browser_snapshot",
        "agent_web_search",
        "agent_web_extract",
        "agent_skill_list",
        "agent_plugin_list",
        "agent_clarify",
        "agent_todo_set",
        "agent_vision_analyze",
        "agent_image_generate",
        "agent_text_to_speech",
        "agent_message_send",
        "agent_model_manage",
        "agent_memory_manage",
        "agent_acp_manage",
        "agent_security_scan",
        "agent_skill_pack_manage",
    } <= names


def test_general_file_terminal_and_code_tools_are_workspace_scoped(tmp_path) -> None:
    registry = build_default_tool_registry(
        session_store=AgentSessionStore(tmp_path / "state.sqlite3"),
        policy_engine=ToolPolicyEngine(ToolPolicy("general_full", True, (str(tmp_path),))),
    )
    pending = asyncio.run(
        registry.call_tool(
            "agent_file_write",
            {"path": "notes/demo.txt", "content": "alpha beta", "create_parent_dirs": True},
        )
    )
    assert pending["success"] is False
    assert pending["error_code"] == "ACTION_INTENT_REQUIRED"
    assert pending["data"]["intent"]["action"] == "file.write"
    assert not (tmp_path / "notes" / "demo.txt").exists()
    with internal_control_context("test.file_write"):
        written = asyncio.run(
            registry.call_tool(
                "agent_file_write",
                {"path": "notes/demo.txt", "content": "alpha beta", "create_parent_dirs": True},
            )
        )
    assert written["success"] is True
    read = asyncio.run(registry.call_tool("agent_file_read", {"path": "notes/demo.txt"}))
    assert read["data"]["content"] == "alpha beta"
    terminal = asyncio.run(registry.call_tool("agent_terminal", {"command": "pwd", "cwd": "."}))
    assert terminal["success"] is False
    assert terminal["error_code"] == "ACTION_INTENT_REQUIRED"
    assert terminal["data"]["intent"]["action"] == "terminal.run"
    with internal_control_context("test.terminal_pwd"):
        terminal = asyncio.run(registry.call_tool("agent_terminal", {"command": "pwd", "cwd": "."}))
    assert terminal["success"] is True
    assert terminal["data"]["returncode"] == 0
    code = asyncio.run(registry.call_tool("agent_execute_python", {"code": "print(2 + 3)"}))
    assert code["success"] is False
    assert code["error_code"] == "ACTION_INTENT_REQUIRED"
    assert code["data"]["intent"]["action"] == "code.execute_python"
    with internal_control_context("test.execute_python"):
        code = asyncio.run(registry.call_tool("agent_execute_python", {"code": "print(2 + 3)"}))
    assert code["success"] is True
    assert "5" in code["data"]["stdout"]
    denied = asyncio.run(registry.call_tool("agent_file_read", {"path": str(Path.home() / ".ssh" / "config")}))
    assert denied["success"] is False


def test_file_checkpoint_and_rollback_for_workspace_mutations(tmp_path) -> None:
    registry = build_default_tool_registry(
        session_store=AgentSessionStore(tmp_path / "state.sqlite3"),
        policy_engine=ToolPolicyEngine(ToolPolicy("general_full", True, (str(tmp_path),))),
    )
    target = tmp_path / "notes.txt"
    target.write_text("original", encoding="utf-8")

    with internal_control_context("test.file_patch"):
        patched = asyncio.run(registry.call_tool("agent_file_patch", {"path": "notes.txt", "old": "original", "new": "changed"}))
    assert patched["success"] is True
    checkpoint_id = patched["data"]["checkpoint"]["checkpoint_id"]
    assert target.read_text(encoding="utf-8") == "changed"

    with internal_control_context("test.file_rollback"):
        rollback = asyncio.run(registry.call_tool("agent_file_rollback", {"checkpoint_id": checkpoint_id}))
    assert rollback["success"] is True
    assert rollback["data"]["rolled_back_to"]["checkpoint_id"] == checkpoint_id
    assert rollback["data"]["pre_rollback_checkpoint"]["checkpoint_id"] != checkpoint_id
    assert rollback["data"]["mutation_verification"]["changed"] is True
    assert target.read_text(encoding="utf-8") == "original"

    with internal_control_context("test.file_write_created"):
        created = asyncio.run(registry.call_tool("agent_file_write", {"path": "created.txt", "content": "temporary"}))
    assert created["success"] is True
    created_checkpoint = created["data"]["checkpoint"]["checkpoint_id"]
    assert (tmp_path / "created.txt").exists()
    with internal_control_context("test.file_rollback_created"):
        deleted = asyncio.run(registry.call_tool("agent_file_rollback", {"checkpoint_id": created_checkpoint}))
    assert deleted["success"] is True
    assert not (tmp_path / "created.txt").exists()


def test_terminal_background_process_lifecycle(tmp_path) -> None:
    registry = build_default_tool_registry(
        session_store=AgentSessionStore(tmp_path / "state.sqlite3"),
        policy_engine=ToolPolicyEngine(ToolPolicy("general_full", True, (str(tmp_path),))),
    )

    async def scenario() -> None:
        with internal_control_context("test.background_terminal_start"):
            started = await registry.call_tool(
                "agent_terminal",
                {
                    "command": "python -c 'import time; time.sleep(30)'",
                    "cwd": ".",
                    "background": True,
                    "session_id": "s1",
                },
            )
        assert started["success"] is True
        process_id = started["data"]["process_id"]
        listed = await registry.call_tool("agent_process", {"action": "list", "session_id": "s1"})
        assert any(item["process_id"] == process_id and item["status"] in {"running", "detached_running"} for item in listed["data"]["items"])
        with internal_control_context("test.background_process_kill"):
            killed = await registry.call_tool("agent_process", {"action": "kill", "process_id": process_id})
        assert killed["success"] is True
        assert killed["data"]["killed"] is True

    asyncio.run(scenario())


def test_background_process_recovers_as_detached_after_registry_rebuild(tmp_path) -> None:
    store = AgentSessionStore(tmp_path / "state.sqlite3")

    async def scenario() -> None:
        registry = build_default_tool_registry(
            session_store=store,
            policy_engine=ToolPolicyEngine(ToolPolicy("general_full", True, (str(tmp_path),))),
        )
        with internal_control_context("test.recover_terminal_start"):
            started = await registry.call_tool(
                "agent_terminal",
                {
                    "command": "python -c 'import time; print(\"ready\", flush=True); time.sleep(30)'",
                    "cwd": ".",
                    "background": True,
                    "session_id": "recover",
                },
            )
        assert started["success"] is True
        process_id = started["data"]["process_id"]
        await asyncio.sleep(0.2)

        rebuilt = build_default_tool_registry(
            session_store=store,
            policy_engine=ToolPolicyEngine(ToolPolicy("general_full", True, (str(tmp_path),))),
        )
        listed = await rebuilt.call_tool("agent_process", {"action": "list", "session_id": "recover"})
        recovered = next(item for item in listed["data"]["items"] if item["process_id"] == process_id)
        assert recovered["status"] in {"running", "detached_running"}
        assert recovered["metadata"]["recovered"] is True

        read = await rebuilt.call_tool("agent_process", {"action": "read", "process_id": process_id, "max_output_bytes": 1000})
        assert read["success"] is True
        assert "ready" in read["data"]["stdout"]

        with internal_control_context("test.recover_process_kill"):
            killed = await rebuilt.call_tool("agent_process", {"action": "kill", "process_id": process_id})
        assert killed["success"] is True
        assert killed["data"]["killed"] is True
        await asyncio.sleep(0.1)

    asyncio.run(scenario())


def test_execute_python_can_call_enabled_aiask_tools_via_rpc(tmp_path) -> None:
    store = AgentSessionStore(tmp_path / "state.sqlite3")
    runtime = AgentRuntime(
        model_client=MockModelClient(),
        tool_registry=build_default_tool_registry(
            session_store=store,
            policy_engine=ToolPolicyEngine(ToolPolicy("general_full", True, (str(tmp_path),))),
        ),
        session_store=store,
        max_iterations=1,
    )
    with internal_control_context("test.execute_python_rpc"):
        result = asyncio.run(
            runtime.tool_registry.call_tool(
                "agent_execute_python",
                {
                    "code": (
                        "from aiask_tools import agent_file_write\n"
                        "result = agent_file_write(path='rpc-note.txt', content='native rpc ok')\n"
                        "print(result.get('error_code'))\n"
                    ),
                    "cwd": ".",
                },
            )
        )
    assert result["success"] is True
    assert result["data"]["stdout"]
    assert [item["tool"] for item in result["data"]["tool_calls"]] == ["agent_file_write"]


def test_execute_python_rpc_tool_calls_are_audited_inside_agent_run(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("AIASK_AGENT_WORKSPACE_ROOTS", str(tmp_path))
    store = AgentSessionStore(tmp_path / "state.sqlite3")
    runtime = AgentRuntime(
        model_client=ExecutePythonRpcModel(),
        tool_registry=build_default_tool_registry(
            session_store=store,
            policy_engine=ToolPolicyEngine(ToolPolicy("general_full", True, (str(tmp_path),))),
        ),
        session_store=store,
        max_iterations=2,
    )

    result = asyncio.run(runtime.run([{"role": "user", "content": "run python rpc audit"}], user_id="local"))

    assert result.status == "completed"
    assert result.tool_calls[0]["id"] == "call_python_rpc"
    pending = result.tool_calls[0]["result"]
    assert pending["success"] is False
    assert pending["error_code"] == "ACTION_INTENT_REQUIRED"
    assert pending["data"]["intent"]["action"] == "code.execute_python"

    invocations = store.list_tool_invocations(run_id=result.run_id, limit=10)
    names = {item["tool_name"] for item in invocations}
    assert {"agent_execute_python"} <= names
    events = [item["event"] for item in store.list_run_events(result.run_id)]
    assert events.count("tool.rpc.started") == 0
    assert events.count("tool.rpc.completed") == 0


def test_runtime_events_context_compaction_planner_and_retry(tmp_path) -> None:
    store = AgentSessionStore(tmp_path / "state.sqlite3")
    runtime = AgentRuntime(
        model_client=FlakyModel(),
        tool_registry=AgentToolRegistry(),
        session_store=store,
        context_manager=ContextManager(max_tokens=30, head_messages=1, tail_messages=4),
        max_iterations=1,
        retry_attempts=2,
    )
    long_message = "股票、策略、治理、风险、估值、因子 " + ("very long context " * 80)
    result = asyncio.run(runtime.run([{"role": "user", "content": long_message}], user_id="u1", stream=True))
    event_names = [item["event"] for item in store.list_run_events(result.run_id)]
    assert result.content == "ok after retry"
    assert result.context_summary_id
    assert result.context_snapshot_id
    assert result.planner_steps
    assert "context.compacted" in event_names
    assert "context.snapshot_created" in event_names
    assert "planner.plan_created" in event_names
    assert "model.retry" in event_names
    assert "run.completed" in event_names
    snapshot = store.get_context_snapshot(result.context_snapshot_id)
    assert snapshot is not None
    assert snapshot["context_summary_id"] == result.context_summary_id
    assert snapshot["compacted"] is True
    assert "lossy_compression" in snapshot["risk_flags"]
    assert "token_estimate_not_reduced" in snapshot["risk_flags"]
    assert snapshot["token_estimate_before"] is not None
    assert snapshot["token_estimate_after"] is not None


def test_runtime_injects_hermes_style_context_references(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("AIASK_AGENT_WORKSPACE_ROOTS", str(tmp_path))
    monkeypatch.setenv("AIASK_AGENT_ALLOW_PRIVATE_WEB", "1")
    (tmp_path / "AGENTS.md").write_text("Project rule: prefer audited AIASK tools.", encoding="utf-8")
    (tmp_path / "notes.txt").write_text("Referenced file payload alpha.", encoding="utf-8")

    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            body = b"<html><body>Referenced URL payload beta.</body></html>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_: Any) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}/context"
        model = CaptureMessagesModel()
        store = AgentSessionStore(tmp_path / "state.sqlite3")
        runtime = AgentRuntime(
            model_client=model,
            tool_registry=AgentToolRegistry(),
            session_store=store,
            max_iterations=1,
        )

        result = asyncio.run(
            runtime.run(
                [{"role": "user", "content": f"Use @file:notes.txt and @{url} for context."}],
                user_id="u1",
            )
        )
    finally:
        server.shutdown()
        server.server_close()

    context_messages = [item for item in model.messages if item.get("name") == "context_references"]
    assert context_messages
    context_text = context_messages[0]["content"]
    assert "Project rule: prefer audited AIASK tools." in context_text
    assert "Referenced file payload alpha." in context_text
    assert "Referenced URL payload beta." in context_text
    event_names = [item["event"] for item in store.list_run_events(result.run_id)]
    assert "context.references_resolved" in event_names
    assert "context.snapshot_created" in event_names
    assert result.context_snapshot_id
    snapshot = store.get_context_snapshot(result.context_snapshot_id)
    assert snapshot is not None
    assert len(snapshot["source_ids"]) == 1
    assert len(snapshot["artifact_ids"]) >= 2
    assert snapshot["metadata"]["context_reference_count"] >= 3
    assert any(item["kind"] in {"project_file", "file_reference"} for item in store.list_artifacts(run_id=result.run_id))
    assert any(item["source_type"] == "context_url" for item in store.list_sources(run_id=result.run_id))
    assert any(item["kind"] == "artifact" for item in store.search(query="payload alpha", session_id=result.session_id))
    assert any(item["kind"] == "source" for item in store.search(query="payload beta", session_id=result.session_id))


def test_memory_provider_catalog_covers_hermes_external_providers(tmp_path) -> None:
    manager = MemoryProviderManager(
        path=tmp_path / "state.sqlite3",
        env={
            "AIASK_MEMORY_PROVIDER": "mem0",
            "MEM0_API_KEY": "secret-value",
        },
    )

    status = manager.status()
    providers = {item["name"]: item for item in status["providers"]}
    expected = {item["name"] for item in HERMES_EXTERNAL_MEMORY_PROVIDERS}
    assert expected <= set(providers)
    assert providers["mem0"]["configured"] is True
    assert providers["mem0"]["status"] == "live_unverified"
    assert providers["supermemory"]["required_env"] == ["SUPERMEMORY_API_KEY"]
    assert "secret-value" not in json.dumps(status)

    catalog = manager.catalog()
    assert catalog["catalog"]["hermes_external_provider_count"] == len(HERMES_EXTERNAL_MEMORY_PROVIDERS)
    audit = manager.audit()
    assert audit["issue_count"] == 0

    registry = build_default_tool_registry(
        session_store=AgentSessionStore(tmp_path / "tool-state.sqlite3"),
        policy_engine=ToolPolicyEngine(ToolPolicy("general_full", True, (str(tmp_path),))),
    )
    tool_catalog = asyncio.run(registry.call_tool("agent_memory_manage", {"action": "catalog"}))
    names = {item["name"] for item in tool_catalog["data"]["providers"]}
    assert expected <= names


def test_runtime_subagent_and_job_execution(tmp_path) -> None:
    policy = ToolPolicyEngine(ToolPolicy("general_full", True, (str(tmp_path),)))
    store = AgentSessionStore(tmp_path / "state.sqlite3")
    registry = build_default_tool_registry(session_store=store, policy_engine=policy)
    runtime = AgentRuntime(
        model_client=MockModelClient(),
        tool_registry=registry,
        session_store=store,
        max_iterations=2,
    )
    delegated = asyncio.run(
        runtime.tool_registry.call_tool("agent_delegate_task", {"task": "hello child", "toolset": "finance_safe"})
    )
    assert delegated["success"] is True
    assert delegated["data"]["subrun"]["run_id"].startswith("run_")

    job = runtime.job_store.create(name="daily review", prompt="job prompt", interval_seconds=3600)
    with internal_control_context("test.job_run"):
        ran = asyncio.run(runtime.tool_registry.call_tool("agent_job_run", {"job_id": job["job_id"]}))
    assert ran["success"] is True
    assert ran["data"]["job"]["last_run_at"]
    cron_job = runtime.job_store.create(name="cron review", prompt="cron prompt", schedule="*/5 * * * *")
    assert cron_job["next_run_at"]


def test_delegate_task_links_parent_and_child_context_snapshots_inside_agent_run(tmp_path) -> None:
    policy = ToolPolicyEngine(ToolPolicy("general_full", True, (str(tmp_path),)))
    store = AgentSessionStore(tmp_path / "state.sqlite3")
    registry = build_default_tool_registry(session_store=store, policy_engine=policy)
    runtime = AgentRuntime(
        model_client=DelegateTaskModel(),
        tool_registry=registry,
        session_store=store,
        max_iterations=2,
    )

    result = asyncio.run(runtime.run([{"role": "user", "content": "delegate with context"}], user_id="u1"))

    assert result.context_snapshot_id
    assert result.subruns
    record = result.subruns[0]
    assert record["mode"] == "delegation_subrun"
    assert record["parent_run_id"] == result.run_id
    assert record["parent_session_id"] == result.session_id
    assert record["parent_tool_call_id"] == "call_delegate"
    assert record["parent_context_snapshot_id"] == result.context_snapshot_id
    assert record["child_context_snapshot_id"]
    assert store.get_context_snapshot(record["child_context_snapshot_id"]) is not None
    child_run = store.get_run(record["run_id"])
    assert child_run["payload"]["user_id"] == "u1"
    assert result.tool_calls[0]["result"]["data"]["subrun"]["parent_context_snapshot_id"] == result.context_snapshot_id


def test_delegate_task_cannot_target_other_user_from_runtime(tmp_path) -> None:
    policy = ToolPolicyEngine(ToolPolicy("general_full", True, (str(tmp_path),)))
    store = AgentSessionStore(tmp_path / "state.sqlite3")
    registry = build_default_tool_registry(session_store=store, policy_engine=policy)
    runtime = AgentRuntime(
        model_client=CrossUserDelegateTaskModel(),
        tool_registry=registry,
        session_store=store,
        max_iterations=2,
    )

    result = asyncio.run(
        runtime.run(
            [{"role": "user", "content": "delegate but stay in user scope"}],
            session_id="active-session",
            user_id="u1",
        )
    )

    assert result.subruns == []
    assert result.tool_calls[0]["name"] == "agent_delegate_task"
    tool_result = result.tool_calls[0]["result"]
    assert tool_result["success"] is False
    assert tool_result["error_code"] == "DELEGATE_USER_SCOPE_VIOLATION"
