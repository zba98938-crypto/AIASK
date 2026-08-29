"""Explicit Strategy Factory cycle pipeline boundary."""

from __future__ import annotations

import inspect
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from .factory_execution import build_artifact_refs, build_run_artifacts
from .run_models import FactoryRunStatus, StageStatus, normalize_stage_status
from .runtime_boundary import (
    RUNTIME_BOUNDARY_CONTRACT_VERSION,
    RuntimeBoundaryReport,
    validate_strategy_factory_runtime,
)


CYCLE_PIPELINE_CONTRACT_VERSION = "strategy_factory.cycle_pipeline.v1"
CYCLE_PIPELINE_STAGE_ORDER: tuple[str, ...] = (
    "warmup",
    "collect",
    "factor_research",
    "readiness",
    "spawn",
    "autonomy",
    "quality_gate",
    "backtest",
    "deduplicate",
    "submit",
    "elimination",
)
CYCLE_PIPELINE_STAGE_ALIASES: dict[str, tuple[str, ...]] = {
    stage: (stage,) for stage in CYCLE_PIPELINE_STAGE_ORDER
}


@dataclass(slots=True)
class FactoryStageResult:
    """Typed stage marker used by the pipeline boundary."""

    name: str
    status: str = StageStatus.COMPLETED.value
    ok: bool = True
    payload: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "status": self.status,
            "ok": bool(self.ok),
            **dict(self.payload or {}),
        }


class FactoryCyclePipeline:
    """Preflight and compatibility wrapper around the current cycle body."""

    def __init__(
        self,
        runner: Any,
        *,
        legacy_run: Callable[..., Awaitable[Any]],
    ) -> None:
        self._runner = runner
        self._legacy_run = legacy_run

    async def run(self):
        report = validate_strategy_factory_runtime(
            self._runner._context.db,
            self._runner._context.runtime_adapters,
        )
        if not report.ok:
            return self._runtime_boundary_failed(report)
        if inspect.ismethod(self._legacy_run):
            outcome = await self._legacy_run()
        else:
            outcome = await self._legacy_run(self._runner)
        result = outcome.result if isinstance(outcome.result, dict) else {}
        result["cycle_pipeline"] = self._pipeline_metadata(result.get("stages") or {})
        summary = dict(result.get("summary") or {})
        summary.setdefault("cycle_pipeline_contract_version", CYCLE_PIPELINE_CONTRACT_VERSION)
        summary.setdefault("cycle_pipeline_stage_order", list(CYCLE_PIPELINE_STAGE_ORDER))
        summary.setdefault(
            "cycle_pipeline_stage_aliases",
            {key: list(value) for key, value in CYCLE_PIPELINE_STAGE_ALIASES.items()},
        )
        result["summary"] = summary
        outcome.result = result
        return outcome

    def _pipeline_metadata(self, stages: dict[str, Any] | None = None) -> dict[str, Any]:
        return {
            "contract_version": CYCLE_PIPELINE_CONTRACT_VERSION,
            "stage_order": list(CYCLE_PIPELINE_STAGE_ORDER),
            "stage_aliases": {key: list(value) for key, value in CYCLE_PIPELINE_STAGE_ALIASES.items()},
            "stage_results": [item.to_dict() for item in self._canonical_stage_results(stages or {})],
            "legacy_cycle_body_delegated": True,
        }

    def _canonical_stage_results(self, stages: dict[str, Any]) -> list[FactoryStageResult]:
        results: list[FactoryStageResult] = []
        for stage_name in CYCLE_PIPELINE_STAGE_ORDER:
            aliases = CYCLE_PIPELINE_STAGE_ALIASES.get(stage_name, (stage_name,))
            observed: list[dict[str, Any]] = [
                dict(stages.get(alias) or {})
                for alias in aliases
                if isinstance(stages.get(alias), dict)
            ]
            if not observed:
                results.append(
                    FactoryStageResult(
                        name=stage_name,
                        status=StageStatus.SKIPPED.value,
                        ok=True,
                        payload={"observed_stage_names": [], "expected_stage_names": list(aliases)},
                    )
                )
                continue
            statuses = [normalize_stage_status(item.get("status")) for item in observed]
            if StageStatus.FAILED in statuses:
                status = StageStatus.FAILED
            elif StageStatus.PARTIAL in statuses:
                status = StageStatus.PARTIAL
            elif all(item == StageStatus.SKIPPED for item in statuses):
                status = StageStatus.SKIPPED
            else:
                status = StageStatus.COMPLETED
            results.append(
                FactoryStageResult(
                    name=stage_name,
                    status=status.value,
                    ok=status != StageStatus.FAILED,
                    payload={
                        "observed_stage_names": [
                            alias
                            for alias in aliases
                            if isinstance(stages.get(alias), dict)
                        ],
                        "expected_stage_names": list(aliases),
                    },
                )
            )
        return results

    def _runtime_boundary_failed(self, report: RuntimeBoundaryReport):
        from .cycle_runner import FactoryCycleOutcome

        context = self._runner._context
        scheduler = self._runner._scheduler
        now = scheduler._now()
        elapsed = round((now - context.start).total_seconds(), 1)
        report_payload = report.to_dict()
        stage = {
            "stage": "runtime_boundary_failed",
            "status": StageStatus.FAILED.value,
            "ok": False,
            "hard_failure": True,
            "runtime_boundary_contract_version": RUNTIME_BOUNDARY_CONTRACT_VERSION,
            **report_payload,
        }
        results = {
            "run_id": context.run_id,
            "trace_id": context.trace_id,
            "started_at": context.start.isoformat(),
            "completed_at": now.isoformat(),
            "elapsed_seconds": elapsed,
            "status": FactoryRunStatus.FAILED.value,
            "execution_mode": context.execution_mode,
            "engine_version": context.engine_version,
            "summary": {
                "trace_id": context.trace_id,
                "error": "runtime_boundary_failed",
                "runtime_boundary_contract_version": RUNTIME_BOUNDARY_CONTRACT_VERSION,
                "runtime_boundary_status": report.status,
                "runtime_boundary_blocking_reason_codes": list(report.blocking_reason_codes),
                "missing_repository_methods": list(report.missing_repository_methods),
                "missing_runtime_adapters": list(report.missing_runtime_adapters),
                "cycle_pipeline_contract_version": CYCLE_PIPELINE_CONTRACT_VERSION,
                "cycle_pipeline_stage_order": list(CYCLE_PIPELINE_STAGE_ORDER),
                "elapsed_seconds": elapsed,
            },
            "stages": {"runtime_boundary_failed": stage},
            "runtime_boundary": report_payload,
            "cycle_pipeline": self._pipeline_metadata({"runtime_boundary_failed": stage}),
            "quality_gate": {},
            "gate_report": {},
            "backtest_report": {},
            "submit_result": {},
            "artifact_refs": [],
        }
        results["artifacts"] = build_run_artifacts(results)
        results["artifact_refs"] = build_artifact_refs(results.get("artifacts") or [])
        return FactoryCycleOutcome(results, [])


__all__ = [
    "CYCLE_PIPELINE_CONTRACT_VERSION",
    "CYCLE_PIPELINE_STAGE_ALIASES",
    "CYCLE_PIPELINE_STAGE_ORDER",
    "FactoryCyclePipeline",
    "FactoryStageResult",
]
