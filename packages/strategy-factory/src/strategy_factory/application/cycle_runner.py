"""Single-cycle strategy factory runner."""


from __future__ import annotations

import logging
import inspect
import os
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from ._cycle_success_summary import build_success_run_summary
from .factory_market_views import build_research_window_status
from .factory_execution import (
    DEFAULT_FACTORY_EXECUTION_MODE,
    FACTORY_ENGINE_VERSION,
    build_artifact_refs,
    build_run_artifacts,
    build_run_header,
)
from .compact_contracts import compact_backtest_report, compact_quality_gate_report
from .governance_plane_contract import build_governance_plane_artifact
from .tradeability_gate import build_a_share_tradeability_profile, build_tradeability_certificate
from .research.runner import ResearchPlaneRunner
from .services.candidate_pipeline import CandidatePipeline
from .cycle_pipeline import FactoryCyclePipeline, FactoryStageResult
from .run_models import FactoryRunStatus, StageStatus, summarize_stage_results
from .services.readiness_service import (
    READINESS_CONTRACT_VERSION,
    ReadinessService,
    build_readiness_authority,
)
from ..domain.constants import (
    is_factory_readiness_hard_block_enabled,
    is_factory_runtime_enabled,
    resolve_event_runtime_mode,
)

logger = logging.getLogger(__name__)

FactoryRunResult = dict[str, Any]


@dataclass(slots=True)
class FactoryRunContext:
    db: Any
    factory_pkg: Any
    runtime_adapters: Any
    start: datetime
    trace_id: str
    run_id: str
    execution_mode: str = DEFAULT_FACTORY_EXECUTION_MODE.value
    engine_version: str = FACTORY_ENGINE_VERSION
    parity_role: str = "primary"
    read_only: bool = False
    target_codes: list[str] = field(default_factory=list)


@dataclass(slots=True)
class FactoryCycleOutcome:
    result: FactoryRunResult
    persistence_failures: list[dict[str, Any]] = field(default_factory=list)


def _build_warmup_error_topn(
    warmup_result: dict[str, Any] | None,
    *,
    limit: int = 5,
) -> list[dict[str, Any]]:
    """Extract a structured top-N list of warmup task failures.

    Reads ``warmup_result["schedules"]`` (produced by
    ``run_runtime_data_warmup``) and returns at most ``limit`` entries,
    each with ``task_type / schedule_id / task_id / error_message /
    error_kind``. The shape is what ``factory_runs.summary`` and the
    console warning use; keep it stable.
    """
    if not isinstance(warmup_result, dict):
        return []
    schedules = warmup_result.get("schedules") or []
    if not isinstance(schedules, list):
        return []

    out: list[dict[str, Any]] = []
    for entry in schedules:
        if not isinstance(entry, dict):
            continue
        task = entry.get("task") if isinstance(entry.get("task"), dict) else {}
        status = str(task.get("status") or "").strip().lower()
        if status in {"completed", "success"}:
            continue

        msg = task.get("error_message")
        if not msg and isinstance(task.get("results"), dict):
            errs = task["results"].get("errors") or []
            if errs:
                msg = "; ".join(str(e) for e in errs[:3])

        # Cheap classification so dashboards can group later. Pure
        # string match — keep it conservative; the underlying message
        # itself remains the source of truth.
        msg_str = str(msg or "")
        if "脚本不存在" in msg_str or "script_missing" in msg_str:
            error_kind: str | None = "script_missing"
        elif "timeout" in msg_str.lower():
            error_kind = "timeout"
        elif msg_str:
            error_kind = "exception"
        else:
            error_kind = None

        out.append({
            "task_type": entry.get("task_type") or task.get("task_type"),
            "schedule_id": entry.get("schedule_id"),
            "task_id": task.get("task_id"),
            "error_message": (msg_str or None) and msg_str[:500],
            "error_kind": error_kind,
        })
        if len(out) >= max(1, int(limit or 5)):
            break
    return out


class FactoryCycleRunner:
    """Runs one strategy factory cycle without owning scheduling state."""

    def __init__(self, scheduler: "StrategyFactoryScheduler", context: FactoryRunContext):
        self._scheduler = scheduler
        self._context = context
        self._readiness_service = getattr(self._scheduler, "_readiness_service", ReadinessService())
        self._research_runner = ResearchPlaneRunner(
            self._scheduler,
            self._context.factory_pkg,
        )

    async def _build_factor_research_artifact(self, factor_gateway, db, snapshot: dict[str, Any]) -> dict[str, Any]:
        return await self._research_runner.build_factor_research_artifact(
            factor_gateway,
            db,
            snapshot,
        )

    def _build_factory_readiness(
        self,
        snapshot: dict[str, Any],
        factor_research: dict[str, Any] | None,
    ) -> dict[str, Any]:
        return self._readiness_service.evaluate(snapshot, factor_research)

    @staticmethod
    def _resolve_readiness_stage_status(readiness: dict[str, Any]) -> StageStatus:
        if not bool(readiness.get("can_proceed")):
            return StageStatus.FAILED
        critical_blockers = [
            str(item or "").strip()
            for item in list(readiness.get("critical_blockers") or [])
            if str(item or "").strip()
        ]
        soft_blockers = [
            str(item or "").strip()
            for item in list(readiness.get("blockers") or [])
            if str(item or "").strip()
        ]
        if critical_blockers or soft_blockers:
            return StageStatus.PARTIAL
        return StageStatus.COMPLETED

    async def _persist_enriched_snapshot(self, db, snapshot: dict[str, Any]) -> dict[str, Any] | None:
        if bool(self._context.read_only):
            return None
        method = getattr(db, "save_daily_snapshot", None)
        if not callable(method):
            return None
        try:
            result = method(
                snapshot.get("date") or self._scheduler._now().date(),
                snapshot,
            )
            if inspect.isawaitable(result):
                await result
            return None
        except Exception as exc:
            logger.warning("StrategyFactory: enriched snapshot persistence failed: %s", exc)
            return {
                "operation": "save_daily_snapshot",
                "stage": "collect",
                "error_type": exc.__class__.__name__,
                "error": str(exc),
            }

    def _build_research_plane(
        self,
        *,
        snapshot: dict[str, Any],
        readiness: dict[str, Any] | None = None,
        autonomy_stage: dict[str, Any] | None = None,
        candidates: list[dict[str, Any]] | None = None,
        experiments: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        return self._research_runner.build_research_plane(
            snapshot=snapshot,
            readiness=readiness,
            autonomy_stage=autonomy_stage,
            candidates=candidates,
            experiments=experiments,
        )

    def _attach_result_contracts(self, results: FactoryRunResult) -> FactoryRunResult:
        run_header = build_run_header(
            run_id=self._context.run_id,
            trace_id=self._context.trace_id,
            started_at=str(results.get("started_at") or self._context.start.isoformat()),
            execution_mode=self._context.execution_mode,
            engine_version=self._context.engine_version,
            parity_role=self._context.parity_role,
            read_only=self._context.read_only,
        )
        artifacts = build_run_artifacts(results)
        stage_results = summarize_stage_results(dict(results.get("stages") or {}))
        summary = dict(results.get("summary") or {})
        summary.setdefault("execution_mode", run_header.get("execution_mode"))
        summary.setdefault("engine_version", run_header.get("engine_version"))
        summary.setdefault("read_only", run_header.get("read_only"))
        results["run_header"] = run_header
        results["execution_mode"] = run_header.get("execution_mode")
        results["engine_version"] = run_header.get("engine_version")
        results["summary"] = summary
        results["stage_results"] = stage_results
        results["artifacts"] = artifacts
        results["artifact_refs"] = build_artifact_refs(artifacts)
        results.setdefault("parity_result", {})
        return results

    @staticmethod
    def _build_governance_plane(
        *,
        quality_gate_report: dict[str, Any] | None = None,
        backtest_report: dict[str, Any] | None = None,
        dedup_report: dict[str, Any] | None = None,
        submit_result: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return build_governance_plane_artifact(
            quality_gate_report=quality_gate_report,
            backtest_report=backtest_report,
            dedup_report=dedup_report,
            submit_result=submit_result,
        )

    @staticmethod
    def _apply_research_plane_summary(
        summary: dict[str, Any],
        research_plane: dict[str, Any] | None,
    ) -> dict[str, Any]:
        payload = dict(summary or {})
        plane = dict(research_plane or {})
        research_artifact = dict(plane.get("research_artifact") or {})
        task_artifact = dict(plane.get("task_artifact") or {})
        candidate_artifact = dict(plane.get("candidate_artifact") or {})
        evidence_artifact = dict(plane.get("evidence_artifact") or {})
        payload.update(
            {
                "research_plane_contract_version": plane.get("contract_version"),
                "research_plane_available": bool(plane.get("available")),
                "research_artifact_contract_version": research_artifact.get("contract_version"),
                "research_task_artifact_contract_version": task_artifact.get("contract_version"),
                "research_candidate_artifact_contract_version": candidate_artifact.get("contract_version"),
                "research_evidence_artifact_contract_version": evidence_artifact.get("contract_version"),
                "research_task_artifact_available": bool(task_artifact.get("available")),
                "research_candidate_artifact_available": bool(candidate_artifact.get("available")),
                "research_evidence_artifact_available": bool(evidence_artifact.get("available")),
                "research_task_count": int(task_artifact.get("planned_task_count") or 0),
                "research_candidate_count": int(candidate_artifact.get("candidate_count") or 0),
                "research_candidate_origin_counts": dict(
                    candidate_artifact.get("candidate_origin_counts") or {}
                ),
                "research_local_rule_candidate_count": int(
                    candidate_artifact.get("local_rule_candidate_count") or 0
                ),
                "research_external_autonomy_candidate_count": int(
                    candidate_artifact.get("external_autonomy_candidate_count") or 0
                ),
                "research_governed_candidate_activation_count": int(
                    candidate_artifact.get("governed_candidate_activation_count") or 0
                ),
                "research_experiment_count": int(evidence_artifact.get("experiment_count") or 0),
                "research_task_evidence_count": int(evidence_artifact.get("task_evidence_count") or 0),
                "research_task_origin_counts": dict(task_artifact.get("task_origin_counts") or {}),
                "research_governed_candidate_activation_task_count": int(
                    task_artifact.get("governed_candidate_activation_task_count") or 0
                ),
                "router_artifact_contract_version": task_artifact.get(
                    "router_artifact_contract_version"
                ),
                "router_enabled": bool(task_artifact.get("router_enabled")),
                "router_strict": bool(task_artifact.get("router_strict")),
                "router_telemetry_enabled": bool(task_artifact.get("router_telemetry_enabled")),
                "router_candidate_stock_count": int(
                    task_artifact.get("router_candidate_stock_count") or 0
                ),
                "router_applied_count": int(task_artifact.get("router_applied_count") or 0),
                "router_status_counts": dict(task_artifact.get("router_status_counts") or {}),
                "router_fallback_reason_counts": dict(
                    task_artifact.get("router_fallback_reason_counts") or {}
                ),
                "router_family_counts": dict(task_artifact.get("router_family_counts") or {}),
                "router_holding_bucket_counts": dict(
                    task_artifact.get("router_holding_bucket_counts") or {}
                ),
                "profile_summary_present_count": int(
                    task_artifact.get("profile_summary_present_count") or 0
                ),
                "profile_summary_missing_count": int(
                    task_artifact.get("profile_summary_missing_count") or 0
                ),
                "profile_summary_generated_count": int(
                    task_artifact.get("profile_summary_generated_count") or 0
                ),
                "selected_router_applied_count": int(
                    task_artifact.get("selected_router_applied_count") or 0
                ),
                "selected_profile_summary_missing_count": int(
                    task_artifact.get("selected_profile_summary_missing_count") or 0
                ),
                "lifecycle_feedback_input_contract_version": research_artifact.get(
                    "lifecycle_feedback_input_contract_version"
                ),
                "lifecycle_feedback_input_available": bool(
                    research_artifact.get("lifecycle_feedback_input_available")
                ),
                "lifecycle_feedback_family_count": int(
                    research_artifact.get("lifecycle_feedback_family_count") or 0
                ),
                "lifecycle_feedback_strategy_count": int(
                    research_artifact.get("lifecycle_feedback_strategy_count") or 0
                ),
                "lifecycle_feedback_target_pool_scope_count": int(
                    research_artifact.get("lifecycle_feedback_target_pool_scope_count") or 0
                ),
                "lifecycle_feedback_generator_mode_scope_count": int(
                    research_artifact.get("lifecycle_feedback_generator_mode_scope_count") or 0
                ),
                "lifecycle_feedback_runtime_alert_count": int(
                    research_artifact.get("lifecycle_feedback_runtime_alert_count") or 0
                ),
                "lifecycle_feedback_runtime_risk_event_count": int(
                    research_artifact.get("lifecycle_feedback_runtime_risk_event_count") or 0
                ),
                "lifecycle_feedback_promotion_review_count": int(
                    research_artifact.get("lifecycle_feedback_promotion_review_count") or 0
                ),
                "lifecycle_feedback_promotion_review_status_counts": dict(
                    research_artifact.get("lifecycle_feedback_promotion_review_status_counts") or {}
                ),
            }
        )
        return payload

    @staticmethod
    def _apply_governance_plane_summary(
        summary: dict[str, Any],
        governance_plane: dict[str, Any] | None,
    ) -> dict[str, Any]:
        payload = dict(summary or {})
        plane = dict(governance_plane or {})
        gate_artifact = dict(plane.get("gate_artifact") or {})
        dedup_artifact = dict(plane.get("dedup_artifact") or {})
        submission_artifact = dict(plane.get("submission_artifact") or {})
        evidence_artifact = dict(plane.get("evidence_artifact") or {})
        refresh_existing_count = max(
            int(payload.get("refresh_existing_count") or 0),
            int(dedup_artifact.get("refreshed_existing_count") or 0),
        )
        spawn_revision_from_existing_count = max(
            int(payload.get("spawn_revision_from_existing_count") or 0),
            int(
                dict(dedup_artifact.get("refresh_mode_counts") or {}).get(
                    "spawn_revision_from_existing"
                )
                or 0
            ),
        )
        payload.update(
            {
                "governance_plane_contract_version": plane.get("contract_version"),
                "governance_plane_available": bool(plane.get("available")),
                "governance_gate_artifact_contract_version": gate_artifact.get("contract_version"),
                "governance_dedup_artifact_contract_version": dedup_artifact.get("contract_version"),
                "governance_submission_artifact_contract_version": submission_artifact.get(
                    "contract_version"
                ),
                "governance_evidence_artifact_contract_version": evidence_artifact.get(
                    "contract_version"
                ),
                "governance_gate_artifact_available": bool(gate_artifact.get("available")),
                "governance_dedup_artifact_available": bool(dedup_artifact.get("available")),
                "governance_submission_artifact_available": bool(
                    submission_artifact.get("available")
                ),
                "governance_evidence_artifact_available": bool(evidence_artifact.get("available")),
                "governance_gate_2_passed": int(gate_artifact.get("gate_2_passed") or 0),
                "governance_after_dedup": int(dedup_artifact.get("kept_count") or 0),
                "governance_gate_3_passed": int(submission_artifact.get("gate_3_passed") or 0),
                "governance_refresh_existing_count": int(
                    dedup_artifact.get("refreshed_existing_count") or 0
                ),
                "refresh_existing_count": refresh_existing_count,
                "spawn_revision_from_existing_count": spawn_revision_from_existing_count,
                "research_only_count": int(submission_artifact.get("research_only_count") or 0),
                "deferred_submission_count": int(
                    submission_artifact.get("deferred_submission_count") or 0
                ),
                "validation_grade_distribution": dict(
                    submission_artifact.get("validation_grade_distribution") or {}
                ),
                "raw_validation_grade_distribution": dict(
                    submission_artifact.get("raw_validation_grade_distribution") or {}
                ),
                "effective_validation_grade_distribution": dict(
                    submission_artifact.get("effective_validation_grade_distribution") or {}
                ),
                "raw_validation_total_score_mean": float(
                    submission_artifact.get("raw_validation_total_score_mean") or 0.0
                ),
                "raw_validation_total_score_p50": float(
                    submission_artifact.get("raw_validation_total_score_p50") or 0.0
                ),
                "raw_validation_total_score_p90": float(
                    submission_artifact.get("raw_validation_total_score_p90") or 0.0
                ),
                "raw_validation_a_rate": float(
                    submission_artifact.get("raw_validation_a_rate") or 0.0
                ),
                "raw_validation_b_rate": float(
                    submission_artifact.get("raw_validation_b_rate") or 0.0
                ),
                "raw_validation_c_rate": float(
                    submission_artifact.get("raw_validation_c_rate") or 0.0
                ),
                "raw_validation_d_rate": float(
                    submission_artifact.get("raw_validation_d_rate") or 0.0
                ),
                "strict_incubation_ready_count": int(
                    submission_artifact.get("strict_incubation_ready_count") or 0
                ),
                "strict_incubation_ready_rate": float(
                    submission_artifact.get("strict_incubation_ready_rate") or 0.0
                ),
                "live_candidate_ready_count": int(
                    submission_artifact.get("live_candidate_ready_count") or 0
                ),
                "live_candidate_ready_rate": float(
                    submission_artifact.get("live_candidate_ready_rate") or 0.0
                ),
                "raw_b_or_above_count": int(
                    submission_artifact.get("raw_b_or_above_count") or 0
                ),
                "raw_b_or_above_rate": float(
                    submission_artifact.get("raw_b_or_above_rate") or 0.0
                ),
                "strict_ready_given_raw_b_count": int(
                    submission_artifact.get("strict_ready_given_raw_b_count") or 0
                ),
                "strict_ready_given_raw_b_rate": float(
                    submission_artifact.get("strict_ready_given_raw_b_rate") or 0.0
                ),
                "live_ready_given_raw_b_count": int(
                    submission_artifact.get("live_ready_given_raw_b_count") or 0
                ),
                "live_ready_given_raw_b_rate": float(
                    submission_artifact.get("live_ready_given_raw_b_rate") or 0.0
                ),
                "validation_family_quality_panel": list(
                    submission_artifact.get("validation_family_quality_panel") or []
                ),
                "candidate_local_attempt_count": int(
                    submission_artifact.get("candidate_local_attempt_count") or 0
                ),
                "task_local_attempt_count": int(
                    submission_artifact.get("task_local_attempt_count") or 0
                ),
                "cohort_effective_trials": round(
                    float(submission_artifact.get("cohort_effective_trials") or 0.0),
                    4,
                ),
                "unique_family_holding_universe_count": int(
                    submission_artifact.get("unique_family_holding_universe_count") or 0
                ),
                "economic_semantics_missing_count": int(
                    submission_artifact.get("economic_semantics_missing_count") or 0
                ),
                "governance_multiple_testing_registry_count": int(
                    evidence_artifact.get("multiple_testing_registry_count") or 0
                ),
                "governance_vector_profile_count": int(
                    evidence_artifact.get("vector_profile_count") or 0
                ),
            }
        )
        return payload
    async def run(self) -> FactoryCycleOutcome:
        scheduler = self._scheduler
        db = self._context.db
        factory_pkg = self._context.factory_pkg
        start = self._context.start
        trace_id = self._context.trace_id
        results: FactoryRunResult = {
            "run_id": self._context.run_id,
            "trace_id": trace_id,
            "started_at": start.isoformat(),
            "status": "running",
            "execution_mode": self._context.execution_mode,
            "engine_version": self._context.engine_version,
            "summary": {},
            "stages": {},
        }
        persistence_failures: list[dict[str, Any]] = []

        logger.info("StrategyFactory: starting daily cycle")

        if not is_factory_runtime_enabled():
            readiness_authority = build_readiness_authority(
                can_proceed=False,
                hard_gate_enabled=is_factory_readiness_hard_block_enabled(),
                blocking_reason_codes=["runtime_disabled"],
                critical_blocking_reason_codes=["runtime_disabled"],
                skip_reason="runtime_disabled",
            )
            results["status"] = FactoryRunStatus.SKIPPED.value
            results["completed_at"] = scheduler._now().isoformat()
            results["elapsed_seconds"] = 0.0
            results["stages"]["readiness"] = scheduler._with_stage_meta(
                "readiness",
                trace_id,
                {
                    "readiness_contract_version": READINESS_CONTRACT_VERSION,
                    "runtime_enabled": False,
                    "event_runtime_mode": resolve_event_runtime_mode(),
                    "hard_block_enabled": is_factory_readiness_hard_block_enabled(),
                    "readiness_score": 0.0,
                    "can_proceed": False,
                    "warnings": [],
                    "warning_count": 0,
                    "blockers": ["runtime_disabled"],
                    "blocker_count": 1,
                    "critical_blockers": ["runtime_disabled"],
                    "critical_blocker_count": 1,
                    "authority": readiness_authority,
                    "authority_contract_version": readiness_authority.get("authority_contract_version"),
                    "decision": readiness_authority.get("decision"),
                    "blocked": readiness_authority.get("blocked"),
                    "hard_gate": readiness_authority.get("hard_gate"),
                    "gate_mode": readiness_authority.get("gate_mode"),
                    "blocking_stage": readiness_authority.get("blocking_stage"),
                    "blocking_reason_codes": list(
                        readiness_authority.get("blocking_reason_codes") or []
                    ),
                    "effective_blocking_reason_codes": list(
                        readiness_authority.get("effective_blocking_reason_codes") or []
                    ),
                    "raw_blocking_reason_codes": list(
                        readiness_authority.get("raw_blocking_reason_codes") or []
                    ),
                    "blocking_reason_codes_source": readiness_authority.get(
                        "blocking_reason_codes_source"
                    ),
                    "critical_blocking_reason_codes": list(
                        readiness_authority.get("critical_blocking_reason_codes") or []
                    ),
                    "skip_reason": readiness_authority.get("skip_reason"),
                },
                status=StageStatus.SKIPPED,
                ok=True,
                skip_reason="runtime_disabled",
            )
            results["summary"] = {
                "trace_id": trace_id,
                "runtime_enabled": False,
                "event_runtime_mode": resolve_event_runtime_mode(),
                "factory_readiness_contract_version": READINESS_CONTRACT_VERSION,
                "factory_readiness_authority_version": readiness_authority.get(
                    "authority_contract_version"
                ),
                "factory_readiness_decision": readiness_authority.get("decision"),
                "factory_readiness_hard_gate": readiness_authority.get("hard_gate"),
                "factory_readiness_blocking_stage": readiness_authority.get("blocking_stage"),
                "factory_readiness_effective_blocking_reason_codes": list(
                    readiness_authority.get("effective_blocking_reason_codes") or []
                ),
                "factory_readiness_blocking_reason_codes": list(
                    readiness_authority.get("effective_blocking_reason_codes") or []
                ),
                "factory_readiness_raw_blocking_reason_codes": list(
                    readiness_authority.get("blocking_reason_codes") or []
                ),
                "factory_readiness_blocking_reason_codes_source": readiness_authority.get(
                    "blocking_reason_codes_source"
                ),
                "factory_readiness_critical_blocking_reason_codes": list(
                    readiness_authority.get("critical_blocking_reason_codes") or []
                ),
                "factory_readiness_score": 0.0,
                "factory_readiness_can_proceed": False,
                "factory_readiness_blocker_count": 1,
                "factory_readiness_warning_count": 0,
                "skip_reason": readiness_authority.get("skip_reason"),
                "elapsed_seconds": 0.0,
            }
            governance_plane = self._build_governance_plane()
            results["governance_plane"] = governance_plane
            results["summary"] = self._apply_governance_plane_summary(
                results["summary"],
                governance_plane,
            )
            results["summary"].update(getattr(scheduler, "_build_layered_run_summary", lambda summary, _submit=None: dict(summary or {}))(results["summary"], None))
            scheduler._apply_run_audit(results, persistence_failures=persistence_failures)
            return FactoryCycleOutcome(self._attach_result_contracts(results), persistence_failures)

        try:
            warmup_fn = getattr(scheduler, "_run_startup_warmup", None)
            if callable(warmup_fn):
                warmup_result = await warmup_fn()
            else:
                warmup_result = {"status": "skipped", "ok": True, "failed": 0, "reason": "warmup_not_supported"}
            warmup_status = StageStatus.COMPLETED
            if str(warmup_result.get("status") or "").strip().lower() in {"disabled", "skipped"}:
                warmup_status = StageStatus.SKIPPED
            elif not bool(warmup_result.get("ok", True)):
                warmup_status = StageStatus.FAILED
            elif int(warmup_result.get("failed") or 0) > 0:
                warmup_status = StageStatus.PARTIAL
            results["stages"]["warmup"] = scheduler._with_stage_meta(
                "warmup",
                trace_id,
                warmup_result,
                status=warmup_status,
                ok=warmup_status != StageStatus.FAILED,
                hard_failure=warmup_status == StageStatus.FAILED,
                degraded=warmup_status == StageStatus.PARTIAL,
            )

            # P0 (R3 / R9): when warmup degrades, surface a structured error
            # summary on the console and persist top-N to factory_runs.summary
            # so operators don't have to query sync_tasks to find the cause.
            if warmup_status in (StageStatus.FAILED, StageStatus.PARTIAL):
                error_topn = _build_warmup_error_topn(warmup_result, limit=5)
                results["summary"]["warmup_error_topn"] = error_topn
                results["summary"]["sync_task_failed_count"] = int(
                    warmup_result.get("failed") or 0
                )
                for entry in error_topn:
                    logger.warning(
                        "StrategyFactory: warmup %s task=%s schedule=%s "
                        "task_id=%s error=%s",
                        warmup_status.value,
                        entry.get("task_type") or "unknown",
                        entry.get("schedule_id") or "-",
                        entry.get("task_id") or "-",
                        (entry.get("error_message") or "")[:200],
                    )

            collector = factory_pkg.DataCollector()
            snapshot = await collector.collect(db)
            snapshot["factory_run_id"] = results["run_id"]
            snapshot["trace_id"] = trace_id
            snapshot["factory_execution_mode"] = self._context.execution_mode
            snapshot["execution_mode"] = self._context.execution_mode
            snapshot["factory_engine_version"] = self._context.engine_version
            target_codes = [
                str(code or "").strip()
                for code in list(getattr(self._context, "target_codes", []) or [])
                if str(code or "").strip()
            ]
            if target_codes:
                snapshot["candidate_codes"] = list(target_codes)
                snapshot["target_codes"] = list(target_codes)
                snapshot["requested_target_codes"] = list(target_codes)
                snapshot["target_scope"] = {
                    "mode": "explicit",
                    "source": "factory_run_context",
                    "codes": list(target_codes),
                    "count": len(target_codes),
                }
            # P2-D：将调度器累积的 family 历史表现注入 snapshot
            _family_feedback = dict(getattr(scheduler, "_family_gate_feedback", {}) or {})
            if _family_feedback:
                snapshot["family_gate_feedback"] = _family_feedback
            results["snapshot_summary"] = {
                "date": snapshot.get("date"),
                "fear_greed": snapshot.get("fear_greed_index"),
                "fear_greed_index": snapshot.get("fear_greed_index"),
                "fg_level": snapshot.get("fg_level"),
                "listed_count": snapshot.get("listed_count", 0),
                "incubating_count": snapshot.get("incubating_count", 0),
                "degraded": bool(snapshot.get("degraded")),
                "completion_ratio": (snapshot.get("completeness") or {}).get("completion_ratio", 1.0),
                "missing_sources": (snapshot.get("completeness") or {}).get("missing_sources") or [],
                "optional_unavailable_sources": (
                    (snapshot.get("completeness") or {}).get("optional_unavailable_sources") or []
                ),
                "failure_reason_count": len(snapshot.get("failure_reasons") or []),
                "target_scope": dict(snapshot.get("target_scope") or {}),
            }
            results["stages"]["collect"] = scheduler._with_stage_meta(
                "collect",
                trace_id,
                {
                    **results["snapshot_summary"],
                    "completeness": snapshot.get("completeness") or {},
                },
                status=StageStatus.PARTIAL if bool(snapshot.get("degraded")) else StageStatus.COMPLETED,
            )

            factor_research = {}
            try:
                factor_gateway = scheduler._get_factor_research_gateway()
                factor_refresh_self_heal = str(
                    os.getenv("STRATEGY_FACTORY_FACTOR_REFRESH_SELF_HEAL", "0") or "0"
                ).strip().lower() in {"1", "true", "yes", "on"}
                factor_research_snapshot = {
                    **snapshot,
                    "_factor_refresh_self_heal": factor_refresh_self_heal,
                }
                factor_research = await self._build_factor_research_artifact(
                    factor_gateway,
                    db,
                    factor_research_snapshot,
                )
                snapshot["factor_research"] = dict(factor_research or {})
                factor_summary = dict((snapshot.get("factor_research") or {}).get("summary") or {})
                compact_factor_research = scheduler._compact_factor_research_snapshot(snapshot.get("factor_research"))
                results["stages"]["factor_research"] = scheduler._with_stage_meta(
                    "factor_research",
                    trace_id,
                    {
                        "active_factor_count": int(factor_summary.get("active_factor_count") or 0),
                        "active_candidate_count": int(factor_summary.get("active_candidate_count") or 0),
                        "active_family_count": len(list(factor_summary.get("active_family_names") or [])),
                        "active_regime_count": len(list(factor_summary.get("active_regime_names") or [])),
                        "ranked_factor_count": int(factor_summary.get("ranked_factor_count") or 0),
                        "top_factor_names": list(factor_summary.get("top_factor_names") or []),
                        "top_candidate_names": list(factor_summary.get("top_candidate_names") or []),
                        "active_family_names": list(factor_summary.get("active_family_names") or []),
                        "active_regime_names": list(factor_summary.get("active_regime_names") or []),
                        "preferred_strategy_types": list(factor_summary.get("preferred_strategy_types") or []),
                        "family_preference_order": list(factor_summary.get("family_preference_order") or []),
                        "family_preference_source_mode": factor_summary.get("family_preference_source_mode"),
                        "factor_source_mode": factor_summary.get("factor_source_mode"),
                        "governed_candidate_pool_mode": factor_summary.get("governed_candidate_pool_mode"),
                        "governed_candidate_pool_provisional": bool(
                            factor_summary.get("governed_candidate_pool_provisional")
                        ),
                        "governed_pending_ratio": factor_summary.get("governed_pending_ratio"),
                        "governed_pending_candidate_count": int(
                            factor_summary.get("governed_pending_candidate_count") or 0
                        ),
                        "governed_ineligible_candidate_count": int(
                            factor_summary.get("governed_ineligible_candidate_count") or 0
                        ),
                        "governed_candidate_pool_provisional_spillover_count": int(
                            factor_summary.get("governed_candidate_pool_provisional_spillover_count") or 0
                        ),
                        "governed_candidate_pool_provisional_spillover_policy_status": (
                            factor_summary.get("governed_candidate_pool_provisional_spillover_policy_status")
                        ),
                        "governed_candidate_pool_provisional_pending_count": int(
                            factor_summary.get("governed_candidate_pool_provisional_pending_count") or 0
                        ),
                        "governed_candidate_pool_strict_shortfall_count": int(
                            factor_summary.get("governed_candidate_pool_strict_shortfall_count") or 0
                        ),
                        "degraded": bool((snapshot.get("factor_research") or {}).get("degraded")),
                        "freshness_days": factor_summary.get("freshness_days"),
                        "governed_freshness_days": factor_summary.get("governed_freshness_days"),
                        "governed_blocked_ratio": factor_summary.get("governed_blocked_ratio"),
                        "governed_latest_candidate_at": factor_summary.get("governed_latest_candidate_at"),
                        "latest_factor_date": factor_summary.get("latest_factor_date"),
                        "scheduler_recent_success": bool(factor_summary.get("scheduler_recent_success")),
                        "scheduler_llm_validation_status": factor_summary.get("scheduler_llm_validation_status"),
                        "stock_family_allocation_count": int(factor_summary.get("stock_family_allocation_count") or 0),
                        "stock_family_allocation_entropy": factor_summary.get("stock_family_allocation_entropy"),
                        "stock_family_allocation_source_mode": factor_summary.get("stock_family_allocation_source_mode"),
                        "quality_flags": list(factor_summary.get("quality_flags") or []),
                        "governed_blocking_reason_counts": dict(
                            factor_summary.get("governed_blocking_reason_counts") or {}
                        ),
                        "governed_pending_reason_counts": dict(
                            factor_summary.get("governed_pending_reason_counts") or {}
                        ),
                        "governed_ineligible_reason_counts": dict(
                            factor_summary.get("governed_ineligible_reason_counts") or {}
                        ),
                        "governed_candidate_pool_provisional_spillover_policy": dict(
                            factor_summary.get("governed_candidate_pool_provisional_spillover_policy") or {}
                        ),
                        "refresh_attempted": bool(factor_summary.get("refresh_attempted")),
                        "refresh_status": factor_summary.get("refresh_status"),
                        "refresh_trigger": factor_summary.get("refresh_trigger"),
                        "refresh_error": factor_summary.get("refresh_error"),
                        "active_candidate_pool": compact_factor_research.get("active_candidate_pool") or {},
                        "source_chain": list((snapshot.get("factor_research") or {}).get("source_chain") or []),
                    },
                    status=(
                        StageStatus.PARTIAL
                        if bool((snapshot.get("factor_research") or {}).get("degraded"))
                        or bool(factor_summary.get("stale"))
                        or (
                            bool(factor_summary.get("refresh_attempted"))
                            and str(factor_summary.get("refresh_status") or "").strip().lower()
                            not in {"success", "not_needed", "disabled"}
                        )
                        else StageStatus.COMPLETED
                    ),
                )
            except Exception as exc:
                logger.warning("StrategyFactory: factor research stage failed: %s", exc)
                snapshot["factor_research"] = {
                    "active_factors": [],
                    "ranked_factors": [],
                    "positive_rising_factors": [],
                    "preferred_strategy_types": [],
                    "governed_candidates": [],
                    "active_candidate_pool": {},
                    "active_family_summary": [],
                    "active_regime_summary": [],
                    "research_rationale": [str(exc)],
                    "source_chain": ["factor_research_error"],
                    "degraded": True,
                    "summary": {
                        "active_factor_count": 0,
                        "active_candidate_count": 0,
                        "ranked_factor_count": 0,
                        "top_factor_names": [],
                        "top_candidate_names": [],
                        "active_family_names": [],
                        "active_regime_names": [],
                        "preferred_strategy_types": [],
                        "factor_source_mode": "error_fallback",
                        "governed_blocked_ratio": 0.0,
                        "governed_freshness_days": None,
                        "governed_latest_candidate_at": None,
                        "scheduler_recent_success": False,
                        "scheduler_llm_validation_status": None,
                        "degraded": True,
                        "quality_flags": ["failed"],
                    },
                }
                results["stages"]["factor_research"] = scheduler._with_stage_meta(
                    "factor_research",
                    trace_id,
                    {
                        "active_factor_count": 0,
                        "active_candidate_count": 0,
                        "active_family_count": 0,
                        "active_regime_count": 0,
                        "ranked_factor_count": 0,
                        "top_factor_names": [],
                        "top_candidate_names": [],
                        "active_family_names": [],
                        "active_regime_names": [],
                        "preferred_strategy_types": [],
                        "factor_source_mode": "error_fallback",
                        "governed_blocked_ratio": 0.0,
                        "governed_freshness_days": None,
                        "governed_latest_candidate_at": None,
                        "scheduler_recent_success": False,
                        "scheduler_llm_validation_status": None,
                        "degraded": True,
                        "quality_flags": ["failed"],
                        "error": str(exc),
                    },
                    status=StageStatus.FAILED,
                    ok=False,
                    hard_failure=True,
                    degraded=True,
                )

            results["snapshot_summary"] = {
                **dict(results.get("snapshot_summary") or {}),
                "factor_research": scheduler._compact_factor_research_snapshot(snapshot.get("factor_research")),
            }

            readiness = self._build_factory_readiness(snapshot, snapshot.get("factor_research"))
            factor_stage = dict(results.get("stages", {}).get("factor_research") or {})
            if bool(factor_stage.get("hard_failure")):
                reason = "factor_research_hard_failure"
                blockers = list(readiness.get("blockers") or [])
                critical_blockers = list(readiness.get("critical_blockers") or [])
                if reason not in blockers:
                    blockers.append(reason)
                if reason not in critical_blockers:
                    critical_blockers.append(reason)
                authority = build_readiness_authority(
                    can_proceed=False,
                    hard_gate_enabled=True,
                    blocking_reason_codes=blockers,
                    critical_blocking_reason_codes=critical_blockers,
                    warning_reason_codes=list(readiness.get("warnings") or []),
                    skip_reason=reason,
                    blocking_stage="factor_research",
                )
                readiness.update(authority)
                readiness.update(
                    {
                        "can_proceed": False,
                        "blockers": blockers,
                        "blocker_count": len(blockers),
                        "critical_blockers": critical_blockers,
                        "critical_blocker_count": len(critical_blockers),
                        "skip_reason": reason,
                    }
                )
            snapshot["factory_readiness"] = readiness
            readiness_status = self._resolve_readiness_stage_status(readiness)
            results["stages"]["readiness"] = scheduler._with_stage_meta(
                "readiness",
                trace_id,
                readiness,
                status=readiness_status,
                ok=readiness_status != StageStatus.FAILED,
                hard_failure=False,
                degraded=readiness_status == StageStatus.PARTIAL,
            )
            research_plane = self._build_research_plane(
                snapshot=snapshot,
                readiness=readiness,
            )
            results["research_plane"] = research_plane
            governance_plane = self._build_governance_plane()
            results["governance_plane"] = governance_plane
            if "factor_research" in results.get("stages", {}):
                results["stages"]["factor_research"]["research_plane_contract_version"] = (
                    research_plane.get("contract_version")
                )
                results["stages"]["factor_research"]["research_artifact"] = dict(
                    research_plane.get("research_artifact") or {}
                )
            snapshot_persist_failure = await self._persist_enriched_snapshot(db, snapshot)
            if snapshot_persist_failure is not None:
                persistence_failures.append(snapshot_persist_failure)

            if not bool(readiness.get("can_proceed")):
                elapsed = (scheduler._now() - start).total_seconds()
                factor_research_summary = dict((snapshot.get("factor_research") or {}).get("summary") or {})
                factor_refresh_summary = dict((snapshot.get("factor_research") or {}).get("freshness_repair") or {})
                results["status"] = FactoryRunStatus.SKIPPED.value
                results["completed_at"] = scheduler._now().isoformat()
                results["elapsed_seconds"] = round(elapsed, 1)
                results["summary"] = {
                    "trace_id": trace_id,
                    "runtime_enabled": bool(readiness.get("runtime_enabled")),
                    "event_runtime_mode": readiness.get("event_runtime_mode"),
                    "factory_readiness_contract_version": readiness.get("readiness_contract_version"),
                    "factory_readiness_authority_version": readiness.get(
                        "authority_contract_version"
                    ),
                    "factory_readiness_decision": readiness.get("decision"),
                    "factory_readiness_hard_gate": readiness.get("hard_gate"),
                    "factory_readiness_blocking_stage": readiness.get("blocking_stage"),
                    "factory_readiness_effective_blocking_reason_codes": list(
                        readiness.get("effective_blocking_reason_codes") or []
                    ),
                    "factory_readiness_blocking_reason_codes": list(
                        readiness.get("effective_blocking_reason_codes") or []
                    ),
                    "factory_readiness_raw_blocking_reason_codes": list(
                        readiness.get("blocking_reason_codes") or []
                    ),
                    "factory_readiness_blocking_reason_codes_source": readiness.get(
                        "blocking_reason_codes_source"
                    ),
                    "factory_readiness_critical_blocking_reason_codes": list(
                        readiness.get("critical_blocking_reason_codes") or []
                    ),
                    "factory_readiness_score": readiness.get("readiness_score"),
                    "factory_readiness_can_proceed": False,
                    "factory_readiness_blocker_count": readiness.get("blocker_count", 0),
                    "factory_readiness_warning_count": readiness.get("warning_count", 0),
                    "factor_source_mode": readiness.get("factor_source_mode"),
                        "governed_candidate_pool_active": bool(readiness.get("governed_candidate_pool_active")),
                        "governed_candidate_pool_runtime_state": readiness.get("governed_candidate_pool_runtime_state"),
                        "active_candidate_count": int(readiness.get("active_candidate_count") or 0),
                        "governed_source_candidate_count": int(readiness.get("governed_source_candidate_count") or 0),
                        "governed_blocked_candidate_count": int(readiness.get("governed_blocked_candidate_count") or 0),
                        "governed_blocked_ratio": readiness.get("governed_blocked_ratio"),
                        "governed_pending_candidate_count": int(
                            readiness.get("governed_pending_candidate_count") or 0
                        ),
                        "governed_pending_ratio": readiness.get("governed_pending_ratio"),
                        "governed_freshness_days": readiness.get("governed_freshness_days"),
                        "scheduler_recent_success": bool(readiness.get("scheduler_recent_success")),
                        "scheduler_llm_validation_status": readiness.get("scheduler_llm_validation_status"),
                        "factor_llm_provider_enabled": factor_research_summary.get("factor_llm_provider_enabled"),
                        "factor_llm_provider_ready": factor_research_summary.get("factor_llm_provider_ready"),
                    "factor_llm_provider_health_status": factor_research_summary.get("factor_llm_provider_health_status"),
                    "factor_llm_provider_rebuild_count": factor_research_summary.get("factor_llm_provider_rebuild_count"),
                    "factor_llm_provider_last_error_type": factor_research_summary.get("factor_llm_provider_last_error_type"),
                    "factor_research_stale": bool(factor_research_summary.get("stale")),
                    "factor_research_degraded": bool((snapshot.get("factor_research") or {}).get("degraded")),
                    "factor_research_refresh_attempted": bool(factor_refresh_summary.get("refresh_attempted")),
                        "factor_research_refresh_status": factor_refresh_summary.get("refresh_status"),
                        "factor_research_refresh_trigger": factor_refresh_summary.get("refresh_trigger"),
                        "governed_exclusion_reason_counts": dict(readiness.get("governed_exclusion_reason_counts") or {}),
                        "governed_blocking_reason_counts": dict(
                            readiness.get("governed_blocking_reason_counts") or {}
                        ),
                        "governed_pending_reason_counts": dict(
                            readiness.get("governed_pending_reason_counts") or {}
                        ),
                        "governed_risk_counts": dict(readiness.get("governed_risk_counts") or {}),
                        "skip_reason": readiness.get("skip_reason") or "readiness_blocked",
                        "elapsed_seconds": round(elapsed, 1),
                    }
                results["summary"] = self._apply_research_plane_summary(
                    results["summary"],
                    research_plane,
                )
                results["summary"] = self._apply_governance_plane_summary(
                    results["summary"],
                    governance_plane,
                )
                results["summary"].update(getattr(scheduler, "_build_layered_run_summary", lambda summary, _submit=None: dict(summary or {}))(results["summary"], None))
                logger.warning(
                    "StrategyFactory: run %s blocked by readiness controls: %s",
                    results.get("run_id"),
                    readiness.get("blockers"),
                )
                scheduler._apply_run_audit(results, persistence_failures=persistence_failures)
                return FactoryCycleOutcome(self._attach_result_contracts(results), persistence_failures)

            generation = await self._research_runner.run_generation(db, snapshot)
            candidates = list(generation.generated_candidates)
            spawn_report = dict(generation.local_spawn_report or {})
            results["stages"]["spawn"] = scheduler._with_stage_meta(
                "spawn",
                trace_id,
                {"count": len(generation.local_candidates), **spawn_report},
                status=StageStatus.COMPLETED,
            )

            autonomy_stage = dict(generation.autonomy_stage or {"generated_count": 0})
            autonomy_experiments = list(generation.experiments or [])
            full_market_topn = dict(generation.full_market_topn or {})
            full_market_score_rows = [
                dict(item or {})
                for item in list(generation.full_market_score_rows or [])
                if isinstance(item, dict)
            ]
            if full_market_topn:
                results["full_market_topn"] = full_market_topn
            if full_market_score_rows:
                results["_full_market_score_rows"] = full_market_score_rows
            autonomy_stage_status = StageStatus.COMPLETED
            if generation.autonomy_error:
                autonomy_stage_status = StageStatus.FAILED
            else:
                external_llm_status = str(autonomy_stage.get("external_llm_status") or "").strip().lower()
                if external_llm_status == "failed":
                    autonomy_stage_status = StageStatus.FAILED
                elif external_llm_status in {"partial"} or int(autonomy_stage.get("persistence_failure_count") or 0) > 0:
                    autonomy_stage_status = StageStatus.PARTIAL
                elif external_llm_status == "skipped":
                    autonomy_stage_status = StageStatus.SKIPPED
            results["stages"]["autonomy"] = scheduler._with_stage_meta(
                "autonomy",
                trace_id,
                autonomy_stage,
                status=autonomy_stage_status,
                ok=autonomy_stage_status != StageStatus.FAILED,
                hard_failure=autonomy_stage_status == StageStatus.FAILED,
                degraded=autonomy_stage_status == StageStatus.PARTIAL,
            )

            research_plane = self._build_research_plane(
                snapshot=snapshot,
                readiness=readiness,
                autonomy_stage=autonomy_stage,
                candidates=candidates,
                experiments=autonomy_experiments,
            )
            results["research_plane"] = research_plane
            if "factor_research" in results.get("stages", {}):
                results["stages"]["factor_research"]["research_plane_contract_version"] = (
                    research_plane.get("contract_version")
                )
                results["stages"]["factor_research"]["research_artifact"] = dict(
                    research_plane.get("research_artifact") or {}
                )
            if "autonomy" in results.get("stages", {}):
                results["stages"]["autonomy"]["research_plane_contract_version"] = (
                    research_plane.get("contract_version")
                )
                results["stages"]["autonomy"]["task_artifact"] = dict(
                    research_plane.get("task_artifact") or {}
                )
                results["stages"]["autonomy"]["candidate_artifact"] = dict(
                    research_plane.get("candidate_artifact") or {}
                )
                results["stages"]["autonomy"]["evidence_artifact"] = dict(
                    research_plane.get("evidence_artifact") or {}
                )

            pipeline_run = await CandidatePipeline(factory_pkg, scheduler).run(
                candidates,
                snapshot,
                db,
                read_only=bool(self._context.read_only),
                execution_mode=self._context.execution_mode,
            )
            passed = list(pipeline_run.passed or [])
            unique = list(pipeline_run.unique or [])
            quality_gate_report = compact_quality_gate_report(pipeline_run.quality_gate_report or {})
            backtest_report = compact_backtest_report(pipeline_run.backtest_report or {})
            submit_result = dict(pipeline_run.submit_result or {})

            backtest_summary = backtest_report.get("summary") or {}
            results["stages"]["quality_gate"] = scheduler._with_stage_meta(
                "quality_gate",
                trace_id,
                quality_gate_report,
                status=StageStatus.COMPLETED,
            )
            results["stages"]["backtest"] = scheduler._with_stage_meta(
                "backtest",
                trace_id,
                {
                    "input_count": backtest_summary.get("input_count", len(candidates)),
                    "passed_count": backtest_summary.get("passed_count", len(passed)),
                    "failed_count": backtest_summary.get("failed_count", max(len(candidates) - len(passed), 0)),
                    **backtest_report,
                },
                status=StageStatus.COMPLETED,
            )
            results["stages"]["deduplicate"] = scheduler._with_stage_meta(
                "deduplicate",
                trace_id,
                pipeline_run.deduplicator_report(),
                status=StageStatus.COMPLETED,
            )
            results["stages"]["submit"] = scheduler._with_stage_meta(
                "submit",
                trace_id,
                submit_result,
                status=StageStatus.COMPLETED,
            )
            results["quality_gate"] = quality_gate_report
            results["gate_report"] = quality_gate_report
            results["backtest_report"] = backtest_report
            results["submit_result"] = submit_result
            results["governance_plane"] = dict(pipeline_run.governance_plane or {})

            # Legacy quality/backtest/submission gates remain candidate screens.
            # These certificates are separate and hash-bound; an LLM, legacy
            # backtest pass, or successful submission cannot grant certification.
            snapshot_binding = snapshot.get("tradeability_data_snapshot")
            if not isinstance(snapshot_binding, dict):
                snapshot_binding = {}
            tradeability_certificates: list[dict[str, Any]] = []
            raw_backtest_evidence_count = 0
            raw_paper_evidence_count = 0
            for candidate in candidates:
                if isinstance(candidate.get("tradeability_backtest_evidence"), dict):
                    raw_backtest_evidence_count += 1
                if isinstance(candidate.get("tradeability_paper_evidence"), dict):
                    raw_paper_evidence_count += 1
                profile = candidate.get("tradeability_profile")
                if not isinstance(profile, dict):
                    profile = build_a_share_tradeability_profile(candidate)
                certificate = build_tradeability_certificate(
                    candidate,
                    profile=profile,
                    data_snapshot=snapshot_binding,
                    backtest_evidence=(
                        candidate.get("tradeability_backtest_evidence")
                        if isinstance(candidate.get("tradeability_backtest_evidence"), dict)
                        else None
                    ),
                    paper_evidence=(
                        candidate.get("tradeability_paper_evidence")
                        if isinstance(candidate.get("tradeability_paper_evidence"), dict)
                        else None
                    ),
                    issued_at=scheduler._now().isoformat(),
                )
                tradeability_certificates.append(certificate)
            results["tradeability_certificates"] = tradeability_certificates
            tradeability_status_counts: dict[str, int] = {}
            for certificate in tradeability_certificates:
                status = str(certificate.get("status") or "research_only")
                tradeability_status_counts[status] = tradeability_status_counts.get(status, 0) + 1
            results["tradeability_certification"] = {
                "certificate_count": len(tradeability_certificates),
                "status_counts": tradeability_status_counts,
                "reason_counts": {
                    reason: sum(
                        1
                        for certificate in tradeability_certificates
                        if reason in list(certificate.get("reason_codes") or [])
                    )
                    for reason in sorted(
                        {
                            reason
                            for certificate in tradeability_certificates
                            for reason in list(certificate.get("reason_codes") or [])
                        }
                    )
                },
                "raw_backtest_evidence_count": raw_backtest_evidence_count,
                "raw_paper_evidence_count": raw_paper_evidence_count,
                "data_snapshot_bound": bool(
                    snapshot_binding.get("snapshot_hash") or snapshot_binding.get("content_hash")
                ),
                "evidence_generation": "candidate_payload_only",
                "backtest_candidate_count": tradeability_status_counts.get("backtest_candidate", 0),
                "paper_trial_count": tradeability_status_counts.get("paper_trial", 0),
                "paper_validated_count": tradeability_status_counts.get("paper_validated", 0),
                "rejected_count": tradeability_status_counts.get("rejected", 0),
                "legacy_backtest_or_submission_pass_is_certification": False,
                "certificate_only_no_strategy_promotion": True,
                "live_trading_enabled": False,
            }

            eliminator = factory_pkg.EliminationChecker()
            eliminated = await eliminator.check(db, snapshot.get("fg_level", "neutral"))
            results["stages"]["elimination"] = scheduler._with_stage_meta(
                "elimination",
                trace_id,
                {"count": len(eliminated), "items": eliminated},
                status=StageStatus.COMPLETED,
            )

            elapsed = (scheduler._now() - start).total_seconds()
            results["status"] = FactoryRunStatus.SUCCESS.value
            results["completed_at"] = scheduler._now().isoformat()
            results["elapsed_seconds"] = round(elapsed, 1)
            autonomy_summary = results.get("stages", {}).get("autonomy") or {}
            vector_summary = scheduler._aggregate_vector_submission_metrics(submit_result)
            task_scan_summary = dict((autonomy_summary.get("task_scan") or {}).get("summary") or {})
            task_source_counts = dict(
                autonomy_summary.get("task_source_counts")
                or task_scan_summary.get("task_sources")
                or {}
            )
            bulk_stock_matrix_family_counts = dict(
                task_scan_summary.get("bulk_stock_matrix_family_counts") or {}
            )
            bulk_stock_matrix_allocation_pass_counts = dict(
                task_scan_summary.get("bulk_stock_matrix_allocation_pass_counts") or {}
            )
            factor_research_summary = dict((snapshot.get("factor_research") or {}).get("summary") or {})
            factor_refresh_summary = dict((snapshot.get("factor_research") or {}).get("freshness_repair") or {})
            readiness_summary = dict(results.get("stages", {}).get("readiness") or {})
            warmup_summary = dict(results.get("stages", {}).get("warmup") or {})
            backtest_audit_summary = scheduler._aggregate_backtest_audit_metrics(backtest_report)
            submission_audit_summary = scheduler._aggregate_submission_audit_metrics(submit_result)
            results["summary"] = build_success_run_summary(
                trace_id=trace_id,
                snapshot=snapshot,
                candidates=candidates,
                passed=passed,
                unique=unique,
                eliminated=eliminated,
                spawn_report=spawn_report,
                submit_result=submit_result,
                quality_gate_report=quality_gate_report,
                backtest_report=backtest_report,
                autonomy_summary=autonomy_summary,
                task_scan_summary=task_scan_summary,
                task_source_counts=task_source_counts,
                bulk_stock_matrix_family_counts=bulk_stock_matrix_family_counts,
                bulk_stock_matrix_allocation_pass_counts=bulk_stock_matrix_allocation_pass_counts,
                factor_research_summary=factor_research_summary,
                factor_refresh_summary=factor_refresh_summary,
                readiness_summary=readiness_summary,
                warmup_summary=warmup_summary,
                backtest_audit_summary=backtest_audit_summary,
                submission_audit_summary=submission_audit_summary,
                vector_summary=vector_summary,
                elapsed=elapsed,
            )
            results["summary"]["tradeability_certification"] = dict(
                results.get("tradeability_certification") or {}
            )
            results["research_window"] = build_research_window_status(results["summary"])
            results["summary"]["research_window"] = dict(results.get("research_window") or {})
            if full_market_topn:
                results["summary"]["full_market_topn"] = full_market_topn
            results["summary"]["spawn_policy_version"] = (
                dict(spawn_report.get("summary") or {}).get("policy_version")
            )
            results["summary"] = self._apply_research_plane_summary(
                results["summary"],
                research_plane,
            )
            results["summary"] = self._apply_governance_plane_summary(
                results["summary"],
                results["governance_plane"],
            )
            observe_first_summary = dict(quality_gate_report.get("observe_first") or {})
            if observe_first_summary:
                results["summary"].update(
                    {
                        "observe_first_enabled": bool(observe_first_summary.get("enabled")),
                        "observe_first_mode": observe_first_summary.get("mode"),
                        "observed_candidate_count": int(
                            observe_first_summary.get("deduped_observe_intake_count")
                            or observe_first_summary.get("observe_intake_count")
                            or 0
                        ),
                        "pre_observe_hard_reject_count": int(
                            observe_first_summary.get("pre_observe_hard_reject_count") or 0
                        ),
                        "gate3_pre_observe_block_count": int(
                            observe_first_summary.get("gate3_pre_observe_block_count") or 0
                        ),
                        "pre_observe_gate_removed": bool(
                            observe_first_summary.get("pre_observe_gate_removed")
                        ),
                        "legacy_gate_report_mode": observe_first_summary.get(
                            "legacy_gate_report_mode"
                        ),
                        "legacy_gate_executed": bool(
                            observe_first_summary.get("legacy_gate_executed")
                        ),
                        "legacy_funnel_executed": bool(
                            observe_first_summary.get("legacy_funnel_executed")
                        ),
                        "evidence_scoring_mode": (
                            observe_first_summary.get("evidence_scoring_mode")
                            or quality_gate_report.get("evidence_scoring_mode")
                        ),
                        "stock_first_flow": "observe_first",
                    }
                )
            results["summary"].update(getattr(scheduler, "_build_layered_run_summary", lambda summary, _submit=None: dict(summary or {}))(results["summary"], submit_result))
            scheduler._apply_run_audit(results, persistence_failures=persistence_failures)
            submit_log_summary = {
                "created": submit_result.get("created", 0),
                "created_total": submit_result.get("created_total", 0),
                "created_strategy_pool": submit_result.get("created_strategy_pool", 0),
                "created_audit_only": submit_result.get("created_audit_only", 0),
                "submitted": submit_result.get("submitted", 0),
                "gate_3_input": submit_result.get("gate_3_input", len(unique)),
                "gate_3_passed": submit_result.get("gate_3_passed", 0),
                "gate_3_failed": submit_result.get("gate_3_failed", 0),
                "gate_3_recorded": submit_result.get("gate_3_recorded", 0),
                "gate3_quality_recorded": submit_result.get("gate3_quality_recorded", 0),
                "gate3_record_quality_qualified_count": submit_result.get(
                    "gate3_record_quality_qualified_count",
                    0,
                ),
                "gate3_record_diagnostic_only_count": submit_result.get(
                    "gate3_record_diagnostic_only_count",
                    0,
                ),
                "gate_3_failure_topn": list(submit_result.get("gate_3_failure_reason_topn") or [])[:5],
                "validation_grade_counts": dict(results["summary"].get("validation_grade_counts") or {}),
                "qualified_validation_grade_counts": dict(
                    results["summary"].get("qualified_validation_grade_counts") or {}
                ),
                "diagnostic_validation_grade_counts": dict(
                    results["summary"].get("diagnostic_validation_grade_counts") or {}
                ),
            }

            logger.info(
                "StrategyFactory: completed in %.1fs — spawned %d, backtest passed %d, dedup %d, submitted %s, eliminated %d",
                elapsed,
                len(candidates),
                len(passed),
                len(unique),
                submit_log_summary,
                len(eliminated),
            )
        except Exception as exc:
            elapsed = (scheduler._now() - start).total_seconds()
            logger.error("StrategyFactory: run_once failed: %s", exc, exc_info=True)
            results["status"] = FactoryRunStatus.FAILED.value
            results["completed_at"] = scheduler._now().isoformat()
            results["elapsed_seconds"] = round(elapsed, 1)
            results["error"] = str(exc)
            results["summary"] = {"trace_id": trace_id, "elapsed_seconds": round(elapsed, 1), "error": str(exc)}
            results["summary"].update(getattr(scheduler, "_build_layered_run_summary", lambda summary, _submit=None: dict(summary or {}))(results["summary"], None))
            scheduler._apply_run_audit(results, persistence_failures=persistence_failures)

        return FactoryCycleOutcome(self._attach_result_contracts(results), persistence_failures)



async def _legacy_factory_cycle_runner_run(self) -> FactoryCycleOutcome:
    """Original cycle body used as pipeline legacy delegate."""
    return await FactoryCycleRunner._legacy_run_impl(self)


async def _pipeline_factory_cycle_runner_run(self) -> FactoryCycleOutcome:
    return await FactoryCyclePipeline(
        self,
        legacy_run=getattr(self, "_run_legacy_cycle", _legacy_factory_cycle_runner_run),
    ).run()


# Preserve the concrete cycle body under a stable legacy name, then route run()
# through the explicit pipeline preflight/compat wrapper.
FactoryCycleRunner._legacy_run_impl = FactoryCycleRunner.run
FactoryCycleRunner._run_legacy_cycle = _legacy_factory_cycle_runner_run
FactoryCycleRunner.run = _pipeline_factory_cycle_runner_run

__all__ = [
    "FactoryCycleOutcome",
    "FactoryCyclePipeline",
    "FactoryCycleRunner",
    "FactoryRunContext",
    "FactoryRunResult",
    "FactoryStageResult",
]
