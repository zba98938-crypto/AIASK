from __future__ import annotations

import json
import os
import re
import inspect
from dataclasses import dataclass, field
from typing import Any, Protocol
from uuid import uuid4

from .env_config import load_project_env
from .model_providers import ModelProviderRegistry, ProviderSpec, ProviderUsageStore, prompt_cache_policy


@dataclass
class ModelResponse:
    content: str = ""
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    usage: dict[str, Any] = field(default_factory=dict)
    raw: Any = None


class ModelClient(Protocol):
    async def complete(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        model: str,
    ) -> ModelResponse:
        ...


class OpenAIChatClient:
    def __init__(self, *, api_key: str | None = None, base_url: str | None = None) -> None:
        from openai import AsyncOpenAI

        self.client = AsyncOpenAI(api_key=api_key, base_url=_openai_compatible_api_base(base_url))

    async def aclose(self) -> None:
        closer = getattr(self.client, "close", None)
        if callable(closer):
            result = closer()
            if inspect.isawaitable(result):
                await result

    async def complete(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        model: str,
    ) -> ModelResponse:
        response = await self.client.chat.completions.create(
            model=model,
            messages=messages,
            tools=tools or None,
            tool_choice="auto" if tools else None,
        )
        if isinstance(response, str):
            if _looks_like_html(response):
                raise RuntimeError("model provider returned HTML instead of a chat completion response")
            return ModelResponse(content=response, raw=response)
        if isinstance(response, dict):
            return self._model_response_from_mapping(response)
        choices = list(getattr(response, "choices", []) or [])
        if not choices:
            return ModelResponse(content=str(response or ""), raw=response)
        choice = choices[0]
        message = getattr(choice, "message", None) or {}
        tool_calls: list[dict[str, Any]] = []
        for call in list(getattr(message, "tool_calls", None) or []):
            tool_calls.append(
                {
                    "id": getattr(call, "id", ""),
                    "type": getattr(call, "type", "function"),
                    "function": {
                        "name": getattr(getattr(call, "function", None), "name", ""),
                        "arguments": getattr(getattr(call, "function", None), "arguments", "") or "{}",
                    },
                }
            )
        usage_obj = getattr(response, "usage", None)
        if hasattr(usage_obj, "model_dump"):
            usage = usage_obj.model_dump()
        elif isinstance(usage_obj, dict):
            usage = dict(usage_obj)
        else:
            usage = {}
        return ModelResponse(
            content=getattr(message, "content", "") or "",
            tool_calls=tool_calls,
            usage=usage,
            raw=response,
        )

    @staticmethod
    def _model_response_from_mapping(response: dict[str, Any]) -> ModelResponse:
        choices = list(response.get("choices") or [])
        message: dict[str, Any] = {}
        if choices and isinstance(choices[0], dict):
            message = dict(choices[0].get("message") or {})
        content = message.get("content")
        if isinstance(content, list):
            parts = []
            for item in content:
                if isinstance(item, dict):
                    text = item.get("text") or item.get("content")
                    if text not in (None, ""):
                        parts.append(str(text))
                elif item not in (None, ""):
                    parts.append(str(item))
            content_text = "\n".join(parts)
        else:
            content_text = str(content or response.get("content") or "")
        tool_calls: list[dict[str, Any]] = []
        for call in list(message.get("tool_calls") or []):
            if not isinstance(call, dict):
                continue
            function = dict(call.get("function") or {})
            tool_calls.append(
                {
                    "id": str(call.get("id") or ""),
                    "type": str(call.get("type") or "function"),
                    "function": {
                        "name": str(function.get("name") or call.get("name") or ""),
                        "arguments": str(function.get("arguments") or call.get("arguments") or "{}"),
                    },
                }
            )
        usage = dict(response.get("usage") or {}) if isinstance(response.get("usage"), dict) else {}
        return ModelResponse(content=content_text, tool_calls=tool_calls, usage=usage, raw=response)


def _openai_compatible_api_base(base_url: str | None) -> str | None:
    base = str(base_url or "").strip().rstrip("/")
    if not base:
        return None
    lowered = base.lower()
    if lowered.endswith("/chat/completions"):
        base = base.rsplit("/", 2)[0]
        lowered = base.lower()
    if lowered.endswith("/models"):
        base = base.rsplit("/", 1)[0]
        lowered = base.lower()
    if lowered.endswith("/v1"):
        return base
    remainder = base.split("://", 1)[1] if "://" in base else base
    path = remainder.split("/", 1)[1] if "/" in remainder else ""
    if not path:
        return f"{base}/v1"
    return base


def _looks_like_html(value: str) -> bool:
    prefix = value.lstrip()[:80].lower()
    return prefix.startswith("<!doctype html") or prefix.startswith("<html")


def _anthropic_messages_api_base(base_url: str | None) -> str:
    base = str(base_url or "").rstrip("/")
    if not base:
        return "https://api.anthropic.com/v1"
    lowered = base.lower()
    if lowered.endswith("/messages"):
        return base.rsplit("/", 1)[0]
    remainder = base.split("://", 1)[1] if "://" in base else base
    path = remainder.split("/", 1)[1] if "/" in remainder else ""
    if not path:
        return f"{base}/v1"
    return base


class AnthropicMessagesClient:
    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        transport: Any | None = None,
    ) -> None:
        import httpx

        self.api_key = api_key or ""
        self.base_url = _anthropic_messages_api_base(base_url)
        self.client = httpx.AsyncClient(transport=transport, follow_redirects=True, http2=False)

    async def aclose(self) -> None:
        await self.client.aclose()

    @staticmethod
    def _content_text(value: Any) -> str:
        if value is None:
            return ""
        if isinstance(value, str):
            return value
        if isinstance(value, list):
            parts: list[str] = []
            for item in value:
                text = AnthropicMessagesClient._content_text(item)
                if text:
                    parts.append(text)
            return "\n".join(parts)
        if isinstance(value, dict):
            if isinstance(value.get("text"), str):
                return str(value.get("text") or "")
            if "content" in value:
                return AnthropicMessagesClient._content_text(value.get("content"))
        return str(value)

    @staticmethod
    def _adapt_messages(messages: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], str | None]:
        adapted: list[dict[str, Any]] = []
        system_parts: list[str] = []
        for message in messages or []:
            role = str(message.get("role") or "user").strip().lower()
            content = AnthropicMessagesClient._content_text(message.get("content"))
            if role == "system":
                if content:
                    system_parts.append(content)
                continue
            if role == "tool":
                name = message.get("name") or message.get("tool_call_id") or "tool"
                adapted.append({"role": "user", "content": f"Tool result ({name}):\n{content}"})
                continue
            if role not in {"user", "assistant"}:
                role = "user"
            if role == "assistant" and message.get("tool_calls"):
                tool_lines = []
                for call in list(message.get("tool_calls") or []):
                    function = dict(call.get("function") or {})
                    tool_lines.append(
                        f"Tool requested: {function.get('name') or call.get('name') or 'tool'} "
                        f"arguments={function.get('arguments') or call.get('arguments') or '{}'}"
                    )
                content = "\n".join([part for part in [content, *tool_lines] if part])
            adapted.append({"role": role, "content": content})
        if not adapted:
            adapted.append({"role": "user", "content": ""})
        system = "\n\n".join(system_parts) if system_parts else None
        return adapted, system

    @staticmethod
    def _adapt_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
        adapted: list[dict[str, Any]] = []
        for tool in tools or []:
            if not isinstance(tool, dict):
                continue
            function = dict(tool.get("function") or {})
            name = str(function.get("name") or "").strip()
            if not name:
                continue
            adapted.append(
                {
                    "name": name,
                    "description": str(function.get("description") or ""),
                    "input_schema": function.get("parameters") or {"type": "object", "properties": {}},
                }
            )
        return adapted

    @staticmethod
    def _extract_response(body: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
        text_parts: list[str] = []
        tool_calls: list[dict[str, Any]] = []
        for item in list(body.get("content") or []):
            if not isinstance(item, dict):
                if item not in (None, ""):
                    text_parts.append(str(item))
                continue
            item_type = str(item.get("type") or "").strip().lower()
            if item_type == "text" or item.get("text") is not None:
                text = item.get("text")
                if text not in (None, ""):
                    text_parts.append(str(text))
                continue
            if item_type == "tool_use":
                tool_calls.append(
                    {
                        "id": str(item.get("id") or f"call_{uuid4().hex[:12]}"),
                        "type": "function",
                        "function": {
                            "name": str(item.get("name") or ""),
                            "arguments": json.dumps(item.get("input") or {}, ensure_ascii=False),
                        },
                    }
                )
        return "\n".join(text_parts), tool_calls

    @staticmethod
    def _anthropic_cache_block(text: str) -> dict[str, Any]:
        return {"type": "text", "text": text, "cache_control": {"type": "ephemeral"}}

    @classmethod
    def _apply_prompt_cache_policy(
        cls,
        messages: list[dict[str, Any]],
        system: str | None,
    ) -> tuple[list[dict[str, Any]], str | list[dict[str, Any]] | None, dict[str, Any]]:
        policy_env = dict(os.environ)
        policy_env["AIASK_AGENT_MODEL_PROVIDER"] = "anthropic"
        policy = prompt_cache_policy(policy_env)
        if not policy.get("enabled"):
            return messages, system, {"policy": policy, "applied": False, "system": False, "message_count": 0}
        cacheable_recent = max(0, int(policy.get("recent_non_system_messages") or 0))
        next_messages = [dict(item) for item in messages]
        applied_count = 0
        if cacheable_recent:
            candidate_indexes = [
                index
                for index, item in enumerate(next_messages)
                if str(item.get("role") or "").strip().lower() in {"user", "assistant"}
                and isinstance(item.get("content"), str)
                and str(item.get("content") or "")
            ]
            for index in candidate_indexes[-cacheable_recent:]:
                content = str(next_messages[index].get("content") or "")
                next_messages[index]["content"] = [cls._anthropic_cache_block(content)]
                applied_count += 1
        next_system: str | list[dict[str, Any]] | None = system
        system_applied = False
        if system and policy.get("system_prompt"):
            next_system = [cls._anthropic_cache_block(system)]
            system_applied = True
        return next_messages, next_system, {
            "policy": policy,
            "applied": bool(system_applied or applied_count),
            "system": system_applied,
            "message_count": applied_count,
        }

    async def complete(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        model: str,
    ) -> ModelResponse:
        adapted_messages, system = self._adapt_messages(messages)
        adapted_messages, system, prompt_cache = self._apply_prompt_cache_policy(adapted_messages, system)
        payload: dict[str, Any] = {
            "model": model,
            "messages": adapted_messages,
            "max_tokens": max(1, int(os.getenv("AIASK_AGENT_MAX_TOKENS", "2048") or 2048)),
        }
        if system:
            payload["system"] = system
        adapted_tools = self._adapt_tools(tools)
        if adapted_tools:
            payload["tools"] = adapted_tools
        response = await self.client.post(
            f"{self.base_url}/messages",
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            json=payload,
            timeout=max(5.0, float(os.getenv("AIASK_AGENT_MODEL_TIMEOUT", "120") or 120)),
        )
        response.raise_for_status()
        body = response.json()
        content, tool_calls = self._extract_response(body if isinstance(body, dict) else {})
        usage = dict(body.get("usage") or {}) if isinstance(body, dict) else {}
        usage.setdefault("prompt_cache", {key: value for key, value in prompt_cache.items() if key != "policy"})
        return ModelResponse(content=content, tool_calls=tool_calls, usage=usage, raw=body)


class MockModelClient:
    """Deterministic local model used when no external provider is configured."""

    _TOOL_PATTERN = re.compile(r"tool:([A-Za-z0-9_]+)(?:\s+(\{.*\}))?", re.DOTALL)

    async def complete(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        model: str,
    ) -> ModelResponse:
        tool_messages = [msg for msg in messages if msg.get("role") == "tool"]
        if tool_messages:
            latest = tool_messages[-1]
            return ModelResponse(
                content=f"工具调用完成: {latest.get('name') or latest.get('tool_call_id')}",
                usage={"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
            )

        last_user = ""
        for msg in reversed(messages):
            if msg.get("role") == "user":
                last_user = str(msg.get("content") or "")
                break
        match = self._TOOL_PATTERN.search(last_user)
        allowed_names = {
            (tool.get("function") or {}).get("name")
            for tool in tools
            if isinstance(tool, dict)
        }
        if match and match.group(1) in allowed_names:
            name = match.group(1)
            raw_args = match.group(2) or "{}"
            try:
                parsed_args = json.loads(raw_args)
            except Exception:
                parsed_args = {}
            return ModelResponse(
                content="",
                tool_calls=[
                    {
                        "id": f"call_{uuid4().hex[:12]}",
                        "type": "function",
                        "function": {
                            "name": name,
                            "arguments": json.dumps(parsed_args, ensure_ascii=False),
                        },
                    }
                ],
                usage={"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
            )

        return ModelResponse(
            content=last_user or "AIASK Agent ready.",
            usage={"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        )


class ProviderPoolModelClient:
    """OpenAI-compatible provider fallback with credential attempt accounting."""

    def __init__(self, *, registry: ModelProviderRegistry | None = None) -> None:
        self.registry = registry or ModelProviderRegistry(usage_store=ProviderUsageStore())

    async def aclose(self) -> None:
        return None

    async def complete(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        model: str,
    ) -> ModelResponse:
        errors: list[str] = []
        providers = {item.name: item for item in self.registry.providers() if item.enabled}
        allow_mock_fallback = str(os.getenv("AIASK_AGENT_ALLOW_MOCK_FALLBACK", "")).strip().lower() in {"1", "true", "yes", "on"}
        for provider_name in self.registry.fallback_order():
            provider = providers.get(provider_name)
            if provider is None:
                continue
            if provider.provider_type == "mock":
                if self.registry.active_provider_name() != "mock" and not allow_mock_fallback:
                    continue
                return await MockModelClient().complete(messages=messages, tools=tools, model=model)
            for credential in self._credentials_for(provider):
                if not credential.secret_value:
                    continue
                if provider.provider_type in {"anthropic_messages", "anthropic"}:
                    client = AnthropicMessagesClient(api_key=credential.secret_value, base_url=provider.base_url)
                else:
                    client = OpenAIChatClient(api_key=credential.secret_value, base_url=provider.base_url)
                try:
                    response = await client.complete(messages=messages, tools=tools, model=provider.model or model)
                    self.registry.record_attempt(provider=provider.name, credential_id=credential.credential_id, success=True)
                    return response
                except Exception as exc:
                    error_class = self.registry.classify_error(exc)
                    self.registry.record_attempt(
                        provider=provider.name,
                        credential_id=credential.credential_id,
                        success=False,
                        error=str(exc),
                    )
                    errors.append(f"{provider.name}:{credential.credential_id}:{error_class}")
                    if error_class not in {"auth_failed", "rate_limited", "timeout", "network_error"}:
                        raise
                finally:
                    await client.aclose()
        raise RuntimeError("all configured model providers failed or are unavailable: " + "; ".join(errors))

    def _credentials_for(self, provider: ProviderSpec) -> list[Any]:
        configured = [item for item in provider.credentials if item.configured]
        usage = self.registry.usage_store.summary()
        return sorted(
            configured,
            key=lambda item: (
                int((usage.get(item.credential_id) or {}).get("failure_count") or 0),
                str((usage.get(item.credential_id) or {}).get("last_used_at") or ""),
                item.credential_id,
            ),
        )


def build_model_client_from_env() -> ModelClient:
    load_project_env()
    provider = str(os.getenv("AIASK_AGENT_MODEL_PROVIDER", "")).strip().lower()
    api_key = str(os.getenv("OPENAI_API_KEY", "")).strip()
    base_url = str(os.getenv("OPENAI_BASE_URL", "")).strip() or None
    has_provider_pool = bool(str(os.getenv("OPENAI_API_KEYS", "")).strip() or str(os.getenv("AIASK_AGENT_MODEL_PROVIDERS", "")).strip())
    if provider == "mock" or (not provider and not api_key and not has_provider_pool):
        return MockModelClient()
    if provider in {"anthropic_messages", "anthropic"}:
        return AnthropicMessagesClient(api_key=api_key or None, base_url=base_url)
    if has_provider_pool or provider not in {"", "openai"}:
        registry = ModelProviderRegistry(usage_store=ProviderUsageStore())
        if has_provider_pool and provider in {"", "openai"}:
            return ProviderPoolModelClient(registry=registry)
        if provider in {item.name for item in registry.providers()}:
            return ProviderPoolModelClient(registry=registry)
        if provider not in {"", "openai"}:
            raise ValueError(f"unsupported AIASK_AGENT_MODEL_PROVIDER: {provider}")
    if provider in {"", "openai"}:
        return OpenAIChatClient(api_key=api_key or None, base_url=base_url)
    raise ValueError(f"unsupported AIASK_AGENT_MODEL_PROVIDER: {provider}")
