from __future__ import annotations

import asyncio
import json

from fastapi.testclient import TestClient

from aiask_agent.financial_payloads import financial_intent_payload_for_runtime, financial_query_payload_for_runtime
from aiask_agent.model_client import MockModelClient
from aiask_agent.runtime import AgentRuntime
from aiask_agent.server import create_app
from aiask_agent.session_store import AgentSessionStore
from aiask_agent.tool_registry import AgentToolRegistry, aiask_envelope


def _runtime(tmp_path) -> AgentRuntime:
    return AgentRuntime(
        model_client=MockModelClient(),
        session_store=AgentSessionStore(tmp_path / "state.sqlite3"),
        max_iterations=2,
    )


def _client(tmp_path, monkeypatch) -> TestClient:
    monkeypatch.setenv("AIASK_AGENT_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("AIASK_AGENT_CONTROL_TOKEN", "secret")
    return TestClient(create_app(runtime=_runtime(tmp_path)))


def _client_with_store(tmp_path, monkeypatch) -> tuple[TestClient, AgentSessionStore]:
    monkeypatch.setenv("AIASK_AGENT_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("AIASK_AGENT_CONTROL_TOKEN", "secret")
    runtime = _runtime(tmp_path)
    return TestClient(create_app(runtime=runtime)), runtime.session_store


def test_financial_manager_catalog_and_status_redact_secrets(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-financial-manager-secret")
    monkeypatch.setenv("AIASK_FINANCE_THS_BROKER_TOKEN", "ths-broker-secret")
    monkeypatch.setenv("AIASK_FINANCE_QMT_BROKER_TOKEN", "qmt-broker-secret")
    client = _client(tmp_path, monkeypatch)

    catalog = client.get("/v1/finance/manager/catalog")
    status = client.get("/v1/finance/manager/status")

    assert catalog.status_code == 200
    assert status.status_code == 200
    catalog_payload = catalog.json()
    status_payload = status.json()
    assert catalog_payload["safety"]["live_trading_enabled"] is False
    assert any(item["mode"] == "blocked" for item in catalog_payload["actions"])
    risk_action = next(item for item in catalog_payload["actions"] if item["capability_id"] == "portfolio" and item["action_id"] == "risk")
    assert risk_action["availability"]["reason_code"] == "agent_tool_ready"
    reports_action = next(item for item in catalog_payload["actions"] if item["capability_id"] == "research" and item["action_id"] == "reports")
    assert reports_action["status"] == "missing_mcp_tool"
    assert reports_action["availability"]["reason_code"] == "no_financial_mcp_server_registered"
    matrix = catalog_payload["safety_matrix"]
    assert matrix["object"] == "aiask.financial_manager.safety_matrix"
    assert matrix["live_trading_enabled"] is False
    assert matrix["live_trading_policy"] == "blocked_in_financial_manager_v1"
    assert matrix["control_token_configured"] is True
    assert matrix["summary"]["total_actions"] == len(catalog_payload["actions"])
    assert matrix["summary"]["read_only"] > matrix["summary"]["stateful_intent"] > 0
    assert matrix["summary"]["trade_risk_blocked"] == 2
    assert matrix["summary"]["dry_run_only"] == 2
    assert matrix["summary"]["broker_token_env_configured"] == 2
    assert catalog_payload["safety"]["matrix"] == matrix
    assert status_payload["safety_matrix"] == matrix
    assert status_payload["broker"]["token_env_presence"] == matrix["broker_token_policy"]["env_presence"]
    assert matrix["broker_token_policy"]["explicit_token_required_for_live_mcp"] is True
    assert set(matrix["broker_token_policy"]["guarded_tools"]) == {
        "ths_place_order",
        "ths_cancel_order",
        "qmt_place_order",
        "qmt_cancel_order",
    }
    assert matrix["broker_token_policy"]["env_presence"]["tonghuashun"] == {
        "env_var": "AIASK_FINANCE_THS_BROKER_TOKEN",
        "configured": True,
    }
    assert matrix["broker_token_policy"]["env_presence"]["qmt"] == {
        "env_var": "AIASK_FINANCE_QMT_BROKER_TOKEN",
        "configured": True,
    }
    matrix_actions = {item["operation_id"]: item for item in matrix["actions"]}
    assert matrix_actions["portfolio.risk"]["read_only"] is True
    assert matrix_actions["portfolio.risk"]["direct_execution_allowed"] is True
    assert matrix_actions["portfolio.risk"]["action_intent_required"] is False
    assert matrix_actions["portfolio.create"]["action_intent_required"] is True
    assert matrix_actions["portfolio.create"]["control_token_required"] is True
    assert matrix_actions["portfolio.create"]["endpoint"] == "/v1/finance/manager/intent"
    assert matrix_actions["paper.submit_order"]["dry_run_required"] is True
    assert matrix_actions["paper.submit_order"]["execution_mode"] == "confirmed_execute_dry_run_only"
    assert matrix_actions["broker-live.place_order"]["live_trading_blocked"] is True
    assert matrix_actions["broker-live.place_order"]["broker_token_required"] is True
    assert matrix_actions["broker-live.place_order"]["risk_level"] == "trade_risk"
    assert "broker" in status_payload
    raw = json.dumps({"catalog": catalog_payload, "status": status_payload})
    assert "sk-financial-manager-secret" not in raw
    assert "ths-broker-secret" not in raw
    assert "qmt-broker-secret" not in raw


def test_financial_manager_read_only_query_dispatches_agent_tool(tmp_path, monkeypatch) -> None:
    client = _client(tmp_path, monkeypatch)

    response = client.post(
        "/v1/finance/manager/query",
        json={
            "capability_id": "portfolio",
            "action_id": "risk",
            "params": {"codes": ["600519", "000001"], "weights": [0.6, 0.4]},
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["object"] == "aiask.financial_manager.query"
    assert payload["tool"] == "agent_portfolio_risk"
    assert payload["success"] is True
    assert payload["meta"]["side_effect"]["level"] == "read_only"


def test_financial_manager_read_only_query_blocks_non_read_only_tool_result(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("AIASK_AGENT_HOME", str(tmp_path / "home"))
    session_store = AgentSessionStore(tmp_path / "state.sqlite3")
    registry = AgentToolRegistry()

    async def mislabelled_portfolio_risk(arguments: dict[str, object]) -> dict[str, object]:
        return aiask_envelope(
            True,
            data={
                "unexpected_mutation_marker": "should-not-leak",
                "codes": arguments.get("codes"),
            },
            error=None,
            tool_name="agent_portfolio_risk",
            source_chain=["test", "mislabelled_portfolio_risk"],
            side_effect={
                "level": "stateful",
                "target": "portfolio_manager.risk_write",
                "confirmation_required": True,
                "idempotent": False,
            },
        )

    registry.register(
        "agent_portfolio_risk",
        description="Mislabelled portfolio risk query",
        parameters={"type": "object", "properties": {}},
        handler=mislabelled_portfolio_risk,
        metadata={"category": "financial_read", "side_effect": "read_only"},
    )
    runtime = AgentRuntime(
        model_client=MockModelClient(),
        session_store=session_store,
        tool_registry=registry,
        max_iterations=2,
    )
    client = TestClient(create_app(runtime=runtime))
    session_id = session_store.create_session(session_id="sess_fin_query_guard", user_id="local")

    response = client.post(
        "/v1/finance/manager/query",
        json={
            "user_id": "local",
            "session_id": session_id,
            "capability_id": "portfolio",
            "action_id": "risk",
            "params": {"codes": ["600519"], "weights": [1.0]},
        },
    )

    assert response.status_code == 200
    payload = response.json()
    raw = json.dumps(payload, ensure_ascii=False)
    assert payload["object"] == "aiask.financial_manager.query"
    assert payload["tool"] == "agent_portfolio_risk"
    assert payload["success"] is False
    assert payload["error_code"] == "READ_ONLY_TOOL_RESULT_BLOCKED"
    assert payload["data"]["side_effect"]["level"] == "stateful"
    assert payload["meta"]["read_only_desktop_guard"]["blocked"] is True
    assert "unexpected_mutation_marker" not in raw
    assert "should-not-leak" not in raw

    invocations = session_store.list_tool_invocations(session_id=session_id)
    assert len(invocations) == 1
    invocation = invocations[0]
    audit_raw = json.dumps(invocation, ensure_ascii=False)
    assert invocation["tool_name"] == "agent_portfolio_risk"
    assert invocation["status"] == "failed"
    assert invocation["error_code"] == "READ_ONLY_TOOL_RESULT_BLOCKED"
    assert invocation["output_summary"]["data"]["side_effect"]["level"] == "stateful"
    assert "unexpected_mutation_marker" not in audit_raw
    assert "should-not-leak" not in audit_raw


def test_financial_manager_query_and_intent_are_audited(tmp_path, monkeypatch) -> None:
    client, store = _client_with_store(tmp_path, monkeypatch)
    session_id = store.create_session(session_id="sess_financial_audit", user_id="local")

    query = client.post(
        "/v1/finance/manager/query",
        json={
            "user_id": "local",
            "session_id": session_id,
            "capability_id": "portfolio",
            "action_id": "risk",
            "params": {"codes": ["600519", "000001"], "weights": [0.6, 0.4]},
        },
    )
    intent = client.post(
        "/v1/finance/manager/intent",
        headers={"Authorization": "Bearer secret"},
        json={
            "user_id": "local",
            "session_id": session_id,
            "capability_id": "portfolio",
            "action_id": "create",
            "params": {"name": "Audited book"},
            "rationale": "audit test",
        },
    )

    assert query.status_code == 200
    assert intent.status_code == 200
    invocations = store.list_tool_invocations(session_id=session_id)
    assert [item["tool_name"] for item in invocations] == ["agent_action_intent_create", "agent_portfolio_risk"]
    assert invocations[0]["action_intent_id"] == intent.json()["data"]["intent"]["intent_id"]
    assert invocations[0]["source_chain"] == ["aiask_agent.server", "finance.manager.intent"]
    assert invocations[1]["source_chain"] == ["aiask_agent.server", "finance.manager"]


def test_financial_manager_payloads_redact_free_text_secrets(tmp_path, monkeypatch) -> None:
    runtime = _runtime(tmp_path)

    async def fake_query_tool(tool_name: str, arguments: dict[str, object]) -> dict[str, object]:
        return {
            "success": False,
            "data": {
                "note": "broker_token=query-secret",
                "callback": "https://broker.local/cb?password=query-url-secret&ok=1",
                "headers": {"Authorization": "Bearer query-header-secret-123456"},
            },
            "error": "failed with Authorization: Bearer query-error-secret-123456",
            "meta": {
                "diagnostic": "token=query-meta-secret",
                "side_effect": {"level": "read_only"},
            },
        }

    async def fake_intent_tool(tool_name: str, arguments: dict[str, object]) -> dict[str, object]:
        return {
            "success": True,
            "data": {
                "intent": {
                    "intent_id": "intent_test",
                    "status": "awaiting_confirmation",
                    "rationale": "password=intent-rationale-secret",
                    "params": {"broker_token": "intent-param-secret"},
                }
            },
            "error": None,
            "meta": {"debug": "Authorization: Bearer intent-meta-secret-123456"},
        }

    query = asyncio.run(
        financial_query_payload_for_runtime(
            runtime,
            {
                "capability_id": "portfolio",
                "action_id": "risk",
                "params": {"codes": ["600519"], "weights": [1.0]},
            },
            tool_caller=fake_query_tool,
        )
    )
    intent = asyncio.run(
        financial_intent_payload_for_runtime(
            runtime,
            {
                "capability_id": "portfolio",
                "action_id": "create",
                "params": {"name": "Desk book"},
                "rationale": "broker_token=request-secret",
            },
            tool_caller=fake_intent_tool,
        )
    )

    raw = json.dumps({"query": query, "intent": intent})
    for leaked in (
        "query-secret",
        "query-url-secret",
        "query-header-secret",
        "query-error-secret",
        "query-meta-secret",
        "intent-rationale-secret",
        "intent-param-secret",
        "intent-meta-secret",
    ):
        assert leaked not in raw
    assert query["data"]["note"] == "broker_token=[redacted]"
    assert query["error"] == "failed with Authorization: Bearer [redacted]"
    assert intent["data"]["intent"]["params"]["broker_token"] == "[REDACTED]"
    assert intent["data"]["intent"]["rationale"] == "password=[redacted]"


def test_financial_manager_missing_mcp_tool_returns_availability_reason(tmp_path, monkeypatch) -> None:
    client = _client(tmp_path, monkeypatch)

    response = client.post(
        "/v1/finance/manager/query",
        json={
            "capability_id": "research",
            "action_id": "reports",
            "params": {"code": "600519", "limit": 5},
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["success"] is False
    assert payload["error_code"] == "FINANCIAL_TOOL_UNAVAILABLE"
    assert payload["data"]["availability"]["required_mcp_tool"] == "research_manager"
    assert payload["data"]["availability"]["reason_code"] == "no_financial_mcp_server_registered"


def test_financial_manager_internal_read_only_mcp_does_not_expose_manager_tool(tmp_path, monkeypatch) -> None:
    config_path = tmp_path / "mcp_servers.json"
    config_path.write_text(
        json.dumps(
            {
                "servers": [
                    {
                        "name": "akshare-demo",
                        "domain": "financial",
                        "transport": "stdio",
                        "command": "dummy",
                        "tools": [
                            {
                                "name": "research_manager",
                                "description": "raw multi-action research manager",
                                "parameters": {"type": "object", "properties": {}},
                                "side_effect": {"level": "stateful"},
                            }
                        ],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("AIASK_AGENT_MCP_CONFIG", str(config_path))
    runtime = _runtime(tmp_path)
    captured: dict[str, object] = {}

    class FakeSession:
        async def call_tool(self, tool_name: str, arguments: dict[str, object]) -> dict[str, object]:
            captured["tool_name"] = tool_name
            captured["arguments"] = dict(arguments)
            return {"success": True, "data": {"reports": [{"code": "600519"}]}, "error": None}

    async def fake_with_session(self, server, operation):
        captured["server"] = dict(server)
        return await operation(FakeSession())

    monkeypatch.setattr("aiask_agent.mcp_client.MCPAggregator._with_session", fake_with_session)

    assert "agent_mcp_akshare_demo_research_manager" not in set(runtime.tool_registry.names())
    catalog = client_payload = None
    from aiask_agent.financial_payloads import financial_catalog_payload_for_runtime

    catalog = financial_catalog_payload_for_runtime(runtime)
    reports_action = next(item for item in catalog["actions"] if item["capability_id"] == "research" and item["action_id"] == "reports")
    assert reports_action["available"] is True
    assert reports_action["availability"]["reason_code"] == "desktop_internal_mcp_tool_ready"

    client_payload = asyncio.run(
        financial_query_payload_for_runtime(
            runtime,
            {
                "user_id": "local",
                "session_id": runtime.session_store.create_session(session_id="sess_internal_mcp", user_id="local"),
                "capability_id": "research",
                "action_id": "reports",
                "params": {"code": "600519", "action": "delete_all"},
            },
        )
    )

    assert client_payload["success"] is True
    assert client_payload["tool"] == "desktop_internal_mcp.research_manager"
    assert captured["tool_name"] == "research_manager"
    assert captured["arguments"]["action"] == "get_reports"
    assert captured["arguments"]["params"]["code"] == "600519"
    assert "action" not in captured["arguments"]["params"]
    assert client_payload["meta"]["side_effect"]["level"] == "read_only"
    invocations = runtime.session_store.list_tool_invocations(session_id="sess_internal_mcp")
    assert invocations[0]["tool_name"] == "desktop_internal_mcp.research_manager"
    assert invocations[0]["source_chain"] == ["aiask_agent.server", "financial_manager.internal_mcp"]


def test_financial_manager_internal_mcp_blocks_non_read_only_envelope_result(tmp_path, monkeypatch) -> None:
    config_path = tmp_path / "mcp_servers.json"
    config_path.write_text(
        json.dumps(
            {
                "servers": [
                    {
                        "name": "akshare-demo",
                        "domain": "financial",
                        "transport": "stdio",
                        "command": "dummy",
                        "tools": [
                            {
                                "name": "research_manager",
                                "description": "raw multi-action research manager",
                                "parameters": {"type": "object", "properties": {}},
                                "side_effect": {"level": "stateful"},
                            }
                        ],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("AIASK_AGENT_MCP_CONFIG", str(config_path))
    runtime = _runtime(tmp_path)

    class FakeSession:
        async def call_tool(self, tool_name: str, arguments: dict[str, object]) -> dict[str, object]:
            return {
                "success": True,
                "data": {
                    "unexpected_mutation_marker": "should-not-leak",
                    "reports": [{"code": "600519"}],
                },
                "error": None,
                "meta": {
                    "side_effect": {
                        "level": "stateful",
                        "target": "research_manager.write_cache",
                        "confirmation_required": True,
                        "idempotent": False,
                    },
                    "source_chain": ["test", "internal_mcp"],
                },
            }

    async def fake_with_session(self, server, operation):
        return await operation(FakeSession())

    monkeypatch.setattr("aiask_agent.mcp_client.MCPAggregator._with_session", fake_with_session)
    session_id = runtime.session_store.create_session(session_id="sess_internal_mcp_guard", user_id="local")

    payload = asyncio.run(
        financial_query_payload_for_runtime(
            runtime,
            {
                "user_id": "local",
                "session_id": session_id,
                "capability_id": "research",
                "action_id": "reports",
                "params": {"code": "600519"},
            },
        )
    )

    raw = json.dumps(payload, ensure_ascii=False)
    assert payload["success"] is False
    assert payload["tool"] == "desktop_internal_mcp.research_manager"
    assert payload["error_code"] == "READ_ONLY_TOOL_RESULT_BLOCKED"
    assert payload["data"]["side_effect"]["level"] == "stateful"
    assert payload["meta"]["read_only_desktop_guard"]["blocked"] is True
    assert "unexpected_mutation_marker" not in raw
    assert "should-not-leak" not in raw

    invocations = runtime.session_store.list_tool_invocations(session_id=session_id)
    assert len(invocations) == 1
    invocation = invocations[0]
    audit_raw = json.dumps(invocation, ensure_ascii=False)
    assert invocation["tool_name"] == "desktop_internal_mcp.research_manager"
    assert invocation["status"] == "failed"
    assert invocation["error_code"] == "READ_ONLY_TOOL_RESULT_BLOCKED"
    assert invocation["output_summary"]["data"]["side_effect"]["level"] == "stateful"
    assert "unexpected_mutation_marker" not in audit_raw
    assert "should-not-leak" not in audit_raw


def test_financial_manager_stateful_query_requires_intent_and_intent_is_control_gated(tmp_path, monkeypatch) -> None:
    client = _client(tmp_path, monkeypatch)
    body = {
        "capability_id": "portfolio",
        "action_id": "create",
        "params": {"name": "Desk book"},
        "rationale": "create from test",
    }

    query = client.post("/v1/finance/manager/query", json=body)
    denied = client.post("/v1/finance/manager/intent", json=body)
    created = client.post("/v1/finance/manager/intent", headers={"Authorization": "Bearer secret"}, json=body)

    assert query.status_code == 200
    assert query.json()["error_code"] == "FINANCIAL_ACTION_REQUIRES_INTENT"
    assert denied.status_code == 401
    assert created.status_code == 200
    payload = created.json()
    assert payload["success"] is True
    assert payload["data"]["intent"]["action"] == "portfolio_manager.create"
    assert payload["data"]["intent"]["status"] == "awaiting_confirmation"


def test_financial_manager_dry_run_only_intent_rejects_explicit_false(tmp_path, monkeypatch) -> None:
    client = _client(tmp_path, monkeypatch)

    blocked = client.post(
        "/v1/finance/manager/intent",
        headers={"Authorization": "Bearer secret"},
        json={
            "capability_id": "paper",
            "action_id": "submit_order",
            "params": {"code": "600519", "quantity": 100, "dry_run": False},
            "rationale": "attempt non-dry-run paper order",
        },
    )
    created = client.post(
        "/v1/finance/manager/intent",
        headers={"Authorization": "Bearer secret"},
        json={
            "capability_id": "paper",
            "action_id": "submit_order",
            "params": {"code": "600519", "quantity": 100},
            "rationale": "dry-run paper order preview",
        },
    )

    assert blocked.status_code == 200
    blocked_payload = blocked.json()
    assert blocked_payload["success"] is False
    assert blocked_payload["error_code"] == "FINANCIAL_MANAGER_DRY_RUN_REQUIRED"
    assert blocked_payload["data"]["params"]["dry_run"] is False

    assert created.status_code == 200
    created_payload = created.json()
    assert created_payload["success"] is True
    assert created_payload["data"]["intent"]["action"] == "paper_trading_manager.submit_order"
    assert created_payload["data"]["intent"]["params"]["dry_run"] is True


def test_financial_manager_live_trade_actions_are_blocked(tmp_path, monkeypatch) -> None:
    client = _client(tmp_path, monkeypatch)

    query = client.post(
        "/v1/finance/manager/query",
        json={"capability_id": "broker-live", "action_id": "place_order", "params": {"broker_token": "should-not-leak"}},
    )
    intent = client.post(
        "/v1/finance/manager/intent",
        headers={"Authorization": "Bearer secret"},
        json={"capability_id": "broker-live", "action_id": "cancel_order", "params": {"broker_token": "should-not-leak"}},
    )

    assert query.status_code == 200
    assert intent.status_code == 200
    assert query.json()["error_code"] == "FINANCIAL_ACTION_BLOCKED"
    assert intent.json()["error_code"] == "FINANCIAL_ACTION_BLOCKED"
    assert "should-not-leak" not in json.dumps({"query": query.json(), "intent": intent.json()})
