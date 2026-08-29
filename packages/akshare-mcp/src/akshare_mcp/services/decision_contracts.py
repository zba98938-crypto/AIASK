"""Unified decision contracts and payload builders."""

from __future__ import annotations

import asyncio
from typing import Any

from ..utils import now_iso, resolve_security_code
from .decision_context_builder import build_stock_context, build_user_context
from .decision_event_builder import build_event_context
from .decision_fusion import fuse_unified_decision
from .decision_pipeline_shared import merge_context_meta, unique_texts
from .decision_quant_builder import build_quant_context
from .decision_rule_gate import build_rule_gates

UNIFIED_DECISION_VERSION = "unified-decision.v1"


def _normalize_style(style: str | None) -> str:
    raw = str(style or "").strip().lower()
    if raw in {"aggressive", "balanced", "conservative"}:
        return raw
    return "balanced"


def _provenance_entry(source: str, dataset: str, timestamp: str | None = None) -> dict[str, Any]:
    return {
        "source": source,
        "dataset": dataset,
        "timestamp": timestamp or now_iso(),
    }


def _merge_warnings(*sections: dict[str, Any]) -> list[str]:
    return unique_texts(*(section.get("warnings", []) for section in sections))


async def _build_contexts(
    *,
    code: str,
    user_id: str | None,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]]:
    stock_context, quant_context, event_context, user_context = await asyncio.gather(
        build_stock_context(code),
        build_quant_context(code),
        build_event_context(code),
        build_user_context(user_id),
    )
    return stock_context, quant_context, event_context, user_context


def _build_summary_payload(
    *,
    code: str,
    investment_style: str,
    user_id: str | None,
    stock_context: dict[str, Any],
    quant_context: dict[str, Any],
    event_context: dict[str, Any],
    user_context: dict[str, Any],
    gate: dict[str, Any],
    fusion: dict[str, Any],
) -> dict[str, Any]:
    pipeline_meta = merge_context_meta(stock_context, quant_context, event_context, user_context)
    timestamp = now_iso()
    return {
        "version": UNIFIED_DECISION_VERSION,
        "scene": "unified_decision",
        "code": code,
        "name": stock_context.get("name") or "",
        "action": fusion["action"],
        "confidence": fusion["confidence"],
        "final_score": fusion["final_score"],
        "summary": fusion["summary"],
        "reasons": fusion["reasons"],
        "risks": fusion["risks"],
        "gate_flags": gate.get("flags", []),
        "veto_reason": fusion.get("veto_reason"),
        "position_signal": fusion.get("position_signal"),
        "data_provenance": [
            _provenance_entry("decision_context_builder", "investment_analysis+market_snapshot", stock_context.get("updated_at")),
            _provenance_entry("decision_quant_builder", "factor_profile+conditional_returns+oos", quant_context.get("updated_at")),
            _provenance_entry("decision_event_builder", "stock_text_signals+event_gate", event_context.get("updated_at")),
            _provenance_entry("decision_context_builder", "user_context", user_context.get("updated_at")),
        ],
        "compliance_notice": "本结果仅供研究与辅助决策，不构成投资建议。",
        "details_available": True,
        "details_hint": {
            "tool": "get_unified_decision_details",
            "args": {
                "code": code,
                "investment_style": investment_style,
                **({"user_id": user_id} if user_id else {}),
            },
        },
        "diagnostics": fusion.get("score_breakdown", {}),
        "weights": fusion.get("weights", {}),
        "raw_ai_action": fusion.get("raw_ai_action"),
        "raw_ai_output": fusion.get("raw_ai_output"),
        "recommended_horizon": fusion.get("recommended_horizon"),
        "warnings": _merge_warnings(stock_context, quant_context, event_context, user_context),
        "fallback_reason": pipeline_meta.get("fallback_reason"),
        "cached": pipeline_meta.get("cached", False),
        "updated_at": pipeline_meta.get("updated_at"),
        "data_quality": pipeline_meta.get("data_quality"),
        "timestamp": timestamp,
    }


async def _build_pipeline_payload(
    *,
    code: str,
    investment_style: str = "balanced",
    user_id: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    normalized_code = resolve_security_code(code)
    if not normalized_code:
        raise ValueError("需要提供股票代码（支持 code / stock_code / symbol / ticker）")

    style = _normalize_style(investment_style)
    stock_context, quant_context, event_context, user_context = await _build_contexts(
        code=normalized_code,
        user_id=user_id,
    )
    gate = build_rule_gates(
        code=normalized_code,
        investment_style=style,
        stock_context=stock_context,
        quant_context=quant_context,
        event_context=event_context,
        user_context=user_context,
    )
    fusion = fuse_unified_decision(
        stock_context=stock_context,
        quant_context=quant_context,
        event_context=event_context,
        user_context=user_context,
        gate=gate,
    )
    summary = _build_summary_payload(
        code=normalized_code,
        investment_style=style,
        user_id=user_id,
        stock_context=stock_context,
        quant_context=quant_context,
        event_context=event_context,
        user_context=user_context,
        gate=gate,
        fusion=fusion,
    )
    details = {
        "requested": {
            "code": normalized_code,
            "investment_style": style,
            "user_id": user_id,
        },
        "meta": {
            "updated_at": summary.get("updated_at"),
            "cached": summary.get("cached"),
            "fallback_reason": summary.get("fallback_reason"),
            "data_quality": summary.get("data_quality"),
        },
        "stock_context": stock_context,
        "quant_context": quant_context,
        "event_context": event_context,
        "user_context": user_context,
        "gate_result": gate,
        "fusion": fusion,
    }
    return summary, details


async def get_unified_decision_summary_payload(
    *,
    code: str,
    investment_style: str = "balanced",
    user_id: str | None = None,
) -> dict[str, Any]:
    summary, _ = await _build_pipeline_payload(
        code=code,
        investment_style=investment_style,
        user_id=user_id,
    )
    return summary


async def get_unified_decision_details_payload(
    *,
    code: str,
    investment_style: str = "balanced",
    user_id: str | None = None,
) -> dict[str, Any]:
    summary, details = await _build_pipeline_payload(
        code=code,
        investment_style=investment_style,
        user_id=user_id,
    )
    return {
        **summary,
        "details": details,
    }
