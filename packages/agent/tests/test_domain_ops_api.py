from __future__ import annotations

import json
import os

from fastapi.testclient import TestClient

from aiask_agent.intents import ActionIntentStore, IntentExecutor
from aiask_agent.moa import aggregator_model, reference_models
from aiask_agent.model_client import MockModelClient
from aiask_agent.runtime import AgentRuntime
from aiask_agent.server import create_app
from aiask_agent.session_store import AgentSessionStore
from aiask_agent.tool_registry import AgentToolRegistry, aiask_envelope, build_default_tool_registry


def _runtime(tmp_path) -> AgentRuntime:
    return AgentRuntime(
        model_client=MockModelClient(),
        session_store=AgentSessionStore(tmp_path / "state.sqlite3"),
        max_iterations=2,
    )


def _client(tmp_path, monkeypatch) -> TestClient:
    monkeypatch.setenv("AIASK_AGENT_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("AIASK_AGENT_ENABLE_HERMES_FULL", "1")
    monkeypatch.setenv("AIASK_AGENT_ENABLE_GENERAL_TOOLS", "1")
    monkeypatch.setenv("AIASK_AGENT_TOOLSET", "general_full")
    monkeypatch.setenv("AIASK_AGENT_CONTROL_TOKEN", "secret")
    monkeypatch.setenv("AIASK_STOCK_DATA_SOURCES_FILE", str(tmp_path / "stock_data_sources.json"))
    return TestClient(create_app(runtime=_runtime(tmp_path)))


def test_jobs_state_changes_require_control_token_in_fastapi_app(tmp_path, monkeypatch) -> None:
    client = _client(tmp_path, monkeypatch)
    payload = {"name": "smoke", "prompt": "hello job", "interval_seconds": 3600}

    denied = client.post("/v1/jobs", json=payload)
    assert denied.status_code == 401

    created = client.post("/v1/jobs", headers={"Authorization": "Bearer secret"}, json=payload)
    assert created.status_code == 201
    job_id = created.json()["job_id"]

    listed = client.get("/v1/jobs")
    assert listed.status_code == 200
    assert job_id in json.dumps(listed.json())

    patch_denied = client.patch(f"/v1/jobs/{job_id}", json={"enabled": False})
    assert patch_denied.status_code == 401

    patched = client.patch(
        f"/v1/jobs/{job_id}",
        headers={"Authorization": "Bearer secret"},
        json={"enabled": True},
    )
    assert patched.status_code == 200
    assert patched.json()["enabled"] is True

    run_denied = client.post(f"/v1/jobs/{job_id}/run", json={})
    assert run_denied.status_code == 401

    run = client.post(f"/v1/jobs/{job_id}/run", headers={"Authorization": "Bearer secret"}, json={})
    assert run.status_code == 200
    assert run.json()["success"] is True

    runs = client.get(f"/v1/jobs/{job_id}/runs?limit=20")
    assert runs.status_code == 200
    assert runs.json()["job_id"] == job_id

    delete_denied = client.delete(f"/v1/jobs/{job_id}")
    assert delete_denied.status_code == 401

    deleted = client.delete(f"/v1/jobs/{job_id}", headers={"Authorization": "Bearer secret"})
    assert deleted.status_code == 200
    assert deleted.json()["deleted"] is True


def test_desktop_settings_status_redacts_secret_state_and_reports_profile(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-secret-value")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://models.example.test/v1")
    client = _client(tmp_path, monkeypatch)

    response = client.get("/v1/settings/status", headers={"Authorization": "Bearer secret"})

    assert response.status_code == 200
    payload = response.json()
    raw = json.dumps(payload)
    assert payload["object"] == "aiask.settings_status"
    assert payload["secrets_redacted"] is True
    assert payload["agent"]["control_authorized"] is True
    assert payload["llm"]["ai_status"]["api_key_configured"] is True
    assert payload["llm"]["ai_status"]["secrets_redacted"] is True
    assert payload["profile"]["user_id"] == "local"
    assert "sk-test-secret-value" not in raw


def test_desktop_settings_status_uses_project_env_llm_config(tmp_path, monkeypatch) -> None:
    project_env = tmp_path / ".env"
    project_env.write_text(
        "\n".join(
            [
                "AIASK_AGENT_MODEL_PROVIDER=openai",
                "AIASK_AGENT_MODEL=root-api-model",
                "OPENAI_BASE_URL=https://root-api.example.test/v1",
                "OPENAI_API_KEY=sk-root-project-secret",
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("AIASK_AGENT_LOAD_PROJECT_ENV", "1")
    monkeypatch.setenv("AIASK_AGENT_ENV_FILE", str(project_env))
    # The test session sets AIASK_LOAD_PROJECT_ENV=0, under which a named file is ignored.
    monkeypatch.delenv("AIASK_LOAD_PROJECT_ENV", raising=False)
    for key in ("AIASK_AGENT_MODEL_PROVIDER", "AIASK_AGENT_MODEL", "OPENAI_BASE_URL", "OPENAI_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    client = _client(tmp_path, monkeypatch)

    response = client.get("/v1/settings/status", headers={"Authorization": "Bearer secret"})

    assert response.status_code == 200
    payload = response.json()
    raw = json.dumps(payload)
    ai_status = payload["llm"]["ai_status"]
    provider_status = payload["llm"]["providers"]
    assert ai_status["provider"] == "openai"
    assert ai_status["model"] == "root-api-model"
    assert ai_status["base_url_configured"] is True
    assert ai_status["api_key_configured"] is True
    assert ai_status["config_source"]["loaded"] is True
    assert ai_status["config_source"]["source"] == "explicit"
    assert provider_status["default_model"] == "root-api-model"
    assert provider_status["config_source"]["loaded"] is True
    assert "sk-root-project-secret" not in raw


def test_desktop_settings_status_project_env_llm_overrides_process_env(tmp_path, monkeypatch) -> None:
    project_env = tmp_path / ".env"
    project_env.write_text(
        "\n".join(
            [
                "AIASK_AGENT_MODEL_PROVIDER=openai",
                "AIASK_AGENT_MODEL=file-api-model",
                "AIASK_AGENT_VISION_MODEL=file-vision-model",
                "AIASK_AGENT_VISION_PROVIDER=openai",
                "AIASK_MOA_REFERENCE_MODELS=file-ref-a,file-ref-b",
                "AIASK_MOA_AGGREGATOR_MODEL=file-agg-model",
                "OPENAI_BASE_URL=https://file-api.example.test/v1",
                "OPENAI_API_KEY=sk-file-project-secret",
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("AIASK_AGENT_LOAD_PROJECT_ENV", "1")
    monkeypatch.setenv("AIASK_AGENT_ENV_FILE", str(project_env))
    # The test session sets AIASK_LOAD_PROJECT_ENV=0, under which a named file is ignored.
    monkeypatch.delenv("AIASK_LOAD_PROJECT_ENV", raising=False)
    monkeypatch.setenv("AIASK_AGENT_MODEL_PROVIDER", "mock")
    monkeypatch.setenv("AIASK_AGENT_MODEL", "process-model")
    monkeypatch.setenv("AIASK_AGENT_VISION_MODEL", "process-vision-model")
    monkeypatch.setenv("AIASK_AGENT_VISION_PROVIDER", "mock")
    monkeypatch.setenv("AIASK_MOA_REFERENCE_MODELS", "process-ref")
    monkeypatch.setenv("AIASK_MOA_AGGREGATOR_MODEL", "process-agg-model")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://process.example.test/v1")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-process-secret")

    client = _client(tmp_path, monkeypatch)
    response = client.get("/v1/settings/status", headers={"Authorization": "Bearer secret"})

    assert response.status_code == 200
    payload = response.json()
    raw = json.dumps(payload)
    ai_status = payload["llm"]["ai_status"]
    provider_status = payload["llm"]["providers"]
    assert ai_status["provider"] == "openai"
    assert ai_status["model"] == "file-api-model"
    assert provider_status["default_model"] == "file-api-model"
    assert reference_models() == ["file-ref-a", "file-ref-b"]
    assert aggregator_model("default-agg") == "file-agg-model"
    assert os.environ["AIASK_AGENT_VISION_MODEL"] == "file-vision-model"
    assert os.environ["AIASK_AGENT_VISION_PROVIDER"] == "openai"
    assert ai_status["config_source"]["source"] == "explicit"
    assert "sk-file-project-secret" not in raw
    assert "sk-process-secret" not in raw


def test_ai_config_can_be_saved_from_desktop_and_redacts_secret(tmp_path, monkeypatch) -> None:
    project_env = tmp_path / ".env"
    project_env.write_text("AIASK_AGENT_MODEL_PROVIDER=mock\nAIASK_AGENT_MODEL=mock-local\n", encoding="utf-8")
    monkeypatch.setenv("AIASK_AGENT_LOAD_PROJECT_ENV", "1")
    monkeypatch.setenv("AIASK_AGENT_ENV_FILE", str(project_env))
    # The test session sets AIASK_LOAD_PROJECT_ENV=0, under which a named file is ignored.
    monkeypatch.delenv("AIASK_LOAD_PROJECT_ENV", raising=False)
    client = _client(tmp_path, monkeypatch)

    config = client.get("/v1/ai/config", headers={"Authorization": "Bearer secret"})
    assert config.status_code == 200
    assert {item["id"] for item in config.json()["presets"]} >= {"openai", "deepseek", "dashscope-qwen-cn", "anthropic", "mock"}

    saved = client.patch(
        "/v1/ai/config",
        headers={"Authorization": "Bearer secret"},
        json={
            "preset": "deepseek",
            "model": "deepseek-chat",
            "api_key": "sk-desktop-config-secret",
        },
    )

    assert saved.status_code == 200
    payload = saved.json()
    raw = json.dumps(payload)
    assert payload["saved"] is True
    assert payload["provider"] == "openai"
    assert payload["model"] == "deepseek-chat"
    assert payload["base_url_configured"] is True
    assert payload["api_key_configured"] is True
    assert "OPENAI_API_KEY" in payload["updated_keys"]
    assert "sk-desktop-config-secret" not in raw

    status = client.get("/v1/ai/status")
    assert status.json()["provider"] == "openai"
    assert status.json()["model"] == "deepseek-chat"
    env_text = project_env.read_text(encoding="utf-8")
    assert "AIASK_AGENT_MODEL_PROVIDER=openai" in env_text
    assert "AIASK_AGENT_MODEL=deepseek-chat" in env_text
    assert "OPENAI_BASE_URL=https://api.deepseek.com" in env_text


def test_ai_config_save_requires_control_token(tmp_path, monkeypatch) -> None:
    client = _client(tmp_path, monkeypatch)

    response = client.patch("/v1/ai/config", json={"preset": "mock"})

    assert response.status_code == 401


def test_desktop_local_profile_can_be_saved_and_reloaded(tmp_path, monkeypatch) -> None:
    client = _client(tmp_path, monkeypatch)

    denied = client.patch(
        "/v1/users/local-profile",
        json={"user_id": "desk-user", "profile_name": "Desktop Operator"},
    )
    saved = client.patch(
        "/v1/users/local-profile",
        headers={"Authorization": "Bearer secret"},
        json={"user_id": "desk-user", "profile_name": "Desktop Operator"},
    )
    fetched = client.get("/v1/users/local-profile")

    assert denied.status_code == 401
    assert saved.status_code == 200
    assert saved.json()["user_id"] == "desk-user"
    assert fetched.status_code == 200
    assert fetched.json()["profile_name"] == "Desktop Operator"
    assert fetched.json()["storage"] == "local_file"
    assert fetched.json()["secrets_redacted"] is True


def test_desktop_data_sync_plan_creates_intent_request_without_executing(tmp_path, monkeypatch) -> None:
    client = _client(tmp_path, monkeypatch)

    response = client.post(
        "/v1/data/sync-plan",
        json={"codes": ["600519", "000001"], "task_type": "kline", "period": "daily"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["object"] == "aiask.data_sync_plan"
    assert payload["intent_request"]["action"] == "data_sync.sync"
    assert payload["intent_request"]["params"]["codes"] == ["600519", "000001"]
    assert payload["commands"][0]["path"] == "/intents"
    assert payload["side_effect"]["confirmation_required"] is True
    assert payload["secrets_redacted"] is True


def test_desktop_data_sync_plan_supports_market_temperature_cache_without_codes(tmp_path, monkeypatch) -> None:
    client = _client(tmp_path, monkeypatch)

    response = client.post(
        "/v1/data/sync-plan",
        json={"codes": [], "task_type": "market_temperature_snapshot_cache"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ready"
    assert payload["intent_request"]["action"] == "data_sync.sync"
    assert payload["intent_request"]["params"]["codes"] == []
    assert payload["intent_request"]["params"]["task_type"] == "market_temperature_snapshot_cache"
    assert payload["intent_request"]["params"]["limit"] == 1000
    assert payload["intent_request"]["params"]["top_n"] == 20
    assert payload["intent_request"]["params"]["min_bars"] == 20
    assert payload["side_effect"]["confirmation_required"] is True


def test_desktop_stock_data_sources_can_save_test_and_redact_search_keys(tmp_path, monkeypatch) -> None:
    client = _client(tmp_path, monkeypatch)

    status = client.get("/v1/data/stock-data-sources", headers={"Authorization": "Bearer secret"})
    assert status.status_code == 200
    providers = {item["provider"] for item in status.json()["presets"]}
    assert {"duckduckgo", "tavily", "brave_search", "serpapi", "exa", "akshare"} <= providers

    saved = client.post(
        "/v1/data/stock-data-sources",
        headers={"Authorization": "Bearer secret"},
        json={
            "provider": "tavily",
            "name": "Tavily Desktop",
            "base_url": "https://api.tavily.com",
            "api_key": "tvly-desktop-secret",
            "priority": 10,
            "enabled": True,
        },
    )
    assert saved.status_code == 200
    saved_payload = saved.json()
    raw = json.dumps(saved_payload)
    assert saved_payload["source"]["provider"] == "tavily"
    assert saved_payload["source"]["api_key"] == "[redacted]"
    assert "tvly-desktop-secret" not in raw

    seen_tavily_bodies = []

    def fake_tavily_test(url, *, method="GET", body=None, headers=None, timeout=8.0):
        assert method == "POST"
        assert url == "https://api.tavily.com/search"
        assert headers == {"Authorization": "Bearer tvly-desktop-secret"}
        assert body["query"] == "AIASK data source smoke test"
        seen_tavily_bodies.append(body)
        return 200, {"results": [{"title": "ok", "url": "https://example.com"}]}

    monkeypatch.setattr("aiask_agent.stock_data_sources._http_json", fake_tavily_test)

    smoke = client.post(
        "/v1/data/stock-data-sources/test",
        headers={"Authorization": "Bearer secret"},
        json={"provider": "tavily", "mode": "connectivity"},
    )
    assert smoke.status_code == 200
    assert smoke.json()["success"] is True
    assert "tvly-desktop-secret" not in json.dumps(smoke.json())

    draft_smoke = client.post(
        "/v1/data/stock-data-sources/test",
        headers={"Authorization": "Bearer secret"},
        json={
            "id": saved_payload["source"]["id"],
            "provider": "tavily",
            "mode": "connectivity",
            "source": {
                "id": saved_payload["source"]["id"],
                "provider": "tavily",
                "name": "Tavily Desktop",
                "base_url": "https://api.tavily.com",
                "search_depth": "advanced",
            },
        },
    )
    assert draft_smoke.status_code == 200
    assert draft_smoke.json()["success"] is True
    assert seen_tavily_bodies[-1]["search_depth"] == "advanced"
    assert "tvly-desktop-secret" not in json.dumps(draft_smoke.json())


def test_agent_web_search_uses_configured_search_source(tmp_path, monkeypatch) -> None:
    client = _client(tmp_path, monkeypatch)
    client.post(
        "/v1/data/stock-data-sources",
        headers={"Authorization": "Bearer secret"},
        json={
            "provider": "tavily",
            "name": "Tavily Desktop",
            "base_url": "https://api.tavily.com",
            "api_key": "tvly-desktop-secret",
            "priority": 1,
            "enabled": True,
        },
    )

    def fake_json_request(method, url, payload=None, *, headers=None, timeout=30.0):
        assert method == "POST"
        assert url == "https://api.tavily.com/search"
        assert headers == {"Authorization": "Bearer tvly-desktop-secret"}
        assert payload["query"] == "AIASK search"
        return {
            "ok": True,
            "status_code": 200,
            "body": {
                "answer": "ok",
                "results": [{"title": "AIASK result", "url": "https://example.com/aiask", "content": "result snippet"}],
            },
        }

    monkeypatch.setattr("aiask_agent.native_capabilities._json_request", fake_json_request)
    result = client.post(
        "/v1/tools/agent_web_search",
        headers={"Authorization": "Bearer secret"},
        json={"query": "AIASK search", "provider": "tavily", "limit": 1},
    )

    assert result.status_code == 200
    payload = result.json()
    raw = json.dumps(payload)
    assert payload["success"] is True
    assert payload["data"]["provider"] == "tavily"
    assert payload["data"]["results"][0]["url"] == "https://example.com/aiask"
    assert "tvly-desktop-secret" not in raw


def test_fastapi_read_only_tool_api_blocks_non_read_only_result(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("AIASK_AGENT_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("AIASK_AGENT_LOAD_PROJECT_ENV", "0")
    session_store = AgentSessionStore(tmp_path / "state.sqlite3")
    registry = AgentToolRegistry()

    async def mislabelled_reader(arguments: dict[str, object]) -> dict[str, object]:
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
        session_store=session_store,
        tool_registry=registry,
        max_iterations=2,
    )
    client = TestClient(create_app(runtime=runtime))

    response = client.post(
        "/v1/tools/agent_mislabelled_reader",
        json={"code": "600519", "session_id": "sess_guard"},
    )

    assert response.status_code == 200
    payload = response.json()
    raw = json.dumps(payload, ensure_ascii=False)
    assert payload["success"] is False
    assert payload["error_code"] == "READ_ONLY_TOOL_RESULT_BLOCKED"
    assert payload["data"]["tool"] == "agent_mislabelled_reader"
    assert payload["data"]["side_effect"]["level"] == "stateful"
    assert payload["meta"]["read_only_desktop_guard"]["blocked"] is True
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


def test_fastapi_read_only_tool_api_rejects_confirm_required_full_tools(tmp_path, monkeypatch) -> None:
    client = _client(tmp_path, monkeypatch)

    response = client.post("/v1/tools/agent_browser_click", json={"selector": "#buy"})

    assert response.status_code == 403
    assert "read-only desktop API" in json.dumps(response.json())


def test_desktop_intent_create_is_control_gated_and_returns_intent(tmp_path, monkeypatch) -> None:
    client = _client(tmp_path, monkeypatch)
    payload = {
        "action": "factor_factory.run_once",
        "params": {"candidate_count": 1},
        "rationale": "test desktop intent",
    }

    denied = client.post("/intents", json=payload)
    created = client.post("/intents", headers={"Authorization": "Bearer secret"}, json=payload)

    assert denied.status_code == 401
    assert created.status_code == 200
    body = created.json()
    assert body["success"] is True
    assert body["data"]["intent"]["action"] == "factor_factory.run_once"
    assert body["data"]["intent"]["target_tool"] == "factor_factory"
    assert body["data"]["intent"]["status"] == "awaiting_confirmation"


def test_intent_http_routes_return_public_redacted_intents(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("AIASK_AGENT_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("AIASK_AGENT_CONTROL_TOKEN", "secret")
    session_store = AgentSessionStore(tmp_path / "state.sqlite3")
    intent_store = ActionIntentStore(tmp_path / "intents.sqlite3")
    runtime = AgentRuntime(
        model_client=MockModelClient(),
        session_store=session_store,
        tool_registry=build_default_tool_registry(intent_store, session_store=session_store),
        max_iterations=2,
    )
    client = TestClient(create_app(runtime=runtime, intent_executor=IntentExecutor(intent_store)))

    created = client.post(
        "/intents",
        headers={"Authorization": "Bearer secret"},
        json={
            "action": "data_sync.sync",
            "params": {
                "codes": ["600519"],
                "broker_token": "http-param-secret",
                "nested": {"password": "http-nested-secret"},
                "note": "Authorization: Bearer http-note-secret",
            },
            "rationale": "created with broker_token=http-rationale-secret",
        },
    )
    assert created.status_code == 200
    intent_id = created.json()["data"]["intent"]["intent_id"]
    assert intent_store.get(intent_id)["params"]["broker_token"] == "http-param-secret"

    listed = client.get("/intents")
    fetched = client.get(f"/intents/{intent_id}")
    denied = client.post(
        f"/intents/{intent_id}/deny",
        headers={"Authorization": "Bearer secret"},
        json={"reason": "blocked password=http-deny-secret Authorization: Bearer http-deny-bearer-secret"},
    )

    assert listed.status_code == 200
    assert listed.json()["secrets_redacted"] is True
    assert fetched.status_code == 200
    assert denied.status_code == 200
    assert denied.json()["secrets_redacted"] is True
    combined = json.dumps(
        {
            "created": created.json(),
            "listed": listed.json(),
            "fetched": fetched.json(),
            "denied": denied.json(),
        },
        ensure_ascii=False,
    )
    for secret in (
        "http-param-secret",
        "http-nested-secret",
        "http-note-secret",
        "http-rationale-secret",
        "http-deny-secret",
        "http-deny-bearer-secret",
    ):
        assert secret not in combined
    assert "http-deny-secret" in intent_store.get(intent_id)["error"]


def test_full_runtime_external_message_tool_uses_route_intent_store(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("AIASK_AGENT_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("AIASK_AGENT_ENABLE_HERMES_FULL", "1")
    monkeypatch.setenv("AIASK_AGENT_CONTROL_TOKEN", "secret")
    session_store = AgentSessionStore(tmp_path / "state.sqlite3")
    intent_store = ActionIntentStore(tmp_path / "intents.sqlite3")
    runtime = AgentRuntime(
        model_client=MockModelClient(),
        session_store=session_store,
        tool_registry=build_default_tool_registry(
            ActionIntentStore(tmp_path / "base_intents.sqlite3"),
            session_store=session_store,
        ),
        max_iterations=2,
    )
    client = TestClient(create_app(runtime=runtime, intent_executor=IntentExecutor(intent_store)))

    response = client.post(
        "/v1/hermes/admin/tools/agent_gateway_send_message",
        headers={"Authorization": "Bearer secret"},
        json={"platform": "webhook", "target": "ops", "message": "hello"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["success"] is False
    assert body["error_code"] == "ACTION_INTENT_REQUIRED"
    intents = intent_store.list(status="awaiting_confirmation", limit=10)
    assert len(intents) == 1
    stored = intents[0]
    assert stored["action"] == "gateway.send_message"
    assert stored["params"]["platform"] == "webhook"
    assert stored["params"]["target"] == "ops"
    assert stored["params"]["message"] == "hello"
    assert ActionIntentStore(tmp_path / "base_intents.sqlite3").list(limit=10) == []


def test_admin_full_tool_file_write_creates_intent_without_writing(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("AIASK_AGENT_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("AIASK_AGENT_ENABLE_HERMES_FULL", "1")
    monkeypatch.setenv("AIASK_AGENT_ENABLE_GENERAL_TOOLS", "1")
    monkeypatch.setenv("AIASK_AGENT_TOOLSET", "general_full")
    monkeypatch.setenv("AIASK_AGENT_CONTROL_TOKEN", "secret")
    monkeypatch.setenv("AIASK_AGENT_WORKSPACE_ROOTS", str(tmp_path))
    session_store = AgentSessionStore(tmp_path / "state.sqlite3")
    intent_store = ActionIntentStore(tmp_path / "intents.sqlite3")
    runtime = AgentRuntime(
        model_client=MockModelClient(),
        session_store=session_store,
        tool_registry=build_default_tool_registry(
            ActionIntentStore(tmp_path / "base_intents.sqlite3"),
            session_store=session_store,
        ),
        max_iterations=2,
    )
    client = TestClient(create_app(runtime=runtime, intent_executor=IntentExecutor(intent_store)))

    response = client.post(
        "/v1/hermes/admin/tools/agent_file_write",
        headers={"Authorization": "Bearer secret"},
        json={"path": "admin-write.txt", "content": "must wait for confirmation"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["success"] is False
    assert body["error_code"] == "ACTION_INTENT_REQUIRED"
    assert body["data"]["intent"]["action"] == "file.write"
    assert body["data"]["executed"] is False
    assert not (tmp_path / "admin-write.txt").exists()
    intents = intent_store.list(status="awaiting_confirmation", limit=10)
    assert len(intents) == 1
    assert intents[0]["action"] == "file.write"
    assert intents[0]["params"]["path"] == "admin-write.txt"
    assert ActionIntentStore(tmp_path / "base_intents.sqlite3").list(limit=10) == []


def test_desktop_factor_factory_status_endpoint_uses_safe_facade(tmp_path, monkeypatch) -> None:
    async def fake_factor_status(limit: int = 50) -> dict:
        return {
            "object": "aiask.factor_factory_status",
            "status": "ready",
            "configured": True,
            "active_factors": [{"name": "alpha"}][:limit],
            "engine_health": {"search": "ready"},
            "pool_health": {"active": 1},
            "secrets_redacted": True,
        }

    monkeypatch.setattr("aiask_agent.server.factor_factory_status", fake_factor_status)
    client = _client(tmp_path, monkeypatch)

    response = client.get("/v1/research/factor-factory/status?limit=1")

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ready"
    assert payload["active_factors"] == [{"name": "alpha"}]
    assert payload["secrets_redacted"] is True


def test_desktop_trade_prediction_routes_use_agent_read_facades(tmp_path, monkeypatch) -> None:
    async def fake_call_db_facade(loader, params=None):
        loader_name = getattr(loader, "__name__", "")
        if loader_name == "_load_trade_prediction_status_handler":
            return {
                "success": True,
                "data": {"object": "trade_prediction.status", "sample_n": 2, "status": "ready"},
                "error": None,
                "meta": {"side_effect": {"level": "read_only"}},
            }
        if loader_name == "_load_trade_prediction_outcomes_handler":
            return {
                "success": True,
                "data": {
                    "object": "trade_prediction.outcomes",
                    "items": [{"prediction_id": "tp_1", "score_version": "trade_prediction_score_v2"}],
                    "count": 1,
                },
                "error": None,
                "meta": {"side_effect": {"level": "read_only"}},
            }
        if loader_name == "_load_trade_prediction_matrix_handler":
            return {
                "success": True,
                "data": {
                    "object": "trade_prediction.matrix",
                    "rows": [{"dimension": "family", "value": "momentum", "sample_n": 2}],
                    "row_count": 1,
                },
                "error": None,
                "meta": {"side_effect": {"level": "read_only"}},
            }
        raise AssertionError(loader_name)

    monkeypatch.setattr("aiask_agent.adapters.strategy_factory._call_db_facade", fake_call_db_facade)
    client = _client(tmp_path, monkeypatch)

    status = client.get("/v1/research/trade-predictions/status?strategy_id=s1")
    outcomes = client.get("/v1/research/trade-predictions/outcomes?score_version=trade_prediction_score_v2")
    matrix = client.get("/v1/research/trade-predictions/matrix?dimensions=family")

    assert status.status_code == 200
    assert status.json()["success"] is True
    assert status.json()["data"]["object"] == "trade_prediction.status"
    assert outcomes.status_code == 200
    assert outcomes.json()["data"]["items"][0]["score_version"] == "trade_prediction_score_v2"
    assert matrix.status_code == 200
    assert matrix.json()["data"]["rows"][0]["dimension"] == "family"


def test_desktop_stock_radar_routes_use_agent_read_facades(tmp_path, monkeypatch) -> None:
    calls: list[tuple[str, dict]] = []

    async def fake_stock_radar_facade(loader, params=None):
        payload = dict(params or {})
        loader_name = getattr(loader, "__name__", "")
        calls.append((loader_name, payload))
        if loader_name == "_load_status_handler":
            return {
                "success": True,
                "data": {"object": "stock_radar.status", "status": "completed", "latest_run": {"run_id": payload.get("run_id")}},
                "error": None,
                "meta": {"side_effect": {"level": "read_only"}},
            }
        if loader_name == "_load_candidates_handler":
            return {
                "success": True,
                "data": {
                    "object": "stock_radar.candidates",
                    "candidates": [{"symbol": payload.get("symbol"), "tier": payload.get("tier")}],
                    "count": 1,
                },
                "error": None,
                "meta": {"side_effect": {"level": "read_only"}},
            }
        if loader_name == "_load_digest_handler":
            return {
                "success": True,
                "data": {"object": "stock_radar.digest", "channels": payload.get("channels"), "digest_preview": "preview"},
                "error": None,
                "meta": {"side_effect": {"level": "read_only"}},
            }
        raise AssertionError(loader_name)

    monkeypatch.setattr("aiask_agent.adapters.stock_radar._call_db_handler", fake_stock_radar_facade)
    client = _client(tmp_path, monkeypatch)

    status = client.get("/v1/research/stock-radar/status?run_id=radar_1&limit=5")
    candidates = client.get("/v1/research/stock-radar/candidates?run_id=radar_1&tier=alert&symbol=600000&min_score=80&limit=3")
    digest = client.get("/v1/research/stock-radar/digest?run_id=radar_1&channels=wecom,telegram")

    assert status.status_code == 200
    assert status.json()["success"] is True
    assert candidates.status_code == 200
    assert candidates.json()["data"]["candidates"] == [{"symbol": "600000", "tier": "alert"}]
    assert digest.status_code == 200
    assert digest.json()["data"]["channels"] == ["wecom", "telegram"]
    assert calls == [
        ("_load_status_handler", {"run_id": "radar_1", "limit": 5}),
        ("_load_candidates_handler", {"run_id": "radar_1", "tier": "alert", "symbol": "600000", "min_score": 80.0, "limit": 3}),
        ("_load_digest_handler", {"run_id": "radar_1", "limit": 20, "channels": ["wecom", "telegram"]}),
    ]


def test_gateway_pairing_status_proxies_agent_tool_and_create_uses_intent(tmp_path, monkeypatch) -> None:
    calls: list[tuple[str, dict]] = []

    async def fake_call_tool(tool_name: str, arguments: dict[str, object]) -> dict[str, object]:
        calls.append((tool_name, dict(arguments)))
        return {
            "success": True,
            "data": {
                "action": arguments.get("action"),
                "platform": arguments.get("platform"),
                "user_id": arguments.get("user_id"),
                "session_id": arguments.get("session_id"),
                "configured": True,
            },
            "error": None,
            "meta": {"side_effect": {"level": "stateful" if arguments.get("action") == "create" else "read_only"}},
        }

    monkeypatch.setattr("aiask_agent.app_route_callbacks.AppRouteCallbackFactory.full_tool_call", lambda self, request, tool_name, arguments: fake_call_tool(tool_name, arguments))
    client = _client(tmp_path, monkeypatch)

    status = client.get(
        "/v1/gateway/pairing?platform=feishu&user_id=user-1&session_id=sess-1",
        headers={"Authorization": "Bearer secret"},
    )
    created = client.post(
        "/v1/gateway/pairing",
        headers={"Authorization": "Bearer secret"},
        json={"platform": "discord", "user_id": "user-2", "session_id": "sess-2"},
    )

    assert status.status_code == 200
    assert status.json()["object"] == "gateway.pairing"
    assert status.json()["success"] is True
    assert status.json()["data"]["action"] == "status"
    assert status.json()["data"]["platform"] == "feishu"
    assert status.json()["secrets_redacted"] is True

    assert created.status_code == 200
    assert created.json()["object"] == "gateway.pairing_intent"
    assert created.json()["success"] is True
    assert created.json()["data"]["intent"]["action"] == "gateway.pairing_create"
    assert created.json()["data"]["intent"]["status"] == "awaiting_confirmation"
    assert created.json()["data"]["intent"]["params"]["action"] == "create"
    assert created.json()["data"]["intent"]["params"]["platform"] == "discord"
    assert created.json()["meta"]["action_intent_required"] is True
    assert created.json()["secrets_redacted"] is True

    assert calls == [
        (
            "agent_gateway_pairing",
            {"action": "status", "platform": "feishu", "user_id": "user-1", "session_id": "sess-1"},
        ),
    ]


def test_gateway_directory_refresh_route_creates_intent_without_mutation(tmp_path, monkeypatch) -> None:
    def blocked_refresh(self, *, config=None):
        raise AssertionError("gateway directory refresh must wait for ActionIntent confirmation")

    monkeypatch.setattr("aiask_agent.gateway.stores.GatewayChannelDirectoryStore.refresh", blocked_refresh)
    client = _client(tmp_path, monkeypatch)

    response = client.post(
        "/v1/gateway/directory/refresh",
        headers={"Authorization": "Bearer secret"},
        json={"session_id": "sess_gateway", "rationale": "refresh aliases"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["object"] == "gateway.directory_refresh_intent"
    assert payload["success"] is True
    assert payload["data"]["intent"]["action"] == "gateway.directory_refresh"
    assert payload["data"]["intent"]["status"] == "awaiting_confirmation"
    assert payload["data"]["intent"]["params"]["action"] == "refresh"
    assert payload["meta"]["action_intent_required"] is True
    assert payload["secrets_redacted"] is True


def test_webhook_management_routes_create_intents_without_mutation(tmp_path, monkeypatch) -> None:
    def blocked_subscribe(self, **kwargs):
        raise AssertionError("webhook subscribe must wait for ActionIntent confirmation")

    def blocked_remove(self, webhook_id):
        raise AssertionError("webhook remove must wait for ActionIntent confirmation")

    monkeypatch.setattr("aiask_agent.webhooks.WebhookStore.subscribe", blocked_subscribe)
    monkeypatch.setattr("aiask_agent.webhooks.WebhookStore.remove", blocked_remove)
    client = _client(tmp_path, monkeypatch)

    subscribed = client.post(
        "/v1/webhooks",
        headers={"Authorization": "Bearer secret"},
        json={
            "name": "factory-alerts",
            "events": ["factory.done"],
            "prompt": "Handle {event}: {payload}",
            "session_id": "sess_webhook",
        },
    )
    removed = client.delete(
        "/v1/webhooks/wh_demo",
        headers={"Authorization": "Bearer secret"},
    )

    assert subscribed.status_code == 200
    subscribed_payload = subscribed.json()
    assert subscribed_payload["object"] == "webhook.subscribe_intent"
    assert subscribed_payload["success"] is True
    assert subscribed_payload["data"]["intent"]["action"] == "webhook.subscribe"
    assert subscribed_payload["data"]["intent"]["status"] == "awaiting_confirmation"
    assert subscribed_payload["data"]["intent"]["params"]["name"] == "factory-alerts"
    assert subscribed_payload["meta"]["action_intent_required"] is True
    assert subscribed_payload["secrets_redacted"] is True

    assert removed.status_code == 200
    removed_payload = removed.json()
    assert removed_payload["object"] == "webhook.remove_intent"
    assert removed_payload["success"] is True
    assert removed_payload["data"]["intent"]["action"] == "webhook.remove"
    assert removed_payload["data"]["intent"]["status"] == "awaiting_confirmation"
    assert removed_payload["data"]["intent"]["params"]["webhook_id"] == "wh_demo"
    assert removed_payload["meta"]["action_intent_required"] is True
    assert removed_payload["secrets_redacted"] is True


def test_skill_plugin_management_routes_create_intents_without_mutation(tmp_path, monkeypatch) -> None:
    def blocked_skill_write(*args, **kwargs):
        raise AssertionError("skill management must wait for ActionIntent confirmation")

    def blocked_plugin_write(*args, **kwargs):
        raise AssertionError("plugin management must wait for ActionIntent confirmation")

    monkeypatch.setattr("aiask_agent.native_skill_store.SkillStore.save", blocked_skill_write)
    monkeypatch.setattr("aiask_agent.native_skill_store.SkillStore.set_enabled", blocked_skill_write)
    monkeypatch.setattr("aiask_agent.native_skill_store.SkillStore.archive", blocked_skill_write)
    monkeypatch.setattr("aiask_agent.plugin_runtime.NativePluginManager.update", blocked_plugin_write)
    monkeypatch.setattr("aiask_agent.plugin_runtime.NativePluginManager.set_enabled", blocked_plugin_write)
    client = _client(tmp_path, monkeypatch)

    responses = [
        (
            client.post(
                "/v1/skills",
                headers={"Authorization": "Bearer secret"},
                json={"name": "risk-review", "description": "Risk review", "content": "# Risk Review\n"},
            ),
            "skill.install_intent",
            "skill.install",
        ),
        (
            client.patch(
                "/v1/skills/risk-review",
                headers={"Authorization": "Bearer secret"},
                json={"enabled": False},
            ),
            "skill.set_enabled_intent",
            "skill.set_enabled",
        ),
        (
            client.delete(
                "/v1/skills/risk-review",
                headers={"Authorization": "Bearer secret"},
            ),
            "skill.uninstall_intent",
            "skill.uninstall",
        ),
        (
            client.post(
                "/v1/plugins",
                headers={"Authorization": "Bearer secret"},
                json={"name": "audit-plugin", "manifest": {"description": "Audit", "enabled": False}},
            ),
            "plugin.upsert_intent",
            "plugin.upsert",
        ),
        (
            client.patch(
                "/v1/plugins/audit-plugin",
                headers={"Authorization": "Bearer secret"},
                json={"enabled": False},
            ),
            "plugin.disable_intent",
            "plugin.disable",
        ),
    ]

    for response, object_name, action in responses:
        assert response.status_code == 200
        payload = response.json()
        assert payload["object"] == object_name
        assert payload["success"] is True
        assert payload["data"]["intent"]["action"] == action
        assert payload["data"]["intent"]["status"] == "awaiting_confirmation"
        assert payload["meta"]["action_intent_required"] is True
        assert payload["secrets_redacted"] is True


def test_mcp_management_routes_create_intents_without_mutation(tmp_path, monkeypatch) -> None:
    def blocked_mcp_write(*args, **kwargs):
        raise AssertionError("MCP management must wait for ActionIntent confirmation")

    monkeypatch.setattr("aiask_agent.mcp_client.MCPAggregator.register_client_server", blocked_mcp_write)
    monkeypatch.setattr("aiask_agent.mcp_client.MCPAggregator.remove_server", blocked_mcp_write)
    monkeypatch.setattr("aiask_agent.mcp_client.MCPAggregator.register_local_server", blocked_mcp_write)
    monkeypatch.setattr("aiask_agent.mcp_client.MCPAggregator.discover_and_update", blocked_mcp_write)
    monkeypatch.setattr("aiask_agent.mcp_client.MCPAggregator.oauth_callback", blocked_mcp_write)
    client = _client(tmp_path, monkeypatch)

    responses = [
        (
            client.post(
                "/v1/mcp/servers",
                headers={"Authorization": "Bearer secret"},
                json={
                    "name": "broker",
                    "transport": "streamable_http",
                    "url": "http://127.0.0.1:8765/mcp",
                    "domain": "financial",
                },
            ),
            "mcp.server_upsert_intent",
            "mcp.server_upsert",
        ),
        (
            client.patch(
                "/v1/mcp/servers/broker",
                headers={"Authorization": "Bearer secret"},
                json={"enabled": False},
            ),
            "mcp.server_update_intent",
            "mcp.server_update",
        ),
        (
            client.delete(
                "/v1/mcp/servers/broker",
                headers={"Authorization": "Bearer secret"},
            ),
            "mcp.server_remove_intent",
            "mcp.server_remove",
        ),
        (
            client.post(
                "/v1/mcp/register-local",
                headers={"Authorization": "Bearer secret"},
                json={"name": "local-broker", "url": "http://127.0.0.1:8765/mcp", "domain": "financial"},
            ),
            "mcp.register_local_intent",
            "mcp.register_local",
        ),
        (
            client.post(
                "/v1/mcp/discover",
                headers={"Authorization": "Bearer secret"},
                json={"server": "broker"},
            ),
            "mcp.discover_update_intent",
            "mcp.discover_update",
        ),
        (
            client.post(
                "/v1/mcp/oauth/callback",
                headers={"Authorization": "Bearer secret"},
                json={"server": "broker", "access_token": "oauth-route-secret"},
            ),
            "mcp.oauth_callback_intent",
            "mcp.oauth_callback",
        ),
    ]

    for response, object_name, action in responses:
        assert response.status_code == 200
        payload = response.json()
        assert payload["object"] == object_name
        assert payload["success"] is True
        assert payload["data"]["intent"]["action"] == action
        assert payload["data"]["intent"]["status"] == "awaiting_confirmation"
        assert payload["meta"]["action_intent_required"] is True
        assert payload["secrets_redacted"] is True
    assert "oauth-route-secret" not in json.dumps([response.json() for response, _, _ in responses], ensure_ascii=False)


def test_learning_rl_http_routes_create_intents_without_mutation(tmp_path, monkeypatch) -> None:
    def blocked_learning_apply(*args, **kwargs):
        raise AssertionError("learning apply must wait for ActionIntent confirmation")

    def blocked_rl_write(*args, **kwargs):
        raise AssertionError("RL mutation must wait for ActionIntent confirmation")

    monkeypatch.setattr("aiask_agent.learning_loop.LearningLoop.apply", blocked_learning_apply)
    monkeypatch.setattr("aiask_agent.rl_atropos.RLAtroposManager.edit_config", blocked_rl_write)
    monkeypatch.setattr("aiask_agent.rl_atropos.RLAtroposManager.start_training", blocked_rl_write)
    monkeypatch.setattr("aiask_agent.rl_atropos.RLAtroposManager.stop_training", blocked_rl_write)
    monkeypatch.setenv("AIASK_AGENT_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("AIASK_AGENT_ENABLE_HERMES_FULL", "1")
    monkeypatch.setenv("AIASK_AGENT_ENABLE_GENERAL_TOOLS", "1")
    monkeypatch.setenv("AIASK_AGENT_TOOLSET", "general_full")
    monkeypatch.setenv("AIASK_AGENT_CONTROL_TOKEN", "secret")
    monkeypatch.setenv("AIASK_AGENT_LOAD_PROJECT_ENV", "0")

    session_store = AgentSessionStore(tmp_path / "state.sqlite3")
    intent_store = ActionIntentStore(tmp_path / "intents.sqlite3")
    runtime = AgentRuntime(
        model_client=MockModelClient(),
        session_store=session_store,
        max_iterations=2,
    )
    client = TestClient(create_app(runtime=runtime, intent_executor=IntentExecutor(intent_store)))

    responses = [
        (
            client.post(
                "/v1/learning/apply",
                headers={"Authorization": "Bearer secret"},
                json={"proposal_id": "learn_demo", "rationale": "apply after review"},
            ),
            "learning.apply_intent",
            "learning.apply",
        ),
        (
            client.patch(
                "/v1/rl/config",
                headers={"Authorization": "Bearer secret"},
                json={"config": {"learning_rate": 0.01, "total_steps": 1000}},
            ),
            "rl.edit_config_intent",
            "rl.edit_config",
        ),
        (
            client.post(
                "/v1/rl/runs",
                headers={"Authorization": "Bearer secret"},
                json={"environment": "demo", "config": {"learning_rate": 0.01}},
            ),
            "rl.start_training_intent",
            "rl.start_training",
        ),
        (
            client.post(
                "/v1/rl/runs/rlrun_demo/stop",
                headers={"Authorization": "Bearer secret"},
                json={},
            ),
            "rl.stop_training_intent",
            "rl.stop_training",
        ),
    ]

    for response, object_name, action in responses:
        assert response.status_code == 200
        payload = response.json()
        raw = json.dumps(payload, ensure_ascii=False)
        assert payload["object"] == object_name
        assert payload["success"] is True
        assert payload["data"]["intent"]["action"] == action
        assert payload["data"]["intent"]["status"] == "awaiting_confirmation"
        assert "_state_path" not in payload["data"]["intent"]["params"]
        assert str(session_store.path) not in raw
        assert payload["meta"]["action_intent_required"] is True
        assert payload["secrets_redacted"] is True

    stored_by_action = {item["action"]: item for item in intent_store.list(status="awaiting_confirmation", limit=10)}
    assert set(stored_by_action) == {"learning.apply", "rl.edit_config", "rl.start_training", "rl.stop_training"}
    for stored in stored_by_action.values():
        assert stored["params"]["_state_path"] == str(session_store.path)
    assert stored_by_action["learning.apply"]["params"]["proposal_id"] == "learn_demo"
    assert stored_by_action["rl.edit_config"]["params"]["config"]["learning_rate"] == 0.01
    assert stored_by_action["rl.start_training"]["params"]["environment"] == "demo"
    assert stored_by_action["rl.stop_training"]["params"]["run_id"] == "rlrun_demo"
    runtime.close()


def test_control_plane_error_responses_redact_secret_text(tmp_path, monkeypatch) -> None:
    client = _client(tmp_path, monkeypatch)

    ai_error = client.patch(
        "/v1/ai/config",
        headers={"Authorization": "Bearer secret"},
        json={"provider": "bad broker_token=ai-config-secret"},
    )
    source_error = client.post(
        "/v1/data/stock-data-sources",
        headers={"Authorization": "Bearer secret"},
        json={"provider": "bad password=source-save-secret"},
    )
    job_error = client.post(
        "/v1/jobs/job_missing%20broker_token=job-run-secret/run",
        headers={"Authorization": "Bearer secret"},
        json={},
    )
    validation_error = client.get("/v1/jobs/job/runs?limit=bad%20broker_token=validation-secret")
    run_error = client.get("/v1/runs/run_missing%20broker_token=run-trace-secret/trace-eval")
    session_error = client.post(
        "/v1/sessions/session_missing%20password=session-archive-secret/archive",
        headers={"Authorization": "Bearer secret"},
        json={"archived": True},
    )

    for response in (ai_error, source_error, job_error, validation_error, run_error, session_error):
        assert response.status_code in {400, 404, 422}
        assert response.json()["secrets_redacted"] is True
        raw = json.dumps(response.json(), ensure_ascii=False)
        assert "ai-config-secret" not in raw
        assert "source-save-secret" not in raw
        assert "job-run-secret" not in raw
        assert "validation-secret" not in raw
        assert "run-trace-secret" not in raw
        assert "session-archive-secret" not in raw
    assert ai_error.json()["detail"] == "unsupported model provider: bad broker_token=[redacted]"
    assert source_error.json()["detail"] == "unsupported stock data source provider: bad password=[redacted]"
    assert job_error.json()["detail"] == "job not found: job_missing broker_token=[redacted]"
    assert validation_error.status_code == 422
    assert run_error.json()["detail"] == "run not found: run_missing broker_token=[redacted]"
    assert session_error.json()["detail"] == "session not found: session_missing password=[redacted]"


def test_stock_data_source_test_redacts_provider_error_and_preview(tmp_path, monkeypatch) -> None:
    client = _client(tmp_path, monkeypatch)

    def fake_http_json(url, *, method="GET", body=None, headers=None, timeout=8.0):
        return 500, {
            "error": "failed Authorization: Bearer stock-source-bearer-secret broker_token=stock-source-token-secret",
            "raw_preview": "preview password=stock-source-preview-secret",
        }

    monkeypatch.setattr("aiask_agent.stock_data_sources._http_json", fake_http_json)
    response = client.post(
        "/v1/data/stock-data-sources/test",
        headers={"Authorization": "Bearer secret"},
        json={
            "provider": "tavily",
            "mode": "connectivity",
            "source": {
                "provider": "tavily",
                "base_url": "https://api.tavily.com",
                "api_key": "stock-source-api-secret",
            },
        },
    )

    assert response.status_code == 200
    payload = response.json()
    raw = json.dumps(payload, ensure_ascii=False)
    assert payload["success"] is False
    assert payload["secrets_redacted"] is True
    assert "stock-source-bearer-secret" not in raw
    assert "stock-source-token-secret" not in raw
    assert "stock-source-preview-secret" not in raw
    assert "stock-source-api-secret" not in raw
    assert payload["error"] == "failed Authorization: Bearer [redacted] broker_token=[redacted]"


def test_gateway_pairing_redacts_tool_result_error_data_and_meta(tmp_path, monkeypatch) -> None:
    async def fake_call_tool(tool_name: str, arguments: dict[str, object]) -> dict[str, object]:
        return {
            "success": False,
            "data": {
                "broker_token": "gateway-data-secret",
                "note": "Authorization: Bearer gateway-data-bearer-secret",
            },
            "error": "failed password=gateway-error-secret",
            "error_code": "PAIRING_FAILED",
            "meta": {"debug": {"password": "gateway-meta-secret"}},
        }

    monkeypatch.setattr("aiask_agent.app_route_callbacks.AppRouteCallbackFactory.full_tool_call", lambda self, request, tool_name, arguments: fake_call_tool(tool_name, arguments))
    client = _client(tmp_path, monkeypatch)
    response = client.get(
        "/v1/gateway/pairing?platform=feishu&user_id=user-1&session_id=sess-1",
        headers={"Authorization": "Bearer secret"},
    )

    assert response.status_code == 200
    payload = response.json()
    raw = json.dumps(payload, ensure_ascii=False)
    assert payload["success"] is False
    assert payload["secrets_redacted"] is True
    assert "gateway-data-secret" not in raw
    assert "gateway-data-bearer-secret" not in raw
    assert "gateway-error-secret" not in raw
    assert "gateway-meta-secret" not in raw
    assert payload["data"]["broker_token"] == "[redacted]"
    assert payload["data"]["note"] == "Authorization: Bearer [redacted]"
    assert payload["error"] == "failed password=[redacted]"
    assert payload["meta"]["debug"]["password"] == "[redacted]"


def test_gateway_http_routes_redact_delivery_and_platform_secret_payloads(tmp_path, monkeypatch) -> None:
    from aiask_agent.gateway_route_factories import GatewayRouteFactories

    class FakeDeliveryRouter:
        async def send(self, **kwargs):
            raise AssertionError("HTTP gateway send routes must create an ActionIntent instead of direct delivery")

        async def retry(self, message_id):
            raise AssertionError("HTTP gateway retry route must create an ActionIntent instead of direct delivery")

    class FakeAdapter:
        async def start(self):
            raise AssertionError("HTTP gateway platform start route must create an ActionIntent instead of direct platform control")

        async def stop(self):
            raise AssertionError("HTTP gateway platform stop route must create an ActionIntent instead of direct platform control")

        async def health(self):
            return {
                "tenant_access_token": "platform-health-secret",
                "response": {
                    "url": "https://platform.test/health?access_token=platform-query-secret",
                    "headers": {"Authorization": "Bearer platform-health-bearer"},
                },
            }

    def fake_delivery_router(self, *, messages=None, directory=None):
        return FakeDeliveryRouter()

    def fake_adapter_for(status):
        return FakeAdapter()

    monkeypatch.setattr(GatewayRouteFactories, "delivery_router", fake_delivery_router)
    monkeypatch.setattr(GatewayRouteFactories, "adapter_for", staticmethod(fake_adapter_for))
    client = _client(tmp_path, monkeypatch)

    responses = [
        client.post(
            "/v1/gateway/send",
            headers={"Authorization": "Bearer secret"},
            json={"platform": "local", "target": "desktop", "message": "hello", "session_id": "sess_gateway"},
        ),
        client.post(
            "/v1/gateway/direct-deliver",
            headers={"Authorization": "Bearer secret"},
            json={"platform": "local", "target": "desktop", "message": "hello", "session_id": "sess_gateway"},
        ),
        client.post(
            "/v1/gateway/messages/gwmsg_original/retry",
            headers={"Authorization": "Bearer secret"},
            json={"session_id": "sess_gateway"},
        ),
        client.post("/v1/gateway/platforms/feishu/start", headers={"Authorization": "Bearer secret"}),
        client.post("/v1/gateway/platforms/feishu/stop", headers={"Authorization": "Bearer secret"}),
        client.get("/v1/gateway/platforms/feishu/health", headers={"Authorization": "Bearer secret"}),
    ]

    for response in responses:
        assert response.status_code == 200
        payload = response.json()
        raw = json.dumps(payload, ensure_ascii=False)
        assert payload["secrets_redacted"] is True
        assert "delivery-access-secret" not in raw
        assert "delivery-query-secret" not in raw
        assert "delivery-tenant-secret" not in raw
        assert "delivery-bearer-secret" not in raw
        assert "retry-client-secret" not in raw
        assert "retry-broker-secret" not in raw
        assert "platform-start-secret" not in raw
        assert "platform-start-bearer" not in raw
        assert "platform-stop-secret" not in raw
        assert "platform-health-secret" not in raw
        assert "platform-query-secret" not in raw
        assert "platform-health-bearer" not in raw

    send_intent = responses[0].json()
    direct_intent = responses[1].json()
    retry_intent = responses[2].json()
    platform_start_intent = responses[3].json()
    platform_stop_intent = responses[4].json()
    assert send_intent["object"] == "gateway.message_intent"
    assert send_intent["data"]["intent"]["action"] == "gateway.send_message"
    assert send_intent["data"]["intent"]["status"] == "awaiting_confirmation"
    assert direct_intent["object"] == "gateway.direct_delivery_intent"
    assert direct_intent["data"]["intent"]["action"] == "gateway.direct_deliver"
    assert direct_intent["data"]["intent"]["status"] == "awaiting_confirmation"
    assert retry_intent["object"] == "gateway.retry_intent"
    assert retry_intent["data"]["intent"]["action"] == "gateway.retry"
    assert retry_intent["data"]["intent"]["status"] == "awaiting_confirmation"
    assert retry_intent["data"]["intent"]["params"]["message_id"] == "gwmsg_original"
    assert platform_start_intent["object"] == "gateway.platform_start_intent"
    assert platform_start_intent["data"]["intent"]["action"] == "gateway.platform_start"
    assert platform_start_intent["data"]["intent"]["status"] == "awaiting_confirmation"
    assert platform_start_intent["data"]["intent"]["params"]["platform"] == "feishu"
    assert platform_stop_intent["object"] == "gateway.platform_stop_intent"
    assert platform_stop_intent["data"]["intent"]["action"] == "gateway.platform_stop"
    assert platform_stop_intent["data"]["intent"]["status"] == "awaiting_confirmation"
    assert platform_stop_intent["data"]["intent"]["params"]["platform"] == "feishu"


def test_gateway_message_and_directory_metadata_are_redacted_at_rest(tmp_path, monkeypatch) -> None:
    from aiask_agent.gateway import GatewayChannelDirectoryStore, GatewayMessageStore

    db_path = tmp_path / "state.sqlite3"
    message_store = GatewayMessageStore(db_path)
    directory_store = GatewayChannelDirectoryStore(db_path)

    message = message_store.record(
        direction="outbound",
        platform="feishu",
        target="chat_1",
        content="hello",
        status="failed",
        metadata={
            "adapter": {
                "tenant_access_token": "stored-tenant-secret",
                "error": "Authorization: Bearer stored-bearer-secret",
                "response": {"url": "https://platform.test/send?access_token=stored-query-secret"},
            }
        },
    )
    directory = directory_store.upsert(
        platform="feishu",
        name="ops",
        target="chat_1",
        metadata={"clientSecret": "directory-client-secret", "note": "broker_token=directory-broker-secret"},
    )

    raw = json.dumps({"message": message, "directory": directory}, ensure_ascii=False)
    assert "stored-tenant-secret" not in raw
    assert "stored-bearer-secret" not in raw
    assert "stored-query-secret" not in raw
    assert "directory-client-secret" not in raw
    assert "directory-broker-secret" not in raw
    assert message["metadata"]["adapter"]["tenant_access_token"] == "[redacted]"
    assert message["metadata"]["adapter"]["error"] == "Authorization: Bearer [redacted]"
    assert directory["metadata"]["clientSecret"] == "[redacted]"
