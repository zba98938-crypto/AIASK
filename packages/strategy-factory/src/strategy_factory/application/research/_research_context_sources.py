"""Context-loading helpers for factor research builder inputs."""

from __future__ import annotations

import asyncio
from datetime import date
from typing import Any, List, Optional, Tuple

from ...infrastructure.mcp_services import get_quant_manager_callable
from ..runtime import _call_optional_async


async def load_factor_history_meta(
    builder_cls,
    db,
    factor_names: List[str],
) -> Tuple[dict[str, dict[str, Any]], Optional[date]]:
    """优化 1：并发加载所有因子 IC 历史（原为串行）。"""
    history_meta: dict[str, dict[str, Any]] = {}
    latest_dates: List[date] = []
    unique_factor_names = list(
        dict.fromkeys(
            [str(item or "").strip() for item in factor_names if str(item or "").strip()]
        )
    )
    if not unique_factor_names:
        return history_meta, None

    async def _load_one(factor_name: str) -> Tuple[str, dict[str, Any]]:
        # P2-1: IC period 错配修复。因子挖掘 strict 按 horizon_days=10 写 IC
        # (factor_validation_pipeline.py:421 period="10"),原硬编码读 "20" → 挖出因子 IC
        # 永远查不到 → factor_ic.get(name)=0 垫底选不进策略。改为按优先级尝试多 horizon。
        rows = []
        for _period in ("10", "20"):
            _candidate = await _call_optional_async(
                db,
                "get_factor_ic_history",
                factor_name,
                _period,
                builder_cls.HISTORY_LIMIT,
                default=[],
            )
            if isinstance(_candidate, list) and _candidate:
                rows = _candidate
                break
        if not isinstance(rows, list):
            rows = []
        meta = builder_cls._history_summary(rows)
        return factor_name, meta

    # 并发加载所有因子历史
    results = await asyncio.gather(
        *[_load_one(name) for name in unique_factor_names],
        return_exceptions=True,
    )

    for item in results:
        if isinstance(item, BaseException):
            continue
        factor_name, meta = item
        if meta.get("history_count"):
            history_meta[factor_name] = meta
        latest_date = builder_cls._parse_date(meta.get("latest_ic_date"))
        if latest_date is not None:
            latest_dates.append(latest_date)

    latest_factor_date = max(latest_dates) if latest_dates else None
    return history_meta, latest_factor_date


async def load_governed_candidate_pool(
    builder_cls,
    snapshot: dict[str, Any],
    *,
    quant_manager_provider=get_quant_manager_callable,
) -> dict[str, Any]:
    quant_manager = None
    try:
        quant_manager = quant_manager_provider()
    except Exception:
        quant_manager = None
    if quant_manager is None:
        return {"available": False, "reason": "quant_manager_unavailable"}

    candidate_codes = builder_cls._normalize_codes(snapshot.get("candidate_codes"))
    kwargs: dict[str, Any] = {"op": "active_pool", "limit": 80, "market_codes_only": True}
    if candidate_codes:
        kwargs["codes"] = candidate_codes
    try:
        result = await quant_manager(action="factor_candidate_registry", kwargs=kwargs)
    except Exception as exc:
        return {"available": False, "reason": f"factor_candidate_registry_failed:{exc}"}
    if not isinstance(result, dict) or not result.get("success"):
        return {
            "available": False,
            "reason": str(
                (result or {}).get("error")
                or (result or {}).get("message")
                or "active_pool_unavailable"
            ),
        }
    data = result.get("data") if isinstance(result.get("data"), dict) else {}
    active_pool = data.get("active_pool") if isinstance(data.get("active_pool"), dict) else {}
    summary = data.get("summary") if isinstance(data.get("summary"), dict) else {}
    if not active_pool:
        return {"available": False, "reason": "active_pool_empty", "summary": summary}
    return {
        "available": bool(active_pool.get("count")),
        "summary": summary,
        "active_pool": active_pool,
    }


async def load_model_registry_lineage(
    builder_cls,
    candidates: List[dict[str, Any]],
    *,
    quant_manager_provider=get_quant_manager_callable,
) -> dict[str, Any]:
    quant_manager = None
    try:
        quant_manager = quant_manager_provider()
    except Exception:
        quant_manager = None
    if quant_manager is None:
        return {"available": False, "reason": "quant_manager_unavailable"}

    validation_ids = list(
        dict.fromkeys(
            [
                str(item.get("source_validation_artifact_id") or item.get("artifact_id") or "").strip()
                for item in list(candidates or [])
                if str(item.get("source_validation_artifact_id") or item.get("artifact_id") or "").strip()
            ]
        )
    )
    generation_ids = list(
        dict.fromkeys(
            [
                str(item.get("source_generation_artifact_id") or "").strip()
                for item in list(candidates or [])
                if str(item.get("source_generation_artifact_id") or "").strip()
            ]
        )
    )
    if not validation_ids and not generation_ids:
        return {"available": False, "reason": "candidate_lineage_missing"}

    try:
        result = await quant_manager(
            action="model_registry",
            kwargs={
                "op": "lineage",
                "validation_artifact_ids": validation_ids,
                "generation_artifact_ids": generation_ids,
                "limit": max(10, len(validation_ids) * 4, len(generation_ids) * 4),
                "market_codes_only": True,
            },
        )
    except Exception as exc:
        return {"available": False, "reason": f"model_registry_lineage_failed:{exc}"}

    if not isinstance(result, dict) or not result.get("success"):
        return {
            "available": False,
            "reason": str(
                (result or {}).get("error")
                or (result or {}).get("message")
                or "model_registry_lineage_unavailable"
            ),
        }
    data = result.get("data") if isinstance(result.get("data"), dict) else {}
    items = [dict(item or {}) for item in list(data.get("items") or []) if isinstance(item, dict)]
    return {
        "available": True,
        "summary": dict(data.get("summary") or {}),
        "items": items,
        "by_validation_artifact_id": {
            str(item.get("validation_artifact_id") or "").strip(): item
            for item in items
            if str(item.get("validation_artifact_id") or "").strip()
        },
    }


def extract_candidate_codes(
    builder_cls,
    item: dict[str, Any],
) -> List[str]:
    codes: List[str] = []

    def _extend(value: Any) -> None:
        for code in builder_cls._normalize_codes(value):
            if code not in codes:
                codes.append(code)

    payload = dict(item or {})
    _extend(payload.get("codes"))
    _extend(payload.get("target_symbols"))
    _extend((payload.get("stock_pool") or {}).get("symbols"))
    _extend((payload.get("validation_params") or {}).get("codes"))
    _extend((payload.get("lineage") or {}).get("codes"))
    _extend((payload.get("candidate") or {}).get("codes"))
    source_symbol_summary = payload.get("source_symbol_summary")
    if isinstance(source_symbol_summary, dict):
        _extend(
            [
                source_symbol_summary.get("code"),
                source_symbol_summary.get("symbol"),
                source_symbol_summary.get("stock_code"),
            ]
        )
    return codes[:12]


def build_candidate_hint_map(
    builder_cls,
    candidates: List[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    hint_map: dict[str, dict[str, Any]] = {}
    for item in list(candidates or []):
        payload = dict(item or {})
        family_name = str(payload.get("family") or "").strip().lower()
        mapped_families = builder_cls._preferred_types_for_factor(family_name)
        families: List[str] = []
        if family_name and family_name in mapped_families:
            families.append(family_name)
        for strategy_type in mapped_families:
            lowered = str(strategy_type or "").strip().lower()
            if lowered and lowered not in families:
                families.append(lowered)
        if not families:
            continue
        hint_score = max(0.0, min(builder_cls._safe_float(payload.get("total_score")) / 100.0, 1.0))
        for code in extract_candidate_codes(builder_cls, payload):
            bucket = hint_map.setdefault(code, {"families": [], "scores": []})
            for family in families:
                if family not in bucket["families"]:
                    bucket["families"].append(family)
            bucket["scores"].append(hint_score)
    return hint_map
