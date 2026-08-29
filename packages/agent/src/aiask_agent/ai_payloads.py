from __future__ import annotations

import os
import time
from typing import Any

from .env_config import load_project_env, project_env_status, update_project_env_values, writable_project_env_path
from .model_client import MockModelClient, _openai_compatible_api_base, build_model_client_from_env
from .model_providers import ModelProviderRegistry, ProviderUsageStore, prompt_cache_policy
from .runtime import AgentRuntime


AI_MODEL_PROVIDER_PRESETS: list[dict[str, Any]] = [
    # ---- 国际厂商 (international) ----
    {
        "id": "openai",
        "label": "OpenAI",
        "provider": "openai",
        "provider_type": "openai",
        "category": "international",
        "base_url": "https://api.openai.com/v1",
        "default_model": "gpt-4.1-mini",
        "api_key_url": "https://platform.openai.com/api-keys",
        "docs_url": "https://platform.openai.com/docs/api-reference/models/list",
        "model_list_supported": True,
        "notes": ["Uses the official OpenAI /v1/models endpoint."],
    },
    {
        "id": "anthropic",
        "label": "Anthropic Claude",
        "provider": "anthropic",
        "provider_type": "anthropic_messages",
        "category": "international",
        "base_url": "https://api.anthropic.com/v1",
        "default_model": "claude-sonnet-4-5",
        "api_key_url": "https://console.anthropic.com/settings/keys",
        "docs_url": "https://docs.anthropic.com/en/api/models-list",
        "model_list_supported": True,
        "notes": ["Uses Anthropic Messages protocol; API keys are stored in OPENAI_API_KEY for the Agent compatibility bridge."],
    },
    {
        "id": "gemini-openai",
        "label": "Google Gemini (OpenAI 兼容)",
        "provider": "openai",
        "provider_type": "openai_compatible",
        "category": "international",
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
        "default_model": "gemini-2.5-flash",
        "api_key_url": "https://aistudio.google.com/apikey",
        "docs_url": "https://ai.google.dev/gemini-api/docs/openai",
        "model_list_supported": True,
        "notes": ["Use the Gemini OpenAI-compatibility endpoint; create the key in Google AI Studio."],
    },
    {
        "id": "openrouter",
        "label": "OpenRouter",
        "provider": "openai",
        "provider_type": "openai_compatible",
        "category": "international",
        "base_url": "https://openrouter.ai/api/v1",
        "default_model": "openai/gpt-4o-mini",
        "api_key_url": "https://openrouter.ai/keys",
        "docs_url": "https://openrouter.ai/docs/quickstart",
        "model_list_supported": True,
        "notes": ["Aggregates 300+ models; use vendor/model slugs such as anthropic/claude-3.7-sonnet."],
    },
    {
        "id": "groq",
        "label": "Groq",
        "provider": "openai",
        "provider_type": "openai_compatible",
        "category": "international",
        "base_url": "https://api.groq.com/openai/v1",
        "default_model": "llama-3.3-70b-versatile",
        "api_key_url": "https://console.groq.com/keys",
        "docs_url": "https://console.groq.com/docs/openai",
        "model_list_supported": True,
        "notes": ["Low-latency inference on custom hardware; OpenAI-compatible API."],
    },
    {
        "id": "together",
        "label": "Together AI",
        "provider": "openai",
        "provider_type": "openai_compatible",
        "category": "international",
        "base_url": "https://api.together.xyz/v1",
        "default_model": "meta-llama/Llama-3.3-70B-Instruct-Turbo",
        "api_key_url": "https://api.together.ai/settings/api-keys",
        "docs_url": "https://docs.together.ai/docs/openai-api-compatibility",
        "model_list_supported": True,
        "notes": ["Open-model hosting with OpenAI-compatible endpoints."],
    },
    {
        "id": "xai-grok",
        "label": "xAI Grok",
        "provider": "openai",
        "provider_type": "openai_compatible",
        "category": "international",
        "base_url": "https://api.x.ai/v1",
        "default_model": "grok-4",
        "api_key_url": "https://console.x.ai/",
        "docs_url": "https://docs.x.ai/docs/api-reference",
        "model_list_supported": True,
        "notes": ["OpenAI-compatible Grok API from xAI."],
    },
    {
        "id": "mistral",
        "label": "Mistral AI",
        "provider": "openai",
        "provider_type": "openai_compatible",
        "category": "international",
        "base_url": "https://api.mistral.ai/v1",
        "default_model": "mistral-large-latest",
        "api_key_url": "https://console.mistral.ai/api-keys/",
        "docs_url": "https://docs.mistral.ai/api/",
        "model_list_supported": True,
        "notes": ["OpenAI-compatible chat completions from Mistral."],
    },
    # ---- 国产厂商 (domestic) ----
    {
        "id": "deepseek",
        "label": "DeepSeek 深度求索",
        "provider": "openai",
        "provider_type": "openai_compatible",
        "category": "domestic",
        "base_url": "https://api.deepseek.com",
        "default_model": "deepseek-chat",
        "api_key_url": "https://platform.deepseek.com/api_keys",
        "docs_url": "https://api-docs.deepseek.com/",
        "model_list_supported": True,
        "notes": ["OpenAI-compatible API; the Agent normalizes bare hosts to /v1."],
    },
    {
        "id": "dashscope-qwen-cn",
        "label": "通义千问 / DashScope 北京",
        "provider": "openai",
        "provider_type": "openai_compatible",
        "category": "domestic",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "default_model": "qwen-plus",
        "api_key_url": "https://bailian.console.aliyun.com/",
        "docs_url": "https://help.aliyun.com/zh/model-studio/compatibility-of-openai-with-dashscope",
        "model_list_supported": True,
        "notes": ["Use the DashScope OpenAI-compatible endpoint for China region workloads."],
    },
    {
        "id": "dashscope-qwen-intl",
        "label": "Qwen / DashScope 美国弗吉尼亚",
        "provider": "openai",
        "provider_type": "openai_compatible",
        "category": "domestic",
        "base_url": "https://dashscope-us.aliyuncs.com/compatible-mode/v1",
        "default_model": "qwen-plus",
        "api_key_url": "https://modelstudio.console.alibabacloud.com/",
        "docs_url": "https://help.aliyun.com/zh/model-studio/compatibility-of-openai-with-dashscope",
        "model_list_supported": True,
        "notes": ["Use the Virginia endpoint for international DashScope accounts; Singapore workspaces use a workspace-specific base URL."],
    },
    {
        "id": "moonshot-kimi",
        "label": "月之暗面 Kimi (Moonshot)",
        "provider": "openai",
        "provider_type": "openai_compatible",
        "category": "domestic",
        "base_url": "https://api.moonshot.cn/v1",
        "default_model": "kimi-k2-0905-preview",
        "api_key_url": "https://platform.moonshot.cn/console/api-keys",
        "docs_url": "https://platform.moonshot.cn/docs/api/chat",
        "model_list_supported": True,
        "notes": ["OpenAI-compatible Kimi API; use api.moonshot.ai/v1 for the international endpoint."],
    },
    {
        "id": "zhipu-glm",
        "label": "智谱 GLM (Zhipu)",
        "provider": "openai",
        "provider_type": "openai_compatible",
        "category": "domestic",
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "default_model": "glm-4.6",
        "api_key_url": "https://open.bigmodel.cn/usercenter/apikeys",
        "docs_url": "https://docs.bigmodel.cn/cn/guide/develop/openai/introduction",
        "model_list_supported": True,
        "notes": ["智谱 BigModel OpenAI-compatible endpoint (paas/v4)."],
    },
    {
        "id": "siliconflow",
        "label": "硅基流动 SiliconFlow",
        "provider": "openai",
        "provider_type": "openai_compatible",
        "category": "domestic",
        "base_url": "https://api.siliconflow.cn/v1",
        "default_model": "deepseek-ai/DeepSeek-V3",
        "api_key_url": "https://cloud.siliconflow.cn/account/ak",
        "docs_url": "https://docs.siliconflow.cn/cn/userguide/quickstart",
        "model_list_supported": True,
        "notes": ["Aggregates many open models behind one OpenAI-compatible key."],
    },
    {
        "id": "volcengine-ark",
        "label": "火山方舟 (豆包 Doubao)",
        "provider": "openai",
        "provider_type": "openai_compatible",
        "category": "domestic",
        "base_url": "https://ark.cn-beijing.volces.com/api/v3",
        "default_model": "doubao-seed-1-6-250615",
        "api_key_url": "https://console.volcengine.com/ark",
        "docs_url": "https://www.volcengine.com/docs/82379/1330626",
        "model_list_supported": True,
        "notes": ["Use the Ark inference endpoint ID as the model when an endpoint is provisioned."],
    },
    {
        "id": "qianfan",
        "label": "百度千帆 (文心 ERNIE)",
        "provider": "openai",
        "provider_type": "openai_compatible",
        "category": "domestic",
        "base_url": "https://qianfan.baidubce.com/v2",
        "default_model": "ernie-4.5-turbo-128k",
        "api_key_url": "https://console.bce.baidu.com/iam/#/iam/apikey/list",
        "docs_url": "https://cloud.baidu.com/doc/qianfan-api/index.html",
        "model_list_supported": True,
        "notes": ["Qianfan v2 OpenAI-compatible endpoint; create a bearer API key in the IAM console."],
    },
    {
        "id": "minimax",
        "label": "MiniMax 海螺",
        "provider": "openai",
        "provider_type": "openai_compatible",
        "category": "domestic",
        "base_url": "https://api.minimaxi.com/v1",
        "default_model": "MiniMax-Text-01",
        "api_key_url": "https://platform.minimaxi.com/user-center/basic-information/interface-key",
        "docs_url": "https://platform.minimaxi.com/document/guides",
        "model_list_supported": True,
        "notes": ["Use api.minimax.io/v1 for the international endpoint."],
    },
    {
        "id": "stepfun",
        "label": "阶跃星辰 StepFun",
        "provider": "openai",
        "provider_type": "openai_compatible",
        "category": "domestic",
        "base_url": "https://api.stepfun.com/v1",
        "default_model": "step-2-16k",
        "api_key_url": "https://platform.stepfun.com/interface-key",
        "docs_url": "https://platform.stepfun.com/docs/overview/concept",
        "model_list_supported": True,
        "notes": ["OpenAI-compatible StepFun API."],
    },
    # ---- 本地部署 (local) ----
    {
        "id": "ollama",
        "label": "Ollama 本地",
        "provider": "openai",
        "provider_type": "openai_compatible",
        "category": "local",
        "base_url": "http://localhost:11434/v1",
        "default_model": "llama3.1",
        "api_key_url": "https://ollama.com/download",
        "docs_url": "https://github.com/ollama/ollama/blob/main/docs/openai.md",
        "model_list_supported": True,
        "api_key_optional": True,
        "notes": ["Local Ollama exposes an OpenAI-compatible API; the key is ignored — enter any placeholder such as 'ollama'."],
    },
    {
        "id": "lmstudio",
        "label": "LM Studio 本地",
        "provider": "openai",
        "provider_type": "openai_compatible",
        "category": "local",
        "base_url": "http://localhost:1234/v1",
        "default_model": "local-model",
        "api_key_url": "https://lmstudio.ai/",
        "docs_url": "https://lmstudio.ai/docs/app/api/endpoints/openai",
        "model_list_supported": True,
        "api_key_optional": True,
        "notes": ["Start the LM Studio local server; the key is ignored — enter any placeholder such as 'lm-studio'."],
    },
    {
        "id": "vllm",
        "label": "vLLM 本地",
        "provider": "openai",
        "provider_type": "openai_compatible",
        "category": "local",
        "base_url": "http://localhost:8000/v1",
        "default_model": "",
        "api_key_url": "https://docs.vllm.ai/en/latest/getting_started/quickstart.html",
        "docs_url": "https://docs.vllm.ai/en/latest/serving/openai_compatible_server.html",
        "model_list_supported": True,
        "api_key_optional": True,
        "notes": ["Point at your vLLM OpenAI server; set the model to the served model name."],
    },
    # ---- 自定义 / Mock ----
    {
        "id": "custom-openai-compatible",
        "label": "自定义 OpenAI 兼容",
        "provider": "openai",
        "provider_type": "openai_compatible",
        "category": "custom",
        "base_url": "",
        "default_model": "",
        "api_key_url": "",
        "docs_url": "",
        "model_list_supported": True,
        "notes": ["Fill in provider Base URL, API key, and model name from the vendor console."],
    },
    {
        "id": "mock",
        "label": "本地 Mock",
        "provider": "mock",
        "provider_type": "mock",
        "category": "mock",
        "base_url": "",
        "default_model": "mock-local",
        "api_key_url": "",
        "docs_url": "",
        "model_list_supported": False,
        "api_key_optional": True,
        "notes": ["Deterministic local fallback for development; no external key required."],
    },
]


def ai_config_payload_for_runtime(runtime: AgentRuntime) -> dict[str, Any]:
    load_project_env()
    env_path, env_source = writable_project_env_path()
    status = ai_status_payload_for_runtime(runtime)
    return {
        "object": "aiask.ai_config",
        "status": "ready",
        "current": {
            "provider": status["provider"],
            "model": status["model"],
            "base_url": status.get("base_url"),
            "api_key_configured": status["api_key_configured"],
            "base_url_configured": status["base_url_configured"],
            "mock": status["mock"],
            "configured": status["configured"],
            "prompt_cache": status.get("prompt_cache"),
            "secrets_redacted": True,
        },
        "editable": {
            "provider_env": "AIASK_AGENT_MODEL_PROVIDER",
            "model_env": "AIASK_AGENT_MODEL",
            "base_url_env": "OPENAI_BASE_URL",
            "api_key_env": "OPENAI_API_KEY",
            "prompt_cache_enabled_env": "AIASK_AGENT_PROMPT_CACHE_ENABLED",
            "prompt_cache_strategy_env": "AIASK_AGENT_PROMPT_CACHE_STRATEGY",
            "prompt_cache_recent_messages_env": "AIASK_AGENT_PROMPT_CACHE_RECENT_MESSAGES",
            "env_file": str(env_path),
            "env_source": env_source,
        },
        "presets": AI_MODEL_PROVIDER_PRESETS,
        "actions": {
            "save": {"method": "PATCH", "path": "/v1/ai/config", "requires_control_token": True},
            "models": {"method": "GET", "path": "/v1/ai/models"},
            "smoke": {"method": "POST", "path": "/v1/ai/smoke"},
        },
        "docs": {
            "openai_models": "https://platform.openai.com/docs/api-reference/models/list",
            "deepseek": "https://api-docs.deepseek.com/",
            "dashscope_openai_compatible": "https://help.aliyun.com/zh/model-studio/compatibility-of-openai-with-dashscope",
            "anthropic_models": "https://docs.anthropic.com/en/api/models-list",
        },
        "config_source": project_env_status(),
        "secrets_redacted": True,
    }


async def save_ai_config_for_runtime(runtime: AgentRuntime, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    body = dict(payload or {})
    preset_id = str(body.get("preset") or body.get("preset_id") or "").strip()
    preset = next((item for item in AI_MODEL_PROVIDER_PRESETS if item["id"] == preset_id), None) if preset_id else None
    provider = str(body.get("provider") or (preset or {}).get("provider") or "").strip().lower()
    model = str(body.get("model") or (preset or {}).get("default_model") or "").strip()
    base_url = str(body.get("base_url") if body.get("base_url") is not None else (preset or {}).get("base_url") or "").strip()
    api_key = str(body.get("api_key") or "").strip()
    update_api_key = bool(api_key or body.get("replace_api_key") is True)
    prompt_cache_enabled = body.get("prompt_cache_enabled")
    prompt_cache_recent_messages = body.get("prompt_cache_recent_messages")
    if provider not in {"mock", "openai", "anthropic", "anthropic_messages"}:
        raise ValueError(f"unsupported model provider: {provider}")
    if provider == "anthropic_messages":
        provider = "anthropic"
    if provider != "mock" and not model:
        raise ValueError("model is required for non-mock providers")
    existing_key = str(os.getenv("OPENAI_API_KEY") or "").strip()
    if provider != "mock" and not api_key and not existing_key and not bool(body.get("replace_api_key")):
        raise ValueError("api_key is required when no existing provider API key is configured")
    updates: dict[str, str | None] = {
        "AIASK_AGENT_MODEL_PROVIDER": provider,
        "AIASK_AGENT_MODEL": model or "mock-local",
        "OPENAI_BASE_URL": base_url,
    }
    if update_api_key:
        updates["OPENAI_API_KEY"] = api_key
    if prompt_cache_enabled is not None:
        updates["AIASK_AGENT_PROMPT_CACHE_ENABLED"] = "1" if bool(prompt_cache_enabled) else "0"
    if prompt_cache_recent_messages is not None:
        try:
            recent = max(0, min(int(prompt_cache_recent_messages), 20))
        except (TypeError, ValueError):
            recent = 3
        updates["AIASK_AGENT_PROMPT_CACHE_RECENT_MESSAGES"] = str(recent)
        updates["AIASK_AGENT_PROMPT_CACHE_STRATEGY"] = "system_and_recent"
    written = update_project_env_values(updates)
    await refresh_runtime_model_client(runtime)
    status = ai_status_payload_for_runtime(runtime)
    return {
        "object": "aiask.ai_config",
        "saved": True,
        "provider": status["provider"],
        "model": status["model"],
        "base_url_configured": status["base_url_configured"],
        "api_key_configured": status["api_key_configured"],
        "mock": status["mock"],
        "configured": status["configured"],
        "prompt_cache": status.get("prompt_cache"),
        "config_source": status["config_source"],
        "updated_keys": written["updated_keys"],
        "env_file": written["path"],
        "secrets_redacted": True,
    }


def ai_status_payload_for_runtime(runtime: AgentRuntime) -> dict[str, Any]:
    load_project_env()
    provider_registry = ModelProviderRegistry(usage_store=ProviderUsageStore(runtime.session_store.path))
    provider_payload = provider_registry.status()
    active_provider = str(provider_payload.get("active_provider") or "").strip().lower()
    providers = list(provider_registry.providers())
    active_spec = next((item for item in providers if item.name == active_provider), None)
    provider = str(os.getenv("AIASK_AGENT_MODEL_PROVIDER", "")).strip().lower()
    api_key_configured = bool(
        str(os.getenv("OPENAI_API_KEY", "")).strip()
        or str(os.getenv("OPENAI_API_KEYS", "")).strip()
        or (active_spec is not None and active_spec.provider_type != "mock" and active_spec.configured)
    )
    base_url = str(os.getenv("OPENAI_BASE_URL", "")).strip()
    model = str(provider_payload.get("default_model") or os.getenv("AIASK_AGENT_MODEL", runtime.model)).strip() or runtime.model
    runtime_client = runtime.model_client
    runtime_client_name = runtime_client.__class__.__name__
    effective_provider = provider or active_provider or ("openai" if api_key_configured else "mock")
    is_mock = effective_provider == "mock" or (not provider and isinstance(runtime_client, MockModelClient))
    configured = bool(active_spec.configured if active_spec is not None else is_mock or api_key_configured)
    return {
        "object": "aiask.ai_status",
        "provider": effective_provider,
        "model": model,
        "base_url_configured": bool(base_url),
        "base_url": base_url if base_url else None,
        "api_key_configured": api_key_configured,
        "mock": is_mock,
        "configured": configured,
        "runtime_client": runtime_client_name,
        "prompt_cache": prompt_cache_policy(),
        "config_source": project_env_status(),
        "secrets_redacted": True,
    }


def ai_error_payload(exc: BaseException, *, configured: bool = True) -> dict[str, Any]:
    name = exc.__class__.__name__
    message = str(exc)
    lowered = message.lower()
    if "401" in message or "unauthorized" in lowered or "api key" in lowered:
        code = "AUTH_FAILED"
    elif "timeout" in lowered:
        code = "TIMEOUT"
    elif "connection" in lowered or "connect" in lowered or "refused" in lowered:
        code = "NETWORK_ERROR"
    else:
        code = "AI_SMOKE_FAILED"
    return {
        "object": "aiask.ai_smoke",
        "configured": configured,
        "success": False,
        "error_code": code,
        "error": f"{name}: {message}",
        "secrets_redacted": True,
    }


async def refresh_runtime_model_client(runtime: AgentRuntime) -> None:
    old_client = runtime.model_client
    runtime.model_client = build_model_client_from_env()
    runtime.model = os.getenv("AIASK_AGENT_MODEL", runtime.model)
    context_manager = getattr(runtime, "context_manager", None)
    if context_manager is not None and hasattr(context_manager, "model_client"):
        context_manager.model_client = runtime.model_client
    if old_client is not runtime.model_client:
        closer = getattr(old_client, "aclose", None) or getattr(old_client, "close", None)
        if callable(closer):
            try:
                closed = closer()
                if hasattr(closed, "__await__"):
                    await closed
            except Exception:
                pass


async def ai_smoke_payload_for_runtime(runtime: AgentRuntime, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    status = ai_status_payload_for_runtime(runtime)
    if not status["configured"]:
        return {
            "object": "aiask.ai_smoke",
            "configured": False,
            "success": False,
            "model": status["model"],
            "provider": status["provider"],
            "error_code": "AI_MODEL_UNCONFIGURED",
            "error": "No mock provider or OpenAI-compatible API key is configured.",
            "secrets_redacted": True,
        }
    request_payload = dict(payload or {})
    prompt = str(request_payload.get("prompt") or "Reply with AIASK model smoke ok.").strip()
    model = str(request_payload.get("model") or status["model"] or runtime.model).strip()
    started = time.perf_counter()
    try:
        response = await runtime.model_client.complete(
            messages=[{"role": "user", "content": prompt}],
            tools=[],
            model=model,
        )
    except Exception as exc:
        result = ai_error_payload(exc, configured=bool(status["configured"]))
        result.update({"model": model, "provider": status["provider"], "latency_ms": int((time.perf_counter() - started) * 1000)})
        return result
    return {
        "object": "aiask.ai_smoke",
        "configured": True,
        "success": True,
        "provider": status["provider"],
        "mock": status["mock"],
        "model": model,
        "latency_ms": int((time.perf_counter() - started) * 1000),
        "response_preview": str(response.content or "")[:300],
        "usage": response.usage,
        "tool_call_count": len(response.tool_calls),
        "secrets_redacted": True,
    }


async def ai_models_payload_for_runtime(runtime: AgentRuntime) -> dict[str, Any]:
    load_project_env()
    status = ai_status_payload_for_runtime(runtime)

    def public_model_item(item: Any) -> dict[str, Any]:
        if hasattr(item, "model_dump"):
            raw = item.model_dump()
        elif isinstance(item, dict):
            raw = dict(item)
        else:
            raw = {"id": str(getattr(item, "id", item))}
        return _redact_secrets(raw)

    def fallback_model_list(error_payload: dict[str, Any]) -> dict[str, Any]:
        return {
            "object": "list",
            "configured": True,
            "provider": status["provider"],
            "unsupported": True,
            "data": [
                {
                    "id": status["model"],
                    "owned_by": status["provider"],
                    "fallback": True,
                }
            ],
            "warning_code": error_payload.get("error_code"),
            "warning": error_payload.get("error"),
            "message": "Provider model listing is not standard; showing the configured runtime model.",
            "secrets_redacted": True,
        }

    if status["mock"]:
        return {
            "object": "list",
            "configured": True,
            "provider": "mock",
            "unsupported": True,
            "data": [{"id": status["model"], "owned_by": "aiask_mock"}],
            "message": "Mock provider exposes the configured runtime model only.",
            "secrets_redacted": True,
        }
    if not status["api_key_configured"]:
        return {
            "object": "list",
            "configured": False,
            "provider": status["provider"],
            "unsupported": False,
            "data": [],
            "error_code": "AI_MODEL_UNCONFIGURED",
            "error": "OPENAI_API_KEY is not configured.",
            "secrets_redacted": True,
        }
    if str(status.get("provider") or "").strip().lower() in {"anthropic", "anthropic_messages"}:
        try:
            import httpx

            base_url = str(os.getenv("OPENAI_BASE_URL", "")).strip().rstrip("/")
            if not base_url:
                base_url = "https://api.anthropic.com/v1"
            elif base_url.lower().endswith("/messages"):
                base_url = base_url.rsplit("/", 1)[0]
            elif not base_url.lower().endswith("/v1") and not base_url.lower().endswith("/models"):
                base_url = f"{base_url}/v1"
            url = base_url if base_url.lower().endswith("/models") else f"{base_url}/models"
            async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
                response = await client.get(
                    url,
                    headers={
                        "x-api-key": str(os.getenv("OPENAI_API_KEY", "")).strip(),
                        "anthropic-version": "2023-06-01",
                        "Accept": "application/json",
                    },
                )
                response.raise_for_status()
                body = response.json()
            data = [public_model_item(item) for item in list((body or {}).get("data") or [])] if isinstance(body, dict) else []
            return {
                "object": "list",
                "configured": True,
                "provider": status["provider"],
                "unsupported": False,
                "data": data,
                "secrets_redacted": True,
            }
        except Exception as exc:
            result = ai_error_payload(exc, configured=True)
            return fallback_model_list(result)
    client = None
    try:
        from openai import AsyncOpenAI

        client = AsyncOpenAI(
            api_key=str(os.getenv("OPENAI_API_KEY", "")).strip(),
            base_url=_openai_compatible_api_base(str(os.getenv("OPENAI_BASE_URL", "")).strip() or None),
        )
        response = await client.models.list()
        data = [public_model_item(item) for item in list(getattr(response, "data", []) or [])]
        return {
            "object": "list",
            "configured": True,
            "provider": status["provider"],
            "unsupported": False,
            "data": data,
            "secrets_redacted": True,
        }
    except Exception as exc:
        result = ai_error_payload(exc, configured=True)
        return fallback_model_list(result)
    finally:
        if client is not None:
            closer = getattr(client, "close", None)
            if callable(closer):
                closed = closer()
                if hasattr(closed, "__await__"):
                    await closed


def _redact_secrets(value: Any) -> Any:
    if isinstance(value, dict):
        redacted: dict[str, Any] = {}
        for key, item in value.items():
            lowered = str(key).lower()
            if any(token in lowered for token in ("secret", "token", "api_key", "apikey", "password", "credential")):
                redacted[str(key)] = "[REDACTED]"
            else:
                redacted[str(key)] = _redact_secrets(item)
        return redacted
    if isinstance(value, list):
        return [_redact_secrets(item) for item in value]
    return value
