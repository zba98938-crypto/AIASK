"""Strategy manager runtime action handlers: risk, alerts, controls, domain, promotion."""

import logging

from ...utils import fail, ok
from .strategy_mgr_helpers import parse_bool

logger = logging.getLogger(__name__)


# ── Risk ─────────────────────────────────────────────────────────────────────

async def handle_risk_events(db, params: dict) -> dict:
    limit = min(max(int(params.get("limit", 50)), 1), 500)
    rows = await db.list_strategy_runtime_risk_events(
        strategy_id=(str(params.get("strategy_id") or params.get("id") or "").strip() or None),
        account_id=(str(params.get("account_id") or "").strip() or None),
        status=(str(params.get("status") or "").strip() or None),
        severity=(str(params.get("severity") or "").strip() or None),
        limit=limit,
    ) if hasattr(db, "list_strategy_runtime_risk_events") else []
    return ok({"items": rows, "count": len(rows)})


async def handle_risk_snapshots(db, params: dict) -> dict:
    limit = min(max(int(params.get("limit", 20)), 1), 500)
    rows = await db.list_strategy_runtime_risk_snapshots(
        strategy_id=(str(params.get("strategy_id") or params.get("id") or "").strip() or None),
        posture_level=(str(params.get("posture_level") or "").strip() or None),
        control_mode=(str(params.get("control_mode") or "").strip() or None),
        limit=limit,
    ) if hasattr(db, "list_strategy_runtime_risk_snapshots") else []
    latest = rows[0] if rows else None
    return ok({"items": rows, "count": len(rows), "latest": latest})


async def handle_risk_scan_run(db, params: dict) -> dict:
    from ...services.runtime_risk import get_strategy_runtime_risk_service
    sid = str(params.get("strategy_id") or params.get("id") or "").strip() or None
    if sid:
        strategy = await db.get_strategy(sid)
        if not strategy:
            return fail(f"Strategy not found: {sid}")
        result = await get_strategy_runtime_risk_service().scan(db, [strategy], enforce_actions=bool(params.get("enforce_actions", True)))
    else:
        result = await get_strategy_runtime_risk_service().scan(db, None, enforce_actions=bool(params.get("enforce_actions", True)))
    return ok(result)


async def handle_risk_recovery(db, params: dict) -> dict:
    sid = str(params.get("strategy_id") or params.get("id") or "").strip()
    if not sid:
        return fail("strategy_id is required")
    strategy = await db.get_strategy(sid)
    if not strategy:
        return fail(f"Strategy not found: {sid}")
    from ...services.runtime_risk import get_strategy_runtime_risk_service
    result = await get_strategy_runtime_risk_service().attempt_recovery(
        db,
        strategy,
        source=str(params.get("source") or "strategy_manager"),
    )
    return ok(result)


async def handle_resolve_risk_event(db, params: dict) -> dict:
    event_id = params.get("event_id")
    if event_id is None:
        return fail("event_id is required")
    row = await db.resolve_strategy_runtime_risk_event(int(event_id), {
        "resolution": params.get("resolution") or "manual_resolved",
    }) if hasattr(db, "resolve_strategy_runtime_risk_event") else None
    if not row:
        return fail("risk event not found")
    return ok(row)


# ── Runtime alerts ───────────────────────────────────────────────────────────

async def handle_runtime_alerts(db, params: dict) -> dict:
    sid = str(params.get("strategy_id") or params.get("id") or "").strip() or None
    limit = min(max(int(params.get("limit", 20)), 1), 500)
    rows = await db.list_strategy_runtime_alerts(
        strategy_id=sid,
        category=(str(params.get("category") or "").strip() or None),
        severity=(str(params.get("severity") or "").strip() or None),
        status=(str(params.get("status") or "").strip() or None),
        limit=limit,
    ) if hasattr(db, "list_strategy_runtime_alerts") else []
    latest = rows[0] if rows else None
    return ok({"items": rows, "count": len(rows), "latest": latest})


async def handle_runtime_alert_dispatch_run(db, params: dict) -> dict:
    sid = str(params.get("strategy_id") or params.get("id") or "").strip() or None
    from ...services.runtime_alerts import get_strategy_runtime_alert_service
    service = get_strategy_runtime_alert_service()
    if sid:
        strategy = await db.get_strategy(sid)
        if not strategy:
            return fail(f"Strategy not found: {sid}")
        result = await service.dispatch_for_strategy(db, strategy, source=str(params.get("source") or "strategy_manager"))
        return ok(result)
    statuses = params.get("statuses") or ['incubating', 'listed', 'suspended']
    if isinstance(statuses, str):
        statuses = [item.strip() for item in statuses.split(',') if item.strip()]
    strategies = []
    limit = min(max(int(params.get("limit", 200)), 1), 500)
    for status in list(statuses or ['incubating', 'listed', 'suspended']):
        strategies.extend(await db.list_strategies(status, limit=limit))
    result = await service.dispatch_batch(db, strategies, source=str(params.get("source") or "strategy_manager"))
    return ok(result)


async def handle_runtime_alert_ack(db, params: dict) -> dict:
    alert_id = int(params.get("alert_id") or 0)
    if alert_id <= 0:
        return fail("alert_id is required")
    from ...services.runtime_alerts import get_strategy_runtime_alert_service
    row = await get_strategy_runtime_alert_service().acknowledge_alert(
        db,
        alert_id,
        acknowledged_by=(str(params.get("acknowledged_by") or params.get("user_id") or "").strip() or None),
        source=str(params.get("source") or "strategy_manager"),
    )
    if not row:
        return fail("runtime alert not found")
    return ok(row)


# ── Runtime control ──────────────────────────────────────────────────────────

async def handle_runtime_control(db, params: dict) -> dict:
    sid = str(params.get("strategy_id") or params.get("id") or "").strip()
    if not sid:
        return fail("strategy_id is required")
    row = await db.get_strategy_runtime_control(sid) if hasattr(db, "get_strategy_runtime_control") else None
    return ok(row or {"strategy_id": sid, "control_mode": "active", "status": "released"})


async def handle_runtime_control_set(db, params: dict) -> dict:
    sid = str(params.get("strategy_id") or params.get("id") or "").strip()
    if not sid:
        return fail("strategy_id is required")
    strategy = await db.get_strategy(sid)
    if not strategy:
        return fail(f"Strategy not found: {sid}")
    from ...services.runtime_control import get_strategy_runtime_control_service
    result = await get_strategy_runtime_control_service().set_control(
        db,
        strategy,
        control_mode=str(params.get("control_mode") or "active"),
        source=str(params.get("source") or "strategy_manager"),
        reason=(str(params.get("reason") or "").strip() or None),
        trigger_event_type=(str(params.get("trigger_event_type") or "manual_override").strip() or None),
        action_summary=params.get("action_summary") or {},
        metadata=params.get("metadata") or {},
        apply_runtime_changes=True,
    )
    return ok(result)


# ── Promotion ────────────────────────────────────────────────────────────────

async def handle_promotion_reviews(db, params: dict) -> dict:
    limit = min(max(int(params.get("limit", 20)), 1), 500)
    rows = await db.list_strategy_promotion_reviews(
        strategy_id=(str(params.get("strategy_id") or params.get("id") or "").strip() or None),
        status=(str(params.get("status") or "").strip() or None),
        limit=limit,
    ) if hasattr(db, "list_strategy_promotion_reviews") else []
    latest = rows[0] if rows else None
    return ok({"items": rows, "count": len(rows), "latest": latest})


async def handle_promotion_review_run(db, params: dict) -> dict:
    sid = str(params.get("strategy_id") or params.get("id") or "").strip()
    if not sid:
        return fail("strategy_id is required")
    strategy = await db.get_strategy(sid)
    if not strategy:
        return fail(f"Strategy not found: {sid}")
    from ...services.promotion_pipeline import get_strategy_promotion_pipeline_service
    result = await get_strategy_promotion_pipeline_service().review(
        db,
        strategy,
        source=str(params.get("source") or "strategy_manager"),
        auto_apply=bool(params.get("auto_apply")),
    )
    return ok(result)


# ── Runtime cycle ────────────────────────────────────────────────────────────

async def handle_runtime_cycle_status(db, params: dict) -> dict:
    from strategy_factory.runtime.default_bootstrap import ensure_default_runtime_services
    from strategy_factory.runtime.signal_tracker import get_signal_tracker_runtime

    ensure_default_runtime_services()
    return ok(get_signal_tracker_runtime().status())


async def handle_runtime_cycle_run(db, params: dict) -> dict:
    from strategy_factory.runtime.default_bootstrap import ensure_default_runtime_services
    from strategy_factory.runtime.signal_tracker import get_signal_tracker_runtime

    ensure_default_runtime_services()
    return ok(await get_signal_tracker_runtime().run_once())


# ── Domain events & projection ───────────────────────────────────────────────

async def handle_domain_events(db, params: dict) -> dict:
    limit = min(max(int(params.get("limit", 50)), 1), 500)
    rows = await db.list_strategy_domain_events(
        strategy_id=(str(params.get("strategy_id") or params.get("id") or "").strip() or None),
        aggregate_type=(str(params.get("aggregate_type") or "").strip() or None),
        event_type=(str(params.get("event_type") or "").strip() or None),
        source=(str(params.get("source") or "").strip() or None),
        correlation_id=(str(params.get("correlation_id") or "").strip() or None),
        limit=limit,
    ) if hasattr(db, "list_strategy_domain_events") else []
    return ok({"items": rows, "count": len(rows)})


async def handle_domain_projection(db, params: dict) -> dict:
    sid = str(params.get("strategy_id") or params.get("id") or "").strip()
    if not sid:
        return fail("strategy_id is required")
    from ...services.domain_projection import get_strategy_domain_projection_service
    result = await get_strategy_domain_projection_service().project_strategy(
        db,
        sid,
        limit=min(max(int(params.get("limit", 200)), 20), 500),
    )
    return ok(result)


async def handle_domain_projection_snapshot(db, params: dict) -> dict:
    sid = str(params.get("strategy_id") or params.get("id") or "").strip()
    if not sid:
        return fail("strategy_id is required")
    limit = min(max(int(params.get("limit", 20)), 1), 200)
    latest = await db.get_latest_strategy_projection_snapshot(sid) if hasattr(db, "get_latest_strategy_projection_snapshot") else None
    rows = await db.list_strategy_projection_snapshots(sid, limit=limit) if hasattr(db, "list_strategy_projection_snapshots") else ([] if latest is None else [latest])
    return ok({"latest": latest, "items": rows, "count": len(rows)})


async def handle_domain_projection_rebuild(db, params: dict) -> dict:
    from ...services.domain_projection import get_strategy_domain_projection_service
    sid = str(params.get("strategy_id") or params.get("id") or "").strip()
    service = get_strategy_domain_projection_service()
    if sid:
        result = await service.rebuild_projection(
            db,
            sid,
            limit=min(max(int(params.get("limit", 200)), 20), 500),
            source=str(params.get("source") or "strategy_manager"),
            persist=True,
        )
    else:
        statuses = params.get("statuses") or ["incubating", "listed", "suspended", "deprecated"]
        if isinstance(statuses, str):
            statuses = [item.strip() for item in statuses.split(',') if item.strip()]
        result = await service.rebuild_batch(
            db,
            statuses=list(statuses or ["incubating", "listed", "suspended", "deprecated"]),
            limit=min(max(int(params.get("limit", 200)), 1), 500),
            source=str(params.get("source") or "strategy_manager"),
        )
    return ok(result)
