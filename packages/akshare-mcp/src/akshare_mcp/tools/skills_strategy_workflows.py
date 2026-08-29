"""Extracted strategy-factory skill workflows."""

from __future__ import annotations

from typing import Any, Awaitable, Callable, Dict, List

from . import skills_support as skill_support


SUPPORTED_STRATEGY_FACTORY_TASKS = [
    "factory_cycle",
    "strategy_review",
    "submission_gate",
    "incubation_pipeline",
    "runtime_governance",
    "vector_governance",
    "domain_projection",
    "ai_generation",
    "smoke_test",
]


def _skill_support():
    return skill_support


def _trim_text(value: Any) -> str:
    return str(value or "").strip()


def _bool_flag(value: Any, default: bool = False) -> bool:
    parsed = skill_support._parse_bool_flag(value)
    if parsed is None:
        return bool(default)
    return bool(parsed)


def _split_values(value: Any) -> list[str]:
    if isinstance(value, str):
        items = [item.strip() for item in value.split(",")]
    elif isinstance(value, (list, tuple, set)):
        items = [str(item or "").strip() for item in value]
    else:
        items = []
    return [item for item in items if item]


def _workflow_step(step: str, response: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "step": step,
        "success": not isinstance(response, dict) or response.get("success", True) is not False,
    }
    if isinstance(response, dict):
        payload["output"] = response
    else:
        payload["output"] = {"value": response}
    return payload


def _workflow_failed_steps(steps: list[dict[str, Any]]) -> list[str]:
    return [str(item.get("step") or "") for item in steps if not item.get("success")]


async def _append_manager_step(
    steps: List[Dict[str, Any]],
    *,
    runtime_strategy_manager,
    action: str,
    params: Dict[str, Any] | None = None,
    step_name: str | None = None,
) -> Dict[str, Any]:
    output = await runtime_strategy_manager(action=action, params=params or {})
    steps.append(
        skill_support._step_result(
            step_name or f"strategy_manager.{action}",
            output=output,
        )
    )
    return output


async def build_strategy_review_workflow_payload(
    strategy_id: str,
    *,
    runtime_strategy_manager,
    build_strategy_review_payload: Callable[[str], Awaitable[Dict[str, Any]]] | None = None,
    include_factory_status: bool = True,
    include_review_report: bool = True,
    include_runtime_alerts: bool = True,
    run_factory_once: bool = False,
    run_runtime_cycle: bool = False,
    runtime_alert_limit: int = 20,
    as_of: str | None = None,
    actor_id: str | None = None,
    actor_roles: Any = None,
) -> Dict[str, Any]:
    resolved_strategy_id = _trim_text(strategy_id)
    if not resolved_strategy_id:
        raise ValueError("strategy_id is required")

    builder = build_strategy_review_payload
    if builder is None:
        from ..resources.strategy import build_strategy_review_payload as default_builder

        builder = default_builder

    steps: list[dict[str, Any]] = []
    resource_payload = await builder(resolved_strategy_id)
    steps.append(
        _workflow_step(
            "resource.strategy_review",
            {
                "success": bool(resource_payload.get("found", True)),
                "data": resource_payload,
            },
        )
    )

    closure_review_payload = await runtime_strategy_manager(
        action="closure_review",
        params={
            "strategy_id": resolved_strategy_id,
            **({"as_of": as_of} if _trim_text(as_of) else {}),
            **({"actor_id": actor_id} if _trim_text(actor_id) else {}),
            **({"actor_roles": actor_roles} if actor_roles not in (None, "", [], ()) else {}),
        },
    )
    steps.append(_workflow_step("strategy_manager.closure_review", closure_review_payload))

    if include_review_report:
        review_payload = await runtime_strategy_manager(
            action="review_report",
            params={"strategy_id": resolved_strategy_id},
        )
        steps.append(_workflow_step("strategy_manager.review_report", review_payload))

    if include_factory_status:
        factory_payload = await runtime_strategy_manager(action="factory_status", params={})
        steps.append(_workflow_step("strategy_manager.factory_status", factory_payload))

    if include_runtime_alerts:
        runtime_payload = await runtime_strategy_manager(
            action="runtime_alerts",
            params={"strategy_id": resolved_strategy_id, "limit": runtime_alert_limit},
        )
        steps.append(_workflow_step("strategy_manager.runtime_alerts", runtime_payload))

    if run_factory_once:
        factory_run_payload = await runtime_strategy_manager(action="factory_run_once", params={})
        steps.append(_workflow_step("strategy_manager.factory_run_once", factory_run_payload))

    if run_runtime_cycle:
        runtime_cycle_payload = await runtime_strategy_manager(action="runtime_cycle_run", params={})
        steps.append(_workflow_step("strategy_manager.runtime_cycle_run", runtime_cycle_payload))

    failed_steps = _workflow_failed_steps(steps)
    completed_stages = [step["step"] for step in steps if step.get("success")]
    closure_review_data = dict((closure_review_payload or {}).get("data") or {})
    closure_runtime = dict(closure_review_data.get("runtime") or {})
    closure_overview = dict(dict(closure_review_data.get("incubation") or {}).get("overview") or {})
    runtime_risk_events = closure_runtime.get("risk_events")
    open_risk_count = (
        len(list(runtime_risk_events or []))
        if isinstance(runtime_risk_events, list)
        else ((resource_payload.get("summary") or {}).get("open_risk_count"))
    )

    execution_reality_payload: dict[str, Any] | None = None
    try:
        from ..services.execution_reality import build_execution_reality_report

        execution_reality_payload = build_execution_reality_report(mode="backtest").to_dict()
    except Exception:
        execution_reality_payload = None

    result_payload: dict[str, Any] = {
        "workflow": "strategy_review_workflow",
        "strategy_id": resolved_strategy_id,
        "steps": steps,
        "summary": {
            "current_status": (
                closure_overview.get("status")
                or ((resource_payload.get("summary") or {}).get("current_status"))
            ),
            "open_risk_count": open_risk_count,
            "as_of": closure_review_data.get("as_of"),
            "correlation_id": closure_review_data.get("correlation_id"),
            "factory_run_id": closure_review_data.get("factory_run_id"),
            "owner_state": closure_review_data.get("owner_state"),
            "favorite_state": closure_review_data.get("favorite_state"),
            "paper_session_state": closure_review_data.get("paper_session_state"),
            "failed_steps": failed_steps,
        },
        "artifacts": {
            "strategy_review_resource": f"resource://strategy/{resolved_strategy_id}/review",
        },
        "closure_review": closure_review_data or None,
        "owner_state": closure_review_data.get("owner_state"),
        "favorite_state": closure_review_data.get("favorite_state"),
        "paper_session_state": closure_review_data.get("paper_session_state"),
        "presentation": closure_review_data.get("presentation"),
        "workflow_stage": {
            "completed_stages": completed_stages,
            "last_completed_stage": completed_stages[-1] if completed_stages else None,
            "recoverable": bool(failed_steps),
            "resume_hint": "retry_failed_steps" if failed_steps else None,
        },
    }
    if execution_reality_payload:
        result_payload["execution_reality"] = execution_reality_payload
    return result_payload


async def _exec_factory_cycle_task(
    params: Dict[str, Any],
    *,
    task_name: str,
    runtime_strategy_manager,
    allow_stateful: bool,
) -> Dict[str, Any]:
    strategy_id = _trim_text(params.get("strategy_id") or params.get("id"))
    run_id = _trim_text(params.get("run_id"))
    limit = max(1, min(skill_support._safe_int(params.get("limit"), 5), 100))
    trigger_factory_run = allow_stateful and _bool_flag(params.get("trigger_factory_run"))

    steps: List[Dict[str, Any]] = []
    await _append_manager_step(steps, runtime_strategy_manager=runtime_strategy_manager, action="capabilities")
    await _append_manager_step(steps, runtime_strategy_manager=runtime_strategy_manager, action="factory_status")
    if trigger_factory_run:
        await _append_manager_step(steps, runtime_strategy_manager=runtime_strategy_manager, action="factory_run_once")
    await _append_manager_step(
        steps,
        runtime_strategy_manager=runtime_strategy_manager,
        action="factory_runs",
        params={"limit": limit},
    )
    if run_id:
        await _append_manager_step(
            steps,
            runtime_strategy_manager=runtime_strategy_manager,
            action="factory_run_detail",
            params={"run_id": run_id},
        )
    if strategy_id:
        await _append_manager_step(
            steps,
            runtime_strategy_manager=runtime_strategy_manager,
            action="task_runs",
            params={"strategy_id": strategy_id, "limit": limit},
        )

    result = skill_support._finalize_skill_result(task_name, steps)
    result["summary"]["strategy_id"] = strategy_id or None
    result["summary"]["run_id"] = run_id or None
    result["summary"]["trigger_factory_run"] = trigger_factory_run
    return result


async def _exec_strategy_review_task(
    params: Dict[str, Any],
    *,
    runtime_strategy_manager,
) -> Dict[str, Any]:
    strategy_id = _trim_text(params.get("strategy_id") or params.get("id"))
    limit = max(1, min(skill_support._safe_int(params.get("limit"), 5), 100))
    runtime_alert_limit = max(
        1,
        min(skill_support._safe_int(params.get("runtime_alert_limit"), 20), 100),
    )
    trigger_factory_run = _bool_flag(params.get("trigger_factory_run"))
    trigger_runtime_cycle = _bool_flag(params.get("trigger_runtime_cycle"))

    steps: List[Dict[str, Any]] = []

    if strategy_id:
        workflow_payload = await build_strategy_review_workflow_payload(
            strategy_id,
            runtime_strategy_manager=runtime_strategy_manager,
            include_factory_status=_bool_flag(params.get("include_factory_status"), True),
            include_review_report=_bool_flag(params.get("include_review_report"), True),
            include_runtime_alerts=_bool_flag(params.get("include_runtime_alerts"), True),
            run_factory_once=trigger_factory_run,
            run_runtime_cycle=trigger_runtime_cycle,
            runtime_alert_limit=runtime_alert_limit,
            as_of=_trim_text(params.get("as_of")) or None,
        )
        steps.append(
            skill_support._step_result(
                "strategy_review_workflow.review",
                output={"success": True, "data": workflow_payload},
            )
        )
        await _append_manager_step(
            steps,
            runtime_strategy_manager=runtime_strategy_manager,
            action="events",
            params={"strategy_id": strategy_id, "limit": limit},
        )
        result = skill_support._finalize_skill_result("strategy_review", steps)
        result["summary"]["strategy_id"] = strategy_id
        result["summary"]["current_status"] = ((workflow_payload.get("summary") or {}).get("current_status"))
        result["summary"]["open_risk_count"] = ((workflow_payload.get("summary") or {}).get("open_risk_count"))
        result["summary"]["trigger_factory_run"] = trigger_factory_run
        result["summary"]["trigger_runtime_cycle"] = trigger_runtime_cycle
        return result

    await _append_manager_step(
        steps,
        runtime_strategy_manager=runtime_strategy_manager,
        action="rank",
        params={"status": "listed", "limit": limit},
    )
    await _append_manager_step(
        steps,
        runtime_strategy_manager=runtime_strategy_manager,
        action="list",
        params={"limit": limit},
    )
    result = skill_support._finalize_skill_result("strategy_review", steps)
    result["summary"]["strategy_id"] = None
    result["summary"]["trigger_factory_run"] = False
    result["summary"]["trigger_runtime_cycle"] = False
    return result


async def _exec_submission_gate_task(
    params: Dict[str, Any],
    *,
    runtime_strategy_manager,
) -> Dict[str, Any]:
    strategy_id = _trim_text(params.get("strategy_id") or params.get("id"))
    strategy_ids = _split_values(params.get("strategy_ids"))
    trigger_review_report_recheck = _bool_flag(params.get("trigger_review_report_recheck"))
    trigger_submission_replay = _bool_flag(params.get("trigger_submission_replay"))
    trigger_submit = _bool_flag(params.get("trigger_submit"))

    steps: List[Dict[str, Any]] = []
    if trigger_review_report_recheck and strategy_id:
        await _append_manager_step(
            steps,
            runtime_strategy_manager=runtime_strategy_manager,
            action="review_report_recheck",
            params={"strategy_id": strategy_id},
        )
    if trigger_submission_replay and (strategy_id or strategy_ids):
        replay_params: Dict[str, Any] = {
            "recheck_reports": _bool_flag(params.get("recheck_reports"), True),
        }
        if strategy_ids:
            replay_params["strategy_ids"] = strategy_ids
        elif strategy_id:
            replay_params["strategy_id"] = strategy_id
        await _append_manager_step(
            steps,
            runtime_strategy_manager=runtime_strategy_manager,
            action="submission_replay",
            params=replay_params,
        )
    if trigger_submit and strategy_id:
        await _append_manager_step(
            steps,
            runtime_strategy_manager=runtime_strategy_manager,
            action="submit",
            params={"strategy_id": strategy_id},
        )
    await _append_manager_step(
        steps,
        runtime_strategy_manager=runtime_strategy_manager,
        action="execution_audit_verification",
        params={"strategy_id": strategy_id} if strategy_id else {},
    )

    result = skill_support._finalize_skill_result("submission_gate", steps)
    result["summary"]["strategy_id"] = strategy_id or None
    result["summary"]["strategy_ids"] = strategy_ids
    result["summary"]["trigger_review_report_recheck"] = trigger_review_report_recheck
    result["summary"]["trigger_submission_replay"] = trigger_submission_replay
    result["summary"]["trigger_submit"] = trigger_submit
    return result


async def _exec_incubation_pipeline_task(
    params: Dict[str, Any],
    *,
    runtime_strategy_manager,
) -> Dict[str, Any]:
    strategy_id = _trim_text(params.get("strategy_id") or params.get("id"))
    limit = max(1, min(skill_support._safe_int(params.get("limit"), 20), 200))
    statuses = _split_values(params.get("statuses")) or ["incubating", "listed"]
    signal_date = _trim_text(params.get("signal_date")) or None
    pipeline_stage = _trim_text(params.get("pipeline_stage")) or None
    pipeline_status = _trim_text(params.get("pipeline_status")) or None
    source = _trim_text(params.get("source")) or "strategy_manager"
    auto_apply_review = _bool_flag(params.get("auto_apply_review"), True)
    auto_apply = _bool_flag(params.get("auto_apply"), False)
    trigger_incubation_sync = _bool_flag(params.get("trigger_incubation_sync"))
    trigger_incubation_pipeline_run = _bool_flag(params.get("trigger_incubation_pipeline_run"))
    trigger_promotion_review = _bool_flag(params.get("trigger_promotion_review"))

    steps: List[Dict[str, Any]] = []
    await _append_manager_step(
        steps,
        runtime_strategy_manager=runtime_strategy_manager,
        action="incubation_overview",
        params={"strategy_id": strategy_id} if strategy_id else {"limit": limit},
    )
    await _append_manager_step(
        steps,
        runtime_strategy_manager=runtime_strategy_manager,
        action="incubation_accounts",
        params={"strategy_id": strategy_id, "limit": limit} if strategy_id else {"limit": limit},
    )
    if strategy_id:
        await _append_manager_step(
            steps,
            runtime_strategy_manager=runtime_strategy_manager,
            action="incubation_metrics",
            params={
                "strategy_id": strategy_id,
                "limit": limit,
                "start_date": _trim_text(params.get("start_date")) or None,
                "end_date": _trim_text(params.get("end_date")) or None,
            },
        )
        await _append_manager_step(
            steps,
            runtime_strategy_manager=runtime_strategy_manager,
            action="paper_account",
            params={"strategy_id": strategy_id, "limit": limit},
        )
        await _append_manager_step(
            steps,
            runtime_strategy_manager=runtime_strategy_manager,
            action="paper_orders",
            params={
                "strategy_id": strategy_id,
                "signal_date": signal_date,
                "status": _trim_text(params.get("status")) or None,
                "limit": limit,
            },
        )
        await _append_manager_step(
            steps,
            runtime_strategy_manager=runtime_strategy_manager,
            action="paper_nav",
            params={"strategy_id": strategy_id, "limit": limit},
        )
    await _append_manager_step(
        steps,
        runtime_strategy_manager=runtime_strategy_manager,
        action="incubation_pipeline",
        params={
            **({"strategy_id": strategy_id} if strategy_id else {}),
            "pipeline_stage": pipeline_stage,
            "pipeline_status": pipeline_status,
            "limit": limit,
        },
    )
    await _append_manager_step(
        steps,
        runtime_strategy_manager=runtime_strategy_manager,
        action="promotion_reviews",
        params={
            **({"strategy_id": strategy_id} if strategy_id else {}),
            "status": _trim_text(params.get("status")) or None,
            "limit": limit,
        },
    )
    if trigger_incubation_sync:
        sync_params: Dict[str, Any] = {"signal_date": signal_date}
        if strategy_id:
            sync_params["strategy_id"] = strategy_id
        else:
            sync_params["statuses"] = statuses
            sync_params["limit"] = limit
        await _append_manager_step(
            steps,
            runtime_strategy_manager=runtime_strategy_manager,
            action="incubation_sync_run",
            params=sync_params,
        )
    if trigger_incubation_pipeline_run:
        pipeline_run_params: Dict[str, Any] = {
            "source": source,
            "auto_apply_review": auto_apply_review,
        }
        if strategy_id:
            pipeline_run_params["strategy_id"] = strategy_id
        else:
            pipeline_run_params["statuses"] = statuses
            pipeline_run_params["limit"] = limit
        await _append_manager_step(
            steps,
            runtime_strategy_manager=runtime_strategy_manager,
            action="incubation_pipeline_run",
            params=pipeline_run_params,
        )
    if trigger_promotion_review and strategy_id:
        await _append_manager_step(
            steps,
            runtime_strategy_manager=runtime_strategy_manager,
            action="promotion_review_run",
            params={"strategy_id": strategy_id, "auto_apply": auto_apply, "source": source},
        )

    result = skill_support._finalize_skill_result("incubation_pipeline", steps)
    result["summary"]["strategy_id"] = strategy_id or None
    result["summary"]["statuses"] = statuses
    result["summary"]["trigger_incubation_sync"] = trigger_incubation_sync
    result["summary"]["trigger_incubation_pipeline_run"] = trigger_incubation_pipeline_run
    result["summary"]["trigger_promotion_review"] = trigger_promotion_review
    return result


async def _exec_runtime_governance_task(
    params: Dict[str, Any],
    *,
    runtime_strategy_manager,
) -> Dict[str, Any]:
    strategy_id = _trim_text(params.get("strategy_id") or params.get("id"))
    limit = max(1, min(skill_support._safe_int(params.get("limit"), 20), 500))
    runtime_alert_limit = max(
        1,
        min(skill_support._safe_int(params.get("runtime_alert_limit"), 20), 500),
    )
    statuses = _split_values(params.get("statuses")) or ["incubating", "listed", "suspended"]
    source = _trim_text(params.get("source")) or "strategy_manager"
    event_id = skill_support._safe_int(params.get("event_id"), 0)
    alert_id = skill_support._safe_int(params.get("alert_id"), 0)
    trigger_risk_scan = _bool_flag(params.get("trigger_risk_scan"))
    trigger_risk_recovery = _bool_flag(params.get("trigger_risk_recovery"))
    trigger_resolve_risk_event = _bool_flag(params.get("trigger_resolve_risk_event"))
    trigger_runtime_alert_dispatch = _bool_flag(params.get("trigger_runtime_alert_dispatch"))
    trigger_runtime_alert_ack = _bool_flag(params.get("trigger_runtime_alert_ack"))
    trigger_runtime_control_set = _bool_flag(params.get("trigger_runtime_control_set"))
    trigger_runtime_cycle = _bool_flag(params.get("trigger_runtime_cycle"))

    steps: List[Dict[str, Any]] = []
    await _append_manager_step(steps, runtime_strategy_manager=runtime_strategy_manager, action="runtime_cycle_status")
    await _append_manager_step(
        steps,
        runtime_strategy_manager=runtime_strategy_manager,
        action="risk_events",
        params={
            **({"strategy_id": strategy_id} if strategy_id else {}),
            "account_id": _trim_text(params.get("account_id")) or None,
            "status": _trim_text(params.get("status")) or None,
            "severity": _trim_text(params.get("severity")) or None,
            "limit": limit,
        },
    )
    await _append_manager_step(
        steps,
        runtime_strategy_manager=runtime_strategy_manager,
        action="risk_snapshots",
        params={
            **({"strategy_id": strategy_id} if strategy_id else {}),
            "posture_level": _trim_text(params.get("posture_level")) or None,
            "control_mode": _trim_text(params.get("control_mode")) or None,
            "limit": limit,
        },
    )
    await _append_manager_step(
        steps,
        runtime_strategy_manager=runtime_strategy_manager,
        action="runtime_alerts",
        params={
            **({"strategy_id": strategy_id} if strategy_id else {}),
            "category": _trim_text(params.get("category")) or None,
            "severity": _trim_text(params.get("severity")) or None,
            "status": _trim_text(params.get("status")) or None,
            "limit": runtime_alert_limit,
        },
    )
    if strategy_id:
        await _append_manager_step(
            steps,
            runtime_strategy_manager=runtime_strategy_manager,
            action="runtime_control",
            params={"strategy_id": strategy_id},
        )
    if trigger_risk_scan:
        await _append_manager_step(
            steps,
            runtime_strategy_manager=runtime_strategy_manager,
            action="risk_scan_run",
            params={
                **({"strategy_id": strategy_id} if strategy_id else {}),
                "enforce_actions": _bool_flag(params.get("enforce_actions"), True),
            },
        )
    if trigger_risk_recovery and strategy_id:
        await _append_manager_step(
            steps,
            runtime_strategy_manager=runtime_strategy_manager,
            action="risk_recovery",
            params={"strategy_id": strategy_id, "source": source},
        )
    if trigger_resolve_risk_event and event_id > 0:
        await _append_manager_step(
            steps,
            runtime_strategy_manager=runtime_strategy_manager,
            action="resolve_risk_event",
            params={"event_id": event_id, "resolution": _trim_text(params.get("resolution")) or "manual_resolved"},
        )
    if trigger_runtime_alert_dispatch:
        dispatch_params: Dict[str, Any] = {"source": source}
        if strategy_id:
            dispatch_params["strategy_id"] = strategy_id
        else:
            dispatch_params["statuses"] = statuses
            dispatch_params["limit"] = limit
        await _append_manager_step(
            steps,
            runtime_strategy_manager=runtime_strategy_manager,
            action="runtime_alert_dispatch_run",
            params=dispatch_params,
        )
    if trigger_runtime_alert_ack and alert_id > 0:
        await _append_manager_step(
            steps,
            runtime_strategy_manager=runtime_strategy_manager,
            action="runtime_alert_ack",
            params={
                "alert_id": alert_id,
                "acknowledged_by": _trim_text(params.get("acknowledged_by")) or None,
                "source": source,
            },
        )
    if trigger_runtime_control_set and strategy_id:
        await _append_manager_step(
            steps,
            runtime_strategy_manager=runtime_strategy_manager,
            action="runtime_control_set",
            params={
                "strategy_id": strategy_id,
                "control_mode": _trim_text(params.get("control_mode")) or "active",
                "reason": _trim_text(params.get("reason")) or None,
                "source": source,
                "trigger_event_type": _trim_text(params.get("trigger_event_type")) or "manual_override",
            },
        )
    if trigger_runtime_cycle:
        await _append_manager_step(
            steps,
            runtime_strategy_manager=runtime_strategy_manager,
            action="runtime_cycle_run",
        )

    result = skill_support._finalize_skill_result("runtime_governance", steps)
    result["summary"]["strategy_id"] = strategy_id or None
    result["summary"]["statuses"] = statuses
    result["summary"]["trigger_runtime_cycle"] = trigger_runtime_cycle
    return result


async def _exec_vector_governance_task(
    params: Dict[str, Any],
    *,
    runtime_strategy_manager,
) -> Dict[str, Any]:
    strategy_id = _trim_text(params.get("strategy_id") or params.get("id"))
    similar_to = _trim_text(params.get("similar_to"))
    index_name = _trim_text(params.get("index_name")) or "strategy_behavior"
    index_version = _trim_text(params.get("index_version")) or None
    profile_type = _trim_text(params.get("profile_type")) or "behavior"
    limit = max(1, min(skill_support._safe_int(params.get("limit"), 20), 200))
    candidate_limit = max(
        1,
        min(skill_support._safe_int(params.get("candidate_limit"), 80), 500),
    )
    statuses = _split_values(params.get("statuses")) or ["incubating", "listed"]
    protect_versions = _split_values(params.get("protect_versions"))
    trigger_vector_reconcile = _bool_flag(params.get("trigger_vector_reconcile"))
    trigger_vector_rebuild = _bool_flag(params.get("trigger_vector_rebuild"))
    trigger_vector_cleanup = _bool_flag(params.get("trigger_vector_cleanup"))

    steps: List[Dict[str, Any]] = []
    await _append_manager_step(
        steps,
        runtime_strategy_manager=runtime_strategy_manager,
        action="vector_health",
        params={
            "index_name": index_name,
            "limit_versions": limit,
            "include_hnsw_indexes": _bool_flag(params.get("include_hnsw_indexes")),
            "include_embedding_smoke_check": _bool_flag(params.get("include_embedding_smoke_check")),
        },
    )
    await _append_manager_step(
        steps,
        runtime_strategy_manager=runtime_strategy_manager,
        action="vector_indexes",
        params={
            "index_name": index_name,
            "status": _trim_text(params.get("status")) or None,
            "limit": limit,
        },
    )
    await _append_manager_step(
        steps,
        runtime_strategy_manager=runtime_strategy_manager,
        action="vector_index_snapshots",
        params={
            "index_name": index_name,
            "index_version": index_version,
            "status": _trim_text(params.get("status")) or None,
            "limit": limit,
        },
    )
    await _append_manager_step(
        steps,
        runtime_strategy_manager=runtime_strategy_manager,
        action="vector_profiles",
        params={
            **({"strategy_id": strategy_id} if strategy_id else {}),
            **({"similar_to": similar_to} if similar_to else {}),
            "profile_type": profile_type,
            "limit": limit,
        },
    )
    if strategy_id or similar_to:
        ann_params: Dict[str, Any] = {
            "profile_type": profile_type,
            "candidate_limit": candidate_limit,
            "index_name": index_name,
            "index_version": index_version,
            "limit": limit,
        }
        if strategy_id:
            ann_params["strategy_id"] = strategy_id
        if similar_to:
            ann_params["similar_to"] = similar_to
        await _append_manager_step(
            steps,
            runtime_strategy_manager=runtime_strategy_manager,
            action="vector_ann_search",
            params=ann_params,
        )
    if trigger_vector_reconcile:
        await _append_manager_step(
            steps,
            runtime_strategy_manager=runtime_strategy_manager,
            action="vector_reconcile",
            params={
                "index_name": index_name,
                "profile_type": profile_type,
                "limit_profiles": max(1, min(skill_support._safe_int(params.get("limit_profiles"), 2000), 5000)),
            },
        )
    if trigger_vector_rebuild:
        await _append_manager_step(
            steps,
            runtime_strategy_manager=runtime_strategy_manager,
            action="vector_rebuild",
            params={
                "index_name": index_name,
                "index_version": index_version,
                "statuses": statuses,
                "limit": min(limit, 1000),
                "profile_type": profile_type,
                "vector_method": _trim_text(params.get("vector_method")) or None,
            },
        )
    if trigger_vector_cleanup:
        await _append_manager_step(
            steps,
            runtime_strategy_manager=runtime_strategy_manager,
            action="vector_cleanup",
            params={
                "index_name": index_name,
                "keep_versions": max(skill_support._safe_int(params.get("keep_versions"), 1), 0),
                "dry_run": _bool_flag(params.get("dry_run"), True),
                "cleanup_hnsw": _bool_flag(params.get("cleanup_hnsw"), True),
                "limit_versions": limit,
                "protect_versions": protect_versions,
            },
        )

    result = skill_support._finalize_skill_result("vector_governance", steps)
    result["summary"]["strategy_id"] = strategy_id or None
    result["summary"]["similar_to"] = similar_to or None
    result["summary"]["index_name"] = index_name
    result["summary"]["trigger_vector_reconcile"] = trigger_vector_reconcile
    result["summary"]["trigger_vector_rebuild"] = trigger_vector_rebuild
    result["summary"]["trigger_vector_cleanup"] = trigger_vector_cleanup
    return result


async def _exec_domain_projection_task(
    params: Dict[str, Any],
    *,
    runtime_strategy_manager,
) -> Dict[str, Any]:
    strategy_id = _trim_text(params.get("strategy_id") or params.get("id"))
    limit = max(1, min(skill_support._safe_int(params.get("limit"), 50), 500))
    statuses = _split_values(params.get("statuses")) or ["incubating", "listed", "suspended", "deprecated"]
    source = _trim_text(params.get("source")) or "strategy_manager"
    trigger_domain_projection_rebuild = _bool_flag(params.get("trigger_domain_projection_rebuild"))

    steps: List[Dict[str, Any]] = []
    await _append_manager_step(
        steps,
        runtime_strategy_manager=runtime_strategy_manager,
        action="domain_events",
        params={
            **({"strategy_id": strategy_id} if strategy_id else {}),
            "aggregate_type": _trim_text(params.get("aggregate_type")) or None,
            "event_type": _trim_text(params.get("event_type")) or None,
            "source": _trim_text(params.get("event_source")) or None,
            "correlation_id": _trim_text(params.get("correlation_id")) or None,
            "limit": limit,
        },
    )
    if strategy_id:
        await _append_manager_step(
            steps,
            runtime_strategy_manager=runtime_strategy_manager,
            action="domain_projection",
            params={"strategy_id": strategy_id, "limit": limit},
        )
        await _append_manager_step(
            steps,
            runtime_strategy_manager=runtime_strategy_manager,
            action="domain_projection_snapshot",
            params={"strategy_id": strategy_id, "limit": limit},
        )
    if trigger_domain_projection_rebuild:
        rebuild_params: Dict[str, Any] = {
            "limit": limit,
            "source": source,
        }
        if strategy_id:
            rebuild_params["strategy_id"] = strategy_id
        else:
            rebuild_params["statuses"] = statuses
        await _append_manager_step(
            steps,
            runtime_strategy_manager=runtime_strategy_manager,
            action="domain_projection_rebuild",
            params=rebuild_params,
        )

    result = skill_support._finalize_skill_result("domain_projection", steps)
    result["summary"]["strategy_id"] = strategy_id or None
    result["summary"]["statuses"] = statuses
    result["summary"]["trigger_domain_projection_rebuild"] = trigger_domain_projection_rebuild
    return result


async def _exec_ai_generation_task(
    params: Dict[str, Any],
    *,
    runtime_strategy_manager,
) -> Dict[str, Any]:
    strategy_id = _trim_text(params.get("strategy_id") or params.get("id"))
    limit = max(1, min(skill_support._safe_int(params.get("limit"), 20), 200))
    trigger_ai_generate = _bool_flag(params.get("trigger_ai_generate"))

    steps: List[Dict[str, Any]] = []
    await _append_manager_step(
        steps,
        runtime_strategy_manager=runtime_strategy_manager,
        action="ai_experiments",
        params={
            "strategy_id": strategy_id or None,
            "parent_strategy_id": _trim_text(params.get("parent_strategy_id")) or None,
            "generated_strategy_id": _trim_text(params.get("generated_strategy_id")) or None,
            "task_run_id": (
                skill_support._safe_int(params.get("task_run_id"), 0) or None
            ),
            "status": _trim_text(params.get("status")) or None,
            "source": _trim_text(params.get("source")) or None,
            "limit": limit,
        },
    )
    await _append_manager_step(
        steps,
        runtime_strategy_manager=runtime_strategy_manager,
        action="task_runs",
        params={
            "strategy_id": strategy_id or None,
            "task_name": _trim_text(params.get("task_name")) or None,
            "task_scope": _trim_text(params.get("task_scope")) or None,
            "status": _trim_text(params.get("task_status")) or None,
            "limit": limit,
        },
    )
    if trigger_ai_generate:
        await _append_manager_step(
            steps,
            runtime_strategy_manager=runtime_strategy_manager,
            action="ai_generate",
            params={
                "limit": min(limit, 10),
                "parent_strategy_id": _trim_text(params.get("parent_strategy_id")) or None,
                "auto_submit": _bool_flag(params.get("auto_submit")),
            },
        )

    result = skill_support._finalize_skill_result("ai_generation", steps)
    result["summary"]["strategy_id"] = strategy_id or None
    result["summary"]["trigger_ai_generate"] = trigger_ai_generate
    return result


async def exec_strategy_factory(
    params: Dict[str, Any],
    *,
    runtime_strategy_manager,
) -> Dict[str, Any]:
    task = _trim_text(params.get("task") or "factory_cycle").lower()
    if task not in SUPPORTED_STRATEGY_FACTORY_TASKS:
        return skill_support._unsupported_task_result(task, SUPPORTED_STRATEGY_FACTORY_TASKS)

    if task == "smoke_test":
        return await _exec_factory_cycle_task(
            params,
            task_name="smoke_test",
            runtime_strategy_manager=runtime_strategy_manager,
            allow_stateful=False,
        )
    if task == "factory_cycle":
        return await _exec_factory_cycle_task(
            params,
            task_name="factory_cycle",
            runtime_strategy_manager=runtime_strategy_manager,
            allow_stateful=True,
        )
    if task == "strategy_review":
        return await _exec_strategy_review_task(params, runtime_strategy_manager=runtime_strategy_manager)
    if task == "submission_gate":
        return await _exec_submission_gate_task(params, runtime_strategy_manager=runtime_strategy_manager)
    if task == "incubation_pipeline":
        return await _exec_incubation_pipeline_task(params, runtime_strategy_manager=runtime_strategy_manager)
    if task == "runtime_governance":
        return await _exec_runtime_governance_task(params, runtime_strategy_manager=runtime_strategy_manager)
    if task == "vector_governance":
        return await _exec_vector_governance_task(params, runtime_strategy_manager=runtime_strategy_manager)
    if task == "domain_projection":
        return await _exec_domain_projection_task(params, runtime_strategy_manager=runtime_strategy_manager)
    return await _exec_ai_generation_task(params, runtime_strategy_manager=runtime_strategy_manager)
