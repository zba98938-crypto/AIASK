"""Replay, feature-store, and history handlers for quant_manager artifacts."""

from __future__ import annotations

import time
from datetime import datetime
from typing import Any, Callable
from uuid import uuid4

import numpy as np

from ...services import get_artifact_async, list_artifacts_async, register_artifact
from ...storage import get_db
from .quant_mgr_artifact_common import QuantManagerCall, _payload_from_artifact_row
from .quant_mgr_helpers import _as_code_list, _filter_quant_artifacts, _safe_float


def _normalize_episode_item(artifact: dict, payload: dict) -> dict:
    summary = payload.get("episode_summary") if isinstance(payload.get("episode_summary"), dict) else {}
    return {
        "artifact_id": str(artifact.get("artifact_id") or payload.get("artifact_id") or ""),
        "strategy": str(artifact.get("strategy") or payload.get("strategy") or ""),
        "strategy_version": str(artifact.get("strategy_version") or payload.get("strategy_version") or ""),
        "created_at": artifact.get("created_at") or payload.get("created_at"),
        "updated_at": artifact.get("updated_at") or payload.get("updated_at"),
        "source_artifact_id": payload.get("source_artifact_id"),
        "codes": _as_code_list(payload.get("codes")),
        "candidate_limit": int(payload.get("candidate_limit", 0) or 0),
        "validated_count": int(summary.get("validated_count", 0) or 0),
        "failed_count": int(summary.get("failed_count", 0) or 0),
        "grade_counts": summary.get("grade_counts") if isinstance(summary.get("grade_counts"), dict) else {},
        "recommendation_counts": (
            summary.get("recommendation_counts") if isinstance(summary.get("recommendation_counts"), dict) else {}
        ),
        "best_candidate": summary.get("best_candidate") if isinstance(summary.get("best_candidate"), dict) else {},
        "worst_candidate": summary.get("worst_candidate") if isinstance(summary.get("worst_candidate"), dict) else {},
    }


async def _list_replay_episode_items(
    *,
    limit: int = 20,
    codes: list[str] | None = None,
    source_artifact_id: str | None = None,
) -> list[dict]:
    fetch_limit = max(50, min(1000, int(limit) * 12))
    rows = await list_artifacts_async(limit=fetch_limit)
    summary_rows = rows if isinstance(rows, list) else []
    items = []
    for row in summary_rows:
        if str(row.get("strategy") or "").strip().lower() != "quant_factor_episode_replay":
            continue
        artifact_id = str(row.get("artifact_id") or "").strip()
        if not artifact_id:
            continue
        artifact = await get_artifact_async(artifact_id)
        if not artifact:
            continue
        payload = _payload_from_artifact_row(artifact)
        record_codes = _as_code_list(payload.get("codes"))
        record_source_artifact_id = str(payload.get("source_artifact_id") or "").strip()
        if codes and not (set(codes) & set(record_codes)):
            continue
        if source_artifact_id and record_source_artifact_id != str(source_artifact_id).strip():
            continue
        items.append(_normalize_episode_item(artifact, payload))

    items.sort(
        key=lambda item: (
            int(item.get("validated_count", 0) or 0),
            str(item.get("updated_at") or item.get("created_at") or ""),
        ),
        reverse=True,
    )
    return items[: max(1, int(limit))]


def _summarize_replay_episode_items(items: list[dict]) -> dict:
    validated_counts = []
    failed_counts = []
    for item in list(items or []):
        validated_counts.append(int(item.get("validated_count", 0) or 0))
        failed_counts.append(int(item.get("failed_count", 0) or 0))
    replayed_counts = [v + f for v, f in zip(validated_counts, failed_counts)]
    success_rates = [(v / max(1, v + f)) for v, f in zip(validated_counts, failed_counts)]
    return {
        "count": len(list(items or [])),
        "avg_validated_count": round(float(np.mean(validated_counts)), 6) if validated_counts else 0.0,
        "avg_failed_count": round(float(np.mean(failed_counts)), 6) if failed_counts else 0.0,
        "avg_replayed_count": round(float(np.mean(replayed_counts)), 6) if replayed_counts else 0.0,
        "avg_success_rate": round(float(np.mean(success_rates)), 6) if success_rates else 0.0,
        "max_validated_count": max(validated_counts) if validated_counts else 0,
    }


async def handle_replay_factor_episode(
    *,
    kw: dict[str, Any],
    ok: Callable[..., dict],
    fail: Callable[..., dict],
    quant_manager_call: QuantManagerCall,
) -> dict:
    op = str(kw.get("op", "run") or "run").strip().lower()

    if op in {"list", "ls"}:
        codes = _as_code_list(kw.get("codes"))
        source_artifact_id = str(kw.get("source_artifact_id") or "").strip() or None
        limit = max(1, min(int(kw.get("limit", 20) or 20), 100))
        items = await _list_replay_episode_items(limit=limit, codes=codes or None, source_artifact_id=source_artifact_id)
        return ok(
            {"op": "list", "count": len(items), "items": items, "summary": _summarize_replay_episode_items(items)},
            source_chain=["services.artifact_registry", "quant_manager.replay_factor_episode"],
        )

    if op in {"summary", "stats"}:
        codes = _as_code_list(kw.get("codes"))
        source_artifact_id = str(kw.get("source_artifact_id") or "").strip() or None
        limit = max(1, min(int(kw.get("limit", 200) or 200), 500))
        items = await _list_replay_episode_items(limit=limit, codes=codes or None, source_artifact_id=source_artifact_id)
        return ok(
            {"op": "summary", "summary": _summarize_replay_episode_items(items)},
            source_chain=["services.artifact_registry", "quant_manager.replay_factor_episode"],
        )

    if op in {"get", "detail"}:
        artifact_id = str(kw.get("artifact_id") or "").strip()
        if not artifact_id:
            return fail("replay_factor_episode get 需要 artifact_id")
        artifact = await get_artifact_async(artifact_id)
        if not artifact:
            return fail(f"artifact not found: {artifact_id}")
        if str(artifact.get("strategy") or "").strip().lower() != "quant_factor_episode_replay":
            return fail(f"artifact {artifact_id} is not quant_factor_episode_replay")
        payload = _payload_from_artifact_row(artifact)
        return ok(
            {"op": "get", "item": _normalize_episode_item(artifact, payload), "artifact": artifact},
            source_chain=["services.artifact_registry", "quant_manager.replay_factor_episode"],
        )

    artifact_id = str(kw.get("artifact_id") or "").strip()
    if not artifact_id:
        return fail("replay_factor_episode 需要 artifact_id")

    artifact = await get_artifact_async(artifact_id)
    if not artifact:
        return fail(f"artifact not found: {artifact_id}")

    strategy = str(artifact.get("strategy") or "").strip().lower()
    payload = _payload_from_artifact_row(artifact)
    if strategy != "quant_llm_factor_mining":
        return fail(f"artifact {artifact_id} is not quant_llm_factor_mining")

    source_codes = _as_code_list(payload.get("codes"))
    replay_codes = _as_code_list(kw.get("codes")) or source_codes
    if len(replay_codes) < 4:
        return fail("replay_factor_episode 至少需要 4 个 codes，可通过 kwargs.codes 覆盖")

    candidates = [dict(item) for item in list(payload.get("candidates") or []) if isinstance(item, dict)]
    if not candidates:
        return fail(f"artifact {artifact_id} does not contain candidates")

    lookback_bars = max(
        120,
        min(int(kw.get("lookback_bars", (payload.get("params") or {}).get("lookback_bars", 220)) or 220), 500),
    )
    horizon_days = max(3, min(int(kw.get("horizon_days", (payload.get("params") or {}).get("horizon_days", 10)) or 10), 30))
    max_dates = max(20, min(int(kw.get("max_dates", 60) or 60), 120))
    write_memory = bool(kw.get("write_memory", False))
    persist_artifact = bool(kw.get("persist_artifact", True))
    candidate_limit = max(1, min(int(kw.get("candidate_limit", len(candidates)) or len(candidates)), len(candidates)))

    outcomes = []
    grade_counts = {}
    recommendation_counts = {}
    success_count = 0
    failed_count = 0
    best_outcome = None
    worst_outcome = None

    for idx, candidate_item in enumerate(candidates[:candidate_limit]):
        validation_resp = await quant_manager_call(
            action="validate_factor_candidate",
            kwargs={
                "candidate": candidate_item,
                "codes": replay_codes,
                "lookback_bars": lookback_bars,
                "horizon_days": horizon_days,
                "max_dates": max_dates,
                "persist_artifact": False,
                "write_memory": write_memory,
            },
        )
        if validation_resp.get("success"):
            data = validation_resp.get("data", {}) if isinstance(validation_resp.get("data"), dict) else {}
            rating = data.get("rating") if isinstance(data.get("rating"), dict) else {}
            grade = str(rating.get("grade") or "").strip().upper()
            recommendation = str(rating.get("recommendation") or "").strip().lower()
            total_score = _safe_float(rating.get("total_score"), 0.0)
            row = {
                "candidate_index": idx,
                "name": candidate_item.get("name"),
                "family": candidate_item.get("family"),
                "status": "validated",
                "grade": grade,
                "recommendation": recommendation,
                "total_score": total_score,
                "rank_ic_mean": _safe_float((data.get("metrics") or {}).get("rank_ic_mean"), 0.0),
                "warnings": list(data.get("warnings") or []),
            }
            outcomes.append(row)
            success_count += 1
            if grade:
                grade_counts[grade] = int(grade_counts.get(grade, 0)) + 1
            if recommendation:
                recommendation_counts[recommendation] = int(recommendation_counts.get(recommendation, 0)) + 1
            if best_outcome is None or total_score > _safe_float(best_outcome.get("total_score"), -1e9):
                best_outcome = row
            if worst_outcome is None or total_score < _safe_float(worst_outcome.get("total_score"), 1e9):
                worst_outcome = row
        else:
            failed_count += 1
            fail_payload = validation_resp.get("data", {}) if isinstance(validation_resp.get("data"), dict) else {}
            outcomes.append(
                {
                    "candidate_index": idx,
                    "name": candidate_item.get("name"),
                    "family": candidate_item.get("family"),
                    "status": "failed",
                    "stage": fail_payload.get("stage"),
                    "error": validation_resp.get("error") or validation_resp.get("message") or "candidate validation failed",
                }
            )

    output_artifact_id = str(kw.get("output_artifact_id") or f"factor_episode_{int(time.time())}_{uuid4().hex[:8]}")
    replay_payload = {
        "artifact_id": output_artifact_id,
        "action": "replay_factor_episode",
        "source_artifact_id": artifact_id,
        "codes": replay_codes,
        "candidate_limit": candidate_limit,
        "params": {
            "lookback_bars": lookback_bars,
            "horizon_days": horizon_days,
            "max_dates": max_dates,
            "write_memory": write_memory,
        },
        "episode_summary": {
            "input_candidate_count": len(candidates),
            "replayed_candidate_count": len(outcomes),
            "validated_count": success_count,
            "failed_count": failed_count,
            "grade_counts": grade_counts,
            "recommendation_counts": recommendation_counts,
            "best_candidate": best_outcome,
            "worst_candidate": worst_outcome,
        },
        "outcomes": outcomes,
        "source_generation_summary": {
            "provider": payload.get("provider"),
            "model": payload.get("model"),
            "generation_mode": payload.get("generation_mode"),
            "dedup_summary": payload.get("dedup_summary") if isinstance(payload.get("dedup_summary"), dict) else {},
            "blocked_candidates": list(payload.get("blocked_candidates") or []),
        },
    }

    if persist_artifact:
        register_artifact(
            {
                "artifact_id": output_artifact_id,
                "strategy": "quant_factor_episode_replay",
                "strategy_version": "p2.v2",
                "code": ",".join(replay_codes[:5]),
                "payload": replay_payload,
                "created_at": datetime.now().isoformat(),
            }
        )

    return ok(
        replay_payload,
        source_chain=["services.artifact_registry", "quant_manager.validate_factor_candidate"],
    )


async def handle_feature_store(
    *,
    kw: dict[str, Any],
    code: str | None,
    ok: Callable[..., dict],
    fail: Callable[..., dict],
    quant_manager_call: QuantManagerCall,
) -> dict:
    op = str(kw.get("op", "list") or "list").strip().lower()
    if op in {"snapshot", "create"}:
        codes = _as_code_list(kw.get("codes"))
        if not codes and code:
            codes = [code]
        if not codes:
            return fail("feature_store snapshot 需要 code 或 codes")

        factors = kw.get(
            "factors",
            [
                "momentum",
                "value",
                "quality",
                "volatility",
                "liquidity",
                "sentiment",
                "event",
                "capital_flow",
                "alternative_composite",
            ],
        )
        snapshot_rows = []
        for one_code in codes:
            fac_res = await quant_manager_call(action="calculate_factors", code=one_code, factors=factors)
            if fac_res.get("success"):
                snapshot_rows.append(
                    {
                        "code": one_code,
                        "factors": fac_res.get("data", {}).get("factors", {}),
                        "composite_score": fac_res.get("data", {}).get("composite_score"),
                    }
                )

        artifact_id = str(kw.get("artifact_id") or f"feature_store_{int(time.time())}_{uuid4().hex[:8]}")
        payload = {
            "artifact_id": artifact_id,
            "strategy": "feature_store_snapshot",
            "strategy_version": "p2.v1",
            "code": ",".join(codes[:5]),
            "snapshot_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "codes": codes,
            "factors": factors,
            "rows": snapshot_rows,
            "count": len(snapshot_rows),
        }
        register_artifact(payload)
        return ok(
            {"op": "snapshot", "artifact_id": artifact_id, "count": len(snapshot_rows), "codes": codes},
            source_chain=["quant_manager.calculate_factors", "services.artifact_registry"],
        )

    if op in {"get", "detail"}:
        artifact_id = str(kw.get("artifact_id") or "").strip()
        if not artifact_id:
            return fail("feature_store get 需要 artifact_id")
        artifact = await get_artifact_async(artifact_id)
        if not artifact:
            return fail(f"artifact not found: {artifact_id}")
        return ok({"op": "get", "artifact": artifact}, source_chain=["services.artifact_registry"])

    if op in {"list", "ls"}:
        limit = int(kw.get("limit", 20) or 20)
        items = await list_artifacts_async(limit=max(1, min(200, limit)))
        filtered = _filter_quant_artifacts(items if isinstance(items, list) else [])
        return ok({"op": "list", "items": filtered, "count": len(filtered)}, source_chain=["services.artifact_registry"])

    if op in {"track", "log"}:
        artifact_id = str(kw.get("artifact_id") or f"quant_exp_{int(time.time())}_{uuid4().hex[:8]}")
        payload = {
            "artifact_id": artifact_id,
            "strategy": "quant_experiment",
            "strategy_version": str(kw.get("strategy_version") or "p2.v1"),
            "code": str(kw.get("code") or code or ""),
            "name": str(kw.get("name") or ""),
            "params": kw.get("params") if isinstance(kw.get("params"), dict) else {},
            "metrics": kw.get("metrics") if isinstance(kw.get("metrics"), dict) else {},
            "notes": str(kw.get("notes") or ""),
            "tracked_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        register_artifact(payload)
        return ok({"op": "track", "artifact_id": artifact_id}, source_chain=["services.artifact_registry"])

    return fail("Unknown feature_store op. Supported: snapshot|get|list|track")


async def handle_replay_experiment(
    *,
    kw: dict[str, Any],
    ok: Callable[..., dict],
    fail: Callable[..., dict],
    quant_manager_call: QuantManagerCall,
) -> dict:
    artifact_id = str(kw.get("artifact_id") or "").strip()
    if not artifact_id:
        return fail("需要 artifact_id")

    artifact = await get_artifact_async(artifact_id)
    if not artifact:
        return fail(f"artifact not found: {artifact_id}")

    strategy = str(artifact.get("strategy") or "").lower()
    payload = artifact.get("payload") if isinstance(artifact.get("payload"), dict) else artifact
    if strategy != "quant_automl_discovery":
        return fail(f"artifact {artifact_id} is not quant_automl_discovery")

    params = payload.get("params", {}) if isinstance(payload, dict) else {}
    replay_codes = _as_code_list(payload.get("codes")) or _as_code_list(params.get("codes")) or _as_code_list(kw.get("codes"))
    if not replay_codes:
        return fail("replay requires codes in artifact or kwargs")

    replay_action = await quant_manager_call(
        action="automl_discovery",
        kwargs={
            "codes": replay_codes,
            "horizon_days": params.get("horizon_days", 10),
            "lookback_bars": params.get("lookback_bars", 160),
            "top_k_features": params.get("top_k_features", 6),
            "train_ratio": params.get("train_ratio", 0.7),
            "include_alternative": params.get("include_alternative", True),
            "alt_lookback_days": params.get("alt_lookback_days", 30),
            "persist_artifact": False,
            "run_anchor_oos": bool(kw.get("run_anchor_oos", True)),
        },
    )
    if not replay_action.get("success"):
        return replay_action

    old_metrics = (payload.get("metrics") or {}) if isinstance(payload, dict) else {}
    new_metrics = replay_action.get("data", {}).get("metrics", {})
    metric_delta = {}
    for metric_name in ("test_ic", "hit_rate", "long_short_return"):
        old_value = _safe_float(old_metrics.get(metric_name), 0.0)
        new_value = _safe_float(new_metrics.get(metric_name), 0.0)
        metric_delta[metric_name] = {"old": old_value, "new": new_value, "delta": float(new_value - old_value)}

    return ok(
        {
            "artifact_id": artifact_id,
            "replay_metrics": new_metrics,
            "metric_delta": metric_delta,
            "replay_result": replay_action.get("data"),
        },
        source_chain=["services.artifact_registry", "quant_manager.automl_discovery"],
    )


async def handle_factor_ic_history(
    *,
    kw: dict[str, Any],
    ok: Callable[..., dict],
    fail: Callable[..., dict],
    get_db_fn: Callable[[], Any] = get_db,
) -> dict:
    factor_name = str(kw.get("factor_name", "")).strip()
    period = str(kw.get("period", "20"))
    limit = min(max(int(kw.get("limit", 60)), 1), 500)
    if not factor_name:
        return fail("factor_name is required")
    db = get_db_fn()
    rows = await db.get_factor_ic_history(factor_name, period, limit)
    return ok(
        {
            "factor_name": factor_name,
            "period": period,
            "history": [
                {
                    "date": str(row.get("ic_date", "")),
                    "ic_value": row.get("ic_value"),
                    "rank_ic": row.get("rank_ic"),
                    "stock_count": row.get("stock_count"),
                }
                for row in rows
            ],
            "count": len(rows),
        }
    )
