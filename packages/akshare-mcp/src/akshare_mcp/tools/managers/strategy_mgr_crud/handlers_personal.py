"""Strategy manager CRUD action handlers."""

import asyncio
import os
import random
import time
from datetime import datetime, timezone
from uuid import uuid4

from ....storage import get_db
from ....utils import fail, ok
from ....services.strategy_lifecycle_shared.presentation import (
    build_favorite_state,
    build_owner_state,
    build_paper_session_state,
    build_strategy_presentation,
    is_admin_actor,
    is_personal_strategy,
    normalize_actor_roles,
)
from ..strategy_mgr_helpers import (
    build_factory_capability_health,
    build_incubation_overview,
    compute_nav_series,
    get_latest_quality_report,
    list_quality_reports,
    normalize_quality_report_contract,
    normalize_status_alias,
    parse_bool,
    normalize_time_filter,
    update_status,
)

import logging

logger = logging.getLogger(__name__)

from ._support import (
    _actor_context,
    _build_strategy_incubation_surface,
    _build_strategy_market_summary,
    _clean_string_list,
    _closure_snapshot_overview_payload,
    _enrich_rank_strategy,
    _ensure_personal_strategy_mutation_allowed,
    _execution_audit_entity_chain_available,
    _extract_strategy_market_summary_value,
    _incubation_surface_bool,
    _incubation_surface_issue_count,
    _load_latest_vector_index_snapshot,
    _load_personal_strategy_surface_state,
    _load_signal_quality_registry_snapshot,
    _load_similar_vector_profiles,
    _load_strategy_incubation_surface,
    _load_vector_profiles,
    _normalize_personal_strategy_focus_fields,
    _normalize_strategy_status_value,
    _resolve_incubation_surface_stage,
    _resolve_strategy_incubation_overview,
    _resolve_strategy_status_filter,
    _resolved,
    _sanitize_personal_strategy_snapshot,
    _strategy_source_strategy_id,
    _trimmed,
)
from ._personal_support import (
    _build_personal_strategy_change_plan,
    _build_personal_strategy_context,
    _extract_personal_strategy_backtest_metrics,
    _personal_strategy_pipeline_warning,
    _personal_strategy_update_changed,
    _recompile_personal_strategy_runtime,
    _rerun_personal_strategy_backtest,
    _run_personal_strategy_post_update_pipeline,
    _select_personal_strategy_update_fields,
)

async def handle_fork_strategy(db, params: dict) -> dict:
    sid = _trimmed(params.get("strategy_id") or params.get("id"))
    if not sid:
        return fail("strategy_id is required")
    actor_id, actor_roles = _actor_context(params)
    if not actor_id:
        return fail("actor_id is required")
    parent = await db.get_strategy(sid)
    if not parent:
        return fail(f"Strategy not found: {sid}")
    fork_id = f"strat_{int(time.time())}_{uuid4().hex[:8]}"
    parent_tags = [str(item or "").strip() for item in list(parent.get("tags") or []) if str(item or "").strip()]
    tags = list(dict.fromkeys([*parent_tags, "personal_strategy", "forked_strategy"]))
    parent_params = dict(parent.get("params") or {})
    metadata = dict(parent_params.get("metadata") or {})
    metadata.update({
        "source_strategy_id": sid,
        "forked_at": datetime.now(timezone.utc).isoformat(),
        "forked_by": actor_id,
    })
    fork_params = {
        **parent_params,
        "metadata": metadata,
    }
    data = {
        "id": fork_id,
        "name": f"{parent.get('name') or sid} · 我的版本",
        "description": parent.get("description") or "",
        "author_id": actor_id,
        "strategy_type": str(parent.get("strategy_type") or "custom"),
        "params": fork_params,
        "factor_weights": dict(parent.get("factor_weights") or {}),
        "status": "draft",
        "tags": tags,
        "backtest_artifact_id": parent.get("backtest_artifact_id"),
    }
    strategy = await db.save_strategy(data)
    if hasattr(db, "save_strategy_lineage"):
        await db.save_strategy_lineage(
            fork_id,
            sid,
            "user_fork",
            {"source": "strategy_market", "actor_id": actor_id, "actor_roles": actor_roles},
        )
    owner_state, favorite_state, paper_session_state = await _load_personal_strategy_surface_state(
        db,
        strategy,
        actor_id=actor_id,
        actor_roles=actor_roles,
    )
    return ok({
        "strategy_id": fork_id,
        "source_strategy_id": sid,
        "strategy": strategy,
        "owner_state": owner_state,
        "favorite_state": favorite_state,
        "paper_session_state": paper_session_state,
    })


async def handle_personal_strategy_context(db, params: dict) -> dict:
    sid = _trimmed(params.get("strategy_id") or params.get("id"))
    if not sid:
        return fail("strategy_id is required")
    strategy = await db.get_strategy(sid) if hasattr(db, "get_strategy") else None
    if not strategy:
        return fail(f"Strategy not found: {sid}")
    actor_id, actor_roles = _actor_context(params)
    owner_state, favorite_state, paper_session_state = await _load_personal_strategy_surface_state(
        db,
        strategy,
        actor_id=actor_id,
        actor_roles=actor_roles,
    )
    return ok(_build_personal_strategy_context(
        strategy,
        actor_id=actor_id,
        actor_roles=actor_roles,
        owner_state=owner_state,
        favorite_state=favorite_state,
        paper_session_state=paper_session_state,
    ))


async def handle_personal_strategy_suggestions(db, params: dict) -> dict:
    sid = _trimmed(params.get("strategy_id") or params.get("id"))
    if not sid:
        return fail("strategy_id is required")
    actor_id, actor_roles = _actor_context(params)
    strategy = await db.get_strategy(sid) if hasattr(db, "get_strategy") else None
    error = _ensure_personal_strategy_mutation_allowed(
        strategy,
        actor_id=actor_id,
        actor_roles=actor_roles,
    )
    if error:
        return fail(error)
    owner_state, favorite_state, paper_session_state = await _load_personal_strategy_surface_state(
        db,
        strategy,
        actor_id=actor_id,
        actor_roles=actor_roles,
    )
    plan = _build_personal_strategy_change_plan(
        strategy,
        params,
        mode="suggest",
        actor_id=actor_id,
        actor_roles=actor_roles,
        owner_state=owner_state,
        favorite_state=favorite_state,
        paper_session_state=paper_session_state,
    )
    persist = parse_bool(params.get("persist"), False)
    run_post_update_pipeline = parse_bool(params.get("run_post_update_pipeline"), persist)
    if not persist:
        return ok({
            "strategy_id": sid,
            "advisory_only": True,
            "persisted": False,
            **plan,
        })
    apply_payload = dict(plan.get("apply_payload") or {})
    updated = (
        await db.update_strategy_fields(sid, apply_payload)
        if apply_payload and hasattr(db, "update_strategy_fields")
        else strategy
    )
    if not updated:
        return fail(f"Strategy not found: {sid}")
    post_update_pipeline = {
        "requested": False,
        "overall_status": "skipped",
    }
    pipeline_notes: list[str] = []
    if run_post_update_pipeline:
        post_update_pipeline, updated, pipeline_notes = await _run_personal_strategy_post_update_pipeline(
            db,
            updated,
        )
    owner_state, favorite_state, paper_session_state = await _load_personal_strategy_surface_state(
        db,
        updated,
        actor_id=actor_id,
        actor_roles=actor_roles,
    )
    return ok({
        "strategy_id": sid,
        "advisory_only": False,
        "persisted": True,
        **plan,
        "risk_notes": [
            *list(plan.get("risk_notes") or []),
            *pipeline_notes,
        ],
        "post_update_pipeline": post_update_pipeline,
        "strategy": updated,
        "context": _build_personal_strategy_context(
            updated,
            actor_id=actor_id,
            actor_roles=actor_roles,
            owner_state=owner_state,
            favorite_state=favorite_state,
            paper_session_state=paper_session_state,
        ),
    })


async def handle_update_strategy(db, params: dict) -> dict:
    sid = _trimmed(params.get("strategy_id") or params.get("id"))
    if not sid:
        return fail("strategy_id is required")
    actor_id, actor_roles = _actor_context(params)
    strategy = await db.get_strategy(sid)
    error = _ensure_personal_strategy_mutation_allowed(
        strategy,
        actor_id=actor_id,
        actor_roles=actor_roles,
    )
    if error:
        return fail(error)
    updates = dict(params.get("updates") or {})
    for field in ("name", "description", "params", "factor_weights", "tags", "backtest_artifact_id"):
        if field in params:
            updates[field] = params.get(field)
    if "tags" in updates:
        updates["tags"] = list(dict.fromkeys(
            [str(item or "").strip() for item in list(updates.get("tags") or []) if str(item or "").strip()]
        ))
    updated = await db.update_strategy_fields(sid, updates) if hasattr(db, "update_strategy_fields") else None
    if not updated:
        return fail(f"Strategy not found: {sid}")
    post_update_pipeline = {
        "requested": False,
        "overall_status": "skipped",
    }
    pipeline_notes: list[str] = []
    if parse_bool(params.get("run_post_update_pipeline"), False):
        post_update_pipeline, updated, pipeline_notes = await _run_personal_strategy_post_update_pipeline(
            db,
            updated,
        )
    owner_state, favorite_state, paper_session_state = await _load_personal_strategy_surface_state(
        db,
        updated,
        actor_id=actor_id,
        actor_roles=actor_roles,
    )
    return ok({
        "strategy_id": sid,
        "strategy": updated,
        "owner_state": owner_state,
        "favorite_state": favorite_state,
        "paper_session_state": paper_session_state,
        "post_update_pipeline": post_update_pipeline,
        "risk_notes": pipeline_notes,
    })


async def handle_delete_personal_strategy(db, params: dict) -> dict:
    sid = _trimmed(params.get("strategy_id") or params.get("id"))
    if not sid:
        return fail("strategy_id is required")
    actor_id, actor_roles = _actor_context(params)
    strategy = await db.get_strategy(sid)
    error = _ensure_personal_strategy_mutation_allowed(
        strategy,
        actor_id=actor_id,
        actor_roles=actor_roles,
    )
    if error:
        return fail(error)
    await update_status(
        db,
        sid,
        "archived",
        actor_id=actor_id or "strategy_manager",
        reason="personal_strategy_deleted",
    )
    return ok({"strategy_id": sid, "archived": True, "status": "archived"})


async def handle_paper_session_get(db, params: dict) -> dict:
    sid = _trimmed(params.get("strategy_id") or params.get("id"))
    if not sid:
        return fail("strategy_id is required")
    actor_id, actor_roles = _actor_context(params)
    if not actor_id:
        return fail("actor_id is required")
    strategy = await db.get_strategy(sid)
    if not strategy:
        return fail(f"Strategy not found: {sid}")
    session = await db.get_strategy_paper_session(sid, actor_id) if hasattr(db, "get_strategy_paper_session") else None
    state = build_paper_session_state(session, actor_id=actor_id)
    return ok({
        "strategy_id": sid,
        "strategy_name": strategy.get("name"),
        "session": session,
        "paper_session_state": state,
    })


async def handle_paper_session_get_or_create(db, params: dict) -> dict:
    sid = _trimmed(params.get("strategy_id") or params.get("id"))
    if not sid:
        return fail("strategy_id is required")
    actor_id, actor_roles = _actor_context(params)
    if not actor_id:
        return fail("actor_id is required")
    strategy = await db.get_strategy(sid)
    if not strategy:
        return fail(f"Strategy not found: {sid}")
    existing = await db.get_strategy_paper_session(sid, actor_id) if hasattr(db, "get_strategy_paper_session") else None
    created = False
    session = existing
    if existing and hasattr(db, "touch_strategy_paper_session"):
        session = await db.touch_strategy_paper_session(sid, actor_id) or existing
    if not existing:
        account_id = f"pp_{uuid4().hex[:8]}"
        account = await db.save_paper_account({
            "id": account_id,
            "user_id": actor_id,
            "name": f"个人策略模拟盘_{strategy.get('name') or sid}",
            "initial_capital": 100000,
            "current_capital": 100000,
            "total_value": 100000,
            "account_type": "personal_strategy",
            "status": "active",
        }) if hasattr(db, "save_paper_account") else {"id": account_id}
        session = await db.save_strategy_paper_session({
            "strategy_id": sid,
            "user_id": actor_id,
            "account_id": account.get("id") or account_id,
            "session_type": "personal_paper",
            "source_strategy_id": _strategy_source_strategy_id(strategy),
            "last_used_at": datetime.now(timezone.utc).isoformat(),
        }) if hasattr(db, "save_strategy_paper_session") else None
        created = True
    account = await db.get_paper_account(session.get("account_id")) if session and hasattr(db, "get_paper_account") else None
    payload = dict(session or {})
    if account:
        payload.setdefault("account_name", account.get("name"))
        payload.setdefault("account_status", account.get("status"))
    state = build_paper_session_state(payload, actor_id=actor_id)
    return ok({
        "strategy_id": sid,
        "strategy_name": strategy.get("name"),
        "created": created,
        "session": payload or None,
        "account": account,
        "paper_session_state": state,
    })


async def handle_rank(db, params: dict) -> dict:
    from ...services.ranking import rrf_rank, DEFAULT_RANK_KEYS

    status = _resolve_strategy_status_filter(params.get("status"), default="visible")
    strategy_type = params.get("strategy_type") or params.get("type")
    limit = min(max(int(params.get("limit", 50)), 1), 200)
    offset = max(int(params.get("offset", 0)), 0)
    rank_keys = params.get("rank_keys")

    # F-N22-3 fix (诊断报告 §N42 rank): sort_by 枚举校验。
    # 历史问题: sort_by=nonexistent_metric_zzz 未报错，静默 fallback 到 rrf_score。
    # 修复: 非法 sort_by 显式回显告警（不静默），合法单指标作为 rank_keys 使用。
    sort_by = str(params.get("sort_by") or "").strip()
    sort_by_warning = None
    _valid_sort_keys = set(DEFAULT_RANK_KEYS) | {"rank", "rrf_score"}
    if sort_by and sort_by not in _valid_sort_keys:
        sort_by_warning = (
            f"sort_by='{sort_by}' 非法（支持 {sorted(_valid_sort_keys)}），"
            f"已回退默认 RRF 多指标排序"
        )
    elif sort_by and sort_by in set(DEFAULT_RANK_KEYS) and not rank_keys:
        rank_keys = [sort_by]

    fetch_limit = limit + offset
    strategies = await db.list_strategies(status, strategy_type, fetch_limit, 0)
    if not strategies:
        result = {"strategies": [], "count": 0, "offset": offset, "limit": limit}
        if sort_by_warning:
            result["sort_by_warning"] = sort_by_warning
        return ok(result)

    semaphore = asyncio.Semaphore(8)
    enriched = await asyncio.gather(*[
        _enrich_rank_strategy(db, strategy, semaphore)
        for strategy in strategies
    ])

    ranked = rrf_rank(enriched, rank_keys)
    page = ranked[offset:offset + limit]
    result = {"strategies": page, "count": len(ranked), "offset": offset, "limit": limit}
    if sort_by_warning:
        result["sort_by_warning"] = sort_by_warning
    return ok(result)


async def handle_ai_optimize_personal_strategy(db, params: dict) -> dict:
    sid = _trimmed(params.get("strategy_id") or params.get("id"))
    if not sid:
        return fail("strategy_id is required")
    actor_id, actor_roles = _actor_context(params)
    strategy = await db.get_strategy(sid)
    error = _ensure_personal_strategy_mutation_allowed(
        strategy,
        actor_id=actor_id,
        actor_roles=actor_roles,
    )
    if error:
        return fail(error)
    owner_state, favorite_state, paper_session_state = await _load_personal_strategy_surface_state(
        db,
        strategy,
        actor_id=actor_id,
        actor_roles=actor_roles,
    )
    plan = _build_personal_strategy_change_plan(
        strategy,
        params,
        mode="optimize",
        actor_id=actor_id,
        actor_roles=actor_roles,
        owner_state=owner_state,
        favorite_state=favorite_state,
        paper_session_state=paper_session_state,
    )
    before = dict(plan.get("before") or {})
    started_at = datetime.now(timezone.utc)
    task_run = await db.save_strategy_task_run({
        "strategy_id": sid,
        "task_name": "ai_optimize_personal_strategy",
        "task_scope": "strategy_market.personal_strategy",
        "task_key": f"{sid}:ai_optimize_personal_strategy",
        "status": "running",
        "trace_id": f"ai_opt_{uuid4().hex[:12]}",
        "payload": {
            "strategy_id": sid,
            "actor_id": actor_id,
            "before": before,
            "objective": plan.get("objective"),
            "instructions": plan.get("instructions"),
            "focus_fields": list(plan.get("focus_fields") or []),
        },
        "started_at": started_at.isoformat(),
    }) if hasattr(db, "save_strategy_task_run") else None
    experiment_id = f"sge_{uuid4().hex[:16]}"
    try:
        apply_payload = dict(plan.get("apply_payload") or {})
        if apply_payload:
            updated = await db.update_strategy_fields(sid, apply_payload) if hasattr(db, "update_strategy_fields") else None
            if not updated:
                raise ValueError(f"Strategy not found: {sid}")
        else:
            updated = strategy
        post_update_pipeline, updated, pipeline_notes = await _run_personal_strategy_post_update_pipeline(
            db,
            updated,
        )
        after = {
            "name": updated.get("name"),
            "description": updated.get("description"),
            "params": dict(updated.get("params") or {}),
            "factor_weights": dict(updated.get("factor_weights") or {}),
            "tags": list(updated.get("tags") or []),
        }
        changed_fields = list(plan.get("changed_fields") or [])
        if hasattr(db, "save_strategy_generation_experiment"):
            await db.save_strategy_generation_experiment({
                "experiment_id": experiment_id,
                "strategy_id": sid,
                "generated_strategy_id": sid,
                "task_run_id": task_run.get("id") if task_run else None,
                "source": "strategy_manager.personal_strategy",
                "generator_type": "personal_strategy_optimizer",
                "optimizer_type": "heuristic",
                "status": "completed",
                "hypothesis": "Improve personal strategy readiness with stronger metadata and normalized weights.",
                "parameters": {
                    "actor_id": actor_id,
                    "requested_changes": list(changed_fields),
                    "objective": plan.get("objective"),
                    "instructions": plan.get("instructions"),
                    "focus_fields": list(plan.get("focus_fields") or []),
                },
                "strategy_spec": {"before": before, "after": after},
                "evaluation": {"changed_fields": changed_fields},
                "result": after,
            })
        if task_run and hasattr(db, "update_strategy_task_run"):
            await db.update_strategy_task_run(
                int(task_run.get("id")),
                status="success",
                result={
                    "strategy_id": sid,
                    "experiment_id": experiment_id,
                    "changed_fields": changed_fields,
                    "summary": plan.get("summary"),
                    "after": after,
                    "post_update_pipeline": post_update_pipeline,
                },
                completed_at=datetime.now(timezone.utc).isoformat(),
            )
        response_context = _build_personal_strategy_context(
            updated,
            actor_id=actor_id,
            actor_roles=actor_roles,
            owner_state=owner_state,
            favorite_state=favorite_state,
            paper_session_state=paper_session_state,
        )
        return ok({
            "strategy_id": sid,
            "task_run_id": task_run.get("id") if task_run else None,
            "experiment_id": experiment_id,
            "before": before,
            "after": after,
            "changed_fields": changed_fields,
            "summary": plan.get("summary"),
            "suggestions": plan.get("suggestions"),
            "risk_notes": [
                *list(plan.get("risk_notes") or []),
                *pipeline_notes,
            ],
            "post_update_pipeline": post_update_pipeline,
            "context": response_context,
            "strategy": updated,
        })
    except Exception as exc:
        if task_run and hasattr(db, "update_strategy_task_run"):
            await db.update_strategy_task_run(
                int(task_run.get("id")),
                status="failed",
                error=str(exc),
                completed_at=datetime.now(timezone.utc).isoformat(),
            )
        return fail(f"ai optimize failed: {exc}")


async def handle_capabilities(db, params: dict) -> dict:
    from strategy_factory.api import get_factory_constants

    factory_constants = get_factory_constants()
    high_confidence_feature_flags = dict(factory_constants.get("HIGH_CONFIDENCE_FEATURE_FLAGS") or {})
    latest_run = await db.get_latest_strategy_factory_run() if hasattr(db, "get_latest_strategy_factory_run") else None
    capability_health = build_factory_capability_health(
        db,
        factory_constants=factory_constants,
        latest_run=latest_run,
    )
    return ok({
        "daily_snapshot": hasattr(db, "get_daily_snapshot") and hasattr(db, "list_daily_snapshots"),
        "factory_runs": hasattr(db, "save_strategy_factory_run") and hasattr(db, "get_latest_strategy_factory_run"),
        "factory_dispatch": hasattr(db, "create_strategy_factory_dispatch") and hasattr(db, "get_strategy_factory_dispatch"),
        "factory_bulk_lane": hasattr(db, "list_stock_universe") and hasattr(db, "save_strategy_factory_run"),
        "factory_bulk_lane_enabled": bool(factory_constants.get("STOCK_STRATEGY_MATRIX_ENABLED")),
        "factory_pre_gate_enabled": bool(factory_constants.get("FACTORY_PRE_GATE_ENABLED")),
        "high_confidence_enabled": bool(factory_constants.get("STRATEGY_FACTORY_HIGH_CONFIDENCE_ENABLED")),
        "evidence_contract_enabled": bool(factory_constants.get("STRATEGY_FACTORY_EVIDENCE_CONTRACT_ENABLED")),
        "confidence_diagnostics_enabled": bool(
            factory_constants.get("STRATEGY_FACTORY_CONFIDENCE_DIAGNOSTICS_ENABLED")
        ),
        "execution_audit_enabled": bool(factory_constants.get("STRATEGY_FACTORY_EXECUTION_AUDIT_ENABLED")),
        "execution_audit_verification": hasattr(db, "get_execution_audit_verification"),
        "quality_ui_v2_enabled": bool(factory_constants.get("STRATEGY_FACTORY_QUALITY_UI_V2_ENABLED")),
        "research_protocol_v2_enabled": bool(
            factory_constants.get("STRATEGY_FACTORY_RESEARCH_PROTOCOL_V2_ENABLED")
        ),
        "gate_model_v2_enabled": bool(factory_constants.get("STRATEGY_FACTORY_GATE_MODEL_V2_ENABLED")),
        "trace_ledger_v2_enabled": bool(factory_constants.get("STRATEGY_FACTORY_TRACE_LEDGER_V2_ENABLED")),
        "feedback_v2_enabled": bool(factory_constants.get("STRATEGY_FACTORY_FEEDBACK_V2_ENABLED")),
        "trace_ledger_v2_implemented": True,
        "governance_gate_report_v2_implemented": True,
        "execution_audit_entity_chain_available": _execution_audit_entity_chain_available(db),
        "spec_completeness_mode": str(
            factory_constants.get("STRATEGY_FACTORY_SPEC_COMPLETENESS_MODE") or "warn"
        ),
        "high_confidence_feature_flags": high_confidence_feature_flags,
        "signal_quality_registry": _load_signal_quality_registry_snapshot(),
        "paper_incubation": hasattr(db, "save_strategy_incubation_account") and hasattr(db, "save_strategy_incubation_metric"),
        "paper_trading": hasattr(db, "save_paper_account") and hasattr(db, "save_paper_order") and hasattr(db, "get_paper_nav_rows"),
        "incubation_pipeline": hasattr(db, "save_strategy_incubation_pipeline_snapshot") and hasattr(db, "list_strategy_incubation_metrics"),
        "runtime_risk": hasattr(db, "save_strategy_runtime_risk_event"),
        "risk_snapshots": hasattr(db, "save_strategy_runtime_risk_snapshot") and hasattr(db, "list_strategy_runtime_risk_snapshots"),
        "risk_recovery": hasattr(db, "save_strategy_runtime_risk_snapshot") and hasattr(db, "save_strategy_runtime_control"),
        "execution_risk": hasattr(db, "save_strategy_runtime_risk_event"),
        "runtime_controls": hasattr(db, "save_strategy_runtime_control") and hasattr(db, "get_strategy_runtime_control"),
        "runtime_alerting": hasattr(db, "save_strategy_runtime_alert") and hasattr(db, "list_strategy_runtime_alerts"),
        "signal_event_snapshots": hasattr(db, "save_strategy_signal_event_snapshot") and hasattr(db, "list_strategy_signal_event_snapshots"),
        "promotion_pipeline": hasattr(db, "save_strategy_promotion_review") and hasattr(db, "get_latest_strategy_promotion_review"),
        "projection_snapshots": hasattr(db, "save_strategy_projection_snapshot") and hasattr(db, "get_latest_strategy_projection_snapshot"),
        "event_replay": hasattr(db, "save_strategy_projection_snapshot") and hasattr(db, "list_strategy_domain_events"),
        "vector_platform": hasattr(db, "save_strategy_vector_profile") and hasattr(db, "save_vector_index_registry"),
        "vector_governance": hasattr(db, "save_vector_index_registry") and hasattr(db, "list_strategy_vector_profiles"),
        "persistent_vector_index": hasattr(db, "save_strategy_vector_index_snapshot") and hasattr(db, "replace_strategy_vector_index_items"),
        "ann_vector_search": hasattr(db, "list_strategy_vector_index_items") and hasattr(db, "save_strategy_vector_index_snapshot"),
        "vector_health": hasattr(db, "get_strategy_vector_health") or (
            hasattr(db, "list_vector_collections") and hasattr(db, "list_vector_index_snapshots")
        ),
        "vector_cleanup": hasattr(db, "cleanup_strategy_vector_history") or hasattr(db, "cleanup_vector_collection_history"),
        "ai_generation": hasattr(db, "save_strategy_generation_experiment") and hasattr(db, "save_strategy_task_run"),
        "multi_agent_review": hasattr(db, "save_strategy_generation_experiment"),
        "quality_governance": hasattr(db, "save_strategy_quality_report") and hasattr(db, "list_strategy_status_events"),
        "domain_events": hasattr(db, "save_strategy_domain_event") and hasattr(db, "list_strategy_domain_events"),
        "domain_projection": hasattr(db, "list_strategy_status_events") and hasattr(db, "list_strategy_domain_events"),
        "runtime_cycle": hasattr(db, "save_strategy_task_run") and hasattr(db, "save_strategy_incubation_metric"),
        "personal_strategy_crud": hasattr(db, "list_user_strategies") and hasattr(db, "update_strategy_fields"),
        "personal_paper_sessions": hasattr(db, "save_strategy_paper_session") and hasattr(db, "get_strategy_paper_session"),
        "capability_health": capability_health,
    })


async def handle_daily_snapshot(db, params: dict) -> dict:
    snapshot_date = params.get("snapshot_date")
    row = await db.get_daily_snapshot(snapshot_date) if hasattr(db, "get_daily_snapshot") else None
    if not row:
        return fail("daily snapshot not found")
    return ok(row)


async def handle_daily_snapshots(db, params: dict) -> dict:
    limit = min(max(int(params.get("limit", 20)), 1), 200)
    rows = await db.list_daily_snapshots(
        limit=limit,
        start_date=params.get("start_date"),
        end_date=params.get("end_date"),
    ) if hasattr(db, "list_daily_snapshots") else []
    return ok({"items": rows, "count": len(rows)})


async def handle_get_signals(db, params: dict) -> dict:
    sid = str(params.get("strategy_id") or params.get("id") or "").strip()
    if not sid:
        return fail("strategy_id is required")
    user_id = str(params.get("user_id", "default"))
    limit = min(max(int(params.get("limit", 100)), 1), 500)
    is_sub = await db.is_subscribed(sid, user_id)
    if is_sub:
        signals = await db.get_signals(sid, limit=limit)
    else:
        signals = await db.get_signals_public(sid, limit=limit)
    return ok({"signals": signals, "count": len(signals), "subscriber": is_sub})


async def handle_get_forward_returns(db, params: dict) -> dict:
    sid = str(params.get("strategy_id") or params.get("id") or "").strip()
    if not sid:
        return fail("strategy_id is required")
    extra_kwargs = {}
    if params.get("lookback_days") is not None:
        extra_kwargs["lookback_days"] = int(params.get("lookback_days"))
    if params.get("eps") is not None:
        extra_kwargs["eps"] = float(params.get("eps"))
    stats = await db.get_signal_stats(sid, **extra_kwargs)
    return ok(stats)


async def handle_get_signal_stats(db, params: dict) -> dict:
    sid = str(params.get("strategy_id") or params.get("id") or "").strip()
    if not sid:
        return fail("strategy_id is required")
    extra_kwargs = {}
    if params.get("lookback_days") is not None:
        extra_kwargs["lookback_days"] = int(params.get("lookback_days"))
    if params.get("eps") is not None:
        extra_kwargs["eps"] = float(params.get("eps"))
    stats = await db.get_signal_stats(sid, **extra_kwargs)
    return ok(stats)
