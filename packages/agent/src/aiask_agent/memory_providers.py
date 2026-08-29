from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from .memory import FinancialMemoryStore
from .paths import default_state_db_path


HERMES_EXTERNAL_MEMORY_PROVIDERS: tuple[dict[str, Any], ...] = (
    {
        "name": "honcho",
        "type": "external_memory",
        "required_env": ["HONCHO_API_KEY"],
        "capabilities": ["catalog", "readiness", "reserved_provider_slot"],
    },
    {
        "name": "openviking",
        "type": "external_memory",
        "required_env": ["OPENVIKING_API_KEY"],
        "capabilities": ["catalog", "readiness", "reserved_provider_slot"],
    },
    {
        "name": "mem0",
        "type": "external_memory",
        "required_env": ["MEM0_API_KEY"],
        "capabilities": ["catalog", "readiness", "reserved_provider_slot"],
    },
    {
        "name": "hindsight",
        "type": "external_memory",
        "required_env": ["HINDSIGHT_API_KEY"],
        "capabilities": ["catalog", "readiness", "reserved_provider_slot"],
    },
    {
        "name": "holographic",
        "type": "external_memory",
        "required_env": ["HOLOGRAPHIC_API_KEY"],
        "capabilities": ["catalog", "readiness", "reserved_provider_slot"],
    },
    {
        "name": "retaindb",
        "type": "external_memory",
        "required_env": ["RETAINDB_API_KEY"],
        "capabilities": ["catalog", "readiness", "reserved_provider_slot"],
    },
    {
        "name": "byterover",
        "type": "external_memory",
        "required_env": ["BYTEROVER_API_KEY"],
        "capabilities": ["catalog", "readiness", "reserved_provider_slot"],
    },
    {
        "name": "supermemory",
        "type": "external_memory",
        "required_env": ["SUPERMEMORY_API_KEY"],
        "capabilities": ["catalog", "readiness", "reserved_provider_slot"],
    },
)


class MemoryProviderManager:
    """AIASK-native memory provider facade with SQLite as the default durable backend."""

    def __init__(self, *, path: Path | None = None, env: dict[str, str] | None = None) -> None:
        self.path = path or default_state_db_path()
        self.env = dict(os.environ if env is None else env)
        self.sqlite = FinancialMemoryStore(self.path)

    def status(self) -> dict[str, Any]:
        provider = str(self.env.get("AIASK_MEMORY_PROVIDER") or "sqlite").strip().lower() or "sqlite"
        providers: list[dict[str, Any]] = [
            {
                "name": "sqlite",
                "type": "sqlite",
                "default": True,
                "configured": True,
                "status": "implemented",
                "path": str(self.path),
                "capabilities": ["save", "search", "status"],
            },
            {
                "name": "vector",
                "type": "semantic_memory",
                "default": False,
                "configured": bool(str(self.env.get("AIASK_VECTOR_MEMORY_URL") or "").strip()),
                "status": "live_unverified" if str(self.env.get("AIASK_VECTOR_MEMORY_URL") or "").strip() else "skipped_missing_credentials",
                "required_env": ["AIASK_VECTOR_MEMORY_URL"],
                "capabilities": ["reserved_provider_slot"],
            },
            {
                "name": "custom",
                "type": "custom",
                "default": False,
                "configured": bool(str(self.env.get("AIASK_CUSTOM_MEMORY_PROVIDER") or "").strip()),
                "status": "live_unverified" if str(self.env.get("AIASK_CUSTOM_MEMORY_PROVIDER") or "").strip() else "skipped_missing_credentials",
                "required_env": ["AIASK_CUSTOM_MEMORY_PROVIDER"],
                "capabilities": ["reserved_provider_slot"],
            },
        ]
        providers.extend(self._external_provider_status(item) for item in HERMES_EXTERNAL_MEMORY_PROVIDERS)
        return {
            "object": "aiask.memory_provider_status",
            "active_provider": provider,
            "providers": providers,
            "default_provider": "sqlite",
            "catalog": {
                "hermes_external_provider_count": len(HERMES_EXTERNAL_MEMORY_PROVIDERS),
                "hermes_external_providers": [item["name"] for item in HERMES_EXTERNAL_MEMORY_PROVIDERS],
                "status_semantics": "configured providers are live_unverified until an explicit smoke/sync integration is added",
            },
            "pluggable_interface": True,
            "status": "implemented",
            "secrets_redacted": True,
        }

    def catalog(self) -> dict[str, Any]:
        status = self.status()
        return {
            "object": "aiask.memory_provider_catalog",
            "default_provider": status.get("default_provider"),
            "active_provider": status.get("active_provider"),
            "providers": status.get("providers", []),
            "catalog": status.get("catalog", {}),
            "secrets_redacted": True,
        }

    def save(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return self.sqlite.add(
            content=str(arguments.get("content") or ""),
            user_id=arguments.get("user_id"),
            symbol=arguments.get("symbol"),
            strategy_id=arguments.get("strategy_id"),
            research_topic=arguments.get("research_topic"),
        )

    def search(self, arguments: dict[str, Any]) -> list[dict[str, Any]]:
        return self.sqlite.search(
            query=arguments.get("query"),
            user_id=arguments.get("user_id"),
            symbol=arguments.get("symbol"),
            strategy_id=arguments.get("strategy_id"),
            research_topic=arguments.get("research_topic"),
            limit=int(arguments.get("limit") or 20),
        )

    def audit(self) -> dict[str, Any]:
        status = self.status()
        providers = list(status.get("providers") or [])
        issues: list[dict[str, Any]] = []
        names = {str(item.get("name") or "") for item in providers if isinstance(item, dict)}
        for item in providers:
            if item.get("name") == status.get("active_provider") and not item.get("configured"):
                issues.append(
                    {
                        "severity": "warning",
                        "code": "active_memory_provider_unconfigured",
                        "provider": item.get("name"),
                        "message": "The requested memory provider is not configured; AIASK will use SQLite unless explicitly overridden.",
                    }
                )
        for item in HERMES_EXTERNAL_MEMORY_PROVIDERS:
            if str(item.get("name") or "") not in names:
                issues.append(
                    {
                        "severity": "warning",
                        "code": "memory_provider_catalog_gap",
                        "provider": item.get("name"),
                        "message": "Hermes external memory provider is not represented in the AIASK provider catalog.",
                    }
                )
        return {"status": status, "issues": issues, "issue_count": len(issues)}

    def _external_provider_status(self, item: dict[str, Any]) -> dict[str, Any]:
        required_env = [str(key) for key in list(item.get("required_env") or []) if str(key).strip()]
        configured = all(str(self.env.get(key) or "").strip() for key in required_env)
        return {
            "name": str(item.get("name") or ""),
            "type": str(item.get("type") or "external_memory"),
            "default": False,
            "configured": configured,
            "status": "live_unverified" if configured else "skipped_missing_credentials",
            "required_env": required_env,
            "capabilities": list(item.get("capabilities") or ["catalog", "readiness", "reserved_provider_slot"]),
            "integration_status": "catalog_only",
            "secrets_redacted": True,
        }
