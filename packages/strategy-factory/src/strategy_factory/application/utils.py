"""策略工厂共享工具。"""

from __future__ import annotations

from importlib import import_module
from typing import Optional

from ..domain.naming import _auto_name
from ..domain.targets import (
    _extract_target_codes_from_payload,
    _normalize_target_codes,
    _resolve_strategy_sample_codes,
    _update_strategy_status,
)
from .runtime import _call_optional_async, get_strategy_factory_package


_LAZY_EXPORT_MAP = {
    "_build_strategy_panels": (".panels", "_build_strategy_panels"),
    "_run_validation_report": (".panels", "_run_validation_report"),
    "_run_risk_report": (".panels", "_run_risk_report"),
}


def _extract_event_context(payload: Optional[dict], limit: int = 5) -> dict:
    item = dict(payload or {})
    evidence_bundle = dict(item.get("evidence_bundle") or {})
    target_symbols = _normalize_target_codes(
        [
            item.get("target_symbols"),
            item.get("stock_pool"),
            evidence_bundle.get("target_symbols"),
            (evidence_bundle.get("score_summary") or {}).get("top_symbols")
            if isinstance(evidence_bundle.get("score_summary"), dict)
            else None,
        ],
        limit=limit,
    )
    focus_industries = [
        str(value).strip()
        for value in list(item.get("focus_industries") or [])
        if str(value).strip()
    ][:3]
    selection_logic = [
        str(value).strip()
        for value in list(item.get("selection_logic") or [])
        if str(value).strip()
    ][:3]
    supporting_reasons = [
        str(value).strip()
        for value in list(evidence_bundle.get("supporting_reasons") or [])
        if str(value).strip()
    ][:4]
    context = {
        "task_id": item.get("task_id"),
        "task_key": item.get("task_key"),
        "task_source": item.get("task_source"),
        "event_id": item.get("event_id") or evidence_bundle.get("event_id"),
        "event_type": item.get("event_type") or evidence_bundle.get("event_type"),
        "event_name": evidence_bundle.get("event_name"),
        "event_summary": evidence_bundle.get("event_summary") or item.get("event_summary"),
        "theme": item.get("theme"),
        "theme_code": item.get("theme_code") or evidence_bundle.get("theme_code"),
        "theme_name": evidence_bundle.get("theme_name"),
        "direction": item.get("direction") or evidence_bundle.get("direction"),
        "horizon": item.get("horizon") or evidence_bundle.get("horizon"),
        "priority": item.get("priority"),
        "signal_count": evidence_bundle.get("signal_count"),
        "target_symbols": target_symbols,
        "focus_industries": focus_industries,
        "selection_logic": selection_logic,
        "supporting_reasons": supporting_reasons,
        "score_summary": dict(evidence_bundle.get("score_summary") or {}),
    }
    return {key: value for key, value in context.items() if value not in (None, [], {}, "")}


__all__ = [
    "get_strategy_factory_package",
    "_call_optional_async",
    "_auto_name",
    "_update_strategy_status",
    "_normalize_target_codes",
    "_extract_target_codes_from_payload",
    "_resolve_strategy_sample_codes",
    "_extract_event_context",
    "_build_strategy_panels",
    "_run_validation_report",
    "_run_risk_report",
]


def __getattr__(name: str):
    try:
        module_name, attr_name = _LAZY_EXPORT_MAP[name]
    except KeyError as exc:
        raise AttributeError(name) from exc
    module = import_module(module_name, __package__)
    return getattr(module, attr_name)


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))
