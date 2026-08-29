from __future__ import annotations

import asyncio
import sys
import types
from typing import Any

import pytest
from fastapi.testclient import TestClient

from aiask_agent.model_client import AnthropicMessagesClient, ModelResponse, MockModelClient
from aiask_agent.runtime import AgentRuntime
from aiask_agent.server import create_app
from aiask_agent.session_store import AgentSessionStore


class RecordingModel:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def complete(self, *, messages: list[dict[str, Any]], tools: list[dict[str, Any]], model: str) -> ModelResponse:
        self.calls.append({"messages": messages, "tools": tools, "model": model})
        return ModelResponse(content=f"smoke-ok:{model}", usage={"total_tokens": 3})


def test_ai_status_reports_mock_runtime_without_secrets(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    monkeypatch.setenv("AIASK_AGENT_MODEL_PROVIDER", "mock")
    runtime = AgentRuntime(model_client=MockModelClient(), session_store=AgentSessionStore(tmp_path / "state.sqlite3"))
    client = TestClient(create_app(runtime=runtime))

    response = client.get("/v1/ai/status")
    assert response.status_code == 200
    payload = response.json()
    assert payload["provider"] == "mock"
    assert payload["mock"] is True
    assert payload["configured"] is True
    assert payload["secrets_redacted"] is True
    assert "sk-" not in str(payload)


def test_ai_smoke_uses_runtime_model_client_and_model_override(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("AIASK_AGENT_MODEL_PROVIDER", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("OPENAI_BASE_URL", "http://localhost:8317/v1")
    model = RecordingModel()
    runtime = AgentRuntime(model_client=model, model="default-model", session_store=AgentSessionStore(tmp_path / "state.sqlite3"))
    client = TestClient(create_app(runtime=runtime))

    response = client.post("/v1/ai/smoke", json={"model": "custom-model", "prompt": "ping"})
    assert response.status_code == 200
    payload = response.json()
    assert payload["success"] is True
    assert payload["configured"] is True
    assert payload["model"] == "custom-model"
    assert payload["response_preview"] == "smoke-ok:custom-model"
    assert model.calls[0]["messages"][0]["content"] == "ping"


def test_ai_smoke_returns_structured_error_for_model_failure(tmp_path, monkeypatch) -> None:
    class FailingModel:
        async def complete(self, *, messages: list[dict[str, Any]], tools: list[dict[str, Any]], model: str) -> ModelResponse:
            raise TimeoutError("model timeout")

    monkeypatch.setenv("AIASK_AGENT_MODEL_PROVIDER", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    runtime = AgentRuntime(model_client=FailingModel(), session_store=AgentSessionStore(tmp_path / "state.sqlite3"))
    client = TestClient(create_app(runtime=runtime))

    response = client.post("/v1/ai/smoke", json={})
    assert response.status_code == 200
    payload = response.json()
    assert payload["success"] is False
    assert payload["error_code"] == "TIMEOUT"
    assert payload["secrets_redacted"] is True


def test_ai_models_lists_openai_compatible_models_with_fake_client(tmp_path, monkeypatch) -> None:
    class FakeModel:
        def __init__(self, model_id: str) -> None:
            self.id = model_id

        def model_dump(self) -> dict[str, str]:
            return {"id": self.id, "owned_by": "fake"}

    class FakeModels:
        async def list(self):
            return types.SimpleNamespace(data=[FakeModel("model-a"), {"id": "model-b"}])

    class FakeAsyncOpenAI:
        def __init__(self, *, api_key: str | None = None, base_url: str | None = None) -> None:
            self.api_key = api_key
            self.base_url = base_url
            self.models = FakeModels()

        async def close(self) -> None:
            return None

    fake_openai = types.ModuleType("openai")
    fake_openai.AsyncOpenAI = FakeAsyncOpenAI
    monkeypatch.setitem(sys.modules, "openai", fake_openai)
    monkeypatch.setenv("AIASK_AGENT_MODEL_PROVIDER", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("OPENAI_BASE_URL", "http://localhost:8317/v1")
    runtime = AgentRuntime(model_client=MockModelClient(), session_store=AgentSessionStore(tmp_path / "state.sqlite3"))
    client = TestClient(create_app(runtime=runtime))

    response = client.get("/v1/ai/models")
    assert response.status_code == 200
    payload = response.json()
    assert payload["configured"] is True
    assert {item["id"] for item in payload["data"]} == {"model-a", "model-b"}
    assert payload["secrets_redacted"] is True


def test_ai_models_lists_anthropic_models_with_native_headers(tmp_path, monkeypatch) -> None:
    calls: list[dict[str, Any]] = []

    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, Any]:
            return {
                "data": [
                    {"id": "claude-sonnet-4-5", "display_name": "Claude Sonnet 4.5"},
                    {"id": "claude-haiku-4-5", "api_key": "sk-secret-from-provider"},
                ]
            }

    class FakeAsyncClient:
        def __init__(self, **kwargs: Any) -> None:
            calls.append({"init": kwargs})

        async def __aenter__(self) -> "FakeAsyncClient":
            return self

        async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
            return None

        async def get(self, url: str, *, headers: dict[str, str]) -> FakeResponse:
            calls.append({"url": url, "headers": headers})
            return FakeResponse()

    fake_httpx = types.ModuleType("httpx")
    fake_httpx.AsyncClient = FakeAsyncClient
    monkeypatch.setitem(sys.modules, "httpx", fake_httpx)
    monkeypatch.setenv("AIASK_AGENT_MODEL_PROVIDER", "anthropic")
    monkeypatch.setenv("AIASK_AGENT_MODEL", "claude-sonnet-4-5")
    monkeypatch.setenv("OPENAI_API_KEY", "anthropic-test-key")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://api.anthropic.com")
    runtime = AgentRuntime(model_client=MockModelClient(), session_store=AgentSessionStore(tmp_path / "state.sqlite3"))
    client = TestClient(create_app(runtime=runtime))

    response = client.get("/v1/ai/models")

    assert response.status_code == 200
    payload = response.json()
    assert payload["configured"] is True
    assert payload["provider"] == "anthropic"
    assert payload["unsupported"] is False
    assert {item["id"] for item in payload["data"]} == {"claude-sonnet-4-5", "claude-haiku-4-5"}
    assert payload["data"][1]["api_key"] == "[REDACTED]"
    assert payload["secrets_redacted"] is True
    request_call = next(item for item in calls if "url" in item)
    assert request_call["url"] == "https://api.anthropic.com/v1/models"
    assert request_call["headers"]["x-api-key"] == "anthropic-test-key"
    assert request_call["headers"]["anthropic-version"] == "2023-06-01"


def test_anthropic_prompt_cache_marks_system_and_recent_messages(monkeypatch) -> None:
    monkeypatch.setenv("AIASK_AGENT_MODEL_PROVIDER", "anthropic")
    monkeypatch.setenv("AIASK_AGENT_PROMPT_CACHE_ENABLED", "1")
    monkeypatch.setenv("AIASK_AGENT_PROMPT_CACHE_RECENT_MESSAGES", "2")

    messages, system = AnthropicMessagesClient._adapt_messages([
        {"role": "system", "content": "stable system prompt"},
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "second"},
        {"role": "user", "content": "third"},
    ])
    cached_messages, cached_system, evidence = AnthropicMessagesClient._apply_prompt_cache_policy(messages, system)

    assert evidence["applied"] is True
    assert evidence["system"] is True
    assert evidence["message_count"] == 2
    assert isinstance(cached_system, list)
    assert cached_system[0]["cache_control"] == {"type": "ephemeral"}
    assert isinstance(cached_messages[-1]["content"], list)
    assert cached_messages[-1]["content"][0]["cache_control"] == {"type": "ephemeral"}
    assert isinstance(cached_messages[-2]["content"], list)
    assert cached_messages[-2]["content"][0]["cache_control"] == {"type": "ephemeral"}
    assert isinstance(cached_messages[0]["content"], str)


def test_ai_config_exposes_and_saves_prompt_cache_policy(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("AIASK_AGENT_MODEL_PROVIDER", "anthropic")
    monkeypatch.setenv("AIASK_AGENT_MODEL", "claude-sonnet-4-5")
    monkeypatch.setenv("OPENAI_API_KEY", "anthropic-test-key")
    monkeypatch.setenv("AIASK_AGENT_CONTROL_TOKEN", "secret")
    monkeypatch.setenv("AIASK_AGENT_PROMPT_CACHE_ENABLED", "1")
    monkeypatch.setenv("AIASK_AGENT_PROMPT_CACHE_RECENT_MESSAGES", "3")
    monkeypatch.setenv("AIASK_AGENT_ENV_FILE", str(tmp_path / ".env"))
    runtime = AgentRuntime(model_client=MockModelClient(), session_store=AgentSessionStore(tmp_path / "state.sqlite3"))
    client = TestClient(create_app(runtime=runtime))

    payload = client.get("/v1/ai/config").json()

    assert payload["current"]["prompt_cache"]["enabled"] is True
    assert payload["current"]["prompt_cache"]["recent_non_system_messages"] == 3
    assert payload["editable"]["prompt_cache_enabled_env"] == "AIASK_AGENT_PROMPT_CACHE_ENABLED"

    response = client.patch(
        "/v1/ai/config",
        json={
            "provider": "anthropic",
            "model": "claude-sonnet-4-5",
            "base_url": "https://api.anthropic.com/v1",
            "prompt_cache_enabled": False,
            "prompt_cache_recent_messages": 1,
        },
        headers={"Authorization": "Bearer secret"},
    )

    assert response.status_code == 200
    saved = response.json()
    assert saved["prompt_cache"]["requested_enabled"] is False
    assert "AIASK_AGENT_PROMPT_CACHE_ENABLED" in saved["updated_keys"]
    assert "AIASK_AGENT_PROMPT_CACHE_RECENT_MESSAGES" in saved["updated_keys"]


def test_ai_smoke_accepts_openai_compatible_string_response(tmp_path, monkeypatch) -> None:
    created_clients: list[dict[str, str | None]] = []

    class FakeCompletions:
        async def create(self, **kwargs):
            return "AIASK_LIVE_OK"

    class FakeChat:
        def __init__(self) -> None:
            self.completions = FakeCompletions()

    class FakeAsyncOpenAI:
        def __init__(self, *, api_key: str | None = None, base_url: str | None = None) -> None:
            self.api_key = api_key
            self.base_url = base_url
            created_clients.append({"api_key": api_key, "base_url": str(base_url) if base_url is not None else None})
            self.chat = FakeChat()

        async def close(self) -> None:
            return None

    fake_openai = types.ModuleType("openai")
    fake_openai.AsyncOpenAI = FakeAsyncOpenAI
    monkeypatch.setitem(sys.modules, "openai", fake_openai)
    monkeypatch.setenv("AIASK_AGENT_MODEL_PROVIDER", "openai")
    monkeypatch.setenv("AIASK_AGENT_MODEL", "compat-model")
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("OPENAI_BASE_URL", "http://localhost:8317")
    runtime = AgentRuntime(session_store=AgentSessionStore(tmp_path / "state.sqlite3"))
    client = TestClient(create_app(runtime=runtime))

    response = client.post("/v1/ai/smoke", json={"prompt": "ping"})

    assert response.status_code == 200
    payload = response.json()
    assert payload["success"] is True
    assert payload["model"] == "compat-model"
    assert payload["response_preview"] == "AIASK_LIVE_OK"
    assert payload["secrets_redacted"] is True
    assert created_clients[0]["base_url"] == "http://localhost:8317/v1"


def test_ai_smoke_rejects_html_gateway_response(tmp_path, monkeypatch) -> None:
    class FakeCompletions:
        async def create(self, **kwargs):
            return "<!doctype html><html><title>Gateway</title></html>"

    class FakeChat:
        def __init__(self) -> None:
            self.completions = FakeCompletions()

    class FakeAsyncOpenAI:
        def __init__(self, *, api_key: str | None = None, base_url: str | None = None) -> None:
            self.chat = FakeChat()

        async def close(self) -> None:
            return None

    fake_openai = types.ModuleType("openai")
    fake_openai.AsyncOpenAI = FakeAsyncOpenAI
    monkeypatch.setitem(sys.modules, "openai", fake_openai)
    monkeypatch.setenv("AIASK_AGENT_MODEL_PROVIDER", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("OPENAI_BASE_URL", "http://localhost:8317")
    runtime = AgentRuntime(session_store=AgentSessionStore(tmp_path / "state.sqlite3"))
    client = TestClient(create_app(runtime=runtime))

    response = client.post("/v1/ai/smoke", json={"prompt": "ping"})

    assert response.status_code == 200
    payload = response.json()
    assert payload["success"] is False
    assert payload["error_code"] == "AI_SMOKE_FAILED"
    assert "returned HTML" in payload["error"]
    assert payload["secrets_redacted"] is True


def test_ai_models_falls_back_to_configured_model_on_nonstandard_provider(tmp_path, monkeypatch) -> None:
    created_clients: list[dict[str, str | None]] = []

    class FakeModels:
        async def list(self):
            raise AttributeError("'str' object has no attribute '_set_private_attributes'")

    class FakeAsyncOpenAI:
        def __init__(self, *, api_key: str | None = None, base_url: str | None = None) -> None:
            self.api_key = api_key
            self.base_url = base_url
            created_clients.append({"api_key": api_key, "base_url": str(base_url) if base_url is not None else None})
            self.models = FakeModels()

        async def close(self) -> None:
            return None

    fake_openai = types.ModuleType("openai")
    fake_openai.AsyncOpenAI = FakeAsyncOpenAI
    monkeypatch.setitem(sys.modules, "openai", fake_openai)
    monkeypatch.setenv("AIASK_AGENT_MODEL_PROVIDER", "openai")
    monkeypatch.setenv("AIASK_AGENT_MODEL", "compat-model")
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("OPENAI_BASE_URL", "http://localhost:8317")
    runtime = AgentRuntime(model_client=MockModelClient(), session_store=AgentSessionStore(tmp_path / "state.sqlite3"))
    client = TestClient(create_app(runtime=runtime))

    response = client.get("/v1/ai/models")

    assert response.status_code == 200
    payload = response.json()
    assert payload["configured"] is True
    assert payload["unsupported"] is True
    assert payload["data"] == [{"id": "compat-model", "owned_by": "openai", "fallback": True}]
    assert payload["warning_code"] == "AI_SMOKE_FAILED"
    assert "error" not in payload
    assert payload["secrets_redacted"] is True
    assert created_clients[0]["base_url"] == "http://localhost:8317/v1"


def test_live_ai_smoke_when_configured(tmp_path, request, monkeypatch) -> None:
    if not request.config.getoption("--run-live-ai"):
        pytest.skip("live AI smoke tests disabled")
    if not sys.modules.get("openai") and not __import__("importlib").util.find_spec("openai"):
        pytest.skip("openai package is not installed")
    if not __import__("os").getenv("OPENAI_API_KEY"):
        pytest.skip("OPENAI_API_KEY is not configured")
    runtime = AgentRuntime(session_store=AgentSessionStore(tmp_path / "state.sqlite3"))
    client = TestClient(create_app(runtime=runtime))
    response = client.post("/v1/ai/smoke", json={"prompt": "Reply with ok."})
    assert response.status_code == 200
    assert response.json()["configured"] is True
