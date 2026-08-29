"""MCP tools that wrap the optional-external-library adapters.

Two tools are exposed:
- experiment_tracker  — log/query experiment runs (builtin; MLflow when installed)
- data_validation     — validate datasets (builtin; Great Expectations when installed)

Both tools follow the standard ok_with_meta envelope and auto-infer backend.
"""

from __future__ import annotations

import time
from typing import Any

from .manager_protocol import fail_with_meta, ok_with_meta
from .tool_catalog import build_tool_meta


def register(mcp) -> None:

    @mcp.tool(
        title="Experiment Tracker",
        description=(
            "Log and query experiment runs, metrics, and artifacts. "
            "Uses builtin in-memory tracker by default; switches to MLflow when installed. "
            "Actions: log_run, log_metric, log_artifact, get_run, list_runs, backend."
        ),
        structured_output=True,
        meta=build_tool_meta("experiment_tracker"),
    )
    async def experiment_tracker(
        action: str,
        experiment_name: str | None = None,
        run_id: str | None = None,
        metric_key: str | None = None,
        metric_value: float | None = None,
        metric_step: int | None = None,
        artifact_key: str | None = None,
        artifact_data: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        tags: dict[str, str] | None = None,
        limit: int = 20,
    ) -> dict[str, Any]:
        """Experiment tracking adapter — log runs, metrics and artifacts."""
        started_at = time.perf_counter()
        _action = str(action or "").strip().lower()
        try:
            from ..services.adapters.experiment_tracker_adapter import get_experiment_tracker

            tracker = get_experiment_tracker()
            backend = tracker.backend_name()
            backend_requested = "mlflow" if "mlflow" in backend or backend == "builtin_fallback" else "builtin"
            backend_used = "mlflow" if backend == "mlflow" else "builtin"
            fallback_used = backend_used != backend_requested or backend.endswith("fallback")
            fallback_reason = "mlflow_not_installed" if fallback_used and backend_requested == "mlflow" else None

            if _action == "backend":
                return ok_with_meta(
                    {
                        "backend": backend,
                        "backend_requested": backend_requested,
                        "backend_used": backend_used,
                        "fallback_used": fallback_used,
                        "fallback_reason": fallback_reason,
                    },
                    tool_name="experiment_tracker",
                    action=_action,
                    started_at=started_at,
                    source_chain=[f"adapter.experiment_tracker.{backend}"],
                )

            if _action == "log_run":
                if not experiment_name:
                    return fail_with_meta(
                        "experiment_name is required for log_run",
                        tool_name="experiment_tracker",
                        action=_action,
                        started_at=started_at,
                        error_code="PARAM_ERROR",
                    )
                new_run_id = tracker.log_run(
                    str(experiment_name),
                    params=dict(params or {}),
                    tags=dict(tags or {}),
                )
                return ok_with_meta(
                    {
                        "run_id": new_run_id,
                        "experiment_name": experiment_name,
                        "backend": backend,
                        "backend_requested": backend_requested,
                        "backend_used": backend_used,
                        "fallback_used": fallback_used,
                        "fallback_reason": fallback_reason,
                    },
                    tool_name="experiment_tracker",
                    action=_action,
                    started_at=started_at,
                    source_chain=[f"adapter.experiment_tracker.{backend}"],
                    extra_meta={
                        "side_effect": {"level": "stateful", "target": "experiment_run", "confirmation_required": False},
                        "lineage": {"run_id": new_run_id},
                    },
                )

            if _action == "log_metric":
                if not run_id or metric_key is None or metric_value is None:
                    return fail_with_meta(
                        "run_id, metric_key, and metric_value are required for log_metric",
                        tool_name="experiment_tracker",
                        action=_action,
                        started_at=started_at,
                        error_code="PARAM_ERROR",
                    )
                tracker.log_metric(str(run_id), str(metric_key), float(metric_value), step=metric_step)
                return ok_with_meta(
                    {
                        "run_id": run_id,
                        "metric_key": metric_key,
                        "metric_value": metric_value,
                        "backend": backend,
                        "backend_requested": backend_requested,
                        "backend_used": backend_used,
                        "fallback_used": fallback_used,
                        "fallback_reason": fallback_reason,
                    },
                    tool_name="experiment_tracker",
                    action=_action,
                    started_at=started_at,
                    source_chain=[f"adapter.experiment_tracker.{backend}"],
                    extra_meta={"side_effect": {"level": "stateful", "target": run_id, "confirmation_required": False}},
                )

            if _action == "log_artifact":
                if not run_id or not artifact_key or not artifact_data:
                    return fail_with_meta(
                        "run_id, artifact_key, and artifact_data are required for log_artifact",
                        tool_name="experiment_tracker",
                        action=_action,
                        started_at=started_at,
                        error_code="PARAM_ERROR",
                    )
                tracker.log_artifact(str(run_id), str(artifact_key), dict(artifact_data))
                return ok_with_meta(
                    {
                        "run_id": run_id,
                        "artifact_key": artifact_key,
                        "backend": backend,
                        "backend_requested": backend_requested,
                        "backend_used": backend_used,
                        "fallback_used": fallback_used,
                        "fallback_reason": fallback_reason,
                    },
                    tool_name="experiment_tracker",
                    action=_action,
                    started_at=started_at,
                    source_chain=[f"adapter.experiment_tracker.{backend}"],
                    extra_meta={"side_effect": {"level": "stateful", "target": run_id, "confirmation_required": False}},
                )

            if _action == "get_run":
                if not run_id:
                    return fail_with_meta(
                        "run_id is required for get_run",
                        tool_name="experiment_tracker",
                        action=_action,
                        started_at=started_at,
                        error_code="PARAM_ERROR",
                    )
                run = tracker.get_run(str(run_id))
                if run is None:
                    return fail_with_meta(
                        f"run not found: {run_id}",
                        tool_name="experiment_tracker",
                        action=_action,
                        started_at=started_at,
                        error_code="NOT_FOUND",
                    )
                return ok_with_meta(
                    {
                        "run": run,
                        "backend": backend,
                        "backend_requested": backend_requested,
                        "backend_used": backend_used,
                        "fallback_used": fallback_used,
                        "fallback_reason": fallback_reason,
                    },
                    tool_name="experiment_tracker",
                    action=_action,
                    started_at=started_at,
                    source_chain=[f"adapter.experiment_tracker.{backend}"],
                    extra_meta={"lineage": {"run_id": run_id}},
                )

            if _action == "list_runs":
                runs = tracker.list_runs(experiment_name, limit=max(1, min(int(limit or 20), 200)))
                return ok_with_meta(
                    {
                        "runs": runs,
                        "count": len(runs),
                        "experiment_name": experiment_name,
                        "backend": backend,
                        "backend_requested": backend_requested,
                        "backend_used": backend_used,
                        "fallback_used": fallback_used,
                        "fallback_reason": fallback_reason,
                    },
                    tool_name="experiment_tracker",
                    action=_action,
                    started_at=started_at,
                    source_chain=[f"adapter.experiment_tracker.{backend}"],
                )

            return fail_with_meta(
                f"Unknown action: {action}. Supported: backend, log_run, log_metric, log_artifact, get_run, list_runs.",
                tool_name="experiment_tracker",
                action=_action,
                started_at=started_at,
                error_code="UNSUPPORTED_ACTION",
            )

        except Exception as exc:
            return fail_with_meta(
                str(exc),
                tool_name="experiment_tracker",
                action=_action,
                started_at=started_at,
                error_code="INTERNAL_ERROR",
                extra_meta={"degraded": True},
            )

    @mcp.tool(
        title="Data Validation",
        description=(
            "Validate a dataset against expectations: check required fields, "
            "missing-value ratios, type conformance, and minimum quality threshold. "
            "Uses builtin validator by default; switches to Great Expectations when installed. "
            "Actions: validate, backend."
        ),
        structured_output=True,
        meta=build_tool_meta("data_validation"),
    )
    async def data_validation(
        action: str = "validate",
        records: list[dict[str, Any]] | None = None,
        expectations: dict[str, Any] | None = None,
        dataset_id: str | None = None,
        minimum_quality_threshold: float = 0.95,
        checkpoint_name: str | None = None,
        validation_results: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Data validation adapter — validate datasets against expectations."""
        started_at = time.perf_counter()
        _action = str(action or "validate").strip().lower()
        try:
            from ..services.adapters.data_validation_adapter import get_data_validation_adapter
            from ..services.adapters.data_validation_adapter import ValidationResult

            adapter = get_data_validation_adapter()
            backend = adapter.backend_name()

            if _action == "backend":
                return ok_with_meta(
                    {"backend": backend},
                    tool_name="data_validation",
                    action=_action,
                    started_at=started_at,
                    source_chain=[f"adapter.data_validation.{backend}"],
                )

            if _action == "validate":
                rows = list(records or [])
                exp = dict(expectations or {})
                if not rows:
                    return fail_with_meta(
                        "records is required and must be non-empty",
                        tool_name="data_validation",
                        action=_action,
                        started_at=started_at,
                        error_code="PARAM_ERROR",
                    )
                exp.setdefault("min_quality_threshold", minimum_quality_threshold)
                result = adapter.validate_dataset(rows, exp)
                # P2-4.6.7 fix: evaluated_expectations=0 → vacuous pass(诊断报告 §4.6.7)
                # 历史问题:GE 没配 expectation suite 时 passed=true 但 evaluated=0/successful=0
                # AI 看 passed=true 以为数据通过校验,实际从未执行任何检查
                vacuous = (
                    int(result.expectations_evaluated or 0) == 0
                    and int(result.expectations_passed or 0) == 0
                )
                effective_passed = result.passed and not vacuous
                payload: dict[str, Any] = {
                    "dataset_id": dataset_id,
                    "backend": backend,
                    "passed": effective_passed,  # 修正:vacuous → False
                    "raw_passed": result.passed,
                    "vacuous_pass": vacuous,
                    "validation_status": "vacuous_no_checks_configured" if vacuous else (
                        "passed" if result.passed else "failed"
                    ),
                    "validation_id": result.validation_id,
                    "method": result.method,
                    "stats": result.stats,
                    "expectations_evaluated": result.expectations_evaluated,
                    "expectations_passed": result.expectations_passed,
                    "details": result.details,
                    "minimum_quality_threshold": minimum_quality_threshold,
                    "backend_requested": result.backend_requested,
                    "backend_used": result.backend_used,
                    "fallback_used": result.fallback_used,
                    "fallback_reason": result.fallback_reason,
                }
                degraded = not effective_passed
                return ok_with_meta(
                    payload,
                    tool_name="data_validation",
                    action=_action,
                    started_at=started_at,
                    source_chain=[f"adapter.data_validation.{backend}"],
                    extra_meta={
                        "quality": {
                            "status": (
                                "vacuous"
                                if vacuous
                                else ("good" if effective_passed else "failed")
                            ),
                            "passed": effective_passed,
                            "vacuous_pass": vacuous,
                            "validation_id": result.validation_id,
                        },
                        "side_effect": {"level": "read_only", "confirmation_required": False},
                        "lineage": {"dataset_id": dataset_id, "validation_run_id": result.validation_id},
                        "degraded": degraded,
                    },
                )

            if _action == "checkpoint":
                rows = [ValidationResult.from_dict(dict(item or {})) for item in list(validation_results or [])]
                if not checkpoint_name:
                    return fail_with_meta(
                        "checkpoint_name is required for checkpoint",
                        tool_name="data_validation",
                        action=_action,
                        started_at=started_at,
                        error_code="PARAM_ERROR",
                    )
                if not rows:
                    return fail_with_meta(
                        "validation_results is required for checkpoint",
                        tool_name="data_validation",
                        action=_action,
                        started_at=started_at,
                        error_code="PARAM_ERROR",
                    )
                checkpoint = adapter.create_checkpoint(str(checkpoint_name), rows)
                return ok_with_meta(
                    checkpoint,
                    tool_name="data_validation",
                    action=_action,
                    started_at=started_at,
                    source_chain=[f"adapter.data_validation.{backend}"],
                    extra_meta={
                        "quality": {
                            "status": "good" if checkpoint.get("all_passed") else "failed",
                            "validation_count": checkpoint.get("validation_count"),
                            "actions": checkpoint.get("actions"),
                        },
                        "side_effect": {"level": "read_only", "confirmation_required": False},
                        "lineage": {"dataset_id": dataset_id, "checkpoint_name": checkpoint_name},
                        "degraded": bool(checkpoint.get("fallback_used")),
                    },
                )

            return fail_with_meta(
                f"Unknown action: {action}. Supported: backend, validate, checkpoint.",
                tool_name="data_validation",
                action=_action,
                started_at=started_at,
                error_code="UNSUPPORTED_ACTION",
            )

        except Exception as exc:
            return fail_with_meta(
                str(exc),
                tool_name="data_validation",
                action=_action,
                started_at=started_at,
                error_code="INTERNAL_ERROR",
                extra_meta={"degraded": True},
            )
