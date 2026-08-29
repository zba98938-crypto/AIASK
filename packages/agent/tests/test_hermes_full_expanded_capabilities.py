from __future__ import annotations

import asyncio
import json

from fastapi.testclient import TestClient

from aiask_agent.control_context import internal_control_context
from aiask_agent.model_client import MockModelClient
from aiask_agent.runtime import AgentRuntime
from aiask_agent.server import create_app
from aiask_agent.session_store import AgentSessionStore
from aiask_agent.tool_registry import build_default_tool_registry
from aiask_agent.tools.policy import ToolPolicy, ToolPolicyEngine


def _control_headers() -> dict[str, str]:
    return {"Authorization": "Bearer secret"}


def _full_policy(tmp_path) -> ToolPolicyEngine:
    return ToolPolicyEngine(ToolPolicy("general_full", True, (str(tmp_path),)))


def test_expanded_hermes_tools_are_full_mode_only(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("AIASK_AGENT_HOME", str(tmp_path / "home"))
    finance = build_default_tool_registry(
        session_store=AgentSessionStore(tmp_path / "finance.sqlite3"),
        policy_engine=ToolPolicyEngine(ToolPolicy("finance_safe", False, (str(tmp_path),))),
    )
    assert "agent_gateway_status" not in finance.names()
    assert "agent_rl_list_environments" not in finance.names()
    assert "agent_ha_list_entities" not in finance.names()

    full = build_default_tool_registry(
        session_store=AgentSessionStore(tmp_path / "full.sqlite3"),
        policy_engine=_full_policy(tmp_path),
    )
    names = set(full.names())
    assert {
        "agent_tui_status",
        "agent_terminal_backends",
        "agent_computer_use",
        "agent_file_mutation_verify",
        "agent_file_checkpoint",
        "agent_file_rollback",
        "agent_x_search",
        "agent_video_generate",
        "agent_session_handoff",
        "agent_subgoal",
        "agent_gateway_status",
        "agent_gateway_send_message",
        "agent_learning_status",
        "agent_ha_list_entities",
        "agent_ha_call_service",
        "agent_rl_list_environments",
        "agent_rl_start_training",
    } <= names

    runtime = AgentRuntime(
        model_client=MockModelClient(),
        session_store=AgentSessionStore(tmp_path / "runtime.sqlite3"),
        tool_registry=full,
        max_iterations=2,
    )
    assert "agent_moa" in runtime.tool_registry.names()


def test_expanded_full_mode_api_surfaces(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("AIASK_AGENT_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("AIASK_AGENT_ENABLE_HERMES_FULL", "1")
    monkeypatch.setenv("AIASK_AGENT_CONTROL_TOKEN", "secret")
    store = AgentSessionStore(tmp_path / "state.sqlite3")
    runtime = AgentRuntime(model_client=MockModelClient(), session_store=store, max_iterations=2)
    client = TestClient(create_app(runtime=runtime))

    status = client.get("/v1/hermes/status")
    assert status.status_code == 200
    payload = status.json()
    assert payload["embedded_vendor_runtime"] is False
    assert payload["full_scope"] == "hermes_full_runtime"
    assert payload["platform_gateway"]["implementation"] == "aiask_native"
    assert payload["providers"]["object"] == "aiask.model_provider_status"
    assert payload["memory"]["default_provider"] == "sqlite"
    assert payload["security"]["status"] == "implemented"
    assert payload["skill_packs"]["vendor_text_copied"] is False
    assert any(item["name"] == "local" for item in payload["terminal_backends"])

    for path in (
        "/v1/gateway/status",
        "/v1/gateway/platforms",
        "/v1/gateway/messages",
        "/v1/terminal/backends",
        "/v1/terminal/sessions",
        "/v1/learning/status",
        "/v1/learning/review",
        "/v1/rl/environments",
        "/v1/rl/config",
        "/v1/rl/runs",
    ):
        response = client.get(path, headers=_control_headers())
        assert response.status_code == 200, path

    sent = client.post(
        "/v1/gateway/send",
        headers=_control_headers(),
        json={"platform": "local", "target": "desktop", "message": "hello", "session_id": "sess_gateway"},
    )
    assert sent.status_code == 200
    sent_payload = sent.json()
    assert sent_payload["object"] == "gateway.message_intent"
    assert sent_payload["success"] is True
    assert sent_payload["data"]["intent"]["action"] == "gateway.send_message"
    assert sent_payload["data"]["intent"]["status"] == "awaiting_confirmation"
    assert sent_payload["meta"]["action_intent_required"] is True
    assert sent_payload["meta"]["side_effect"]["confirmation_required"] is True


def test_expanded_tools_report_configured_false_without_external_credentials(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("AIASK_AGENT_HOME", str(tmp_path / "home"))
    full = build_default_tool_registry(
        session_store=AgentSessionStore(tmp_path / "state.sqlite3"),
        policy_engine=_full_policy(tmp_path),
    )

    with internal_control_context("test.rl_readiness"):
        rl_start = asyncio.run(full.call_tool("agent_rl_start_training", {"environment": "demo"}))
    assert rl_start["success"] is False
    assert rl_start["data"]["configured"] is False

    ha_entities = asyncio.run(full.call_tool("agent_ha_list_entities", {}))
    assert ha_entities["success"] is False
    assert ha_entities["data"]["configured"] is False

    ha_call = asyncio.run(full.call_tool("agent_ha_call_service", {"domain": "light", "service": "turn_on", "entity_id": "light.desk"}))
    assert ha_call["success"] is False
    assert ha_call["error_code"] == "ACTION_INTENT_REQUIRED"
    assert ha_call["data"]["intent"]["action"] == "homeassistant.call_service"
    assert ha_call["data"]["executed"] is False

    docker = asyncio.run(full.call_tool("agent_terminal", {"command": "pwd", "backend": "daytona"}))
    assert docker["success"] is False
    assert docker["error_code"] == "ACTION_INTENT_REQUIRED"
    assert docker["data"]["intent"]["action"] == "terminal.run"
    with internal_control_context("test.terminal_backend_config"):
        docker = asyncio.run(full.call_tool("agent_terminal", {"command": "pwd", "backend": "daytona"}))
    assert docker["success"] is False
    assert docker["data"]["backend"]["configured"] is False

    gateway = asyncio.run(full.call_tool("agent_gateway_send_message", {"platform": "feishu", "target": "chat", "message": "hello"}))
    assert gateway["success"] is False
    assert gateway["error_code"] == "ACTION_INTENT_REQUIRED"
    assert gateway["data"]["action_intent_required"] is True
    assert gateway["data"]["executed"] is False
    assert gateway["data"]["intent"]["action"] == "gateway.send_message"
    assert gateway["data"]["intent"]["status"] == "awaiting_confirmation"
    assert gateway["meta"]["side_effect"]["confirmation_required"] is True

    x_search = asyncio.run(full.call_tool("agent_x_search", {"query": "AIASK"}))
    assert x_search["success"] is False
    assert x_search["data"]["configured"] is False

    video = asyncio.run(full.call_tool("agent_video_generate", {"action": "create", "prompt": "market brief"}))
    assert video["success"] is False
    assert video["data"]["configured"] is False

    computer = asyncio.run(full.call_tool("agent_computer_use", {"action": "status"}))
    assert computer["success"] is True
    assert computer["data"]["configured"] is False
    assert computer["data"]["os_desktop_control"] is False

def test_v014_native_session_file_and_subgoal_tools(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("AIASK_AGENT_HOME", str(tmp_path / "home"))
    store = AgentSessionStore(tmp_path / "state.sqlite3")
    full = build_default_tool_registry(
        session_store=store,
        policy_engine=_full_policy(tmp_path),
    )

    write_pending = asyncio.run(full.call_tool("agent_file_write", {"path": "demo.py", "content": "x = 1\n"}))
    assert write_pending["success"] is False
    assert write_pending["error_code"] == "ACTION_INTENT_REQUIRED"
    assert write_pending["data"]["intent"]["action"] == "file.write"

    with internal_control_context("test.native_file_write"):
        write = asyncio.run(full.call_tool("agent_file_write", {"path": "demo.py", "content": "x = 1\n"}))
    assert write["success"] is True
    assert write["data"]["mutation_verification"]["exists"] is True
    assert write["data"]["mutation_verification"]["sha256"]
    assert write["data"]["diagnostics"][0]["status"] == "passed"

    verify = asyncio.run(full.call_tool("agent_file_mutation_verify", {"path": "demo.py"}))
    assert verify["success"] is True
    assert verify["data"]["allowed_workspace"] is True
    assert verify["data"]["sha256"] == write["data"]["mutation_verification"]["sha256"]

    subgoal = asyncio.run(
        full.call_tool(
            "agent_subgoal",
            {"action": "add", "session_id": "s1", "title": "collect evidence", "criteria": ["tests pass"]},
        )
    )
    assert subgoal["success"] is True
    assert subgoal["data"]["subgoal"]["criteria"] == ["tests pass"]
    status = asyncio.run(full.call_tool("agent_subgoal", {"action": "status", "session_id": "s1"}))
    assert status["data"]["by_status"]["pending"] == 1

    with internal_control_context("test.session_handoff_setup"):
        handoff = asyncio.run(
            full.call_tool(
                "agent_session_handoff",
                {"action": "request", "session_id": "s1", "target": "ops", "reason": "continue review"},
            )
        )
    assert handoff["success"] is True
    handoff_id = handoff["data"]["handoff"]["handoff_id"]
    state = store.get_session("s1")["metadata"]["handoff_state"]
    assert state["status"] == "pending"
    assert state["handoff_id"] == handoff_id
    assert state["target"] == "ops"
    with internal_control_context("test.session_handoff_complete"):
        done = asyncio.run(full.call_tool("agent_session_handoff", {"action": "complete", "handoff_id": handoff_id}))
    assert done["success"] is True
    assert done["data"]["handoff"]["status"] == "completed"
    assert done["data"]["handoff_state"]["status"] == "completed"


def test_moa_runtime_tool_uses_aiask_model_client(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("AIASK_AGENT_HOME", str(tmp_path / "home"))
    store = AgentSessionStore(tmp_path / "state.sqlite3")
    runtime = AgentRuntime(
        model_client=MockModelClient(),
        session_store=store,
        tool_registry=build_default_tool_registry(session_store=store, policy_engine=_full_policy(tmp_path)),
        max_iterations=2,
    )
    result = asyncio.run(runtime.tool_registry.call_tool("agent_moa", {"user_prompt": "Summarize AIASK"}))
    assert result["success"] is True
    assert result["data"]["configured"] is True
    assert result["data"]["aggregator_model"] == runtime.model


def test_dynamic_plugin_tools_are_wrapped_as_agent_tools(tmp_path, monkeypatch) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("AIASK_AGENT_HOME", str(home))
    plugin_dir = home / "plugins" / "demo-plugin"
    plugin_dir.mkdir(parents=True)
    plugin_dir.joinpath("plugin.json").write_text(
        json.dumps(
            {
                "name": "demo-plugin",
                "enabled": True,
                "tools": [
                    {
                        "name": "summarize",
                        "description": "Summarize text",
                        "parameters": {"type": "object"},
                        "side_effect": "read_only",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    full = build_default_tool_registry(
        session_store=AgentSessionStore(tmp_path / "state.sqlite3"),
        policy_engine=_full_policy(tmp_path),
    )
    wrapped = "agent_plugin_demo_plugin_summarize"
    assert wrapped in full.names()
    result = asyncio.run(full.call_tool(wrapped, {"text": "hello"}))
    assert result["success"] is True
    assert result["data"]["configured"] is False


def test_dynamic_plugin_tool_name_collisions_are_not_model_visible(tmp_path, monkeypatch) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("AIASK_AGENT_HOME", str(home))
    monkeypatch.setenv("AIASK_AGENT_ENABLE_HERMES_FULL", "1")
    monkeypatch.setenv("AIASK_AGENT_CONTROL_TOKEN", "secret")
    marker = tmp_path / "collision_executed.txt"
    for dirname, plugin_name, value in (
        ("demo-a", "demo-a", "a"),
        ("demo_a", "demo_a", "b"),
    ):
        plugin_dir = home / "plugins" / dirname
        plugin_dir.mkdir(parents=True)
        plugin_dir.joinpath("runner.py").write_text(
            "from pathlib import Path\n"
            "def run(payload):\n"
            "    marker = payload['arguments'].get('marker')\n"
            "    if marker:\n"
            "        Path(marker).write_text(payload['arguments'].get('value', ''), encoding='utf-8')\n"
            "    return {'value': payload['arguments'].get('value')}\n",
            encoding="utf-8",
        )
        plugin_dir.joinpath("plugin.json").write_text(
            json.dumps(
                {
                    "name": plugin_name,
                    "enabled": True,
                    "tools": [
                        {
                            "name": "ship-it",
                            "side_effect": "read_only",
                            "runner": {"type": "python_module", "module": "runner", "function": "run"},
                            "parameters": {"type": "object"},
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )

    full = build_default_tool_registry(
        session_store=AgentSessionStore(tmp_path / "state.sqlite3"),
        policy_engine=_full_policy(tmp_path),
    )
    wrapped = "agent_plugin_demo_a_ship_it"
    assert wrapped not in full.names()

    runtime = AgentRuntime(
        model_client=MockModelClient(),
        session_store=AgentSessionStore(tmp_path / "state.sqlite3"),
        max_iterations=2,
    )
    client = TestClient(create_app(runtime=runtime))
    response = client.post(
        "/v1/plugins/demo-a/tools/ship-it/test",
        headers=_control_headers(),
        json={"marker": str(marker), "value": "should-not-run"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["success"] is False
    assert payload["error_code"] == "PLUGIN_TOOL_NAME_COLLISION"
    assert payload["data"]["wrapped_name"] == wrapped
    assert len(payload["data"]["collisions"]) == 2
    assert not marker.exists()


def test_dynamic_plugin_command_name_collisions_are_not_model_visible(tmp_path, monkeypatch) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("AIASK_AGENT_HOME", str(home))
    monkeypatch.setenv("AIASK_AGENT_ENABLE_HERMES_FULL", "1")
    monkeypatch.setenv("AIASK_AGENT_CONTROL_TOKEN", "secret")
    marker = tmp_path / "command_collision_executed.txt"
    for dirname, plugin_name in (
        ("command-a", "command-a"),
        ("command_a", "command_a"),
    ):
        plugin_dir = home / "plugins" / dirname
        plugin_dir.mkdir(parents=True)
        plugin_dir.joinpath("runner.py").write_text(
            "from pathlib import Path\n"
            "def run(payload):\n"
            "    marker = payload['arguments'].get('marker')\n"
            "    if marker:\n"
            "        Path(marker).write_text(payload['arguments'].get('value', ''), encoding='utf-8')\n"
            "    return {'value': payload['arguments'].get('value')}\n",
            encoding="utf-8",
        )
        plugin_dir.joinpath("plugin.json").write_text(
            json.dumps(
                {
                    "name": plugin_name,
                    "enabled": True,
                    "commands": [
                        {
                            "name": "ship-it",
                            "side_effect": "read_only",
                            "runner": {"type": "python_module", "module": "runner", "function": "run"},
                            "parameters": {"type": "object"},
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )

    full = build_default_tool_registry(
        session_store=AgentSessionStore(tmp_path / "state.sqlite3"),
        policy_engine=_full_policy(tmp_path),
    )
    wrapped = "agent_plugin_command_a_command_ship_it"
    assert wrapped not in full.names()

    runtime = AgentRuntime(
        model_client=MockModelClient(),
        session_store=AgentSessionStore(tmp_path / "state.sqlite3"),
        max_iterations=2,
    )
    client = TestClient(create_app(runtime=runtime))
    response = client.post(
        "/v1/plugins/command-a/commands/ship-it/test",
        headers=_control_headers(),
        json={"marker": str(marker), "value": "should-not-run"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["success"] is False
    assert payload["error_code"] == "PLUGIN_COMMAND_NAME_COLLISION"
    assert payload["data"]["wrapped_name"] == wrapped
    assert len(payload["data"]["collisions"]) == 2
    assert not marker.exists()
