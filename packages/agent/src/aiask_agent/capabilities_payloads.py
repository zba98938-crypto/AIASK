from __future__ import annotations

import asyncio
import os
import time
from collections.abc import Callable
from typing import Any

from fastapi import Request

from .acp import ACPManager
from .adapters import quant as quant_adapter
from .capabilities import parity_summary
from .financial_readiness import financial_system_readiness
from .gateway import ADAPTERS
from .mcp_client import MCPAggregator
from .memory_providers import MemoryProviderManager
from .model_providers import ModelProviderRegistry, ProviderUsageStore
from .native_capabilities import SkillStore
from .plugin_runtime import NativePluginManager
from .quant_research import QuantResearchStore
from .runtime import AgentRuntime
from .security import SecurityScanner
from .skill_packs import SkillPackManager


def _capability_counts(*groups: Any) -> dict[str, int]:
    counts = {"implemented": 0, "live_unverified": 0, "unconfigured": 0, "failed": 0, "missing": 0, "gated": 0}
    for group in groups:
        for item in list(group or []):
            if not isinstance(item, dict):
                continue
            status = str(item.get("status") or item.get("live_status") or "unconfigured")
            if status == "implemented":
                counts["implemented"] += 1
            elif status in {"live_unverified", "skipped_missing_credentials", "partial"}:
                counts["live_unverified"] += 1
            elif status in {"missing", "planned"}:
                counts["missing"] += 1
            elif status in {"failed", "blocked"}:
                counts["failed"] += 1
            else:
                counts["unconfigured"] += 1
    return counts


async def capabilities_payload_for_runtime(
    runtime: AgentRuntime,
    request: Request,
    *,
    build_full_runtime: Callable[[], AgentRuntime],
    current_full_runtime: Callable[[], AgentRuntime | None],
    full_authorized: Callable[[Request], tuple[bool, str | None]],
    control_authorized: Callable[[Request], tuple[bool, str | None]],
    hermes_full_enabled: Callable[[], bool],
    hermes_readiness_payload: Callable[[], dict[str, Any]],
    quant_store: QuantResearchStore,
    ai_status_payload: Callable[[], dict[str, Any]],
    intent_store: Any | None = None,
) -> dict[str, Any]:
    selected_names = build_full_runtime().tool_registry.names() if hermes_full_enabled() else runtime.tool_registry.names()
    parity = parity_summary(selected_names, env=dict(os.environ), gateway_adapters=ADAPTERS.keys())
    readiness = hermes_readiness_payload()
    full_ok, full_reason = full_authorized(request)
    control_ok, control_reason = control_authorized(request)
    full_mode_enabled = hermes_full_enabled()
    control_token_configured = bool(
        str(os.getenv("AIASK_AGENT_CONTROL_TOKEN", "")).strip()
        or str(os.getenv("AIASK_LOCAL_CONTROL_TOKEN", "")).strip()
    )
    mcp = MCPAggregator()
    mcp_registration = mcp.registration_diagnostics()

    async def read_only_tool(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        try:
            return await runtime.tool_registry.call_tool(name, arguments)
        except Exception as exc:
            return {"success": False, "data": {"configured": False, "tool": name}, "error": str(exc), "error_code": "DESKTOP_TOOL_UNAVAILABLE"}

    db_env_snapshot = {key: os.environ.get(key) for key in quant_adapter.DATABASE_ENV_KEYS}
    if full_ok:
        factory_status, factory_runs, review_snapshot = await asyncio.gather(
            read_only_tool("agent_factory_status", {"recent_run_limit": 5, "_timeout_seconds": 5}),
            read_only_tool("agent_factory_runs", {"limit": 10, "_timeout_seconds": 5}),
            read_only_tool("agent_strategy_review_snapshot", {"limit": 20}),
        )
    else:
        factory_status = {
            "success": False,
            "data": {"configured": False, "gated": True, "tool": "agent_factory_status"},
            "error": full_reason or "control token required",
            "error_code": "CONTROL_TOKEN_REQUIRED",
        }
        factory_runs = {
            "success": False,
            "data": {"configured": False, "gated": True, "tool": "agent_factory_runs", "runs": []},
            "error": full_reason or "control token required",
            "error_code": "CONTROL_TOKEN_REQUIRED",
        }
        review_snapshot = {
            "success": False,
            "data": {"configured": False, "gated": True, "tool": "agent_strategy_review_snapshot"},
            "error": full_reason or "control token required",
            "error_code": "CONTROL_TOKEN_REQUIRED",
        }
    for key, value in db_env_snapshot.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
    quant_presets = quant_adapter.quant_presets()
    recent_quant_runs = quant_store.list(limit=5)

    gated = {"gated": True, "reason": full_reason or "control token required"}
    skills_payload: Any = gated
    plugins_payload: Any = gated
    mcp_payload: dict[str, Any] = {
        "gated": not full_ok,
        "reason": None if full_ok else full_reason,
        "registration_status": mcp_registration["registration_status"],
        "discovery_status": mcp_registration["discovery_status"],
        "discovered_counts": mcp_registration["discovered_counts"],
        "configured": mcp_registration["configured"],
        "config_path": mcp_registration["config_path"],
        "config_exists": mcp_registration["config_exists"],
        "detected_service_port": mcp_registration["detected_service_port"],
        "detected_service_url": mcp_registration["detected_service_url"],
        "suggested_registration_url": mcp_registration["suggested_registration_url"],
        "auth_configured": mcp_registration["auth_configured"],
        "auth_env_vars": mcp_registration["auth_env_vars"],
        "missing_auth_env_vars": mcp_registration["missing_auth_env_vars"],
        "partial_success": mcp_registration.get("partial_success"),
        "warnings": mcp_registration.get("warnings") or [],
        "unsupported_methods": mcp_registration.get("unsupported_methods") or [],
        "error_code": mcp_registration["error_code"],
        "detail": mcp_registration["detail"],
        "servers": mcp.servers_summary(include_all=full_ok),
        "tools": [],
        "resources": [],
        "prompts": [],
        "oauth": [],
    }
    if full_ok:
        full = build_full_runtime()
        skills_result = await full.tool_registry.call_tool("agent_skill_manage", {"action": "snapshot"})
        skills_payload = dict(skills_result.get("data") or {})
        plugins_payload = NativePluginManager().list()
        mcp_payload.update(
            {
                "tools": mcp.tools_summary(include_all=True),
                "resources": mcp.resources_summary(include_all=True),
                "prompts": mcp.prompts_summary(include_all=True),
                "oauth": mcp.oauth_status(include_all=True),
            }
        )

    provider_payload = ModelProviderRegistry(usage_store=ProviderUsageStore(runtime.session_store.path)).status()
    memory_payload = MemoryProviderManager(path=runtime.session_store.path).status()
    acp_payload = ACPManager(mcp=mcp).status()
    security_payload = SecurityScanner(policy=runtime.tool_registry.policy_engine.policy).status()
    skill_pack_payload = SkillPackManager(skill_store=SkillStore()).status()

    counts = _capability_counts(
        parity.get("hermes_tool_mapping"),
        parity.get("gateway_platform_mapping"),
        parity.get("feature_mapping"),
    )
    issues = [
        *list(parity.get("missing_hermes_tools") or []),
        *list(parity.get("missing_gateway_platforms") or []),
        *list(parity.get("missing_features") or []),
    ]
    full_runtime_for_readiness = build_full_runtime() if hermes_full_enabled() else current_full_runtime()
    return {
        "object": "aiask.desktop_capabilities",
        "summary": {
            "status": parity.get("strict_status") or parity.get("status"),
            "source": "live_backend" if full_ok else "gated",
            "counts": counts,
            "issue_count": len(issues),
            "control": {
                "authorized": full_ok,
                "reason": None if full_ok else full_reason,
                "full_mode_enabled": full_mode_enabled,
                "control_token_configured": control_token_configured,
                "control_authorized": control_ok,
                "control_reason": None if control_ok else control_reason,
                "gated_reason": None if full_ok else full_reason,
            },
            "refreshed_at": int(time.time()),
        },
        "hermes": {
            "status": {
                "implementation": "aiask_native",
                "baseline": parity.get("baseline"),
                "baseline_version": parity.get("baseline_version"),
                "baseline_release_tag": parity.get("baseline_release_tag"),
                "embedded_vendor_runtime": False,
                "full_mode_enabled": hermes_full_enabled(),
                "full_mode_active": full_ok,
            },
            "parity": parity,
            "readiness": readiness,
            "tool_mapping": parity.get("hermes_tool_mapping", []),
            "platform_mapping": parity.get("gateway_platform_mapping", []),
            "feature_mapping": parity.get("feature_mapping", []),
            "issues": issues,
            "providers": provider_payload,
            "memory": memory_payload,
            "acp": acp_payload,
            "security": security_payload,
            "skill_packs": skill_pack_payload,
        },
        "mcp": mcp_payload,
        "strategy_factory": {
            "status": factory_status,
            "runs": factory_runs,
            "review_snapshot": review_snapshot,
        },
        "quant": {
            "presets": quant_presets,
            "recent_runs": recent_quant_runs,
            "data_status": quant_presets.get("data_status", {}),
            "status": "ready" if quant_presets.get("data_status", {}).get("status") == "ready" else "unconfigured",
        },
        "financial_system": await financial_system_readiness(
            runtime,
            full_runtime=full_runtime_for_readiness,
            full_mode_enabled=hermes_full_enabled(),
            control_token_configured=control_token_configured,
            ai_status=ai_status_payload(),
            intent_store=intent_store,
        ),
        "skills": skills_payload,
        "skill_packs": skill_pack_payload,
        "plugins": plugins_payload,
        "providers": provider_payload,
        "memory": memory_payload,
        "acp": acp_payload,
        "security": security_payload,
        "ai": ai_status_payload(),
        "raw_refs": {
            "parity": "/v1/capabilities/parity",
            "readiness": "/v1/hermes/readiness",
            "mcp_servers": "/v1/mcp/servers",
            "skills": "/v1/skills",
            "ai_status": "/v1/ai/status",
            "quant_presets": "/v1/finance/quant/presets",
            "quant_research_runs": "/v1/finance/quant/research-runs",
            "financial_system_readiness": "/v1/financial-system/readiness",
        },
    }
