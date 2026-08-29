"""CandidatePipeline wraps candidate processing for legacy and observe-first modes.

Provides a typed service boundary for the candidate processing phase,
replacing the inline pipeline logic in FactoryCycleRunner.run().

P4 refactor: the pipeline exposes a structured CandidatePipelineReport so
callers (cycle_runner, tests) can access stable counts without fragile dict
key lookups.
"""

from __future__ import annotations

import inspect
import logging
from typing import Any

from ..compact_contracts import compact_backtest_report, compact_quality_gate_report
from ..factory_execution import resolve_runtime_mode_flags
from ..governance_plane_contract import build_governance_plane_artifact
from .._runtime_toggles import observe_first_enabled as _observe_first_enabled
from ..trade_prediction_contract import attach_trade_prediction_context
from ...domain.candidates import CandidatePipelineReport

logger = logging.getLogger(__name__)


class CandidatePipeline:
    """Executes gate-0/1/2 → dedup → gate-3 for a list of candidates.

    Legacy mode delegates to the existing ``factory_pkg`` gate helpers.
    Stock-first observe mode bypasses those helpers and emits an evidence
    report before deduplication and observe intake.

    Usage::

        pipeline = CandidatePipeline(factory_pkg, scheduler)
        result = await pipeline.run(candidates, snapshot, db)
        # result.report.submitted, result.quality_gate_report, etc.
    """

    def __init__(self, factory_pkg: Any, scheduler: Any) -> None:
        self._pkg = factory_pkg
        self._scheduler = scheduler

    async def run(
        self,
        candidates: list[dict[str, Any]],
        snapshot: dict[str, Any],
        db: Any,
        *,
        read_only: bool = False,
        execution_mode: str | None = None,
    ) -> "PipelineRunResult":
        """Run the full candidate pipeline and return a structured result."""
        pkg = self._pkg
        scheduler = self._scheduler
        backtest_filter = pkg.BacktestFilter()
        deduplicator = scheduler._build_deduplicator(pkg)
        submitter = scheduler._build_submitter(pkg)
        resolved_execution_mode = (
            execution_mode
            or snapshot.get("factory_execution_mode")
            or snapshot.get("execution_mode")
        )
        candidates = [
            attach_trade_prediction_context(candidate, snapshot=snapshot)
            for candidate in list(candidates or [])
        ]
        runtime_mode_flags = resolve_runtime_mode_flags(
            resolved_execution_mode or "legacy_primary"
        )
        observe_first_mode = bool(runtime_mode_flags.get("observe_first_enabled"))
        if not resolved_execution_mode:
            observe_first_mode = bool(_observe_first_enabled())
        task_board = getattr(scheduler, "_task_board", None)
        board_task = None
        claim_token = None
        if task_board is not None:
            try:
                task_type = "evidence_scoring" if observe_first_mode else "quality_gate"
                title = (
                    "Candidate evidence scoring/dedup/observe intake"
                    if observe_first_mode
                    else "Candidate quality/backtest/dedup/submit pipeline"
                )
                board_task = task_board.create_task(
                    task_type=task_type,
                    title=title,
                    payload={
                        "candidate_count": len(candidates),
                        "read_only": bool(read_only),
                        "snapshot_date": snapshot.get("date"),
                        "observe_first_mode": bool(observe_first_mode),
                        "execution_mode": str(resolved_execution_mode or ""),
                    },
                )
                claimed_task = task_board.claim_task(
                    board_task["task_id"],
                    worker_id="candidate_pipeline",
                )
                if claimed_task is not None:
                    board_task = claimed_task
                    claim_token = claimed_task.get("claim_token")
            except Exception as exc:
                logger.debug("candidate pipeline task board create/claim failed: %s", exc)

        try:
            supports_unified_submission = bool(
                candidates
                and not read_only
                and hasattr(pkg, "run_gated_submission_pipeline")
                and hasattr(pkg, "run_gated_filter")
                and inspect.iscoroutinefunction(getattr(db, "get_klines", None))
            )
            supports_unified_gate = bool(
                candidates
                and hasattr(pkg, "run_gated_filter")
                and inspect.iscoroutinefunction(getattr(db, "get_klines", None))
            )

            if observe_first_mode and candidates:
                passed = await backtest_filter.filter(candidates, db)
                backtest_report = self._extract_backtest_report(
                    {},
                    backtest_filter,
                    candidates,
                    passed,
                )
                observe_candidates = self._mark_observe_first_candidates(passed)
                unique = await deduplicator.deduplicate(observe_candidates, db)
                submit_result = await submitter.submit(
                    unique,
                    snapshot,
                    db,
                    read_only=read_only,
                )
                quality_gate_report = self._build_observe_first_evidence_report(
                    candidates,
                    observe_candidates,
                    unique,
                    submit_result,
                    execution_mode=str(resolved_execution_mode or ""),
                    backtest_report=backtest_report,
                )
                quality_gate_report["observe_first"] = {
                    "enabled": True,
                    "gate_passed_count": len(passed),
                    "observe_intake_count": len(observe_candidates),
                    "deduped_observe_intake_count": len(unique),
                    "pre_observe_hard_reject_count": 0,
                    "pre_observe_evidence_reject_count": max(len(candidates) - len(passed), 0),
                    "gate3_pre_observe_block_count": max(len(candidates) - len(passed), 0),
                    "mode": "backtest_evidence_screen",
                    "pre_observe_gate_removed": True,
                    "legacy_gate_executed": False,
                    "legacy_funnel_executed": False,
                    "legacy_gate_report_mode": "not_executed",
                    "evidence_scoring_mode": "observe_first_backtest_evidence_screen",
                    "execution_mode": str(resolved_execution_mode or ""),
                }
                quality_gate_report = compact_quality_gate_report(quality_gate_report)
                backtest_report = compact_backtest_report(backtest_report)
            elif supports_unified_submission:
                pipeline_run = await pkg.run_gated_submission_pipeline(
                    candidates,
                    snapshot,
                    db,
                    backtest_filter=backtest_filter,
                    deduplicator=deduplicator,
                    submitter=submitter,
                    gated_runner=pkg.run_gated_filter,
                    kline_cache=getattr(backtest_filter, "_kline_cache", None),
                )
                passed = list(pipeline_run.get("passed") or [])
                unique = list(pipeline_run.get("unique") or [])
                quality_gate_report = dict(
                    pipeline_run.get("gate_report") or pipeline_run.get("quality_gate") or {}
                )
                backtest_report = dict(pipeline_run.get("backtest_report") or {})
                submit_result = dict(pipeline_run.get("submit_result") or {})
                quality_gate_report = compact_quality_gate_report(quality_gate_report)
                backtest_report = compact_backtest_report(backtest_report)
            elif supports_unified_gate:
                gate_run = await pkg.run_gated_filter(
                    candidates,
                    db,
                    backtest_filter,
                    kline_cache=getattr(backtest_filter, "_kline_cache", None),
                )
                passed = list(gate_run.get("passed") or [])
                quality_gate_report = dict(
                    gate_run.get("gate_report") or gate_run.get("quality_gate") or {}
                )
                backtest_report = self._extract_backtest_report(
                    quality_gate_report, backtest_filter, candidates, passed
                )
                if not quality_gate_report:
                    quality_gate_report = pkg.build_legacy_gate_report(
                        candidates, passed, backtest_report
                    )
                unique = await deduplicator.deduplicate(passed, db)
                submit_result = await submitter.submit(
                    unique,
                    snapshot,
                    db,
                    read_only=read_only,
                )
                quality_gate_report = pkg.finalize_gate_report(quality_gate_report, submit_result)
                quality_gate_report = compact_quality_gate_report(quality_gate_report)
                backtest_report = compact_backtest_report(backtest_report)
            else:
                passed = await backtest_filter.filter(candidates, db)
                quality_gate_report = {}
                backtest_report = self._extract_backtest_report(
                    {}, backtest_filter, candidates, passed
                )
                quality_gate_report = pkg.build_legacy_gate_report(
                    candidates, passed, backtest_report
                )
                unique = await deduplicator.deduplicate(passed, db)
                submit_result = await submitter.submit(
                    unique,
                    snapshot,
                    db,
                    read_only=read_only,
                )
                quality_gate_report = pkg.finalize_gate_report(quality_gate_report, submit_result)
                quality_gate_report = compact_quality_gate_report(quality_gate_report)
                backtest_report = compact_backtest_report(backtest_report)

            report = self._build_report(
                candidates, passed, unique, submit_result, quality_gate_report, backtest_report
            )
            dedup_report = (
                dict(deduplicator.get_last_report() or {})
                if hasattr(deduplicator, "get_last_report")
                else {}
            )
            governance_plane = build_governance_plane_artifact(
                candidates=candidates,
                quality_gate_report=quality_gate_report,
                backtest_report=backtest_report,
                dedup_report=dedup_report,
                submit_result=submit_result,
            )

            result = PipelineRunResult(
                passed=passed,
                unique=unique,
                quality_gate_report=quality_gate_report,
                backtest_report=backtest_report,
                submit_result=submit_result,
                deduplicator=deduplicator,
                dedup_report=dedup_report,
                report=report,
                governance_plane=governance_plane,
                read_only=read_only,
            )
            if task_board is not None and board_task is not None:
                try:
                    completed = task_board.complete_task(
                        board_task["task_id"],
                        claim_token=claim_token,
                        artifact_refs=[
                            item
                            for item in [
                                result.gate_artifact,
                                result.dedup_artifact,
                                result.submission_artifact,
                                result.evidence_artifact,
                            ]
                            if item
                        ],
                        result={
                            "passed": len(passed),
                            "unique": len(unique),
                            "submitted": report.submitted,
                            "read_only": bool(read_only),
                        },
                    )
                    result.task_board = {"task_id": board_task["task_id"], "status": (completed or {}).get("status")}
                except Exception as exc:
                    logger.debug("candidate pipeline task board complete failed: %s", exc)
            return result
        except Exception as exc:
            if task_board is not None and board_task is not None:
                try:
                    task_board.block_task(board_task["task_id"], str(exc), claim_token=claim_token)
                except Exception as block_exc:
                    logger.debug("candidate pipeline: task_board.block_task failed: %s", block_exc)
            raise

    @staticmethod
    def _candidate_source_counts(candidates: list[dict[str, Any]]) -> dict[str, int]:
        counts: dict[str, int] = {}
        for item in list(candidates or []):
            candidate = dict(item or {})
            source = str(
                candidate.get("task_source")
                or candidate.get("source")
                or (candidate.get("params") or {}).get("task_source")
                or "unknown"
            ).strip() or "unknown"
            counts[source] = counts.get(source, 0) + 1
        return counts

    @staticmethod
    def _router_status_counts(candidates: list[dict[str, Any]]) -> dict[str, int]:
        counts: dict[str, int] = {}
        for item in list(candidates or []):
            candidate = dict(item or {})
            router = dict(
                candidate.get("stock_first_router")
                or (candidate.get("params") or {}).get("stock_first_router")
                or {}
            )
            status = str(router.get("status") or "not_available").strip().lower() or "not_available"
            counts[status] = counts.get(status, 0) + 1
        return counts

    @classmethod
    def _build_observe_first_backtest_report(
        cls,
        candidates: list[dict[str, Any]],
    ) -> dict[str, Any]:
        return {
            "contract_version": "strategy_factory.observe_first_backtest_report.v1",
            "mode": "not_run_pre_observe",
            "legacy_gate_executed": False,
            "legacy_funnel_executed": False,
            "summary": {
                "mode": "not_run_pre_observe",
                "input_count": len(candidates),
                "passed_count": 0,
                "failed_count": 0,
                "skipped_count": len(candidates),
                "failed_reason_counts": {},
                "candidate_source_counts": cls._candidate_source_counts(candidates),
                "router_status_counts": cls._router_status_counts(candidates),
                "pre_observe_backtest_skipped": True,
                "skip_reason": "observe_first_no_legacy_gate",
            },
            "passed": [],
            "failed": [],
        }

    @classmethod
    def _build_observe_first_evidence_report(
        cls,
        candidates: list[dict[str, Any]],
        observe_candidates: list[dict[str, Any]],
        unique: list[dict[str, Any]],
        submit_result: dict[str, Any],
        *,
        execution_mode: str,
        backtest_report: dict[str, Any],
    ) -> dict[str, Any]:
        gate_3_input = int(submit_result.get("gate_3_input") or len(unique))
        gate_3_passed = int(submit_result.get("gate_3_passed") or 0)
        gate_3_failed = int(submit_result.get("gate_3_failed") or max(gate_3_input - gate_3_passed, 0))
        return {
            "contract_version": "strategy_factory.observe_first_evidence_report.v1",
            "legacy_gate_executed": False,
            "legacy_funnel_executed": False,
            "evidence_scoring_mode": "observe_first_backtest_evidence_screen",
            "pre_observe_gate_removed": True,
            "execution_mode": str(execution_mode or ""),
            "evidence_scoring": {
                "mode": "observe_first_backtest_evidence_screen",
                "input_count": len(candidates),
                "scored_count": len(candidates),
                "evidence_passed_count": len(observe_candidates),
                "pre_observe_evidence_reject_count": max(len(candidates) - len(observe_candidates), 0),
                "observe_intake_count": len(observe_candidates),
                "deduped_observe_intake_count": len(unique),
                "pre_observe_hard_reject_count": 0,
                "gate3_pre_observe_block_count": max(len(candidates) - len(observe_candidates), 0),
                "legacy_gate_executed": False,
                "legacy_funnel_executed": False,
                "candidate_source_counts": cls._candidate_source_counts(candidates),
                "router_status_counts": cls._router_status_counts(candidates),
            },
            "backtest_report": backtest_report,
            "gate_3": {
                "status": "post_observe_submission_report",
                "input_count": gate_3_input,
                "passed_count": gate_3_passed,
                "failed_count": gate_3_failed,
                "pre_observe_blocking": max(len(candidates) - len(observe_candidates), 0) > 0,
            },
            "final_decision": {
                "stage": "observe_intake",
                "passed_count": len(unique),
                "legacy_gate_executed": False,
                "pre_observe_blocking": max(len(candidates) - len(observe_candidates), 0) > 0,
            },
        }

    @staticmethod
    def _mark_observe_first_candidates(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
        marked: list[dict[str, Any]] = []
        for item in list(candidates or []):
            candidate = dict(item or {})
            params = dict(candidate.get("params") or {})
            params["observe_first_intake"] = True
            incubation_budget = dict(candidate.get("incubation_budget") or {})
            incubation_budget["track"] = "observe_incubation"
            incubation_budget.setdefault("budget_tier", "micro")
            candidate.update(
                {
                    "observe_first_intake": True,
                    "pre_observe_gate_required": False,
                    "incubation_budget": incubation_budget,
                    "params": params,
                }
            )
            marked.append(candidate)
        return marked

    @staticmethod
    def _extract_backtest_report(
        quality_gate_report: dict[str, Any],
        backtest_filter: Any,
        candidates: list[dict[str, Any]],
        passed: list[dict[str, Any]],
    ) -> dict[str, Any]:
        report = (quality_gate_report.get("gate_2") or {}).get("report")
        if report:
            return dict(report)
        if hasattr(backtest_filter, "get_last_report"):
            return dict(backtest_filter.get_last_report() or {})
        return {
            "summary": {
                "input_count": len(candidates),
                "passed_count": len(passed),
                "failed_count": max(len(candidates) - len(passed), 0),
                "failed_reason_counts": {},
                "thresholds_by_type": {},
            },
            "passed": [],
            "failed": [],
        }

    @staticmethod
    def _build_report(
        candidates: list[dict[str, Any]],
        passed: list[dict[str, Any]],
        unique: list[dict[str, Any]],
        submit_result: dict[str, Any],
        quality_gate_report: dict[str, Any],
        backtest_report: dict[str, Any],
    ) -> CandidatePipelineReport:
        backtest_summary = backtest_report.get("summary") or {}
        gate_0 = dict(quality_gate_report.get("gate_0") or {})
        pre_gate = dict(quality_gate_report.get("pre_gate") or {})
        gate_1 = dict(quality_gate_report.get("gate_1") or {})
        gate_2 = dict(quality_gate_report.get("gate_2") or {})

        submitted = int(submit_result.get("submitted") or 0)
        gate_3_passed = int(
            submit_result.get("gate_3_passed")
            or submit_result.get("passed_quality_gate")
            or 0
        )
        gate_3_failed = int(
            submit_result.get("gate_3_failed")
            or max(submitted - gate_3_passed, 0)
        )
        gate_3_provisional = int(submit_result.get("gate_3_provisional_passed") or 0)

        failure_reason_counts: dict[str, int] = {}
        for item in list(submit_result.get("gate_3_failure_reason_topn") or []):
            if isinstance(item, dict) and item.get("reason"):
                failure_reason_counts[str(item["reason"])] = int(item.get("count") or 0)
        for reason, count in dict(
            backtest_summary.get("failed_reason_counts") or {}
        ).items():
            failure_reason_counts[str(reason)] = (
                failure_reason_counts.get(str(reason), 0) + int(count or 0)
            )

        return CandidatePipelineReport(
            total_spawned=len(candidates),
            gate_0_passed=int(gate_0.get("passed_count") or 0),
            gate_0_failed=int(gate_0.get("failed_count") or 0),
            pre_gate_passed=int(pre_gate.get("passed_count") or 0),
            pre_gate_failed=int(pre_gate.get("failed_count") or 0),
            gate_1_passed=int(gate_1.get("passed_count") or 0),
            gate_1_failed=int(gate_1.get("failed_count") or 0),
            gate_2_passed=int(gate_2.get("passed_count") or backtest_summary.get("passed_count") or len(passed)),
            gate_2_failed=int(gate_2.get("failed_count") or backtest_summary.get("failed_count") or max(len(candidates) - len(passed), 0)),
            after_dedup=len(unique),
            gate_3_passed=gate_3_passed,
            gate_3_failed=gate_3_failed,
            gate_3_provisional=gate_3_provisional,
            submitted=submitted,
            failure_reason_counts=failure_reason_counts,
        )


class PipelineRunResult:
    """Structured result of a CandidatePipeline.run() call."""

    def __init__(
        self,
        *,
        passed: list[dict[str, Any]],
        unique: list[dict[str, Any]],
        quality_gate_report: dict[str, Any],
        backtest_report: dict[str, Any],
        submit_result: dict[str, Any],
        deduplicator: Any,
        dedup_report: dict[str, Any],
        report: CandidatePipelineReport,
        governance_plane: dict[str, Any],
        read_only: bool,
    ) -> None:
        self.passed = passed
        self.unique = unique
        self.quality_gate_report = quality_gate_report
        self.backtest_report = backtest_report
        self.submit_result = submit_result
        self.deduplicator = deduplicator
        self.dedup_report = dedup_report
        self.report = report
        self.governance_plane = governance_plane
        self.read_only = bool(read_only)
        self.gate_artifact = dict(governance_plane.get("gate_artifact") or {})
        self.dedup_artifact = dict(governance_plane.get("dedup_artifact") or {})
        self.submission_artifact = dict(governance_plane.get("submission_artifact") or {})
        self.evidence_artifact = dict(governance_plane.get("evidence_artifact") or {})
        self.task_board: dict[str, Any] = {}

    def backtest_summary(self) -> dict[str, Any]:
        return dict(self.backtest_report.get("summary") or {})

    def deduplicator_report(self) -> dict[str, Any]:
        if self.dedup_report:
            return dict(self.dedup_report)
        if hasattr(self.deduplicator, "get_last_report"):
            return dict(self.deduplicator.get_last_report() or {})
        return {}


__all__ = [
    "CandidatePipeline",
    "PipelineRunResult",
]
