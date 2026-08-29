from __future__ import annotations

import json

from fastapi.testclient import TestClient

from aiask_agent.mcp_client import MCPAggregator
from aiask_agent.model_client import MockModelClient
from aiask_agent.runtime import AgentRuntime
from aiask_agent.server import create_app
from aiask_agent.session_store import AgentSessionStore


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


def _write_qmt_mcp_config(tmp_path) -> None:
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    (home / "mcp_servers.json").write_text(
        json.dumps(
            {
                "servers": [
                    {
                        "name": "qmt-local",
                        "domain": "financial",
                        "transport": "stdio",
                        "command": "aiask-finance-qmt",
                        "tools": [
                            {"name": "qmt_query_account", "description": "QMT account", "inputSchema": {"type": "object", "properties": {}}},
                            {"name": "qmt_query_position", "description": "QMT positions", "inputSchema": {"type": "object", "properties": {}}},
                            {"name": "qmt_query_orders", "description": "QMT orders", "inputSchema": {"type": "object", "properties": {}}},
                        ],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )


def _write_ths_mcp_config(tmp_path) -> None:
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    (home / "mcp_servers.json").write_text(
        json.dumps(
            {
                "servers": [
                    {
                        "name": "ths-local",
                        "domain": "financial",
                        "transport": "stdio",
                        "command": "aiask-finance-ths",
                        "tools": [
                            {"name": "ths_query_balance", "description": "THS balance", "inputSchema": {"type": "object", "properties": {}}},
                            {"name": "ths_query_position", "description": "THS positions", "inputSchema": {"type": "object", "properties": {}}},
                            {"name": "ths_query_orders", "description": "THS orders", "inputSchema": {"type": "object", "properties": {}}},
                            {"name": "ths_query_deals", "description": "THS deals", "inputSchema": {"type": "object", "properties": {}}},
                        ],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )


def test_broker_readiness_reports_missing_config_without_secret_values(tmp_path, monkeypatch) -> None:
    client = _client(tmp_path, monkeypatch)

    response = client.get("/v1/desktop/broker-readiness")

    assert response.status_code == 200
    payload = response.json()
    assert payload["object"] == "aiask.desktop.broker_readiness"
    qmt = next(item for item in payload["connectors"] if item["provider"] == "qmt")
    assert qmt["read_only"] is True
    assert qmt["live_trading_enabled"] is False
    assert "QMT_PATH" in qmt["missing_env"]
    assert "QMT_ACCOUNT" in qmt["missing_env"]
    assert qmt["test_entry"]["path"] == "/v1/desktop/broker/sync"
    assert qmt["test_entry"]["consent_required"] is True
    assert qmt["test_entry"]["control_token_required"] is True
    assert any("MiniQMT" in item for item in qmt["environment_checks"])
    assert any("Desktop only sends provider" in item for item in qmt["authorization_notes"])
    ths = next(item for item in payload["connectors"] if item["provider"] == "tonghuashun")
    assert ths["read_only"] is True
    assert ths["live_trading_enabled"] is False
    assert "THS_CLIENT_PATH" in ths["missing_env"]
    assert any("Tonghuashun" in item for item in ths["environment_checks"])
    assert ths["test_entry"]["path"] == "/v1/desktop/broker/sync"
    assert ths["test_entry"]["control_token_required"] is True
    assert payload["secrets_redacted"] is True


def test_broker_sync_requires_consent_and_ready_connector(tmp_path, monkeypatch) -> None:
    client = _client(tmp_path, monkeypatch)

    control_denied = client.post("/v1/desktop/broker/sync", json={"provider": "qmt", "broker_token": "should-not-leak"})
    analytics_denied = client.post("/v1/desktop/broker/analytics/run", json={"provider": "qmt"})
    consent_denied = client.post(
        "/v1/desktop/broker/sync",
        headers={"Authorization": "Bearer secret"},
        json={"provider": "qmt", "broker_token": "should-not-leak"},
    )
    not_ready = client.post(
        "/v1/desktop/broker/sync",
        headers={"Authorization": "Bearer secret"},
        json={"provider": "qmt", "consent": True, "broker_token": "should-not-leak"},
    )

    assert control_denied.status_code == 401
    assert analytics_denied.status_code == 401
    assert consent_denied.status_code == 200
    assert consent_denied.json()["error_code"] == "BROKER_CONSENT_REQUIRED"
    assert not_ready.status_code == 200
    assert not_ready.json()["error_code"] == "BROKER_CONNECTOR_NOT_READY"
    assert "should-not-leak" not in json.dumps(
        {
            "control_denied": control_denied.json(),
            "analytics_denied": analytics_denied.json(),
            "consent_denied": consent_denied.json(),
            "not_ready": not_ready.json(),
        }
    )


def test_broker_sync_persists_snapshots_and_analytics_with_mocked_qmt(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("AIASK_AGENT_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("AIASK_AGENT_CONTROL_TOKEN", "secret")
    monkeypatch.setenv("QMT_PATH", "configured")
    monkeypatch.setenv("QMT_ACCOUNT", "configured")
    _write_qmt_mcp_config(tmp_path)

    async def fake_call(self, wrapped_name, arguments):
        if wrapped_name.endswith("qmt_query_account"):
            return {
                "total_asset": 100000,
                "cash_available": 12000,
                "market_value": 88000,
                "account_id": "real-account-id-should-be-hashed",
            }
        if wrapped_name.endswith("qmt_query_position"):
            return {
                "positions": [
                    {"code": "600519", "name": "Kweichow Moutai", "volume": 100, "market_value": 45000, "profit": 3000},
                    {"code": "000001", "name": "Ping An Bank", "volume": 1000, "market_value": 43000, "profit": -800},
                ]
            }
        if wrapped_name.endswith("qmt_query_orders"):
            return {"orders": [{"order_id": "order-1", "code": "600519", "direction": "buy", "price": 450, "volume": 100, "status": "filled"}]}
        raise AssertionError(wrapped_name)

    monkeypatch.setattr(MCPAggregator, "call", fake_call)
    runtime = _runtime(tmp_path)
    session_id = runtime.session_store.create_session(session_id="sess_broker_audit", user_id="local")
    client = TestClient(create_app(runtime=runtime))

    sync = client.post(
        "/v1/desktop/broker/sync",
        headers={"Authorization": "Bearer secret"},
        json={"provider": "qmt", "consent": True, "user_id": "local", "session_id": session_id},
    )
    latest = client.get("/v1/desktop/broker/accounts?user_id=local&provider=qmt")
    analytics = client.get("/v1/desktop/broker/analytics/latest?user_id=local&provider=qmt")

    assert sync.status_code == 200
    sync_payload = sync.json()
    assert sync_payload["success"] is True
    assert sync_payload["data"]["counts"] == {"accounts": 1, "positions": 2, "orders": 1, "deals": 0}
    assert latest.status_code == 200
    latest_payload = latest.json()
    assert len(latest_payload["data"]["accounts"]) == 1
    assert len(latest_payload["data"]["positions"]) == 2
    assert latest_payload["data"]["accounts"][0]["account_ref_hash"]
    assert latest_payload["data"]["orders"][0]["order_ref_hash"]
    assert "real-account-id-should-be-hashed" not in json.dumps(latest_payload)
    assert "order-1" not in json.dumps(latest_payload)
    assert analytics.status_code == 200
    metrics = analytics.json()["data"]["analytics"]["metrics"]
    assert metrics["position_count"] == 2
    assert metrics["cash_ratio"] == 0.12
    invocations = runtime.session_store.list_tool_invocations(session_id=session_id)
    assert len(invocations) == 3
    assert all(item["source_chain"] == ["aiask_agent.server", "desktop.broker_readonly"] for item in invocations)


def test_broker_sync_fails_closed_on_non_read_only_query_result(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("AIASK_AGENT_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("AIASK_AGENT_CONTROL_TOKEN", "secret")
    monkeypatch.setenv("QMT_PATH", "configured")
    monkeypatch.setenv("QMT_ACCOUNT", "configured")
    _write_qmt_mcp_config(tmp_path)

    async def fake_call(self, wrapped_name, arguments):
        if wrapped_name.endswith("qmt_query_account"):
            return {
                "success": True,
                "data": {
                    "total_asset": 100000,
                    "account_id": "account-should-not-persist",
                    "unexpected_mutation_marker": "should-not-leak",
                },
                "error": None,
                "meta": {
                    "side_effect": {
                        "level": "stateful",
                        "target": "qmt_query_account.cache_write",
                        "confirmation_required": True,
                        "idempotent": False,
                    },
                    "source_chain": ["test", "qmt"],
                },
            }
        if wrapped_name.endswith("qmt_query_position"):
            return {"positions": [{"code": "600519", "volume": 100, "market_value": 45000}]}
        if wrapped_name.endswith("qmt_query_orders"):
            return {"orders": [{"order_id": "order-should-not-persist", "code": "600519", "volume": 100}]}
        raise AssertionError(wrapped_name)

    monkeypatch.setattr(MCPAggregator, "call", fake_call)
    runtime = _runtime(tmp_path)
    session_id = runtime.session_store.create_session(session_id="sess_broker_guard", user_id="local")
    client = TestClient(create_app(runtime=runtime))

    sync = client.post(
        "/v1/desktop/broker/sync",
        headers={"Authorization": "Bearer secret"},
        json={"provider": "qmt", "consent": True, "user_id": "local", "session_id": session_id},
    )
    latest = client.get("/v1/desktop/broker/accounts?user_id=local&provider=qmt")

    assert sync.status_code == 200
    payload = sync.json()
    raw = json.dumps(payload, ensure_ascii=False)
    assert payload["success"] is False
    assert payload["error_code"] == "READ_ONLY_TOOL_RESULT_BLOCKED"
    assert payload["data"]["counts"] == {"accounts": 0, "positions": 0, "orders": 0, "deals": 0}
    assert payload["data"]["errors"][0]["key"] == "account"
    assert "unexpected_mutation_marker" not in raw
    assert "should-not-leak" not in raw
    assert "account-should-not-persist" not in raw
    assert "order-should-not-persist" not in raw

    latest_payload = latest.json()
    assert latest_payload["data"]["profiles"] == []
    assert latest_payload["data"]["accounts"] == []
    assert latest_payload["data"]["positions"] == []
    assert latest_payload["data"]["orders"] == []
    assert latest_payload["data"]["analytics"] is None

    invocations = runtime.session_store.list_tool_invocations(session_id=session_id)
    assert any(item["error_code"] == "READ_ONLY_TOOL_RESULT_BLOCKED" for item in invocations)


def test_broker_sync_persists_snapshots_and_analytics_with_mocked_tonghuashun(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("AIASK_AGENT_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("AIASK_AGENT_CONTROL_TOKEN", "secret")
    monkeypatch.setenv("THS_CLIENT_PATH", "configured")
    _write_ths_mcp_config(tmp_path)

    async def fake_call(self, wrapped_name, arguments):
        if wrapped_name.endswith("ths_query_balance"):
            return {
                "总资产": 86000,
                "可用资金": 24000,
                "证券市值": 62000,
                "资金账号": "ths-account-should-be-hashed",
            }
        if wrapped_name.endswith("ths_query_position"):
            return {
                "positions": [
                    {"证券代码": "300750", "证券名称": "CATL", "持仓数量": 200, "市值": 44000, "盈亏": 2000},
                    {"证券代码": "600036", "证券名称": "CMB", "持仓数量": 600, "市值": 18000, "盈亏": 0},
                ]
            }
        if wrapped_name.endswith("ths_query_orders"):
            return {"orders": [{"委托编号": "ths-order-1", "证券代码": "300750", "买卖方向": "sell", "委托价格": 220, "委托数量": 100, "委托状态": "filled"}]}
        if wrapped_name.endswith("ths_query_deals"):
            return {"deals": [{"成交编号": "ths-deal-1", "委托编号": "ths-order-1", "证券代码": "300750", "买卖方向": "sell", "成交价格": 220, "成交数量": 100}]}
        raise AssertionError(wrapped_name)

    monkeypatch.setattr(MCPAggregator, "call", fake_call)
    client = TestClient(create_app(runtime=_runtime(tmp_path)))

    sync = client.post(
        "/v1/desktop/broker/sync",
        headers={"Authorization": "Bearer secret"},
        json={"provider": "ths", "consent": True, "user_id": "local"},
    )
    latest = client.get("/v1/desktop/broker/accounts?user_id=local&provider=tonghuashun")
    analytics = client.get("/v1/desktop/broker/analytics/latest?user_id=local&provider=tonghuashun")

    assert sync.status_code == 200
    sync_payload = sync.json()
    assert sync_payload["success"] is True
    assert sync_payload["data"]["profile"]["provider"] == "tonghuashun"
    assert sync_payload["data"]["counts"] == {"accounts": 1, "positions": 2, "orders": 1, "deals": 1}
    latest_payload = latest.json()
    assert len(latest_payload["data"]["accounts"]) == 1
    assert latest_payload["data"]["accounts"][0]["account_ref_hash"]
    assert latest_payload["data"]["orders"][0]["order_ref_hash"]
    assert latest_payload["data"]["deals"][0]["deal_ref_hash"]
    assert latest_payload["data"]["deals"][0]["order_ref_hash"]
    assert "ths-account-should-be-hashed" not in json.dumps(latest_payload)
    assert "ths-order-1" not in json.dumps(latest_payload)
    assert "ths-deal-1" not in json.dumps(latest_payload)
    metrics = analytics.json()["data"]["analytics"]["metrics"]
    assert metrics["position_count"] == 2
    assert metrics["sell_count"] == 2
