from __future__ import annotations

import asyncio
import json
import sys

from fastapi.testclient import TestClient

from aiask_agent.adapters import quant
from aiask_agent.model_client import MockModelClient
from aiask_agent.runtime import AgentRuntime
from aiask_agent.server import create_app
from aiask_agent.session_store import AgentSessionStore
from aiask_agent.mcp_payloads import mcp_action_error_payload
from aiask_agent.tool_registry import build_default_tool_registry
from aiask_agent.tools.policy import ToolPolicy, ToolPolicyEngine


def _control_headers() -> dict[str, str]:
    return {"Authorization": "Bearer secret"}


def _confirm_intent_response(
    client: TestClient,
    response,
    *,
    object_name: str,
    action: str,
) -> dict:
    assert response.status_code == 200
    payload = response.json()
    assert payload["object"] == object_name
    assert payload["success"] is True
    assert payload["data"]["intent"]["action"] == action
    assert payload["data"]["intent"]["status"] == "awaiting_confirmation"
    assert payload["meta"]["action_intent_required"] is True
    assert payload["meta"]["side_effect"]["confirmation_required"] is True
    confirmed = client.post(
        f"/intents/{payload['data']['intent']['intent_id']}/confirm",
        headers=_control_headers(),
    )
    assert confirmed.status_code == 200
    confirmed_payload = confirmed.json()
    assert confirmed_payload["success"] is True
    assert confirmed_payload["data"]["intent"]["action"] == action
    assert confirmed_payload["data"]["intent"]["status"] == "succeeded"
    return confirmed_payload


def test_fastapi_native_full_management_surface(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("AIASK_AGENT_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("AIASK_AGENT_ENABLE_HERMES_FULL", "1")
    monkeypatch.setenv("AIASK_AGENT_CONTROL_TOKEN", "secret")
    mcp_config = tmp_path / "mcp_servers.json"
    mcp_config.write_text(
        json.dumps(
            {
                "servers": [
                    {
                        "name": "finance-demo",
                        "domain": "financial",
                        "transport": "stdio",
                        "command": "python",
                        "tools": [
                            {"name": "quote", "description": "quote tool", "parameters": {"type": "object"}},
                            {"name": "terminal", "description": "shell tool"},
                        ],
                        "resources": [{"uri": "aiask://quotes"}],
                        "prompts": [{"name": "risk-review"}],
                        "auth": "oauth",
                    },
                    {
                        "name": "general-demo",
                        "domain": "general",
                        "transport": "http",
                        "url": "http://localhost:9999/mcp",
                        "tools": [{"name": "browser", "description": "browser tool"}],
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("AIASK_AGENT_MCP_CONFIG", str(mcp_config))

    store = AgentSessionStore(tmp_path / "state.sqlite3")
    runtime = AgentRuntime(
        model_client=MockModelClient(),
        session_store=store,
        max_iterations=2,
    )
    client = TestClient(create_app(runtime=runtime))

    status = client.get("/v1/hermes/status")
    assert status.status_code == 200
    status_payload = status.json()
    assert status_payload["implementation"] == "aiask_native"
    assert status_payload["embedded_vendor_runtime"] is False
    assert status_payload["parity"]["status"] == "in_progress"

    denied = client.get("/v1/hermes/tools")
    assert denied.status_code == 401
    tools = client.get("/v1/hermes/tools", headers=_control_headers())
    assert tools.status_code == 200
    names = {item["name"] for item in tools.json()["data"]}
    assert {"agent_terminal", "agent_webhook", "agent_mcp_manage", "agent_cronjob"} <= names
    assert not any(not name.startswith("agent_") for name in names)

    mcp_tools = client.get("/v1/mcp/tools?all=true", headers=_control_headers())
    assert mcp_tools.status_code == 200
    assert {item["name"] for item in mcp_tools.json()["data"]} == {"quote", "terminal", "browser"}
    resources = client.get("/v1/mcp/resources", headers=_control_headers())
    assert resources.json()["data"][0]["uri"] == "aiask://quotes"
    prompts = client.get("/v1/mcp/prompts", headers=_control_headers())
    assert prompts.json()["data"][0]["name"] == "risk-review"
    oauth = client.get("/v1/mcp/oauth_status", headers=_control_headers())
    assert oauth.json()["data"][0]["configured"] is True
    resource_read = client.post(
        "/v1/mcp/resources/read",
        headers={**_control_headers(), "Origin": "http://127.0.0.1:1420"},
        json={"server": "missing-demo", "uri": "aiask://quotes"},
    )
    assert resource_read.status_code == 200
    assert resource_read.headers["access-control-allow-origin"] == "http://127.0.0.1:1420"
    assert resource_read.json()["success"] is False
    assert resource_read.json()["error_code"] in {"MCP_DISCOVERY_FAILED", "MCP_DISCOVERY_CONNECTION_FAILED"}
    prompt_get = client.post(
        "/v1/mcp/prompts/get",
        headers={**_control_headers(), "Origin": "http://127.0.0.1:1420"},
        json={"server": "missing-demo", "name": "risk-review"},
    )
    assert prompt_get.status_code == 200
    assert prompt_get.headers["access-control-allow-origin"] == "http://127.0.0.1:1420"
    assert prompt_get.json()["success"] is False

    created_skill = client.post(
        "/v1/skills",
        headers=_control_headers(),
        json={"name": "risk-review", "description": "Risk review", "content": "# Risk Review\n"},
    )
    created_skill_confirmed = _confirm_intent_response(
        client,
        created_skill,
        object_name="skill.install_intent",
        action="skill.install",
    )
    assert created_skill_confirmed["data"]["execution_result"]["data"]["skill"]["name"] == "risk-review"
    skills = client.get("/v1/skills", headers=_control_headers())
    assert any(item["name"] == "risk-review" for item in skills.json()["data"]["skills"])
    assert skills.json()["data"]["count"] >= 1

    disabled_skill = client.patch(
        "/v1/skills/risk-review",
        headers=_control_headers(),
        json={"enabled": False},
    )
    disabled_skill_confirmed = _confirm_intent_response(
        client,
        disabled_skill,
        object_name="skill.set_enabled_intent",
        action="skill.set_enabled",
    )
    assert disabled_skill_confirmed["data"]["execution_result"]["data"]["skill"]["name"] == "risk-review"
    skills = client.get("/v1/skills", headers=_control_headers())
    risk_review = next(item for item in skills.json()["data"]["skills"] if item["name"] == "risk-review")
    assert risk_review["enabled"] is False
    assert risk_review["status"] == "disabled"

    registered_skill = client.post(
        "/v1/skills",
        headers=_control_headers(),
        json={"name": "registered-from-desktop", "type": "local", "path": "C:/skills/registered-from-desktop/SKILL.md"},
    )
    registered_skill_confirmed = _confirm_intent_response(
        client,
        registered_skill,
        object_name="skill.install_intent",
        action="skill.install",
    )
    assert registered_skill_confirmed["data"]["execution_result"]["data"]["skill"]["name"] == "registered-from-desktop"
    skills = client.get("/v1/skills", headers=_control_headers())
    assert any(item["name"] == "registered-from-desktop" for item in skills.json()["data"]["skills"])

    plugin = client.post(
        "/v1/plugins",
        headers=_control_headers(),
        json={"name": "audit-plugin", "manifest": {"description": "Audit", "enabled": False}},
    )
    plugin_confirmed = _confirm_intent_response(
        client,
        plugin,
        object_name="plugin.upsert_intent",
        action="plugin.upsert",
    )
    assert plugin_confirmed["data"]["execution_result"]["data"]["plugin"]["name"] == "audit-plugin"
    enabled = client.patch("/v1/plugins/audit-plugin", headers=_control_headers(), json={"enabled": True})
    enabled_confirmed = _confirm_intent_response(
        client,
        enabled,
        object_name="plugin.enable_intent",
        action="plugin.enable",
    )
    assert enabled_confirmed["data"]["execution_result"]["data"]["plugin"]["enabled"] is True
    plugins = client.get("/v1/plugins", headers=_control_headers())
    assert any(item["name"] == "audit-plugin" and item["enabled"] for item in plugins.json()["data"])
    self_test = client.post("/v1/plugins/audit-plugin/tools/__manifest__/test", headers=_control_headers(), json={})
    assert self_test.status_code == 200
    assert self_test.json()["success"] is True
    assert self_test.json()["data"]["test_type"] == "manifest"
    missing_tool = client.post("/v1/plugins/audit-plugin/tools/missing/test", headers=_control_headers(), json={})
    assert missing_tool.status_code == 200
    assert missing_tool.json()["success"] is False
    assert missing_tool.json()["error_code"] == "PLUGIN_TOOL_NOT_CONFIGURED"

    webhook = client.post(
        "/v1/webhooks",
        headers=_control_headers(),
        json={"name": "factory-alerts", "events": ["factory.done"], "prompt": "Handle {event}: {payload}"},
    )
    assert webhook.status_code == 200
    webhook_payload = webhook.json()
    assert webhook_payload["object"] == "webhook.subscribe_intent"
    assert webhook_payload["data"]["intent"]["action"] == "webhook.subscribe"
    webhook_intent_id = webhook_payload["data"]["intent"]["intent_id"]
    webhook_confirmed = client.post(f"/intents/{webhook_intent_id}/confirm", headers=_control_headers())
    assert webhook_confirmed.status_code == 200
    assert webhook_confirmed.json()["success"] is True
    webhook_id = webhook_confirmed.json()["data"]["execution_result"]["data"]["webhook"]["webhook_id"]
    trigger = client.post(
        f"/v1/webhooks/{webhook_id}/trigger",
        headers=_control_headers(),
        json={"event": "factory.done", "payload": {"run_id": "run_demo"}},
    )
    assert trigger.status_code == 200
    trigger_payload = trigger.json()
    assert trigger_payload["object"] == "webhook.trigger_intent"
    assert trigger_payload["success"] is True
    assert trigger_payload["data"]["intent"]["action"] == "webhook.trigger"
    assert trigger_payload["data"]["intent"]["status"] == "awaiting_confirmation"
    assert trigger_payload["data"]["intent"]["params"]["webhook_id"] == webhook_id
    assert trigger_payload["data"]["intent"]["params"]["event"] == "factory.done"
    assert trigger_payload["data"]["intent"]["params"]["payload"]["run_id"] == "run_demo"
    assert trigger_payload["meta"]["action_intent_required"] is True
    assert trigger_payload["meta"]["side_effect"]["confirmation_required"] is True
    deleted = client.delete(f"/v1/webhooks/{webhook_id}", headers=_control_headers())
    assert deleted.status_code == 200
    deleted_payload = deleted.json()
    assert deleted_payload["object"] == "webhook.remove_intent"
    assert deleted_payload["data"]["intent"]["action"] == "webhook.remove"
    delete_confirmed = client.post(
        f"/intents/{deleted_payload['data']['intent']['intent_id']}/confirm",
        headers=_control_headers(),
    )
    assert delete_confirmed.status_code == 200
    assert delete_confirmed.json()["data"]["execution_result"]["data"]["deleted"] is True

    session_id = store.create_session(user_id="u1")
    run_id = store.create_run(session_id, {"source": "test"})
    steer = client.post(f"/v1/runs/{run_id}/steer", json={"instruction": "stop after current tool"})
    assert steer.status_code == 401
    steer = client.post(
        f"/v1/runs/{run_id}/steer",
        headers=_control_headers(),
        json={"instruction": "stop after current tool"},
    )
    assert steer.status_code == 200
    cancel = client.post(f"/v1/runs/{run_id}/cancel")
    assert cancel.status_code == 401
    cancel = client.post(f"/v1/runs/{run_id}/cancel", headers=_control_headers())
    assert cancel.status_code == 200
    events = [item["event"] for item in store.list_run_events(run_id)]
    assert "run.steer" in events
    assert "run.cancelled" in events


def test_runtime_emits_approval_and_plugin_hook_events(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("AIASK_AGENT_HOME", str(tmp_path / "home"))
    plugin_dir = tmp_path / "home" / "plugins" / "audit-hook"
    plugin_dir.mkdir(parents=True)
    plugin_dir.joinpath("plugin.json").write_text(
        json.dumps(
            {
                "name": "audit-hook",
                "enabled": True,
                "hooks": [{"name": "transform_tool_result", "prefix": "audited"}],
            }
        ),
        encoding="utf-8",
    )
    store = AgentSessionStore(tmp_path / "state.sqlite3")
    policy = ToolPolicyEngine(ToolPolicy("general_full", True, (str(tmp_path),)))
    runtime = AgentRuntime(
        model_client=MockModelClient(),
        session_store=store,
        tool_registry=build_default_tool_registry(session_store=store, policy_engine=policy),
        max_iterations=2,
    )

    result = asyncio.run(
        runtime.run(
            [
                {
                    "role": "user",
                    "content": 'tool:agent_terminal {"command":"rm -rf ./danger broker_token=runtime-approval-secret", "cwd":"."}',
                }
            ]
        )
    )
    run_events = store.list_run_events(result.run_id)
    event_names = [item["event"] for item in run_events]
    assert "approval.pending" not in event_names
    assert "plugin.hook" in event_names
    tool_result = result.tool_calls[0]["result"]
    assert tool_result["error_code"] == "ACTION_INTENT_REQUIRED"
    assert tool_result["data"]["plugin_prefix"] == "audited"
    tool_result_json = json.dumps(tool_result, ensure_ascii=False)
    assert "runtime-approval-secret" not in tool_result_json
    intent = tool_result["data"]["intent"]
    assert intent["action"] == "terminal.run"
    assert intent["secrets_redacted"] is True
    assert "_created_by_agent_terminal_tool" not in intent["params"]
    raw_intent = runtime.tool_registry.intent_store.get(intent["intent_id"])
    assert raw_intent is not None
    assert "runtime-approval-secret" in raw_intent["params"]["command"]


def test_plugin_test_http_responses_redact_runner_arguments_and_results(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("AIASK_AGENT_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("AIASK_AGENT_ENABLE_HERMES_FULL", "1")
    monkeypatch.setenv("AIASK_AGENT_CONTROL_TOKEN", "secret")
    plugin_dir = tmp_path / "home" / "plugins" / "audit-plugin"
    plugin_dir.mkdir(parents=True)
    plugin_dir.joinpath("audit_plugin_runner.py").write_text(
        "def run(payload):\n"
        "    return {\n"
        "        'echo': payload['arguments'],\n"
        "        'password': 'plugin-result-secret',\n"
        "        'note': 'Authorization: Bearer plugin-result-bearer-secret',\n"
        "        'nested': {'broker_token': 'plugin-result-token'},\n"
        "    }\n",
        encoding="utf-8",
    )
    plugin_dir.joinpath("plugin.json").write_text(
        json.dumps(
            {
                "name": "audit-plugin",
                "enabled": True,
                "tools": [
                    {
                        "name": "echo",
                        "description": "Echo payloads for public response redaction tests",
                        "side_effect": "read_only",
                        "runner": {"type": "python_module", "module": "audit_plugin_runner", "function": "run"},
                    }
                ],
                "commands": [
                    {
                        "name": "audit",
                        "description": "Audit command",
                        "side_effect": "read_only",
                        "runner": {"type": "python_module", "module": "audit_plugin_runner", "function": "run"},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    runtime = AgentRuntime(
        model_client=MockModelClient(),
        session_store=AgentSessionStore(tmp_path / "state.sqlite3"),
        max_iterations=2,
    )
    client = TestClient(create_app(runtime=runtime))
    secret_payload = {
        "broker_token": "plugin-param-secret",
        "nested": {"password": "plugin-nested-secret"},
        "note": "Authorization: Bearer plugin-note-secret",
    }

    tool_response = client.post(
        "/v1/plugins/audit-plugin/tools/echo/test",
        headers=_control_headers(),
        json=secret_payload,
    )
    command_response = client.post(
        "/v1/plugins/audit-plugin/commands/audit/test",
        headers=_control_headers(),
        json=secret_payload,
    )

    for response in (tool_response, command_response):
        assert response.status_code == 200
        payload = response.json()
        assert payload["success"] is True
        assert payload["secrets_redacted"] is True
        encoded = json.dumps(payload, ensure_ascii=False)
        assert "plugin-param-secret" not in encoded
        assert "plugin-nested-secret" not in encoded
        assert "plugin-note-secret" not in encoded
        assert "plugin-result-secret" not in encoded
        assert "plugin-result-bearer-secret" not in encoded
        assert "plugin-result-token" not in encoded
        assert payload["data"]["arguments"]["broker_token"] == "[redacted]"
        assert payload["data"]["arguments"]["nested"]["password"] == "[redacted]"
        assert payload["data"]["result"]["password"] == "[redacted]"
        assert payload["data"]["result"]["note"] == "Authorization: Bearer [redacted]"


def test_plugin_test_http_stateful_runner_creates_intent_before_execution(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("AIASK_AGENT_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("AIASK_AGENT_ENABLE_HERMES_FULL", "1")
    monkeypatch.setenv("AIASK_AGENT_CONTROL_TOKEN", "secret")
    plugin_dir = tmp_path / "home" / "plugins" / "stateful-plugin"
    plugin_dir.mkdir(parents=True)
    marker = tmp_path / "stateful_plugin_executed.txt"
    plugin_dir.joinpath("stateful_runner.py").write_text(
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
                "name": "stateful-plugin",
                "enabled": True,
                "tools": [
                    {
                        "name": "mutate",
                        "description": "Stateful runner should require ActionIntent",
                        "runner": {"type": "python_module", "module": "stateful_runner", "function": "run"},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    runtime = AgentRuntime(
        model_client=MockModelClient(),
        session_store=AgentSessionStore(tmp_path / "state.sqlite3"),
        max_iterations=2,
    )
    client = TestClient(create_app(runtime=runtime))

    response = client.post(
        "/v1/plugins/stateful-plugin/tools/mutate/test",
        headers=_control_headers(),
        json={"marker": str(marker), "value": "confirmed"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["object"] == "plugin.tool_test"
    assert payload["success"] is False
    assert payload["error_code"] == "ACTION_INTENT_REQUIRED"
    assert payload["data"]["action_intent_required"] is True
    assert payload["data"]["executed"] is False
    assert payload["data"]["intent"]["action"] == "plugin.tool_call"
    assert payload["data"]["intent"]["status"] == "awaiting_confirmation"
    assert not marker.exists()

    confirmed = client.post(
        f"/intents/{payload['data']['intent']['intent_id']}/confirm",
        headers=_control_headers(),
    )

    assert confirmed.status_code == 200
    assert confirmed.json()["success"] is True
    assert marker.read_text(encoding="utf-8") == "confirmed"


def test_full_control_mutation_responses_are_public_redacted(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("AIASK_AGENT_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("AIASK_AGENT_ENABLE_HERMES_FULL", "1")
    monkeypatch.setenv("AIASK_AGENT_CONTROL_TOKEN", "secret")
    runtime = AgentRuntime(
        model_client=MockModelClient(),
        session_store=AgentSessionStore(tmp_path / "state.sqlite3"),
        max_iterations=2,
    )
    client = TestClient(create_app(runtime=runtime))

    responses = [
        client.post(
            "/v1/skills",
            headers=_control_headers(),
            json={
                "name": "secret-skill",
                "description": "Runtime skill api_key=skill-description-secret",
                "content": "---\ndescription: client_secret=skill-frontmatter-secret\n---\n\n# Secret Skill\n",
            },
        ),
        client.post(
            "/v1/plugins",
            headers=_control_headers(),
            json={
                "name": "secret-plugin",
                "manifest": {
                    "enabled": True,
                    "api_key": "plugin-upsert-api-key-secret",
                    "runner": {
                        "type": "http",
                        "url": "http://127.0.0.1/plugin?access_token=plugin-upsert-url-secret",
                        "headers": {"Authorization": "Bearer plugin-upsert-header-secret"},
                    },
                },
            },
        ),
        client.post(
            "/v1/webhooks",
            headers=_control_headers(),
            json={
                "name": "signed-alerts",
                "events": ["factory.done"],
                "prompt": "Handle {event}: {payload}",
                "secret": "webhook-create-secret",
            },
        ),
    ]

    encoded = ""
    for response in responses:
        assert response.status_code == 200
        payload = response.json()
        assert payload["success"] is True
        assert payload["secrets_redacted"] is True
        encoded += json.dumps(payload, ensure_ascii=False)
    for secret in (
        "skill-description-secret",
        "skill-frontmatter-secret",
        "plugin-upsert-api-key-secret",
        "plugin-upsert-url-secret",
        "plugin-upsert-header-secret",
        "webhook-create-secret",
    ):
        assert secret not in encoded


def test_control_plane_lists_redact_webhook_plugin_and_mcp_secret_payloads(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("AIASK_AGENT_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("AIASK_AGENT_ENABLE_HERMES_FULL", "1")
    monkeypatch.setenv("AIASK_AGENT_CONTROL_TOKEN", "secret")
    monkeypatch.setenv("AIASK_TEST_MCP_AUTH", "Bearer mcp-header-env-secret")
    mcp_config = tmp_path / "mcp_servers.json"
    mcp_config.write_text(
        json.dumps(
            {
                "servers": [
                    {
                        "name": "broker-secret",
                        "domain": "financial",
                        "transport": "streamable_http",
                        "url": "https://broker.example/mcp?access_token=mcp-url-secret",
                        "headers_from_env": {"Authorization": "AIASK_TEST_MCP_AUTH"},
                        "oauth": {
                            "client_id": "broker-client",
                            "client_secret": "mcp-oauth-client-secret",
                            "authorization_url": "https://auth.example/authorize?access_token=mcp-auth-url-secret",
                            "token_url": "https://auth.example/token?client_secret=mcp-token-url-secret",
                            "redirect_uri": "http://127.0.0.1/callback?refresh_token=mcp-redirect-secret",
                        },
                        "tools": [
                            {
                                "name": "quote",
                                "description": "quote Authorization: Bearer mcp-tool-desc-secret",
                                "parameters": {
                                    "type": "object",
                                    "properties": {"symbol": {"type": "string"}},
                                },
                                "examples": [{"api_key": "mcp-tool-example-secret"}],
                            }
                        ],
                        "resources": [
                            {
                                "uri": "aiask://positions?access_token=mcp-resource-url-secret",
                                "metadata": {"password": "mcp-resource-password-secret"},
                            }
                        ],
                        "prompts": [
                            {
                                "name": "risk-review",
                                "description": "prompt client_secret=mcp-prompt-secret",
                            }
                        ],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("AIASK_AGENT_MCP_CONFIG", str(mcp_config))
    plugin_dir = tmp_path / "home" / "plugins" / "secret-plugin"
    plugin_dir.mkdir(parents=True)
    plugin_dir.joinpath("plugin.json").write_text(
        json.dumps(
            {
                "name": "secret-plugin",
                "enabled": True,
                "api_key": "plugin-manifest-api-key-secret",
                "runner": {
                    "type": "http",
                    "url": "http://127.0.0.1/plugin?access_token=plugin-runner-url-secret",
                    "headers": {"Authorization": "Bearer plugin-runner-header-secret"},
                },
                "commands": [
                    {
                        "name": "audit",
                        "description": "command broker_token=plugin-command-desc-secret",
                        "runner": {"env": {"password": "plugin-command-password-secret"}},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    runtime = AgentRuntime(
        model_client=MockModelClient(),
        session_store=AgentSessionStore(tmp_path / "state.sqlite3"),
        max_iterations=2,
    )
    client = TestClient(create_app(runtime=runtime))
    webhook = client.post(
        "/v1/webhooks",
        headers=_control_headers(),
        json={
            "name": "signed-alerts",
            "events": ["factory.done"],
            "prompt": "Handle {event}: {payload}",
            "secret": "webhook-list-secret",
        },
    )
    assert webhook.status_code == 200
    webhook_intent_id = webhook.json()["data"]["intent"]["intent_id"]
    webhook_confirmed = client.post(f"/intents/{webhook_intent_id}/confirm", headers=_control_headers())
    assert webhook_confirmed.status_code == 200
    assert webhook_confirmed.json()["success"] is True

    responses = [
        client.get("/v1/webhooks", headers=_control_headers()),
        client.get("/v1/plugins", headers=_control_headers()),
        client.get("/v1/plugins/secret-plugin/commands", headers=_control_headers()),
        client.get("/v1/mcp/servers?all=true", headers=_control_headers()),
        client.get("/v1/mcp/tools?all=true", headers=_control_headers()),
        client.get("/v1/mcp/resources?all=true", headers=_control_headers()),
        client.get("/v1/mcp/prompts?all=true", headers=_control_headers()),
        client.get("/v1/mcp/oauth_status?all=true", headers=_control_headers()),
    ]

    encoded = ""
    for response in responses:
        assert response.status_code == 200
        payload = response.json()
        assert payload["secrets_redacted"] is True
        encoded += json.dumps(payload, ensure_ascii=False)
    for secret in (
        "webhook-list-secret",
        "plugin-manifest-api-key-secret",
        "plugin-runner-url-secret",
        "plugin-runner-header-secret",
        "plugin-command-desc-secret",
        "plugin-command-password-secret",
        "mcp-header-env-secret",
        "mcp-url-secret",
        "mcp-oauth-client-secret",
        "mcp-auth-url-secret",
        "mcp-token-url-secret",
        "mcp-redirect-secret",
        "mcp-tool-desc-secret",
        "mcp-tool-example-secret",
        "mcp-resource-url-secret",
        "mcp-resource-password-secret",
        "mcp-prompt-secret",
    ):
        assert secret not in encoded


def test_mcp_action_error_payload_redacts_secret_error_text(monkeypatch, tmp_path) -> None:
    config_path = tmp_path / "mcp_servers.json"
    config_path.write_text(
        json.dumps(
            {
                "servers": [
                    {
                        "name": "broker",
                        "domain": "financial",
                        "transport": "streamable_http",
                        "url": "http://127.0.0.1:8765/mcp",
                        "headers_from_env": {"Authorization": "AIASK_TEST_MCP_AUTH"},
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("AIASK_AGENT_MCP_CONFIG", str(config_path))
    monkeypatch.setenv("AIASK_TEST_MCP_AUTH", "Bearer mcp-header-secret")

    exc = RuntimeError(
        "401 unauthorized Authorization: Bearer mcp-error-secret "
        "broker_token=mcp-broker-secret password=mcp-password-secret"
    )
    payload = mcp_action_error_payload(action="resource", server_name="broker", exc=exc)

    encoded = json.dumps(payload, ensure_ascii=False)
    assert payload["success"] is False
    assert payload["secrets_redacted"] is True
    assert "mcp-error-secret" not in encoded
    assert "mcp-broker-secret" not in encoded
    assert "mcp-password-secret" not in encoded
    assert "mcp-header-secret" not in encoded
    assert payload["error"] == (
        "401 unauthorized Authorization: Bearer [redacted] "
        "broker_token=[redacted] password=[redacted]"
    )
    assert payload["data"]["detail"] == payload["error"]
    assert payload["data"]["auth_configured"] is True
    assert payload["data"]["auth_env_vars"] == ["AIASK_TEST_MCP_AUTH"]


def test_mcp_success_routes_redact_secret_payloads(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("AIASK_AGENT_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("AIASK_AGENT_ENABLE_HERMES_FULL", "1")
    monkeypatch.setenv("AIASK_AGENT_CONTROL_TOKEN", "secret")

    async def discover_and_update(self, server_name: str) -> dict:
        return {
            "server": server_name,
            "discovered": {
                "tools": [
                    {
                        "name": "quote",
                        "description": "Authorization: Bearer discover-tool-secret",
                        "metadata": {"api_key": "discover-api-key-secret"},
                    }
                ],
                "warnings": [{"detail": "password=discover-password-secret"}],
            },
            "registration": {
                "url": "https://broker.example/mcp?access_token=discover-registration-token-secret",
            },
        }

    async def read_resource(self, server_name: str, uri: str) -> dict:
        return {
            "server": server_name,
            "uri": uri,
            "result": {
                "api_key": "resource-api-key-secret",
                "contents": [
                    {
                        "text": (
                            "Authorization: Bearer resource-bearer-secret "
                            "refresh_token=resource-refresh-secret"
                        )
                    }
                ],
            },
        }

    async def get_prompt(self, server_name: str, prompt_name: str, arguments: dict | None = None) -> dict:
        return {
            "server": server_name,
            "name": prompt_name,
            "arguments": dict(arguments or {}),
            "result": {
                "broker_token": "prompt-broker-secret",
                "messages": [{"content": {"text": "client_secret=prompt-client-secret"}}],
            },
        }

    def oauth_start(self, server_name: str, *, redirect_uri: str | None = None, scope: str | None = None) -> dict:
        return {
            "server": server_name,
            "status": "oauth_required",
            "authorization_url": "https://auth.example/authorize?access_token=oauth-start-token-secret&safe=1",
            "token_url": "https://auth.example/token?client_secret=oauth-token-url-secret",
            "redirect_uri": "http://127.0.0.1/callback?refresh_token=oauth-redirect-secret",
            "client_secret": "oauth-client-secret",
            "token_available": False,
        }

    def oauth_callback(self, server_name: str, payload: dict) -> dict:
        return {
            "server": server_name,
            "status": "stored",
            "access_token": "oauth-callback-access-secret",
            "note": "Authorization: Bearer oauth-callback-bearer-secret",
        }

    monkeypatch.setattr("aiask_agent.mcp_client.MCPAggregator.discover_and_update", discover_and_update)
    monkeypatch.setattr("aiask_agent.mcp_client.MCPAggregator.read_resource", read_resource)
    monkeypatch.setattr("aiask_agent.mcp_client.MCPAggregator.get_prompt", get_prompt)
    monkeypatch.setattr("aiask_agent.mcp_client.MCPAggregator.oauth_start", oauth_start)
    monkeypatch.setattr("aiask_agent.mcp_client.MCPAggregator.oauth_callback", oauth_callback)

    runtime = AgentRuntime(
        model_client=MockModelClient(),
        session_store=AgentSessionStore(tmp_path / "state.sqlite3"),
        max_iterations=2,
    )
    client = TestClient(create_app(runtime=runtime))
    discover_intent = client.post("/v1/mcp/discover", headers=_control_headers(), json={"server": "broker"})
    discover_confirmed = _confirm_intent_response(
        client,
        discover_intent,
        object_name="mcp.discover_update_intent",
        action="mcp.discover_update",
    )
    oauth_callback_intent = client.post(
        "/v1/mcp/oauth/callback",
        headers=_control_headers(),
        json={"server": "broker", "access_token": "oauth-request-access-secret"},
    )
    oauth_callback_confirmed = _confirm_intent_response(
        client,
        oauth_callback_intent,
        object_name="mcp.oauth_callback_intent",
        action="mcp.oauth_callback",
    )
    responses = [
        discover_intent,
        oauth_callback_intent,
        client.post(
            "/v1/mcp/resources/read",
            headers=_control_headers(),
            json={"server": "broker", "uri": "aiask://positions?access_token=request-uri-secret"},
        ),
        client.post(
            "/v1/mcp/prompts/get",
            headers=_control_headers(),
            json={
                "server": "broker",
                "name": "risk-review",
                "arguments": {"password": "prompt-request-password-secret"},
            },
        ),
        client.post("/v1/mcp/oauth/start", headers=_control_headers(), json={"server": "broker"}),
    ]

    encoded = json.dumps(discover_confirmed, ensure_ascii=False)
    encoded += json.dumps(oauth_callback_confirmed, ensure_ascii=False)
    for response in responses:
        assert response.status_code == 200
        payload = response.json()
        assert payload["secrets_redacted"] is True
        encoded += json.dumps(payload, ensure_ascii=False)
    assert "https://auth.example/authorize?access_token=[redacted]&safe=1" in encoded
    assert "https://auth.example/token?client_secret=[redacted]" in encoded
    assert "http://127.0.0.1/callback?refresh_token=[redacted]" in encoded
    for secret in (
        "discover-tool-secret",
        "discover-api-key-secret",
        "discover-password-secret",
        "discover-registration-token-secret",
        "resource-api-key-secret",
        "resource-bearer-secret",
        "resource-refresh-secret",
        "request-uri-secret",
        "prompt-broker-secret",
        "prompt-client-secret",
        "prompt-request-password-secret",
        "oauth-start-token-secret",
        "oauth-token-url-secret",
        "oauth-redirect-secret",
        "oauth-client-secret",
        "oauth-callback-access-secret",
        "oauth-callback-bearer-secret",
        "oauth-request-access-secret",
    ):
        assert secret not in encoded


def test_mcp_discovery_http_failure_redacts_secret_detail(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("AIASK_AGENT_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("AIASK_AGENT_ENABLE_HERMES_FULL", "1")
    monkeypatch.setenv("AIASK_AGENT_CONTROL_TOKEN", "secret")

    async def fail_discovery(self, server_name: str) -> dict:
        raise PermissionError(
            f"server {server_name} failed Authorization: Bearer discover-error-secret "
            "broker_token=discover-broker-secret"
        )

    def auth_readiness(self, server_name: str | None = None) -> dict:
        return {
            "auth_configured": False,
            "auth_mode": "env",
            "auth_env_vars": ["AIASK_DISCOVER_AUTH"],
            "missing_auth_env_vars": ["AIASK_DISCOVER_AUTH"],
            "debug": {"password": "discover-auth-secret"},
        }

    monkeypatch.setattr("aiask_agent.mcp_client.MCPAggregator.discover_and_update", fail_discovery)
    monkeypatch.setattr("aiask_agent.mcp_client.MCPAggregator.auth_readiness", auth_readiness)

    runtime = AgentRuntime(
        model_client=MockModelClient(),
        session_store=AgentSessionStore(tmp_path / "state.sqlite3"),
        max_iterations=2,
    )
    client = TestClient(create_app(runtime=runtime))
    response = client.post(
        "/v1/mcp/discover",
        headers=_control_headers(),
        json={"server": "broker"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["object"] == "mcp.discover_update_intent"
    assert payload["success"] is True
    assert payload["secrets_redacted"] is True
    assert payload["data"]["intent"]["action"] == "mcp.discover_update"
    assert payload["data"]["intent"]["status"] == "awaiting_confirmation"

    confirmed = client.post(
        f"/intents/{payload['data']['intent']['intent_id']}/confirm",
        headers=_control_headers(),
    )
    assert confirmed.status_code == 200
    confirmed_payload = confirmed.json()
    encoded = json.dumps(confirmed_payload, ensure_ascii=False)
    assert confirmed_payload["success"] is False
    assert confirmed_payload["secrets_redacted"] is True
    assert confirmed_payload["data"]["intent"]["action"] == "mcp.discover_update"
    assert confirmed_payload["data"]["intent"]["status"] == "failed"
    execution = confirmed_payload["data"]["execution_result"]
    assert execution["success"] is False
    assert execution["error_code"] == "MCP_DISCOVERY_AUTH_REQUIRED"
    assert execution["data"]["debug"]["password"] == "[redacted]"
    assert execution["data"]["detail"] == execution["error"]
    assert "discover-error-secret" not in encoded
    assert "discover-broker-secret" not in encoded
    assert "discover-auth-secret" not in encoded


def test_shell_style_pre_tool_call_hook_can_block_tool(tmp_path, monkeypatch) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("AIASK_AGENT_HOME", str(home))
    plugin_dir = home / "plugins" / "guard-hook"
    plugin_dir.mkdir(parents=True)
    plugin_dir.joinpath("guard.py").write_text(
        "import json, sys\n"
        "payload=json.loads(sys.stdin.read())\n"
        "print(json.dumps({'decision':'block','reason':'blocked '+payload['arguments']['tool_name']}))\n",
        encoding="utf-8",
    )
    plugin_dir.joinpath("plugin.json").write_text(
        json.dumps(
            {
                "name": "guard-hook",
                "enabled": True,
                "hooks": [
                    {
                        "name": "pre_tool_call",
                        "tool": "agent_terminal",
                        "command": [sys.executable, "guard.py"],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    store = AgentSessionStore(tmp_path / "state.sqlite3")
    runtime = AgentRuntime(
        model_client=MockModelClient(),
        session_store=store,
        tool_registry=build_default_tool_registry(
            session_store=store,
            policy_engine=ToolPolicyEngine(ToolPolicy("general_full", True, (str(tmp_path),))),
        ),
        max_iterations=2,
    )
    result = asyncio.run(runtime.run([{"role": "user", "content": 'tool:agent_terminal {"command":"pwd","cwd":"."}'}]))
    assert result.tool_calls[0]["result"]["error_code"] == "PLUGIN_HOOK_BLOCKED"
    events = [item["event"] for item in store.list_run_events(result.run_id)]
    assert "tool.blocked" in events
    assert "tool.started" not in events


def test_financial_system_readiness_gate_reports_required_blockers(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("AIASK_AGENT_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("AIASK_AGENT_ENABLE_HERMES_FULL", "1")
    monkeypatch.setenv("AIASK_FINANCE_THS_BROKER_TOKEN", "ths-readiness-secret")
    monkeypatch.setenv("AIASK_FINANCE_QMT_BROKER_TOKEN", "qmt-readiness-secret")
    monkeypatch.delenv("AIASK_AGENT_CONTROL_TOKEN", raising=False)
    monkeypatch.delenv("AIASK_LOCAL_CONTROL_TOKEN", raising=False)
    for key in (
        "AIASK_AGENT_MODEL_PROVIDER",
        "AIASK_AGENT_MODEL_PROVIDERS",
        "OPENAI_API_KEY",
        "OPENAI_API_KEYS",
        "OPENAI_BASE_URL",
    ):
        monkeypatch.delenv(key, raising=False)
    for key in quant.DATABASE_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("AKSHARE_MCP_SQLITE_PATH", "/dev/null/akshare_mcp.sqlite3")
    runtime = AgentRuntime(
        model_client=MockModelClient(),
        session_store=AgentSessionStore(tmp_path / "state.sqlite3"),
        max_iterations=2,
    )
    client = TestClient(create_app(runtime=runtime))

    response = client.get("/v1/financial-system/readiness")
    assert response.status_code == 200
    payload = response.json()
    assert payload["object"] == "aiask.financial_system_readiness"
    assert payload["status"] == "blocked"
    assert payload["backend_ready"] is False
    assert payload["product_ready"] is False
    assert payload["production_ready"] is False
    assert payload["summary"]["backend_ready"] is False
    assert payload["summary"]["product_ready"] is False
    assert payload["product"]["backend_ready"] is False
    assert payload["product"]["product_ready"] is False
    desktop_included = bool(payload["product"]["desktop_workbench"]["included"])
    expected_product_blockers = 0 if desktop_included else 1
    assert payload["summary"]["product_blockers"] == expected_product_blockers
    assert len(payload["product"]["blockers"]) == expected_product_blockers
    if desktop_included:
        assert payload["product"]["desktop_workbench"]["missing"] == []
    else:
        assert payload["product"]["blockers"][0]["code"] == "desktop_workbench_missing"
        assert "desktop/package.json" in payload["product"]["blockers"][0]["missing"]
    gates = {item["name"]: item for item in payload["required_gates"]}
    assert gates["ai_model"]["status"] == "degraded"
    assert gates["financial_action_safety"]["status"] == "ready"
    assert gates["financial_action_safety"]["evidence"]["live_trading_enabled"] is False
    assert gates["financial_action_safety"]["evidence"]["blocked_trade_actions"] == 2
    assert gates["financial_action_safety"]["evidence"]["broker_token_env_presence_only"] is True
    assert gates["financial_action_safety"]["evidence"]["control_token_configured"] is False
    assert gates["action_intent_reconciliation"]["status"] == "ready"
    assert gates["action_intent_reconciliation"]["evidence"]["execution_unknown"] == 0
    assert gates["control_plane"]["status"] == "blocked"
    assert gates["database"]["status"] == "blocked"
    assert gates["semantic_search"]["status"] == "ready"
    assert gates["semantic_search"]["evidence"]["memory_probe_success"] is True
    assert gates["semantic_search"]["evidence"]["session_probe_success"] is True
    assert gates["hermes_code_parity"]["status"] == "ready"
    optional_gates = {item["name"]: item for item in payload["optional_gates"]}
    assert optional_gates["vector_provider"]["status"] == "degraded"
    assert optional_gates["vector_provider"]["evidence"]["required_env"] == ["AIASK_VECTOR_MEMORY_URL"]
    assert payload["parity"]["core_code_status"] == "present"
    assert payload["parity"]["code_status"] == "present"
    assert payload["parity"]["v014_delta"]["missing_count"] == 0
    assert payload["parity"]["v016_delta"]["missing_count"] == 0
    assert payload["parity"]["v016_delta"]["release_tag"] == "v2026.6.5"
    matrix = payload["financial_safety_matrix"]
    assert payload["safety_matrix"] == matrix
    assert matrix["object"] == "aiask.desktop.financial_manager.safety_matrix"
    assert matrix["live_trading_enabled"] is False
    assert matrix["control_token_configured"] is False
    assert matrix["summary"]["control_token_ready"] is False
    assert matrix["summary"]["trade_risk_blocked"] == 2
    assert matrix["summary"]["dry_run_only"] == 2
    assert matrix["summary"]["broker_token_env_configured"] == 2
    assert matrix["broker_token_policy"]["env_presence"]["tonghuashun"] == {
        "env_var": "AIASK_FINANCE_THS_BROKER_TOKEN",
        "configured": True,
    }
    matrix_actions = {item["operation_id"]: item for item in matrix["actions"]}
    assert matrix_actions["portfolio.risk"]["direct_execution_allowed"] is True
    assert matrix_actions["portfolio.create"]["action_intent_required"] is True
    assert matrix_actions["portfolio.create"]["control_token_required"] is True
    assert matrix_actions["broker-live.place_order"]["live_trading_blocked"] is True
    assert "ths-readiness-secret" not in json.dumps(payload)
    assert "qmt-readiness-secret" not in json.dumps(payload)
    actions = {item["action_id"]: item for item in payload["next_actions"]}
    if desktop_included:
        assert "restore_aiask_desktop_workbench" not in actions
    else:
        assert actions["restore_aiask_desktop_workbench"]["priority"] == "critical"
    assert actions["set_control_token"]["priority"] == "critical"
    assert actions["configure_writable_database"]["target_page"] == "data"
    assert payload["live_smoke"]["script"] == "scripts/ops/live_readiness_smoke.py"
    assert payload["live_smoke"]["status"] == "blocked"
    assert payload["live_smoke"]["working_directory"] == "packages/agent"
    assert payload["live_smoke"]["self_test_command"].startswith("uv run python")
    assert "--self-test --pretty" in payload["live_smoke"]["self_test_command"]
    assert "--endpoint http://127.0.0.1:8765 --pretty" in payload["live_smoke"]["live_command"]
    assert "packages/agent" in payload["live_smoke"]["environment_note"]
    smoke_checks = {item["name"] for item in payload["live_smoke"]["checks"]}
    assert {
        "workbench_summary",
        "memory_status",
        "session_search",
        "memory_search",
        "factory_status",
        "market_temperature_cache",
        "market_temperature_forward_validation",
    } <= smoke_checks
    assert len(payload["live_smoke"]["checks"]) == 16
    smoke_check_details = {item["name"]: item for item in payload["live_smoke"]["checks"]}
    assert smoke_check_details["workbench_summary"]["path"] == "/v1/desktop/workbench/summary?session_limit=5&run_limit=5"
    assert "runtime_enabled" in smoke_check_details["factory_status"]["observes"]
    assert "daily_run_count" in smoke_check_details["factory_status"]["observes"]
    assert "warnings" in smoke_check_details["market_temperature_cache"]["observes"]
    assert "quality_status" in smoke_check_details["market_temperature_forward_validation"]["observes"]


def test_financial_system_readiness_can_reach_ready_with_core_runtime_config(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("AIASK_AGENT_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("AIASK_AGENT_ENABLE_HERMES_FULL", "1")
    monkeypatch.setenv("AIASK_AGENT_CONTROL_TOKEN", "secret")
    monkeypatch.setenv("AIASK_AGENT_MODEL_PROVIDER", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("AKSHARE_MCP_SQLITE_PATH", str(tmp_path / "akshare_mcp.sqlite3"))

    async def fake_call_db_facade(loader, params=None) -> dict:
        loader_name = getattr(loader, "__name__", "")
        if loader_name == "_load_factory_status_handler":
            return {"success": True, "data": {"status": "ready"}, "error": None}
        if loader_name == "_load_factory_runs_handler":
            return {"success": True, "data": {"runs": []}, "error": None}
        if loader_name == "_load_promotion_reviews_handler":
            return {"success": True, "data": {"reviews": []}, "error": None}
        return {"success": True, "data": {"loader": loader_name, "params": params}, "error": None}

    monkeypatch.setattr("aiask_agent.adapters.strategy_factory._call_db_facade", fake_call_db_facade)

    runtime = AgentRuntime(
        model_client=MockModelClient(),
        session_store=AgentSessionStore(tmp_path / "state.sqlite3"),
        max_iterations=2,
    )
    client = TestClient(create_app(runtime=runtime))

    response = client.get("/v1/financial-system/readiness")
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ready"
    assert payload["backend_ready"] is True
    desktop_included = bool(payload["product"]["desktop_workbench"]["included"])
    expected_product_blockers = 0 if desktop_included else 1
    assert payload["product_ready"] is desktop_included
    assert payload["production_ready"] is desktop_included
    assert payload["summary"]["backend_ready"] is True
    assert payload["summary"]["product_ready"] is desktop_included
    assert payload["summary"]["product_blockers"] == expected_product_blockers
    assert payload["product"]["backend_ready"] is True
    assert payload["product"]["product_ready"] is desktop_included
    assert len(payload["product"]["blockers"]) == expected_product_blockers
    if desktop_included:
        assert payload["product"]["desktop_workbench"]["missing"] == []
    else:
        assert payload["product"]["blockers"][0]["code"] == "desktop_workbench_missing"
    gates = {item["name"]: item for item in payload["required_gates"]}
    assert all(item["status"] == "ready" for item in gates.values())
    assert gates["financial_action_safety"]["evidence"]["live_trading_enabled"] is False
    assert gates["financial_action_safety"]["evidence"]["stateful_actions"] > 0
    assert gates["financial_action_safety"]["evidence"]["blocked_trade_actions"] == 2
    assert gates["action_intent_reconciliation"]["evidence"]["execution_unknown"] == 0
    optional_gates = {item["name"]: item for item in payload["optional_gates"]}
    assert optional_gates["vector_provider"]["status"] == "degraded"
    assert payload["summary"]["required_blocked"] == 0
    matrix = payload["financial_safety_matrix"]
    assert payload["safety_matrix"] == matrix
    assert matrix["control_token_configured"] is True
    assert matrix["live_trading_enabled"] is False
    assert matrix["live_trading_policy"] == "blocked_in_financial_manager_v1"
    assert matrix["broker_token_policy"]["live_tools_enabled_by_default"] is False
    assert matrix["summary"]["trade_risk_blocked"] == 2
    assert matrix["summary"]["dry_run_only"] == 2
    matrix_actions = {item["operation_id"]: item for item in matrix["actions"]}
    assert matrix_actions["paper.submit_order"]["execution_mode"] == "confirmed_execute_dry_run_only"
    assert matrix_actions["paper.submit_order"]["dry_run_required"] is True
    assert matrix_actions["broker-live.cancel_order"]["live_trading_blocked"] is True
    assert matrix_actions["broker-live.cancel_order"]["direct_execution_allowed"] is False
    actions = {item["action_id"]: item for item in payload["next_actions"]}
    if desktop_included:
        assert "restore_aiask_desktop_workbench" not in actions
    else:
        assert actions["restore_aiask_desktop_workbench"]["priority"] == "critical"
    assert "run_live_financial_workflow" in actions
    assert actions["run_live_financial_workflow"]["target_page"] == "financial-manager"
    assert payload["live_smoke"]["status"] == "ready"
    assert len(payload["live_smoke"]["checks"]) == 16
