"""Model registry and retrain governance handlers for quant_manager."""

from __future__ import annotations

import time
from copy import deepcopy
from datetime import datetime
from typing import Any, Callable
from uuid import uuid4

import numpy as np

from ...services import get_artifact_async, list_artifacts_async, register_artifact_async
from ...services import model_retrain_scheduler as model_retrain_scheduler_module
from ...services.rolling_model_registry import default_rolling_registry
from .quant_mgr_artifact_common import QuantManagerCall, _payload_from_artifact_row
from ._quant_mgr_model_registry_catalog_common import (
    _safe_text,
    _as_text_list,
    _now_iso,
    _normalize_model_registry_item,
)
from .quant_mgr_helpers import _as_code_list, _safe_float
from .quant_mgr_registry import _list_factor_candidate_registry_items

MODEL_REGISTRY_STRATEGY = "quant_model_registry"
MODEL_REGISTRY_VERSION = "p2.v1"
MODEL_FEEDBACK_STRATEGY = "quant_model_feedback"
MODEL_FEEDBACK_VERSION = "p2.v1"
MODEL_RETRAIN_PLAN_STRATEGY = "quant_model_retrain_plan"
MODEL_RETRAIN_PLAN_VERSION = "p2.v3"
MODEL_RETRAIN_RUN_STRATEGY = "quant_model_retrain_run"
MODEL_RETRAIN_RUN_VERSION = "p2.v3"

DEFAULT_STABILITY_FLOOR = 0.35
DEFAULT_DEGRADATION_CEILING = 0.08
DEFAULT_TIGHT_RACE_GAP = 5.0
DEFAULT_REPLAY_SUCCESS_FLOOR = 0.60

def _extract_period_metric(payload: dict[str, Any], period: int) -> float | None:
    data = dict(payload or {})
    for key in (period, str(period), f"{period}d", f"{period}D"):
        if key in data and data.get(key) is not None:
            return _safe_float(data.get(key), 0.0)
    return None

def _extract_strategy_candidate_reference(strategy: dict[str, Any]) -> dict[str, Any]:
    payload = dict(strategy or {})
    params = payload.get("params") if isinstance(payload.get("params"), dict) else {}
    provenance = params.get("candidate_provenance") if isinstance(params.get("candidate_provenance"), dict) else {}
    source_candidate_artifact_id = _safe_text(
        provenance.get("source_candidate_artifact_id"),
        params.get("source_candidate_artifact_id"),
    )
    return {
        "strategy_id": str(payload.get("id") or "").strip() or None,
        "strategy_name": _safe_text(payload.get("name")),
        "strategy_status": _safe_text(payload.get("status")),
        "source_candidate_artifact_id": source_candidate_artifact_id,
        "candidate_family": _safe_text(
            provenance.get("candidate_family"),
            params.get("candidate_family"),
        ),
        "candidate_name": _safe_text(
            provenance.get("candidate_name"),
            params.get("candidate_name"),
        ),
        "expected_regime": _as_text_list(
            provenance.get("expected_regime") if provenance.get("expected_regime") is not None else params.get("expected_regime")
        ),
    }

def _count_severity(rows: list[dict[str, Any]], *, levels: set[str]) -> int:
    return len(
        [
            row
            for row in list(rows or [])
            if str(row.get("severity") or "").strip().lower() in levels
        ]
    )

def _contains_token(values: list[str], *tokens: str) -> bool:
    normalized = " ".join(str(value or "").strip().lower() for value in list(values or []) if str(value or "").strip())
    return any(str(token or "").strip().lower() in normalized for token in tokens if str(token or "").strip())

async def _persist_model_feedback_artifact(
    *,
    strategy: dict[str, Any],
    model_item: dict[str, Any],
    feedback_summary: dict[str, Any],
    feedback_flags: list[str],
    recommended_action: str,
    latest_metric: dict[str, Any],
    signal_stats: dict[str, Any],
    risk_events: list[dict[str, Any]],
    runtime_alerts: list[dict[str, Any]],
    latest_pipeline_snapshot: dict[str, Any],
    output_artifact_id: str | None = None,
) -> dict[str, Any]:
    artifact_id = str(output_artifact_id or f"quant_model_feedback_{int(time.time())}_{uuid4().hex[:8]}").strip()
    created_at = _now_iso()
    payload = {
        "artifact_id": artifact_id,
        "action": "model_registry",
        "op": "feedback_sync",
        "strategy_id": strategy.get("id"),
        "strategy_name": strategy.get("name"),
        "strategy_status": strategy.get("status"),
        "source_validation_artifact_id": model_item.get("source_validation_artifact_id"),
        "source_generation_artifact_id": model_item.get("source_generation_artifact_id"),
        "source_model_registry_artifact_id": model_item.get("artifact_id"),
        "candidate_family": model_item.get("family"),
        "feedback_summary": feedback_summary,
        "feedback_flags": list(feedback_flags),
        "recommended_action": recommended_action,
        "incubation_metric": dict(latest_metric or {}),
        "signal_stats": dict(signal_stats or {}),
        "runtime_risk_events": [dict(item or {}) for item in list(risk_events or []) if isinstance(item, dict)],
        "runtime_alerts": [dict(item or {}) for item in list(runtime_alerts or []) if isinstance(item, dict)],
        "incubation_pipeline_snapshot": dict(latest_pipeline_snapshot or {}),
        "created_at": created_at,
        "updated_at": created_at,
    }
    await register_artifact_async(
        {
            "artifact_id": artifact_id,
            "strategy": MODEL_FEEDBACK_STRATEGY,
            "strategy_version": MODEL_FEEDBACK_VERSION,
            "code": ",".join(list(model_item.get("codes") or [])[:5]),
            "payload": payload,
            "created_at": created_at,
        }
    )
    return payload

async def _apply_model_feedback(
    *,
    model_item: dict[str, Any],
    strategy: dict[str, Any],
    feedback_artifact: dict[str, Any],
    feedback_summary: dict[str, Any],
    feedback_flags: list[str],
    recommended_action: str,
) -> dict[str, Any]:
    model_artifact_id = str(model_item.get("artifact_id") or "").strip()
    model_artifact = await get_artifact_async(model_artifact_id)
    if not model_artifact:
        raise RuntimeError(f"model registry artifact not found: {model_artifact_id}")
    model_payload = _payload_from_artifact_row(model_artifact)
    existing_feedback_flags = [
        str(item).strip()
        for item in list(model_payload.get("feedback_flags") or [])
        if str(item).strip()
    ]
    combined_feedback_flags = list(dict.fromkeys([*existing_feedback_flags, *list(feedback_flags or [])]))
    feedback_artifact_ids = list(
        dict.fromkeys(
            [
                str(feedback_artifact.get("artifact_id") or "").strip(),
                *[
                    str(item).strip()
                    for item in list(model_payload.get("feedback_artifact_ids") or [])
                    if str(item).strip()
                ],
            ]
        )
    )[:10]
    feedback_strategy_ids = list(
        dict.fromkeys(
            [
                str(strategy.get("id") or "").strip(),
                *[
                    str(item).strip()
                    for item in list(model_payload.get("feedback_strategy_ids") or [])
                    if str(item).strip()
                ],
            ]
        )
    )[:10]
    synced_at = _now_iso()
    updated_payload = {
        **deepcopy(model_payload),
        "feedback_summary": {
            **dict(model_payload.get("feedback_summary") or {}),
            **dict(feedback_summary or {}),
            "strategy_count": len(feedback_strategy_ids),
            "combined_feedback_flags": combined_feedback_flags,
            "latest_feedback_artifact_id": feedback_artifact.get("artifact_id"),
        },
        "feedback_flags": combined_feedback_flags,
        "feedback_recommended_action": recommended_action,
        "last_feedback_artifact_id": feedback_artifact.get("artifact_id"),
        "last_feedback_sync_at": synced_at,
        "last_feedback_strategy_id": strategy.get("id"),
        "feedback_artifact_ids": feedback_artifact_ids,
        "feedback_strategy_ids": feedback_strategy_ids,
        "updated_at": synced_at,
    }
    await register_artifact_async(
        {
            "artifact_id": model_artifact_id,
            "strategy": MODEL_REGISTRY_STRATEGY,
            "strategy_version": str(model_artifact.get("strategy_version") or MODEL_REGISTRY_VERSION),
            "code": model_artifact.get("code") or ",".join(list(updated_payload.get("codes") or [])[:5]),
            "payload": updated_payload,
            "created_at": model_artifact.get("created_at") or model_payload.get("created_at") or synced_at,
        }
    )
    return _normalize_model_registry_item(
        {
            "artifact_id": model_artifact_id,
            "strategy": MODEL_REGISTRY_STRATEGY,
            "strategy_version": str(model_artifact.get("strategy_version") or MODEL_REGISTRY_VERSION),
            "created_at": model_artifact.get("created_at") or updated_payload.get("created_at"),
            "updated_at": updated_payload.get("updated_at"),
        },
        updated_payload,
    )

async def _load_feedback_sync_targets(
    *,
    db: Any,
    strategy_id: str | None,
    strategy_ids: list[str] | None,
    statuses: list[str] | None,
    limit: int,
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    for sid in [str(item).strip() for item in [strategy_id, *(strategy_ids or [])] if str(item).strip()]:
        if sid in seen or not hasattr(db, "get_strategy"):
            continue
        strategy = await db.get_strategy(sid)
        if not strategy:
            continue
        seen.add(sid)
        items.append(dict(strategy))
    if items:
        return items
    resolved_statuses = [str(item).strip().lower() for item in list(statuses or ["incubating", "listed"]) if str(item).strip()]
    if not resolved_statuses or not hasattr(db, "list_strategies"):
        return items
    for status in resolved_statuses:
        for strategy in await db.list_strategies(status, limit=limit):
            sid = str((strategy or {}).get("id") or "").strip()
            if not sid or sid in seen:
                continue
            seen.add(sid)
            items.append(dict(strategy or {}))
            if len(items) >= limit:
                return items
    return items

async def _build_strategy_feedback_payload(
    *,
    db: Any,
    strategy: dict[str, Any],
    model_item: dict[str, Any],
) -> dict[str, Any]:
    strategy_id = str(strategy.get("id") or "").strip()
    latest_metric = {}
    if hasattr(db, "list_strategy_incubation_metrics"):
        metric_rows = await db.list_strategy_incubation_metrics(strategy_id, limit=1)
        latest_metric = dict(metric_rows[0] or {}) if metric_rows else {}
    signal_stats = await db.get_signal_stats(strategy_id) if hasattr(db, "get_signal_stats") else {}
    risk_events = (
        await db.list_strategy_runtime_risk_events(strategy_id=strategy_id, status="open", limit=20)
        if hasattr(db, "list_strategy_runtime_risk_events")
        else []
    )
    runtime_alerts = (
        await db.list_strategy_runtime_alerts(strategy_id=strategy_id, limit=20)
        if hasattr(db, "list_strategy_runtime_alerts")
        else []
    )
    latest_pipeline_snapshot = (
        await db.get_latest_strategy_incubation_pipeline_snapshot(strategy_id)
        if hasattr(db, "get_latest_strategy_incubation_pipeline_snapshot")
        else None
    ) or {}
    hit_rate_5d = latest_metric.get("hit_rate_5d")
    if hit_rate_5d is None:
        hit_rate_5d = _extract_period_metric(dict(signal_stats.get("hit_rate") or {}), 5)
    forward_ic_5d = latest_metric.get("forward_ic_5d")
    if forward_ic_5d is None:
        forward_ic_5d = _extract_period_metric(dict(signal_stats.get("forward_ic") or {}), 5)
    forward_sharpe_5d = latest_metric.get("forward_sharpe_5d")
    if forward_sharpe_5d is None:
        forward_sharpe_5d = _extract_period_metric(dict(signal_stats.get("forward_sharpe") or {}), 5)

    alpha_decay = _safe_float(latest_metric.get("alpha_decay"), 0.0)
    drift_score = _safe_float(latest_metric.get("drift_score"), 0.0)
    turnover_rate = _safe_float(latest_metric.get("turnover_rate"), 0.0)
    exposure_rate = _safe_float(latest_metric.get("exposure_rate"), 0.0)
    open_risk_event_count = len(list(risk_events or []))
    critical_open_event_count = _count_severity(list(risk_events or []), levels={"critical", "high"})
    open_runtime_alerts = [
        dict(item or {})
        for item in list(runtime_alerts or [])
        if str((item or {}).get("status") or "open").strip().lower() not in {"resolved", "closed"}
    ]
    critical_runtime_alert_count = _count_severity(open_runtime_alerts, levels={"critical", "high"})
    combined_risk_tokens = [
        *[str(item).strip() for item in list(latest_metric.get("risk_flags") or []) if str(item).strip()],
        *[str(item).strip() for item in list(latest_metric.get("blockers") or []) if str(item).strip()],
        *[str(item).strip() for item in list(latest_pipeline_snapshot.get("risk_flags") or []) if str(item).strip()],
        *[str(item).strip() for item in list(latest_pipeline_snapshot.get("blockers") or []) if str(item).strip()],
    ]

    feedback_flags: list[str] = []
    if (
        alpha_decay >= 0.12
        or _safe_float(forward_sharpe_5d, 0.0) < -0.05
        or (_safe_float(hit_rate_5d, 0.0) > 0 and _safe_float(hit_rate_5d, 0.0) < 0.45)
    ):
        feedback_flags.append("feedback_decay")
    if drift_score >= 0.55 or _contains_token(combined_risk_tokens, "drift", "regime", "style_shift"):
        feedback_flags.append("feedback_regime_shift")
    if turnover_rate >= 0.65 and exposure_rate >= 0.80:
        feedback_flags.append("feedback_crowding")
    if critical_open_event_count > 0 or critical_runtime_alert_count > 0:
        feedback_flags.append("feedback_runtime_critical")
    elif open_risk_event_count >= 3 or len(open_runtime_alerts) >= 3:
        feedback_flags.append("feedback_runtime_pressure")
    decision_token = str(latest_metric.get("decision") or latest_pipeline_snapshot.get("latest_decision") or latest_pipeline_snapshot.get("next_action") or "").strip().lower()
    pipeline_status = str(latest_pipeline_snapshot.get("pipeline_status") or "").strip().lower()
    if decision_token in {"halt", "review", "defer", "stop"} or pipeline_status in {"halted", "blocked", "review"}:
        feedback_flags.append("feedback_pipeline_halt")
    feedback_flags = list(dict.fromkeys(feedback_flags))

    recommended_action = (
        "schedule_retrain"
        if any(
            flag in {
                "feedback_decay",
                "feedback_regime_shift",
                "feedback_runtime_critical",
                "feedback_pipeline_halt",
            }
            for flag in feedback_flags
        )
        else ("review" if feedback_flags else "monitor")
    )
    feedback_summary = {
        "strategy_id": strategy_id,
        "strategy_name": strategy.get("name"),
        "strategy_status": strategy.get("status"),
        "source_validation_artifact_id": model_item.get("source_validation_artifact_id"),
        "source_generation_artifact_id": model_item.get("source_generation_artifact_id"),
        "metric_date": latest_metric.get("metric_date"),
        "pipeline_status": latest_pipeline_snapshot.get("pipeline_status"),
        "pipeline_next_action": latest_pipeline_snapshot.get("next_action"),
        "decision": latest_metric.get("decision") or latest_pipeline_snapshot.get("latest_decision"),
        "nav": latest_metric.get("nav"),
        "daily_return": latest_metric.get("daily_return"),
        "sharpe_ratio": latest_metric.get("sharpe_ratio"),
        "alpha_decay": latest_metric.get("alpha_decay"),
        "drift_score": latest_metric.get("drift_score"),
        "turnover_rate": latest_metric.get("turnover_rate"),
        "exposure_rate": latest_metric.get("exposure_rate"),
        "hit_rate_5d": hit_rate_5d,
        "forward_ic_5d": forward_ic_5d,
        "forward_sharpe_5d": forward_sharpe_5d,
        "total_signals": latest_metric.get("total_signals") or signal_stats.get("total_signals"),
        "open_risk_event_count": open_risk_event_count,
        "critical_open_event_count": critical_open_event_count,
        "open_runtime_alert_count": len(open_runtime_alerts),
        "critical_runtime_alert_count": critical_runtime_alert_count,
        "risk_flags": list(dict.fromkeys(combined_risk_tokens)),
        "feedback_flags": list(feedback_flags),
        "recommended_action": recommended_action,
        "synced_at": _now_iso(),
        "decay_detected": "feedback_decay" in feedback_flags,
        "regime_shift_detected": "feedback_regime_shift" in feedback_flags,
        "crowding_elevated": "feedback_crowding" in feedback_flags,
        "runtime_pressure_detected": any(flag in {"feedback_runtime_critical", "feedback_runtime_pressure"} for flag in feedback_flags),
    }
    return {
        "latest_metric": dict(latest_metric or {}),
        "signal_stats": dict(signal_stats or {}),
        "risk_events": [dict(item or {}) for item in list(risk_events or []) if isinstance(item, dict)],
        "runtime_alerts": open_runtime_alerts,
        "latest_pipeline_snapshot": dict(latest_pipeline_snapshot or {}),
        "feedback_flags": feedback_flags,
        "recommended_action": recommended_action,
        "feedback_summary": feedback_summary,
    }


__all__ = [name for name in globals() if name.startswith("_") or name.isupper()]
