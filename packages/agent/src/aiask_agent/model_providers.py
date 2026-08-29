from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import time
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .env_config import load_project_env, project_env_status
from .paths import default_state_db_path


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _hash_secret(value: str) -> str:
    return hashlib.sha256(str(value or "").encode("utf-8", errors="replace")).hexdigest()[:16]


def _split_env_list(value: str) -> list[str]:
    raw = str(value or "").strip()
    if not raw:
        return []
    items = re.split(r"[,;\n]", raw)
    return [item.strip() for item in items if item.strip()]


def _truthy(value: Any, *, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, str):
        text = value.strip().lower()
        if not text:
            return default
        return text in {"1", "true", "yes", "on", "enabled"}
    return bool(value)


def prompt_cache_policy(env: dict[str, str] | None = None) -> dict[str, Any]:
    source = dict(os.environ if env is None else env)
    raw_provider = str(source.get("AIASK_AGENT_MODEL_PROVIDER") or "").strip().lower()
    active_provider = raw_provider or ("openai" if str(source.get("OPENAI_API_KEY") or "").strip() else "mock")
    provider_type = "anthropic_messages" if active_provider in {"anthropic", "anthropic_messages"} else "openai_compatible" if active_provider not in {"mock", ""} else "mock"
    enabled = _truthy(source.get("AIASK_AGENT_PROMPT_CACHE_ENABLED"), default=provider_type == "anthropic_messages")
    recent_messages = 3
    try:
        recent_messages = max(0, min(int(source.get("AIASK_AGENT_PROMPT_CACHE_RECENT_MESSAGES") or 3), 20))
    except (TypeError, ValueError):
        recent_messages = 3
    strategy = str(source.get("AIASK_AGENT_PROMPT_CACHE_STRATEGY") or "system_and_recent").strip().lower() or "system_and_recent"
    supported = provider_type == "anthropic_messages"
    return {
        "object": "aiask.prompt_cache_policy",
        "enabled": bool(enabled and supported),
        "requested_enabled": bool(enabled),
        "supported": supported,
        "provider": active_provider,
        "provider_type": provider_type,
        "strategy": strategy,
        "system_prompt": bool(enabled and supported and strategy in {"system", "system_and_recent"}),
        "recent_non_system_messages": recent_messages if enabled and supported and strategy in {"recent", "system_and_recent"} else 0,
        "cache_control": {"type": "ephemeral"} if enabled and supported else None,
        "env": {
            "enabled": "AIASK_AGENT_PROMPT_CACHE_ENABLED",
            "strategy": "AIASK_AGENT_PROMPT_CACHE_STRATEGY",
            "recent_messages": "AIASK_AGENT_PROMPT_CACHE_RECENT_MESSAGES",
        },
        "secrets_redacted": True,
        "notes": [
            "AIASK applies Anthropic Messages API cache_control markers to the system prompt and the most recent non-system messages when supported.",
            "OpenAI-compatible providers are reported as unsupported unless they expose compatible prompt-cache semantics.",
        ],
    }


def _classify_error(error: BaseException | str | None) -> str:
    text = str(error or "").lower()
    if not text:
        return "unknown"
    if "401" in text or "unauthorized" in text or "api key" in text or "authentication" in text:
        return "auth_failed"
    if "429" in text or "rate limit" in text or "quota" in text:
        return "rate_limited"
    if "timeout" in text:
        return "timeout"
    if "connection" in text or "connect" in text or "refused" in text or "network" in text:
        return "network_error"
    return "provider_error"


@dataclass(frozen=True)
class ProviderCredential:
    provider: str
    credential_id: str
    source: str
    configured: bool
    secret_value: str | None = field(default=None, repr=False)

    def public(self, usage: dict[str, Any] | None = None) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "credential_id": self.credential_id,
            "source": self.source,
            "configured": self.configured,
            "secret_redacted": bool(self.secret_value),
            "usage": dict(usage or {}),
        }


@dataclass
class ProviderSpec:
    name: str
    provider_type: str
    model: str | None = None
    base_url: str | None = None
    enabled: bool = True
    credentials: list[ProviderCredential] = field(default_factory=list)
    live_env: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def configured(self) -> bool:
        if self.provider_type == "mock":
            return True
        return any(item.configured for item in self.credentials)

    @property
    def status(self) -> str:
        if not self.enabled:
            return "blocked"
        if self.configured:
            return "implemented"
        if self.live_env:
            return "skipped_missing_credentials"
        return "partial"

    def public(self, usage_by_credential: dict[str, dict[str, Any]] | None = None) -> dict[str, Any]:
        usage_by_credential = usage_by_credential or {}
        return {
            "name": self.name,
            "type": self.provider_type,
            "model": self.model,
            "base_url_configured": bool(self.base_url),
            "enabled": self.enabled,
            "configured": self.configured,
            "status": self.status,
            "required_env": list(self.live_env),
            "credentials": [
                credential.public(usage_by_credential.get(credential.credential_id))
                for credential in self.credentials
            ],
            "notes": list(self.notes),
            "secrets_redacted": True,
        }


class ProviderUsageStore:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or default_state_db_path()

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.path), timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS model_provider_usage (
                provider TEXT NOT NULL,
                credential_id TEXT NOT NULL,
                success_count INTEGER NOT NULL DEFAULT 0,
                failure_count INTEGER NOT NULL DEFAULT 0,
                last_status TEXT,
                last_error_class TEXT,
                last_used_at TEXT,
                PRIMARY KEY (provider, credential_id)
            )
            """
        )
        conn.commit()
        return conn

    def summary(self) -> dict[str, dict[str, Any]]:
        with closing(self._connect()) as conn:
            rows = conn.execute(
                """
                SELECT provider, credential_id, success_count, failure_count, last_status,
                       last_error_class, last_used_at
                FROM model_provider_usage
                """
            ).fetchall()
        return {
            str(row["credential_id"]): {
                "provider": row["provider"],
                "success_count": int(row["success_count"] or 0),
                "failure_count": int(row["failure_count"] or 0),
                "last_status": row["last_status"],
                "last_error_class": row["last_error_class"],
                "last_used_at": row["last_used_at"],
            }
            for row in rows
        }

    def record(self, *, provider: str, credential_id: str, success: bool, error: str | None = None) -> dict[str, Any]:
        error_class = None if success else _classify_error(error)
        status = "success" if success else "failure"
        with closing(self._connect()) as conn:
            conn.execute(
                """
                INSERT INTO model_provider_usage
                    (provider, credential_id, success_count, failure_count, last_status, last_error_class, last_used_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(provider, credential_id) DO UPDATE SET
                    success_count = success_count + excluded.success_count,
                    failure_count = failure_count + excluded.failure_count,
                    last_status = excluded.last_status,
                    last_error_class = excluded.last_error_class,
                    last_used_at = excluded.last_used_at
                """,
                (
                    provider,
                    credential_id,
                    1 if success else 0,
                    0 if success else 1,
                    status,
                    error_class,
                    _now(),
                ),
            )
            conn.commit()
        return self.summary().get(credential_id, {})


class ModelProviderRegistry:
    def __init__(self, *, env: dict[str, str] | None = None, usage_store: ProviderUsageStore | None = None) -> None:
        if env is None:
            load_project_env()
        self.env = dict(os.environ if env is None else env)
        self.usage_store = usage_store or ProviderUsageStore()
        self.config_issues: list[str] = []

    def providers(self) -> list[ProviderSpec]:
        specs: list[ProviderSpec] = [self._mock_provider()]
        openai = self._openai_provider()
        if openai is not None:
            specs.append(openai)
        specs.extend(self._configured_provider_specs())
        seen: set[str] = set()
        unique: list[ProviderSpec] = []
        for spec in specs:
            key = spec.name
            if key in seen:
                continue
            seen.add(key)
            unique.append(spec)
        return unique

    def status(self) -> dict[str, Any]:
        providers = self.providers()
        usage = self.usage_store.summary()
        active = self.active_provider_name()
        fallback_order = self.fallback_order()
        configured_count = sum(1 for item in providers if item.configured and item.enabled)
        live_unverified = [
            item.name
            for item in providers
            if item.enabled and item.provider_type != "mock" and item.configured
        ]
        return {
            "object": "aiask.model_provider_status",
            "active_provider": active,
            "default_model": str(self.env.get("AIASK_AGENT_MODEL") or "gpt-4.1-mini"),
            "config_source": project_env_status(),
            "providers": [item.public(usage) for item in providers],
            "configured_count": configured_count,
            "fallback_order": fallback_order,
            "credential_pool": self.credential_pool_status(),
            "rotation_strategy": "least_recent_failure_then_first_configured",
            "fallback_restore": {
                "implemented": True,
                "auth_and_rate_limit_errors_trigger_fallback": True,
                "restore_policy": "next run reconsiders all configured providers",
            },
            "prompt_cache": prompt_cache_policy(self.env),
            "status": "implemented" if configured_count else "blocked",
            "live_status": "live_unverified" if live_unverified else "not_required",
            "config_issues": list(self.config_issues),
            "secrets_redacted": True,
        }

    def credential_pool_status(self, provider_name: str | None = None) -> dict[str, Any]:
        usage = self.usage_store.summary()
        providers = [item for item in self.providers() if provider_name in {None, item.name}]
        pools: list[dict[str, Any]] = []
        for provider in providers:
            credentials = [
                credential.public(usage.get(credential.credential_id))
                for credential in provider.credentials
            ]
            next_credential = self.select_credential(provider.name)
            pools.append(
                {
                    "provider": provider.name,
                    "configured": provider.configured,
                    "credential_count": len(credentials),
                    "next_credential_id": next_credential.credential_id if next_credential else None,
                    "credentials": credentials,
                }
            )
        return {"pools": pools, "secrets_redacted": True}

    def select_credential(self, provider_name: str) -> ProviderCredential | None:
        provider = next((item for item in self.providers() if item.name == provider_name), None)
        if provider is None:
            return None
        configured = [item for item in provider.credentials if item.configured]
        if not configured:
            return None
        usage = self.usage_store.summary()

        def sort_key(item: ProviderCredential) -> tuple[int, str, str]:
            row = usage.get(item.credential_id) or {}
            return (
                int(row.get("failure_count") or 0),
                str(row.get("last_used_at") or ""),
                item.credential_id,
            )

        return sorted(configured, key=sort_key)[0]

    def record_attempt(self, *, provider: str, credential_id: str, success: bool, error: str | None = None) -> dict[str, Any]:
        return {
            "provider": provider,
            "credential_id": credential_id,
            "usage": self.usage_store.record(provider=provider, credential_id=credential_id, success=success, error=error),
        }

    def fallback_order(self) -> list[str]:
        active = self.active_provider_name()
        ordered: list[str] = []
        for name in [active, *[item.name for item in self.providers()]]:
            if name and name not in ordered:
                ordered.append(name)
        return ordered

    def active_provider_name(self) -> str:
        provider = str(self.env.get("AIASK_AGENT_MODEL_PROVIDER") or "").strip().lower()
        if provider:
            return provider
        return "openai" if str(self.env.get("OPENAI_API_KEY") or "").strip() else "mock"

    @staticmethod
    def classify_error(error: BaseException | str | None) -> str:
        return _classify_error(error)

    def _mock_provider(self) -> ProviderSpec:
        return ProviderSpec(
            name="mock",
            provider_type="mock",
            model="mock-local",
            enabled=True,
            credentials=[ProviderCredential("mock", "mock-local", "built_in", True, secret_value=None)],
            notes=["Deterministic local provider for development and tests."],
        )

    def _openai_provider(self) -> ProviderSpec | None:
        provider_env = str(self.env.get("AIASK_AGENT_MODEL_PROVIDER") or "").strip().lower()
        api_key = str(self.env.get("OPENAI_API_KEY") or "").strip()
        api_keys = _split_env_list(str(self.env.get("OPENAI_API_KEYS") or ""))
        if api_key and api_key not in api_keys:
            api_keys.insert(0, api_key)
        direct_providers = {"", "openai", "anthropic", "anthropic_messages"}
        if provider_env not in direct_providers and not api_keys:
            return None
        provider_name = provider_env if provider_env in {"anthropic", "anthropic_messages"} else "openai"
        credentials = [
            ProviderCredential(provider_name, f"{provider_name}:{_hash_secret(value)}", "OPENAI_API_KEYS" if index else "OPENAI_API_KEY", True, value)
            for index, value in enumerate(api_keys)
        ]
        if not credentials:
            credentials = [ProviderCredential(provider_name, f"{provider_name}:missing", "OPENAI_API_KEY", False, None)]
        base_url = str(self.env.get("OPENAI_BASE_URL") or "").strip() or None
        provider_type = "anthropic_messages" if provider_env in {"anthropic", "anthropic_messages"} else ("openai_compatible" if base_url else "openai")
        return ProviderSpec(
            name=provider_name,
            provider_type=provider_type,
            model=str(self.env.get("AIASK_AGENT_MODEL") or "gpt-4.1-mini"),
            base_url=base_url,
            enabled=True,
            credentials=credentials,
            live_env=["OPENAI_API_KEY"],
        )

    def _configured_provider_specs(self) -> list[ProviderSpec]:
        raw = str(self.env.get("AIASK_AGENT_MODEL_PROVIDERS") or "").strip()
        if not raw:
            return []
        try:
            parsed = json.loads(raw)
        except Exception as exc:
            self.config_issues.append(f"AIASK_AGENT_MODEL_PROVIDERS is not valid JSON: {exc}")
            return []
        items = parsed if isinstance(parsed, list) else parsed.get("providers") if isinstance(parsed, dict) else []
        specs: list[ProviderSpec] = []
        for item in list(items or []):
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or item.get("provider") or "").strip().lower()
            if not name or name in {"mock", "openai"}:
                continue
            provider_type = str(item.get("type") or "openai_compatible").strip().lower()
            env_name = str(item.get("api_key_env") or "").strip()
            secret = str(self.env.get(env_name) or "").strip() if env_name else ""
            credentials = [
                ProviderCredential(name, f"{name}:{_hash_secret(secret)}", env_name or "inline_ref", bool(secret), secret or None)
            ]
            specs.append(
                ProviderSpec(
                    name=name,
                    provider_type=provider_type,
                    model=str(item.get("model") or self.env.get("AIASK_AGENT_MODEL") or "gpt-4.1-mini"),
                    base_url=str(item.get("base_url") or "").strip() or None,
                    enabled=bool(item.get("enabled", True)),
                    credentials=credentials,
                    live_env=[env_name] if env_name else [],
                    notes=["Configured through AIASK_AGENT_MODEL_PROVIDERS."],
                )
            )
        return specs
