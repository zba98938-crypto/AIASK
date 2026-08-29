"""Shared helpers for the unified decision pipeline."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Iterable

from ..tools.data_quality import build_quality_meta, normalize_reason_list, parse_asof_time
from ..utils import now_iso


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def safe_float(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def safe_int(value: Any) -> int | None:
    try:
        if value is None or value == "":
            return None
        return int(float(value))
    except (TypeError, ValueError):
        return None


def unique_texts(*groups: Iterable[Any]) -> list[str]:
    results: list[str] = []
    seen: set[str] = set()
    for group in groups:
        for item in list(group or []):
            text = str(item or "").strip()
            if not text or text in seen:
                continue
            seen.add(text)
            results.append(text)
    return results


def latest_timestamp(*values: Any) -> str:
    latest: datetime | None = None
    for value in values:
        parsed = parse_asof_time(value)
        if parsed is None:
            continue
        if latest is None or parsed > latest:
            latest = parsed
    return latest.isoformat() if latest is not None else now_iso()


def build_context_meta(
    *,
    source: str,
    source_chain: Iterable[str],
    asof_value: Any = None,
    warnings: Iterable[Any] | None = None,
    fallback_reason: Any = None,
    missing_fields: Iterable[str] | None = None,
    degraded: bool = False,
    cached: bool = False,
    success: bool = True,
) -> dict[str, Any]:
    warning_list = unique_texts(warnings or [])
    fallback_list = normalize_reason_list(fallback_reason)
    quality = build_quality_meta(
        source=source,
        source_chain=list(source_chain or []),
        fallback_reason=fallback_list,
        asof_value=asof_value,
        missing_fields=list(missing_fields or []),
        degraded=degraded,
        success=success,
    )
    updated_at = quality.get("asof_time") or latest_timestamp(asof_value)
    return {
        "source_chain": [str(item).strip() for item in list(source_chain or []) if str(item).strip()],
        "warnings": warning_list,
        "fallback_reason": fallback_list or None,
        "cached": bool(cached),
        "updated_at": updated_at,
        "timestamp": now_iso(),
        "data_quality": quality,
    }


def merge_context_meta(*sections: dict[str, Any]) -> dict[str, Any]:
    warnings = unique_texts(*(section.get("warnings", []) for section in sections))
    fallback_reason = unique_texts(*(section.get("fallback_reason", []) or [] for section in sections))
    source_chain = unique_texts(*(section.get("source_chain", []) for section in sections))
    missing_fields = unique_texts(
        *(
            ((section.get("data_quality") or {}).get("missing_fields") or [])
            for section in sections
            if isinstance(section, dict)
        )
    )
    degraded = any(bool((section.get("data_quality") or {}).get("degraded")) for section in sections)
    cached = any(bool(section.get("cached")) for section in sections)
    updated_at = latest_timestamp(*(section.get("updated_at") for section in sections))
    quality = build_quality_meta(
        source="unified_decision_pipeline",
        source_chain=source_chain,
        fallback_reason=fallback_reason,
        asof_value=updated_at,
        missing_fields=missing_fields,
        degraded=degraded,
        success=True,
    )
    return {
        "warnings": warnings,
        "fallback_reason": fallback_reason or None,
        "cached": cached,
        "updated_at": updated_at,
        "data_quality": quality,
        "source_chain": source_chain,
    }
