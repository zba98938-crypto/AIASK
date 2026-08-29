from __future__ import annotations

import json

from fastapi.testclient import TestClient

from aiask_agent.model_client import MockModelClient
from aiask_agent.adapters.strategy_factory import _unavailable_factory
from aiask_agent.mcp_client import MCPAggregator
from aiask_agent.runtime import AgentRuntime
from aiask_agent.server import create_app
from aiask_agent.session_store import AgentSessionStore


def _runtime(tmp_path) -> AgentRuntime:
    return AgentRuntime(model_client=MockModelClient(), session_store=AgentSessionStore(tmp_path / "state.sqlite3"), max_iterations=2)


def _confirm_intent(client: TestClient, response, *, object_name: str, action: str) -> dict:
    assert response.status_code == 200
    payload = response.json()
    assert payload["object"] == object_name
    assert payload["success"] is True
    assert payload["data"]["intent"]["action"] == action
    assert payload["data"]["intent"]["status"] == "awaiting_confirmation"
    assert payload["meta"]["action_intent_required"] is True
    confirmed = client.post(
        f"/intents/{payload['data']['intent']['intent_id']}/confirm",
        headers={"Authorization": "Bearer secret"},
    )
    assert confirmed.status_code == 200
    confirmed_payload = confirmed.json()
    assert confirmed_payload["success"] is True
    assert confirmed_payload["data"]["intent"]["action"] == action
    assert confirmed_payload["data"]["intent"]["status"] == "succeeded"
    return confirmed_payload


def test_strategy_factory_database_error_is_classified(monkeypatch) -> None:
    monkeypatch.setenv("AKSHARE_MCP_SQLITE_PATH", "/tmp/aiask-test.sqlite3")
    payload = _unavailable_factory(RuntimeError("unable to open database file"))
    assert payload["error_code"] == "STRATEGY_FACTORY_DATABASE_UNAVAILABLE"
    assert payload["data"]["database_backend"] == "sqlite"
    assert payload["data"]["database_configured"] is True


def test_strategy_factory_db_env_and_recovery_are_classified(monkeypatch) -> None:
    monkeypatch.setenv("AKSHARE_MCP_SQLITE_PATH", "/tmp/aiask-test.sqlite3")

    payload = _unavailable_factory(RuntimeError("database is locked"))

    assert payload["error_code"] == "STRATEGY_FACTORY_DATABASE_RECOVERY"
    assert payload["data"]["database_configured"] is True
    assert "AKSHARE_MCP_SQLITE_PATH" in payload["data"]["database_config_sources"]


def test_desktop_capabilities_returns_public_subset_without_control_token(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("AIASK_AGENT_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("AIASK_AGENT_ENABLE_HERMES_FULL", "1")
    monkeypatch.setenv("AIASK_AGENT_CONTROL_TOKEN", "secret")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-secret-value")
    monkeypatch.delenv("MCP_PORT", raising=False)
    runtime = _runtime(tmp_path)
    client = TestClient(create_app(runtime=runtime))

    response = client.get("/v1/desktop/capabilities")
    assert response.status_code == 200
    payload = response.json()
    assert payload["object"] == "aiask.desktop_capabilities"
    assert payload["summary"]["control"]["authorized"] is False
    assert payload["summary"]["source"] == "gated"
    assert payload["summary"]["control"]["full_mode_enabled"] is True
    assert payload["summary"]["control"]["control_token_configured"] is True
    assert payload["summary"]["control"]["control_authorized"] is False
    assert payload["hermes"]["parity"]["baseline"] == "Hermes v0.16.0 full runtime capability reference"
    assert payload["hermes"]["parity"]["baseline_version"] == "0.16.0"
    assert payload["hermes"]["parity"]["baseline_release_tag"] == "v2026.6.5"
    assert payload["hermes"]["parity"]["core_missing_hermes_tools"] == []
    assert payload["hermes"]["parity"]["v014_delta"]["missing_count"] == 0
    assert payload["hermes"]["parity"]["v016_delta"]["missing_count"] == 0
    assert payload["hermes"]["readiness"]["baseline_version"] == "0.16.0"
    assert "OPENAI_API_KEY" in payload["hermes"]["readiness"]["live_evidence"]["required_env_names"]
    serialized = json.dumps(payload)
    assert "OPENAI_API_KEY" in serialized
    assert "sk-test-secret-value" not in serialized
    detailed_health = client.get("/health/detailed").json()
    health_serialized = json.dumps(detailed_health)
    assert "OPENAI_API_KEY" in health_serialized
    assert "sk-test-secret-value" not in health_serialized
    assert payload["mcp"]["gated"] is True
    assert payload["mcp"]["registration_status"] == "not_registered"
    assert payload["mcp"]["discovery_status"] == "not_registered"
    assert payload["skills"]["gated"] is True
    assert payload["providers"]["secrets_redacted"] is True
    assert payload["memory"]["default_provider"] == "sqlite"
    assert payload["security"]["status"] == "implemented"
    assert payload["skill_packs"]["vendor_text_copied"] is False
    assert payload["strategy_factory"]["status"]["success"] is False
    assert payload["ai"]["secrets_redacted"] is True


def test_desktop_capabilities_reports_mcp_port_without_aggregator_config(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("AIASK_AGENT_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("AIASK_AGENT_ENABLE_HERMES_FULL", "1")
    monkeypatch.setenv("AIASK_AGENT_CONTROL_TOKEN", "secret")
    monkeypatch.setenv("MCP_PORT", "3100")
    monkeypatch.delenv("AIASK_AGENT_MCP_CONFIG", raising=False)
    runtime = _runtime(tmp_path)
    client = TestClient(create_app(runtime=runtime))

    response = client.get("/v1/desktop/capabilities", headers={"Authorization": "Bearer secret"})
    assert response.status_code == 200
    payload = response.json()
    assert payload["summary"]["source"] == "live_backend"
    assert payload["mcp"]["registration_status"] == "MCP_SERVICE_RUNNING_BUT_NOT_REGISTERED"
    assert payload["mcp"]["discovery_status"] == "MCP_SERVICE_RUNNING_BUT_NOT_REGISTERED"
    assert payload["mcp"]["error_code"] == "MCP_SERVICE_RUNNING_BUT_NOT_REGISTERED"
    assert payload["mcp"]["detected_service_port"] == "3100"
    assert payload["mcp"]["suggested_registration_url"] == "http://127.0.0.1:3100/mcp"
    assert payload["mcp"]["auth_env_vars"] == []
    assert payload["mcp"]["missing_auth_env_vars"] == []
    assert "mcp" in payload["hermes"]["readiness"]
    assert payload["hermes"]["readiness"]["mcp"]["registration_status"] == "MCP_SERVICE_RUNNING_BUT_NOT_REGISTERED"


def test_desktop_capabilities_aggregates_controlled_surfaces(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("AIASK_AGENT_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("AIASK_AGENT_ENABLE_HERMES_FULL", "1")
    monkeypatch.setenv("AIASK_AGENT_CONTROL_TOKEN", "secret")
    monkeypatch.setenv("MCP_PORT", "3100")
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
                        "tools": [{"name": "quote", "description": "quote tool"}],
                        "resources": [{"uri": "aiask://quotes"}],
                        "prompts": [{"name": "risk-review"}],
                        "auth": "oauth",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("AIASK_AGENT_MCP_CONFIG", str(mcp_config))
    runtime = _runtime(tmp_path)
    client = TestClient(create_app(runtime=runtime))
    create_skill = client.post(
        "/v1/skills",
        headers={"Authorization": "Bearer secret"},
        json={"name": "risk-review", "description": "Risk review", "content": "# Risk Review\n"},
    )
    _confirm_intent(
        client,
        create_skill,
        object_name="skill.install_intent",
        action="skill.install",
    )

    response = client.get("/v1/desktop/capabilities", headers={"Authorization": "Bearer secret"})
    assert response.status_code == 200
    payload = response.json()
    assert payload["summary"]["control"]["authorized"] is True
    assert payload["summary"]["source"] == "live_backend"
    assert payload["mcp"]["gated"] is False
    assert payload["mcp"]["registration_status"] == "registered"
    assert payload["mcp"]["discovery_status"] == "discovered"
    assert payload["mcp"]["config_path"] == str(mcp_config)
    assert payload["mcp"]["auth_configured"] is True
    assert payload["mcp"]["tools"][0]["name"] == "quote"
    assert payload["mcp"]["resources"][0]["uri"] == "aiask://quotes"
    assert any(item["name"] == "risk-review" for item in payload["skills"]["skills"])
    assert payload["hermes"]["providers"]["object"] == "aiask.model_provider_status"
    assert payload["hermes"]["acp"]["implemented"] is True
    assert payload["hermes"]["skill_packs"]["source"] == "aiask_native_rewrite"
    assert payload["hermes"]["status"]["full_mode_active"] is True
    assert "ai_status" in payload["raw_refs"]


def test_connectors_financial_payload_blocks_live_trade_tools(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("AIASK_AGENT_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("AIASK_AGENT_ENABLE_HERMES_FULL", "1")
    monkeypatch.setenv("AIASK_AGENT_CONTROL_TOKEN", "secret")
    runtime = _runtime(tmp_path)
    client = TestClient(create_app(runtime=runtime))

    response = client.get("/v1/connectors?type=financial", headers={"Authorization": "Bearer secret"})

    assert response.status_code == 200
    payload = response.json()
    connectors = {item["id"]: item for item in payload["data"]}
    ths = connectors["financial:tonghuashun"]["metadata"]
    qmt = connectors["financial:qmt"]["metadata"]
    tdx = connectors["financial:tongdaxin"]["metadata"]

    for metadata in (ths, qmt, tdx):
        assert metadata["tools_trade"] == []
        assert metadata["tools_trade_executable"] == []
        assert metadata["live_trading_enabled"] is False
        assert metadata["trade_direct_execution_allowed"] is False
        assert metadata["live_trading_policy"] == "blocked_in_agent_connector_surface"
        assert metadata["side_effect_policy"]["blocked_tools_level"] == "trade_risk"

    assert set(ths["tools_trade_blocked"]) == {"ths_place_order", "ths_cancel_order"}
    assert set(qmt["tools_trade_blocked"]) == {"qmt_place_order", "qmt_cancel_order"}
    assert {"tdx_place_order", "tdx_cancel_order"} <= set(tdx["tools_trade_blocked"])
    assert {"tdx_query_position", "tdx_query_orders", "tdx_query_balance"} <= set(tdx["tools_read"])
    assert "qmt_query_orders" in qmt["tools_read"]


def test_mcp_tools_all_marks_live_broker_tools_as_trade_risk(tmp_path, monkeypatch) -> None:
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
                            {"name": "quote", "description": "read-only quote", "side_effect": {"level": "read_only"}},
                            {"name": "ths_place_order", "description": "live broker order"},
                            {
                                "name": "broker_place_order",
                                "description": "badly labelled broker order",
                                "parameters": {
                                    "type": "object",
                                    "properties": {
                                        "code": {"type": "string"},
                                        "price": {"type": "number"},
                                        "volume": {"type": "integer"},
                                        "direction": {"type": "string"},
                                    },
                                    "required": ["code", "price", "volume", "direction"],
                                },
                                "side_effect": {"level": "read_only"},
                            },
                            {"name": "query_broker_orders", "description": "read-only broker order query", "side_effect": {"level": "read_only"}},
                        ],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("AIASK_AGENT_MCP_CONFIG", str(mcp_config))
    runtime = _runtime(tmp_path)
    client = TestClient(create_app(runtime=runtime))

    response = client.get("/v1/mcp/tools?all=true", headers={"Authorization": "Bearer secret"})

    assert response.status_code == 200
    tools = {item["name"]: item for item in response.json()["data"]}
    assert tools["quote"]["side_effect"]["level"] == "read_only"
    assert tools["query_broker_orders"]["side_effect"]["level"] == "read_only"
    assert tools["ths_place_order"]["side_effect"]["level"] == "trade_risk"
    assert tools["broker_place_order"]["side_effect"]["level"] == "trade_risk"
    assert tools["broker_place_order"]["side_effect"]["live_broker_tool"] is True


def test_mcp_register_local_writes_non_secret_aggregator_config(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("AIASK_AGENT_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("AIASK_AGENT_ENABLE_HERMES_FULL", "1")
    monkeypatch.setenv("AIASK_AGENT_CONTROL_TOKEN", "secret")
    monkeypatch.setenv("MCP_PORT", "3100")
    runtime = _runtime(tmp_path)
    client = TestClient(create_app(runtime=runtime))

    response = client.post("/v1/mcp/register-local", headers={"Authorization": "Bearer secret"}, json={})
    pending_capabilities = client.get("/v1/desktop/capabilities", headers={"Authorization": "Bearer secret"}).json()
    assert pending_capabilities["mcp"]["registration_status"] == "MCP_SERVICE_RUNNING_BUT_NOT_REGISTERED"

    confirmed = _confirm_intent(
        client,
        response,
        object_name="mcp.register_local_intent",
        action="mcp.register_local",
    )
    server = confirmed["data"]["execution_result"]["data"]["server"]
    assert server["name"] == "akshare-local"
    assert server["transport"] == "streamable_http"
    assert server["url"] == "http://127.0.0.1:3100/mcp"
    assert server["headers_from_env"]["Authorization"] == "[redacted]"
    assert "token" not in json.dumps(server).lower()

    config = json.loads((tmp_path / "home" / "mcp_servers.json").read_text(encoding="utf-8"))
    stored_server = config["servers"][0]
    assert stored_server["headers_from_env"]["Authorization"] == "AIASK_MCP_AKSHARE_LOCAL_AUTHORIZATION"
    assert "token" not in json.dumps(stored_server).lower()

    capabilities = client.get("/v1/desktop/capabilities", headers={"Authorization": "Bearer secret"}).json()
    assert capabilities["mcp"]["registration_status"] == "registered"
    assert capabilities["mcp"]["discovery_status"] == "auth_missing"
    assert capabilities["mcp"]["servers"][0]["name"] == "akshare-local"
    assert capabilities["mcp"]["auth_configured"] is False
    assert capabilities["mcp"]["auth_env_vars"] == ["AIASK_MCP_AKSHARE_LOCAL_AUTHORIZATION"]
    assert capabilities["mcp"]["missing_auth_env_vars"] == ["AIASK_MCP_AKSHARE_LOCAL_AUTHORIZATION"]


def test_mcp_env_auth_headers_are_loaded_from_environment(tmp_path, monkeypatch) -> None:
    mcp_config = tmp_path / "mcp_servers.json"
    mcp_config.write_text(
        json.dumps(
            {
                "servers": [
                    {
                        "name": "finance-demo",
                        "domain": "financial",
                        "transport": "streamable_http",
                        "url": "http://127.0.0.1:3100/mcp",
                        "headers_from_env": {"Authorization": "AIASK_TEST_MCP_AUTH"},
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("AIASK_TEST_MCP_AUTH", "Bearer secret-value")

    mcp = MCPAggregator(config_path=mcp_config)
    server = mcp.servers_summary(include_all=True)[0]

    assert server["auth_configured"] is True
    assert server["auth_env_vars"] == ["AIASK_TEST_MCP_AUTH"]
    assert server["missing_auth_env_vars"] == []
    assert MCPAggregator._headers({"headers_from_env": {"Authorization": "AIASK_TEST_MCP_AUTH"}}) == {
        "Authorization": "Bearer secret-value"
    }
