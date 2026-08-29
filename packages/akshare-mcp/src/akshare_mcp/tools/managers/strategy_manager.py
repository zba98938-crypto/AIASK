"""Strategy marketplace manager: CRUD, ranking, reviews, subscriptions, lifecycle.

This module is the public entry-point.  Heavy handler logic lives in
``strategy_mgr_crud``, ``strategy_mgr_lifecycle``, ``strategy_mgr_incubation``,
``strategy_mgr_runtime``, and ``strategy_mgr_helpers``.
"""

import json
import logging
import time
from typing import Any

from ...contracts.strategy_manager_contract import (
    STRATEGY_MANAGER_ACTIONS,
    STRATEGY_MANAGER_REQUIRED_PARAMS,
)
from ...storage import get_db
from ...utils import fail, ok
from ..manager_protocol import build_manager_meta

# ── Sub-module handler imports ───────────────────────────────────────────────
from .strategy_mgr_crud import (
    handle_archive,
    handle_ai_optimize_personal_strategy,
    handle_capabilities,
    handle_create,
    handle_daily_snapshot,
    handle_daily_snapshots,
    handle_delete_personal_strategy,
    handle_detail,
    handle_events,
    handle_fork_strategy,
    handle_get_forward_returns,
    handle_get_signal_stats,
    handle_get_signals,
    handle_help,
    handle_list,
    handle_my_strategies,
    handle_my_subscriptions,
    handle_personal_strategy_context,
    handle_personal_strategy_suggestions,
    handle_paper_session_get,
    handle_paper_session_get_or_create,
    handle_publish,
    handle_rank,
    handle_review,
    handle_review_report,
    handle_subscribe,
    handle_unsubscribe,
    handle_update_strategy,
    handle_update_metrics,
)
from .strategy_mgr_lifecycle import (
    handle_closure_review,
    handle_execution_audit_acceptance,
    handle_factory_dispatch_run,
    handle_factory_dispatch_status,
    handle_execution_audit_verification,
    handle_factory_run_detail,
    handle_factory_run_topn,
    handle_factory_run_once,
    handle_factory_runs,
    handle_factory_status,
    handle_factory_topn_latest,
    handle_incubation_overview,
    handle_lifecycle_scan,
    handle_review_report_recheck,
    handle_submission_replay,
    handle_submit,
)
from .strategy_mgr_incubation import (
    handle_incubation_accounts,
    handle_incubation_metrics,
    handle_incubation_pipeline,
    handle_incubation_pipeline_run,
    handle_incubation_sync_run,
    handle_paper_account,
    handle_paper_nav,
    handle_paper_orders,
)
from .strategy_mgr_runtime import (
    handle_domain_events,
    handle_domain_projection,
    handle_domain_projection_rebuild,
    handle_domain_projection_snapshot,
    handle_promotion_review_run,
    handle_promotion_reviews,
    handle_resolve_risk_event,
    handle_risk_events,
    handle_risk_recovery,
    handle_risk_scan_run,
    handle_risk_snapshots,
    handle_runtime_alert_ack,
    handle_runtime_alert_dispatch_run,
    handle_runtime_alerts,
    handle_runtime_control,
    handle_runtime_control_set,
    handle_runtime_cycle_run,
    handle_runtime_cycle_status,
)
from .strategy_mgr_helpers import (
    parse_bool,
)

logger = logging.getLogger(__name__)
_STRATEGY_MANAGER_IMPL = None


def _strategy_manager_error(code: str, message: str, *, detail: dict | None = None) -> dict:
    payload = fail(message)
    payload["message"] = message
    payload["error_code"] = code
    if detail is not None:
        payload["detail"] = detail
    return payload


def _validate_strategy_manager_params(action: str, params: dict) -> dict | None:
    required_any = STRATEGY_MANAGER_REQUIRED_PARAMS.get(action)
    if not required_any:
        return None
    for key in required_any:
        value = params.get(key)
        if value is None:
            continue
        if isinstance(value, str) and not value.strip():
            continue
        return None
    message = f"{'/'.join(required_any)} is required"
    return _strategy_manager_error(
        "STRATEGY_MANAGER_INVALID_PARAMS",
        message,
        detail={"action": action, "required_any_of": list(required_any)},
    )


def _infer_strategy_manager_error_code(message: str) -> str:
    text = str(message or "").strip().lower()
    if "not found" in text or "不存在" in text:
        return "STRATEGY_MANAGER_NOT_FOUND"
    if "required" in text or "must" in text or "invalid" in text or "不能为空" in text or "必须" in text:
        return "STRATEGY_MANAGER_INVALID_PARAMS"
    if "gate" in text or "门禁" in text:
        return "STRATEGY_MANAGER_GATE_FAILED"
    if "unsupported" in text or "unknown action" in text:
        return "STRATEGY_MANAGER_UNSUPPORTED"
    return "STRATEGY_MANAGER_BACKEND_ERROR"


def _normalize_strategy_manager_failure(action: str, result: dict) -> dict:
    if not isinstance(result, dict) or result.get("success") is not False:
        return result
    if result.get("error_code"):
        return result
    message = str(result.get("error") or result.get("message") or f"{action} failed")
    normalized = dict(result)
    normalized["message"] = message
    normalized["error_code"] = _infer_strategy_manager_error_code(message)
    normalized.setdefault("detail", {"action": action})
    return normalized


# ── Action dispatch table (supported_actions) ───────────────────────────────
# Maps action name -> async handler(db, params) -> dict
# Vector / AI actions are defined inline below because they're short and
# tightly coupled to register_strategy_manager.

ACTION_HANDLERS: dict[str, ...] = {
    # CRUD (strategy_mgr_crud)
    "help": handle_help,
    "create": handle_create,
    "publish": handle_publish,
    "archive": handle_archive,
    "list": handle_list,
    "detail": handle_detail,
    "review_report": handle_review_report,
    "events": handle_events,
    "update_metrics": handle_update_metrics,
    "review": handle_review,
    "subscribe": handle_subscribe,
    "unsubscribe": handle_unsubscribe,
    "my_subscriptions": handle_my_subscriptions,
    "my_strategies": handle_my_strategies,
    "fork_strategy": handle_fork_strategy,
    "personal_strategy_context": handle_personal_strategy_context,
    "personal_strategy_suggestions": handle_personal_strategy_suggestions,
    "update_strategy": handle_update_strategy,
    "delete_personal_strategy": handle_delete_personal_strategy,
    "paper_session_get": handle_paper_session_get,
    "paper_session_get_or_create": handle_paper_session_get_or_create,
    "rank": handle_rank,
    "capabilities": handle_capabilities,
    "daily_snapshot": handle_daily_snapshot,
    "daily_snapshots": handle_daily_snapshots,
    "get_signals": handle_get_signals,
    "get_forward_returns": handle_get_forward_returns,
    "get_signal_stats": handle_get_signal_stats,
    # Lifecycle (strategy_mgr_lifecycle)
    "review_report_recheck": handle_review_report_recheck,
    "submission_replay": handle_submission_replay,
    "submit": handle_submit,
    "lifecycle_scan": handle_lifecycle_scan,
    "incubation_overview": handle_incubation_overview,
    "closure_review": handle_closure_review,
    "factory_status": handle_factory_status,
    "factory_run_once": handle_factory_run_once,
    "factory_dispatch_run": handle_factory_dispatch_run,
    "factory_dispatch_status": handle_factory_dispatch_status,
    "factory_runs": handle_factory_runs,
    "factory_run_detail": handle_factory_run_detail,
    "factory_topn_latest": handle_factory_topn_latest,
    "factory_run_topn": handle_factory_run_topn,
    "execution_audit_verification": handle_execution_audit_verification,
    "execution_audit_acceptance": handle_execution_audit_acceptance,
    # Incubation (strategy_mgr_incubation)
    "incubation_accounts": handle_incubation_accounts,
    "incubation_metrics": handle_incubation_metrics,
    "paper_account": handle_paper_account,
    "paper_orders": handle_paper_orders,
    "paper_nav": handle_paper_nav,
    "incubation_sync_run": handle_incubation_sync_run,
    "incubation_pipeline": handle_incubation_pipeline,
    "incubation_pipeline_run": handle_incubation_pipeline_run,
    # Runtime (strategy_mgr_runtime)
    "risk_events": handle_risk_events,
    "risk_snapshots": handle_risk_snapshots,
    "risk_scan_run": handle_risk_scan_run,
    "risk_recovery": handle_risk_recovery,
    "resolve_risk_event": handle_resolve_risk_event,
    "runtime_alerts": handle_runtime_alerts,
    "runtime_alert_dispatch_run": handle_runtime_alert_dispatch_run,
    "runtime_alert_ack": handle_runtime_alert_ack,
    "runtime_control": handle_runtime_control,
    "runtime_control_set": handle_runtime_control_set,
    "promotion_reviews": handle_promotion_reviews,
    "promotion_review_run": handle_promotion_review_run,
    "runtime_cycle_status": handle_runtime_cycle_status,
    "runtime_cycle_run": handle_runtime_cycle_run,
    "domain_events": handle_domain_events,
    "domain_projection": handle_domain_projection,
    "domain_projection_snapshot": handle_domain_projection_snapshot,
    "domain_projection_rebuild": handle_domain_projection_rebuild,
    "ai_optimize_personal_strategy": handle_ai_optimize_personal_strategy,
}


def _normalize_strategy_manager_params(kwargs: Any = "{}", params: Any = None) -> dict:
    """Accept both legacy JSON-string kwargs and structured dict params."""
    candidate = params if params is not None else kwargs
    if candidate is None:
        return {}
    if isinstance(candidate, dict):
        return candidate
    if isinstance(candidate, str):
        try:
            parsed = json.loads(candidate)
        except Exception:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


# ── Vector / AI inline handlers (kept here to avoid circular deps) ───────────

async def _handle_vector_profiles(db, params: dict) -> dict:
    sid = str(params.get("strategy_id") or params.get("id") or "").strip() or None
    limit = min(max(int(params.get("limit", 20)), 1), 200)
    from ...services.vector_platform import get_strategy_vector_platform
    if params.get("similar_to"):
        rows = await get_strategy_vector_platform().find_similar_profiles(db, str(params.get("similar_to")), limit=limit)
    else:
        rows = await get_strategy_vector_platform().list_profiles(
            db,
            strategy_id=sid,
            profile_type=params.get("profile_type"),
            index_name=(str(params.get("index_name") or "").strip() or None),
            index_version=(str(params.get("index_version") or "").strip() or None),
            limit=limit,
        )
    return ok({"items": rows, "count": len(rows)})


async def _handle_vector_indexes(db, params: dict) -> dict:
    limit = min(max(int(params.get("limit", 20)), 1), 200)
    rows = await db.list_vector_index_registry(index_name=params.get("index_name"), status=params.get("status"), limit=limit) if hasattr(db, "list_vector_index_registry") else []
    items = [
        {
            **dict(item or {}),
            "source": dict(item or {}).get("source") or "legacy_registry",
            "source_of_truth": "legacy_strategy_vector_tables",
            "table_family": "legacy_strategy_vector_tables",
            "legacy_only": True,
        }
        for item in list(rows or [])
    ]
    return ok(
        {
            "items": items,
            "count": len(items),
            "source_of_truth": "legacy_strategy_vector_tables",
            "table_family": "legacy_strategy_vector_tables",
            "legacy_only": True,
        }
    )


async def _handle_vector_index_snapshots(db, params: dict) -> dict:
    limit = min(max(int(params.get("limit", 20)), 1), 200)
    from ...services.vector_platform import get_strategy_vector_platform
    rows = await get_strategy_vector_platform().list_index_snapshots(
        db,
        index_name=(str(params.get("index_name") or "").strip() or None),
        index_version=(str(params.get("index_version") or "").strip() or None),
        status=(str(params.get("status") or "").strip() or None),
        limit=limit,
    )
    latest = rows[0] if rows else None
    return ok({"items": rows, "count": len(rows), "latest": latest})


async def _handle_vector_ann_search(db, params: dict) -> dict:
    sid = str(params.get("strategy_id") or params.get("similar_to") or params.get("id") or "").strip()
    if not sid:
        return fail("strategy_id is required")
    limit = min(max(int(params.get("limit", 10)), 1), 50)
    from ...services.vector_platform import get_strategy_vector_platform
    result = await get_strategy_vector_platform().search_similar(
        db,
        sid,
        profile_type=str(params.get("profile_type") or 'behavior'),
        limit=limit,
        candidate_limit=min(max(int(params.get("candidate_limit", 80)), 1), 500),
        index_name=(str(params.get("index_name") or "").strip() or None),
        index_version=(str(params.get("index_version") or "").strip() or None),
    )
    return ok(result)


async def _handle_vector_reconcile(db, params: dict) -> dict:
    from ...services.vector_governance import get_strategy_vector_governance_service
    result = await get_strategy_vector_governance_service().reconcile_registry(
        db,
        index_name=(str(params.get("index_name") or "").strip() or None),
        profile_type=(str(params.get("profile_type") or "").strip() or None),
        limit_profiles=min(max(int(params.get("limit_profiles", 2000)), 1), 5000),
    )
    return ok(result)


async def _handle_vector_rebuild(db, params: dict) -> dict:
    from ...services.vector_governance import get_strategy_vector_governance_service
    statuses = params.get("statuses") or ['incubating', 'listed']
    if isinstance(statuses, str):
        statuses = [item.strip() for item in statuses.split(',') if item.strip()]
    result = await get_strategy_vector_governance_service().rebuild_index(
        db,
        index_name=str(params.get("index_name") or 'strategy_behavior'),
        index_version=(str(params.get("index_version") or "").strip() or None),
        statuses=list(statuses or ['incubating', 'listed']),
        limit=min(max(int(params.get("limit", 200)), 1), 1000),
        profile_type=str(params.get("profile_type") or 'behavior'),
        vector_method=(str(params.get("vector_method") or "").strip() or None),
    )
    return ok(result)


async def _handle_vector_health(db, params: dict) -> dict:
    from ...services.vector_platform import get_strategy_vector_platform
    result = await get_strategy_vector_platform().health_check(
        db,
        index_name=str(params.get("index_name") or 'strategy_behavior'),
        limit_versions=min(max(int(params.get("limit_versions", 20)), 1), 200),
        include_hnsw_indexes=parse_bool(params.get("include_hnsw_indexes"), False),
        include_embedding_smoke_check=parse_bool(
            params.get("include_embedding_smoke_check") or params.get("embedding_smoke_check"),
            False,
        ),
        force_embedding_smoke_check=parse_bool(
            params.get("force_embedding_smoke_check"),
            False,
        ),
    )
    if result.get("fallback_reason") == "health_unsupported":
        return fail("vector health unsupported")
    return ok(result)


def _empty_vector_cleanup_deleted() -> dict[str, int]:
    return {
        "vector_index_registry": 0,
        "vector_index_snapshots": 0,
        "vector_profiles": 0,
        "vector_profile_store": 0,
        "vector_index_items": 0,
        "vector_index_item_store": 0,
        "hnsw_indexes": 0,
    }


def _merge_vector_cleanup_deleted(target: dict[str, int], payload: dict | None) -> dict[str, int]:
    merged = dict(target or _empty_vector_cleanup_deleted())
    for key, value in dict(payload or {}).items():
        merged[key] = int(merged.get(key) or 0) + int(value or 0)
    return merged


def _unique_cleanup_values(values: list[Any]) -> list[str]:
    ordered: list[str] = []
    seen: set[str] = set()
    for item in list(values or []):
        normalized = str(item or "").strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        ordered.append(normalized)
    return ordered


def _decorate_legacy_cleanup_result(index_name: str, result: dict) -> dict:
    payload = dict(result or {})
    payload["index_name"] = str(payload.get("index_name") or index_name)
    payload["cleanup_scope"] = "legacy"
    payload["health_mode"] = "legacy"
    payload["source_of_truth"] = "legacy_strategy_vector_tables"
    payload["table_family"] = "legacy_strategy_vector_tables"
    payload["legacy_only"] = True
    payload.setdefault("target_version_keys", [str(item) for item in payload.get("target_versions") or [] if str(item).strip()])
    payload.setdefault("deleted", _empty_vector_cleanup_deleted())
    return payload


def _aggregate_vector_cleanup_results(
    *,
    index_name: str,
    requested_scope: str,
    executed_scope: str,
    scope_results: dict[str, dict],
    dry_run: bool,
    keep_versions: int,
    fallback_used: bool = False,
    fallback_reason: str | None = None,
) -> dict:
    ordered_scopes = [scope for scope in ("unified", "legacy") if scope in scope_results]
    if requested_scope != "both" and executed_scope in scope_results:
        summary = dict(scope_results[executed_scope] or {})
        summary["requested_scope"] = requested_scope
        summary["cleanup_scope"] = executed_scope
        summary["fallback_used"] = bool(fallback_used)
        summary["fallback_reason"] = fallback_reason
        if len(ordered_scopes) > 1:
            summary["scopes"] = dict(scope_results)
        return summary
    if len(ordered_scopes) == 1:
        summary = dict(scope_results[ordered_scopes[0]] or {})
        summary["requested_scope"] = requested_scope
        summary["cleanup_scope"] = executed_scope
        summary["fallback_used"] = bool(fallback_used)
        summary["fallback_reason"] = fallback_reason
        return summary

    protected_versions: list[str] = []
    target_versions: list[str] = []
    target_version_keys: list[str] = []
    hnsw_indexes_to_drop: list[str] = []
    version_details: list[dict] = []
    deleted = _empty_vector_cleanup_deleted()
    collections: list[dict] = []
    for scope in ordered_scopes:
        payload = dict(scope_results.get(scope) or {})
        protected_versions.extend(payload.get("protected_versions") or [])
        target_versions.extend(payload.get("target_versions") or [])
        target_version_keys.extend(payload.get("target_version_keys") or [])
        hnsw_indexes_to_drop.extend(payload.get("hnsw_indexes_to_drop") or [])
        version_details.extend(payload.get("version_details") or [])
        deleted = _merge_vector_cleanup_deleted(deleted, payload.get("deleted"))
        collections.extend(payload.get("collections") or [])

    return {
        "index_name": index_name,
        "requested_scope": requested_scope,
        "cleanup_scope": executed_scope,
        "health_mode": "mixed",
        "source_of_truth": "mixed_vector_tables",
        "table_family": "mixed_vector_tables",
        "legacy_only": False,
        "dry_run": bool(dry_run),
        "keep_versions": max(0, int(keep_versions or 0)),
        "protected_versions": _unique_cleanup_values(protected_versions),
        "target_versions": _unique_cleanup_values(target_versions),
        "target_version_keys": _unique_cleanup_values(target_version_keys),
        "hnsw_indexes_to_drop": _unique_cleanup_values(hnsw_indexes_to_drop),
        "deleted": deleted,
        "version_details": version_details,
        "collection_count": len(collections),
        "collections": collections,
        "scopes": dict(scope_results),
        "fallback_used": bool(fallback_used),
        "fallback_reason": fallback_reason,
    }


async def _handle_vector_cleanup(db, params: dict) -> dict:
    from ...services.vector_governance import get_strategy_vector_governance_service

    requested_scope = str(params.get("scope") or "").strip().lower() or "unified"
    if requested_scope not in {"unified", "legacy", "both"}:
        return fail("scope must be one of unified, legacy, both")
    protect_versions = params.get("protect_versions") or []
    if isinstance(protect_versions, str):
        protect_versions = [item.strip() for item in protect_versions.split(',') if item.strip()]
    index_name = str(params.get("index_name") or "strategy_behavior")
    keep_versions = max(int(params.get("keep_versions", 1)), 0)
    dry_run = parse_bool(params.get("dry_run"), True)
    cleanup_hnsw = parse_bool(params.get("cleanup_hnsw"), True)
    limit_versions = min(max(int(params.get("limit_versions", 200)), 1), 500)

    scope_results: dict[str, dict] = {}
    if requested_scope in {"unified", "both"}:
        scope_results["unified"] = await get_strategy_vector_governance_service().cleanup_unified_history(
            db,
            index_name=index_name,
            keep_versions=keep_versions,
            dry_run=dry_run,
            cleanup_hnsw=cleanup_hnsw,
            limit_versions=limit_versions,
            protect_versions=protect_versions,
        )
    if requested_scope in {"legacy", "both"}:
        if not hasattr(db, "cleanup_strategy_vector_history"):
            return fail("legacy vector cleanup unsupported")
        scope_results["legacy"] = _decorate_legacy_cleanup_result(
            index_name,
            await db.cleanup_strategy_vector_history(
                index_name=index_name,
                keep_versions=keep_versions,
                dry_run=dry_run,
                cleanup_hnsw=cleanup_hnsw,
                limit_versions=limit_versions,
                protect_versions=protect_versions,
            ),
        )

    executed_scope = requested_scope
    fallback_used = False
    fallback_reason = None
    if requested_scope == "unified":
        unified_result = dict(scope_results.get("unified") or {})
        unified_reason = str(unified_result.get("reason") or "").strip()
        if (
            unified_reason in {"no_unified_collections", "unified_cleanup_unsupported"}
            and hasattr(db, "cleanup_strategy_vector_history")
        ):
            scope_results["legacy"] = _decorate_legacy_cleanup_result(
                index_name,
                await db.cleanup_strategy_vector_history(
                    index_name=index_name,
                    keep_versions=keep_versions,
                    dry_run=dry_run,
                    cleanup_hnsw=cleanup_hnsw,
                    limit_versions=limit_versions,
                    protect_versions=protect_versions,
                ),
            )
            executed_scope = "legacy"
            fallback_used = True
            fallback_reason = unified_reason

    if not scope_results:
        return fail("vector cleanup unsupported")
    return ok(
        _aggregate_vector_cleanup_results(
            index_name=index_name,
            requested_scope=requested_scope,
            executed_scope=executed_scope,
            scope_results=scope_results,
            dry_run=dry_run,
            keep_versions=keep_versions,
            fallback_used=fallback_used,
            fallback_reason=fallback_reason,
        )
    )


async def _handle_ai_generate(db, params: dict) -> dict:
    from ...services.strategy_autonomy import get_strategy_autonomy_service
    result = await get_strategy_autonomy_service().run_cycle(
        db,
        snapshot=await db.get_daily_snapshot() if hasattr(db, "get_daily_snapshot") else None,
        limit=min(max(int(params.get("limit", 3)), 1), 10),
        source="strategy_manager",
        parent_strategy_id=(str(params.get("parent_strategy_id") or "").strip() or None),
        auto_submit=bool(params.get("auto_submit")),
    )
    return ok(result)


async def _handle_ai_experiments(db, params: dict) -> dict:
    experiment_id = str(params.get("experiment_id") or "").strip()
    if experiment_id:
        row = await db.get_strategy_generation_experiment(experiment_id) if hasattr(db, "get_strategy_generation_experiment") else None
        if not row:
            return fail(f"experiment not found: {experiment_id}")
        return ok(row)
    limit = min(max(int(params.get("limit", 20)), 1), 200)
    rows = await db.list_strategy_generation_experiments(
        strategy_id=(str(params.get("strategy_id") or params.get("id") or "").strip() or None),
        parent_strategy_id=(str(params.get("parent_strategy_id") or "").strip() or None),
        generated_strategy_id=(str(params.get("generated_strategy_id") or "").strip() or None),
        task_run_id=(int(params.get("task_run_id")) if params.get("task_run_id") is not None else None),
        status=(str(params.get("status") or "").strip() or None),
        source=(str(params.get("source") or "").strip() or None),
        limit=limit,
    ) if hasattr(db, "list_strategy_generation_experiments") else []
    return ok({"items": rows, "count": len(rows)})


async def _handle_task_runs(db, params: dict) -> dict:
    limit = min(max(int(params.get("limit", 20)), 1), 500)
    rows = await db.list_strategy_task_runs(
        strategy_id=(str(params.get("strategy_id") or params.get("id") or "").strip() or None),
        task_name=(str(params.get("task_name") or "").strip() or None),
        task_scope=(str(params.get("task_scope") or "").strip() or None),
        status=(str(params.get("status") or "").strip() or None),
        limit=limit,
    ) if hasattr(db, "list_strategy_task_runs") else []
    return ok({"items": rows, "count": len(rows)})


# NOTE: The 6 inline `_handle_factory_event_*` placeholders (list / create /
# update / approve / record_outcome / preview_tasks) were removed in PR-9
# (PROJECT_FIX_PLAN.md). They were registered into ACTION_HANDLERS at the
# end of this module and then immediately overwritten by the real
# implementations imported from `strategy_mgr_factory_events` (PR-2),
# making the inline versions dead code that only added noise. The
# strategy_manager contract guard (lines below) ensures the real handlers
# remain bound; if any of them go missing, RuntimeError is raised at
# import time.


# Register vector/AI handlers into dispatch table
ACTION_HANDLERS.update({
    "vector_profiles": _handle_vector_profiles,
    "vector_indexes": _handle_vector_indexes,
    "vector_index_snapshots": _handle_vector_index_snapshots,
    "vector_ann_search": _handle_vector_ann_search,
    "vector_reconcile": _handle_vector_reconcile,
    "vector_rebuild": _handle_vector_rebuild,
    "vector_health": _handle_vector_health,
    "vector_cleanup": _handle_vector_cleanup,
    "ai_generate": _handle_ai_generate,
    "ai_experiments": _handle_ai_experiments,
    "task_runs": _handle_task_runs,
    # NOTE: factory_event_* handlers are bound below from
    # strategy_mgr_factory_events (PR-2). The inline placeholders that used
    # to live here were removed in PR-9 because they were immediately
    # overwritten and never reached.
})

# Register factory event handlers (PR-2)
from .strategy_mgr_factory_events import (
    handle_factory_event_bootstrap,
    handle_factory_event_create,
    handle_factory_event_lineage,
    handle_factory_event_list,
    handle_factory_event_outbox_drain,
    handle_factory_event_outbox_status,
    handle_factory_event_update,
    handle_factory_event_approve,
    handle_factory_event_record_outcome,
    handle_factory_event_preview_tasks,
    handle_factory_theme_exposure_refresh,
    handle_factory_theme_exposure_status,
    handle_factory_theme_regression_run,
)

ACTION_HANDLERS.update({
    "factory_event_create": handle_factory_event_create,
    "factory_event_list": handle_factory_event_list,
    "factory_event_update": handle_factory_event_update,
    "factory_event_approve": handle_factory_event_approve,
    "factory_event_record_outcome": handle_factory_event_record_outcome,
    "factory_event_preview_tasks": handle_factory_event_preview_tasks,
    "factory_event_lineage": handle_factory_event_lineage,
    "factory_theme_exposure_status": handle_factory_theme_exposure_status,
    "factory_event_outbox_status": handle_factory_event_outbox_status,
    "factory_event_bootstrap": handle_factory_event_bootstrap,
    "factory_theme_exposure_refresh": handle_factory_theme_exposure_refresh,
    "factory_event_outbox_drain": handle_factory_event_outbox_drain,
    "factory_theme_regression_run": handle_factory_theme_regression_run,
})


_HANDLER_ACTION_SET = set(ACTION_HANDLERS)
_CONTRACT_ACTION_SET = set(STRATEGY_MANAGER_ACTIONS)
if _HANDLER_ACTION_SET != _CONTRACT_ACTION_SET:
    missing_from_contract = sorted(_HANDLER_ACTION_SET - _CONTRACT_ACTION_SET)
    missing_from_handlers = sorted(_CONTRACT_ACTION_SET - _HANDLER_ACTION_SET)
    raise RuntimeError(
        "strategy_manager contract mismatch: "
        f"missing_from_contract={missing_from_contract} "
        f"missing_from_handlers={missing_from_handlers}"
    )


# ── MCP tool registration ───────────────────────────────────────────────────

def register_strategy_manager(mcp):
    @mcp.tool()
    async def strategy_manager(action: str, kwargs: Any = "{}", params: Any = None) -> dict:
        """策略超市管理器 — 创建/发布/排名/评价/订阅/生命周期管理。

        Supports legacy ``kwargs`` JSON strings and structured ``params`` / dict kwargs.
        Actions: create, publish, archive, list, detail, update_metrics, review, subscribe, unsubscribe, my_subscriptions, my_strategies, fork_strategy, personal_strategy_context, personal_strategy_suggestions, update_strategy, delete_personal_strategy, paper_session_get, paper_session_get_or_create, rank, submit, lifecycle_scan, get_signals, get_forward_returns, get_signal_stats, factory_status, factory_run_once, factory_runs, factory_run_detail, execution_audit_verification, review_report, review_report_recheck, events, incubation_overview, vector_health, vector_cleanup, ai_optimize_personal_strategy, help
        """
        started_at = time.perf_counter()
        params = _normalize_strategy_manager_params(kwargs=kwargs, params=params)

        db = get_db()

        handler = ACTION_HANDLERS.get(action)
        if handler is None:
            result = _strategy_manager_error(
                "STRATEGY_MANAGER_INVALID_ACTION",
                f"Unknown action: {action}. Use action='help' for available actions.",
                detail={"action": action},
            )
            result["meta"] = build_manager_meta(
                tool_name="strategy_manager",
                action=action,
                started_at=started_at,
                source_chain=["strategy_manager"],
            )
            return result
        validation_error = _validate_strategy_manager_params(action, params)
        if validation_error is not None:
            validation_error["meta"] = build_manager_meta(
                tool_name="strategy_manager",
                action=action,
                started_at=started_at,
                source_chain=["strategy_manager"],
            )
            return validation_error
        result = await handler(db, params)
        result = _normalize_strategy_manager_failure(action, result)
        if isinstance(result, dict) and "meta" not in result:
            result["meta"] = build_manager_meta(
                tool_name="strategy_manager",
                action=action,
                started_at=started_at,
                source_chain=["strategy_manager"],
                extra={
                    "strategy_id": str(params.get("strategy_id") or params.get("id") or "").strip() or None,
                },
            )
        return result


class _StrategyManagerProbeMCP:
    """Minimal MCP stub used to expose the registered strategy_manager as a module-level callable."""

    def __init__(self):
        self.fn = None

    def tool(self, **kwargs):
        def _decorator(fn):
            self.fn = fn
            return fn

        return _decorator


def _get_strategy_manager_impl():
    global _STRATEGY_MANAGER_IMPL
    if _STRATEGY_MANAGER_IMPL is None:
        probe = _StrategyManagerProbeMCP()
        register_strategy_manager(probe)
        _STRATEGY_MANAGER_IMPL = probe.fn
    return _STRATEGY_MANAGER_IMPL


async def strategy_manager(action: str, kwargs: Any = "{}", params: Any = None) -> dict:
    """Module-level wrapper so internal services can import and call strategy_manager directly."""

    impl = _get_strategy_manager_impl()
    if impl is None:
        raise RuntimeError("strategy_manager implementation is unavailable")
    return await impl(action=action, kwargs=kwargs, params=params)


# ── Backward-compatible re-exports ───────────────────────────────────────────
# External services do ``from ..tools.managers.strategy_manager import _xxx``.
# We re-export the underscore-prefixed aliases from helpers / lifecycle.

from .strategy_mgr_helpers import (  # noqa: E402, F401
    LIFECYCLE_TRANSITIONS,
    _build_incubation_overview,
    _build_quality_report,
    _compute_nav_series,
    _get_latest_quality_report,
    _has_only_statistical_gate_failures,
    _is_factory_ai_prototype_strategy,
    _list_quality_reports,
    _maybe_grant_provisional_incubation,
    _metric_bucket_value,
    _normalize_quality_gate_result,
    _normalize_status_alias,
    _normalize_time_filter,
    _parse_bool,
    _quality_gate_reason_code,
    _safe_metric_value,
    _save_quality_report,
    _update_status,
    _validate_transition,
)
from .strategy_mgr_lifecycle import (  # noqa: E402, F401
    _lifecycle_scan,
    _run_quality_gate,
)
