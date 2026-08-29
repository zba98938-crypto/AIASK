"""Submit-time lifecycle coordination for strategy factory lanes."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import logging
from typing import Any, Optional
from uuid import uuid4

from .._runtime_toggles import diagnostic_observation_final_status
from ..utils import _update_strategy_status as _local_update_strategy_status
from ...infrastructure.mcp_services import build_strategy_vector_profile

logger = logging.getLogger(__name__)


def _string(value: Any) -> str:
    return str(value or "").strip()


def _as_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return _string(value).lower() in {"1", "true", "yes", "y"}


def _compact_mapping(payload: Optional[dict[str, Any]], keys: tuple[str, ...]) -> dict[str, Any]:
    source = dict(payload or {})
    return {
        key: source.get(key)
        for key in keys
        if source.get(key) not in (None, "", [], {})
    }


async def _update_strategy_status(*args, **kwargs):
    try:
        from .._submitter_actions import runner as submitter_runner

        patched = getattr(submitter_runner, "_update_strategy_status", None)
        if callable(patched) and patched is not _update_strategy_status:
            return await patched(*args, **kwargs)
    except Exception:
        pass
    return await _local_update_strategy_status(*args, **kwargs)


@dataclass(slots=True)
class LifecycleTransitionRequest:
    strategy_id: str
    name: str
    candidate: dict[str, Any]
    data: dict[str, Any]
    gate: dict[str, Any]
    quality_report: dict[str, Any]
    snapshot: dict[str, Any]
    submission_lane: str
    submission_action: dict[str, Any] = field(default_factory=dict)
    backtest_metrics: Optional[dict[str, Any]] = None
    validation_report: Optional[dict[str, Any]] = None
    risk_report: Optional[dict[str, Any]] = None
    read_only: bool = False
    factory_run_id: Optional[str] = None
    trace_id: Optional[str] = None
    correlation_id: Optional[str] = None
    parent_task_run_id: Optional[str] = None
    source_action: str = "strategy_factory_submit"
    execution_audit_snapshot_id: Optional[str] = None
    snapshot_date: Optional[str] = None
    quality_gate_summary: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class LifecycleTransitionStepResult:
    step: str
    status: str
    detail: dict[str, Any] = field(default_factory=dict)
    error: Optional[str] = None
    retryable: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "step": _string(self.step),
            "status": _string(self.status) or "unknown",
            "detail": dict(self.detail or {}),
            "error": _string(self.error) or None,
            "retryable": bool(self.retryable),
        }


@dataclass(slots=True)
class LifecycleTransitionResult:
    final_status: str
    submission_lane: str
    action_audit: dict[str, Any] = field(default_factory=dict)
    steps: list[LifecycleTransitionStepResult] = field(default_factory=list)
    lifecycle_task_run: Optional[dict[str, Any]] = None
    lifecycle_event: Optional[dict[str, Any]] = None
    execution_audit_snapshot: Optional[dict[str, Any]] = None
    incubation_binding: Optional[dict[str, Any]] = None
    incubation_pipeline: Optional[dict[str, Any]] = None
    vector_profile: Optional[dict[str, Any]] = None
    vector_audit: dict[str, Any] = field(default_factory=dict)
    paper_action: dict[str, Any] = field(default_factory=dict)
    diagnostic_action: dict[str, Any] = field(default_factory=dict)
    live_review_action: dict[str, Any] = field(default_factory=dict)
    action_refs: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "final_status": _string(self.final_status) or "submitted",
            "submission_lane": _string(self.submission_lane) or "deferred_submission",
            "action_audit": dict(self.action_audit or {}),
            "steps": [item.to_dict() for item in self.steps],
            "lifecycle_task_run": self.lifecycle_task_run,
            "lifecycle_event": self.lifecycle_event,
            "execution_audit_snapshot": self.execution_audit_snapshot,
            "incubation_binding": self.incubation_binding,
            "incubation_pipeline": self.incubation_pipeline,
            "vector_profile": self.vector_profile,
            "vector_audit": dict(self.vector_audit or {}),
            "paper_action": dict(self.paper_action or {}),
            "diagnostic_action": dict(self.diagnostic_action or {}),
            "live_review_action": dict(self.live_review_action or {}),
            "action_refs": dict(self.action_refs or {}),
        }
        if self.execution_audit_snapshot:
            payload["execution_audit_snapshot_id"] = self.execution_audit_snapshot.get("snapshot_id")
            payload["correlation_id"] = self.execution_audit_snapshot.get("correlation_id")
            payload["trace_id"] = self.execution_audit_snapshot.get("trace_id")
            payload["factory_run_id"] = self.execution_audit_snapshot.get("factory_run_id")
            payload["parent_task_run_id"] = self.execution_audit_snapshot.get("parent_task_run_id")
        return payload

    def to_task_run_result(self) -> dict[str, Any]:
        payload = {
            "final_status": _string(self.final_status) or "submitted",
            "submission_lane": _string(self.submission_lane) or "deferred_submission",
            "action_audit": dict(self.action_audit or {}),
            "action_refs": dict(self.action_refs or {}),
            "steps": [item.to_dict() for item in self.steps],
            "step_count": len(self.steps),
            "failed_step_count": sum(1 for item in self.steps if item.status == "failed"),
            "successful_step_count": sum(1 for item in self.steps if item.status == "success"),
            "vector_audit": _compact_mapping(
                dict(self.vector_audit or {}),
                ("backend_requested", "backend_used", "fallback_used", "fallback_reason", "latency_ms", "score"),
            ),
        }
        if self.execution_audit_snapshot:
            payload["execution_audit_snapshot"] = _compact_mapping(
                self.execution_audit_snapshot,
                (
                    "snapshot_id",
                    "strategy_id",
                    "as_of",
                    "factory_run_id",
                    "correlation_id",
                    "trace_id",
                    "submission_lane",
                    "verdict_status",
                    "execution_hard_gate_passed",
                ),
            )
            payload["execution_audit_snapshot_id"] = self.execution_audit_snapshot.get("snapshot_id")
            payload["correlation_id"] = self.execution_audit_snapshot.get("correlation_id")
            payload["trace_id"] = self.execution_audit_snapshot.get("trace_id")
            payload["factory_run_id"] = self.execution_audit_snapshot.get("factory_run_id")
            payload["parent_task_run_id"] = self.execution_audit_snapshot.get("parent_task_run_id")
        if self.lifecycle_task_run:
            payload["lifecycle_task_run"] = _compact_mapping(
                self.lifecycle_task_run,
                ("id", "strategy_id", "task_name", "task_scope", "task_key", "status", "trace_id", "started_at", "completed_at"),
            )
        if self.lifecycle_event:
            payload["lifecycle_event"] = _compact_mapping(
                self.lifecycle_event,
                ("id", "aggregate_type", "aggregate_id", "event_type", "source", "severity", "correlation_id", "created_at"),
            )
        if self.incubation_binding:
            account = dict((self.incubation_binding or {}).get("account") or {})
            binding = dict((self.incubation_binding or {}).get("binding") or {})
            incubation_binding = _compact_mapping(account, ("id", "status", "stage"))
            if binding.get("account_id") not in (None, "", [], {}):
                incubation_binding["binding_account_id"] = binding.get("account_id")
            if incubation_binding:
                payload["incubation_binding"] = incubation_binding
        if self.incubation_pipeline:
            snapshot = dict((self.incubation_pipeline or {}).get("snapshot") or {})
            incubation_pipeline = {
                **_compact_mapping(self.incubation_pipeline, ("task_run_id", "status")),
                **_compact_mapping(snapshot, ("id", "pipeline_stage", "pipeline_status", "readiness_score", "closure_snapshot_id")),
            }
            if incubation_pipeline:
                payload["incubation_pipeline"] = incubation_pipeline
        if self.vector_profile:
            payload["vector_profile"] = _compact_mapping(
                self.vector_profile,
                ("id", "strategy_id", "backend", "collection_name", "profile_kind", "version"),
            )
        if self.paper_action:
            payload["paper_action"] = _compact_mapping(
                self.paper_action,
                ("paper_account_id", "paper_lane_ready", "paper_account_status"),
            )
        if self.diagnostic_action:
            payload["diagnostic_action"] = _compact_mapping(
                self.diagnostic_action,
                (
                    "diagnostic_account_id",
                    "diagnostic_lane_ready",
                    "diagnostic_account_status",
                    "diagnostic_fingerprint",
                    "diagnostic_guard",
                    "diagnostic_reason",
                    "diagnostic_ttl_days",
                    "admission_layer",
                ),
            )
        if self.live_review_action:
            payload["live_review_action"] = _compact_mapping(
                self.live_review_action,
                (
                    "promotion_review_id",
                    "final_status",
                    "submission_action_completed",
                    "live_review_ready",
                    "live_review_account_id",
                    "runtime_control_mode",
                    "runtime_control_status",
                    "promotion_review_status",
                    "promotion_review_recommendation",
                    "promotion_review_score",
                ),
            )
        return payload


class StrategyLifecycleCoordinator:
    def __init__(self, submitter: Any) -> None:
        self._submitter = submitter

    def _build_trace_context(self, request: LifecycleTransitionRequest) -> dict[str, Any]:
        candidate = dict(request.candidate or {})
        params = dict(candidate.get("params") or {})
        trace_id = (
            _string(request.trace_id)
            or _string(candidate.get("trace_id"))
            or _string(params.get("prediction_trace_id"))
            or uuid4().hex[:12]
        )
        correlation_id = (
            _string(request.correlation_id)
            or _string(candidate.get("correlation_id"))
            or trace_id
        )
        return {
            "factory_run_id": _string(request.factory_run_id or candidate.get("factory_run_id")) or None,
            "trace_id": trace_id,
            "correlation_id": correlation_id,
            "strategy_id": _string(request.strategy_id),
            "submission_lane": _string(request.submission_lane) or "deferred_submission",
            "parent_task_run_id": _string(
                request.parent_task_run_id
                or candidate.get("task_run_id")
                or params.get("task_run_id")
            ) or None,
            "source_action": _string(request.source_action) or "strategy_factory_submit",
            "snapshot_date": _string(request.snapshot_date or request.snapshot.get("date")) or None,
        }

    async def _start_task_run(
        self,
        db: Any,
        request: LifecycleTransitionRequest,
        trace_context: dict[str, Any],
    ) -> Optional[dict[str, Any]]:
        save_task_run = getattr(db, "save_strategy_task_run", None)
        if not callable(save_task_run):
            return None
        try:
            return await save_task_run(
                {
                    "strategy_id": request.strategy_id,
                    "task_name": "strategy_lifecycle_transition",
                    "task_scope": trace_context.get("source_action"),
                    "task_key": f"{request.strategy_id}:{trace_context.get('submission_lane')}",
                    "status": "running",
                    "trace_id": trace_context.get("trace_id"),
                    "payload": {
                        **trace_context,
                        "quality_gate_summary": dict(request.quality_gate_summary or {}),
                        "execution_audit_snapshot_id": request.execution_audit_snapshot_id,
                    },
                }
            )
        except Exception as exc:
            logger.warning("StrategyLifecycleCoordinator: create task run failed for %s: %s", request.strategy_id, exc)
            return None

    async def _finish_task_run(
        self,
        db: Any,
        task_run: Optional[dict[str, Any]],
        *,
        status: str,
        result: dict[str, Any],
        error: Optional[str] = None,
    ) -> Optional[dict[str, Any]]:
        if not task_run or not task_run.get("id"):
            return task_run
        update_task_run = getattr(db, "update_strategy_task_run", None)
        if not callable(update_task_run):
            return task_run
        try:
            return await update_task_run(
                int(task_run.get("id")),
                status=status,
                result=result,
                error=error,
                completed_at=datetime.now(timezone.utc),
            )
        except Exception as exc:
            logger.warning("StrategyLifecycleCoordinator: update task run failed for %s: %s", task_run.get("id"), exc)
            return task_run

    async def _record_domain_event(
        self,
        db: Any,
        *,
        request: LifecycleTransitionRequest,
        trace_context: dict[str, Any],
        event_type: str,
        payload: dict[str, Any],
        severity: str = "info",
    ) -> Optional[dict[str, Any]]:
        save_domain_event = getattr(db, "save_strategy_domain_event", None)
        if not callable(save_domain_event):
            return None
        try:
            return await save_domain_event(
                {
                    "strategy_id": request.strategy_id,
                    "aggregate_type": "strategy_lifecycle_transition",
                    "aggregate_id": request.strategy_id,
                    "event_type": event_type,
                    "source": trace_context.get("source_action"),
                    "severity": severity,
                    "correlation_id": trace_context.get("correlation_id"),
                    "payload": {
                        **trace_context,
                        **dict(payload or {}),
                    },
                }
            )
        except Exception as exc:
            logger.warning("StrategyLifecycleCoordinator: save domain event failed for %s: %s", request.strategy_id, exc)
            return None

    async def _persist_execution_snapshot(
        self,
        db: Any,
        request: LifecycleTransitionRequest,
        trace_context: dict[str, Any],
    ) -> Optional[dict[str, Any]]:
        upsert = getattr(db, "upsert_execution_audit_snapshot", None)
        if not callable(upsert):
            return None
        try:
            from ...infrastructure.mcp_services import get_execution_audit_snapshot_builder

            build_execution_audit_snapshot_payload = get_execution_audit_snapshot_builder()

            quality_summary = dict((request.quality_report or {}).get("summary") or {})
            snapshot_row = await upsert(
                build_execution_audit_snapshot_payload(
                    strategy_id=request.strategy_id,
                    quality_gate=request.gate,
                    audit_summary=dict(request.gate.get("audit") or {}),
                    snapshot=dict(request.snapshot or {}),
                    verdict_status=(
                        request.gate.get("execution_audit_gate_status")
                        or quality_summary.get("execution_audit_gate_status")
                    ),
                    verdict_reasons=list(
                        request.gate.get("execution_audit_gate_reasons")
                        or quality_summary.get("execution_audit_gate_reasons")
                        or []
                    ),
                    execution_hard_gate_passed=(
                        request.gate.get("execution_hard_gate_passed")
                        if request.gate.get("execution_hard_gate_passed") is not None
                        else quality_summary.get("execution_hard_gate_passed")
                    ),
                    as_of=trace_context.get("snapshot_date"),
                    source_run_id=trace_context.get("snapshot_date"),
                    factory_run_id=trace_context.get("factory_run_id"),
                    correlation_id=trace_context.get("correlation_id"),
                    trace_id=trace_context.get("trace_id"),
                    submission_lane=trace_context.get("submission_lane"),
                    parent_task_run_id=trace_context.get("parent_task_run_id"),
                    source_action=trace_context.get("source_action"),
                    metadata={
                        "strategy_name": request.name,
                        "quality_gate_summary": dict(request.quality_gate_summary or {}),
                    },
                )
            )
            if snapshot_row:
                request.execution_audit_snapshot_id = snapshot_row.get("snapshot_id")
            return snapshot_row
        except Exception as exc:
            logger.warning("StrategyLifecycleCoordinator: persist execution snapshot failed for %s: %s", request.strategy_id, exc)
            return None

    async def _step(
        self,
        step_name: str,
        coro,
        *,
        retryable: bool = False,
        normalizer=None,
    ) -> tuple[LifecycleTransitionStepResult, Any]:
        try:
            payload = await coro
            detail = normalizer(payload) if callable(normalizer) else dict(payload or {}) if isinstance(payload, dict) else {"value": payload}
            return LifecycleTransitionStepResult(step=step_name, status="success", detail=detail, retryable=retryable), payload
        except Exception as exc:
            logger.warning("StrategyLifecycleCoordinator: %s failed: %s", step_name, exc)
            return LifecycleTransitionStepResult(step=step_name, status="failed", error=str(exc), retryable=retryable), None

    async def _refresh_closure_review(
        self,
        db: Any,
        *,
        request: LifecycleTransitionRequest,
        trace_context: dict[str, Any],
        final_status: str,
    ) -> Optional[dict[str, Any]]:
        try:
            from ...infrastructure.mcp_services import get_closure_review_builder

            build_closure_review = get_closure_review_builder()

            strategy_view = {
                **dict(request.data or {}),
                "id": request.strategy_id,
                "name": request.name,
                "status": final_status or dict(request.data or {}).get("status"),
            }
            return await build_closure_review(
                db,
                strategy_view,
                as_of=trace_context.get("snapshot_date"),
                correlation_id=trace_context.get("correlation_id"),
                force_recompute=True,
            )
        except Exception as exc:
            logger.warning(
                "StrategyLifecycleCoordinator: closure review refresh failed for %s: %s",
                request.strategy_id,
                exc,
            )
            return None

    async def execute(
        self,
        db: Any,
        request: LifecycleTransitionRequest,
    ) -> LifecycleTransitionResult:
        trace_context = self._build_trace_context(request)
        result = LifecycleTransitionResult(
            final_status=_string(request.submission_action.get("final_status")) or "submitted",
            submission_lane=_string(request.submission_lane) or "deferred_submission",
        )
        lifecycle_task_run = await self._start_task_run(db, request, trace_context)
        result.lifecycle_task_run = lifecycle_task_run
        execution_snapshot = await self._persist_execution_snapshot(db, request, trace_context)
        result.execution_audit_snapshot = execution_snapshot
        if execution_snapshot:
            trace_context["execution_audit_snapshot_id"] = execution_snapshot.get("snapshot_id")
        enriched_data = {
            **dict(request.data or {}),
            "id": request.strategy_id,
            "name": request.name,
            "_closure_trace": trace_context,
        }
        incubation_budget = dict(request.candidate.get("incubation_budget") or {})
        candidate_params = dict(request.candidate.get("params") or {})

        def _public_runtime_diagnostic_only() -> bool:
            for source in (
                dict(request.candidate or {}),
                candidate_params,
                dict(request.gate or {}),
            ):
                if "diagnostic_only" not in source:
                    continue
                value = source.get("diagnostic_only")
                if value is None:
                    continue
                return bool(value)
            return False

        async def _status_update(status: str, reason: str) -> None:
            await _update_strategy_status(
                db,
                request.strategy_id,
                status,
                actor_id="strategy_factory",
                reason=reason,
                metadata={
                    "quality_gate": request.gate,
                    "validation_grade": dict(request.quality_report.get("summary") or {}).get("validation_grade"),
                    "incubation_budget": incubation_budget,
                    **trace_context,
                },
            )

        gate_passed = _as_bool(request.gate.get("passed"))
        any_step_failed = False
        if not gate_passed and result.submission_lane == "diagnostic_observation":
            result.final_status = (
                _string(request.submission_action.get("final_status"))
                or diagnostic_observation_final_status()
            )
            diagnostic_reason = (
                _string(request.submission_action.get("diagnostic_reason"))
                or _string(request.submission_action.get("diagnostic_reason_code"))
                or "diagnostic_observation_gate3_failed"
            )
            diagnostic_fingerprint = _string(request.submission_action.get("diagnostic_fingerprint"))
            diagnostic_guard = dict(request.submission_action.get("diagnostic_guard") or {})
            trace_context.update(
                {
                    "admission_layer": "diagnostic",
                    "diagnostic_observation": True,
                    "diagnostic_reason": diagnostic_reason,
                    "diagnostic_reason_code": (
                        _string(request.submission_action.get("diagnostic_reason_code"))
                        or diagnostic_reason
                    ),
                    "diagnostic_fingerprint": diagnostic_fingerprint or None,
                    "diagnostic_guard": diagnostic_guard,
                    "diagnostic_ttl_days": request.submission_action.get("diagnostic_ttl_days"),
                    "source_lane": "diagnostic_observation",
                }
            )
            enriched_data["_closure_trace"] = trace_context
            step, _ = await self._step(
                "status_transition",
                _status_update(result.final_status, "diagnostic_observation_gate3_failed"),
            )
            any_step_failed = step.status != "success"
            result.steps.append(step)
            step, result.diagnostic_action = await self._step(
                "enqueue_diagnostic_observation",
                self._submitter._enqueue_diagnostic_observation(
                    db,
                    {
                        **enriched_data,
                        "status": result.final_status,
                        "admission_layer": "diagnostic",
                        "diagnostic_observation": True,
                        "diagnostic_reason": diagnostic_reason,
                        "diagnostic_reason_code": trace_context.get("diagnostic_reason_code"),
                        "diagnostic_fingerprint": trace_context.get("diagnostic_fingerprint"),
                        "diagnostic_guard": trace_context.get("diagnostic_guard"),
                        "diagnostic_ttl_days": trace_context.get("diagnostic_ttl_days"),
                    },
                    request.snapshot,
                ),
                retryable=True,
            )
            any_step_failed = any_step_failed or step.status != "success"
            result.steps.append(step)
            result.action_audit = {
                **dict(request.submission_action or {}),
                **dict(result.diagnostic_action or {}),
                "final_status": result.final_status,
                "submission_action_completed": bool(
                    (result.diagnostic_action or {}).get("diagnostic_lane_ready")
                ),
                "admission_layer": "diagnostic",
                "diagnostic_observation": True,
                "diagnostic_fingerprint": diagnostic_fingerprint or (result.diagnostic_action or {}).get("diagnostic_fingerprint"),
                "diagnostic_guard": diagnostic_guard or (result.diagnostic_action or {}).get("diagnostic_guard"),
                "diagnostic_reason": diagnostic_reason,
                "diagnostic_ttl_days": trace_context.get("diagnostic_ttl_days"),
                "execution_audit_snapshot_id": (execution_snapshot or {}).get("snapshot_id"),
                "correlation_id": trace_context.get("correlation_id"),
                "factory_run_id": trace_context.get("factory_run_id"),
                "trace_id": trace_context.get("trace_id"),
                "parent_task_run_id": trace_context.get("parent_task_run_id"),
                "source_action": trace_context.get("source_action"),
            }
        elif not gate_passed and result.submission_lane == "observe_incubation":
            # === INVERT-DESIGN P1 改动A: Layer 1 宽进准入 ===
            # Gate-3 未通过但结构合法的候选,进零资本 paper observation(不 rejected),
            # 由 ForwardVerifier 用向前真实数据(带 regime 标签)持续测量。
            result.final_status = "submitted"
            step, _ = await self._step(
                "status_transition",
                _status_update("submitted", "wide_intake_observe_queue"),
            )
            any_step_failed = step.status != "success"
            result.steps.append(step)
            step, result.paper_action = await self._step(
                "enqueue_paper_observation",
                self._submitter._enqueue_paper_observation(
                    db,
                    {**enriched_data, "status": "submitted"},
                    request.snapshot,
                ),
                retryable=True,
            )
            any_step_failed = any_step_failed or step.status != "success"
            result.steps.append(step)
            result.action_audit = {
                **dict(request.submission_action or {}),
                **dict(result.paper_action or {}),
                "final_status": result.final_status,
                "admission_layer": "observe",
                "diagnostic_only": _public_runtime_diagnostic_only(),
                "wide_intake_observe": True,
                "submission_action_completed": bool(
                    result.paper_action or request.submission_action.get("submission_action_completed")
                ),
                "execution_audit_snapshot_id": (execution_snapshot or {}).get("snapshot_id"),
                "correlation_id": trace_context.get("correlation_id"),
                "factory_run_id": trace_context.get("factory_run_id"),
                "trace_id": trace_context.get("trace_id"),
                "parent_task_run_id": trace_context.get("parent_task_run_id"),
                "source_action": trace_context.get("source_action"),
            }
        elif not gate_passed:
            result.final_status = _string(request.submission_action.get("final_status")) or "rejected"
            transition_reason = _string(request.submission_action.get("submission_action_trigger")) or "quality_gate_failed"
            step, _ = await self._step(
                "status_transition",
                _status_update(result.final_status, transition_reason),
            )
            any_step_failed = step.status != "success"
            result.steps.append(step)
            result.action_audit = {
                **dict(request.submission_action or {}),
                "final_status": result.final_status,
                "execution_audit_snapshot_id": (execution_snapshot or {}).get("snapshot_id"),
                "correlation_id": trace_context.get("correlation_id"),
                "factory_run_id": trace_context.get("factory_run_id"),
                "trace_id": trace_context.get("trace_id"),
                "parent_task_run_id": trace_context.get("parent_task_run_id"),
                "source_action": trace_context.get("source_action"),
            }
        else:
            lane = result.submission_lane
            queue_reason = {
                "live_ready_review": "quality_gate_live_ready",
                "observe_incubation": "paper_observation_queue",
                "formal_incubation": "quality_gate_provisional_passed" if request.gate.get("provisional_pass") else "quality_gate_passed",
            }.get(lane, "incubation_budget_deferred_queue")
            if lane == "formal_incubation":
                result.final_status = "incubating"
                step, _ = await self._step("status_transition", _status_update("incubating", queue_reason))
                any_step_failed = any_step_failed or step.status != "success"
                result.steps.append(step)
                incubation_gateway = self._submitter._get_incubation_gateway()
                step, result.incubation_binding = await self._step(
                    "ensure_incubation_account",
                    incubation_gateway.ensure_account(
                        db,
                        enriched_data,
                        source_run_id=trace_context.get("snapshot_date"),
                    ),
                )
                any_step_failed = any_step_failed or step.status != "success"
                result.steps.append(step)
                step, result.incubation_pipeline = await self._step(
                    "run_incubation_pipeline",
                    incubation_gateway.run_pipeline(
                        db,
                        {**enriched_data, "status": "incubating"},
                        source=trace_context.get("source_action"),
                        auto_apply_review=False,
                    ),
                    retryable=True,
                )
                any_step_failed = any_step_failed or step.status != "success"
                result.steps.append(step)
                step, result.vector_profile = await self._step(
                    "build_vector_profile",
                    build_strategy_vector_profile(db, enriched_data),
                    retryable=True,
                )
                any_step_failed = any_step_failed or step.status != "success"
                result.steps.append(step)
                result.vector_audit = dict((result.vector_profile or {}).get("metadata") or {}).get("audit") or {}
                result.action_audit = {
                    **dict(request.submission_action or {}),
                    "paper_account_id": ((result.incubation_binding or {}).get("account") or {}).get("id"),
                    "submission_action_completed": True,
                }
            elif lane == "live_ready_review":
                result.final_status = "submitted"
                step, _ = await self._step("status_transition", _status_update("submitted", queue_reason))
                any_step_failed = any_step_failed or step.status != "success"
                result.steps.append(step)
                step, result.live_review_action = await self._step(
                    "live_ready_review",
                    self._submitter._enqueue_live_ready_review(
                        db,
                        {**enriched_data, "status": "submitted"},
                        request.snapshot,
                        request.gate,
                        trace_context=trace_context,
                    ),
                    retryable=True,
                )
                any_step_failed = any_step_failed or step.status != "success"
                result.steps.append(step)
                result.final_status = _string((result.live_review_action or {}).get("final_status")) or "submitted"
                result.action_audit = {
                    **dict(request.submission_action or {}),
                    **dict(result.live_review_action or {}),
                    "submission_action_completed": bool(
                        result.live_review_action or request.submission_action.get("submission_action_completed")
                    ),
                }
            elif lane == "observe_incubation":
                result.final_status = "submitted"
                step, _ = await self._step("status_transition", _status_update("submitted", queue_reason))
                any_step_failed = any_step_failed or step.status != "success"
                result.steps.append(step)
                step, result.paper_action = await self._step(
                    "enqueue_paper_observation",
                    self._submitter._enqueue_paper_observation(
                        db,
                        {**enriched_data, "status": "submitted"},
                        request.snapshot,
                    ),
                    retryable=True,
                )
                any_step_failed = any_step_failed or step.status != "success"
                result.steps.append(step)
                result.action_audit = {
                    **dict(request.submission_action or {}),
                    **dict(result.paper_action or {}),
                    "diagnostic_only": _public_runtime_diagnostic_only(),
                    "submission_action_completed": bool(
                        result.paper_action or request.submission_action.get("submission_action_completed")
                    ),
                }
            elif lane == "refresh_existing":
                result.final_status = _string(request.submission_action.get("final_status")) or "submitted"
                result.steps.append(
                    LifecycleTransitionStepResult(
                        step="refresh_existing",
                        status="success",
                        detail={"side_effects": "skipped"},
                    )
                )
                result.action_audit = dict(request.submission_action or {})
            else:
                result.final_status = _string(request.submission_action.get("final_status")) or "submitted"
                step, _ = await self._step("status_transition", _status_update(result.final_status, queue_reason))
                any_step_failed = any_step_failed or step.status != "success"
                result.steps.append(step)
                result.action_audit = {
                    **dict(request.submission_action or {}),
                    "submission_action_completed": False,
                }

        result.action_audit = {
            **dict(result.action_audit or {}),
            "final_status": result.final_status,
            "execution_audit_snapshot_id": (execution_snapshot or {}).get("snapshot_id"),
            "execution_audit_as_of": (execution_snapshot or {}).get("as_of"),
            "correlation_id": trace_context.get("correlation_id"),
            "factory_run_id": trace_context.get("factory_run_id"),
            "trace_id": trace_context.get("trace_id"),
            "parent_task_run_id": trace_context.get("parent_task_run_id"),
            "source_action": trace_context.get("source_action"),
        }
        result.action_refs = {
            "execution_audit_snapshot_id": (execution_snapshot or {}).get("snapshot_id"),
            "lifecycle_task_run_id": (lifecycle_task_run or {}).get("id"),
            "incubation_account_id": ((result.incubation_binding or {}).get("account") or {}).get("id"),
            "diagnostic_account_id": (result.diagnostic_action or {}).get("diagnostic_account_id"),
            "promotion_review_id": (result.live_review_action or {}).get("promotion_review_id"),
            "vector_profile_id": (result.vector_profile or {}).get("id"),
        }
        lifecycle_event = await self._record_domain_event(
            db,
            request=request,
            trace_context=trace_context,
            event_type="strategy.lifecycle_transition.completed",
            payload={
                "final_status": result.final_status,
                "steps": [item.to_dict() for item in result.steps],
                "action_refs": dict(result.action_refs or {}),
                "action_audit": dict(result.action_audit or {}),
            },
            severity="warning" if any_step_failed else "info",
        )
        result.lifecycle_event = lifecycle_event
        closure_review = None
        should_refresh_closure_review = (
            not request.read_only
            and callable(getattr(db, "get_strategy_metrics", None))
            and callable(getattr(db, "get_signal_stats", None))
        )
        if should_refresh_closure_review:
            step, closure_review = await self._step(
                "refresh_closure_review",
                self._refresh_closure_review(
                    db,
                    request=request,
                    trace_context=trace_context,
                    final_status=result.final_status,
                ),
                retryable=True,
                normalizer=lambda payload: {
                    "as_of": dict(payload or {}).get("as_of"),
                    "correlation_id": dict(payload or {}).get("correlation_id"),
                    "factory_run_id": dict(payload or {}).get("factory_run_id"),
                    "overview_closure_snapshot_id": dict(
                        dict(dict(payload or {}).get("incubation") or {}).get("overview") or {}
                    ).get("closure_snapshot_id"),
                },
            )
            any_step_failed = any_step_failed or step.status != "success"
            result.steps.append(step)
        if closure_review:
            overview_payload = dict(
                dict(dict(closure_review.get("incubation") or {}).get("overview") or {})
            )
            result.action_refs["closure_snapshot_id"] = overview_payload.get("closure_snapshot_id")
            result.action_refs["closure_review_as_of"] = closure_review.get("as_of")
        await self._finish_task_run(
            db,
            lifecycle_task_run,
            status="partial" if any_step_failed else "success",
            result=result.to_task_run_result(),
        )
        return result
