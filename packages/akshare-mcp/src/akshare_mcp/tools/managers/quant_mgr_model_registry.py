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

from ._quant_mgr_model_registry_support import *

async def handle_champion_challenger(
    *,
    kw: dict[str, Any],
    ok: Callable[..., dict],
    fail: Callable[..., dict],
    filter_market_codes: Callable[[Any], list[str]],
) -> dict:
    op = str(kw.get("op", "review") or "review").strip().lower()
    if op not in {"review", "compare"}:
        return fail("champion_challenger 目前仅支持 op=review|compare")

    codes = _as_code_list(kw.get("codes"))
    family = str(kw.get("family") or "").strip() or None
    limit = max(2, min(int(kw.get("limit", 20) or 20), 100))
    market_codes_only = _coerce_bool(kw.get("market_codes_only"), False)
    update_registry = _coerce_bool(kw.get("update_registry"), True)
    tight_race_gap = float(kw.get("tight_race_gap", DEFAULT_TIGHT_RACE_GAP) or DEFAULT_TIGHT_RACE_GAP)

    candidates = await _list_factor_candidate_registry_items(
        limit=limit,
        codes=codes or None,
        family=family,
        recommendation=None,
        min_score=None,
        only_active=True,
        market_codes_only=market_codes_only,
        include_synthetic=False,
        filter_market_codes=filter_market_codes,
    )
    governed_items = [
        item
        for item in list(candidates or [])
        if not bool(((item.get("risk_audit") or {}).get("blocked")))
    ]
    if not governed_items:
        return fail("champion_challenger review 未找到可治理的候选模型")

    champion = dict(governed_items[0])
    challengers = [dict(item) for item in governed_items[1:]]
    first_challenger = challengers[0] if challengers else None
    score_gap = (
        round(
            _safe_float(champion.get("rating", {}).get("total_score"), 0.0)
            - _safe_float((first_challenger or {}).get("rating", {}).get("total_score"), 0.0),
            6,
        )
        if first_challenger is not None
        else None
    )
    review_status = "no_challenger"
    if first_challenger is not None:
        review_status = "tight_race" if score_gap is not None and score_gap <= tight_race_gap else "clear_leader"

    registered_models: list[dict[str, Any]] = []
    if update_registry:
        champion_entry = await _persist_model_registry_entry(
            validation_item=champion,
            deployment_stage="champion",
            review_status=review_status,
            review_rank=1,
            comparison_to_champion={"score_gap": 0.0},
        )
        registered_models.append(champion_entry)
        for idx, challenger in enumerate(challengers, start=2):
            comparison = {
                "score_gap": round(
                    _safe_float(champion.get("rating", {}).get("total_score"), 0.0)
                    - _safe_float(challenger.get("rating", {}).get("total_score"), 0.0),
                    6,
                ),
                "champion_validation_artifact_id": champion.get("artifact_id"),
            }
            challenger_entry = await _persist_model_registry_entry(
                validation_item=challenger,
                deployment_stage="challenger",
                review_status=review_status,
                review_rank=idx,
                comparison_to_champion=comparison,
            )
            registered_models.append(challenger_entry)

    return ok(
        {
            "op": "review",
            "review_status": review_status,
            "score_gap_to_first_challenger": score_gap,
            "champion": champion,
            "challengers": challengers,
            "registered_models": registered_models,
        },
        source_chain=["services.artifact_registry", "quant_manager.factor_candidate_registry", "quant_manager.model_registry"],
    )

async def handle_model_registry(
    *,
    kw: dict[str, Any],
    ok: Callable[..., dict],
    fail: Callable[..., dict],
    db: Any | None,
    quant_manager_call: QuantManagerCall,
    filter_market_codes: Callable[[Any], list[str]],
) -> dict:
    op = str(kw.get("op", "list") or "list").strip().lower()

    if op in {"list", "ls"}:
        codes = _as_code_list(kw.get("codes"))
        family = str(kw.get("family") or "").strip() or None
        deployment_stage = str(kw.get("deployment_stage") or "").strip() or None
        limit = max(1, min(int(kw.get("limit", 20) or 20), 100))
        market_codes_only = _coerce_bool(kw.get("market_codes_only"), False)
        items = await _list_model_registry_items(
            limit=limit,
            codes=codes or None,
            family=family,
            deployment_stage=deployment_stage,
            market_codes_only=market_codes_only,
            filter_market_codes=filter_market_codes,
        )
        return ok(
            {"op": "list", "count": len(items), "items": items, "summary": _summarize_model_registry_items(items)},
            source_chain=["services.artifact_registry", "quant_manager.model_registry"],
        )

    if op in {"summary", "stats"}:
        codes = _as_code_list(kw.get("codes"))
        family = str(kw.get("family") or "").strip() or None
        deployment_stage = str(kw.get("deployment_stage") or "").strip() or None
        limit = max(1, min(int(kw.get("limit", 200) or 200), 500))
        market_codes_only = _coerce_bool(kw.get("market_codes_only"), False)
        items = await _list_model_registry_items(
            limit=limit,
            codes=codes or None,
            family=family,
            deployment_stage=deployment_stage,
            market_codes_only=market_codes_only,
            filter_market_codes=filter_market_codes,
        )
        return ok(
            {"op": "summary", "summary": _summarize_model_registry_items(items)},
            source_chain=["services.artifact_registry", "quant_manager.model_registry"],
        )

    if op in {"get", "detail"}:
        artifact_id = str(kw.get("artifact_id") or "").strip()
        if not artifact_id:
            return fail("model_registry get 需要 artifact_id")
        items = await _list_model_registry_items(
            limit=1,
            artifact_id=artifact_id,
            filter_market_codes=filter_market_codes,
        )
        if not items:
            return fail(f"model registry item not found: {artifact_id}")
        return ok(
            {"op": "get", "item": items[0]},
            source_chain=["services.artifact_registry", "quant_manager.model_registry"],
        )

    if op in {"lineage", "graph"}:
        artifact_id = str(kw.get("artifact_id") or "").strip() or None
        family = str(kw.get("family") or "").strip() or None
        codes = _as_code_list(kw.get("codes"))
        limit = max(1, min(int(kw.get("limit", 20) or 20), 100))
        market_codes_only = _coerce_bool(kw.get("market_codes_only"), False)
        lineage = await _build_model_registry_lineage(
            artifact_id=artifact_id,
            validation_artifact_ids=_as_text_list(kw.get("validation_artifact_ids") or kw.get("source_validation_artifact_ids")),
            generation_artifact_ids=_as_text_list(kw.get("generation_artifact_ids") or kw.get("source_generation_artifact_ids")),
            family=family,
            codes=codes or None,
            limit=limit,
            market_codes_only=market_codes_only,
            filter_market_codes=filter_market_codes,
        )
        return ok(
            {"op": "lineage", **lineage},
            source_chain=["services.artifact_registry", "quant_manager.factor_candidate_registry", "quant_manager.model_registry"],
        )

    if op in {"feedback_sync", "sync_feedback"}:
        if db is None:
            return fail("feedback_sync requires db access")
        limit = max(1, min(int(kw.get("limit", 20) or 20), 100))
        strategy_id = str(kw.get("strategy_id") or kw.get("id") or "").strip() or None
        strategy_ids = _as_text_list(kw.get("strategy_ids"))
        statuses = _as_text_list(kw.get("statuses"))
        model_items = await _list_model_registry_items(
            limit=max(limit * 10, 50),
            codes=_as_code_list(kw.get("codes")) or None,
            family=str(kw.get("family") or "").strip() or None,
            deployment_stage=str(kw.get("deployment_stage") or "").strip() or None,
            market_codes_only=_coerce_bool(kw.get("market_codes_only"), False),
            filter_market_codes=filter_market_codes,
        )
        models_by_validation = {
            str(item.get("source_validation_artifact_id") or "").strip(): dict(item)
            for item in model_items
            if str(item.get("source_validation_artifact_id") or "").strip()
        }
        strategies = await _load_feedback_sync_targets(
            db=db,
            strategy_id=strategy_id,
            strategy_ids=strategy_ids or None,
            statuses=statuses or None,
            limit=limit,
        )
        if not strategies:
            return fail("feedback_sync 未找到策略")

        synced_items: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        for strategy in strategies:
            reference = _extract_strategy_candidate_reference(strategy)
            source_validation_artifact_id = str(reference.get("source_candidate_artifact_id") or "").strip()
            if not source_validation_artifact_id:
                skipped.append(
                    {
                        "strategy_id": reference.get("strategy_id"),
                        "reason": "missing_source_candidate_artifact_id",
                    }
                )
                continue
            model_item = models_by_validation.get(source_validation_artifact_id)
            if not model_item:
                skipped.append(
                    {
                        "strategy_id": reference.get("strategy_id"),
                        "source_validation_artifact_id": source_validation_artifact_id,
                        "reason": "model_registry_item_not_found",
                    }
                )
                continue
            feedback_payload = await _build_strategy_feedback_payload(
                db=db,
                strategy=dict(strategy or {}),
                model_item=model_item,
            )
            feedback_artifact = await _persist_model_feedback_artifact(
                strategy=dict(strategy or {}),
                model_item=model_item,
                feedback_summary=dict(feedback_payload.get("feedback_summary") or {}),
                feedback_flags=list(feedback_payload.get("feedback_flags") or []),
                recommended_action=str(feedback_payload.get("recommended_action") or "monitor"),
                latest_metric=dict(feedback_payload.get("latest_metric") or {}),
                signal_stats=dict(feedback_payload.get("signal_stats") or {}),
                risk_events=list(feedback_payload.get("risk_events") or []),
                runtime_alerts=list(feedback_payload.get("runtime_alerts") or []),
                latest_pipeline_snapshot=dict(feedback_payload.get("latest_pipeline_snapshot") or {}),
            )
            updated_model = await _apply_model_feedback(
                model_item=model_item,
                strategy=dict(strategy or {}),
                feedback_artifact=feedback_artifact,
                feedback_summary=dict(feedback_payload.get("feedback_summary") or {}),
                feedback_flags=list(feedback_payload.get("feedback_flags") or []),
                recommended_action=str(feedback_payload.get("recommended_action") or "monitor"),
            )
            synced_items.append(
                {
                    "strategy_id": strategy.get("id"),
                    "strategy_name": strategy.get("name"),
                    "source_validation_artifact_id": source_validation_artifact_id,
                    "feedback_artifact_id": feedback_artifact.get("artifact_id"),
                    "feedback_flags": list(feedback_payload.get("feedback_flags") or []),
                    "recommended_action": feedback_payload.get("recommended_action"),
                    "model_item": updated_model,
                }
            )

        if not synced_items:
            return fail("feedback_sync 未找到可回写的 model registry 项", source_chain=["services.artifact_registry", "strategy_runtime"])
        summary = {
            "strategy_count": len(strategies),
            "synced_count": len(synced_items),
            "skipped_count": len(skipped),
            "feedback_flagged_count": len([item for item in synced_items if list(item.get("feedback_flags") or [])]),
            "schedule_retrain_count": len(
                [
                    item
                    for item in synced_items
                    if str(item.get("recommended_action") or "").strip().lower() == "schedule_retrain"
                ]
            ),
        }
        return ok(
            {
                "op": "feedback_sync",
                "count": len(synced_items),
                "items": synced_items,
                "summary": summary,
                "skipped": skipped,
            },
            source_chain=["services.artifact_registry", "strategy_incubation", "strategy_runtime", "quant_manager.model_registry"],
        )

    if op in {"lifecycle_scan", "scan"}:
        codes = _as_code_list(kw.get("codes"))
        family = str(kw.get("family") or "").strip() or None
        deployment_stage = str(kw.get("deployment_stage") or "").strip() or None
        limit = max(1, min(int(kw.get("limit", 200) or 200), 500))
        items = await _list_model_registry_items(
            limit=limit,
            codes=codes or None,
            family=family,
            deployment_stage=deployment_stage,
            market_codes_only=_coerce_bool(kw.get("market_codes_only"), False),
            filter_market_codes=filter_market_codes,
        )
        scanned_items = await _scan_model_registry_items(
            items,
            stability_floor=float(kw.get("stability_floor", DEFAULT_STABILITY_FLOOR) or DEFAULT_STABILITY_FLOOR),
            degradation_ceiling=float(kw.get("degradation_ceiling", DEFAULT_DEGRADATION_CEILING) or DEFAULT_DEGRADATION_CEILING),
            tight_race_gap=float(kw.get("tight_race_gap", DEFAULT_TIGHT_RACE_GAP) or DEFAULT_TIGHT_RACE_GAP),
            replay_success_floor=float(kw.get("replay_success_floor", DEFAULT_REPLAY_SUCCESS_FLOOR) or DEFAULT_REPLAY_SUCCESS_FLOOR),
        )
        return ok(
            {"op": "lifecycle_scan", "count": len(scanned_items), "items": scanned_items, "summary": _summarize_lifecycle_scan(scanned_items)},
            source_chain=["services.artifact_registry", "quant_manager.model_registry", "quant_manager.replay_factor_episode"],
        )

    if op in {"schedule_retrain", "plan_retrain"}:
        artifact_id = str(kw.get("artifact_id") or "").strip() or None
        family = str(kw.get("family") or "").strip() or None
        codes = _as_code_list(kw.get("codes"))
        only_flagged = _coerce_bool(kw.get("only_flagged"), False)
        items = await _list_model_registry_items(
            limit=max(1, min(int(kw.get("limit", 50) or 50), 200)),
            codes=codes or None,
            family=family,
            artifact_id=artifact_id,
            deployment_stage=str(kw.get("deployment_stage") or "").strip() or None,
            market_codes_only=_coerce_bool(kw.get("market_codes_only"), False),
            filter_market_codes=filter_market_codes,
        )
        if not items and artifact_id:
            return fail(f"model registry item not found: {artifact_id}")

        scanned_items = await _scan_model_registry_items(
            items,
            stability_floor=float(kw.get("stability_floor", DEFAULT_STABILITY_FLOOR) or DEFAULT_STABILITY_FLOOR),
            degradation_ceiling=float(kw.get("degradation_ceiling", DEFAULT_DEGRADATION_CEILING) or DEFAULT_DEGRADATION_CEILING),
            tight_race_gap=float(kw.get("tight_race_gap", DEFAULT_TIGHT_RACE_GAP) or DEFAULT_TIGHT_RACE_GAP),
            replay_success_floor=float(kw.get("replay_success_floor", DEFAULT_REPLAY_SUCCESS_FLOOR) or DEFAULT_REPLAY_SUCCESS_FLOOR),
        )
        target_items = [item for item in scanned_items if not only_flagged or list(item.get("flags") or [])]
        if not target_items:
            return fail("schedule_retrain 未找到满足条件的目标模型")

        reason_codes: list[str] = []
        generation_ids: list[str] = []
        plan_codes: list[str] = []
        target_models: list[dict[str, Any]] = []
        for item in target_items:
            plan_codes.extend(list(item.get("codes") or []))
            generation_id = str(item.get("source_generation_artifact_id") or "").strip()
            if generation_id:
                generation_ids.append(generation_id)
            for flag in list(item.get("flags") or []):
                if flag not in reason_codes:
                    reason_codes.append(str(flag))
            target_models.append(
                {
                    "artifact_id": item.get("artifact_id"),
                    "model_key": item.get("model_key"),
                    "deployment_stage": item.get("deployment_stage"),
                    "source_validation_artifact_id": item.get("source_validation_artifact_id"),
                    "source_generation_artifact_id": item.get("source_generation_artifact_id"),
                    "codes": list(item.get("codes") or []),
                    "flags": list(item.get("flags") or []),
                    "recommended_action": item.get("recommended_action"),
                }
            )
        plan_codes = list(dict.fromkeys([str(code).strip() for code in plan_codes if str(code).strip()]))
        target_generation_artifact_ids = list(dict.fromkeys([item for item in generation_ids if item]))
        plan_id = str(kw.get("output_artifact_id") or f"quant_retrain_plan_{int(time.time())}_{uuid4().hex[:8]}")
        created_at = _now_iso()
        plan_payload = {
            "artifact_id": plan_id,
            "plan_id": plan_id,
            "action": "model_registry",
            "op": "schedule_retrain",
            "status": "planned",
            "priority": "high" if reason_codes else "normal",
            "family": family or target_items[0].get("family"),
            "codes": codes or plan_codes,
            "target_model_count": len(target_models),
            "target_models": target_models,
            "target_generation_artifact_ids": target_generation_artifact_ids,
            "reason_codes": reason_codes or ["manual_retrain"],
            "next_action": "replay_factor_episode",
            "execution_mode": str(kw.get("execution_mode") or "plan_only").strip().lower() or "plan_only",
            "schedule_hint": str(kw.get("schedule_hint") or "manual_review").strip().lower() or "manual_review",
            "scheduler_status": "pending",
            "run_count": 0,
            "failure_count": 0,
            "created_at": created_at,
            "updated_at": created_at,
        }
        await register_artifact_async(
            {
                "artifact_id": plan_id,
                "strategy": MODEL_RETRAIN_PLAN_STRATEGY,
                "strategy_version": MODEL_RETRAIN_PLAN_VERSION,
                "code": ",".join(list(plan_payload.get("codes") or [])[:5]),
                "payload": plan_payload,
                "created_at": created_at,
            }
        )
        return ok(
            {"op": "schedule_retrain", "artifact_id": plan_id, "plan": plan_payload},
            source_chain=["services.artifact_registry", "quant_manager.model_registry"],
        )

    if op in {"retrain_list", "retrain_ls"}:
        items = await _list_retrain_plan_items(
            limit=max(1, min(int(kw.get("limit", 20) or 20), 100)),
            family=str(kw.get("family") or "").strip() or None,
            codes=_as_code_list(kw.get("codes")) or None,
        )
        return ok(
            {"op": "retrain_list", "count": len(items), "items": items, "summary": _summarize_retrain_plan_items(items)},
            source_chain=["services.artifact_registry", "quant_manager.model_registry"],
        )

    if op in {"retrain_summary", "retrain_stats"}:
        items = await _list_retrain_plan_items(
            limit=max(1, min(int(kw.get("limit", 200) or 200), 500)),
            family=str(kw.get("family") or "").strip() or None,
            codes=_as_code_list(kw.get("codes")) or None,
        )
        return ok(
            {"op": "retrain_summary", "summary": _summarize_retrain_plan_items(items)},
            source_chain=["services.artifact_registry", "quant_manager.model_registry"],
        )

    if op == "execute_retrain":
        plan_artifact_id = str(kw.get("artifact_id") or "").strip()
        if not plan_artifact_id:
            return fail("execute_retrain 需要 retrain plan artifact_id")
        plan_artifact = await get_artifact_async(plan_artifact_id)
        if not plan_artifact:
            return fail(f"retrain plan not found: {plan_artifact_id}")
        if str(plan_artifact.get("strategy") or "").strip().lower() != MODEL_RETRAIN_PLAN_STRATEGY:
            return fail(f"artifact {plan_artifact_id} is not {MODEL_RETRAIN_PLAN_STRATEGY}")

        plan_payload = _payload_from_artifact_row(plan_artifact)
        codes = _as_code_list(kw.get("codes")) or _as_code_list(plan_payload.get("codes"))
        lookback_bars = max(120, min(int(kw.get("lookback_bars", 220) or 220), 500))
        horizon_days = max(3, min(int(kw.get("horizon_days", 10) or 10), 30))
        max_dates = max(20, min(int(kw.get("max_dates", 60) or 60), 120))
        write_memory = _coerce_bool(kw.get("write_memory"), False)
        update_registry = _coerce_bool(kw.get("update_registry"), True)
        execution_mode = str(kw.get("execution_mode") or plan_payload.get("execution_mode") or "manual").strip().lower() or "manual"

        replay_artifact_ids: list[str] = []
        validation_artifact_ids: list[str] = []
        registry_artifact_ids: list[str] = []
        replay_run_count = 0
        validation_run_count = 0
        registry_update_count = 0
        target_models = [dict(item or {}) for item in list(plan_payload.get("target_models") or []) if isinstance(item, dict)]
        failures: list[dict[str, Any]] = []

        for target_model in target_models:
            source_generation_artifact_id = str(target_model.get("source_generation_artifact_id") or "").strip()
            if not source_generation_artifact_id:
                failures.append(
                    {
                        "artifact_id": target_model.get("artifact_id"),
                        "error": "missing source_generation_artifact_id",
                    }
                )
                continue
            replay_resp = await quant_manager_call(
                action="replay_factor_episode",
                kwargs={
                    "artifact_id": source_generation_artifact_id,
                    "codes": codes,
                    "lookback_bars": lookback_bars,
                    "horizon_days": horizon_days,
                    "max_dates": max_dates,
                    "write_memory": write_memory,
                    "persist_artifact": True,
                },
            )
            if not replay_resp.get("success"):
                failures.append(
                    {
                        "artifact_id": target_model.get("artifact_id"),
                        "error": replay_resp.get("error") or replay_resp.get("message") or "replay_factor_episode failed",
                    }
                )
                continue

            replay_data = replay_resp.get("data") if isinstance(replay_resp.get("data"), dict) else {}
            replay_artifact_id = str(replay_data.get("artifact_id") or "").strip()
            if replay_artifact_id:
                replay_artifact_ids.append(replay_artifact_id)
            replay_run_count += 1

            source_artifact = await get_artifact_async(source_generation_artifact_id)
            source_payload = _payload_from_artifact_row(source_artifact or {})
            source_candidates = [dict(item or {}) for item in list(source_payload.get("candidates") or []) if isinstance(item, dict)]
            validated_outcomes = [
                dict(item or {})
                for item in list(replay_data.get("outcomes") or [])
                if isinstance(item, dict) and str(item.get("status") or "").strip().lower() == "validated"
            ]
            validated_outcomes.sort(key=lambda item: _safe_float(item.get("total_score"), 0.0), reverse=True)
            if not validated_outcomes:
                continue

            best_outcome = validated_outcomes[0]
            candidate_index = int(best_outcome.get("candidate_index", 0) or 0)
            if candidate_index < 0 or candidate_index >= len(source_candidates):
                failures.append(
                    {
                        "artifact_id": target_model.get("artifact_id"),
                        "error": f"candidate_index out of range: {candidate_index}",
                    }
                )
                continue

            validation_resp = await quant_manager_call(
                action="validate_factor_candidate",
                kwargs={
                    "candidate": source_candidates[candidate_index],
                    "codes": codes,
                    "lookback_bars": lookback_bars,
                    "horizon_days": horizon_days,
                    "max_dates": max_dates,
                    "write_memory": write_memory,
                    "persist_artifact": True,
                },
            )
            if not validation_resp.get("success"):
                failures.append(
                    {
                        "artifact_id": target_model.get("artifact_id"),
                        "error": validation_resp.get("error") or validation_resp.get("message") or "validate_factor_candidate failed",
                    }
                )
                continue

            validation_run_count += 1
            validation_data = validation_resp.get("data") if isinstance(validation_resp.get("data"), dict) else {}
            validation_artifact_id = str(validation_data.get("artifact_id") or "").strip()
            if validation_artifact_id:
                validation_artifact_ids.append(validation_artifact_id)

            if update_registry and validation_artifact_id:
                validation_item = {
                    "artifact_id": validation_artifact_id,
                    "codes": list(codes),
                    "source_generation_artifact_id": source_generation_artifact_id,
                    "candidate": {
                        "name": source_candidates[candidate_index].get("name"),
                        "family": source_candidates[candidate_index].get("family"),
                    },
                    "rating": dict(validation_data.get("rating") or {}),
                }
                registry_entry = await _persist_model_registry_entry(
                    validation_item=validation_item,
                    deployment_stage="challenger",
                    review_status="retrain",
                    review_rank=1,
                    comparison_to_champion={"source_plan_id": plan_artifact_id},
                )
                registry_artifact_ids.append(str(registry_entry.get("artifact_id") or ""))
                registry_update_count += 1

        completed = validation_run_count > 0 and not failures
        status = "completed" if completed else ("partial" if validation_run_count > 0 else "failed")
        run_artifact_id = str(kw.get("output_artifact_id") or f"quant_retrain_run_{int(time.time())}_{uuid4().hex[:8]}")
        created_at = _now_iso()
        run_payload = {
            "artifact_id": run_artifact_id,
            "action": "model_registry",
            "op": "execute_retrain",
            "plan_id": plan_artifact_id,
            "source_plan_artifact_id": plan_artifact_id,
            "status": status,
            "execution_mode": execution_mode,
            "codes": list(codes),
            "replay_artifact_ids": replay_artifact_ids,
            "validation_artifact_ids": validation_artifact_ids,
            "registry_artifact_ids": registry_artifact_ids,
            "failures": failures,
            "execution_summary": {
                "target_model_count": len(target_models),
                "replay_run_count": replay_run_count,
                "validation_run_count": validation_run_count,
                "registry_update_count": registry_update_count,
                "failed_target_count": len(failures),
            },
            "created_at": created_at,
            "updated_at": created_at,
        }
        await register_artifact_async(
            {
                "artifact_id": run_artifact_id,
                "strategy": MODEL_RETRAIN_RUN_STRATEGY,
                "strategy_version": MODEL_RETRAIN_RUN_VERSION,
                "code": ",".join(list(codes)[:5]),
                "payload": run_payload,
                "created_at": created_at,
            }
        )

        updated_plan_payload = {
            **deepcopy(plan_payload),
            "status": "completed" if status == "completed" else plan_payload.get("status", "planned"),
            "last_run_status": status,
            "last_run_artifact_id": run_artifact_id,
            "run_count": int(plan_payload.get("run_count", 0) or 0) + 1,
            "updated_at": _now_iso(),
        }
        await register_artifact_async(
            {
                "artifact_id": plan_artifact_id,
                "strategy": MODEL_RETRAIN_PLAN_STRATEGY,
                "strategy_version": str(plan_artifact.get("strategy_version") or MODEL_RETRAIN_PLAN_VERSION),
                "code": plan_artifact.get("code") or ",".join(list(updated_plan_payload.get("codes") or [])[:5]),
                "payload": updated_plan_payload,
                "created_at": plan_artifact.get("created_at") or plan_payload.get("created_at") or created_at,
            }
        )
        return ok(
            {"op": "execute_retrain", "artifact_id": run_artifact_id, "plan": updated_plan_payload, "run": run_payload},
            source_chain=["services.artifact_registry", "quant_manager.replay_factor_episode", "quant_manager.validate_factor_candidate", "quant_manager.model_registry"],
        )

    if op == "retrain_status":
        artifact_id = str(kw.get("artifact_id") or "").strip()
        if not artifact_id:
            return fail("retrain_status 需要 artifact_id")
        artifact = await get_artifact_async(artifact_id)
        if not artifact:
            return fail(f"artifact not found: {artifact_id}")
        strategy = str(artifact.get("strategy") or "").strip().lower()
        payload = _payload_from_artifact_row(artifact)
        if strategy == MODEL_RETRAIN_PLAN_STRATEGY:
            plan = _normalize_retrain_plan_item(artifact, payload)
            runs = await _list_retrain_run_items(limit=max(1, min(int(kw.get("limit", 20) or 20), 100)), plan_id=artifact_id)
            latest_run = runs[0] if runs else None
            return ok(
                {"op": "retrain_status", "plan": plan, "latest_run": latest_run, "run_summary": _summarize_retrain_run_items(runs)},
                source_chain=["services.artifact_registry", "quant_manager.model_registry"],
            )
        if strategy == MODEL_RETRAIN_RUN_STRATEGY:
            run = _normalize_retrain_run_item(artifact, payload)
            plan_id = str(run.get("plan_id") or "").strip()
            plan_artifact = await get_artifact_async(plan_id) if plan_id else None
            plan_payload = _payload_from_artifact_row(plan_artifact or {}) if plan_artifact else {}
            runs = await _list_retrain_run_items(limit=max(1, min(int(kw.get("limit", 20) or 20), 100)), plan_id=plan_id or None)
            return ok(
                {
                    "op": "retrain_status",
                    "run": run,
                    "plan": _normalize_retrain_plan_item(plan_artifact, plan_payload) if plan_artifact else None,
                    "latest_run": runs[0] if runs else run,
                    "run_summary": _summarize_retrain_run_items(runs),
                },
                source_chain=["services.artifact_registry", "quant_manager.model_registry"],
            )
        return fail(f"artifact {artifact_id} is neither {MODEL_RETRAIN_PLAN_STRATEGY} nor {MODEL_RETRAIN_RUN_STRATEGY}")

    if op == "retrain_scheduler_status":
        scheduler = model_retrain_scheduler_module.get_model_retrain_scheduler()
        return ok(
            scheduler.status(),
            source_chain=["services.model_retrain_scheduler"],
        )

    if op == "retrain_scheduler_run_now":
        scheduler = model_retrain_scheduler_module.get_model_retrain_scheduler()
        result = await scheduler.run_once(
            reason=str(kw.get("reason") or "manual").strip() or "manual",
            force=_coerce_bool(kw.get("force"), False),
        )
        return ok(
            result,
            source_chain=["services.model_retrain_scheduler"],
        )

    return fail(
        "Unknown model_registry op. Supported: list|get|summary|lineage|feedback_sync|lifecycle_scan|schedule_retrain|retrain_list|retrain_summary|execute_retrain|retrain_status|retrain_scheduler_status|retrain_scheduler_run_now"
    )
