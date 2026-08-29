from __future__ import annotations

import asyncio
import json
import threading
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from aiask_agent.intents import ActionIntentStore, IntentExecutor
from aiask_agent.model_client import MockModelClient, ModelResponse
from aiask_agent.runtime import AgentRuntime
from aiask_agent.server import _json_dumps, build_server
from aiask_agent.session_store import AgentSessionStore
from aiask_agent.tool_registry import AgentToolRegistry, aiask_envelope, build_default_tool_registry
from aiask_agent.tools.policy import ToolPolicy, ToolPolicyEngine

HTTP_TEST_TIMEOUT_SECONDS = 30


class EmptyModelClient:
    async def complete(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        model: str,
    ) -> ModelResponse:
        return ModelResponse(content="", tool_calls=[], usage={})


def _request(method: str, url: str, payload: dict[str, Any] | None = None, headers: dict[str, str] | None = None):
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    req = Request(url, data=data, method=method, headers={"Content-Type": "application/json", **dict(headers or {})})
    with urlopen(req, timeout=HTTP_TEST_TIMEOUT_SECONDS) as response:
        return response.status, json.loads(response.read().decode("utf-8"))


def _http_error_json(exc: HTTPError) -> dict[str, Any]:
    return json.loads(exc.read().decode("utf-8"))


def _raw_request(
    method: str,
    url: str,
    payload: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
):
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    req = Request(url, data=data, method=method, headers={"Content-Type": "application/json", **dict(headers or {})})
    with urlopen(req, timeout=HTTP_TEST_TIMEOUT_SECONDS) as response:
        return response.status, dict(response.headers), response.read()


def _write_collision_plugin(home, dirname: str, plugin_name: str, surface: str) -> None:
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
                surface: [
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


def test_http_json_encoder_sanitizes_non_finite_numbers() -> None:
    body = _json_dumps({"value": float("nan"), "nested": [float("inf"), -float("inf"), 1.25]})
    text = body.decode("utf-8")

    assert "NaN" not in text
    assert "Infinity" not in text
    assert json.loads(text) == {"nested": [None, None, 1.25], "value": None}
    json.dumps(json.loads(text), allow_nan=False)


def test_http_health_chat_and_control_endpoints(tmp_path, monkeypatch) -> None:
    registry = AgentToolRegistry()

    async def analyze(arguments: dict[str, Any]) -> dict[str, Any]:
        return aiask_envelope(
            True,
            data={"code": arguments.get("code"), "rating": "mock"},
            error=None,
            tool_name="agent_analyze_stock",
            source_chain=["test"],
        )

    registry.register(
        "agent_analyze_stock",
        description="mock stock analysis",
        parameters={"type": "object", "properties": {"code": {"type": "string"}}},
        handler=analyze,
    )
    runtime = AgentRuntime(
        model_client=MockModelClient(),
        tool_registry=registry,
        session_store=AgentSessionStore(tmp_path / "state.sqlite3"),
        max_iterations=3,
    )
    intent_store = ActionIntentStore(tmp_path / "intents.sqlite3")
    executor = IntentExecutor(intent_store)
    server = build_server("127.0.0.1", 0, runtime=runtime, intent_executor=executor)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_address[1]}"

    try:
        status, health = _request("GET", f"{base_url}/health")
        assert status == 200
        assert health["status"] == "ok"

        status, chat = _request(
            "POST",
            f"{base_url}/v1/chat/completions",
            {"messages": [{"role": "user", "content": 'tool:agent_analyze_stock {"code":"600519"}'}]},
        )
        assert status == 200
        assert chat["object"] == "chat.completion"
        assert chat["aiask"]["tool_calls"][0]["name"] == "agent_analyze_stock"
        assert chat["aiask"]["tool_calls"][0]["result"]["data"]["code"] == "600519"

        intent = intent_store.create(action="factory_run_once", params={"execution_mode": "dry_run"})
        try:
            _request("POST", f"{base_url}/intents/{intent['intent_id']}/deny", {"reason": "missing token"})
            raise AssertionError("deny without token should fail")
        except HTTPError as exc:
            assert exc.code == 503

        monkeypatch.setenv("AIASK_AGENT_CONTROL_TOKEN", "secret")
        status, denied = _request(
            "POST",
            f"{base_url}/intents/{intent['intent_id']}/deny",
            {"reason": "test"},
            headers={"Authorization": "Bearer secret"},
        )
        assert status == 200
        assert denied["success"] is True

        async def fake_call_db_facade(loader, params=None) -> dict:
            return {"success": True, "data": {"action": "factory_run_once", "params": dict(params or {})}, "error": None}

        monkeypatch.setattr("aiask_agent.adapters.strategy_factory._call_db_facade", fake_call_db_facade)
        intent2 = intent_store.create(action="factory_run_once", params={"execution_mode": "dry_run"})
        status, confirmed = _request(
            "POST",
            f"{base_url}/intents/{intent2['intent_id']}/confirm",
            {},
            headers={"X-AIASK-Agent-Control-Token": "secret"},
        )
        assert status == 200
        assert confirmed["success"] is True
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_tools_payload_includes_optional_contract_metadata(tmp_path) -> None:
    registry = AgentToolRegistry()

    async def quote(arguments: dict[str, Any]) -> dict[str, Any]:
        return aiask_envelope(
            True,
            data={"code": arguments.get("code"), "price": 10.5},
            error=None,
            tool_name="agent_quote",
            source_chain=["test"],
        )

    registry.register(
        "agent_quote",
        description="mock quote",
        parameters={"type": "object", "properties": {"code": {"type": "string"}}, "required": ["code"]},
        handler=quote,
        metadata={
            "category": "financial_read",
            "side_effect": "read_only",
            "input_schema": {"type": "object", "properties": {"code": {"type": "string"}}, "required": ["code"]},
            "output_schema": {"type": "object", "properties": {"price": {"type": "number"}}},
            "freshness": {"expectation": "intraday_or_latest_quote_snapshot"},
            "source_policy": {"priority": ["tdx_local", "akshare"]},
            "examples": [{"arguments": {"code": "600519"}}],
            "contract_version": "ai_tool_contract_v1",
            "contract_source": "akshare_mcp.tool_catalog",
            "standard_model": "EquityQuote",
            "provider_choices": [{"rank": 1, "source": "tdx_local", "provider": "tdx_local"}],
            "provider_status": {"providers": [{"provider": "tdx_local", "available": True}]},
            "quality_gate": {"mode": "report_only", "status": "passed"},
            "reconciliation": {"enabled": True, "mode": "sampled_report_only"},
            "form_schema": {"type": "object", "properties": {"code": {"type": "string"}}, "required": ["code"]},
        },
    )
    runtime = AgentRuntime(
        model_client=MockModelClient(),
        tool_registry=registry,
        session_store=AgentSessionStore(tmp_path / "state.sqlite3"),
        max_iterations=3,
    )
    server = build_server("127.0.0.1", 0, runtime=runtime)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_address[1]}"

    try:
        status, tools = _request("GET", f"{base_url}/v1/tools")
        assert status == 200
        item = next(row for row in tools["data"] if row["name"] == "agent_quote")
        assert item["parameters"]["required"] == ["code"]
        assert item["input_schema"]["required"] == ["code"]
        assert item["freshness"]["expectation"] == "intraday_or_latest_quote_snapshot"
        assert item["source_policy"]["priority"] == ["tdx_local", "akshare"]
        assert item["contract_version"] == "ai_tool_contract_v1"
        assert item["contract_source"] == "akshare_mcp.tool_catalog"
        assert item["standard_model"] == "EquityQuote"
        assert item["provider_choices"][0]["provider"] == "tdx_local"
        assert item["provider_status"]["providers"][0]["available"] is True
        assert item["quality_gate"]["mode"] == "report_only"
        assert item["reconciliation"]["enabled"] is True
        assert item["form_schema"]["required"] == ["code"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_runtime_explicit_tool_directive_runs_when_model_returns_empty(tmp_path) -> None:
    registry = AgentToolRegistry()

    async def analyze(arguments: dict[str, Any]) -> dict[str, Any]:
        return aiask_envelope(
            True,
            data={"code": arguments.get("code"), "rating": "mock"},
            error=None,
            tool_name="agent_analyze_stock",
            source_chain=["test"],
        )

    registry.register(
        "agent_analyze_stock",
        description="mock stock analysis",
        parameters={"type": "object", "properties": {"code": {"type": "string"}}},
        handler=analyze,
        metadata={"category": "financial_read", "side_effect": "read_only"},
    )
    runtime = AgentRuntime(
        model_client=EmptyModelClient(),
        tool_registry=registry,
        session_store=AgentSessionStore(tmp_path / "state.sqlite3"),
        max_iterations=3,
    )

    result = asyncio.run(
        runtime.run([{"role": "user", "content": '请调用工具 agent_analyze_stock {"code":"600519"}'}])
    )

    assert result.content == "工具调用完成: agent_analyze_stock"
    assert result.tool_calls[0]["name"] == "agent_analyze_stock"
    assert result.tool_calls[0]["arguments"] == {"code": "600519"}
    assert {event["event"] for event in result.audit_events} >= {
        "tool.directive_detected",
        "tool.completed",
        "model.empty_response",
        "response.fallback",
    }


def test_desktop_http_surface_and_read_only_tool_api(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("AIASK_AGENT_CORS_ORIGINS", "http://localhost:1420")
    intent_store = ActionIntentStore(tmp_path / "intents.sqlite3")
    registry = build_default_tool_registry(intent_store)
    runtime = AgentRuntime(
        model_client=MockModelClient(),
        tool_registry=registry,
        session_store=AgentSessionStore(tmp_path / "state.sqlite3"),
        max_iterations=3,
    )
    executor = IntentExecutor(intent_store)
    server = build_server("127.0.0.1", 0, runtime=runtime, intent_executor=executor)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_address[1]}"

    try:
        status, headers, _ = _raw_request(
            "OPTIONS",
            f"{base_url}/v1/tools",
            headers={
                "Origin": "http://localhost:1420",
                "Access-Control-Request-Method": "GET",
            },
        )
        assert status == 204
        assert headers["Access-Control-Allow-Origin"] == "http://localhost:1420"
        assert "Authorization" in headers["Access-Control-Allow-Headers"]

        status, detailed = _request("GET", f"{base_url}/health/detailed")
        assert status == 200
        assert detailed["service"] == "aiask-agent"
        assert detailed["tools"]["count"] >= 1
        assert detailed["control"]["token_configured"] is False
        assert "secret" not in json.dumps(detailed).lower()

        status, tools = _request("GET", f"{base_url}/v1/tools")
        assert status == 200
        names = {item["name"] for item in tools["data"]}
        assert "agent_tool_catalog" in names
        assert "agent_analyze_stock" in names
        assert "agent_action_intent_create" in names

        status, catalog = _request("POST", f"{base_url}/v1/tools/agent_tool_catalog", {})
        assert status == 200
        assert catalog["success"] is True
        assert catalog["meta"]["side_effect"]["level"] == "read_only"

        try:
            _request(
                "POST",
                f"{base_url}/v1/tools/agent_action_intent_create",
                {"action": "factory_run_once"},
            )
            raise AssertionError("stateful tool should not be callable through read-only API")
        except HTTPError as exc:
            assert exc.code == 403

        intent = intent_store.create(action="factory_run_once", params={"execution_mode": "dry_run"})
        status, fetched = _request("GET", f"{base_url}/intents/{intent['intent_id']}")
        assert status == 200
        assert fetched["success"] is True
        assert fetched["data"]["intent"]["status"] == "awaiting_confirmation"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_legacy_read_only_tool_api_blocks_non_read_only_result(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("AIASK_AGENT_LOAD_PROJECT_ENV", "0")
    registry = AgentToolRegistry()
    session_store = AgentSessionStore(tmp_path / "state.sqlite3")

    async def mislabelled_reader(arguments: dict[str, Any]) -> dict[str, Any]:
        return aiask_envelope(
            True,
            data={
                "unexpected_mutation_marker": "should-not-leak",
                "requested": arguments.get("code"),
            },
            error=None,
            tool_name="agent_mislabelled_reader",
            source_chain=["test", "mislabelled_reader"],
            side_effect={
                "level": "stateful",
                "target": "mislabelled.position",
                "confirmation_required": True,
                "idempotent": False,
            },
        )

    registry.register(
        "agent_mislabelled_reader",
        description="Claims to be read-only but returns stateful metadata",
        parameters={"type": "object", "properties": {"code": {"type": "string"}}},
        handler=mislabelled_reader,
        metadata={"category": "financial_read", "side_effect": "read_only"},
    )
    runtime = AgentRuntime(
        model_client=MockModelClient(),
        tool_registry=registry,
        session_store=session_store,
        max_iterations=2,
    )
    server = build_server("127.0.0.1", 0, runtime=runtime)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_address[1]}"

    try:
        status, body = _request(
            "POST",
            f"{base_url}/v1/tools/agent_mislabelled_reader",
            {"code": "600519", "session_id": "sess_guard"},
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    raw = json.dumps(body, ensure_ascii=False)
    assert status == 200
    assert body["success"] is False
    assert body["error_code"] == "READ_ONLY_TOOL_RESULT_BLOCKED"
    assert body["data"]["tool"] == "agent_mislabelled_reader"
    assert body["data"]["side_effect"]["level"] == "stateful"
    assert body["meta"]["read_only_desktop_guard"]["blocked"] is True
    assert "unexpected_mutation_marker" not in raw
    assert "should-not-leak" not in raw

    invocations = session_store.list_tool_invocations(session_id="sess_guard")
    assert len(invocations) == 1
    invocation = invocations[0]
    audit_raw = json.dumps(invocation, ensure_ascii=False)
    assert invocation["tool_name"] == "agent_mislabelled_reader"
    assert invocation["status"] == "failed"
    assert invocation["error_code"] == "READ_ONLY_TOOL_RESULT_BLOCKED"
    assert invocation["output_summary"]["success"] is False
    assert invocation["output_summary"]["data"]["side_effect"]["level"] == "stateful"
    assert "unexpected_mutation_marker" not in audit_raw
    assert "should-not-leak" not in audit_raw


def test_legacy_read_only_tool_api_rejects_confirm_required_full_tools(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("AIASK_AGENT_LOAD_PROJECT_ENV", "0")
    session_store = AgentSessionStore(tmp_path / "state.sqlite3")
    registry = build_default_tool_registry(
        ActionIntentStore(tmp_path / "intents.sqlite3"),
        session_store=session_store,
        policy_engine=ToolPolicyEngine(ToolPolicy("general_full", True, (str(tmp_path),))),
    )
    runtime = AgentRuntime(
        model_client=MockModelClient(),
        tool_registry=registry,
        session_store=session_store,
        max_iterations=2,
    )
    server = build_server("127.0.0.1", 0, runtime=runtime)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_address[1]}"

    try:
        try:
            _request(
                "POST",
                f"{base_url}/v1/tools/agent_browser_click",
                {"selector": "#buy"},
            )
        except HTTPError as exc:
            assert exc.code == 403
            body = _http_error_json(exc)
            assert "read-only desktop API" in json.dumps(body)
        else:
            raise AssertionError("confirm-required full tools must be rejected by the read-only tool API")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_legacy_http_error_responses_redact_secret_text(tmp_path) -> None:
    secret = "fallback-artifact-secret"
    runtime = AgentRuntime(
        model_client=MockModelClient(),
        tool_registry=AgentToolRegistry(),
        session_store=AgentSessionStore(tmp_path / "state.sqlite3"),
        max_iterations=2,
    )
    server = build_server("127.0.0.1", 0, runtime=runtime)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_address[1]}"

    try:
        try:
            _request("GET", f"{base_url}/v1/artifacts/missing-broker_token={secret}/content")
            raise AssertionError("missing artifact should return a redacted 404 body")
        except HTTPError as exc:
            assert exc.code == 404
            body = _http_error_json(exc)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    text = json.dumps(body)
    assert secret not in text
    assert body["secrets_redacted"] is True
    assert "[redacted]" in body["error"]["message"]


def test_legacy_http_json_success_responses_redact_secret_payloads(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("AIASK_AGENT_CONTROL_TOKEN", "secret")
    runtime = AgentRuntime(
        model_client=MockModelClient(),
        tool_registry=AgentToolRegistry(),
        session_store=AgentSessionStore(tmp_path / "state.sqlite3"),
        max_iterations=2,
    )

    def fake_analytics_summary(*, user_id=None, limit=20):
        return {
            "object": "aiask.analytics_summary",
            "data": {
                "broker_token": "fallback-json-broker-secret",
                "callback": "https://broker.local/cb?password=fallback-json-url-secret",
            },
            "error": "failed Authorization: Bearer fallback-json-bearer-secret",
            "meta": {"debug": "client_secret=fallback-json-client-secret"},
        }

    monkeypatch.setattr(runtime.session_store, "analytics_summary", fake_analytics_summary)
    server = build_server("127.0.0.1", 0, runtime=runtime)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_address[1]}"

    try:
        status, body = _request(
            "GET",
            f"{base_url}/v1/desktop/analytics/summary?limit=1",
            headers={"Authorization": "Bearer secret"},
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    assert status == 200
    raw = json.dumps(body, ensure_ascii=False)
    for leaked in (
        "fallback-json-broker-secret",
        "fallback-json-url-secret",
        "fallback-json-bearer-secret",
        "fallback-json-client-secret",
    ):
        assert leaked not in raw
    assert body["data"]["broker_token"] == "[redacted]"
    assert body["error"] == "failed Authorization: Bearer [redacted]"
    assert body["meta"]["debug"] == "client_secret=[redacted]"


def test_native_hermes_full_mode_is_control_gated(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("AIASK_AGENT_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("AIASK_AGENT_ENABLE_HERMES_FULL", "1")
    monkeypatch.setenv("AIASK_AGENT_CONTROL_TOKEN", "secret")
    runtime = AgentRuntime(
        model_client=MockModelClient(),
        tool_registry=AgentToolRegistry(),
        session_store=AgentSessionStore(tmp_path / "state.sqlite3"),
        max_iterations=2,
    )
    server = build_server("127.0.0.1", 0, runtime=runtime)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_address[1]}"

    try:
        status, status_body = _request("GET", f"{base_url}/v1/hermes/status")
        assert status == 200
        assert status_body["implementation"] == "aiask_native"
        assert status_body["embedded_vendor_runtime"] is False

        try:
            _request("GET", f"{base_url}/v1/hermes/tools")
            raise AssertionError("hermes full tools require control token")
        except HTTPError as exc:
            assert exc.code == 401

        status, tools = _request("GET", f"{base_url}/v1/hermes/tools", headers={"Authorization": "Bearer secret"})
        assert status == 200
        names = {item["name"] for item in tools["data"]}
        assert {"agent_web_search", "agent_skill_list", "agent_terminal", "agent_message_send"} <= names
        assert not any(name in names for name in {"web_search", "terminal", "delegate_task"})

        try:
            _request(
                "POST",
                f"{base_url}/v1/responses",
                {"mode": "hermes_full", "input": 'tool:agent_clarify {"question":"Need universe?"}'},
            )
            raise AssertionError("hermes_full mode should require control token")
        except HTTPError as exc:
            assert exc.code == 401

        status, response = _request(
            "POST",
            f"{base_url}/v1/responses",
            {"mode": "hermes_full", "input": 'tool:agent_clarify {"question":"Need universe?"}'},
            headers={"Authorization": "Bearer secret"},
        )
        assert status == 200
        assert response["metadata"]["mode"] == "hermes_full"
        assert response["metadata"]["tool_calls"][0]["name"] == "agent_clarify"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_legacy_plugin_tool_collision_uses_specific_error_and_does_not_run(tmp_path, monkeypatch) -> None:
    home = tmp_path / "home"
    marker = tmp_path / "legacy_tool_collision_executed.txt"
    monkeypatch.setenv("AIASK_AGENT_HOME", str(home))
    monkeypatch.setenv("AIASK_AGENT_LOAD_PROJECT_ENV", "0")
    monkeypatch.setenv("AIASK_AGENT_ENABLE_HERMES_FULL", "1")
    monkeypatch.setenv("AIASK_AGENT_CONTROL_TOKEN", "secret")
    _write_collision_plugin(home, "demo-a", "demo-a", "tools")
    _write_collision_plugin(home, "demo_a", "demo_a", "tools")
    runtime = AgentRuntime(
        model_client=MockModelClient(),
        tool_registry=AgentToolRegistry(),
        session_store=AgentSessionStore(tmp_path / "state.sqlite3"),
        max_iterations=2,
    )
    server = build_server("127.0.0.1", 0, runtime=runtime)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_address[1]}"

    try:
        status, payload = _request(
            "POST",
            f"{base_url}/v1/plugins/demo-a/tools/ship-it/test",
            {"marker": str(marker), "value": "should-not-run"},
            headers={"X-AIASK-Agent-Control-Token": "secret"},
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    assert status == 200
    assert payload["success"] is False
    assert payload["error_code"] == "PLUGIN_TOOL_NAME_COLLISION"
    assert payload["data"]["wrapped_name"] == "agent_plugin_demo_a_ship_it"
    assert len(payload["data"]["collisions"]) == 2
    assert marker.exists() is False


def test_legacy_plugin_command_collision_uses_specific_error_and_does_not_run(tmp_path, monkeypatch) -> None:
    home = tmp_path / "home"
    marker = tmp_path / "legacy_command_collision_executed.txt"
    monkeypatch.setenv("AIASK_AGENT_HOME", str(home))
    monkeypatch.setenv("AIASK_AGENT_LOAD_PROJECT_ENV", "0")
    monkeypatch.setenv("AIASK_AGENT_ENABLE_HERMES_FULL", "1")
    monkeypatch.setenv("AIASK_AGENT_CONTROL_TOKEN", "secret")
    _write_collision_plugin(home, "command-a", "command-a", "commands")
    _write_collision_plugin(home, "command_a", "command_a", "commands")
    runtime = AgentRuntime(
        model_client=MockModelClient(),
        tool_registry=AgentToolRegistry(),
        session_store=AgentSessionStore(tmp_path / "state.sqlite3"),
        max_iterations=2,
    )
    server = build_server("127.0.0.1", 0, runtime=runtime)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_address[1]}"

    try:
        status, payload = _request(
            "POST",
            f"{base_url}/v1/plugins/command-a/commands/ship-it/test",
            {"marker": str(marker), "value": "should-not-run"},
            headers={"X-AIASK-Agent-Control-Token": "secret"},
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    assert status == 200
    assert payload["success"] is False
    assert payload["error_code"] == "PLUGIN_COMMAND_NAME_COLLISION"
    assert payload["data"]["wrapped_name"] == "agent_plugin_command_a_command_ship_it"
    assert len(payload["data"]["collisions"]) == 2
    assert marker.exists() is False


def test_legacy_plugin_tool_test_creates_intent_before_stateful_runner_execution(tmp_path, monkeypatch) -> None:
    home = tmp_path / "home"
    marker = tmp_path / "legacy_stateful_plugin_executed.txt"
    monkeypatch.setenv("AIASK_AGENT_HOME", str(home))
    monkeypatch.setenv("AIASK_AGENT_LOAD_PROJECT_ENV", "0")
    monkeypatch.setenv("AIASK_AGENT_ENABLE_HERMES_FULL", "1")
    monkeypatch.setenv("AIASK_AGENT_CONTROL_TOKEN", "secret")
    plugin_dir = home / "plugins" / "stateful-plugin"
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
                "name": "stateful-plugin",
                "enabled": True,
                "tools": [
                    {
                        "name": "mutate",
                        "runner": {"type": "python_module", "module": "runner", "function": "run"},
                        "parameters": {"type": "object"},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    runtime = AgentRuntime(
        model_client=MockModelClient(),
        tool_registry=AgentToolRegistry(),
        session_store=AgentSessionStore(tmp_path / "state.sqlite3"),
        max_iterations=2,
    )
    server = build_server("127.0.0.1", 0, runtime=runtime)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_address[1]}"

    try:
        status, payload = _request(
            "POST",
            f"{base_url}/v1/plugins/stateful-plugin/tools/mutate/test",
            {"marker": str(marker), "value": "confirmed"},
            headers={"X-AIASK-Agent-Control-Token": "secret"},
        )
        assert status == 200
        assert payload["object"] == "plugin.tool_test"
        assert payload["success"] is False
        assert payload["error_code"] == "ACTION_INTENT_REQUIRED"
        assert payload["data"]["action_intent_required"] is True
        assert payload["data"]["executed"] is False
        assert payload["data"]["intent"]["action"] == "plugin.tool_call"
        assert payload["data"]["intent"]["status"] == "awaiting_confirmation"
        assert marker.exists() is False

        intent_id = payload["data"]["intent"]["intent_id"]
        confirm_status, confirmed = _request(
            "POST",
            f"{base_url}/intents/{intent_id}/confirm",
            {},
            headers={"X-AIASK-Agent-Control-Token": "secret"},
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    assert confirm_status == 200
    assert confirmed["success"] is True
    assert marker.read_text(encoding="utf-8") == "confirmed"


def test_legacy_admin_full_tool_file_write_creates_intent_without_writing(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("AIASK_AGENT_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("AIASK_AGENT_LOAD_PROJECT_ENV", "0")
    monkeypatch.setenv("AIASK_AGENT_ENABLE_HERMES_FULL", "1")
    monkeypatch.setenv("AIASK_AGENT_ENABLE_GENERAL_TOOLS", "1")
    monkeypatch.setenv("AIASK_AGENT_TOOLSET", "general_full")
    monkeypatch.setenv("AIASK_AGENT_CONTROL_TOKEN", "secret")
    monkeypatch.setenv("AIASK_AGENT_WORKSPACE_ROOTS", str(tmp_path))
    runtime = AgentRuntime(
        model_client=MockModelClient(),
        tool_registry=AgentToolRegistry(),
        session_store=AgentSessionStore(tmp_path / "state.sqlite3"),
        max_iterations=2,
    )
    server = build_server("127.0.0.1", 0, runtime=runtime)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_address[1]}"

    try:
        status, payload = _request(
            "POST",
            f"{base_url}/v1/hermes/admin/tools/agent_file_write",
            {"path": "legacy-admin-write.txt", "content": "must wait for confirmation"},
            headers={"X-AIASK-Agent-Control-Token": "secret"},
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    assert status == 200
    assert payload["success"] is False
    assert payload["error_code"] == "ACTION_INTENT_REQUIRED"
    assert payload["data"]["intent"]["action"] == "file.write"
    assert payload["data"]["executed"] is False
    assert not (tmp_path / "legacy-admin-write.txt").exists()


def test_legacy_job_create_preserves_execution_payload(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("AIASK_AGENT_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("AIASK_AGENT_LOAD_PROJECT_ENV", "0")
    monkeypatch.setenv("AIASK_AGENT_CONTROL_TOKEN", "secret")
    runtime = AgentRuntime(
        model_client=MockModelClient(),
        tool_registry=AgentToolRegistry(),
        session_store=AgentSessionStore(tmp_path / "state.sqlite3"),
        max_iterations=2,
    )
    server = build_server("127.0.0.1", 0, runtime=runtime)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_address[1]}"

    try:
        status, payload = _request(
            "POST",
            f"{base_url}/v1/jobs",
            {
                "name": "daily review",
                "prompt": "review portfolio",
                "interval_seconds": 3600,
                "script": str(tmp_path / "review.ps1"),
                "skills": ["risk-review"],
                "silent_pattern": "NO_CHANGES",
            },
            headers={"X-AIASK-Agent-Control-Token": "secret"},
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    assert status == 201
    assert payload["object"] == "job"
    assert payload["payload"] == {
        "script": str(tmp_path / "review.ps1"),
        "skills": ["risk-review"],
        "silent_pattern": "NO_CHANGES",
    }


def test_legacy_http_mcp_inventory_honors_all_query_and_full_gate(tmp_path, monkeypatch) -> None:
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
                            {"name": "quote", "description": "quote tool", "side_effect": {"level": "read_only"}},
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
                            {
                                "name": "query_broker_orders",
                                "description": "read-only broker order query",
                                "side_effect": {"level": "read_only"},
                            },
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
    runtime = AgentRuntime(
        model_client=MockModelClient(),
        tool_registry=AgentToolRegistry(),
        session_store=AgentSessionStore(tmp_path / "state.sqlite3"),
        max_iterations=2,
    )
    server = build_server("127.0.0.1", 0, runtime=runtime)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_address[1]}"

    try:
        status, financial_servers = _request("GET", f"{base_url}/v1/mcp/servers")
        assert status == 200
        assert {item["name"] for item in financial_servers["data"]} == {"finance-demo"}
        financial_server = financial_servers["data"][0]
        assert financial_server["tools"] == ["quote", "query_broker_orders"]
        assert financial_server["safe_financial_tools"] == ["quote", "query_broker_orders"]
        assert set(financial_server["trade_risk_tools_blocked"]) == {"ths_place_order", "broker_place_order"}
        assert financial_server["live_trading_enabled"] is False

        status, all_servers = _request("GET", f"{base_url}/v1/mcp/servers?all=true")
        assert status == 200
        assert {item["name"] for item in all_servers["data"]} == {"finance-demo", "general-demo"}

        try:
            _request("GET", f"{base_url}/v1/mcp/tools?all=true")
            raise AssertionError("MCP tools inventory should require full-mode control token")
        except HTTPError as exc:
            assert exc.code == 401

        headers = {"Authorization": "Bearer secret"}
        status, financial_tools = _request("GET", f"{base_url}/v1/mcp/tools", headers=headers)
        assert status == 200
        assert {item["name"] for item in financial_tools["data"]} == {"quote", "query_broker_orders"}

        status, all_tools = _request("GET", f"{base_url}/v1/mcp/tools?all=true", headers=headers)
        assert status == 200
        all_tools_by_name = {item["name"]: item for item in all_tools["data"]}
        assert set(all_tools_by_name) == {"quote", "ths_place_order", "broker_place_order", "query_broker_orders", "browser"}
        assert all_tools_by_name["quote"]["side_effect"]["level"] == "read_only"
        assert all_tools_by_name["query_broker_orders"]["side_effect"]["level"] == "read_only"
        assert all_tools_by_name["ths_place_order"]["side_effect"]["level"] == "trade_risk"
        assert all_tools_by_name["broker_place_order"]["side_effect"]["level"] == "trade_risk"

        status, resources = _request("GET", f"{base_url}/v1/mcp/resources?all=true", headers=headers)
        assert status == 200
        assert resources["data"][0]["uri"] == "aiask://quotes"

        status, prompts = _request("GET", f"{base_url}/v1/mcp/prompts?all=true", headers=headers)
        assert status == 200
        assert prompts["data"][0]["name"] == "risk-review"

        status, oauth = _request("GET", f"{base_url}/v1/mcp/oauth_status?all=true", headers=headers)
        assert status == 200
        assert {item["server"] for item in oauth["data"]} == {"finance-demo", "general-demo"}
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
