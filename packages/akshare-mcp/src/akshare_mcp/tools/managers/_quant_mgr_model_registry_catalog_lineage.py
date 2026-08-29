"""Model registry lineage helpers for quant_manager."""

from __future__ import annotations

from typing import Any, Callable

from ...services import get_artifact_async
from ._quant_mgr_model_registry_catalog_common import (
    MODEL_REGISTRY_STRATEGY,
    MODEL_RETRAIN_PLAN_STRATEGY,
    MODEL_RETRAIN_RUN_STRATEGY,
    _as_code_list,
    _as_text_list,
    _list_model_registry_items,
    _list_retrain_plan_items,
    _list_retrain_run_items,
)
from .quant_mgr_artifact_common import _payload_from_artifact_row
from .quant_mgr_registry import _list_factor_candidate_registry_items


def _normalize_generation_artifact(artifact: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    research_episode = payload.get("research_episode") if isinstance(payload.get("research_episode"), dict) else {}
    return {
        "artifact_id": str(artifact.get("artifact_id") or payload.get("artifact_id") or ""),
        "created_at": artifact.get("created_at") or payload.get("created_at"),
        "updated_at": artifact.get("updated_at") or payload.get("updated_at"),
        "codes": _as_code_list(payload.get("codes")),
        "candidate_count": int(payload.get("candidate_count", 0) or 0),
        "generation_mode": str(payload.get("generation_mode") or "").strip().lower() or None,
        "provider": str(payload.get("provider") or "").strip() or None,
        "model": str(payload.get("model") or "").strip() or None,
        "theme": research_episode.get("theme"),
        "blocked_candidate_count": int(research_episode.get("candidate_count_blocked", 0) or 0),
    }


async def _load_generation_artifacts(artifact_ids: list[str]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    for artifact_id in [str(item).strip() for item in list(artifact_ids or []) if str(item).strip()]:
        if artifact_id in seen:
            continue
        seen.add(artifact_id)
        artifact = await get_artifact_async(artifact_id)
        if not artifact:
            continue
        if str(artifact.get("strategy") or "").strip().lower() != "quant_llm_factor_mining":
            continue
        payload = _payload_from_artifact_row(artifact)
        items.append(_normalize_generation_artifact(artifact, payload))
    items.sort(key=lambda item: str(item.get("updated_at") or item.get("created_at") or ""), reverse=True)
    return items


async def _build_model_registry_lineage(
    *,
    artifact_id: str | None,
    validation_artifact_ids: list[str] | None,
    generation_artifact_ids: list[str] | None,
    family: str | None,
    codes: list[str] | None,
    limit: int,
    market_codes_only: bool,
    filter_market_codes: Callable[[Any], list[str]],
) -> dict[str, Any]:
    resolved_artifact = await get_artifact_async(artifact_id) if artifact_id else None
    resolved_strategy = str((resolved_artifact or {}).get("strategy") or "").strip().lower()
    resolved_payload = _payload_from_artifact_row(resolved_artifact or {}) if resolved_artifact else {}

    requested_validation_ids = list(dict.fromkeys(_as_text_list(validation_artifact_ids)))
    requested_generation_ids = list(dict.fromkeys(_as_text_list(generation_artifact_ids)))

    if artifact_id and resolved_artifact:
        if resolved_strategy == MODEL_REGISTRY_STRATEGY:
            requested_validation_ids = list(
                dict.fromkeys(
                    [
                        *requested_validation_ids,
                        str(resolved_payload.get("source_validation_artifact_id") or "").strip(),
                    ]
                )
            )
            requested_generation_ids = list(
                dict.fromkeys(
                    [
                        *requested_generation_ids,
                        str(resolved_payload.get("source_generation_artifact_id") or "").strip(),
                    ]
                )
            )
        elif resolved_strategy == MODEL_RETRAIN_PLAN_STRATEGY:
            plan_targets = [dict(item or {}) for item in list(resolved_payload.get("target_models") or []) if isinstance(item, dict)]
            requested_validation_ids = list(
                dict.fromkeys(
                    [
                        *requested_validation_ids,
                        *[
                            str(item.get("source_validation_artifact_id") or "").strip()
                            for item in plan_targets
                            if str(item.get("source_validation_artifact_id") or "").strip()
                        ],
                    ]
                )
            )
            requested_generation_ids = list(
                dict.fromkeys(
                    [
                        *requested_generation_ids,
                        *[
                            str(item.get("source_generation_artifact_id") or "").strip()
                            for item in plan_targets
                            if str(item.get("source_generation_artifact_id") or "").strip()
                        ],
                        *[
                            str(item).strip()
                            for item in list(resolved_payload.get("target_generation_artifact_ids") or [])
                            if str(item).strip()
                        ],
                    ]
                )
            )
        elif resolved_strategy == MODEL_RETRAIN_RUN_STRATEGY:
            requested_validation_ids = list(
                dict.fromkeys(
                    [
                        *requested_validation_ids,
                        *[
                            str(item).strip()
                            for item in list(resolved_payload.get("validation_artifact_ids") or [])
                            if str(item).strip()
                        ],
                    ]
                )
            )
            if str(resolved_payload.get("plan_id") or "").strip():
                plan_artifact = await get_artifact_async(str(resolved_payload.get("plan_id") or "").strip())
                plan_payload = _payload_from_artifact_row(plan_artifact or {}) if plan_artifact else {}
                requested_generation_ids = list(
                    dict.fromkeys(
                        [
                            *requested_generation_ids,
                            *[
                                str(item).strip()
                                for item in list(plan_payload.get("target_generation_artifact_ids") or [])
                                if str(item).strip()
                            ],
                        ]
                    )
                )
        elif resolved_strategy == "quant_factor_candidate_validation":
            requested_validation_ids = list(dict.fromkeys([*requested_validation_ids, str(artifact_id).strip()]))
            source_generation_artifact_id = (
                str(resolved_payload.get("lineage", {}).get("source_generation_artifact_id") or "").strip()
                if isinstance(resolved_payload.get("lineage"), dict)
                else ""
            )
            if source_generation_artifact_id:
                requested_generation_ids = list(dict.fromkeys([*requested_generation_ids, source_generation_artifact_id]))
        elif resolved_strategy == "quant_llm_factor_mining":
            requested_generation_ids = list(dict.fromkeys([*requested_generation_ids, str(artifact_id).strip()]))

    requested_validation_ids = [item for item in requested_validation_ids if item]
    requested_generation_ids = [item for item in requested_generation_ids if item]

    candidate_items = await _list_factor_candidate_registry_items(
        limit=max(limit * 4, 40),
        codes=codes or None,
        family=family,
        recommendation=None,
        min_score=None,
        only_active=False,
        market_codes_only=market_codes_only,
        include_synthetic=False,
        filter_market_codes=filter_market_codes,
    )
    if requested_validation_ids:
        validation_set = set(requested_validation_ids)
        candidate_items = [item for item in candidate_items if str(item.get("artifact_id") or "").strip() in validation_set]
    if requested_generation_ids:
        generation_set = set(requested_generation_ids)
        candidate_items = [
            item
            for item in candidate_items
            if str(item.get("source_generation_artifact_id") or "").strip() in generation_set
            or str(item.get("artifact_id") or "").strip() in generation_set
        ]

    candidate_items = candidate_items[: max(1, limit)]
    resolved_validation_ids = list(
        dict.fromkeys(
            [
                *requested_validation_ids,
                *[str(item.get("artifact_id") or "").strip() for item in candidate_items if str(item.get("artifact_id") or "").strip()],
            ]
        )
    )
    resolved_generation_ids = list(
        dict.fromkeys(
            [
                *requested_generation_ids,
                *[
                    str(item.get("source_generation_artifact_id") or "").strip()
                    for item in candidate_items
                    if str(item.get("source_generation_artifact_id") or "").strip()
                ],
            ]
        )
    )

    model_items = await _list_model_registry_items(
        limit=max(limit * 4, 40),
        codes=codes or None,
        family=family,
        deployment_stage=None,
        artifact_id=artifact_id if resolved_strategy == MODEL_REGISTRY_STRATEGY else None,
        source_validation_artifact_ids=resolved_validation_ids or None,
        source_generation_artifact_ids=resolved_generation_ids or None,
        market_codes_only=market_codes_only,
        filter_market_codes=filter_market_codes,
    )
    plan_items = await _list_retrain_plan_items(
        limit=max(limit * 4, 40),
        artifact_id=artifact_id if resolved_strategy == MODEL_RETRAIN_PLAN_STRATEGY else None,
        family=family,
        codes=codes or None,
        source_validation_artifact_ids=resolved_validation_ids or None,
        source_generation_artifact_ids=resolved_generation_ids or None,
    )
    plan_ids = [
        str(item.get("artifact_id") or item.get("plan_id") or "").strip()
        for item in plan_items
        if str(item.get("artifact_id") or item.get("plan_id") or "").strip()
    ]
    run_items = await _list_retrain_run_items(
        limit=max(limit * 6, 60),
        artifact_id=artifact_id if resolved_strategy == MODEL_RETRAIN_RUN_STRATEGY else None,
        plan_ids=plan_ids or None,
    )
    generation_items = await _load_generation_artifacts(resolved_generation_ids)

    models_by_validation: dict[str, list[dict[str, Any]]] = {}
    for item in model_items:
        models_by_validation.setdefault(str(item.get("source_validation_artifact_id") or "").strip(), []).append(dict(item))

    plans_by_validation: dict[str, list[dict[str, Any]]] = {}
    plans_by_generation: dict[str, list[dict[str, Any]]] = {}
    for item in plan_items:
        artifact = await get_artifact_async(str(item.get("artifact_id") or item.get("plan_id") or "").strip())
        payload = _payload_from_artifact_row(artifact or {}) if artifact else {}
        target_models = [dict(target or {}) for target in list(payload.get("target_models") or []) if isinstance(target, dict)]
        validation_ids = {
            str(target.get("source_validation_artifact_id") or "").strip()
            for target in target_models
            if str(target.get("source_validation_artifact_id") or "").strip()
        }
        generation_ids = {
            str(target.get("source_generation_artifact_id") or "").strip()
            for target in target_models
            if str(target.get("source_generation_artifact_id") or "").strip()
        } | {
            str(target).strip()
            for target in list(payload.get("target_generation_artifact_ids") or [])
            if str(target).strip()
        }
        for validation_id in validation_ids:
            plans_by_validation.setdefault(validation_id, []).append(dict(item))
        for generation_id in generation_ids:
            plans_by_generation.setdefault(generation_id, []).append(dict(item))

    runs_by_plan_id: dict[str, list[dict[str, Any]]] = {}
    for item in run_items:
        runs_by_plan_id.setdefault(str(item.get("plan_id") or "").strip(), []).append(dict(item))

    lineage_items: list[dict[str, Any]] = []
    for candidate_item in candidate_items:
        validation_id = str(candidate_item.get("artifact_id") or "").strip()
        generation_id = str(candidate_item.get("source_generation_artifact_id") or "").strip()
        related_models = models_by_validation.get(validation_id, [])
        related_plans: list[dict[str, Any]] = []
        seen_plan_ids: set[str] = set()
        for plan in [*(plans_by_validation.get(validation_id, []) or []), *(plans_by_generation.get(generation_id, []) or [])]:
            plan_id = str(plan.get("artifact_id") or plan.get("plan_id") or "").strip()
            if not plan_id or plan_id in seen_plan_ids:
                continue
            seen_plan_ids.add(plan_id)
            related_plans.append(dict(plan))
        related_runs: list[dict[str, Any]] = []
        for plan in related_plans:
            related_runs.extend(list(runs_by_plan_id.get(str(plan.get("artifact_id") or plan.get("plan_id") or "").strip(), []) or []))
        retrain_statuses = list(
            dict.fromkeys(
                [
                    str(plan.get("status") or "").strip().lower()
                    for plan in related_plans
                    if str(plan.get("status") or "").strip()
                ]
            )
        )
        deployment_stages = list(
            dict.fromkeys(
                [
                    str(item.get("deployment_stage") or "").strip().lower()
                    for item in related_models
                    if str(item.get("deployment_stage") or "").strip()
                ]
            )
        )
        latest_run = (
            sorted(
                related_runs,
                key=lambda item: str(item.get("updated_at") or item.get("created_at") or ""),
                reverse=True,
            )[0]
            if related_runs
            else None
        )
        lineage_items.append(
            {
                "validation_artifact_id": validation_id,
                "source_generation_artifact_id": generation_id or None,
                "candidate_name": ((candidate_item.get("candidate") or {}).get("name") if isinstance(candidate_item.get("candidate"), dict) else None),
                "family": ((candidate_item.get("candidate") or {}).get("family") if isinstance(candidate_item.get("candidate"), dict) else None),
                "expected_regime": (
                    list((candidate_item.get("candidate") or {}).get("expected_regime") or [])
                    if isinstance(candidate_item.get("candidate"), dict)
                    else []
                ),
                "expected_holding_period": (
                    (candidate_item.get("candidate") or {}).get("expected_holding_period")
                    if isinstance(candidate_item.get("candidate"), dict)
                    else None
                ),
                "validation_params": dict(candidate_item.get("validation_params") or {}),
                "registry_stage": candidate_item.get("registry_stage"),
                "admission_blocked": bool(candidate_item.get("admission_blocked")),
                "latest_validation_at": candidate_item.get("latest_validation_at"),
                "model_registry_items": related_models,
                "deployment_stages": deployment_stages,
                "retrain_plans": related_plans,
                "retrain_runs": related_runs,
                "retrain_statuses": retrain_statuses,
                "latest_retrain_run": latest_run,
            }
        )

    lineage_items.sort(
        key=lambda item: (
            str(item.get("latest_validation_at") or ""),
            str(item.get("validation_artifact_id") or ""),
        ),
        reverse=True,
    )

    latest_retrain_at = None
    for run in run_items:
        observed_at = run.get("updated_at") or run.get("created_at")
        if observed_at and (latest_retrain_at is None or str(observed_at) > str(latest_retrain_at)):
            latest_retrain_at = observed_at

    return {
        "artifact_id": artifact_id,
        "query": {
            "artifact_id": artifact_id,
            "family": family,
            "codes": list(codes or []),
            "validation_artifact_ids": requested_validation_ids,
            "generation_artifact_ids": requested_generation_ids,
            "market_codes_only": market_codes_only,
        },
        "root": {
            "artifact_id": artifact_id,
            "strategy": resolved_strategy or None,
        },
        "generation_artifacts": generation_items,
        "candidate_items": candidate_items,
        "model_registry_items": model_items,
        "retrain_plans": plan_items,
        "retrain_runs": run_items,
        "items": lineage_items,
        "summary": {
            "candidate_count": len(candidate_items),
            "governed_candidate_count": len(
                [item for item in candidate_items if str(item.get("registry_stage") or "").strip().lower() == "governed"]
            ),
            "blocked_candidate_count": len([item for item in candidate_items if bool(item.get("admission_blocked"))]),
            "model_count": len(model_items),
            "champion_count": len(
                [item for item in model_items if str(item.get("deployment_stage") or "").strip().lower() == "champion"]
            ),
            "challenger_count": len(
                [item for item in model_items if str(item.get("deployment_stage") or "").strip().lower() == "challenger"]
            ),
            "retrain_plan_count": len(plan_items),
            "retrain_run_count": len(run_items),
            "latest_validation_at": max(
                [str(item.get("latest_validation_at") or "") for item in candidate_items if str(item.get("latest_validation_at") or "").strip()],
                default=None,
            ),
            "latest_retrain_at": latest_retrain_at,
        },
    }


__all__ = [name for name in globals() if name.startswith("_") or name.isupper()]
