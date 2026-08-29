from __future__ import annotations

import json
import os
import time
from collections.abc import Callable
from typing import Any

from .adapters import quant as quant_adapter
from .memory_providers import MemoryProviderManager
from .model_providers import ModelProviderRegistry, ProviderUsageStore
from .paths import aiask_agent_home, default_intent_db_path, default_quant_research_db_path
from .route_auth import control_token_configured
from .stock_data_sources import list_stock_data_sources


def agent_endpoint(default: str = "http://127.0.0.1:8765") -> str:
    host = str(os.getenv("AIASK_AGENT_HOST", "")).strip()
    port = str(os.getenv("AIASK_AGENT_PORT", "")).strip()
    if host and port:
        return f"http://{host}:{port}"
    return default


def local_profile_path() -> Any:
    return aiask_agent_home() / "local_profile.json"


def default_local_profile() -> dict[str, Any]:
    return {
        "object": "aiask.local_profile",
        "user_id": "local",
        "profile_name": "Local Operator",
        "preferences": {
            "investment_style": "balanced",
            "risk_tolerance": 3,
            "preferred_sectors": [],
            "investment_horizon": "medium",
            "experience_level": "intermediate",
            "tags": [],
        },
        "behavior": {
            "frequent_queries": [],
            "preferred_models": [],
            "active_hours": [],
            "common_stocks": [],
        },
        "storage": "local_file",
        "path": str(local_profile_path()),
        "updated_at": None,
        "secrets_redacted": True,
    }


def local_profile_payload() -> dict[str, Any]:
    profile = default_local_profile()
    path = local_profile_path()
    try:
        if path.exists():
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                for key in ("user_id", "profile_name", "updated_at", "preferences", "behavior"):
                    if loaded.get(key):
                        profile[key] = loaded[key]
    except Exception as exc:
        profile["status"] = "degraded"
        profile["error"] = str(exc)
    profile.setdefault("status", "ready")
    return profile


def save_local_profile(payload: dict[str, Any] | None = None) -> dict[str, Any]:
    current = local_profile_payload()
    update = dict(payload or {})
    user_id = str(update.get("user_id") or current.get("user_id") or "local").strip() or "local"
    profile_name = str(update.get("profile_name") or current.get("profile_name") or "Local Operator").strip() or "Local Operator"
    current_preferences = current.get("preferences") if isinstance(current.get("preferences"), dict) else {}
    current_behavior = current.get("behavior") if isinstance(current.get("behavior"), dict) else {}
    update_preferences = update.get("preferences") if isinstance(update.get("preferences"), dict) else {}
    update_behavior = update.get("behavior") if isinstance(update.get("behavior"), dict) else {}
    saved = {
        "object": "aiask.local_profile",
        "user_id": user_id,
        "profile_name": profile_name,
        "preferences": {
            **dict(current_preferences or {}),
            **dict(update_preferences or {}),
        },
        "behavior": {
            **dict(current_behavior or {}),
            **dict(update_behavior or {}),
        },
        "storage": "local_file",
        "path": str(local_profile_path()),
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "secrets_redacted": True,
        "status": "ready",
    }
    path = local_profile_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(saved, ensure_ascii=False, sort_keys=True, indent=2), encoding="utf-8")
    return saved


def desktop_settings_status_payload_for_runtime(
    runtime: Any,
    *,
    ai_status_payload: Callable[[Any], dict[str, Any]],
    endpoint: str | None = None,
    control_authorized: bool = False,
    control_reason: str | None = None,
) -> dict[str, Any]:
    quant_db = quant_adapter.database_status()
    return {
        "object": "aiask.desktop_settings_status",
        "agent": {
            "endpoint": endpoint or agent_endpoint(),
            "api_token_configured": bool(str(os.getenv("AIASK_AGENT_API_TOKEN", "")).strip()),
            "control_token_configured": control_token_configured(),
            "control_authorized": bool(control_authorized),
            "control_reason": None if control_authorized else control_reason,
            "toolset": runtime.tool_registry.policy_engine.toolset,
            "model": runtime.model,
            "max_iterations": runtime.max_iterations,
        },
        "llm": {
            "ai_status": ai_status_payload(runtime),
            "providers": ModelProviderRegistry(usage_store=ProviderUsageStore(runtime.session_store.path)).status(),
        },
        "memory": MemoryProviderManager(path=runtime.session_store.path).status(),
        "databases": {
            "agent_state": {
                "backend": "sqlite",
                "path": str(runtime.session_store.path),
                "configured": True,
                "writable": True,
            },
            "intent_state": {
                "backend": "sqlite",
                "path": str(default_intent_db_path()),
                "configured": True,
            },
            "quant_research": {
                "backend": "sqlite",
                "path": str(default_quant_research_db_path()),
                "configured": True,
            },
            "akshare": quant_db,
        },
        "stock_data_sources": list_stock_data_sources(),
        "profile": local_profile_payload(),
        "secrets_redacted": True,
    }


async def desktop_data_status_payload_for_runtime(
    runtime: Any,
    arguments: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload = dict(arguments or {})
    presets = quant_adapter.quant_presets()
    templates = list(presets.get("templates") or [])
    default_codes = list((templates[0] if templates else {}).get("universe") or [])
    if "codes" in payload:
        raw_codes = payload.get("codes")
    elif "universe" in payload:
        raw_codes = payload.get("universe")
    else:
        raw_codes = default_codes
    if isinstance(raw_codes, str):
        codes = [item.strip() for item in raw_codes.replace("\n", ",").split(",") if item.strip()]
    else:
        codes = [str(item).strip() for item in list(raw_codes or []) if str(item).strip()]
    max_stale_days = int(payload.get("max_stale_days") or 5)
    gate = await runtime.tool_registry.call_tool("agent_quant_data_gate", {"codes": codes, "max_stale_days": max_stale_days})
    gate_data = gate.get("data") if isinstance(gate.get("data"), dict) else {}
    coverage = dict(gate_data.get("coverage") or {})
    return {
        "object": "aiask.desktop_data_status",
        "status": "ready" if gate_data.get("ready") else "blocked",
        "database": quant_adapter.database_status(),
        "presets": presets,
        "quality_gate": gate,
        "data_validation": None,
        "freshness": gate_data.get("freshness"),
        "codes": codes,
        "max_stale_days": max_stale_days,
        "missing_count": int(coverage.get("missing_count") or 0),
        "stale_count": int(coverage.get("stale_count") or 0),
        "secrets_redacted": True,
    }


async def desktop_data_sync_plan_payload_for_runtime(
    runtime: Any,
    arguments: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload = dict(arguments or {})
    data_status = await desktop_data_status_payload_for_runtime(runtime, payload)
    codes = data_status.get("codes") or []
    task_type = str(payload.get("task_type") or payload.get("type") or "kline").strip() or "kline"
    period = str(payload.get("period") or "daily").strip() or "daily"
    no_code_task_types = {
        "core_market",
        "factor_context",
        "market_temperature_snapshot_cache",
        "market_text_source_ingest",
        "vector_backfill_market_docs",
        "vector_backfill_kline_patterns",
        "vector_backfill_stock_profiles",
        "vector_backfill_factor_candidates",
        "factor_external_research_ingest",
        "vector_build_snapshot",
        "vector_benchmark_collection",
        "vector_optimize_bootstrap",
        "factor_validation_bootstrap",
    }
    intent_params = {
        "task_type": task_type,
        "codes": codes,
        "period": period,
        "priority": payload.get("priority") or "normal",
        "force": bool(payload.get("force", False)),
    }
    if task_type == "market_temperature_snapshot_cache":
        intent_params.update(
            {
                "limit": max(1, min(int(payload.get("limit") or 1000), 1000)),
                "top_n": max(0, min(int(payload.get("top_n") or 20), 50)),
                "min_bars": max(2, min(int(payload.get("min_bars") or 20), 120)),
            }
        )
        if payload.get("as_of"):
            intent_params["as_of"] = str(payload.get("as_of")).strip()
    plan_ready = bool(codes) or task_type in no_code_task_types
    rationale = (
        f"Sync {task_type} data from Desktop."
        if task_type in no_code_task_types and not codes
        else f"Sync {task_type} data for {len(codes)} codes from Desktop."
    )
    return {
        "object": "aiask.desktop_data_sync_plan",
        "status": "ready" if plan_ready else "needs_codes",
        "data_status": data_status,
        "intent_request": {
            "action": "data_sync.sync",
            "params": intent_params,
            "rationale": rationale,
        },
        "commands": [
            {"label": "Create approval intent", "method": "POST", "path": "/intents"},
            {"label": "Confirm after review", "method": "POST", "path": "/intents/{intent_id}/confirm"},
        ],
        "side_effect": {"level": "stateful", "confirmation_required": True, "target": "data_sync.sync"},
        "secrets_redacted": True,
    }
