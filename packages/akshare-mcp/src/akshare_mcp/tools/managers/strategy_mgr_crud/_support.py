"""Strategy manager CRUD action handlers."""

import asyncio
import os
import random
import time
from datetime import datetime, timezone
from uuid import uuid4

from aiask_quant_core.strategy_explanation import build_strategy_explanation

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


def _load_signal_quality_registry_snapshot():
    try:
        from ...services.signal_quality_registry import (
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


async def _resolved(value):
    return value


def _trimmed(value) -> str:
    return str(value or "").strip()


def _actor_context(params: dict) -> tuple[str | None, list[str]]:
    actor_id = _trimmed(params.get("actor_id") or params.get("user_id")) or None
    actor_roles = normalize_actor_roles(
        params.get("actor_roles")
        or params.get("roles")
        or params.get("actor_role")
        or params.get("role")
    )
    return actor_id, actor_roles


def _strategy_source_strategy_id(strategy: dict | None) -> str | None:
    payload = dict(strategy or {})
    metadata = dict(dict(payload.get("params") or {}).get("metadata") or {})
    value = _trimmed(metadata.get("source_strategy_id"))
    return value or None


def _ensure_personal_strategy_mutation_allowed(
    strategy: dict | None,
    *,
    actor_id: str | None,
    actor_roles: list[str],
) -> str | None:
    payload = dict(strategy or {})
    if not payload:
        return "Strategy not found"
    if is_admin_actor(actor_roles):
        return None
    if not actor_id:
        return "actor_id is required"
    if _trimmed(payload.get("author_id")) != actor_id:
        return "only the strategy owner can modify this strategy"
    if not is_personal_strategy(payload):
        return "market strategies are read-only"
    return None


async def _load_personal_strategy_surface_state(
    db,
    strategy: dict | None,
    *,
    actor_id: str | None,
    actor_roles: list[str],
) -> tuple[dict, dict, dict]:
    payload = dict(strategy or {})
    owner_state = build_owner_state(payload, actor_id=actor_id, actor_roles=actor_roles)
    is_favorited = False
    if actor_id and payload.get("id") and hasattr(db, "is_subscribed"):
        try:
            is_favorited = bool(await db.is_subscribed(str(payload.get("id")), actor_id))
        except Exception:
            is_favorited = False
    favorite_state = build_favorite_state(actor_id=actor_id, is_favorited=is_favorited)
    paper_session = None
    if actor_id and payload.get("id") and hasattr(db, "get_strategy_paper_session"):
        try:
            paper_session = await db.get_strategy_paper_session(str(payload.get("id")), actor_id)
        except Exception:
            paper_session = None
    paper_session_state = build_paper_session_state(paper_session, actor_id=actor_id)
    return owner_state, favorite_state, paper_session_state


_PERSONAL_STRATEGY_FOCUS_FIELDS = (
    "name",
    "description",
    "params",
    "factor_weights",
    "tags",
)


def _clean_string_list(items) -> list[str]:
    return list(dict.fromkeys(
        [str(item or "").strip() for item in list(items or []) if str(item or "").strip()]
    ))


def _normalize_personal_strategy_focus_fields(value) -> list[str]:
    if isinstance(value, str):
        candidates = value.replace(";", ",").split(",")
    elif isinstance(value, (list, tuple, set)):
        candidates = list(value)
    else:
        candidates = []
    focus_fields: list[str] = []
    allowed = set(_PERSONAL_STRATEGY_FOCUS_FIELDS)
    for item in candidates:
        normalized = _trimmed(item).lower()
        if normalized and normalized in allowed and normalized not in focus_fields:
            focus_fields.append(normalized)
    return focus_fields


def _sanitize_personal_strategy_snapshot(strategy: dict | None) -> dict:
    payload = dict(strategy or {})
    params = dict(payload.get("params") or {})
    return {
        "id": _trimmed(payload.get("id")) or None,
        "name": _trimmed(payload.get("name")) or "",
        "description": _trimmed(payload.get("description")) or "",
        "strategy_type": _trimmed(payload.get("strategy_type")) or None,
        "status": _trimmed(payload.get("status")) or None,
        "author_id": _trimmed(payload.get("author_id")) or None,
        "tags": _clean_string_list(payload.get("tags")),
        "params": params,
        "factor_weights": dict(payload.get("factor_weights") or {}),
        "metadata": dict(params.get("metadata") or {}),
    }


def _resolve_strategy_status_filter(raw_status, *, default: str = "visible"):
    raw = str(raw_status or default).strip()
    if not raw:
        raw = default
    tokens = [item.strip() for item in raw.replace("|", ",").split(",") if item.strip()]
    lowered = [token.lower() for token in tokens]
    if any(token in {"all", "*"} for token in lowered):
        return None
    if len(lowered) == 1 and lowered[0] in {"visible", "active", "market", "marketplace"}:
        return ["incubating", "listed"]
    normalized = [normalize_status_alias(token) for token in tokens]
    normalized = [token for token in normalized if token]
    if not normalized:
        return ["incubating", "listed"]
    return normalized[0] if len(normalized) == 1 else normalized


async def _load_similar_vector_profiles(db, strategy_id: str) -> list:
    try:
        from ...services.vector_platform import get_strategy_vector_platform
        return await get_strategy_vector_platform().find_similar_profiles(db, strategy_id, limit=5)
    except Exception as exc:
        logger.warning("strategy_manager.detail similar profiles failed for %s: %s", strategy_id, exc)
        return []


async def _load_vector_profiles(db, strategy_id: str, *, limit: int = 3) -> list:
    try:
        from ...services.vector_platform import get_strategy_vector_platform

        return await get_strategy_vector_platform().list_profiles(
            db,
            strategy_id=strategy_id,
            limit=max(1, min(int(limit or 3), 20)),
        )
    except Exception as exc:
        logger.warning("strategy_manager.detail vector profiles failed for %s: %s", strategy_id, exc)
        if hasattr(db, "list_strategy_vector_profiles"):
            return await db.list_strategy_vector_profiles(strategy_id=strategy_id, limit=max(1, min(int(limit or 3), 20)))
        return []


async def _load_latest_vector_index_snapshot(db, index_name: str = "strategy_behavior") -> dict | None:
    try:
        from ...services.vector_platform import get_strategy_vector_platform

        rows = await get_strategy_vector_platform().list_index_snapshots(
            db,
            index_name=index_name,
            limit=1,
        )
        return rows[0] if rows else None
    except Exception as exc:
        logger.warning("strategy_manager.detail latest vector snapshot failed for %s: %s", index_name, exc)
        if hasattr(db, "get_latest_strategy_vector_index_snapshot"):
            return await db.get_latest_strategy_vector_index_snapshot(index_name)
        return None


def _extract_strategy_market_summary_value(strategy: dict, key: str):
    value = strategy.get(key)
    if value not in (None, ""):
        return value
    params = strategy.get("params")
    if isinstance(params, dict):
        return params.get(key)
    return None


def _normalize_strategy_status_value(value) -> str:
    raw = _trimmed(value)
    if not raw:
        return ""
    normalized = normalize_status_alias(raw)
    return _trimmed(normalized or raw).lower()


def _incubation_surface_issue_count(value) -> int:
    if isinstance(value, (list, tuple, set)):
        return len([item for item in value if _trimmed(item)])
    return 0


def _incubation_surface_bool(value) -> bool | None:
    if isinstance(value, bool):
        return value
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return bool(value)
    normalized = _trimmed(value).lower()
    if normalized in {"1", "true", "yes", "y", "on"}:
        return True
    if normalized in {"0", "false", "no", "n", "off"}:
        return False
    return None


def _closure_snapshot_overview_payload(snapshot: dict | None, *, strategy_status: str) -> dict:
    payload = dict((snapshot or {}).get("snapshot") or {})
    metadata = dict((snapshot or {}).get("metadata") or {})
    snapshot_status = _normalize_strategy_status_value(metadata.get("strategy_status"))
    if snapshot_status and strategy_status and snapshot_status != strategy_status:
        return {}
    return payload


def _resolve_incubation_surface_stage(
    *,
    strategy_status: str,
    paper_account: dict | None = None,
    latest_pipeline_snapshot: dict | None = None,
    overview: dict | None = None,
    incubation_account: dict | None = None,
) -> tuple[str, str]:
    account_payload = dict(paper_account or {})
    account_stage = _trimmed(account_payload.get("incubation_stage"))
    if account_stage:
        return account_stage, "paper_account"

    account_status = _normalize_strategy_status_value(account_payload.get("status"))
    promotion_candidate = bool(account_payload.get("promotion_candidate"))
    if account_payload:
        if account_status in {"retired"}:
            return "promoted", "paper_account_status"
        if account_status in {"frozen", "failed", "archived"}:
            return "failed", "paper_account_status"
        if account_status == "guarded":
            return "observe", "paper_account_status"
        if account_status == "active":
            return ("candidate" if promotion_candidate else "warmup"), "paper_account_status"

    for value, source in (
        ((latest_pipeline_snapshot or {}).get("pipeline_stage"), "pipeline"),
        ((overview or {}).get("pipeline_stage"), "overview"),
        ((incubation_account or {}).get("stage"), "binding"),
    ):
        normalized = _trimmed(value)
        if normalized:
            return normalized, source
    if strategy_status == "listed":
        return "promoted", "status_fallback"
    if strategy_status == "incubating":
        return "observe", "status_fallback"
    return "not_started", "status_fallback"


def _build_strategy_incubation_surface(
    strategy: dict,
    *,
    paper_account: dict | None = None,
    latest_pipeline_snapshot: dict | None = None,
    overview: dict | None = None,
    incubation_account: dict | None = None,
    latest_metric: dict | None = None,
) -> dict:
    strategy_status = _normalize_strategy_status_value(strategy.get("status"))
    actual_account = dict(paper_account or {})
    snapshot = dict(latest_pipeline_snapshot or {})
    overview_payload = dict(overview or {})
    account = dict(incubation_account or {})
    metric = dict(latest_metric or {})
    pipeline_stage, stage_source = _resolve_incubation_surface_stage(
        strategy_status=strategy_status,
        paper_account=actual_account,
        latest_pipeline_snapshot=snapshot,
        overview=overview_payload,
        incubation_account=account,
    )
    snapshot_summary = dict(snapshot.get("summary") or {})
    hard_gate_result = dict(snapshot.get("hard_gate_result") or {})
    promotion_ready = _incubation_surface_bool(actual_account.get("promotion_candidate"))
    if promotion_ready is None:
        promotion_ready = _incubation_surface_bool(overview_payload.get("promotion_ready"))
    if promotion_ready is None:
        promotion_ready = _incubation_surface_bool(snapshot_summary.get("promotion_ready"))
    if promotion_ready is None:
        promotion_ready = pipeline_stage in {"graduation_ready", "promoted"}

    blockers = overview_payload.get("blockers")
    if blockers is None:
        blockers = snapshot.get("blockers")
    risk_flags = overview_payload.get("risk_flags")
    if risk_flags is None:
        risk_flags = snapshot.get("risk_flags")

    entered_incubator = bool(
        actual_account
        or snapshot
        or overview_payload
        or account
        or pipeline_stage != "not_started"
        or strategy_status in {"incubating", "listed"}
    )

    return {
        "entered_incubator": entered_incubator,
        "pipeline_stage": pipeline_stage,
        "stage_source": stage_source,
        "account_stage": _trimmed(actual_account.get("incubation_stage")) or None,
        "account_status": _trimmed(actual_account.get("status")) or None,
        "promotion_ready": bool(promotion_ready),
        "latest_decision": _trimmed(snapshot.get("latest_decision")) or _trimmed(metric.get("decision")) or None,
        "execution_audit_gate_status": (
            _trimmed(overview_payload.get("execution_audit_gate_status"))
            or _trimmed(hard_gate_result.get("execution_audit_gate_status"))
            or _trimmed(snapshot_summary.get("execution_audit_gate_status"))
            or None
        ),
        "blocker_count": _incubation_surface_issue_count(blockers),
        "risk_count": _incubation_surface_issue_count(risk_flags),
    }


async def _load_strategy_incubation_surface(db, strategy: dict) -> dict:
    sid = _trimmed((strategy or {}).get("id"))
    if not sid:
        return _build_strategy_incubation_surface(strategy or {})

    paper_account_task = (
        db.get_paper_account_by_strategy(sid)
        if hasattr(db, "get_paper_account_by_strategy")
        else _resolved(None)
    )
    latest_pipeline_snapshot_task = (
        db.get_latest_strategy_incubation_pipeline_snapshot(sid)
        if hasattr(db, "get_latest_strategy_incubation_pipeline_snapshot")
        else _resolved(None)
    )
    incubation_account_task = (
        db.get_strategy_incubation_account(sid)
        if hasattr(db, "get_strategy_incubation_account")
        else _resolved(None)
    )
    closure_snapshot_task = (
        db.get_latest_strategy_closure_snapshot(sid, snapshot_type="incubation_overview")
        if hasattr(db, "get_latest_strategy_closure_snapshot")
        else _resolved(None)
    )
    paper_account, latest_pipeline_snapshot, incubation_account, closure_snapshot = await asyncio.gather(
        paper_account_task,
        latest_pipeline_snapshot_task,
        incubation_account_task,
        closure_snapshot_task,
    )

    strategy_status = _normalize_strategy_status_value((strategy or {}).get("status"))
    overview = _closure_snapshot_overview_payload(closure_snapshot, strategy_status=strategy_status)
    if not overview and not paper_account and strategy_status in {"incubating", "listed", "deprecated", "suspended"}:
        overview = await _resolve_strategy_incubation_overview(db, strategy) or {}

    pipeline_stage, _ = _resolve_incubation_surface_stage(
        strategy_status=strategy_status,
        paper_account=paper_account,
        latest_pipeline_snapshot=latest_pipeline_snapshot,
        overview=overview,
        incubation_account=incubation_account,
    )
    latest_metric = None
    if (
        hasattr(db, "get_latest_strategy_incubation_metric")
        and not _trimmed((latest_pipeline_snapshot or {}).get("latest_decision"))
        and pipeline_stage != "not_started"
    ):
        latest_metric = await db.get_latest_strategy_incubation_metric(sid)

    return _build_strategy_incubation_surface(
        strategy or {},
        paper_account=paper_account,
        latest_pipeline_snapshot=latest_pipeline_snapshot,
        overview=overview,
        incubation_account=incubation_account,
        latest_metric=latest_metric,
    )


def _build_strategy_market_summary(
    strategy: dict,
    *,
    metrics: dict | None = None,
    incubation_surface: dict | None = None,
) -> dict:
    params = dict(strategy.get("params") or {})
    strategy_explanation = dict(
        strategy.get("strategy_explanation")
        or params.get("strategy_explanation")
        or {}
    ) or build_strategy_explanation(
        strategy,
        metrics=metrics,
        source="strategy_market_summary",
    )
    summary = {
        "id": strategy.get("id"),
        "name": strategy.get("name"),
        "strategy_type": strategy.get("strategy_type"),
        "description": strategy.get("description"),
        "strategy_explanation_summary": strategy_explanation.get("summary"),
        "strategy_explanation_labels": list(strategy_explanation.get("labels") or []),
        "status": strategy.get("status"),
        "subscriber_count": strategy.get("subscriber_count"),
        "avg_rating": strategy.get("avg_rating"),
        "review_count": strategy.get("review_count"),
        "sample_start_date": _extract_strategy_market_summary_value(strategy, "sample_start_date"),
        "sample_end_date": _extract_strategy_market_summary_value(strategy, "sample_end_date"),
        "turnover_rate": _extract_strategy_market_summary_value(strategy, "turnover_rate"),
        "capacity": _extract_strategy_market_summary_value(strategy, "capacity"),
        "capacity_label": _extract_strategy_market_summary_value(strategy, "capacity_label"),
        "incubation_surface": incubation_surface,
    }

    if metrics:
        metric_summary = {
            "total_return": metrics.get("total_return"),
            "annual_return": metrics.get("annual_return"),
            "sharpe_ratio": metrics.get("sharpe_ratio"),
            "max_drawdown": metrics.get("max_drawdown"),
            "win_rate": metrics.get("win_rate"),
        }
        summary["metrics"] = metric_summary
        summary["total_return"] = metric_summary["total_return"]
        summary["annual_return"] = metric_summary["annual_return"]
        summary["sharpe_ratio"] = metric_summary["sharpe_ratio"]
        summary["max_drawdown"] = metric_summary["max_drawdown"]
        summary["win_rate"] = metric_summary["win_rate"]

    return {key: value for key, value in summary.items() if value is not None or key in {"id", "name"}}


async def _enrich_rank_strategy(db, strategy: dict, semaphore: asyncio.Semaphore) -> dict:
    async with semaphore:
        metrics_list, incubation_surface = await asyncio.gather(
            db.get_strategy_metrics(strategy["id"]),
            _load_strategy_incubation_surface(db, strategy),
        )

    all_period = next((m for m in metrics_list if m.get("period") == "all"), {})
    return _build_strategy_market_summary(strategy, metrics=all_period, incubation_surface=incubation_surface)


async def _resolve_strategy_incubation_overview(db, strategy: dict) -> dict | None:
    try:
        return await build_incubation_overview(db, strategy)
    except Exception as exc:
        logger.warning("strategy_manager.detail incubation overview failed for %s: %s", strategy.get("id"), exc)
        return None
