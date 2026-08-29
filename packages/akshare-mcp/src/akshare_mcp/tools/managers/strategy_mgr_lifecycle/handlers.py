"""Strategy manager lifecycle action handlers: submit, publish, archive, lifecycle_scan, quality gates."""

import inspect
import logging
import os
from datetime import datetime, timezone
from typing import Any, Optional
from uuid import uuid4

from ....services.strategy_lifecycle_shared import build_closure_review
from ....utils import fail, ok
from ..strategy_mgr_helpers import (
    build_factory_recent_run_diagnostics,
    build_strict_incubation_blocker_summary,
    build_factory_capability_health,
    build_factory_quality_baseline,
    build_incubation_overview,
    build_quality_report,
    get_latest_quality_report,
    list_quality_reports,
    merge_factory_run_summary_observability,
    normalize_factory_run_detail_contract,
    normalize_factory_run_summary_contract,
    normalize_quality_gate_result,
    parse_bool,
    refresh_factory_run_detail_quality_contract,
    refresh_factory_run_summary_quality_contract,
    save_quality_report,
    update_status,
    validate_transition,
)

logger = logging.getLogger(__name__)

from . import _lifecycle_support as _ls
from ._lifecycle_support import (
    _build_recheck_quality_inputs,
    _call_supports_parameter,
    _create_external_factory_dispatch,
    _env_bool,
    _execution_audit_entity_chain_available,
    _factory_defaults_from_constants,
    _factory_run_artifact_refs,
    _get_strategy_factory_scheduler_with_runtime,
    _hydrate_factory_run_artifacts,
    _load_signal_quality_registry_snapshot,
    _quality_report_submission_audit,
    _refresh_closure_review_after_mutation,
    _run_once_accepts_db_arg,
    _run_recheck_risk_report,
    _run_recheck_validation_report,
    _select_latest_backtest_metrics,
    _select_rich_backtest_metrics_from_reports,
    _strategy_factory_inline_execution_enabled,
    _trade_audit_backtest_score,
)

async def handle_review_report_recheck(db, params: dict) -> dict:
    sid = str(params.get("strategy_id") or params.get("id") or "").strip()
    if not sid:
        return fail("strategy_id is required")
    strategy = await db.get_strategy(sid)
    if not strategy:
        return fail(f"Strategy not found: {sid}")
    latest_report = await get_latest_quality_report(db, sid)
    validation_report, risk_report, backtest_metrics = await _build_recheck_quality_inputs(db, strategy, latest_report)
    gate_result = await run_quality_gate(
        db,
        strategy,
        validation_report=validation_report,
        risk_report=risk_report,
        backtest_metrics=backtest_metrics,
    )
    recomputed_as_of = datetime.now(timezone.utc).date().isoformat()
    report_type = f"recheck:{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')}"
    report = build_quality_report(
        strategy_id=sid,
        strategy_type=strategy.get("strategy_type"),
        quality_gate=gate_result,
        validation_report=validation_report,
        risk_report=risk_report,
        dedup_report=(latest_report or {}).get("dedup_report") or {},
        backtest_metrics=backtest_metrics,
        snapshot=(latest_report or {}).get("snapshot") or {},
        status_after_review=strategy.get("status"),
        review_source="review_report_recheck",
        report_type=report_type,
        spawn_reason=((latest_report or {}).get("summary") or {}).get("spawn_reason"),
        submission_audit=_quality_report_submission_audit(latest_report),
    )
    report["recomputed"] = True
    report["as_of"] = recomputed_as_of
    await save_quality_report(db, sid, report, report_type=report_type)
    closure_review = await _refresh_closure_review_after_mutation(
        db,
        strategy,
        as_of=recomputed_as_of,
        correlation_id=str(
            dict((report.get("summary") or {})).get("correlation_id") or ""
        ).strip() or None,
    )
    return ok(
        {
            **report,
            "recomputed": True,
            "as_of": recomputed_as_of,
            "closure_review": closure_review,
        }
    )


def _resolve_replay_strategy_ids(params: dict) -> list[str]:
    ordered: list[str] = []
    seen: set[str] = set()
    raw_values = [
        params.get("strategy_id"),
        params.get("id"),
        params.get("strategy_ids"),
    ]
    queue = list(raw_values)
    while queue:
        value = queue.pop(0)
        if isinstance(value, (list, tuple, set)):
            queue[:0] = list(value)
            continue
        text = str(value or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        ordered.append(text)
    return ordered


async def handle_submission_replay(db, params: dict) -> dict:
    strategy_ids = _resolve_replay_strategy_ids(params)
    if not strategy_ids:
        return fail("strategy_id is required")
    recheck_reports = parse_bool(params.get("recheck_reports"), True)
    from strategy_factory.api import StrategySubmitter

    submitter = StrategySubmitter()
    items: list[dict] = []
    recomputed_as_of = datetime.now(timezone.utc).date().isoformat()
    for sid in strategy_ids:
        strategy = await db.get_strategy(sid)
        if not strategy:
            return fail(f"Strategy not found: {sid}")
        latest_report = await get_latest_quality_report(db, sid)
        if recheck_reports:
            validation_report, risk_report, backtest_metrics = await _build_recheck_quality_inputs(
                db,
                strategy,
                latest_report,
            )
        else:
            validation_report = dict((latest_report or {}).get("validation_report") or {})
            risk_report = dict((latest_report or {}).get("risk_report") or {})
            backtest_metrics = _select_rich_backtest_metrics_from_reports(
                [dict(latest_report or {})],
                dict((latest_report or {}).get("backtest_metrics") or {}),
            )
        snapshot = dict((latest_report or {}).get("snapshot") or {})
        if not snapshot.get("date"):
            snapshot["date"] = datetime.now(timezone.utc).date().isoformat()
        replayed = await submitter.replay_existing_submission(
            strategy,
            snapshot,
            db,
            validation_report=validation_report,
            risk_report=risk_report,
            backtest_metrics=backtest_metrics,
            latest_report=latest_report,
        )
        gate = dict(replayed.get("gate") or {})
        items.append(
            {
                "strategy_id": sid,
                "name": replayed.get("name") or strategy.get("name"),
                "status": replayed.get("status"),
                "submission_lane": replayed.get("submission_lane"),
                "incubation_budget_track": replayed.get("incubation_budget_track"),
                "passed": bool(gate.get("passed")),
                "strict_incubation_ready": bool(gate.get("strict_incubation_ready")),
                "validation_grade": (
                    dict((replayed.get("quality_report") or {}).get("summary") or {}).get("validation_grade")
                ),
                "submission_action_trigger": replayed.get("submission_action_trigger"),
                "paper_lane_ready": replayed.get("paper_lane_ready"),
                "live_review_ready": replayed.get("live_review_ready"),
                "execution_audit_snapshot_id": replayed.get("execution_audit_snapshot_id"),
                "correlation_id": replayed.get("correlation_id"),
                "factory_run_id": replayed.get("factory_run_id"),
                "trace_id": replayed.get("trace_id"),
                "lifecycle_task_run_id": replayed.get("lifecycle_task_run_id"),
                "recomputed": bool(recheck_reports),
                "as_of": recomputed_as_of,
            }
        )
    return ok(
        {
            "count": len(items),
            "recheck_reports": recheck_reports,
            "recomputed": bool(recheck_reports),
            "as_of": recomputed_as_of,
            "items": items,
        }
    )


async def handle_submit(db, params: dict) -> dict:
    sid = str(params.get("strategy_id") or params.get("id") or "").strip()
    if not sid:
        return fail("strategy_id is required")
    strategy = await db.get_strategy(sid)
    if not strategy:
        return fail(f"Strategy not found: {sid}")
    current = strategy.get("status", "draft")
    if not validate_transition(current, "submitted"):
        return fail(f"Cannot submit from status: {current}")

    latest_report = await get_latest_quality_report(db, sid)
    validation_report, risk_report, backtest_metrics = await _build_recheck_quality_inputs(
        db,
        strategy,
        latest_report,
    )
    from strategy_factory.api import StrategySubmitter

    submitter = StrategySubmitter()
    snapshot = dict((latest_report or {}).get("snapshot") or {})
    if not snapshot.get("date"):
        snapshot["date"] = datetime.now(timezone.utc).date().isoformat()
    recomputed_as_of = datetime.now(timezone.utc).date().isoformat()
    replayed = await submitter.replay_existing_submission(
        strategy,
        snapshot,
        db,
        validation_report=validation_report,
        risk_report=risk_report,
        backtest_metrics=backtest_metrics,
        latest_report=latest_report,
    )
    gate_result = dict(replayed.get("gate") or {})
    closure_review = await _refresh_closure_review_after_mutation(
        db,
        {**strategy, "status": replayed.get("status") or strategy.get("status")},
        as_of=recomputed_as_of,
        correlation_id=str(replayed.get("correlation_id") or "").strip() or None,
    )
    return ok({
        "strategy_id": sid,
        "status": replayed.get("status"),
        "quality_gate": "passed" if gate_result.get("passed") else "failed",
        "details": gate_result,
        "submission_lane": replayed.get("submission_lane"),
        "incubation_budget_track": replayed.get("incubation_budget_track"),
        "submission_action_trigger": replayed.get("submission_action_trigger"),
        "paper_lane_ready": replayed.get("paper_lane_ready"),
        "live_review_ready": replayed.get("live_review_ready"),
        "incubation_account_id": ((replayed.get("incubation_binding") or {}).get("account") or {}).get("id"),
        "vector_profile_id": (replayed.get("vector_profile") or {}).get("id"),
        "execution_audit_snapshot_id": replayed.get("execution_audit_snapshot_id"),
        "correlation_id": replayed.get("correlation_id"),
        "factory_run_id": replayed.get("factory_run_id"),
        "trace_id": replayed.get("trace_id"),
        "lifecycle_task_run_id": replayed.get("lifecycle_task_run_id"),
        "recomputed": True,
        "as_of": recomputed_as_of,
        "closure_review": closure_review,
    })


async def handle_lifecycle_scan(db, params: dict) -> dict:
    results = await lifecycle_scan(db)
    return ok(results)


async def handle_incubation_overview(db, params: dict) -> dict:
    sid = str(params.get("strategy_id") or params.get("id") or "").strip()
    if sid:
        strategy = await db.get_strategy(sid)
        if not strategy:
            return fail(f"Strategy not found: {sid}")
        return ok(await build_incubation_overview(db, strategy))
    limit = min(max(int(params.get("limit", 20)), 1), 100)
    incubating = await db.list_strategies("incubating", limit=limit)
    items = [await build_incubation_overview(db, s) for s in incubating]
    return ok({"items": items, "count": len(items)})


async def handle_closure_review(db, params: dict) -> dict:
    sid = str(params.get("strategy_id") or params.get("id") or "").strip()
    if not sid:
        return fail("strategy_id is required")
    strategy = await db.get_strategy(sid)
    if not strategy:
        return fail(f"Strategy not found: {sid}")
    return ok(
        await build_closure_review(
            db,
            strategy,
            as_of=params.get("as_of"),
            correlation_id=str(params.get("correlation_id") or "").strip() or None,
            actor_id=str(params.get("actor_id") or params.get("user_id") or "").strip() or None,
            actor_roles=params.get("actor_roles") or params.get("roles") or params.get("actor_role") or params.get("role"),
        )
    )


async def handle_factory_status(db, params: dict) -> dict:
    from strategy_factory.api.market_views import extract_bulk_stock_cursor
    from strategy_factory.api import FactoryStatusDTO, get_factory_constants
    from strategy_factory.api.market_views import (
        build_research_window_status,
        hydrate_full_market_topn_payload,
    )

    factory_constants = get_factory_constants()
    status = _factory_defaults_from_constants(factory_constants)
    current_last_result = dict(status.get("last_result") or {})
    current_last_summary = dict(status.get("last_summary") or {})
    if current_last_result:
        if current_last_summary and not current_last_result.get("summary"):
            current_last_result["summary"] = current_last_summary
        normalized_last_result = normalize_factory_run_summary_contract(current_last_result)
        status["last_result"] = normalized_last_result
        status["last_summary"] = merge_factory_run_summary_observability(
            normalized_last_result.get("summary") or current_last_summary,
            normalized_last_result,
        )
    high_confidence_feature_flags = dict(factory_constants.get("HIGH_CONFIDENCE_FEATURE_FLAGS") or {})

    def _default_bulk_config() -> dict:
        return {
            "enabled": bool(factory_constants.get("STOCK_STRATEGY_MATRIX_ENABLED")),
            "universe_limit": int(factory_constants.get("STOCK_STRATEGY_MATRIX_UNIVERSE_LIMIT") or 0),
            "families_per_stock": int(factory_constants.get("STOCK_STRATEGY_MATRIX_FAMILIES_PER_STOCK") or 0),
            "max_tasks_per_run": int(factory_constants.get("STOCK_STRATEGY_MATRIX_MAX_TASKS_PER_RUN") or 0),
            "max_candidates_per_run": int(factory_constants.get("STOCK_STRATEGY_MATRIX_MAX_CANDIDATES_PER_RUN") or 0),
            "generation_limit_per_task": int(factory_constants.get("STOCK_STRATEGY_MATRIX_GENERATION_LIMIT_PER_TASK") or 0),
            "batch_size": int(factory_constants.get("STOCK_STRATEGY_MATRIX_BATCH_SIZE") or 0),
            "bulk_concurrency": int(factory_constants.get("STOCK_STRATEGY_MATRIX_BULK_CONCURRENCY") or 0),
            "run_window": factory_constants.get("STOCK_STRATEGY_MATRIX_RUN_WINDOW"),
            "tasks_per_shard": int(factory_constants.get("STOCK_STRATEGY_MATRIX_TASKS_PER_SHARD") or 0),
            "pre_gate_enabled": bool(factory_constants.get("FACTORY_PRE_GATE_ENABLED")),
        }

    def _coerce_status_timestamp(value) -> Optional[datetime]:
        if isinstance(value, datetime):
            ts = value
        else:
            raw = str(value or "").strip()
            if not raw:
                return None
            if raw.endswith("Z"):
                raw = f"{raw[:-1]}+00:00"
            try:
                ts = datetime.fromisoformat(raw)
            except ValueError:
                return None
        if ts.tzinfo is None:
            return ts.replace(tzinfo=timezone.utc)
        return ts.astimezone(timezone.utc)

    def _current_status_timestamp(payload: dict) -> Optional[datetime]:
        last_result = dict(payload.get("last_result") or {})
        last_summary = dict(payload.get("last_summary") or {})
        return (
            _coerce_status_timestamp(last_result.get("completed_at"))
            or _coerce_status_timestamp(last_result.get("started_at"))
            or _coerce_status_timestamp(payload.get("last_run"))
            or _coerce_status_timestamp(last_summary.get("completed_at"))
            or _coerce_status_timestamp(last_summary.get("started_at"))
        )

    status["bulk_stock_matrix_config"] = {
        **_default_bulk_config(),
        **dict(status.get("bulk_stock_matrix_config") or {}),
    }
    latest_run = await db.get_latest_strategy_factory_run() if hasattr(db, "get_latest_strategy_factory_run") else None
    if latest_run:
        latest_run_summary = await refresh_factory_run_summary_quality_contract(db, latest_run)
        latest_run_timestamp = (
            _coerce_status_timestamp(latest_run_summary.get("completed_at"))
            or _coerce_status_timestamp(latest_run_summary.get("started_at"))
        )
        current_status_timestamp = _current_status_timestamp(status)
        prefer_persisted_run = not status.get("last_result") or (
            latest_run_timestamp is not None
            and (current_status_timestamp is None or latest_run_timestamp >= current_status_timestamp)
        )
        status["last_persisted_run"] = latest_run
        if prefer_persisted_run:
            status["last_run"] = latest_run_summary.get("completed_at") or latest_run_summary.get("started_at")
            status["last_result"] = latest_run_summary
            status["last_summary"] = merge_factory_run_summary_observability(
                latest_run_summary.get("summary") or {},
                latest_run_summary,
            )
        if not (status.get("bulk_stock_matrix_cursor") or {}).get("available"):
            status["bulk_stock_matrix_cursor"] = extract_bulk_stock_cursor(
                latest_run.get("summary") or {},
                source="persisted_run",
                run_id=latest_run.get("run_id"),
            )
    research_window = {
        **dict(
            status.get("research_window")
            or dict(status.get("last_summary") or {}).get("research_window")
            or dict(status.get("last_result") or {}).get("research_window")
            or {}
        ),
        **build_research_window_status(status.get("last_summary") or {}),
    }
    resolved_run_id = str(
        (status.get("last_result") or {}).get("run_id")
        or (latest_run or {}).get("run_id")
        or ""
    ).strip()
    full_market_topn = {}
    if resolved_run_id and hasattr(db, "get_strategy_factory_topn_snapshot"):
        full_market_topn = dict(
            await db.get_strategy_factory_topn_snapshot(resolved_run_id)
            or {}
        )
    if not full_market_topn and hasattr(db, "get_latest_strategy_factory_topn_snapshot"):
        full_market_topn = dict(await db.get_latest_strategy_factory_topn_snapshot() or {})
    if not full_market_topn:
        full_market_topn = dict(
            status.get("full_market_topn")
            or dict(status.get("last_summary") or {}).get("full_market_topn")
            or dict(status.get("last_result") or {}).get("full_market_topn")
            or {}
        )
    full_market_topn = hydrate_full_market_topn_payload(full_market_topn)
    status["research_window"] = research_window
    status["full_market_topn"] = full_market_topn
    status["execution_mode"] = (
        str(status.get("execution_mode") or "").strip()
        or str((status.get("last_result") or {}).get("execution_mode") or "").strip()
        or str((latest_run or {}).get("execution_mode") or "").strip()
        or "stock_first_observe_primary"
    )
    status["engine_version"] = (
        str(status.get("engine_version") or "").strip()
        or str((status.get("last_result") or {}).get("engine_version") or "").strip()
        or str((latest_run or {}).get("engine_version") or "").strip()
        or "strategy_factory.v2"
    )
    status["latest_parity_result"] = dict(
        status.get("latest_parity_result")
        or (status.get("last_result") or {}).get("parity_result")
        or (latest_run or {}).get("parity_result")
        or {}
    )
    status["capability_health"] = build_factory_capability_health(
        db,
        factory_constants=factory_constants,
        latest_run=status.get("last_result") or latest_run,
    )
    status["quality_baseline"] = await build_factory_quality_baseline(
        db,
        latest_run=status.get("last_result") or latest_run,
    )
    recent_run_limit = min(max(int(params.get("recent_run_limit", 5)), 1), 10)
    recent_run_rows = (
        await db.list_strategy_factory_runs(limit=recent_run_limit)
        if hasattr(db, "list_strategy_factory_runs")
        else []
    )
    recent_run_items = [
        await refresh_factory_run_summary_quality_contract(db, row)
        for row in recent_run_rows
    ]
    status["recent_run_diagnostics"] = build_factory_recent_run_diagnostics(
        recent_run_items,
        limit=recent_run_limit,
    )
    status["strict_incubation_blocker_summary"] = build_strict_incubation_blocker_summary(
        recent_run_items,
        status.get("recent_run_diagnostics") or {},
        limit=recent_run_limit,
    )
    if isinstance(status.get("quality_baseline"), dict):
        status["quality_baseline"] = {
            **dict(status.get("quality_baseline") or {}),
            "recent_run_diagnostics": dict(status.get("recent_run_diagnostics") or {}),
            "strict_incubation_blocker_summary": dict(
                status.get("strict_incubation_blocker_summary") or {}
            ),
        }
    status["high_confidence_enabled"] = bool(factory_constants.get("STRATEGY_FACTORY_HIGH_CONFIDENCE_ENABLED"))
    status["evidence_contract_enabled"] = bool(factory_constants.get("STRATEGY_FACTORY_EVIDENCE_CONTRACT_ENABLED"))
    status["confidence_diagnostics_enabled"] = bool(
        factory_constants.get("STRATEGY_FACTORY_CONFIDENCE_DIAGNOSTICS_ENABLED")
    )
    status["execution_audit_enabled"] = bool(factory_constants.get("STRATEGY_FACTORY_EXECUTION_AUDIT_ENABLED"))
    status["quality_ui_v2_enabled"] = bool(factory_constants.get("STRATEGY_FACTORY_QUALITY_UI_V2_ENABLED"))
    status["research_protocol_v2_enabled"] = bool(
        factory_constants.get("STRATEGY_FACTORY_RESEARCH_PROTOCOL_V2_ENABLED")
    )
    status["gate_model_v2_enabled"] = bool(factory_constants.get("STRATEGY_FACTORY_GATE_MODEL_V2_ENABLED"))
    status["trace_ledger_v2_enabled"] = bool(factory_constants.get("STRATEGY_FACTORY_TRACE_LEDGER_V2_ENABLED"))
    status["feedback_v2_enabled"] = bool(factory_constants.get("STRATEGY_FACTORY_FEEDBACK_V2_ENABLED"))
    status["trace_ledger_v2_implemented"] = True
    status["governance_gate_report_v2_implemented"] = True
    status["execution_audit_entity_chain_available"] = _execution_audit_entity_chain_available(db)
    status["spec_completeness_mode"] = str(
        factory_constants.get("STRATEGY_FACTORY_SPEC_COMPLETENESS_MODE") or "warn"
    )
    status["feature_flags"] = high_confidence_feature_flags
    status["signal_quality_registry"] = _load_signal_quality_registry_snapshot()
    status = {
        **status,
        **FactoryStatusDTO.from_dict(status).to_dict(),
    }
    return ok(status)


async def handle_factory_run_once(db, params: dict) -> dict:
    if not _strategy_factory_inline_execution_enabled():
        return await _create_external_factory_dispatch(db, params, source_action="factory_run_once")
    scheduler = _ls._get_strategy_factory_scheduler_with_runtime(db)
    run_once = scheduler.run_once
    execution_mode = params.get("execution_mode")
    target_codes = params.get("target_codes") or params.get("codes") or []
    kwargs = {}
    if _call_supports_parameter(run_once, "execution_mode"):
        kwargs["execution_mode"] = execution_mode
    if _call_supports_parameter(run_once, "target_codes"):
        kwargs["target_codes"] = target_codes
    if _run_once_accepts_db_arg(run_once):
        return ok(await run_once(db=db, **kwargs))
    return ok(await run_once(**kwargs))


async def handle_factory_dispatch_run(db, params: dict) -> dict:
    if not _strategy_factory_inline_execution_enabled():
        return await _create_external_factory_dispatch(db, params, source_action="factory_dispatch_run")
    scheduler = _ls._get_strategy_factory_scheduler_with_runtime(db)
    dispatch_run = getattr(scheduler, "dispatch_run", None)
    if not callable(dispatch_run):
        return fail("factory dispatch is unavailable")
    execution_mode = params.get("execution_mode")
    target_codes = params.get("target_codes") or params.get("codes") or []
    kwargs = {}
    if _call_supports_parameter(dispatch_run, "execution_mode"):
        kwargs["execution_mode"] = execution_mode
    if _call_supports_parameter(dispatch_run, "target_codes"):
        kwargs["target_codes"] = target_codes
    if _run_once_accepts_db_arg(dispatch_run):
        return ok(await dispatch_run(db=db, **kwargs))
    return ok(await dispatch_run(**kwargs))


async def handle_factory_dispatch_status(db, params: dict) -> dict:
    dispatch_id = str(params.get("dispatch_id") or "").strip()
    if not dispatch_id:
        return fail("dispatch_id is required")
    if hasattr(db, "get_strategy_factory_dispatch"):
        row = await db.get_strategy_factory_dispatch(dispatch_id)
    else:
        row = None
    if row is None and _strategy_factory_inline_execution_enabled():
        scheduler = _ls._get_strategy_factory_scheduler_with_runtime(db)
        get_dispatch_status = getattr(scheduler, "get_dispatch_status", None)
        if callable(get_dispatch_status):
            if _run_once_accepts_db_arg(get_dispatch_status):
                row = await get_dispatch_status(dispatch_id, db=db)
            else:
                row = await get_dispatch_status(dispatch_id)
    if not row:
        return fail(f"Factory dispatch not found: {dispatch_id}")
    return ok(row)


async def handle_factory_runs(db, params: dict) -> dict:
    limit = min(max(int(params.get("limit", 10)), 1), 100)
    rows = await db.list_strategy_factory_runs(limit=limit) if hasattr(db, "list_strategy_factory_runs") else []
    items = [await refresh_factory_run_summary_quality_contract(db, row) for row in rows]
    return ok({"items": items, "count": len(rows)})


async def handle_factory_run_detail(db, params: dict) -> dict:
    from strategy_factory.api.market_views import (
        build_research_window_status,
        hydrate_full_market_topn_payload,
    )

    run_id = str(params.get("run_id") or "").strip()
    if not run_id:
        return fail("run_id is required")
    row = await db.get_strategy_factory_run(run_id) if hasattr(db, "get_strategy_factory_run") else None
    if not row:
        return fail(f"Factory run not found: {run_id}")
    artifact_mode = str(params.get("artifact_mode") or "summary").strip().lower() or "summary"
    if artifact_mode not in {"summary", "refs", "full"}:
        return fail("artifact_mode must be one of summary, refs, full")
    full_artifact_detail_enabled = _env_bool("STRATEGY_FACTORY_ENABLE_FULL_ARTIFACT_DETAIL", False)
    hydrate_artifacts = artifact_mode == "full" and full_artifact_detail_enabled
    if hydrate_artifacts and hasattr(db, "list_strategy_factory_run_artifacts"):
        artifact_rows = await db.list_strategy_factory_run_artifacts(run_id)
    elif hasattr(db, "list_strategy_factory_run_artifact_refs"):
        artifact_rows = await db.list_strategy_factory_run_artifact_refs(run_id)
    else:
        artifact_rows = []
    artifact_refs = _factory_run_artifact_refs(artifact_rows)
    hydrated_row = _hydrate_factory_run_artifacts(row, artifact_rows) if hydrate_artifacts else dict(row or {})
    detail = await refresh_factory_run_detail_quality_contract(db, hydrated_row)
    research_window = {
        **dict(
            detail.get("research_window")
            or dict(detail.get("summary") or {}).get("research_window")
            or {}
        ),
        **build_research_window_status(detail.get("summary") or {}),
    }
    full_market_topn = dict(
        detail.get("full_market_topn")
        or dict(detail.get("summary") or {}).get("full_market_topn")
        or {}
    )
    if hasattr(db, "get_strategy_factory_topn_snapshot"):
        topn_snapshot = await db.get_strategy_factory_topn_snapshot(run_id)
        if topn_snapshot:
            full_market_topn = {**full_market_topn, **dict(topn_snapshot or {})}
    if full_market_topn and hasattr(db, "count_strategy_factory_full_market_scores"):
        full_market_topn["score_row_count"] = await db.count_strategy_factory_full_market_scores(run_id)
    full_market_topn = hydrate_full_market_topn_payload(full_market_topn)
    detail["research_window"] = research_window
    detail["full_market_topn"] = full_market_topn
    detail["artifact_mode"] = artifact_mode
    detail["artifacts"] = artifact_rows if hydrate_artifacts else artifact_refs
    detail["artifact_refs"] = list(
        artifact_refs
        or detail.get("artifact_refs")
        or hydrated_row.get("artifact_refs")
        or []
    )
    if artifact_mode == "full" and not full_artifact_detail_enabled:
        detail["full_artifact_payload_disabled"] = True
        detail["full_artifact_payload_disabled_reason"] = "STRATEGY_FACTORY_ENABLE_FULL_ARTIFACT_DETAIL is not enabled"
    return ok(detail)


async def handle_factory_topn_latest(db, params: dict) -> dict:
    from strategy_factory.api.market_views import hydrate_full_market_topn_payload

    if not hasattr(db, "get_latest_strategy_factory_topn_snapshot"):
        return fail("factory Top N snapshots are unavailable")
    limit = min(max(int(params.get("limit", 20)), 1), 100)
    snapshot = await db.get_latest_strategy_factory_topn_snapshot()
    if not snapshot:
        return ok(
            {
                "available": False,
                "snapshot": None,
                "top_scores": [],
                "score_row_count": 0,
                "requested_limit": limit,
            }
        )
    snapshot = hydrate_full_market_topn_payload(snapshot)
    run_id = str((snapshot or {}).get("run_id") or "").strip()
    top_scores = (
        await db.list_strategy_factory_full_market_scores(run_id, limit=limit)
        if run_id and hasattr(db, "list_strategy_factory_full_market_scores")
        else []
    )
    score_row_count = (
        await db.count_strategy_factory_full_market_scores(run_id)
        if run_id and hasattr(db, "count_strategy_factory_full_market_scores")
        else len(top_scores)
    )
    return ok(
        {
            "available": True,
            "snapshot": snapshot,
            "top_scores": list(top_scores or []),
            "score_row_count": int(score_row_count or 0),
            "requested_limit": limit,
        }
    )


async def handle_factory_run_topn(db, params: dict) -> dict:
    from strategy_factory.api.market_views import hydrate_full_market_topn_payload

    run_id = str(params.get("run_id") or "").strip()
    if not run_id:
        return fail("run_id is required")
    if not hasattr(db, "get_strategy_factory_topn_snapshot"):
        return fail("factory Top N snapshots are unavailable")
    limit = min(max(int(params.get("limit", 20)), 1), 100)
    snapshot = await db.get_strategy_factory_topn_snapshot(run_id)
    if not snapshot:
        return fail(f"Factory Top N snapshot not found: {run_id}")
    snapshot = hydrate_full_market_topn_payload(snapshot)
    top_scores = (
        await db.list_strategy_factory_full_market_scores(run_id, limit=limit)
        if hasattr(db, "list_strategy_factory_full_market_scores")
        else []
    )
    score_row_count = (
        await db.count_strategy_factory_full_market_scores(run_id)
        if hasattr(db, "count_strategy_factory_full_market_scores")
        else len(top_scores)
    )
    return ok(
        {
            "available": True,
            "snapshot": snapshot,
            "top_scores": list(top_scores or []),
            "score_row_count": int(score_row_count or 0),
            "requested_limit": limit,
        }
    )


async def handle_execution_audit_verification(db, params: dict) -> dict:
    if not hasattr(db, "get_execution_audit_verification"):
        return fail("execution audit verification is unavailable")
    strategy_id = str(params.get("strategy_id") or params.get("id") or "").strip() or None
    return ok(await db.get_execution_audit_verification(strategy_id=strategy_id))


async def handle_execution_audit_acceptance(db, params: dict) -> dict:
    if not hasattr(db, "run_execution_audit_acceptance"):
        return fail("execution audit acceptance is unavailable")
    strategy_id = str(params.get("strategy_id") or params.get("id") or "").strip() or None
    backfill = bool(params.get("backfill", True))
    return ok(
        await db.run_execution_audit_acceptance(
            strategy_id=strategy_id,
            backfill=backfill,
        )
    )


# ── Quality gate runner ──────────────────────────────────────────────────────

async def run_quality_gate(
    db,
    strategy: dict,
    *,
    validation_report: Optional[dict] = None,
    risk_report: Optional[dict] = None,
    backtest_metrics: Optional[dict] = None,
) -> dict:
    """Run the shared submission-stage quality gate and normalize the result."""
    from strategy_factory.api import run_submission_quality_gate

    return normalize_quality_gate_result(
        await run_submission_quality_gate(
            db,
            strategy,
            validation_report=validation_report,
            risk_report=risk_report,
            backtest_metrics=backtest_metrics,
        )
    )


# ── Lifecycle scan ───────────────────────────────────────────────────────────

async def lifecycle_scan(db) -> dict:
    """Batch scan for strategy status transitions."""
    from ....services.promotion_pipeline import get_strategy_promotion_pipeline_service

    transitions = []
    blocked = []
    reviews = []
    promotion_service = get_strategy_promotion_pipeline_service()

    incubating = await db.list_strategies("incubating", limit=100)
    for s in incubating:
        review_result = await promotion_service.review(db, s, source='lifecycle_scan', auto_apply=True)
        review = review_result.get('review') or {}
        overview = review_result.get('overview') or {}
        reviews.append(review)
        if review_result.get('applied_transition'):
            transition = review_result['applied_transition']
            reason = 'incubation_promoted' if transition.get('to') == 'listed' else 'incubation_failed'
            transitions.append({
                'id': s['id'],
                **transition,
                'reason': reason,
            })
        else:
            blocked.append({'id': s['id'], 'status': 'incubating', 'blockers': overview.get('blockers') or []})

    listed = await db.list_strategies("listed", limit=200)
    for s in listed:
        overview = await build_incubation_overview(db, s)
        if overview["deprecation_risk"]:
            # Fix #13: 要求连续多期触发降级风险才执行降级，避免单次波动误杀
            deprecation_confirmed = False
            if hasattr(db, 'list_strategy_incubation_metrics'):
                recent_metrics = await db.list_strategy_incubation_metrics(s["id"], limit=3)
                if len(recent_metrics) >= 2:
                    # 最近 2 期以上连续 decision=halt 才确认降级
                    halt_streak = sum(1 for m in recent_metrics if str(m.get('decision') or '') == 'halt')
                    deprecation_confirmed = halt_streak >= 2
                else:
                    # 数据不足时不急于降级
                    deprecation_confirmed = False
            else:
                deprecation_confirmed = True  # 无法查询历史时保持原行为

            if deprecation_confirmed:
                await update_status(
                    db,
                    s["id"],
                    "deprecated",
                    actor_id="lifecycle_scan",
                    reason="listed_degraded",
                    metadata=overview,
                )
                transitions.append({"id": s["id"], "from": "listed", "to": "deprecated", "reason": "listed_degraded"})
            else:
                blocked.append({"id": s["id"], "status": "listed", "blockers": ["deprecation_risk_unconfirmed"]})

    return {"scanned": len(incubating) + len(listed), "transitions": transitions, "blocked": blocked, 'reviews': reviews}


# Backward-compatible aliases
_run_quality_gate = run_quality_gate
_lifecycle_scan = lifecycle_scan
