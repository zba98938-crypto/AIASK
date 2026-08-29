"""Model registry and retrain governance handlers for quant_manager."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Callable

import numpy as np

from ...services import get_artifact_async, list_artifacts_async, register_artifact_async
from ...services.rolling_model_registry import default_rolling_registry
from .quant_mgr_artifact_common import _payload_from_artifact_row
from .quant_mgr_helpers import _as_code_list, _safe_float

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

def _safe_text(*values: Any) -> str | None:
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return None

def _coerce_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    text = str(value or "").strip().lower()
    if text in {"1", "true", "yes", "on", "y"}:
        return True
    if text in {"0", "false", "no", "off", "n"}:
        return False
    return default

def _as_text_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    text = str(value or "").strip()
    if not text:
        return []
    return [item.strip() for item in text.split(",") if item.strip()]

def _safe_optional_int(value: Any) -> int | None:
    if value in {None, ""}:
        return None
    try:
        return int(value)
    except Exception:
        return None

def _now_iso() -> str:
    return datetime.now().astimezone().isoformat()

def _model_registry_artifact_id(source_validation_artifact_id: str) -> str:
    return f"model_registry_{source_validation_artifact_id}"

def _extract_validation_quality(validation_payload: dict[str, Any]) -> dict[str, Any]:
    candidate = validation_payload.get("candidate") if isinstance(validation_payload.get("candidate"), dict) else {}
    rating = validation_payload.get("rating") if isinstance(validation_payload.get("rating"), dict) else {}
    metrics = validation_payload.get("metrics") if isinstance(validation_payload.get("metrics"), dict) else {}
    factor_validation_report = (
        validation_payload.get("factor_validation_report")
        if isinstance(validation_payload.get("factor_validation_report"), dict)
        else {}
    )
    oos = factor_validation_report.get("oos") if isinstance(factor_validation_report.get("oos"), dict) else {}
    walk_forward = oos.get("walk_forward") if isinstance(oos.get("walk_forward"), dict) else {}
    purged_kfold = oos.get("purged_kfold") if isinstance(oos.get("purged_kfold"), dict) else {}
    lookahead_audit = validation_payload.get("lookahead_audit") if isinstance(validation_payload.get("lookahead_audit"), dict) else {}
    multiple_testing = validation_payload.get("multiple_testing") if isinstance(validation_payload.get("multiple_testing"), dict) else {}
    params = validation_payload.get("params") if isinstance(validation_payload.get("params"), dict) else {}
    return {
        "name": candidate.get("name"),
        "family": candidate.get("family"),
        "codes": _as_code_list(validation_payload.get("codes")),
        "expected_regime": _as_text_list(candidate.get("expected_regime")),
        "expected_holding_period": _safe_optional_int(candidate.get("expected_holding_period")),
        "grade": str(rating.get("grade") or "").strip().upper(),
        "recommendation": str(rating.get("recommendation") or "").strip().lower(),
        "total_score": _safe_float(rating.get("total_score"), 0.0),
        "rank_ic_mean": _safe_float(metrics.get("rank_ic_mean"), 0.0),
        "rank_ic_ir": _safe_float(metrics.get("rank_ic_ir"), 0.0),
        "sample_dates": int(metrics.get("sample_dates", 0) or 0),
        "walk_forward_stability_ratio": _safe_float(walk_forward.get("stability_ratio"), 0.0),
        "purged_kfold_stability_ratio": _safe_float(purged_kfold.get("stability_ratio"), 0.0),
        "walk_forward_degradation": _safe_float(walk_forward.get("degradation"), 0.0),
        "purged_kfold_degradation": _safe_float(purged_kfold.get("degradation"), 0.0),
        "walk_forward_oos_rank_ic_mean": _safe_float(walk_forward.get("oos_rank_ic_mean"), 0.0),
        "purged_kfold_oos_rank_ic_mean": _safe_float(purged_kfold.get("oos_rank_ic_mean"), 0.0),
        "lookahead_risk_level": str(lookahead_audit.get("risk_level") or "unknown").strip().lower(),
        "lookahead_available": bool(lookahead_audit.get("available")) if lookahead_audit else False,
        "multiple_testing_risk_level": str(multiple_testing.get("risk_level") or "unknown").strip().lower(),
        "multiple_testing_available": bool(multiple_testing.get("available")) if multiple_testing else False,
        "validation_params": {
            key: parsed
            for key, value in params.items()
            if key in {"lookback_bars", "horizon_days", "max_dates"}
            and (parsed := _safe_optional_int(value)) is not None
        },
    }

def _normalize_model_registry_item(artifact: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "artifact_id": str(artifact.get("artifact_id") or payload.get("artifact_id") or ""),
        "strategy": str(artifact.get("strategy") or payload.get("strategy") or ""),
        "strategy_version": str(artifact.get("strategy_version") or payload.get("strategy_version") or ""),
        "created_at": artifact.get("created_at") or payload.get("created_at"),
        "updated_at": artifact.get("updated_at") or payload.get("updated_at"),
        "model_key": str(payload.get("model_key") or ""),
        "name": payload.get("name"),
        "family": str(payload.get("family") or "").strip().lower(),
        "codes": _as_code_list(payload.get("codes")),
        "deployment_stage": str(payload.get("deployment_stage") or "unknown").strip().lower() or "unknown",
        "expected_regime": _as_text_list(payload.get("expected_regime")),
        "expected_holding_period": _safe_optional_int(payload.get("expected_holding_period")),
        "validation_params": {
            key: parsed
            for key, value in dict(payload.get("validation_params") or {}).items()
            if key in {"lookback_bars", "horizon_days", "max_dates"}
            and (parsed := _safe_optional_int(value)) is not None
        },
        "review_status": str(payload.get("review_status") or "").strip().lower() or None,
        "review_rank": int(payload.get("review_rank", 0) or 0),
        "source_validation_artifact_id": payload.get("source_validation_artifact_id"),
        "source_generation_artifact_id": payload.get("source_generation_artifact_id"),
        "grade": str(payload.get("grade") or "").strip().upper(),
        "recommendation": str(payload.get("recommendation") or "").strip().lower(),
        "total_score": _safe_float(payload.get("total_score"), 0.0),
        "rank_ic_mean": _safe_float(payload.get("rank_ic_mean"), 0.0),
        "rank_ic_ir": _safe_float(payload.get("rank_ic_ir"), 0.0),
        "sample_dates": int(payload.get("sample_dates", 0) or 0),
        "walk_forward_stability_ratio": _safe_float(payload.get("walk_forward_stability_ratio"), 0.0),
        "purged_kfold_stability_ratio": _safe_float(payload.get("purged_kfold_stability_ratio"), 0.0),
        "walk_forward_degradation": _safe_float(payload.get("walk_forward_degradation"), 0.0),
        "purged_kfold_degradation": _safe_float(payload.get("purged_kfold_degradation"), 0.0),
        "lookahead_risk_level": str(payload.get("lookahead_risk_level") or "unknown").strip().lower(),
        "multiple_testing_risk_level": str(payload.get("multiple_testing_risk_level") or "unknown").strip().lower(),
        "flags": [str(item).strip() for item in list(payload.get("flags") or []) if str(item).strip()],
        "recommended_action": str(payload.get("recommended_action") or "monitor").strip().lower() or "monitor",
        "last_replay_success_rate": _safe_float(payload.get("last_replay_success_rate"), 0.0),
        "last_replay_artifact_id": payload.get("last_replay_artifact_id"),
        "feedback_summary": dict(payload.get("feedback_summary") or {}),
        "feedback_flags": [str(item).strip() for item in list(payload.get("feedback_flags") or []) if str(item).strip()],
        "feedback_recommended_action": str(payload.get("feedback_recommended_action") or "").strip().lower() or None,
        "last_feedback_artifact_id": payload.get("last_feedback_artifact_id"),
        "last_feedback_sync_at": payload.get("last_feedback_sync_at"),
        "last_feedback_strategy_id": payload.get("last_feedback_strategy_id"),
        "feedback_artifact_ids": [
            str(item).strip()
            for item in list(payload.get("feedback_artifact_ids") or [])
            if str(item).strip()
        ],
        "feedback_strategy_ids": [
            str(item).strip()
            for item in list(payload.get("feedback_strategy_ids") or [])
            if str(item).strip()
        ],
    }

def _summarize_model_registry_items(items: list[dict[str, Any]]) -> dict[str, Any]:
    stage_counts: dict[str, int] = {}
    family_counts: dict[str, int] = {}
    scores: list[float] = []
    for item in list(items or []):
        stage = str(item.get("deployment_stage") or "unknown").strip().lower() or "unknown"
        family = str(item.get("family") or "unknown").strip().lower() or "unknown"
        stage_counts[stage] = int(stage_counts.get(stage, 0)) + 1
        family_counts[family] = int(family_counts.get(family, 0)) + 1
        scores.append(_safe_float(item.get("total_score"), 0.0))
    return {
        "count": len(list(items or [])),
        "deployment_stage_counts": stage_counts,
        "family_counts": family_counts,
        "champion_count": int(stage_counts.get("champion", 0)),
        "challenger_count": int(stage_counts.get("challenger", 0)),
        "feedback_flagged_count": len([item for item in list(items or []) if list(item.get("feedback_flags") or [])]),
        "feedback_retrain_signal_count": len(
            [
                item
                for item in list(items or [])
                if str(item.get("feedback_recommended_action") or "").strip().lower() == "schedule_retrain"
            ]
        ),
        "avg_total_score": round(float(np.mean(scores)), 6) if scores else 0.0,
        "max_total_score": round(float(max(scores)), 6) if scores else 0.0,
    }

async def _list_model_registry_items(
    *,
    limit: int = 20,
    codes: list[str] | None = None,
    family: str | None = None,
    deployment_stage: str | None = None,
    artifact_id: str | None = None,
    source_validation_artifact_ids: list[str] | None = None,
    source_generation_artifact_ids: list[str] | None = None,
    market_codes_only: bool = False,
    filter_market_codes: Callable[[Any], list[str]],
) -> list[dict[str, Any]]:
    fetch_limit = max(50, min(1000, int(limit) * 12))
    rows = await list_artifacts_async(limit=fetch_limit)
    summary_rows = rows if isinstance(rows, list) else []
    requested_codes = filter_market_codes(codes) if market_codes_only else list(codes or [])
    requested_validation_ids = {str(item).strip() for item in list(source_validation_artifact_ids or []) if str(item).strip()}
    requested_generation_ids = {str(item).strip() for item in list(source_generation_artifact_ids or []) if str(item).strip()}
    items: list[dict[str, Any]] = []

    for row in summary_rows:
        if str(row.get("strategy") or "").strip().lower() != MODEL_REGISTRY_STRATEGY:
            continue
        row_artifact_id = str(row.get("artifact_id") or "").strip()
        if not row_artifact_id:
            continue
        if artifact_id and row_artifact_id != str(artifact_id).strip():
            continue
        artifact = await get_artifact_async(row_artifact_id)
        if artifact is None:
            continue
        payload = _payload_from_artifact_row(artifact)
        item = _normalize_model_registry_item(artifact, payload)
        record_codes = filter_market_codes(item.get("codes")) if market_codes_only else list(item.get("codes") or [])
        if market_codes_only and not record_codes:
            continue
        if requested_codes and not (set(requested_codes) & set(record_codes)):
            continue
        if family and str(item.get("family") or "").strip().lower() != str(family).strip().lower():
            continue
        if deployment_stage and str(item.get("deployment_stage") or "").strip().lower() != str(deployment_stage).strip().lower():
            continue
        if requested_validation_ids and str(item.get("source_validation_artifact_id") or "").strip() not in requested_validation_ids:
            continue
        if requested_generation_ids and str(item.get("source_generation_artifact_id") or "").strip() not in requested_generation_ids:
            continue
        items.append(item)

    items.sort(
        key=lambda item: (
            _safe_float(item.get("total_score"), 0.0),
            str(item.get("updated_at") or item.get("created_at") or ""),
        ),
        reverse=True,
    )
    return items[: max(1, int(limit))]

async def _persist_model_registry_entry(
    *,
    validation_item: dict[str, Any],
    deployment_stage: str,
    review_status: str | None,
    review_rank: int,
    comparison_to_champion: dict[str, Any] | None,
) -> dict[str, Any]:
    validation_artifact_id = str(validation_item.get("artifact_id") or "").strip()
    validation_artifact = await get_artifact_async(validation_artifact_id)
    validation_payload = _payload_from_artifact_row(validation_artifact or {})
    quality = _extract_validation_quality(validation_payload)
    registry_artifact_id = _model_registry_artifact_id(validation_artifact_id)
    existing_artifact = await get_artifact_async(registry_artifact_id)
    existing_payload = _payload_from_artifact_row(existing_artifact or {})
    created_at = (
        (existing_artifact or {}).get("created_at")
        or existing_payload.get("created_at")
        or _now_iso()
    )
    family = str(quality.get("family") or "unknown").strip().lower() or "unknown"
    name = str(quality.get("name") or validation_item.get("candidate", {}).get("name") or validation_artifact_id).strip()
    model_key = f"{family}:{name}:{validation_artifact_id}"
    payload = {
        "artifact_id": registry_artifact_id,
        "action": "model_registry",
        "model_key": model_key,
        "name": name,
        "family": family,
        "codes": list(quality.get("codes") or validation_item.get("codes") or []),
        "deployment_stage": str(deployment_stage or "unknown").strip().lower() or "unknown",
        "expected_regime": list(quality.get("expected_regime") or []),
        "expected_holding_period": quality.get("expected_holding_period"),
        "validation_params": dict(quality.get("validation_params") or {}),
        "review_status": review_status,
        "review_rank": int(review_rank),
        "source_validation_artifact_id": validation_artifact_id,
        "source_generation_artifact_id": validation_item.get("source_generation_artifact_id"),
        "grade": quality.get("grade"),
        "recommendation": quality.get("recommendation"),
        "total_score": _safe_float(quality.get("total_score"), 0.0),
        "rank_ic_mean": _safe_float(quality.get("rank_ic_mean"), 0.0),
        "rank_ic_ir": _safe_float(quality.get("rank_ic_ir"), 0.0),
        "sample_dates": int(quality.get("sample_dates", 0) or 0),
        "walk_forward_stability_ratio": _safe_float(quality.get("walk_forward_stability_ratio"), 0.0),
        "purged_kfold_stability_ratio": _safe_float(quality.get("purged_kfold_stability_ratio"), 0.0),
        "walk_forward_degradation": _safe_float(quality.get("walk_forward_degradation"), 0.0),
        "purged_kfold_degradation": _safe_float(quality.get("purged_kfold_degradation"), 0.0),
        "walk_forward_oos_rank_ic_mean": _safe_float(quality.get("walk_forward_oos_rank_ic_mean"), 0.0),
        "purged_kfold_oos_rank_ic_mean": _safe_float(quality.get("purged_kfold_oos_rank_ic_mean"), 0.0),
        "lookahead_risk_level": quality.get("lookahead_risk_level"),
        "lookahead_available": bool(quality.get("lookahead_available")),
        "multiple_testing_risk_level": quality.get("multiple_testing_risk_level"),
        "multiple_testing_available": bool(quality.get("multiple_testing_available")),
        "flags": list(existing_payload.get("flags") or []),
        "recommended_action": existing_payload.get("recommended_action") or "monitor",
        "comparison_to_champion": comparison_to_champion or {},
        "created_at": created_at,
        "updated_at": _now_iso(),
    }
    await register_artifact_async(
        {
            "artifact_id": registry_artifact_id,
            "strategy": MODEL_REGISTRY_STRATEGY,
            "strategy_version": MODEL_REGISTRY_VERSION,
            "code": ",".join(list(payload.get("codes") or [])[:5]),
            "payload": payload,
            "created_at": created_at,
        }
    )

    default_rolling_registry.record_evaluation(
        model_key,
        {
            "metrics": {
                "rank_ic_mean": payload.get("rank_ic_mean"),
                "rank_ic_ir": payload.get("rank_ic_ir"),
                "sample_dates": payload.get("sample_dates"),
            },
            "rating": {
                "total_score": payload.get("total_score"),
                "recommendation": payload.get("recommendation"),
            },
            "deployment_stage": payload.get("deployment_stage"),
            "purged_kfold_stability_ratio": payload.get("purged_kfold_stability_ratio"),
            "purged_kfold_degradation": payload.get("purged_kfold_degradation"),
            "lookahead_risk_level": payload.get("lookahead_risk_level"),
        },
        window_tag=_now_iso()[:10],
    )
    return _normalize_model_registry_item(
        {
            "artifact_id": registry_artifact_id,
            "strategy": MODEL_REGISTRY_STRATEGY,
            "strategy_version": MODEL_REGISTRY_VERSION,
            "created_at": created_at,
            "updated_at": payload.get("updated_at"),
        },
        payload,
    )

async def _load_replay_items_for_generation(source_generation_artifact_id: str | None) -> list[dict[str, Any]]:
    generation_id = str(source_generation_artifact_id or "").strip()
    if not generation_id:
        return []
    rows = await list_artifacts_async(limit=500)
    items: list[dict[str, Any]] = []
    for row in rows if isinstance(rows, list) else []:
        if str(row.get("strategy") or "").strip().lower() != "quant_factor_episode_replay":
            continue
        artifact_id = str(row.get("artifact_id") or "").strip()
        if not artifact_id:
            continue
        artifact = await get_artifact_async(artifact_id)
        if artifact is None:
            continue
        payload = _payload_from_artifact_row(artifact)
        if str(payload.get("source_artifact_id") or "").strip() != generation_id:
            continue
        summary = payload.get("episode_summary") if isinstance(payload.get("episode_summary"), dict) else {}
        validated = int(summary.get("validated_count", 0) or 0)
        failed = int(summary.get("failed_count", 0) or 0)
        replayed = validated + failed
        items.append(
            {
                "artifact_id": artifact_id,
                "created_at": artifact.get("created_at") or payload.get("created_at"),
                "updated_at": artifact.get("updated_at") or payload.get("updated_at"),
                "validated_count": validated,
                "failed_count": failed,
                "success_rate": float(validated / max(replayed, 1)),
            }
        )
    items.sort(key=lambda item: str(item.get("updated_at") or item.get("created_at") or ""), reverse=True)
    return items

async def _scan_model_registry_items(
    items: list[dict[str, Any]],
    *,
    stability_floor: float,
    degradation_ceiling: float,
    tight_race_gap: float,
    replay_success_floor: float,
) -> list[dict[str, Any]]:
    challengers_by_family: dict[str, list[dict[str, Any]]] = {}
    for item in list(items or []):
        if str(item.get("deployment_stage") or "").strip().lower() == "challenger":
            challengers_by_family.setdefault(str(item.get("family") or "unknown"), []).append(item)

    scanned_items: list[dict[str, Any]] = []
    for item in list(items or []):
        scanned = dict(item)
        flags: list[str] = [str(flag).strip() for flag in list(scanned.get("feedback_flags") or []) if str(flag).strip()]
        min_stability = min(
            _safe_float(scanned.get("walk_forward_stability_ratio"), 0.0),
            _safe_float(scanned.get("purged_kfold_stability_ratio"), 0.0),
        )
        max_degradation = max(
            _safe_float(scanned.get("walk_forward_degradation"), 0.0),
            _safe_float(scanned.get("purged_kfold_degradation"), 0.0),
        )
        if min_stability < float(stability_floor):
            flags.append("low_stability")
        if max_degradation > float(degradation_ceiling):
            flags.append("high_degradation")

        replay_items = await _load_replay_items_for_generation(scanned.get("source_generation_artifact_id"))
        latest_replay = replay_items[0] if replay_items else {}
        replay_success_rate = _safe_float((latest_replay or {}).get("success_rate"), -1.0)
        if replay_items and replay_success_rate < float(replay_success_floor):
            flags.append("replay_decay")
        scanned["last_replay_artifact_id"] = latest_replay.get("artifact_id")
        scanned["last_replay_success_rate"] = max(replay_success_rate, 0.0) if replay_items else 0.0

        if str(scanned.get("deployment_stage") or "").strip().lower() == "champion":
            family = str(scanned.get("family") or "unknown")
            challengers = challengers_by_family.get(family, [])
            if challengers:
                best_challenger = max(challengers, key=lambda row: _safe_float(row.get("total_score"), 0.0))
                score_gap = _safe_float(scanned.get("total_score"), 0.0) - _safe_float(best_challenger.get("total_score"), 0.0)
                scanned["best_challenger_artifact_id"] = best_challenger.get("artifact_id")
                scanned["challenger_score_gap"] = round(score_gap, 6)
                if score_gap <= float(tight_race_gap):
                    flags.append("challenger_pressure")

        seen: set[str] = set()
        deduped_flags: list[str] = []
        for flag in flags:
            if flag in seen:
                continue
            seen.add(flag)
            deduped_flags.append(flag)
        scanned["flags"] = deduped_flags
        feedback_recommended_action = str(scanned.get("feedback_recommended_action") or "").strip().lower()
        scanned["recommended_action"] = (
            "schedule_retrain"
            if (
                str(scanned.get("deployment_stage") or "").strip().lower() == "champion"
                and deduped_flags
            )
            else (feedback_recommended_action or "monitor")
        )
        scanned_items.append(scanned)
    scanned_items.sort(
        key=lambda item: (
            1 if item.get("deployment_stage") == "champion" else 0,
            _safe_float(item.get("total_score"), 0.0),
        ),
        reverse=True,
    )
    return scanned_items

def _summarize_lifecycle_scan(items: list[dict[str, Any]]) -> dict[str, Any]:
    recommended = [item for item in list(items or []) if item.get("recommended_action") == "schedule_retrain"]
    flag_counts: dict[str, int] = {}
    for item in list(items or []):
        for flag in list(item.get("flags") or []):
            flag_counts[str(flag)] = int(flag_counts.get(str(flag), 0)) + 1
    return {
        "count": len(list(items or [])),
        "retrain_recommended_count": len(recommended),
        "flag_counts": flag_counts,
    }

def _normalize_retrain_plan_item(artifact: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "artifact_id": str(artifact.get("artifact_id") or payload.get("artifact_id") or payload.get("plan_id") or ""),
        "plan_id": str(payload.get("plan_id") or artifact.get("artifact_id") or ""),
        "status": str(payload.get("status") or "planned").strip().lower() or "planned",
        "family": str(payload.get("family") or "").strip().lower() or None,
        "codes": _as_code_list(payload.get("codes")),
        "priority": str(payload.get("priority") or "").strip().lower() or None,
        "scheduler_status": str(payload.get("scheduler_status") or "").strip().lower() or None,
        "execution_mode": str(payload.get("execution_mode") or "").strip().lower() or None,
        "schedule_hint": str(payload.get("schedule_hint") or "").strip().lower() or None,
        "target_model_count": int(payload.get("target_model_count", 0) or 0),
        "target_models": [dict(item or {}) for item in list(payload.get("target_models") or []) if isinstance(item, dict)],
        "target_generation_artifact_ids": [str(item).strip() for item in list(payload.get("target_generation_artifact_ids") or []) if str(item).strip()],
        "reason_codes": [str(item).strip() for item in list(payload.get("reason_codes") or []) if str(item).strip()],
        "next_action": str(payload.get("next_action") or "").strip().lower() or None,
        "run_count": int(payload.get("run_count", 0) or 0),
        "failure_count": int(payload.get("failure_count", 0) or 0),
        "last_run_status": payload.get("last_run_status"),
        "last_run_artifact_id": payload.get("last_run_artifact_id"),
        "next_run_at": payload.get("next_run_at"),
        "created_at": artifact.get("created_at") or payload.get("created_at"),
        "updated_at": artifact.get("updated_at") or payload.get("updated_at"),
    }

def _summarize_retrain_plan_items(items: list[dict[str, Any]]) -> dict[str, Any]:
    status_counts: dict[str, int] = {}
    scheduler_status_counts: dict[str, int] = {}
    for item in list(items or []):
        status = str(item.get("status") or "planned").strip().lower() or "planned"
        scheduler_status = str(item.get("scheduler_status") or "none").strip().lower() or "none"
        status_counts[status] = int(status_counts.get(status, 0)) + 1
        scheduler_status_counts[scheduler_status] = int(scheduler_status_counts.get(scheduler_status, 0)) + 1
    return {
        "count": len(list(items or [])),
        "status_counts": status_counts,
        "scheduler_status_counts": scheduler_status_counts,
    }

async def _list_retrain_plan_items(
    *,
    limit: int = 20,
    artifact_id: str | None = None,
    family: str | None = None,
    codes: list[str] | None = None,
    source_validation_artifact_ids: list[str] | None = None,
    source_generation_artifact_ids: list[str] | None = None,
) -> list[dict[str, Any]]:
    fetch_limit = max(50, min(1000, int(limit) * 12))
    rows = await list_artifacts_async(limit=fetch_limit)
    items: list[dict[str, Any]] = []
    requested_validation_ids = {str(item).strip() for item in list(source_validation_artifact_ids or []) if str(item).strip()}
    requested_generation_ids = {str(item).strip() for item in list(source_generation_artifact_ids or []) if str(item).strip()}
    for row in rows if isinstance(rows, list) else []:
        if str(row.get("strategy") or "").strip().lower() != MODEL_RETRAIN_PLAN_STRATEGY:
            continue
        row_artifact_id = str(row.get("artifact_id") or "").strip()
        if not row_artifact_id:
            continue
        if artifact_id and row_artifact_id != str(artifact_id).strip():
            continue
        artifact = await get_artifact_async(row_artifact_id)
        if artifact is None:
            continue
        payload = _payload_from_artifact_row(artifact)
        item = _normalize_retrain_plan_item(artifact, payload)
        if family and str(item.get("family") or "").strip().lower() != str(family).strip().lower():
            continue
        if codes and not (set(codes) & set(item.get("codes") or [])):
            continue
        target_models = [dict(target or {}) for target in list(payload.get("target_models") or []) if isinstance(target, dict)]
        target_validation_ids = {
            str(target.get("source_validation_artifact_id") or "").strip()
            for target in target_models
            if str(target.get("source_validation_artifact_id") or "").strip()
        }
        target_generation_ids = {
            str(target.get("source_generation_artifact_id") or "").strip()
            for target in target_models
            if str(target.get("source_generation_artifact_id") or "").strip()
        } | {
            str(target).strip()
            for target in list(payload.get("target_generation_artifact_ids") or [])
            if str(target).strip()
        }
        if requested_validation_ids and not (requested_validation_ids & target_validation_ids):
            continue
        if requested_generation_ids and not (requested_generation_ids & target_generation_ids):
            continue
        items.append(item)
    items.sort(key=lambda item: str(item.get("updated_at") or item.get("created_at") or ""), reverse=True)
    return items[: max(1, int(limit))]

def _normalize_retrain_run_item(artifact: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "artifact_id": str(artifact.get("artifact_id") or payload.get("artifact_id") or ""),
        "plan_id": str(payload.get("plan_id") or payload.get("source_plan_artifact_id") or ""),
        "status": str(payload.get("status") or "unknown").strip().lower() or "unknown",
        "execution_mode": str(payload.get("execution_mode") or "").strip().lower() or None,
        "execution_summary": dict(payload.get("execution_summary") or {}),
        "replay_artifact_ids": [str(item).strip() for item in list(payload.get("replay_artifact_ids") or []) if str(item).strip()],
        "validation_artifact_ids": [str(item).strip() for item in list(payload.get("validation_artifact_ids") or []) if str(item).strip()],
        "registry_artifact_ids": [str(item).strip() for item in list(payload.get("registry_artifact_ids") or []) if str(item).strip()],
        "created_at": artifact.get("created_at") or payload.get("created_at"),
        "updated_at": artifact.get("updated_at") or payload.get("updated_at"),
    }

async def _list_retrain_run_items(
    *,
    limit: int = 20,
    plan_id: str | None = None,
    plan_ids: list[str] | None = None,
    artifact_id: str | None = None,
) -> list[dict[str, Any]]:
    fetch_limit = max(50, min(1000, int(limit) * 12))
    rows = await list_artifacts_async(limit=fetch_limit)
    items: list[dict[str, Any]] = []
    requested_plan_ids = {str(item).strip() for item in list(plan_ids or []) if str(item).strip()}
    if plan_id:
        requested_plan_ids.add(str(plan_id).strip())
    for row in rows if isinstance(rows, list) else []:
        if str(row.get("strategy") or "").strip().lower() != MODEL_RETRAIN_RUN_STRATEGY:
            continue
        row_artifact_id = str(row.get("artifact_id") or "").strip()
        if not row_artifact_id:
            continue
        if artifact_id and row_artifact_id != str(artifact_id).strip():
            continue
        artifact = await get_artifact_async(row_artifact_id)
        if artifact is None:
            continue
        payload = _payload_from_artifact_row(artifact)
        item = _normalize_retrain_run_item(artifact, payload)
        if requested_plan_ids and str(item.get("plan_id") or "").strip() not in requested_plan_ids:
            continue
        items.append(item)
    items.sort(key=lambda item: str(item.get("updated_at") or item.get("created_at") or ""), reverse=True)
    return items[: max(1, int(limit))]

def _summarize_retrain_run_items(items: list[dict[str, Any]]) -> dict[str, Any]:
    status_counts: dict[str, int] = {}
    for item in list(items or []):
        status = str(item.get("status") or "unknown").strip().lower() or "unknown"
        status_counts[status] = int(status_counts.get(status, 0)) + 1
    return {
        "count": len(list(items or [])),
        "status_counts": status_counts,
    }

__all__ = [name for name in globals() if name.startswith("_") or name.isupper()]
