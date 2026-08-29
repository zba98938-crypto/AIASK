from __future__ import annotations

import importlib.util
import os
import re
import shutil
import time
from collections.abc import Callable
from typing import Any

from .acp import ACPManager
from .capabilities import HERMES_BASELINE, HERMES_BASELINE_VERSION, HERMES_RELEASE_TAG, parity_summary
from .financial_readiness import financial_system_readiness
from .gateway import ADAPTERS, GatewayMessageStore, GatewayRuntime
from .learning_loop import LearningLoop
from .mcp_client import MCPAggregator
from .memory_providers import MemoryProviderManager
from .model_providers import ModelProviderRegistry, ProviderUsageStore
from .native_capabilities import SkillStore
from .plugin_runtime import NativePluginManager
from .rl_atropos import RLAtroposManager
from .runtime import AgentRuntime
from .security import SecurityScanner
from .skill_packs import SkillPackManager
from .terminal_backends import list_backends
from .tools.policy import GENERAL_FULL_TOOLSET
from .tui import status as tui_status_payload


def _redacted_env_name(name: Any) -> str:
    text = str(name or "").strip()
    lowered = text.lower()
    if any(token in lowered for token in ("secret", "token", "password", "credential")):
        return "[redacted-env-name]"
    return text


def _redact_sensitive_terms(text: str) -> str:
    redacted = str(text)
    replacements = {
        "secret": "sensitive",
        "token": "sensitive",
        "api_key": "sensitive",
        "apikey": "sensitive",
        "password": "sensitive",
        "credential": "sensitive",
        "credentials": "sensitive",
    }
    for needle, replacement in replacements.items():
        redacted = re.sub(re.escape(needle), replacement, redacted, flags=re.IGNORECASE)
    return redacted


def redact_required_env(payload: Any, *, redact_sensitive_names: bool = False) -> Any:
    """Keep diagnostic env names visible, with an optional strict mode for public health payloads."""
    if isinstance(payload, dict):
        redacted: dict[str, Any] = {}
        for key, value in payload.items():
            if key in {
                "required_env",
                "live_env",
                "required_env_groups",
                "required_env_names",
                "missing_env",
                "auth_env_vars",
                "missing_auth_env_vars",
            } and isinstance(value, list):
                names = [str(item) for item in value if str(item).strip()]
                redacted[key] = [_redacted_env_name(item) for item in names] if redact_sensitive_names else names
            else:
                redacted[key] = redact_required_env(value, redact_sensitive_names=redact_sensitive_names)
        return redacted
    if isinstance(payload, list):
        return [redact_required_env(item, redact_sensitive_names=redact_sensitive_names) for item in payload]
    if redact_sensitive_names and isinstance(payload, str):
        return _redact_sensitive_terms(payload)
    return payload


def parity_live_evidence(parity: dict[str, Any]) -> dict[str, Any]:
    checked_at = int(time.time())
    rows: list[dict[str, Any]] = []
    required_env_groups: set[str] = set()
    required_env_names: set[str] = set()
    sections = (
        ("capability", parity.get("matrix") or []),
        ("tool", parity.get("hermes_tool_mapping") or []),
        ("gateway_platform", parity.get("gateway_platform_mapping") or []),
        ("feature", parity.get("feature_mapping") or []),
    )
    delta_items: list[dict[str, Any]] = []
    for delta_name in ("v014_delta", "v016_delta"):
        delta = parity.get(delta_name)
        if not isinstance(delta, dict):
            continue
        for bucket in ("implemented", "partial", "missing", "excluded_by_design"):
            delta_items.extend(item for item in list(delta.get(bucket) or []) if isinstance(item, dict))
    if delta_items:
        sections = (*sections, ("delta", delta_items))
    for kind, items in sections:
        for item in list(items or []):
            if not isinstance(item, dict):
                continue
            required_env = [str(name) for name in list(item.get("required_env") or []) if str(name).strip()]
            live_status = str(item.get("live_status") or "unknown")
            if required_env:
                for group in required_env:
                    required_env_groups.add(group)
                    for name in group.split("|"):
                        if name.strip():
                            required_env_names.add(name.strip())
            if not required_env and live_status == "not_required":
                continue
            label = item.get("reference") or item.get("hermes_tool") or item.get("platform") or item.get("feature") or "item"
            rows.append(
                {
                    "kind": kind,
                    "name": str(label),
                    "area": item.get("area"),
                    "code_status": item.get("code_status"),
                    "mock_status": item.get("mock_status"),
                    "live_status": live_status,
                    "required_env": required_env,
                    "safe_to_smoke": live_status in {"not_required", "ready", "skipped_missing_credentials"},
                    "last_checked_at": checked_at,
                }
            )
    return {
        "object": "aiask.hermes_live_evidence",
        "baseline": parity.get("baseline"),
        "baseline_version": parity.get("baseline_version"),
        "baseline_release_tag": parity.get("baseline_release_tag"),
        "code_status": parity.get("code_status"),
        "core_code_status": parity.get("core_code_status"),
        "mock_status": parity.get("mock_status"),
        "live_status": parity.get("live_status"),
        "strict_status": parity.get("strict_status"),
        "live_unverified_count": parity.get("live_unverified_count"),
        "required_env_groups": sorted(required_env_groups),
        "required_env_names": sorted(required_env_names),
        "items": rows,
        "last_checked_at": checked_at,
    }


def full_surface_status_for_runtime(runtime: AgentRuntime) -> dict[str, Any]:
    gateway = GatewayRuntime(messages=GatewayMessageStore(runtime.session_store.path))
    learning = LearningLoop(session_store=runtime.session_store, state_path=runtime.session_store.path)
    rl = RLAtroposManager(runtime.session_store.path)
    mcp = MCPAggregator()
    skills = SkillStore()
    return {
        "full_scope": "hermes_full_runtime",
        "platform_gateway": gateway.status(),
        "terminal_backends": list_backends(),
        "learning_loop": learning.status(),
        "rl_training": rl.current_config(),
        "tui": tui_status_payload(),
        "providers": ModelProviderRegistry(usage_store=ProviderUsageStore(runtime.session_store.path)).status(),
        "memory": MemoryProviderManager(path=runtime.session_store.path).status(),
        "acp": ACPManager(mcp=mcp).status(),
        "security": SecurityScanner(policy=runtime.tool_registry.policy_engine.policy).status(),
        "skill_packs": SkillPackManager(skill_store=skills).status(),
    }


def hermes_readiness_payload_for_runtime(
    runtime: AgentRuntime,
    *,
    build_full_runtime: Callable[[], AgentRuntime],
    hermes_full_enabled: Callable[[], bool],
) -> dict[str, Any]:
    gateway = GatewayRuntime(messages=GatewayMessageStore(runtime.session_store.path))
    rl = RLAtroposManager(runtime.session_store.path)
    terminal_items = list_backends()
    plugin_manager = NativePluginManager()
    plugins = plugin_manager.list()
    mcp = MCPAggregator()
    provider_registry = ModelProviderRegistry(usage_store=ProviderUsageStore(runtime.session_store.path))
    memory_manager = MemoryProviderManager(path=runtime.session_store.path)
    acp_manager = ACPManager(mcp=mcp)
    security = SecurityScanner(policy=runtime.tool_registry.policy_engine.policy)
    skill_packs = SkillPackManager(skill_store=SkillStore())
    selected_names = build_full_runtime().tool_registry.names() if hermes_full_enabled() else runtime.tool_registry.names()
    parity = parity_summary(selected_names, env=dict(os.environ), gateway_adapters=ADAPTERS.keys())
    dependency = {
        "docker": bool(shutil.which("docker") or importlib.util.find_spec("docker")),
        "ssh": bool(shutil.which("ssh") or importlib.util.find_spec("asyncssh")),
        "apptainer_or_singularity": bool(shutil.which("apptainer") or shutil.which("singularity")),
        "modal": bool(shutil.which("modal") or importlib.util.find_spec("modal") or os.getenv("AIASK_MODAL_TERMINAL_COMMAND")),
        "daytona": bool(shutil.which("daytona") or importlib.util.find_spec("daytona") or os.getenv("AIASK_DAYTONA_TERMINAL_COMMAND")),
        "textual": bool(importlib.util.find_spec("textual")),
        "atroposlib": bool(importlib.util.find_spec("atroposlib")),
        "tinker_atropos": bool(importlib.util.find_spec("tinker_atropos")),
    }
    credentials = {
        "homeassistant": bool(os.getenv("HASS_URL") and os.getenv("HASS_TOKEN")),
        "rl": bool(os.getenv("TINKER_API_KEY") and os.getenv("WANDB_API_KEY")),
        "feishu": bool((os.getenv("FEISHU_APP_ID") and os.getenv("FEISHU_APP_SECRET")) or os.getenv("FEISHU_BOT_WEBHOOK")),
        "discord": bool(os.getenv("DISCORD_BOT_TOKEN")),
        "gateway_webhook": bool(os.getenv("AIASK_GATEWAY_WEBHOOK_URL") or os.getenv("AIASK_GATEWAY_WEBHOOK_SECRET")),
    }
    live_evidence = parity_live_evidence(parity)
    return {
        "object": "aiask.hermes_readiness",
        "implementation": "aiask_native",
        "embedded_vendor_runtime": False,
        "parity_baseline": parity.get("baseline"),
        "baseline_version": parity.get("baseline_version"),
        "baseline_release_tag": parity.get("baseline_release_tag"),
        "dependencies": dependency,
        "credentials": credentials,
        "live_evidence": live_evidence,
        "live_readiness": live_evidence,
        "terminal_backends": terminal_items,
        "gateway": gateway.status(),
        "mcp": mcp.registration_diagnostics(),
        "providers": provider_registry.status(),
        "memory": memory_manager.status(),
        "acp": acp_manager.readiness(),
        "security": security.status(),
        "skill_packs": skill_packs.status(),
        "rl": rl.readiness(),
        "plugins": {
            "count": len(plugins),
            "enabled_count": sum(1 for item in plugins if item.get("enabled")),
            "readiness": plugin_manager.readiness(),
            "runners": [
                {
                    "name": item.get("name"),
                    "enabled": item.get("enabled"),
                    "runner": item.get("runner"),
                    "tools": [tool.get("name") for tool in list(item.get("tools") or []) if isinstance(tool, dict)],
                    "commands": [command.get("name") for command in list(item.get("commands") or []) if isinstance(command, dict)],
                }
                for item in plugins
            ],
        },
        "feature_mapping": parity.get("feature_mapping", []),
        "missing_features": parity.get("missing_features", []),
        "implemented_features_count": parity.get("implemented_features_count", 0),
        "network": {
            "live_tests_enabled": bool(os.getenv("AIASK_RUN_LIVE_HERMES_TESTS")),
            "proxy_configured": bool(os.getenv("HTTPS_PROXY") or os.getenv("HTTP_PROXY")),
        },
        "permissions": {
            "control_token_configured": bool(os.getenv("AIASK_AGENT_CONTROL_TOKEN") or os.getenv("AIASK_LOCAL_CONTROL_TOKEN")),
            "workspace_roots": list(runtime.tool_registry.policy_engine.policy.workspace_roots),
        },
    }


def hermes_status_payload_for_runtime(
    runtime: AgentRuntime,
    *,
    build_full_runtime: Callable[[], AgentRuntime],
    hermes_full_enabled: Callable[[], bool],
    full_runtime_active: Callable[[], bool],
) -> dict[str, Any]:
    selected_names = build_full_runtime().tool_registry.names() if hermes_full_enabled() else runtime.tool_registry.names()
    return {
        "object": "aiask.hermes_status",
        "implementation": "aiask_native",
        "baseline": HERMES_BASELINE,
        "baseline_version": HERMES_BASELINE_VERSION,
        "baseline_release_tag": HERMES_RELEASE_TAG,
        "embedded_vendor_runtime": False,
        "full_mode_enabled": hermes_full_enabled(),
        "full_mode_active": full_runtime_active(),
        "evaluated_toolset": GENERAL_FULL_TOOLSET if hermes_full_enabled() else runtime.tool_registry.policy_engine.toolset,
        "parity": parity_summary(selected_names, env=dict(os.environ), gateway_adapters=ADAPTERS.keys()),
        **full_surface_status_for_runtime(runtime),
    }


async def financial_readiness_payload_for_runtime(
    runtime: AgentRuntime,
    *,
    build_full_runtime: Callable[[], AgentRuntime],
    hermes_full_enabled: Callable[[], bool],
    current_full_runtime: Callable[[], AgentRuntime | None],
    control_token_configured: bool,
    ai_status: dict[str, Any],
    intent_store: Any | None = None,
) -> dict[str, Any]:
    return await financial_system_readiness(
        runtime,
        full_runtime=build_full_runtime() if hermes_full_enabled() else current_full_runtime(),
        full_mode_enabled=hermes_full_enabled(),
        control_token_configured=control_token_configured,
        ai_status=ai_status,
        intent_store=intent_store,
    )
