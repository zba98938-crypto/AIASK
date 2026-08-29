"""Unified closure-review aggregation for strategy-market detail surfaces."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any, Optional

from .execution_audit_snapshot import with_execution_audit_snapshot_metadata
from .incubation import get_latest_quality_report
from .overview import build_incubation_overview
from .presentation import (
    build_favorite_state,
    build_owner_state,
    build_paper_session_state,
    build_strategy_presentation,
)


def _string(value: Any) -> str:
    return str(value or "").strip()


async def _maybe_call(method, *args, default=None, **kwargs):
    if not callable(method):
        return default
    try:
        return await method(*args, **kwargs)
    except Exception:
        return default


def _latest(items: list[dict[str, Any]]) -> Optional[dict[str, Any]]:
    return items[0] if items else None


def _summarize_factory_run(run: dict[str, Any]) -> dict[str, Any]:
    """Build a lightweight summary of a strategy-factory run.

    F-N42-2 fix (诊断报告 §N42): closure_review/detail/factory_status 内嵌
    最近 5 个**完整** factory run（每个含全 stages，110-119KB），单响应数十万 token。
    复核单个用户草稿不应拖入全市场 factory 调度历史。此助手仅保留 run 的标量摘要
    （run_id/status/时间/计数/summary 顶层标量），剔除 stages 等重字段。
    """
    if not isinstance(run, dict):
        return {}
    summary = run.get("summary")
    summary_scalars: dict[str, Any] = {}
    if isinstance(summary, dict):
        for key, value in summary.items():
            if isinstance(value, (str, int, float, bool)) or value is None:
                summary_scalars[key] = value
    return {
        "run_id": run.get("run_id") or run.get("id"),
        "status": run.get("status"),
        "task": run.get("task") or run.get("run_type"),
        "started_at": run.get("started_at") or run.get("created_at"),
        "completed_at": run.get("completed_at"),
        "submitted_count": run.get("submitted_count"),
        "spawned_count": run.get("spawned_count"),
        "eliminated_count": run.get("eliminated_count"),
        "summary": summary_scalars or None,
    }



def _coerce_as_of(value: Any) -> Optional[str]:
    if value in (None, ""):
        return None
    if isinstance(value, date):
        return value.isoformat()
    text = _string(value)
    return text[:10] if len(text) >= 10 else None


def _fallback_acceptance(snapshot: Optional[dict[str, Any]]) -> Optional[dict[str, Any]]:
    payload = dict(snapshot or {})
    if not payload:
        return None
    acceptance = dict(payload.get("acceptance") or {})
    if acceptance:
        return acceptance
    verdict = dict(payload.get("verdict") or {})
    audit_summary = dict(payload.get("audit_summary") or {})
    return {
        "status": "ready" if bool(verdict.get("hard_gate_passed")) else "needs_attention",
        "strategy_id": payload.get("strategy_id"),
        "trade_audit_summary": audit_summary or None,
        "snapshot": payload,
        "verification": dict(payload.get("verification") or {}),
        "acceptance_matrix": {
            "hard_gate_ready": bool(verdict.get("hard_gate_passed")),
            "bootstrap_gate_ready": _string(verdict.get("status")) in {"bootstrap_ready", "passed"},
            "overall_ready": bool(verdict.get("hard_gate_passed")),
        },
        "blockers": list(audit_summary.get("execution_audit_gate_reasons") or []),
    }


@dataclass(slots=True)
class ClosureReviewDto:
    strategy_id: str
    as_of: Optional[str]
    correlation_id: Optional[str]
    factory_run_id: Optional[str]
    stale: bool
    owner_state: dict[str, Any]
    favorite_state: dict[str, Any]
    paper_session_state: dict[str, Any]
    presentation: dict[str, Any]
    data_freshness: dict[str, Any]
    report: Optional[dict[str, Any]]
    events: dict[str, Any]
    incubation: dict[str, Any]
    runtime: dict[str, Any]
    vectors: dict[str, Any]
    domain: dict[str, Any]
    ai: dict[str, Any]
    factory: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "strategy_id": self.strategy_id,
            "as_of": self.as_of,
            "correlation_id": self.correlation_id,
            "factory_run_id": self.factory_run_id,
            "stale": self.stale,
            "owner_state": dict(self.owner_state or {}),
            "favorite_state": dict(self.favorite_state or {}),
            "paper_session_state": dict(self.paper_session_state or {}),
            "presentation": dict(self.presentation or {}),
            "data_freshness": dict(self.data_freshness or {}),
            "report": self.report,
            "events": dict(self.events or {}),
            "incubation": dict(self.incubation or {}),
            "runtime": dict(self.runtime or {}),
            "vectors": dict(self.vectors or {}),
            "domain": dict(self.domain or {}),
            "ai": dict(self.ai or {}),
            "factory": dict(self.factory or {}),
        }


async def build_closure_review(
    db,
    strategy: dict[str, Any],
    *,
    as_of: Any = None,
    correlation_id: Optional[str] = None,
    actor_id: Optional[str] = None,
    actor_roles: Any = None,
    force_recompute: bool = False,
) -> dict[str, Any]:
    strategy_id = _string(strategy.get("id"))
    if not strategy_id:
        raise ValueError("strategy_id is required")

    latest_quality_report = await get_latest_quality_report(db, strategy_id)
    normalize_quality_report_contract = None
    try:
        from ...tools.managers.strategy_mgr_helpers import (
            normalize_quality_report_contract as _normalize_quality_report_contract,
        )

        normalize_quality_report_contract = _normalize_quality_report_contract
    except Exception:
        normalize_quality_report_contract = None
    report = (
        normalize_quality_report_contract(
            latest_quality_report,
            strategy_id=strategy_id,
            strategy_type=strategy.get("strategy_type"),
            default_review_source="closure_review",
        )
        if latest_quality_report and callable(normalize_quality_report_contract)
        else (dict(latest_quality_report or {}) or None)
    )

    try:
        overview = await build_incubation_overview(
            db,
            strategy,
            force_recompute=force_recompute,
        )
    except TypeError:
        overview = await build_incubation_overview(db, strategy)
    execution_audit_snapshot = dict(overview.get("execution_audit_snapshot") or {})
    if not execution_audit_snapshot and hasattr(db, "get_execution_audit_verification"):
        try:
            await db.get_execution_audit_verification(strategy_id)
            execution_audit_snapshot = dict(
                await _maybe_call(
                    getattr(db, "get_latest_execution_audit_snapshot", None),
                    strategy_id,
                    default=None,
                )
                or {}
            )
            if execution_audit_snapshot:
                overview = with_execution_audit_snapshot_metadata(
                    overview,
                    snapshot=execution_audit_snapshot,
                )
        except Exception:
            execution_audit_snapshot = {}

    status_events_task = _maybe_call(
        getattr(db, "list_strategy_status_events", None),
        strategy_id,
        limit=20,
        default=[],
    )
    incubation_account_task = _maybe_call(
        getattr(db, "get_strategy_incubation_account", None),
        strategy_id,
        default=None,
    )
    latest_metric_task = _maybe_call(
        getattr(db, "get_latest_strategy_incubation_metric", None),
        strategy_id,
        default=None,
    )
    paper_account_task = _maybe_call(
        getattr(db, "get_paper_account_by_strategy", None),
        strategy_id,
        default=None,
    )
    paper_orders_task = _maybe_call(
        getattr(db, "list_strategy_paper_orders", None),
        strategy_id,
        limit=20,
        default=[],
    )
    pipeline_task = _maybe_call(
        getattr(db, "list_strategy_incubation_pipeline_snapshots", None),
        strategy_id=strategy_id,
        limit=10,
        default=[],
    )
    promotion_reviews_task = _maybe_call(
        getattr(db, "list_strategy_promotion_reviews", None),
        strategy_id=strategy_id,
        limit=10,
        default=[],
    )
    runtime_control_task = _maybe_call(
        getattr(db, "get_strategy_runtime_control", None),
        strategy_id,
        default=None,
    )
    risk_events_task = _maybe_call(
        getattr(db, "list_strategy_runtime_risk_events", None),
        strategy_id=strategy_id,
        status="open",
        limit=20,
        default=[],
    )
    risk_snapshots_task = _maybe_call(
        getattr(db, "list_strategy_runtime_risk_snapshots", None),
        strategy_id=strategy_id,
        limit=10,
        default=[],
    )
    runtime_alerts_task = _maybe_call(
        getattr(db, "list_strategy_runtime_alerts", None),
        strategy_id=strategy_id,
        limit=20,
        default=[],
    )
    vector_profiles_task = _maybe_call(
        getattr(db, "list_strategy_vector_profiles", None),
        strategy_id=strategy_id,
        limit=10,
        default=[],
    )
    vector_index_snapshots_task = _maybe_call(
        getattr(db, "list_vector_index_snapshots", None),
        index_name="strategy_behavior",
        limit=10,
        default=[],
    )
    domain_events_task = _maybe_call(
        getattr(db, "list_strategy_domain_events", None),
        strategy_id=strategy_id,
        limit=20,
        default=[],
    )
    projection_snapshots_task = _maybe_call(
        getattr(db, "list_strategy_projection_snapshots", None),
        strategy_id,
        limit=20,
        default=[],
    )
    latest_projection_snapshot_task = _maybe_call(
        getattr(db, "get_latest_strategy_projection_snapshot", None),
        strategy_id,
        default=None,
    )
    experiments_task = _maybe_call(
        getattr(db, "list_strategy_generation_experiments", None),
        strategy_id=strategy_id,
        limit=10,
        default=[],
    )
    task_runs_task = _maybe_call(
        getattr(db, "list_strategy_task_runs", None),
        strategy_id=strategy_id,
        limit=10,
        default=[],
    )
    factory_runs_task = _maybe_call(
        getattr(db, "list_strategy_factory_runs", None),
        limit=5,
        default=[],
    )

    (
        status_events,
        incubation_account,
        latest_metric,
        paper_account,
        paper_orders,
        pipeline_snapshots,
        promotion_reviews,
        runtime_control,
        risk_events,
        risk_snapshots,
        runtime_alerts,
        vector_profiles,
        vector_index_snapshots,
        domain_events,
        projection_snapshots,
        latest_projection_snapshot,
        experiments,
        task_runs,
        factory_runs,
    ) = await asyncio.gather(
        status_events_task,
        incubation_account_task,
        latest_metric_task,
        paper_account_task,
        paper_orders_task,
        pipeline_task,
        promotion_reviews_task,
        runtime_control_task,
        risk_events_task,
        risk_snapshots_task,
        runtime_alerts_task,
        vector_profiles_task,
        vector_index_snapshots_task,
        domain_events_task,
        projection_snapshots_task,
        latest_projection_snapshot_task,
        experiments_task,
        task_runs_task,
        factory_runs_task,
    )

    similar_profiles: list[dict[str, Any]] = []
    try:
        from ..vector_platform import get_strategy_vector_platform

        similar_profiles = await get_strategy_vector_platform().find_similar_profiles(
            db,
            strategy_id,
            limit=10,
        )
    except Exception:
        similar_profiles = []

    paper_positions = []
    paper_nav_rows = []
    paper_order_summary = None
    if paper_account:
        paper_positions, paper_nav_rows, paper_order_summary = await asyncio.gather(
            _maybe_call(getattr(db, "list_paper_positions", None), paper_account.get("id"), default=[]),
            _maybe_call(getattr(db, "get_paper_nav_rows", None), paper_account.get("id"), limit=20, default=[]),
            _maybe_call(getattr(db, "get_paper_order_summary", None), paper_account.get("id"), default=None),
        )

    domain_projection = None
    try:
        from ..domain_projection import get_strategy_domain_projection_service

        domain_projection = await get_strategy_domain_projection_service().project_strategy(
            db,
            strategy_id,
            limit=100,
        )
    except Exception:
        domain_projection = (
            dict((latest_projection_snapshot or {}).get("projection") or {})
            if latest_projection_snapshot
            else None
        )

    execution_audit_acceptance = _fallback_acceptance(execution_audit_snapshot)
    execution_audit_acceptance = with_execution_audit_snapshot_metadata(
        execution_audit_acceptance,
        snapshot=execution_audit_snapshot,
    ) if execution_audit_acceptance else None
    is_favorited = False
    if actor_id and hasattr(db, "is_subscribed"):
        try:
            is_favorited = bool(await db.is_subscribed(strategy_id, actor_id))
        except Exception:
            is_favorited = False
    paper_session = None
    if actor_id and hasattr(db, "get_strategy_paper_session"):
        try:
            paper_session = await db.get_strategy_paper_session(strategy_id, actor_id)
        except Exception:
            paper_session = None
    owner_state = build_owner_state(strategy, actor_id=actor_id, actor_roles=actor_roles)
    favorite_state = build_favorite_state(actor_id=actor_id, is_favorited=is_favorited)
    paper_session_state = build_paper_session_state(paper_session, actor_id=actor_id)
    presentation = build_strategy_presentation(
        strategy,
        owner_state=owner_state,
        favorite_state=favorite_state,
        paper_session_state=paper_session_state,
        overview=overview,
        report=report,
        runtime_control=runtime_control,
        risk_events=list(risk_events or []),
        execution_audit_acceptance=execution_audit_acceptance,
    )

    latest_factory_run = _latest(list(factory_runs or []))
    latest_factory_run_summary = dict((latest_factory_run or {}).get("summary") or {})
    latest_pipeline_snapshot = _latest(list(pipeline_snapshots or []))
    latest_promotion_review = _latest(list(promotion_reviews or []))
    latest_risk_snapshot = _latest(list(risk_snapshots or []))
    latest_vector_index_snapshot = _latest(list(vector_index_snapshots or []))
    try:
        from strategy_factory.api.market_views import (
            build_research_window_status,
            hydrate_full_market_topn_payload,
        )

        research_window = dict(
            (latest_factory_run or {}).get("research_window")
            or latest_factory_run_summary.get("research_window")
            or build_research_window_status(latest_factory_run_summary)
        )
    except Exception:
        research_window = dict(
            (latest_factory_run or {}).get("research_window")
            or latest_factory_run_summary.get("research_window")
            or {}
        )
        hydrate_full_market_topn_payload = lambda payload: dict(payload or {})
    latest_topn_snapshot = {}
    if hasattr(db, "get_strategy_factory_topn_snapshot"):
        latest_topn_snapshot = dict(
            await _maybe_call(
                getattr(db, "get_strategy_factory_topn_snapshot", None),
                _string((latest_factory_run or {}).get("run_id")),
                default=None,
            )
            or {}
        )
    if not latest_topn_snapshot and hasattr(db, "get_latest_strategy_factory_topn_snapshot"):
        latest_topn_snapshot = dict(
            await _maybe_call(
                getattr(db, "get_latest_strategy_factory_topn_snapshot", None),
                default=None,
            )
            or {}
        )
    if not latest_topn_snapshot:
        latest_topn_snapshot = dict(
            (latest_factory_run or {}).get("full_market_topn")
            or latest_factory_run_summary.get("full_market_topn")
            or {}
        )
    latest_topn_snapshot = hydrate_full_market_topn_payload(latest_topn_snapshot)
    if not latest_projection_snapshot:
        latest_projection_snapshot = _latest(list(projection_snapshots or []))

    resolved_correlation_id = (
        _string(correlation_id)
        or _string((execution_audit_snapshot or {}).get("correlation_id"))
        or _string((runtime_control or {}).get("metadata", {}).get("correlation_id") if isinstance(runtime_control, dict) else None)
        or None
    )
    # F-N42-2 fix (诊断报告 §N42): 仅当策略与某 factory run 有**真实关联**时才归属
    # factory_run_id。历史问题: 手工 create 的 draft（与任何 run 无关）被赋全局
    # latest_factory_run.run_id，AI 据此误判该 draft 是工厂产出。
    # 真实关联来源: (a) execution_audit_snapshot.factory_run_id；
    # (b) 策略自身 metadata/params 记录的 factory_run_id/source_factory_run_id。
    _strategy_meta: dict[str, Any] = {}
    if isinstance(strategy, dict):
        _strategy_params_meta = dict(dict(strategy.get("params") or {}).get("metadata") or {})
        _strategy_meta = {**_strategy_params_meta, **dict(strategy.get("metadata") or {})}
    _strategy_linked_run = (
        _string((execution_audit_snapshot or {}).get("factory_run_id"))
        or _string(_strategy_meta.get("factory_run_id"))
        or _string(_strategy_meta.get("source_factory_run_id"))
        or None
    )
    resolved_factory_run_id = _strategy_linked_run or None
    factory_run_lineage_basis = (
        "execution_audit_snapshot" if _string((execution_audit_snapshot or {}).get("factory_run_id"))
        else "strategy_metadata" if _strategy_linked_run
        else "unlinked"
    )
    resolved_as_of = (
        _coerce_as_of(as_of)
        or _coerce_as_of((execution_audit_snapshot or {}).get("as_of"))
        or _coerce_as_of(dict((latest_pipeline_snapshot or {}).get("metadata") or {}).get("overview", {}).get("execution_audit_snapshot", {}).get("as_of"))
        or datetime.now(timezone.utc).date().isoformat()
    )
    stale = bool(
        resolved_as_of
        and resolved_as_of != datetime.now(timezone.utc).date().isoformat()
    )

    dto = ClosureReviewDto(
        strategy_id=strategy_id,
        as_of=resolved_as_of,
        correlation_id=resolved_correlation_id,
        factory_run_id=resolved_factory_run_id,
        stale=stale,
        owner_state=owner_state,
        favorite_state=favorite_state,
        paper_session_state=paper_session_state,
        presentation=presentation,
        data_freshness={
            "execution_audit_as_of": _coerce_as_of((execution_audit_snapshot or {}).get("as_of")),
            "runtime_cycle_seen_today": bool(overview.get("runtime_cycle_seen_today")),
            "latest_signal_snapshot_date": _coerce_as_of(
                dict(overview.get("latest_signal_snapshot") or {}).get("as_of_date")
            ),
            "latest_factory_run_completed_at": (latest_factory_run or {}).get("completed_at"),
            "overview_recomputed": bool(overview.get("recomputed")),
            "overview_closure_snapshot_id": overview.get("closure_snapshot_id"),
        },
        report=report,
        events={
            "events": list(status_events or []),
            "count": len(list(status_events or [])),
        },
        incubation={
            "overview": overview,
            "current_account": incubation_account,
            "latest_metric": latest_metric,
            "paper_account": {
                "account": paper_account,
                "binding": incubation_account,
                "positions": list(paper_positions or []),
                "latest_nav": _latest(list(paper_nav_rows or [])),
                "order_summary": paper_order_summary,
            } if paper_account else None,
            "paper_orders": list(paper_orders or []),
            "paper_nav_rows": list(paper_nav_rows or []),
            "pipeline": {
                "latest": latest_pipeline_snapshot,
                "items": list(pipeline_snapshots or []),
                "count": len(list(pipeline_snapshots or [])),
            },
            "promotion_reviews": {
                "latest": latest_promotion_review,
                "items": list(promotion_reviews or []),
                "count": len(list(promotion_reviews or [])),
            },
            "execution_audit_acceptance": execution_audit_acceptance,
        },
        runtime={
            "control": runtime_control,
            "risk_events": list(risk_events or []),
            "risk_snapshots": {
                "latest": latest_risk_snapshot,
                "items": list(risk_snapshots or []),
                "count": len(list(risk_snapshots or [])),
            },
            "alerts": list(runtime_alerts or []),
        },
        vectors={
            "profiles": list(vector_profiles or []),
            "similar_profiles": list(similar_profiles or []),
            "index_snapshots": {
                "latest": latest_vector_index_snapshot,
                "items": list(vector_index_snapshots or []),
                "count": len(list(vector_index_snapshots or [])),
            },
        },
        domain={
            "projection": domain_projection,
            "latest_projection_snapshot": latest_projection_snapshot,
            "projection_snapshots": list(projection_snapshots or []),
            "events": list(domain_events or []),
        },
        ai={
            "experiments": list(experiments or []),
            "task_runs": list(task_runs or []),
        },
        factory={
            "research_window": research_window,
            "full_market_topn": latest_topn_snapshot,
            "latest_run": _summarize_factory_run(latest_factory_run or {}) if latest_factory_run else None,
            "runs": [_summarize_factory_run(run) for run in list(factory_runs or [])],
            "runs_truncated": True,
            "runs_note": (
                "仅返回 factory run 标量摘要（run_id/status/计数）；"
                "完整 stages 请用 factory_run_detail(run_id) 单独获取"
            ),
            "factory_run_lineage_basis": factory_run_lineage_basis,
        },
    )
    return dto.to_dict()
