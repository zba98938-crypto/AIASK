"""Execution manager: TWAP/VWAP planning with lifecycle tracking and cost transparency."""

from __future__ import annotations

import asyncio
import os
import logging
import json
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from copy import deepcopy
from datetime import datetime, timezone
from threading import Lock
from typing import Any

from ...services.artifact_registry import (
    get_artifact_async,
    list_artifacts_async,
    register_artifact_async,
)
from ...services.market_data_access import FALLBACK_DB_ONLY, get_quote_snapshot_sync
from ...utils import fail, ok, normalize_code
from .compliance_manager import evaluate_order_compliance
from ..manager_protocol import normalize_manager_payload
from ..risk_guard import audit_event


_EXECUTION_TASKS: dict[str, dict[str, Any]] = {}
_EXECUTION_TASK_ARTIFACT_STRATEGY = "execution_task"
_EXECUTION_TASK_ARTIFACT_VERSION = "v1"
_EXECUTION_CONFIG_ARTIFACT_ID = "execution_manager:soft_gate_config"
_EXECUTION_CONFIG_ARTIFACT_STRATEGY = "execution_manager_config"
_EXECUTION_ARTIFACT_SCAN_LIMIT = 400
_RUNTIME_CONFIG_LOADED = False
_REALTIME_QUOTE_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_REALTIME_QUOTE_SKIP_UNTIL: dict[str, float] = {}
_REALTIME_QUOTE_EXECUTOR: ThreadPoolExecutor | None = None
_REALTIME_QUOTE_EXECUTOR_LOCK = Lock()

logger = logging.getLogger(__name__)


def _get_realtime_quote_executor() -> ThreadPoolExecutor:
    global _REALTIME_QUOTE_EXECUTOR
    with _REALTIME_QUOTE_EXECUTOR_LOCK:
        if _REALTIME_QUOTE_EXECUTOR is None:
            _REALTIME_QUOTE_EXECUTOR = ThreadPoolExecutor(
                max_workers=max(1, int(os.getenv("EXECUTION_MANAGER_REALTIME_ENRICH_MAX_WORKERS", "4") or "4")),
                thread_name_prefix="execution-realtime",
            )
        return _REALTIME_QUOTE_EXECUTOR


def shutdown_realtime_quote_executor(*, wait: bool = False) -> None:
    global _REALTIME_QUOTE_EXECUTOR
    with _REALTIME_QUOTE_EXECUTOR_LOCK:
        executor = _REALTIME_QUOTE_EXECUTOR
        _REALTIME_QUOTE_EXECUTOR = None
    _REALTIME_QUOTE_SKIP_UNTIL.clear()
    if executor is not None:
        executor.shutdown(wait=wait, cancel_futures=True)


_SOFT_GATE_PROFILES: dict[str, dict[str, float | int]] = {
    "conservative": {
        "max_order_shares": 500_000,
        "max_slice_shares": 100_000,
        "min_duration_minutes": 10,
        "max_cost_ratio": 0.003,
    },
    "balanced": {
        "max_order_shares": 1_000_000,
        "max_slice_shares": 200_000,
        "min_duration_minutes": 5,
        "max_cost_ratio": 0.005,
    },
    "aggressive": {
        "max_order_shares": 2_000_000,
        "max_slice_shares": 400_000,
        "min_duration_minutes": 3,
        "max_cost_ratio": 0.008,
    },
}

_SOFT_GATE_THRESHOLD_KEYS = (
    "max_order_shares",
    "max_slice_shares",
    "min_duration_minutes",
    "max_cost_ratio",
)


_SOFT_GATE_RUNTIME_CONFIG: dict[str, Any] = {
    "default_profile": "balanced",
    "default_threshold_overrides": {},
    "code_profiles": {},
}


def _reset_runtime_config_state() -> None:
    global _RUNTIME_CONFIG_LOADED
    _RUNTIME_CONFIG_LOADED = False
    _SOFT_GATE_RUNTIME_CONFIG.clear()
    _SOFT_GATE_RUNTIME_CONFIG.update(
        {
            "default_profile": "balanced",
            "default_threshold_overrides": {},
            "code_profiles": {},
        }
    )

def _apply_soft_gate_runtime_defaults(kwargs: dict) -> dict:
    """Apply runtime soft-gate defaults when request-level params are absent."""
    merged = dict(kwargs)
    code = str(merged.get("code") or "").strip()

    if merged.get("soft_gate_profile") is None:
        code_profiles = _SOFT_GATE_RUNTIME_CONFIG.get("code_profiles", {})
        if code and isinstance(code_profiles, dict) and code_profiles.get(code):
            merged["soft_gate_profile"] = code_profiles.get(code)
        else:
            merged["soft_gate_profile"] = _SOFT_GATE_RUNTIME_CONFIG.get("default_profile", "balanced")

    default_overrides = _SOFT_GATE_RUNTIME_CONFIG.get("default_threshold_overrides", {})
    if isinstance(default_overrides, dict):
        for key in _SOFT_GATE_THRESHOLD_KEYS:
            if merged.get(key) is None and default_overrides.get(key) is not None:
                merged[key] = default_overrides.get(key)

    return merged

def _soft_gate_config_view() -> dict:
    return {
        "default_profile": _SOFT_GATE_RUNTIME_CONFIG.get("default_profile", "balanced"),
        "default_threshold_overrides": dict(_SOFT_GATE_RUNTIME_CONFIG.get("default_threshold_overrides", {})),
        "code_profiles": dict(_SOFT_GATE_RUNTIME_CONFIG.get("code_profiles", {})),
    }

def _parse_iso_timestamp(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value
    text = str(value or "").strip()
    if not text:
        return datetime.min.replace(tzinfo=timezone.utc)
    try:
        normalized = text[:-1] + "+00:00" if text.endswith("Z") else text
        parsed = datetime.fromisoformat(normalized)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed
    except Exception:
        return datetime.min.replace(tzinfo=timezone.utc)

def _task_updated_at(task: dict) -> datetime:
    for key in ("updated_at", "completed_at", "created_at"):
        value = task.get(key)
        if value:
            return _parse_iso_timestamp(value)
    return datetime.min.replace(tzinfo=timezone.utc)

def _touch_task(task: dict) -> None:
    task["updated_at"] = _now_iso()

def _build_runtime_config_artifact() -> dict[str, Any]:
    return {
        "artifact_id": _EXECUTION_CONFIG_ARTIFACT_ID,
        "strategy": _EXECUTION_CONFIG_ARTIFACT_STRATEGY,
        "strategy_version": _EXECUTION_TASK_ARTIFACT_VERSION,
        "payload": {
            "kind": _EXECUTION_CONFIG_ARTIFACT_STRATEGY,
            "schema_version": 1,
            "soft_gate_config": _soft_gate_config_view(),
        },
    }

def _extract_runtime_config_artifact(artifact: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(artifact, dict):
        return None
    payload = artifact.get("payload") if isinstance(artifact.get("payload"), dict) else artifact
    config = payload.get("soft_gate_config") if isinstance(payload.get("soft_gate_config"), dict) else None
    if config is None:
        return None
    return {
        "default_profile": str(config.get("default_profile") or "balanced").strip().lower() or "balanced",
        "default_threshold_overrides": dict(config.get("default_threshold_overrides") or {}),
        "code_profiles": dict(config.get("code_profiles") or {}),
    }

async def _ensure_runtime_config_loaded() -> None:
    global _RUNTIME_CONFIG_LOADED
    if _RUNTIME_CONFIG_LOADED:
        return
    try:
        artifact = await get_artifact_async(_EXECUTION_CONFIG_ARTIFACT_ID)
        config = _extract_runtime_config_artifact(artifact)
        if config:
            _SOFT_GATE_RUNTIME_CONFIG.clear()
            _SOFT_GATE_RUNTIME_CONFIG.update(config)
    except Exception as exc:
        logger.warning("execution_manager load runtime config failed: %s", exc)
    finally:
        _RUNTIME_CONFIG_LOADED = True

async def _persist_runtime_config() -> None:
    global _RUNTIME_CONFIG_LOADED
    try:
        await register_artifact_async(_build_runtime_config_artifact())
        _RUNTIME_CONFIG_LOADED = True
    except Exception as exc:
        logger.warning("execution_manager persist runtime config failed: %s", exc)

def _build_task_artifact(task: dict) -> dict[str, Any]:
    task_payload = deepcopy(task)
    task_payload.setdefault("task_id", str(task_payload.get("task_id") or ""))
    task_payload["updated_at"] = str(task_payload.get("updated_at") or _now_iso())
    return {
        "artifact_id": str(task_payload.get("task_id") or ""),
        "strategy": _EXECUTION_TASK_ARTIFACT_STRATEGY,
        "strategy_version": _EXECUTION_TASK_ARTIFACT_VERSION,
        "code": task_payload.get("code"),
        "payload": {
            "kind": _EXECUTION_TASK_ARTIFACT_STRATEGY,
            "schema_version": 1,
            "task": task_payload,
        },
    }

def _extract_task_from_artifact(artifact: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(artifact, dict):
        return None

    strategy = str(artifact.get("strategy") or "").strip().lower()
    payload = artifact.get("payload") if isinstance(artifact.get("payload"), dict) else artifact
    kind = str(payload.get("kind") or strategy or "").strip().lower()
    if kind != _EXECUTION_TASK_ARTIFACT_STRATEGY:
        return None

    task = payload.get("task") if isinstance(payload.get("task"), dict) else payload
    if not isinstance(task, dict):
        return None

    task_payload = deepcopy(task)
    task_id = str(task_payload.get("task_id") or artifact.get("artifact_id") or "").strip()
    if not task_id:
        return None
    task_payload["task_id"] = task_id
    task_payload.setdefault("updated_at", artifact.get("updated_at") or task_payload.get("created_at") or _now_iso())
    return task_payload

async def _persist_task(task: dict) -> None:
    task_id = str(task.get("task_id") or "").strip()
    if not task_id:
        return
    _touch_task(task)
    _EXECUTION_TASKS[task_id] = task
    try:
        await register_artifact_async(_build_task_artifact(task))
    except Exception as exc:
        logger.warning("execution_manager persist task %s failed: %s", task_id, exc)

async def _load_task(task_id: str) -> dict[str, Any] | None:
    resolved_task_id = str(task_id or "").strip()
    if not resolved_task_id:
        return None
    cached = _EXECUTION_TASKS.get(resolved_task_id)
    if cached is not None:
        return cached
    try:
        artifact = await get_artifact_async(resolved_task_id)
    except Exception as exc:
        logger.warning("execution_manager load task %s failed: %s", resolved_task_id, exc)
        return None
    task = _extract_task_from_artifact(artifact)
    if task is not None:
        _EXECUTION_TASKS[resolved_task_id] = task
    return task

async def _load_all_tasks(limit: int = _EXECUTION_ARTIFACT_SCAN_LIMIT) -> list[dict[str, Any]]:
    task_map: dict[str, dict[str, Any]] = {
        task_id: task for task_id, task in _EXECUTION_TASKS.items() if str(task_id).strip()
    }
    try:
        rows = await list_artifacts_async(limit=max(50, int(limit or _EXECUTION_ARTIFACT_SCAN_LIMIT)))
    except Exception as exc:
        logger.warning("execution_manager list persisted tasks failed: %s", exc)
        rows = []

    artifact_ids = [
        str(row.get("artifact_id") or "").strip()
        for row in rows
        if str(row.get("strategy") or "").strip().lower() == _EXECUTION_TASK_ARTIFACT_STRATEGY
    ]

    if artifact_ids:
        artifacts = await asyncio.gather(
            *(get_artifact_async(artifact_id) for artifact_id in artifact_ids),
            return_exceptions=True,
        )
        for artifact in artifacts:
            if isinstance(artifact, Exception):
                logger.warning("execution_manager read persisted task artifact failed: %s", artifact)
                continue
            task = _extract_task_from_artifact(artifact)
            if task is None:
                continue
            task_id = str(task.get("task_id") or "").strip()
            existing = task_map.get(task_id)
            if existing is None or _task_updated_at(task) >= _task_updated_at(existing):
                task_map[task_id] = task
                _EXECUTION_TASKS[task_id] = task

    return list(task_map.values())

async def _refresh_and_persist_tasks(tasks: list[dict[str, Any]]) -> None:
    dirty_tasks: list[dict[str, Any]] = []
    for task in tasks:
        if _refresh_task_status(task):
            dirty_tasks.append(task)
    if dirty_tasks:
        await asyncio.gather(*(_persist_task(task) for task in dirty_tasks), return_exceptions=True)

def _resolve_soft_gate_thresholds(kwargs: dict) -> dict:
    """Resolve soft gate thresholds by profile + per-request overrides."""
    kwargs = _apply_soft_gate_runtime_defaults(kwargs)
    profile_input = str(kwargs.get("soft_gate_profile", "balanced") or "balanced").strip().lower()
    profile = profile_input
    profile_fallback_used = False
    profile_fallback_reason = None
    if profile not in _SOFT_GATE_PROFILES:
        # P3-5.16 fix: 标记 fallback 已生效(诊断报告 §5.16)
        # 历史问题:'unknown' 之类无效 profile silently fallback 到 'balanced',AI 不知错传
        profile_fallback_used = True
        profile_fallback_reason = (
            f"unknown soft_gate_profile={profile_input!r}, fallback to 'balanced'"
        )
        profile = "balanced"

    base = dict(_SOFT_GATE_PROFILES[profile])
    base["_profile_fallback_used"] = profile_fallback_used
    if profile_fallback_reason:
        base["_profile_fallback_reason"] = profile_fallback_reason
    base["_resolved_profile"] = profile
    base["_requested_profile"] = profile_input

    if kwargs.get("max_order_shares") is not None:
        base["max_order_shares"] = int(kwargs.get("max_order_shares") or base["max_order_shares"])
    if kwargs.get("max_slice_shares") is not None:
        base["max_slice_shares"] = int(kwargs.get("max_slice_shares") or base["max_slice_shares"])
    if kwargs.get("min_duration_minutes") is not None:
        base["min_duration_minutes"] = int(kwargs.get("min_duration_minutes") or base["min_duration_minutes"])
    if kwargs.get("max_cost_ratio") is not None:
        base["max_cost_ratio"] = float(kwargs.get("max_cost_ratio") or base["max_cost_ratio"])

    return {
        "profile": profile,
        "max_order_shares": int(base["max_order_shares"]),
        "max_slice_shares": int(base["max_slice_shares"]),
        "min_duration_minutes": int(base["min_duration_minutes"]),
        "max_cost_ratio": float(base["max_cost_ratio"]),
    }

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

def _normalize_kwargs(kwargs: dict) -> dict:
    """Normalize kwargs, merge kwargs payload and keep backward-compatible aliases."""
    params = kwargs.get("params")
    if isinstance(params, dict):
        kwargs = {**kwargs, **params}
    raw = kwargs.get("kwargs")
    if isinstance(raw, dict):
        kwargs = {**kwargs, **raw}
    elif isinstance(raw, str):
        try:
            extra = json.loads(raw or "{}")
            if isinstance(extra, dict):
                kwargs = {**kwargs, **extra}
        except Exception:
            pass

    if "code" not in kwargs or kwargs.get("code") is None:
        kwargs["code"] = kwargs.get("Code") or kwargs.get("stock_code") or kwargs.get("symbol")

    # Backward-compatible aliases
    if kwargs.get("total_shares") is None:
        kwargs["total_shares"] = kwargs.get("total_quantity") or kwargs.get("quantity") or kwargs.get("qty")
    if kwargs.get("duration") is None:
        kwargs["duration"] = kwargs.get("duration_minutes") or kwargs.get("minutes")
    if kwargs.get("slices") is None and kwargs.get("slice_count") is not None:
        kwargs["slices"] = kwargs.get("slice_count")

    return kwargs

def _to_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "on"}
    return bool(value)

def _normalize_threshold_overrides(raw: dict) -> dict:
    normalized: dict[str, Any] = {}
    for key in _SOFT_GATE_THRESHOLD_KEYS:
        if raw.get(key) is None:
            continue
        if key == "max_cost_ratio":
            normalized[key] = float(raw.get(key))
        else:
            normalized[key] = int(raw.get(key))
    return normalized

def _warnings_by_severity(warnings: list[dict]) -> dict[str, int]:
    result = {"low": 0, "medium": 0, "high": 0}
    for warning in warnings:
        sev = str(warning.get("severity") or "").strip().lower()
        if sev in result:
            result[sev] += 1
    return result

def _warnings_by_profile(warnings: list[dict]) -> dict[str, int]:
    result: dict[str, int] = {}
    for warning in warnings:
        profile = str(warning.get("threshold_profile") or "unknown").strip().lower() or "unknown"
        result[profile] = result.get(profile, 0) + 1
    return result

def _merge_counter(target: dict[str, int], source: dict[str, int]) -> None:
    for key, value in source.items():
        target[key] = target.get(key, 0) + int(value)

def _profile_distribution(tasks: list[dict]) -> dict[str, int]:
    dist: dict[str, int] = {"unknown": 0}
    for name in _SOFT_GATE_PROFILES.keys():
        dist[name] = 0
    for task in tasks:
        soft_gate = task.get("soft_gate") if isinstance(task.get("soft_gate"), dict) else {}
        profile = str(soft_gate.get("profile") or "unknown").strip().lower() or "unknown"
        if profile not in dist:
            dist[profile] = 0
        dist[profile] += 1
    return dist

def _set_config_result() -> dict:
    return {"soft_gate_config": _soft_gate_config_view()}

def _set_runtime_code_profiles(code_profiles: dict, merge: bool) -> tuple[bool, str | None]:
    current = _SOFT_GATE_RUNTIME_CONFIG.get("code_profiles", {}) if merge else {}
    if not isinstance(current, dict):
        current = {}
    new_map: dict[str, str] = dict(current)
    for code_key, profile_val in code_profiles.items():
        code = str(code_key or "").strip()
        if not code:
            continue
        if profile_val is None:
            new_map.pop(code, None)
            continue
        profile = str(profile_val).strip().lower()
        if profile not in _SOFT_GATE_PROFILES:
            return False, f"invalid profile for code {code}: {profile}"
        new_map[code] = profile
    _SOFT_GATE_RUNTIME_CONFIG["code_profiles"] = new_map
    return True, None

def _remove_runtime_code_profiles(codes: list[Any]) -> None:
    current = _SOFT_GATE_RUNTIME_CONFIG.get("code_profiles", {})
    if not isinstance(current, dict):
        current = {}
    new_map = dict(current)
    for item in codes:
        code = str(item or "").strip()
        if code:
            new_map.pop(code, None)
    _SOFT_GATE_RUNTIME_CONFIG["code_profiles"] = new_map

def _apply_default_threshold_overrides(overrides: dict, merge: bool) -> None:
    normalized = _normalize_threshold_overrides(overrides)
    if merge:
        current = _SOFT_GATE_RUNTIME_CONFIG.get("default_threshold_overrides", {})
        if not isinstance(current, dict):
            current = {}
        merged = dict(current)
        merged.update(normalized)
        _SOFT_GATE_RUNTIME_CONFIG["default_threshold_overrides"] = merged
    else:
        _SOFT_GATE_RUNTIME_CONFIG["default_threshold_overrides"] = normalized

def _remove_default_threshold_keys(keys: list[Any]) -> None:
    current = _SOFT_GATE_RUNTIME_CONFIG.get("default_threshold_overrides", {})
    if not isinstance(current, dict):
        current = {}
    new_map = dict(current)
    for item in keys:
        key = str(item or "").strip()
        if key in _SOFT_GATE_THRESHOLD_KEYS:
            new_map.pop(key, None)
    _SOFT_GATE_RUNTIME_CONFIG["default_threshold_overrides"] = new_map

def _set_config_impl(kwargs: dict) -> tuple[bool, str | None]:
    default_profile = kwargs.get("default_profile")
    if default_profile is not None:
        profile = str(default_profile).strip().lower()
        if profile not in _SOFT_GATE_PROFILES:
            return False, "default_profile must be one of conservative/balanced/aggressive"
        _SOFT_GATE_RUNTIME_CONFIG["default_profile"] = profile

    default_threshold_overrides = kwargs.get("default_threshold_overrides")
    merge_default_threshold_overrides = _to_bool(kwargs.get("merge_default_threshold_overrides"), default=False)
    if default_threshold_overrides is not None:
        if not isinstance(default_threshold_overrides, dict):
            return False, "default_threshold_overrides must be a dict"
        try:
            _apply_default_threshold_overrides(default_threshold_overrides, merge=merge_default_threshold_overrides)
        except Exception:
            return False, "default_threshold_overrides contains invalid threshold value"

    remove_default_threshold_keys = kwargs.get("remove_default_threshold_keys")
    if remove_default_threshold_keys is not None:
        if not isinstance(remove_default_threshold_keys, list):
            return False, "remove_default_threshold_keys must be a list"
        _remove_default_threshold_keys(remove_default_threshold_keys)

    code_profiles = kwargs.get("code_profiles")
    merge_code_profiles = _to_bool(kwargs.get("merge_code_profiles"), default=False)
    if code_profiles is not None:
        if not isinstance(code_profiles, dict):
            return False, "code_profiles must be a dict"
        ok_flag, err = _set_runtime_code_profiles(code_profiles, merge=merge_code_profiles)
        if not ok_flag:
            return False, err

    remove_code_profiles = kwargs.get("remove_code_profiles")
    if remove_code_profiles is not None:
        if not isinstance(remove_code_profiles, list):
            return False, "remove_code_profiles must be a list"
        _remove_runtime_code_profiles(remove_code_profiles)

    return True, None

def _summary_aggregates(tasks: list[dict]) -> dict:
    by_status: dict[str, int] = {}
    estimated_total_cost = 0.0
    warning_count = 0
    high_severity_task_count = 0
    soft_gate_profile_distribution = _profile_distribution(tasks)
    warnings_by_profile: dict[str, int] = {}
    warnings_by_severity: dict[str, int] = {"low": 0, "medium": 0, "high": 0}

    for task in tasks:
        status = str(task.get("status", "unknown"))
        by_status[status] = by_status.get(status, 0) + 1
        estimated_total_cost += float(task.get("cost_model", {}).get("estimated", {}).get("total", 0.0))

        sg = task.get("soft_gate") if isinstance(task.get("soft_gate"), dict) else {}
        warnings = task.get("pretrade_warnings") if isinstance(task.get("pretrade_warnings"), list) else []
        warning_count += int(sg.get("warning_count", len(warnings)) or 0)
        if bool(sg.get("has_high_severity", False)):
            high_severity_task_count += 1

        _merge_counter(warnings_by_profile, _warnings_by_profile(warnings))
        _merge_counter(warnings_by_severity, _warnings_by_severity(warnings))

    return {
        "total_tasks": len(tasks),
        "by_status": by_status,
        "estimated_total_cost": float(estimated_total_cost),
        "warning_count": int(warning_count),
        "high_severity_task_count": int(high_severity_task_count),
        "soft_gate_profile_distribution": soft_gate_profile_distribution,
        "warnings_by_profile": warnings_by_profile,
        "warnings_by_severity": warnings_by_severity,
    }

def _append_event(task: dict, status: str, note: str, payload: dict | None = None) -> None:
    event = {
        "at": _now_iso(),
        "status": status,
        "note": note,
    }
    if payload:
        event["payload"] = payload
    task.setdefault("lifecycle", []).append(event)
    _touch_task(task)

def _refresh_task_status(task: dict) -> bool:
    """Advance status by elapsed time to support lifecycle visibility."""
    status = task.get("status")
    if status in {"completed", "failed"}:
        return False

    created_at = task.get("created_at")
    if not created_at:
        return False

    created_dt = _parse_iso_timestamp(created_at)
    if created_dt == datetime.min.replace(tzinfo=timezone.utc):
        return False

    elapsed = (datetime.now(timezone.utc) - created_dt).total_seconds()
    duration_minutes = int(task.get("plan", {}).get("duration_minutes", 0) or 0)
    changed = False

    if status == "queued" and elapsed >= 1:
        task["status"] = "executing"
        _append_event(task, "executing", "task moved from queue to execution")
        status = "executing"
        changed = True

    if status == "executing" and duration_minutes > 0 and elapsed >= duration_minutes * 60:
        task["status"] = "completed"
        task["completed_at"] = _now_iso()
        _append_event(task, "completed", "task execution completed")
        changed = True

    return changed

def _build_cost_model(kwargs: dict, total_shares: int) -> dict:
    """Build execution cost model via unified cost_model service."""
    from ...services.cost_model import build_cost_model

    reference_price = float(kwargs.get("reference_price", 0.0) or 0.0)
    notional = float(total_shares) * reference_price
    return build_cost_model(
        kwargs, notional=notional, default_mode="execution",
        reference_price_fallback=reference_price,
    )

def _get_cached_realtime_quote(code: str, now_monotonic: float, ttl_seconds: float) -> dict[str, Any] | None:
    if ttl_seconds <= 0:
        return None
    cached = _REALTIME_QUOTE_CACHE.get(code)
    if not cached:
        return None
    cached_at, payload = cached
    if now_monotonic - cached_at > ttl_seconds:
        return None
    return payload

def _load_realtime_quote_with_timeout(code: str) -> dict[str, Any] | None:
    timeout_seconds = max(
        0.0,
        float(os.getenv("EXECUTION_MANAGER_REALTIME_ENRICH_TIMEOUT_MS", "1200") or "1200") / 1000.0,
    )
    cache_ttl_seconds = max(
        0.0,
        float(os.getenv("EXECUTION_MANAGER_REALTIME_QUOTE_CACHE_TTL_MS", "15000") or "15000") / 1000.0,
    )
    cooldown_seconds = max(
        0.0,
        float(os.getenv("EXECUTION_MANAGER_REALTIME_ENRICH_COOLDOWN_MS", "30000") or "30000") / 1000.0,
    )
    now_monotonic = time.monotonic()
    cached = _get_cached_realtime_quote(code, now_monotonic, cache_ttl_seconds)
    if cached is not None:
        return cached
    if timeout_seconds <= 0:
        return None

    skip_until = _REALTIME_QUOTE_SKIP_UNTIL.get(code, 0.0)
    if skip_until > now_monotonic:
        return None

    try:
        future = _get_realtime_quote_executor().submit(
            get_quote_snapshot_sync,
            code,
            fallback_mode=FALLBACK_DB_ONLY,
            timeout=timeout_seconds,
        )
        access = future.result(timeout=timeout_seconds)
        quote = access.get("data") if isinstance(access, dict) and access.get("success") else None
        if isinstance(quote, dict) and quote:
            _REALTIME_QUOTE_CACHE[code] = (time.monotonic(), quote)
            _REALTIME_QUOTE_SKIP_UNTIL.pop(code, None)
            return quote
        return None
    except FutureTimeoutError:
        _REALTIME_QUOTE_SKIP_UNTIL[code] = time.monotonic() + cooldown_seconds
        logger.warning("execution_manager realtime enrichment timed out for %s after %.3fs", code, timeout_seconds)
        return None
    except Exception as exc:
        _REALTIME_QUOTE_SKIP_UNTIL[code] = time.monotonic() + cooldown_seconds
        logger.warning("execution_manager realtime enrichment failed for %s: %s", code, exc)
        return None

def _enrich_kwargs_with_realtime(code: str, kwargs: dict) -> dict:
    """从实时行情自动填充 reference_price / avg_minute_volume（P1-c）。"""
    if kwargs.get("reference_price"):
        return kwargs
    try:
        quote = _load_realtime_quote_with_timeout(code)
        if not quote:
            return kwargs
        if not kwargs.get("reference_price"):
            price = quote.get("price")
            if price and float(price) > 0:
                kwargs["reference_price"] = float(price)
        if not kwargs.get("avg_minute_volume"):
            vol = quote.get("volume")
            if vol and float(vol) > 0:
                kwargs["avg_minute_volume"] = float(vol) / 240.0
                # 标记为自动填充，避免将实时估算值直接用于高等级参与率风控判定（防止噪声误报）
                kwargs["__auto_avg_minute_volume"] = True
    except Exception:
        pass
    return kwargs

def _build_soft_gate_warnings(
    kwargs: dict,
    total_shares: int,
    duration: int,
    slices: int,
    cost_model: dict,
) -> tuple[list[dict], dict]:
    """执行前置闸门（P1/P2 软校验）：仅告警不阻断，支持 profile 阈值策略。"""
    warnings: list[dict] = []
    thresholds = _resolve_soft_gate_thresholds(kwargs)

    reference_price = float(kwargs.get("reference_price", 0.0) or 0.0)
    if reference_price <= 0:
        warnings.append(
            {
                "type": "price_missing",
                "severity": "medium",
                "message": "reference_price 缺失或无效，成本估算可能偏差较大",
                "suggestion": "传入 reference_price 以提升费用与冲击成本估算准确性",
                "threshold_profile": thresholds["profile"],
            }
        )

    max_order_shares = int(thresholds["max_order_shares"])
    if total_shares > max_order_shares:
        warnings.append(
            {
                "type": "order_size_large",
                "severity": "high",
                "message": f"委托总量 {total_shares} 超过软阈值 {max_order_shares}",
                "suggestion": "考虑拆分为多批次执行或降低单次下单规模",
                "threshold_profile": thresholds["profile"],
            }
        )

    max_slice_shares = int(thresholds["max_slice_shares"])
    shares_per_slice = max(1, total_shares // max(1, slices))
    if shares_per_slice > max_slice_shares:
        warnings.append(
            {
                "type": "slice_too_large",
                "severity": "medium",
                "message": f"单片委托量 {shares_per_slice} 超过软阈值 {max_slice_shares}",
                "suggestion": "提高 slices 或拉长 duration_minutes，降低瞬时冲击",
                "threshold_profile": thresholds["profile"],
            }
        )

    min_duration_minutes = int(thresholds["min_duration_minutes"])
    if duration < min_duration_minutes:
        warnings.append(
            {
                "type": "duration_too_short",
                "severity": "medium",
                "message": f"执行时长 {duration} 分钟低于建议阈值 {min_duration_minutes} 分钟",
                "suggestion": "适当增加执行时长，平滑成交路径",
                "threshold_profile": thresholds["profile"],
            }
        )

    estimated = cost_model.get("estimated", {}) if isinstance(cost_model, dict) else {}
    notional = float(estimated.get("notional", 0.0) or 0.0)
    total_cost = float(estimated.get("total", 0.0) or 0.0)
    cost_ratio = (total_cost / notional) if notional > 0 else 0.0
    max_cost_ratio = float(thresholds["max_cost_ratio"])
    if notional > 0 and cost_ratio > max_cost_ratio:
        warnings.append(
            {
                "type": "cost_ratio_high",
                "severity": "medium",
                "message": f"预计总成本占比 {cost_ratio:.4%} 超过软阈值 {max_cost_ratio:.2%}",
                "suggestion": "可尝试降低冲击参数、增加执行时长或改用更细粒度切片",
                "threshold_profile": thresholds["profile"],
            }
        )

    market_session = str(kwargs.get("market_session") or "continuous").strip().lower()
    if market_session and market_session not in {"continuous", "normal", "intraday"}:
        warnings.append(
            {
                "type": "market_session_risk",
                "severity": "medium",
                "message": f"当前执行时段标识为 {market_session}，可能存在流动性与冲击成本风险",
                "suggestion": "优先在连续竞价主时段执行，或提高切片数量并延长执行时长",
                "threshold_profile": thresholds["profile"],
            }
        )

    avg_minute_volume_raw = kwargs.get("avg_minute_volume")
    auto_avg_minute_volume = bool(kwargs.get("__auto_avg_minute_volume", False))
    if avg_minute_volume_raw is not None:
        try:
            avg_minute_volume = float(avg_minute_volume_raw)
        except Exception:
            avg_minute_volume = 0.0
        if avg_minute_volume > 0:
            max_participation_rate = float(kwargs.get("max_participation_rate", 0.2) or 0.2)
            participation_rate = float(shares_per_slice) / avg_minute_volume
            if participation_rate > max_participation_rate:
                # 自动填充的成交量为估算值，避免直接触发高严重级别误报
                if auto_avg_minute_volume:
                    severity = "medium"
                else:
                    severity = "high" if participation_rate > max_participation_rate * 1.5 else "medium"
                warnings.append(
                    {
                        "type": "participation_rate_high",
                        "severity": severity,
                        "message": (
                            f"单片参与率 {participation_rate:.2%} 超过阈值 {max_participation_rate:.2%} "
                            f"(avg_minute_volume={int(avg_minute_volume)})"
                        ),
                        "suggestion": "降低单片下单量或延长执行时长，控制单位时间参与率",
                        "threshold_profile": thresholds["profile"],
                    }
                )

    top_of_book_volume_raw = kwargs.get("top_of_book_volume")
    if top_of_book_volume_raw is not None:
        try:
            top_of_book_volume = float(top_of_book_volume_raw)
        except Exception:
            top_of_book_volume = 0.0
        if top_of_book_volume > 0:
            max_top_book_ratio = float(kwargs.get("max_top_book_ratio", 0.3) or 0.3)
            top_book_ratio = float(shares_per_slice) / top_of_book_volume
            if top_book_ratio > max_top_book_ratio:
                severity = "high" if top_book_ratio > max_top_book_ratio * 2 else "medium"
                warnings.append(
                    {
                        "type": "top_book_impact_high",
                        "severity": severity,
                        "message": (
                            f"单片/盘口一档量比值 {top_book_ratio:.2f} 超过阈值 {max_top_book_ratio:.2f} "
                            f"(top_of_book_volume={int(top_of_book_volume)})"
                        ),
                        "suggestion": "降低单片委托量、增加切片，或等待盘口深度改善后执行",
                        "threshold_profile": thresholds["profile"],
                    }
                )

    return warnings, thresholds

def _run_pretrade_gate(
    code: str,
    direction: str,
    total_shares: int,
    kwargs: dict,
    soft_warnings: list[dict],
) -> dict:
    """执行前置闸门（硬闸门 + 软闸门合并）。

    硬闸门：调用 compliance_manager.evaluate_order_compliance，违规则阻断。
    软闸门：合并已有 soft_gate warnings，仅告警不阻断。
    审计：通过 risk_guard.audit_event 记录闸门决策。
    """
    price_raw = kwargs.get("reference_price") or kwargs.get("price")
    compliance = evaluate_order_compliance(code, direction, total_shares, price_raw)

    # 将合规 warnings 转为统一格式并合并到 soft_warnings
    for cw in compliance.get("warnings", []):
        soft_warnings.append({
            "type": "compliance_advisory",
            "severity": "low",
            "message": cw,
            "suggestion": "",
            "source": "compliance_manager",
        })

    # 执行管理语义：可通过拆单化解的限制类违规（数量/金额/买入手数）降级为软告警，不阻断任务创建
    violations = [str(v) for v in (compliance.get("violations", []) or [])]
    soft_violation_patterns = ("单笔数量超限", "单笔金额超限", "买入数量必须为")
    soft_violations: list[str] = []
    hard_violations: list[str] = []
    for v in violations:
        if any(p in v for p in soft_violation_patterns):
            soft_violations.append(v)
        else:
            hard_violations.append(v)

    for v in soft_violations:
        sev = "high" if ("单笔数量超限" in v or "单笔金额超限" in v) else "medium"
        soft_warnings.append({
            "type": "compliance_soft_limit",
            "severity": sev,
            "message": v,
            "suggestion": "建议通过拆单、延长执行时长或分批执行化解该限制",
            "source": "compliance_manager",
        })

    compliance_blocked = len(hard_violations) > 0

    gate_result = {
        "compliance_passed": not compliance_blocked,
        "compliance_blocked": compliance_blocked,
        "compliance_violations": hard_violations,
        "compliance_soft_violations": soft_violations,
        "compliance_checks": compliance.get("checks", {}),
        "order_amount": compliance.get("order_amount"),
    }

    # 审计记录
    audit_event(
        action=f"pretrade_gate:{direction}",
        params={"code": code, "direction": direction, "total_shares": total_shares,
                "price": price_raw},
        result={"compliance_passed": not compliance_blocked,
                "violations": hard_violations,
                "soft_warning_count": len(soft_warnings)},
        reason="blocked" if compliance_blocked else "passed",
    )

    return gate_result

def _task_brief(task: dict) -> dict:
    soft_gate = task.get("soft_gate") if isinstance(task.get("soft_gate"), dict) else {}
    warnings = task.get("pretrade_warnings") if isinstance(task.get("pretrade_warnings"), list) else []
    return {
        "task_id": task.get("task_id"),
        "artifact_id": task.get("artifact_id"),
        "algorithm": task.get("algorithm"),
        "code": task.get("code"),
        "status": task.get("status"),
        "created_at": task.get("created_at"),
        "total_shares": task.get("total_shares"),
        "duration_minutes": task.get("plan", {}).get("duration_minutes"),
        "soft_gate_profile": str(soft_gate.get("profile", "unknown") or "unknown"),
        "warning_count": int(soft_gate.get("warning_count", len(warnings)) or 0),
        "has_high_severity": bool(soft_gate.get("has_high_severity", False)),
    }

def _create_task(algorithm: str, code: str, total_shares: int, duration: int, slices: int, kwargs: dict) -> dict:
    task_id = f"exec_{uuid.uuid4().hex[:12]}"
    artifact_id = str(kwargs.get("artifact_id") or "").strip() or None
    cost_model = _build_cost_model(kwargs, total_shares)

    shares_per_slice = total_shares // slices if slices > 0 else total_shares
    remainder = total_shares - shares_per_slice * slices
    interval = max(1, duration // max(1, slices))

    task = {
        "task_id": task_id,
        "artifact_id": artifact_id,
        "algorithm": algorithm,
        "code": code,
        "status": "queued",
        "created_at": _now_iso(),
        "updated_at": _now_iso(),
        "total_shares": int(total_shares),
        "total_quantity": int(total_shares),
        "plan": {
            "duration_minutes": int(duration),
            "duration": int(duration),
            "slices": int(slices),
            "shares_per_slice": int(shares_per_slice),
            "remainder_shares": int(remainder),
            "interval_minutes": int(interval),
        },
        "cost_model": cost_model,
        "lifecycle": [],
    }
    _append_event(task, "queued", "task created and queued")
    _EXECUTION_TASKS[task_id] = task
    return task
