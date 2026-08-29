"""Context assembly helpers for stock deep analysis."""

from __future__ import annotations

import asyncio
from typing import Any

from ...tools.finance import get_financials
from ..decision_context_builder import build_stock_context, build_user_context
from ..decision_contracts import get_unified_decision_summary_payload
from ..decision_event_builder import build_event_context
from ..decision_quant_builder import build_quant_context
from .shared import _response_data

async def _assemble_contexts(code: str, user_id: str | None) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]]:
    async def _wrap(coro, fallback: dict[str, Any]) -> dict[str, Any]:
        try:
            result = await coro
            return dict(result or {})
        except Exception as exc:
            payload = dict(fallback)
            payload.setdefault("warnings", []).append(f"{type(exc).__name__}: {exc}")
            payload.setdefault("fallback_reason", []).append(f"{type(exc).__name__}: {exc}")
            payload["degraded"] = True
            return payload

    stock_context, quant_context, event_context, user_context = await asyncio.gather(
        _wrap(build_stock_context(code), {"code": code, "score": None, "warnings": [], "fallback_reason": []}),
        _wrap(build_quant_context(code), {"code": code, "score": None, "warnings": [], "fallback_reason": []}),
        _wrap(build_event_context(code), {"code": code, "score": None, "warnings": [], "fallback_reason": []}),
        _wrap(build_user_context(user_id), {"user_id": user_id, "warnings": [], "fallback_reason": [], "degraded": True}),
    )
    return stock_context, quant_context, event_context, user_context


async def _safe_profile_payload(code: str) -> dict[str, Any]:
    try:
        from ...resources.stock_and_watchlist import build_stock_profile_resource_payload

        return await build_stock_profile_resource_payload(code)
    except Exception as exc:
        return {
            "uri": f"resource://stock/{code}/profile",
            "code": code,
            "found": False,
            "error": f"{type(exc).__name__}: {exc}",
            "stock": {"code": code, "name": ""},
            "realtime_quote": {},
        }


async def _safe_financial_payload(code: str) -> dict[str, Any]:
    try:
        result = await get_financials(code)
        return dict(result or {})
    except Exception as exc:
        return {
            "success": False,
            "error": f"{type(exc).__name__}: {exc}",
            "data": {},
        }


async def _safe_decision_summary(code: str, investment_style: str, user_id: str | None) -> dict[str, Any]:
    try:
        return await get_unified_decision_summary_payload(
            code=code,
            investment_style=investment_style,
            user_id=user_id,
        )
    except Exception as exc:
        return {
            "code": code,
            "action": "wait",
            "confidence": None,
            "summary": "统一决策摘要构建失败，已降级为等待确认。",
            "reasons": [],
            "risks": [f"{type(exc).__name__}: {exc}"],
            "fallback_reason": [f"{type(exc).__name__}: {exc}"],
        }
