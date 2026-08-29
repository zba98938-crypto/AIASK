"""Shared manager argument normalization and response metadata helpers."""

from __future__ import annotations

import json
import time
from datetime import datetime
from typing import Any, Iterable, Optional
from uuid import uuid4

from ..utils import fail, normalize_code, ok

DEFAULT_CODE_KEYS = ("code", "Code", "stock_code", "symbol", "ticker")
DEFAULT_LIST_KEYS = {
    "codes": ("Codes",),
}
LINEAGE_REFERENCE_KEYS = (
    "artifact_id",
    "source_artifact_id",
    "output_artifact_id",
    "dataset_id",
    "validation_run_id",
    "run_id",
    "parent_run_id",
    "source_run_id",
    "task_run_id",
    "experiment_id",
    "model_id",
    "strategy_id",
    "factor_candidate_id",
    "promotion_review_id",
    "review_id",
)
_HIGH_RISK_ACTION_TOKENS = ("submit_order", "cancel_order", "live_trade", "place_order")
_WRITE_ACTION_TOKENS = (
    "create",
    "add",
    "remove",
    "delete",
    "update",
    "set",
    "register",
    "rebuild",
    "run",
    "execute",
    "trigger",
    "publish",
    "submit",
    "cancel",
    "sync",
    "warmup",
    "ack",
    "retrain",
    "optimize",
    "write",
    "reorder",
)


def _merge_json_like_payload(payload: Any) -> dict[str, Any]:
    if isinstance(payload, dict):
        return dict(payload)
    if isinstance(payload, str):
        try:
            parsed = json.loads(payload or "{}")
        except Exception:
            return {}
        if isinstance(parsed, dict):
            return dict(parsed)
    return {}


def normalize_manager_kwargs(
    kwargs: Optional[dict[str, Any]],
    *,
    field_aliases: Optional[dict[str, Iterable[str]]] = None,
) -> dict[str, Any]:
    normalized = _merge_json_like_payload(kwargs)

    for key in ("params", "kwargs"):
        normalized.update(_merge_json_like_payload(normalized.get(key)))

    aliases = dict(DEFAULT_LIST_KEYS)
    if field_aliases:
        aliases.update({k: tuple(v) for k, v in field_aliases.items()})

    for canonical, candidates in aliases.items():
        if normalized.get(canonical) not in (None, "", []):
            continue
        for alias in candidates:
            value = normalized.get(alias)
            if value not in (None, "", []):
                normalized[canonical] = value
                break

    if "code" not in normalized or normalized.get("code") in (None, ""):
        for alias in DEFAULT_CODE_KEYS:
            value = normalized.get(alias)
            if value not in (None, ""):
                normalized["code"] = value
                break

    return normalized


def normalize_manager_payload(
    *,
    params: Any = None,
    kwargs: Any = None,
    code: str | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Unified entry point for Manager parameter normalization.

    Merge priority (later wins): kwargs → params → extra → explicit code.

    Accepts BFF legacy calls (kwargs as JSON string) and new structured
    MCP calls (params as dict) transparently.
    """
    merged: dict[str, Any] = {}

    # 1. merge kwargs (legacy compat: may be JSON string or dict)
    merged.update(_merge_json_like_payload(kwargs))

    # 2. merge params (structured MCP input)
    merged.update(_merge_json_like_payload(params))

    # 3. merge explicit extra fields
    if extra and isinstance(extra, dict):
        merged.update({key: value for key, value in extra.items() if value is not None})

    # 4. resolve code from explicit param or merged payload
    if code and isinstance(code, str) and code.strip():
        merged["code"] = code.strip()
    elif "code" not in merged or merged.get("code") in (None, ""):
        for alias in DEFAULT_CODE_KEYS:
            value = merged.get(alias)
            if value not in (None, ""):
                merged["code"] = value
                break

    # 5. apply standard alias resolution
    for canonical, candidates in DEFAULT_LIST_KEYS.items():
        if merged.get(canonical) not in (None, "", []):
            continue
        for alias in candidates:
            value = merged.get(alias)
            if value not in (None, "", []):
                merged[canonical] = value
                break

    return merged


def normalize_manager_code(
    code: Optional[str],
    kwargs: dict[str, Any],
    *,
    normalize: bool = False,
) -> tuple[Optional[str], dict[str, Any]]:
    merged = dict(kwargs or {})
    resolved = code or merged.get("code")
    if isinstance(resolved, str):
        resolved = resolved.strip() or None
    if resolved and normalize:
        resolved = normalize_code(resolved)
    merged["code"] = resolved
    return resolved, merged


def extract_common_meta(kwargs: Optional[dict[str, Any]], *, defaults: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    merged = dict(defaults or {})
    source = dict(kwargs or {})
    for key in (
        "as_of",
        "adjust",
        "price_source_policy",
        "explain",
        "strict_mode",
        "idempotency_key",
        "dataset_id",
        "run_id",
        "parent_run_id",
        "source_run_id",
        "validation_run_id",
        "artifact_id",
        "source_artifact_id",
        "output_artifact_id",
        "model_id",
        "strategy_id",
    ):
        if key in source:
            merged[key] = source.get(key)
    return merged


def _infer_side_effect_level(tool_name: str, action: str, *, default: str = "read_only") -> str:
    combined = f"{tool_name}:{action}".lower()
    if any(token in combined for token in _HIGH_RISK_ACTION_TOKENS):
        return "trade_risk"
    if any(token in combined for token in _WRITE_ACTION_TOKENS):
        if any(token in combined for token in ("submit", "cancel", "publish", "sync", "rebuild")):
            return "external_write"
        return "stateful"
    return default


def _infer_confirmation_policy(level: str) -> str:
    """Return confirmation policy based on side-effect level."""
    if level == "trade_risk":
        return "explicit_token_required"
    if level == "external_write":
        return "recommended"
    return "none"


def generate_audit_event_id(tool_name: str, action: str) -> str:
    """Generate a globally unique audit event ID for every tool invocation."""
    return f"audit:{tool_name}:{action}:{int(time.time() * 1000)}:{uuid4().hex[:8]}"


def build_side_effect_meta(
    *,
    tool_name: str,
    action: str,
    extra: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    payload = dict(extra or {})
    explicit = payload.get("side_effect")
    if isinstance(explicit, dict):
        side_effect = dict(explicit)
    else:
        side_effect = {}
    side_effect.setdefault("level", _infer_side_effect_level(tool_name, action))
    side_effect.setdefault("target", tool_name)
    side_effect.setdefault("confirmation_required", side_effect.get("level") == "trade_risk")
    side_effect.setdefault("confirmation_policy", _infer_confirmation_policy(side_effect["level"]))
    side_effect.setdefault("idempotent", side_effect.get("level") == "read_only")
    side_effect.setdefault("dry_run", bool(payload.get("dry_run", False)))
    return side_effect


def build_lineage_meta(extra: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    payload = dict(extra or {})
    explicit = payload.get("lineage")
    lineage = dict(explicit) if isinstance(explicit, dict) else {}
    for key in LINEAGE_REFERENCE_KEYS:
        value = payload.get(key)
        if value not in (None, "", []):
            lineage.setdefault(key, value)
    return lineage


def build_quality_contract_meta(
    *,
    source_chain: Optional[Iterable[str]] = None,
    extra: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    payload = dict(extra or {})
    explicit = payload.get("quality")
    if isinstance(explicit, dict):
        quality = dict(explicit)
    else:
        quality = {}
    quality.setdefault("status", "not_provided")
    chain = [str(item).strip() for item in list(source_chain or []) if str(item).strip()]
    if chain and "source_chain" not in quality:
        quality["source_chain"] = chain
    return quality


def build_manager_meta(
    *,
    tool_name: str,
    action: str,
    started_at: float,
    source_chain: Optional[Iterable[str]] = None,
    data_timestamp: Optional[str] = None,
    tool_version: str = "v1.1",
    cached: bool = False,
    extra: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    normalized_extra = dict(extra or {})
    audit_event_id = normalized_extra.pop("audit_event_id", None) or generate_audit_event_id(tool_name, action)
    meta = {
        "trace_id": f"{tool_name}:{action}:{int(time.time() * 1000)}",
        "audit_event_id": audit_event_id,
        "tool_version": tool_version,
        "data_timestamp": data_timestamp or datetime.now().strftime("%Y-%m-%d"),
        "source_chain": [str(item).strip() for item in list(source_chain or [tool_name]) if str(item).strip()],
        "cached": bool(cached),
        "latency_ms": int((time.perf_counter() - started_at) * 1000),
        "quality": build_quality_contract_meta(source_chain=source_chain, extra=normalized_extra),
        "side_effect": build_side_effect_meta(tool_name=tool_name, action=action, extra=normalized_extra),
        "lineage": build_lineage_meta(normalized_extra),
        "idempotency_key": normalized_extra.get("idempotency_key"),
        "degraded": bool(normalized_extra.get("degraded", False)),
    }
    if normalized_extra:
        normalized_extra.pop("quality", None)
        normalized_extra.pop("side_effect", None)
        normalized_extra.pop("lineage", None)
        normalized_extra.pop("dry_run", None)
        meta.update(normalized_extra)
    return meta


def ok_with_meta(
    data: Any,
    *,
    tool_name: str,
    action: str,
    started_at: float,
    source_chain: Optional[Iterable[str]] = None,
    data_timestamp: Optional[str] = None,
    tool_version: str = "v1.1",
    cached: bool = False,
    extra_meta: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    response = ok(data, cached=cached)
    extra_meta_payload = dict(extra_meta or {})
    extra_quality = extra_meta_payload.get("quality") if isinstance(extra_meta_payload.get("quality"), dict) else {}
    extra_degraded = bool(extra_meta_payload.get("degraded")) or bool(extra_quality.get("degraded"))
    extra_fallback = bool(extra_meta_payload.get("fallback_used")) or bool(extra_quality.get("fallback_used"))
    extra_fallback_reason = (
        extra_meta_payload.get("fallback_reason")
        if extra_meta_payload.get("fallback_reason") not in (None, "", [])
        else extra_quality.get("fallback_reason")
    )
    if isinstance(data, dict):
        data_degraded = bool(data.get("degraded"))
        data_fallback = bool(data.get("fallback_used")) or data.get("fallback_reason") not in (None, "", [])
        if data_degraded:
            response["degraded"] = True
        if data_fallback:
            response["fallback_used"] = True
        if data.get("fallback_reason") not in (None, "", []):
            response["fallback_reason"] = data.get("fallback_reason")
        if data.get("source_chain") and not source_chain:
            source_chain = data.get("source_chain")
        if data_degraded or data_fallback:
            extra_meta = dict(extra_meta or {})
            extra_meta["degraded"] = data_degraded or bool(extra_meta.get("degraded"))
            quality = dict(extra_meta.get("quality") or {})
            quality.setdefault("status", "degraded" if data_degraded else "available")
            quality.setdefault("fallback_used", data_fallback)
            if data.get("fallback_reason") not in (None, "", []):
                quality.setdefault("fallback_reason", data.get("fallback_reason"))
            flags = list(quality.get("quality_flags") or [])
            if data_degraded and "degraded" not in flags:
                flags.append("degraded")
            if data_fallback and "fallback" not in flags:
                flags.append("fallback")
            if flags:
                quality["quality_flags"] = flags
            extra_meta["quality"] = quality
    if extra_degraded:
        response["degraded"] = True
    if extra_fallback or extra_fallback_reason not in (None, "", []):
        response["fallback_used"] = True
    if extra_fallback_reason not in (None, "", []):
        response["fallback_reason"] = extra_fallback_reason
    extra_flags = []
    if isinstance(extra_quality, dict):
        extra_flags.extend(list(extra_quality.get("quality_flags") or []))
    if extra_degraded:
        extra_flags.append("degraded")
    if extra_fallback or extra_fallback_reason not in (None, "", []):
        extra_flags.append("fallback")
    if extra_flags:
        merged_flags = []
        for item in list(response.get("quality_flags") or []) + extra_flags:
            text = str(item or "").strip()
            if text and text not in merged_flags:
                merged_flags.append(text)
        response["quality_flags"] = merged_flags
    if isinstance(extra_quality, dict) and extra_quality.get("source_chain") and not source_chain:
        source_chain = extra_quality.get("source_chain")
    resolved_chain = [str(item).strip() for item in list(source_chain or []) if str(item).strip()]
    if resolved_chain:
        response["source_chain"] = resolved_chain
    response["meta"] = build_manager_meta(
        tool_name=tool_name,
        action=action,
        started_at=started_at,
        source_chain=source_chain,
        data_timestamp=data_timestamp,
        tool_version=tool_version,
        cached=cached,
        extra=extra_meta,
    )
    return response


def fail_with_meta(
    error: Any,
    *,
    tool_name: str,
    action: str,
    started_at: float,
    source_chain: Optional[Iterable[str]] = None,
    data_timestamp: Optional[str] = None,
    tool_version: str = "v1.1",
    cached: bool = False,
    extra_meta: Optional[dict[str, Any]] = None,
    error_code: Optional[str] = None,
) -> dict[str, Any]:
    resolved_chain = [str(item).strip() for item in list(source_chain or []) if str(item).strip()]
    extra_quality = {}
    if isinstance(extra_meta, dict) and isinstance(extra_meta.get("quality"), dict):
        extra_quality = dict(extra_meta.get("quality") or {})
    extra_flags = [
        str(item).strip()
        for item in list(extra_quality.get("quality_flags") or [])
        if str(item).strip()
    ]
    response = fail(
        error,
        error_code=error_code,
        source_chain=resolved_chain or None,
        quality_flags=extra_flags or None,
    )
    response["meta"] = build_manager_meta(
        tool_name=tool_name,
        action=action,
        started_at=started_at,
        source_chain=source_chain,
        data_timestamp=data_timestamp,
        tool_version=tool_version,
        cached=cached,
        extra=extra_meta,
    )
    return response


# T07: Standard error code constants
ERR_PARAM = "PARAM_ERROR"
ERR_NOT_FOUND = "NOT_FOUND"
ERR_AUTH = "AUTH_ERROR"
ERR_UPSTREAM = "UPSTREAM_ERROR"
ERR_INTERNAL = "INTERNAL_ERROR"
