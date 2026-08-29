"""Strategy manager incubation action handlers."""

import logging
from datetime import date

from ...utils import fail, ok

logger = logging.getLogger(__name__)


async def handle_incubation_accounts(db, params: dict) -> dict:
    sid = str(params.get("strategy_id") or params.get("id") or "").strip() or None
    limit = min(max(int(params.get("limit", 20)), 1), 200)
    rows = await db.list_strategy_incubation_accounts(strategy_id=sid, status=params.get("status"), limit=limit) if hasattr(db, "list_strategy_incubation_accounts") else []
    return ok({"items": rows, "count": len(rows)})


async def handle_incubation_metrics(db, params: dict) -> dict:
    sid = str(params.get("strategy_id") or params.get("id") or "").strip()
    if not sid:
        return fail("strategy_id is required")
    limit = min(max(int(params.get("limit", 30)), 1), 365)
    rows = await db.list_strategy_incubation_metrics(
        sid,
        limit=limit,
        start_date=params.get("start_date"),
        end_date=params.get("end_date"),
    ) if hasattr(db, "list_strategy_incubation_metrics") else []
    latest = rows[0] if rows else None
    return ok({"items": rows, "latest": latest, "count": len(rows)})


async def handle_paper_account(db, params: dict) -> dict:
    sid = str(params.get("strategy_id") or params.get("id") or "").strip()
    if not sid:
        return fail("strategy_id is required")
    account = await db.get_paper_account_by_strategy(sid) if hasattr(db, "get_paper_account_by_strategy") else None
    binding = await db.get_strategy_incubation_account(sid) if hasattr(db, "get_strategy_incubation_account") else None
    positions = await db.list_paper_positions(account["id"]) if account and hasattr(db, "list_paper_positions") else []
    nav_rows = await db.get_paper_nav_rows(account["id"], limit=min(max(int(params.get("limit", 10)), 1), 120)) if account and hasattr(db, "get_paper_nav_rows") else []
    order_summary = await db.get_paper_order_summary(account["id"]) if account and hasattr(db, "get_paper_order_summary") else {"total_orders": 0, "filled_orders": 0, "total_trades": 0, "trade_amount": 0.0}
    latest_nav = nav_rows[0] if nav_rows else None
    return ok({
        "account": account,
        "binding": binding,
        "positions": positions,
        "latest_nav": latest_nav,
        "order_summary": order_summary,
    })


async def handle_paper_orders(db, params: dict) -> dict:
    sid = str(params.get("strategy_id") or params.get("id") or "").strip()
    if not sid:
        return fail("strategy_id is required")
    limit = min(max(int(params.get("limit", 50)), 1), 500)
    rows = await db.list_strategy_paper_orders(
        sid,
        signal_date=params.get("signal_date"),
        status=(str(params.get("status") or "").strip() or None),
        limit=limit,
    ) if hasattr(db, "list_strategy_paper_orders") else []
    return ok({"items": rows, "count": len(rows)})


async def handle_paper_nav(db, params: dict) -> dict:
    sid = str(params.get("strategy_id") or params.get("id") or "").strip()
    account_id = str(params.get("account_id") or "").strip() or None
    if not account_id and sid and hasattr(db, "get_paper_account_by_strategy"):
        account = await db.get_paper_account_by_strategy(sid)
        account_id = (account or {}).get("id")
    if not account_id:
        return fail("account_id or strategy_id is required")
    limit = min(max(int(params.get("limit", 30)), 1), 365)
    rows = await db.get_paper_nav_rows(account_id, limit=limit) if hasattr(db, "get_paper_nav_rows") else []
    latest = rows[0] if rows else None
    return ok({"items": rows, "count": len(rows), "latest": latest, "account_id": account_id})


async def handle_incubation_sync_run(db, params: dict) -> dict:
    from ...services.incubation import get_strategy_incubation_service

    def _coerce_date(value):
        if not value:
            return None
        if isinstance(value, date):
            return value
        try:
            return date.fromisoformat(str(value)[:10])
        except Exception:
            return None

    sid = str(params.get("strategy_id") or params.get("id") or "").strip() or None
    signal_date = params.get("signal_date")
    start_date = _coerce_date(params.get("start_date"))
    end_date = _coerce_date(params.get("end_date"))
    replay_history = bool(params.get("replay_history")) or start_date is not None or end_date is not None
    strategies = []
    if sid:
        strategy = await db.get_strategy(sid)
        if not strategy:
            return fail(f"Strategy not found: {sid}")
        strategies = [strategy]
    else:
        statuses = params.get("statuses") or ["incubating", "listed"]
        if isinstance(statuses, str):
            statuses = [item.strip() for item in statuses.split(",") if item.strip()]
        seen = set()
        for status in statuses:
            for strategy in await db.list_strategies(status, limit=min(max(int(params.get("limit", 200)), 1), 1000)):
                if strategy["id"] in seen:
                    continue
                seen.add(strategy["id"])
                strategies.append(strategy)
    service = get_strategy_incubation_service()
    if replay_history and hasattr(service, "replay_strategies_history"):
        result = await service.replay_strategies_history(
            db,
            strategies,
            start_date=start_date,
            end_date=end_date,
            include_market_days=bool(params.get("include_market_days", True)),
            max_dates=min(max(int(params.get("max_dates", 1500)), 1), 5000),
            force_close_open_positions=bool(params.get("force_close_open_positions")),
            run_acceptance=bool(params.get("run_acceptance", True)),
        )
    else:
        result = await service.process_strategies(db, strategies, signal_date=signal_date or None)
    return ok(result)


async def handle_incubation_pipeline(db, params: dict) -> dict:
    sid = str(params.get("strategy_id") or params.get("id") or "").strip() or None
    limit = min(max(int(params.get("limit", 20)), 1), 200)
    rows = await db.list_strategy_incubation_pipeline_snapshots(
        strategy_id=sid,
        pipeline_stage=(str(params.get("pipeline_stage") or "").strip() or None),
        pipeline_status=(str(params.get("pipeline_status") or "").strip() or None),
        limit=limit,
    ) if hasattr(db, "list_strategy_incubation_pipeline_snapshots") else []
    latest = rows[0] if rows else None
    return ok({"items": rows, "count": len(rows), "latest": latest})


async def handle_incubation_pipeline_run(db, params: dict) -> dict:
    sid = str(params.get("strategy_id") or params.get("id") or "").strip() or None
    from ...services.incubation_pipeline import get_strategy_incubation_pipeline_service
    service = get_strategy_incubation_pipeline_service()
    if sid:
        strategy = await db.get_strategy(sid)
        if not strategy:
            return fail(f"Strategy not found: {sid}")
        result = await service.run_strategy(
            db,
            strategy,
            source=str(params.get("source") or "strategy_manager"),
            auto_apply_review=bool(params.get("auto_apply_review")),
        )
        return ok(result)
    statuses = params.get("statuses") or ['incubating']
    if isinstance(statuses, str):
        statuses = [item.strip() for item in statuses.split(',') if item.strip()]
    result = await service.run_batch(
        db,
        statuses=list(statuses or ['incubating']),
        limit=min(max(int(params.get("limit", 200)), 1), 1000),
        source=str(params.get("source") or "strategy_manager"),
        auto_apply_review=bool(params.get("auto_apply_review", True)),
    )
    return ok(result)
