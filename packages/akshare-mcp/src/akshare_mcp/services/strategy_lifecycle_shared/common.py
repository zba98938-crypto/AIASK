"""Shared strategy lifecycle primitives used by both services and tools layers.

This module exists to break the circular dependency where services
(promotion_pipeline, incubation, runtime_control) imported from the
tools layer (tools.managers.strategy_manager).  Now both sides import
from this services-level module instead.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from typing import Any, Optional

from strategy_factory.api.constants import (
    DEPRECATION_THRESHOLDS,
    PROMOTION_THRESHOLDS,
    STRATEGY_FACTORY_CONFIDENCE_DIAGNOSTICS_ENABLED,
    STRATEGY_FACTORY_EXECUTION_AUDIT_ENABLED,
    STRATEGY_FACTORY_PROMOTION_CROSS_REGIME_ENABLED,
    STRATEGY_FACTORY_PROMOTION_CROSS_REGIME_MIN_N,
)

logger = logging.getLogger(__name__)

_EARLY_SIGNAL_STAGES = {"warmup", "observe"}
_EXECUTION_AUDIT_PROMOTION_BLOCKING_STAGES = {"candidate", "graduation_ready"}
_EARLY_STAGE_PROMOTION_MDD_TOLERANCE = 0.03
_TREND_EXECUTABLE_DSL_TYPES = {"ma_cross", "momentum", "volatility_breakout", "event_structure_breakout"}


def _confidence_diagnostics_enabled() -> bool:
    return bool(STRATEGY_FACTORY_CONFIDENCE_DIAGNOSTICS_ENABLED)


def _promotion_cross_regime_enabled() -> bool:
    return bool(STRATEGY_FACTORY_PROMOTION_CROSS_REGIME_ENABLED)


def evaluate_cross_regime_skill(
    hit_rate_by_regime: Optional[dict],
    *,
    min_n: int = int(STRATEGY_FACTORY_PROMOTION_CROSS_REGIME_MIN_N),
) -> dict:
    """INVERT-DESIGN P3 改动B：评估"跨主要 regime 是否都有正 skill"。

    输入为 forward_verifier 的 hit_rate_by_regime（{dimension: {label: {skill_lcb, n, ...}}}）。
    仅对达到 ``min_n`` 样本量的 regime 标签判定；任一达标标签 skill_lcb<=0 即视为未通过。
    样本不足时不阻断（passed=True, evaluated=False），交由全局 skill_lcb 把关。
    """
    source = dict(hit_rate_by_regime or {})
    evaluated_labels: list[str] = []
    negative_labels: list[str] = []
    for dimension, buckets in source.items():
        for label, stats in dict(buckets or {}).items():
            payload = dict(stats or {})
            try:
                n = int(payload.get("n") or payload.get("effective_n") or 0)
            except (TypeError, ValueError):
                n = 0
            if n < int(min_n):
                continue
            try:
                skill_lcb = float(payload.get("skill_lcb") or 0.0)
            except (TypeError, ValueError):
                skill_lcb = 0.0
            tag = f"{dimension}:{label}"
            evaluated_labels.append(tag)
            if skill_lcb <= 0.0:
                negative_labels.append(tag)
    evaluated = bool(evaluated_labels)
    passed = (not evaluated) or (not negative_labels)
    return {
        "enabled": _promotion_cross_regime_enabled(),
        "evaluated": evaluated,
        "passed": passed,
        "min_n": int(min_n),
        "evaluated_labels": evaluated_labels,
        "negative_labels": negative_labels,
    }


def _execution_audit_enabled() -> bool:
    return bool(STRATEGY_FACTORY_EXECUTION_AUDIT_ENABLED)

# ── Lifecycle state machine ──────────────────────────────────────────────────

LIFECYCLE_TRANSITIONS: dict[str, list[str]] = {
    "draft": ["submitted"],
    "submitted": ["incubating", "rejected"],
    "rejected": ["draft"],
    "incubating": ["listed", "deprecated", "suspended"],
    "listed": ["deprecated", "suspended", "archived"],
    "suspended": ["listed", "deprecated", "incubating"],
    "deprecated": [],
    "published": ["deprecated", "suspended", "archived", "listed"],
    "archived": [],
}


def normalize_status_alias(status: str | None) -> str:
    normalized = str(status or "").strip().lower()
    return "listed" if normalized == "published" else normalized


def validate_transition(current: str, target: str) -> bool:
    current_normalized = normalize_status_alias(current)
    target_normalized = normalize_status_alias(target)
    return target_normalized in LIFECYCLE_TRANSITIONS.get(current_normalized, [])


async def update_status(db, strategy_id: str, status: str, **kwargs) -> None:
    normalized = normalize_status_alias(status)
    try:
        await db.update_strategy_status(strategy_id, normalized, **kwargs)
    except TypeError:
        await db.update_strategy_status(strategy_id, normalized)


# ── Quality report helpers ───────────────────────────────────────────────────

def metric_bucket_value(metric: Optional[dict], key: int) -> Optional[float]:
    if not metric:
        return None
    value = metric.get(key)
    if value is None:
        value = metric.get(str(key))
    return None if value is None else float(value)


def _quality_report_field(
    quality_report: Optional[dict],
    quality_gate: Optional[dict],
    summary: Optional[dict],
    key: str,
) -> Any:
    for payload in (dict(quality_report or {}), dict(quality_gate or {}), dict(summary or {})):
        if key in payload and payload.get(key) is not None:
            return payload.get(key)
    return None


def _quality_report_bool(
    quality_report: Optional[dict],
    quality_gate: Optional[dict],
    summary: Optional[dict],
    key: str,
) -> Optional[bool]:
    sentinel = object()
    value = sentinel
    for payload in (dict(quality_report or {}), dict(quality_gate or {}), dict(summary or {})):
        if key in payload and payload.get(key) is not None:
            value = payload.get(key)
            break
    if value is sentinel:
        return None
    return bool(value)


def _safe_float(value: Any) -> Optional[float]:
    try:
        if value is None:
            return None
        return float(value)
    except Exception:
        return None


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        if value is None:
            return default
        return int(float(value))
    except Exception:
        return default


def _string(value: Any) -> str:
    return str(value or "").strip()


def _safe_boolish(value: Any, *, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    token = _string(value).lower()
    if token in {"1", "true", "yes", "on", "required", "must"}:
        return True
    if token in {"0", "false", "no", "off", "optional"}:
        return False
    return bool(default)


def _contract_version_stable(value: Any, explicit_flag: Any = None) -> bool:
    if explicit_flag is not None:
        return bool(explicit_flag)
    version = _string(value).lower()
    if not version:
        return False
    unstable_tokens = ("draft", "unstable", "experimental", "preview", "beta", "alpha")
    return not any(token in version for token in unstable_tokens)

