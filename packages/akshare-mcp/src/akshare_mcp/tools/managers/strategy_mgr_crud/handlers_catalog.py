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
    _build_personal_strategy_context,
)

async def handle_help(db, params: dict) -> dict:
    return ok({
        "actions": [
            "create", "publish", "archive", "list", "detail",
            "update_metrics", "review", "subscribe", "unsubscribe",
            "my_subscriptions", "my_strategies", "fork_strategy", "personal_strategy_context", "personal_strategy_suggestions", "update_strategy", "delete_personal_strategy",
            "paper_session_get", "paper_session_get_or_create",
            "rank", "submit", "capabilities", "daily_snapshot", "daily_snapshots",
            "incubation_accounts", "incubation_metrics", "paper_account", "paper_orders", "paper_nav", "incubation_sync_run", "risk_events", "risk_snapshots", "risk_scan_run", "risk_recovery", "resolve_risk_event", "runtime_alerts", "runtime_alert_dispatch_run", "runtime_alert_ack",
            "vector_profiles", "vector_indexes", "vector_reconcile", "vector_rebuild", "vector_health", "vector_cleanup",
            "ai_generate", "ai_optimize_personal_strategy", "ai_experiments", "task_runs", "domain_events", "domain_projection", "domain_projection_snapshot", "domain_projection_rebuild",
            "runtime_control", "runtime_control_set", "promotion_reviews", "promotion_review_run",
            "runtime_cycle_run", "runtime_cycle_status", "lifecycle_scan", "get_signals", "get_forward_returns", "get_signal_stats",
            "factory_status", "factory_run_once", "factory_runs", "factory_run_detail", "execution_audit_verification", "review_report", "review_report_recheck", "submission_replay", "events", "closure_review",
            "incubation_overview", "help",
        ],
        "description": "策略超市管理器（含生命周期与前向信号跟踪）",
        # P2-4.6.4 fix: 补 action 参数文档(诊断报告 §4.6.4)
        # 历史问题:incubation_overview 等 action 在 help 里只列名,没说明可选参数
        "action_params": {
            "create": {"required": ["name"], "optional": ["description", "strategy_type", "params", "tags", "user_id"]},
            "publish": {"required": ["strategy_id"], "optional": []},
            "list": {"required": [], "optional": ["status", "limit", "offset", "user_id"]},
            "detail": {"required": ["strategy_id"], "optional": ["user_id"]},
            "subscribe": {"required": ["strategy_id"], "optional": ["user_id"]},
            "rank": {"required": [], "optional": ["sort_by", "limit"]},
            "incubation_overview": {
                "required": [],
                "optional": ["strategy_id", "limit"],
                "doc": "strategy_id 提供时返回该策略 overview;否则返回 incubating 策略列表(默认 limit=20)",
            },
            "factory_run_once": {"required": [], "optional": ["actor_id", "actor_roles"]},
            "execution_audit_verification": {"required": ["strategy_id"], "optional": ["as_of"]},
            "review_report": {"required": ["strategy_id"], "optional": []},
            "closure_review": {"required": ["strategy_id"], "optional": ["as_of", "actor_id", "actor_roles"]},
            "events": {"required": ["strategy_id"], "optional": ["limit"]},
            "get_signals": {"required": ["strategy_id"], "optional": ["limit"]},
            "vector_health": {"required": [], "optional": []},
            "vector_cleanup": {"required": [], "optional": ["dry_run"]},
            "ai_optimize_personal_strategy": {"required": ["strategy_id"], "optional": ["objectives", "user_id"]},
        },
    })


async def handle_create(db, params: dict) -> dict:
    name = str(params.get("name", "")).strip()
    if not name:
        return fail("name is required")
    strategy_type = str(params.get("strategy_type") or params.get("type") or "custom").strip()
    # F-N42-5 fix (诊断报告 §N42): strategy_type 白名单校验。
    # 历史问题: strategy_type='totally_fake_strategy_type_zzz' 无校验直接入库，
    # 下游无执行器时回测/调度可能崩。
    # 修复: 已知执行器类型 + custom/factor_weighted 放行；未知类型附 warning
    # （不硬拒绝以兼容自定义因子权重策略，但显式提示下游可能无执行器）。
    _KNOWN_STRATEGY_TYPES = {
        "ma_cross", "buy_and_hold", "momentum", "rsi",
        "volatility_breakout", "event_structure_breakout", "margin_divergence",
        "custom", "factor_weighted", "factor", "personal",
    }
    strategy_type_warning = None
    if strategy_type.lower() not in _KNOWN_STRATEGY_TYPES:
        strategy_type_warning = (
            f"strategy_type='{strategy_type}' 不在已知执行器类型 {sorted(_KNOWN_STRATEGY_TYPES)} 内；"
            f"该策略可能无可用回测/调度执行器，请确认或改用 custom + factor_weights"
        )
    sid = f"strat_{int(time.time())}_{uuid4().hex[:8]}"
    tags = list(params.get("tags") or [])
    metadata = dict(dict(params.get("params") or {}).get("metadata") or {})
    if params.get("personal_strategy") or metadata.get("source_strategy_id"):
        tags = [*tags, "personal_strategy"]
    if "personal_strategy" in {str(item or "").strip().lower() for item in tags}:
        tags = [str(item or "").strip() for item in tags if str(item or "").strip()]
        tags = list(dict.fromkeys(tags))
    data = {
        "id": sid,
        "name": name,
        "description": params.get("description", ""),
        "author_id": str(params.get("author_id") or params.get("user_id") or "default"),
        "strategy_type": strategy_type,
        "params": params.get("params") or {},
        "factor_weights": params.get("factor_weights") or {},
        "status": "draft",
        "tags": tags,
        "backtest_artifact_id": params.get("backtest_artifact_id"),
    }
    result = await db.save_strategy(data)
    response = {"strategy_id": sid, "strategy": result}
    if strategy_type_warning:
        response["strategy_type_warning"] = strategy_type_warning
    return ok(response)


async def handle_publish(db, params: dict) -> dict:
    sid = str(params.get("strategy_id") or params.get("id") or "").strip()
    if not sid:
        return fail("strategy_id is required")

    # F-N42-1 fix (诊断报告 §N42): publish 必须经过 promotion_gate，
    # 不能把零证据 draft（raw_signal_count=0 / quality_passed=false / promotion_ready=false /
    # blocker_count>0）直接上架 listed。
    # 修复: 发布前评估孵化总览，未达标则拒绝并回显 blockers；
    # 显式 force=true（且带 force_reason）方可绕过（审计留痕）。
    strategy = await db.get_strategy(sid) if hasattr(db, "get_strategy") else None
    if not strategy:
        return fail(f"strategy not found: {sid}")

    force = parse_bool(params.get("force")) if "force" in params else False
    overview = await _resolve_strategy_incubation_overview(db, strategy) or {}
    promotion_ready = bool(overview.get("promotion_ready"))
    quality_passed = overview.get("quality_passed")
    blockers = overview.get("blockers") or []
    if isinstance(blockers, dict):
        blockers = blockers.get("items") or list(blockers.values())
    blocker_list = [str(b) for b in (blockers or [])]

    gate_failed = (not promotion_ready) or (quality_passed is False) or bool(blocker_list)

    if gate_failed and not force:
        return fail(
            "publish 被 promotion_gate 拦截：策略未达发布标准"
            f"（promotion_ready={promotion_ready}, quality_passed={quality_passed}, "
            f"blockers={blocker_list[:10]}）。"
            "请先通过孵化与质量门，或在确认风险后显式传 force=true + force_reason 强制发布。",
        )

    reason = "manual_publish"
    if gate_failed and force:
        force_reason = str(params.get("force_reason") or "").strip()
        reason = f"manual_publish_forced:{force_reason or 'no_reason_given'}"

    await update_status(db, sid, "listed", actor_id="strategy_manager", reason=reason)
    result = {
        "strategy_id": sid,
        "status": "listed",
        # F-N42-3: publish 不可逆提示——上架后 owner 只能 archive，不能 delete。
        "irreversible_note": "策略一旦上架(listed)即不可删除，owner 后续只能 archive(归档)；如需彻底移除请在 publish 前确认",
    }
    if gate_failed and force:
        result["gate_bypassed"] = True
        result["gate_warnings"] = blocker_list[:10]
        result["promotion_ready"] = promotion_ready
        result["quality_passed"] = quality_passed
    return ok(result)


async def handle_archive(db, params: dict) -> dict:
    sid = str(params.get("strategy_id") or params.get("id") or "").strip()
    if not sid:
        return fail("strategy_id is required")
    await update_status(db, sid, "archived", actor_id="strategy_manager", reason="manual_archive")
    return ok({"strategy_id": sid, "status": "archived"})


async def handle_list(db, params: dict) -> dict:
    status = _resolve_strategy_status_filter(params.get("status"), default="visible")
    strategy_type = params.get("strategy_type") or params.get("type")
    limit = min(max(int(params.get("limit", 20)), 1), 100)
    offset = max(int(params.get("offset", 0)), 0)
    rows = await db.list_strategies(status, strategy_type, limit, offset)
    incubation_surfaces = await asyncio.gather(*[
        _load_strategy_incubation_surface(db, row)
        for row in rows
    ]) if rows else []
    return ok({
        "strategies": [
            _build_strategy_market_summary(row, incubation_surface=incubation_surfaces[index])
            for index, row in enumerate(rows)
        ],
        "count": len(rows),
    })


async def handle_detail(db, params: dict) -> dict:
    sid = str(params.get("strategy_id") or params.get("id") or "").strip()
    if not sid:
        return fail("strategy_id is required")
    strategy = await db.get_strategy(sid)
    if not strategy:
        return fail(f"Strategy not found: {sid}")

    actor_id, actor_roles = _actor_context(params)
    user_id = str(actor_id or params.get("user_id") or "default")
    metrics, reviews, is_sub, latest_quality_report, incubation_overview, incubation_account, incubation_metric, strategy_paper_account, risk_events, latest_runtime_risk_snapshot, runtime_control, runtime_alerts, latest_promotion_review, latest_projection_snapshot, latest_vector_index_snapshot, latest_incubation_pipeline_snapshot, vector_profiles, similar_vector_profiles, domain_events, task_runs, nav_series, signal_event_snapshots = await asyncio.gather(
        db.get_strategy_metrics(sid),
        db.get_reviews(sid, limit=10),
        db.is_subscribed(sid, user_id),
        get_latest_quality_report(db, sid),
        _resolve_strategy_incubation_overview(db, strategy),
        db.get_strategy_incubation_account(sid) if hasattr(db, "get_strategy_incubation_account") else _resolved(None),
        db.get_latest_strategy_incubation_metric(sid) if hasattr(db, "get_latest_strategy_incubation_metric") else _resolved(None),
        db.get_paper_account_by_strategy(sid) if hasattr(db, "get_paper_account_by_strategy") else _resolved(None),
        db.list_strategy_runtime_risk_events(strategy_id=sid, status="open", limit=5) if hasattr(db, "list_strategy_runtime_risk_events") else _resolved([]),
        db.get_latest_strategy_runtime_risk_snapshot(sid) if hasattr(db, "get_latest_strategy_runtime_risk_snapshot") else _resolved(None),
        db.get_strategy_runtime_control(sid) if hasattr(db, "get_strategy_runtime_control") else _resolved(None),
        db.list_strategy_runtime_alerts(strategy_id=sid, status="open_or_ack", limit=5) if hasattr(db, "list_strategy_runtime_alerts") else _resolved([]),
        db.get_latest_strategy_promotion_review(sid) if hasattr(db, "get_latest_strategy_promotion_review") else _resolved(None),
        db.get_latest_strategy_projection_snapshot(sid) if hasattr(db, "get_latest_strategy_projection_snapshot") else _resolved(None),
        _load_latest_vector_index_snapshot(db, 'strategy_behavior'),
        db.get_latest_strategy_incubation_pipeline_snapshot(sid) if hasattr(db, "get_latest_strategy_incubation_pipeline_snapshot") else _resolved(None),
        _load_vector_profiles(db, sid, limit=3),
        _load_similar_vector_profiles(db, sid),
        db.list_strategy_domain_events(strategy_id=sid, limit=5) if hasattr(db, "list_strategy_domain_events") else _resolved([]),
        db.list_strategy_task_runs(strategy_id=sid, limit=5) if hasattr(db, "list_strategy_task_runs") else _resolved([]),
        compute_nav_series(db, sid),
        db.list_strategy_signal_event_snapshots(strategy_id=sid, latest_only=True, limit=10) if hasattr(db, "list_strategy_signal_event_snapshots") else _resolved([]),
    )
    metric_noise_enabled = os.getenv("STRATEGY_METRIC_NOISE_ENABLED", "1").strip() not in ("0", "false", "no")
    if not is_sub and metrics and metric_noise_enabled:
        noise = 1 + random.uniform(-0.001, 0.001)
        for m in metrics:
            for key in ("total_return", "annual_return", "sharpe_ratio", "calmar_ratio"):
                if m.get(key) is not None:
                    m[key] = round(float(m[key]) * noise, 6)
            m["approximate"] = True
    latest_quality_report = normalize_quality_report_contract(
        latest_quality_report,
        strategy_id=sid,
        strategy_type=strategy.get("strategy_type"),
        default_review_source="strategy_manager.detail",
    )
    owner_state, favorite_state, paper_session_state = await _load_personal_strategy_surface_state(
        db,
        strategy,
        actor_id=actor_id,
        actor_roles=actor_roles,
    )
    incubation_surface = _build_strategy_incubation_surface(
        strategy,
        paper_account=strategy_paper_account,
        latest_pipeline_snapshot=latest_incubation_pipeline_snapshot,
        overview=incubation_overview,
        incubation_account=incubation_account,
        latest_metric=incubation_metric,
    )
    strategy_with_surface = {
        **dict(strategy or {}),
        "incubation_surface": incubation_surface,
    }
    presentation = build_strategy_presentation(
        strategy_with_surface,
        owner_state=owner_state,
        favorite_state=favorite_state,
        paper_session_state=paper_session_state,
        overview=incubation_overview,
        report=latest_quality_report,
        runtime_control=runtime_control,
        risk_events=risk_events,
    )
    personal_strategy_context = _build_personal_strategy_context(
        strategy_with_surface,
        actor_id=actor_id,
        actor_roles=actor_roles,
        owner_state=owner_state,
        favorite_state=favorite_state,
        paper_session_state=paper_session_state,
    )

    return ok({
        "strategy": strategy_with_surface, "metrics": metrics, "reviews": reviews,
        "nav_series": nav_series,
        "latest_quality_report": latest_quality_report,
        "incubation_overview": incubation_overview,
        "incubation_account": incubation_account,
        "latest_incubation_metric": incubation_metric,
        "latest_promotion_review": latest_promotion_review,
        "latest_projection_snapshot": latest_projection_snapshot,
        "latest_vector_index_snapshot": latest_vector_index_snapshot,
        "latest_incubation_pipeline_snapshot": latest_incubation_pipeline_snapshot,
        "runtime_control": runtime_control,
        "runtime_alerts": runtime_alerts,
        "latest_runtime_risk_snapshot": latest_runtime_risk_snapshot,
        "signal_event_snapshots": signal_event_snapshots,
        "open_risk_events": risk_events,
        "vector_profiles": vector_profiles,
        "similar_vector_profiles": similar_vector_profiles,
        "domain_events": domain_events,
        "task_runs": task_runs,
        "owner_state": owner_state,
        "favorite_state": favorite_state,
        "paper_session_state": paper_session_state,
        "presentation": presentation,
        "personal_strategy_context": personal_strategy_context,
    })


async def handle_review_report(db, params: dict) -> dict:
    sid = str(params.get("strategy_id") or params.get("id") or "").strip()
    if not sid:
        return fail("strategy_id is required")
    limit = min(max(int(params.get("limit", 10)), 1), 50)
    strategy = await db.get_strategy(sid) if hasattr(db, "get_strategy") else None
    strategy_type = str((strategy or {}).get("strategy_type") or "").strip() or None
    reports = [
        normalize_quality_report_contract(
            report,
            strategy_id=sid,
            strategy_type=strategy_type,
            default_review_source="strategy_manager.review_report",
        )
        for report in await list_quality_reports(db, sid, limit=limit)
    ]
    latest = reports[0] if reports else None
    return ok({**(latest or {}), "reports": reports})


async def handle_events(db, params: dict) -> dict:
    sid = str(params.get("strategy_id") or params.get("id") or "").strip()
    if not sid:
        return fail("strategy_id is required")
    limit = min(max(int(params.get("limit", 50)), 1), 200)
    rows = []
    if hasattr(db, "list_strategy_status_events"):
        try:
            rows = await db.list_strategy_status_events(
                sid,
                event_type=str(params.get("event_type") or "").strip() or None,
                from_status=str(params.get("from_status") or "").strip() or None,
                to_status=str(params.get("to_status") or "").strip() or None,
                actor_id=str(params.get("actor_id") or "").strip() or None,
                start_time=normalize_time_filter(params.get("start_time")),
                end_time=normalize_time_filter(params.get("end_time"), is_end=True),
                limit=limit,
            )
        except TypeError:
            rows = await db.list_strategy_status_events(sid, limit=limit)
    return ok({"events": rows, "count": len(rows)})


async def handle_update_metrics(db, params: dict) -> dict:
    sid = str(params.get("strategy_id") or params.get("id") or "").strip()
    if not sid:
        return fail("strategy_id is required")
    metrics = params.get("metrics") or {}
    period = str(params.get("period", "all"))
    await db.save_strategy_metrics(sid, period, metrics)
    return ok({"strategy_id": sid, "period": period, "updated": True})


async def handle_review(db, params: dict) -> dict:
    sid = str(params.get("strategy_id") or params.get("id") or "").strip()
    user_id = str(params.get("user_id", "default"))
    rating = int(params.get("rating", 3))
    comment = params.get("comment")
    if not sid:
        return fail("strategy_id is required")
    if rating < 1 or rating > 5:
        return fail("rating must be 1-5")
    await db.save_review(sid, user_id, rating, comment)
    return ok({"strategy_id": sid, "user_id": user_id, "rating": rating})


async def handle_subscribe(db, params: dict) -> dict:
    sid = str(params.get("strategy_id") or params.get("id") or "").strip()
    user_id = str(params.get("user_id", "default"))
    if not sid:
        return fail("strategy_id is required")
    await db.subscribe_strategy(sid, user_id)
    return ok({"strategy_id": sid, "user_id": user_id, "subscribed": True})


async def handle_unsubscribe(db, params: dict) -> dict:
    sid = str(params.get("strategy_id") or params.get("id") or "").strip()
    user_id = str(params.get("user_id", "default"))
    if not sid:
        return fail("strategy_id is required")
    await db.unsubscribe_strategy(sid, user_id)
    return ok({"strategy_id": sid, "user_id": user_id, "unsubscribed": True})


async def handle_my_subscriptions(db, params: dict) -> dict:
    user_id = str(params.get("user_id", "default"))
    rows = await db.list_user_subscriptions(user_id)
    return ok({"subscriptions": rows, "count": len(rows)})


async def handle_my_strategies(db, params: dict) -> dict:
    actor_id, actor_roles = _actor_context(params)
    if not actor_id:
        return fail("actor_id is required")
    include_archived = str(params.get("include_archived") or "").strip().lower() in {"1", "true", "yes"}
    limit = min(max(int(params.get("limit", 50)), 1), 200)
    offset = max(int(params.get("offset", 0)), 0)
    rows = await db.list_user_strategies(
        actor_id,
        include_archived=include_archived,
        limit=limit,
        offset=offset,
    ) if hasattr(db, "list_user_strategies") else []
    favorite_rows = await db.list_user_subscriptions(actor_id) if hasattr(db, "list_user_subscriptions") else []
    favorite_ids = {
        _trimmed(item.get("id") or item.get("strategy_id"))
        for item in list(favorite_rows or [])
        if _trimmed(item.get("id") or item.get("strategy_id"))
    }
    incubation_surfaces = await asyncio.gather(*[
        _load_strategy_incubation_surface(db, row)
        for row in rows
    ]) if rows else []
    items = []
    for index, row in enumerate(rows):
        owner_state = build_owner_state(row, actor_id=actor_id, actor_roles=actor_roles)
        favorite_state = build_favorite_state(actor_id=actor_id, is_favorited=row.get("id") in favorite_ids)
        session = await db.get_strategy_paper_session(row.get("id"), actor_id) if hasattr(db, "get_strategy_paper_session") else None
        paper_session_state = build_paper_session_state(session, actor_id=actor_id)
        items.append({
            **_build_strategy_market_summary(row, incubation_surface=incubation_surfaces[index]),
            "owner_state": owner_state,
            "favorite_state": favorite_state,
            "paper_session_state": paper_session_state,
        })
    return ok({"strategies": items, "items": items, "count": len(items)})
