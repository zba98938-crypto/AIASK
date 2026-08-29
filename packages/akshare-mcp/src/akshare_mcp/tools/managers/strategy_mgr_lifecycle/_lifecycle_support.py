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


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def _strategy_factory_inline_execution_enabled() -> bool:
    return _env_bool("STRATEGY_FACTORY_INLINE_EXECUTION_ENABLED", False)


def _load_signal_quality_registry_snapshot():
    try:
        from ....services.signal_quality_registry import (
            get_default_signal_quality_registry,
            get_default_signal_quality_registry_snapshot,
        )

        registry = get_default_signal_quality_registry()
        snapshot = dict(get_default_signal_quality_registry_snapshot() or {})
        return {
            **snapshot,
            "snapshot": snapshot,
            "drift": dict(registry.drift_check() or {}),
            "recent_probability": list(registry.recent_probability(5)),
            "recent_sentiment": list(registry.recent_sentiment(5)),
            "recent_factor": list(registry.recent_factor(5)),
        }
    except Exception:
        return {}


def _execution_audit_entity_chain_available(db) -> bool:
    required_methods = (
        "list_strategy_paper_orders",
        "list_strategy_paper_trades",
        "list_strategy_trade_positions",
        "list_strategy_trade_position_fills",
        "get_strategy_trade_audit_summary",
        "get_paper_nav_rows",
    )
    return all(hasattr(db, method) for method in required_methods)

_TRADE_AUDIT_BACKTEST_KEYS = (
    "post_cost_sharpe",
    "target_layer_oos_return",
    "target_layer_abnormal_return",
    "event_window_hit_ratio",
    "post_event_decay",
    "trade_density",
    "parameter_perturbation_trade_stability",
    "primary_validation_layer",
)


def _quality_report_submission_audit(report: Optional[dict]) -> dict:
    payload = dict(report or {})
    summary = dict(payload.get("summary") or {})
    fields = (
        "committee_review",
        "task_signature",
        "refresh_mode",
        "submission_lane",
        "direct_trade_candidate",
        "live_review_ready",
        "paper_lane_ready",
        "paper_account_id",
        "paper_account_status",
        "runtime_control_mode",
        "runtime_control_status",
        "promotion_review_id",
        "promotion_review_status",
        "promotion_review_recommendation",
        "pool_admission_applied",
        "promotion_applied_transition",
        "submission_action",
        "submission_action_type",
        "submission_action_trigger",
        "submission_action_gaps",
        "submission_action_fallback_conditions",
        "submission_action_next_step",
        "submission_action_completed",
        "task_preference",
        "candidate_provenance",
        "trend_cluster_ratio",
        "diversification_debt",
        "pool_profile_distribution",
        "review_issue_buckets",
        "review_issue_primary",
    )
    return {
        field: (payload.get(field) if payload.get(field) not in (None, "", [], {}) else summary.get(field))
        for field in fields
        if (
            payload.get(field) not in (None, "", [], {})
            or summary.get(field) not in (None, "", [], {})
        )
    }


def _run_once_accepts_db_arg(run_once) -> bool:
    """Prefer passing the current db, but tolerate scheduler stubs used in tests."""
    try:
        params = list(inspect.signature(run_once).parameters.values())
    except (TypeError, ValueError):
        return True
    return any(
        param.name == "db"
        or param.kind in (
            inspect.Parameter.VAR_POSITIONAL,
            inspect.Parameter.VAR_KEYWORD,
        )
        for param in params
    )


def _call_supports_parameter(callable_obj, name: str) -> bool:
    try:
        params = inspect.signature(callable_obj).parameters
    except (TypeError, ValueError):
        return False
    if name in params:
        return True
    return any(param.kind == inspect.Parameter.VAR_KEYWORD for param in params.values())


def _get_strategy_factory_scheduler_with_runtime(db):
    from strategy_factory.api import get_strategy_factory_scheduler
    from strategy_factory.api.runtime import build_scheduler_runtime_kwargs
    from strategy_factory.runtime.default_bootstrap import ensure_default_runtime_services

    ensure_default_runtime_services()
    return get_strategy_factory_scheduler(**build_scheduler_runtime_kwargs(db=db))


def _factory_defaults_from_constants(factory_constants: dict[str, Any]) -> dict[str, Any]:
    return {
        "running": False,
        "schedule_mode": str(factory_constants.get("FACTORY_SCHEDULE_MODE") or "continuous"),
        "runtime_enabled": bool(factory_constants.get("FACTORY_RUNTIME_ENABLED")),
        "event_runtime_mode": str(factory_constants.get("FACTORY_EVENT_RUNTIME_MODE") or ""),
        "last_run": None,
        "last_result": None,
        "last_summary": {},
        "daily_run_count": 0,
        "max_daily_runs": int(factory_constants.get("FACTORY_MAX_DAILY_RUNS") or 0),
        "cycle_count": 0,
        "factor_auto_refresh_enabled": bool(factory_constants.get("FACTORY_FACTOR_AUTO_REFRESH")),
        "factor_refresh_timeout_sec": int(factory_constants.get("FACTORY_FACTOR_REFRESH_TIMEOUT_SEC") or 0),
        "readiness_hard_block_enabled": bool(factory_constants.get("FACTORY_READINESS_HARD_BLOCK")),
        "readiness_min_score": float(factory_constants.get("FACTORY_READINESS_MIN_SCORE") or 0.0),
        "readiness_min_completion_ratio": float(
            factory_constants.get("FACTORY_READINESS_MIN_COMPLETION_RATIO") or 0.0
        ),
        "execution_mode": "stock_first_observe_primary",
        "engine_version": "strategy_factory.v2",
        "latest_parity_result": {},
        "active_dispatch_id": None,
        "latest_dispatch_id": None,
        "ownership": {
            "mode": "external_runner",
            "runner_required": True,
            "inline_execution_enabled": _strategy_factory_inline_execution_enabled(),
        },
    }


async def _create_external_factory_dispatch(db, params: dict, *, source_action: str) -> dict:
    if not hasattr(db, "create_strategy_factory_dispatch"):
        return fail("factory dispatch storage is unavailable", error_code="STRATEGY_FACTORY_DISPATCH_UNAVAILABLE")
    execution_mode = (
        str(params.get("execution_mode") or "stock_first_observe_primary").strip()
        or "stock_first_observe_primary"
    )
    dispatch_id = str(params.get("dispatch_id") or "").strip() or f"factory_dispatch_{uuid4().hex[:12]}"
    target_codes = params.get("target_codes") or params.get("codes") or []
    if isinstance(target_codes, str):
        target_codes = [target_codes]
    metadata = {
        "source_action": source_action,
        "runner": "external",
        "target_codes": list(target_codes or []),
        **dict(params.get("metadata") or {}),
    }
    payload = {
        "dispatch_id": dispatch_id,
        "status": "queued",
        "execution_mode": execution_mode,
        "requested_at": datetime.now(timezone.utc).isoformat(),
        "message": "Strategy Factory dispatch queued for the standalone runner.",
        "metadata": metadata,
    }
    persisted = await db.create_strategy_factory_dispatch(payload)
    return ok(
        {
            **dict(persisted or payload),
            "accepted": True,
            "queued": True,
            "already_running": False,
            "runner_required": True,
            "execution_owner": "strategy_factory_runner",
            "inline_execution_enabled": False,
        }
    )


def _hydrate_factory_run_artifacts(row: dict, artifacts: list[dict[str, Any]]) -> dict:
    payload = dict(row or {})
    artifact_items = [dict(item or {}) for item in list(artifacts or []) if isinstance(item, dict)]
    artifact_by_type = {
        str(item.get("artifact_type") or "").strip(): dict(item.get("payload_json") or {})
        for item in artifact_items
        if str(item.get("artifact_type") or "").strip()
    }
    if artifact_by_type.get("research_plane"):
        payload["research_plane"] = dict(artifact_by_type.get("research_plane") or {})
    if artifact_by_type.get("governance_plane"):
        payload["governance_plane"] = dict(artifact_by_type.get("governance_plane") or {})
    if artifact_by_type.get("quality_gate"):
        payload["quality_gate"] = dict(artifact_by_type.get("quality_gate") or {})
    if artifact_by_type.get("backtest_report"):
        payload["backtest_report"] = dict(artifact_by_type.get("backtest_report") or {})
    if artifact_by_type.get("shadow_diff"):
        payload["parity_result"] = dict(artifact_by_type.get("shadow_diff") or {})
    if artifact_by_type.get("task_artifact"):
        payload["task_artifact"] = dict(artifact_by_type.get("task_artifact") or {})
    if artifact_by_type.get("candidate_artifact"):
        payload["candidate_artifact"] = dict(artifact_by_type.get("candidate_artifact") or {})
    if artifact_by_type.get("evidence_artifact"):
        payload["evidence_artifact"] = dict(artifact_by_type.get("evidence_artifact") or {})
    if artifact_by_type.get("submission_audit"):
        payload["submission_artifact"] = dict(artifact_by_type.get("submission_audit") or {})
    payload["artifacts"] = artifact_items
    payload["artifact_refs"] = list(payload.get("artifact_refs") or [
        {
            "artifact_type": item.get("artifact_type"),
            "artifact_version": item.get("artifact_version"),
            "payload_hash": item.get("payload_hash"),
            "storage_mode": item.get("storage_mode"),
        }
        for item in artifact_items
    ])
    return payload


def _factory_run_artifact_refs(artifacts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    refs: list[dict[str, Any]] = []
    for item in list(artifacts or []):
        if not isinstance(item, dict):
            continue
        artifact_type = str(item.get("artifact_type") or "").strip()
        if not artifact_type:
            continue
        refs.append(
            {
                "id": item.get("id"),
                "run_id": item.get("run_id"),
                "artifact_type": artifact_type,
                "artifact_version": item.get("artifact_version"),
                "payload_hash": item.get("payload_hash"),
                "storage_mode": item.get("storage_mode"),
                "created_at": item.get("created_at"),
            }
        )
    return refs


def _select_latest_backtest_metrics(
    metrics_list: list[dict] | None,
    fallback: Optional[dict] = None,
) -> dict:
    items = list(metrics_list or [])
    fallback_payload = dict(fallback or {})
    preferred_periods = ("backtest", "all")
    for period in preferred_periods:
        matched = next((item for item in items if str(item.get("period") or "").strip().lower() == period), None)
        if matched:
            return {
                **fallback_payload,
                **dict(matched),
            }
    return fallback_payload


def _trade_audit_backtest_score(metrics: Optional[dict]) -> int:
    payload = dict(metrics or {})
    if not payload:
        return 0
    present_keys = sum(1 for key in _TRADE_AUDIT_BACKTEST_KEYS if payload.get(key) is not None)
    return present_keys * 100 + len(payload)


def _select_rich_backtest_metrics_from_reports(
    reports: list[dict] | None,
    fallback: Optional[dict] = None,
) -> dict:
    fallback_payload = dict(fallback or {})
    best_payload = dict(fallback_payload)
    best_score = _trade_audit_backtest_score(best_payload)
    for report in list(reports or []):
        candidate = dict((report or {}).get("backtest_metrics") or {})
        candidate_score = _trade_audit_backtest_score(candidate)
        if candidate_score > best_score:
            best_payload = candidate
            best_score = candidate_score
    if not best_payload:
        return fallback_payload
    merged = dict(best_payload)
    for key, value in fallback_payload.items():
        if value is not None:
            merged[key] = value
    return merged


async def _run_recheck_validation_report(db, strategy: dict) -> dict:
    from strategy_factory.api.quality_reporting import run_validation_report

    return (
        await run_validation_report(
            str(strategy.get("strategy_type") or ""),
            dict(strategy.get("params") or {}),
            db,
        )
    ) or {}


async def _run_recheck_risk_report(db, strategy: dict) -> dict:
    from strategy_factory.api.quality_reporting import run_risk_report

    return (
        await run_risk_report(
            str(strategy.get("strategy_type") or ""),
            dict(strategy.get("params") or {}),
            db,
        )
    ) or {}


async def _build_recheck_quality_inputs(db, strategy: dict, latest_report: Optional[dict]) -> tuple[dict, dict, dict]:
    strategy_id = str(strategy.get("id") or "")
    recent_reports = await list_quality_reports(db, strategy_id, limit=12)
    submission_report = None
    if strategy_id and hasattr(db, "get_strategy_quality_report"):
        submission_report = await db.get_strategy_quality_report(strategy_id, "submission")
    validation_report = dict((latest_report or {}).get("validation_report") or {})
    risk_report = dict((latest_report or {}).get("risk_report") or {})
    backtest_metrics = dict((latest_report or {}).get("backtest_metrics") or {})
    backtest_metrics = _select_rich_backtest_metrics_from_reports(
        [dict(latest_report or {}), dict(submission_report or {}), *list(recent_reports or [])],
        backtest_metrics,
    )

    metrics_list = await db.get_strategy_metrics(strategy["id"]) if hasattr(db, "get_strategy_metrics") else []
    backtest_metrics = _select_latest_backtest_metrics(metrics_list, backtest_metrics)

    try:
        fresh_validation_report = await _run_recheck_validation_report(db, strategy)
    except Exception as exc:
        logger.warning(
            "strategy_manager.review_report_recheck validation recompute failed for %s: %s",
            strategy.get("id"),
            exc,
        )
        fresh_validation_report = {}
    if fresh_validation_report:
        validation_report = fresh_validation_report

    try:
        fresh_risk_report = await _run_recheck_risk_report(db, strategy)
    except Exception as exc:
        logger.warning(
            "strategy_manager.review_report_recheck risk recompute failed for %s: %s",
            strategy.get("id"),
            exc,
        )
        fresh_risk_report = {}
    if fresh_risk_report:
        risk_report = fresh_risk_report

    return validation_report, risk_report, backtest_metrics


async def _refresh_closure_review_after_mutation(
    db,
    strategy: dict,
    *,
    as_of: Optional[str],
    correlation_id: Optional[str] = None,
    actor_id: Optional[str] = None,
    actor_roles: Any = None,
) -> Optional[dict]:
    try:
        return await build_closure_review(
            db,
            strategy,
            as_of=as_of,
            correlation_id=correlation_id,
            actor_id=actor_id,
            actor_roles=actor_roles,
            force_recompute=True,
        )
    except TypeError:
        return await build_closure_review(
            db,
            strategy,
            as_of=as_of,
            correlation_id=correlation_id,
            actor_id=actor_id,
            actor_roles=actor_roles,
        )
    except Exception as exc:
        logger.warning(
            "strategy_manager closure review refresh failed for %s: %s",
            strategy.get("id"),
            exc,
        )
        return None
