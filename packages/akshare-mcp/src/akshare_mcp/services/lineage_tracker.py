"""Unified lineage tracking for AI workflow tools.

Provides LineageContext for automatic tracking of run ancestry,
dataset associations, and model/strategy linkage.

Usage::

    from akshare_mcp.services.lineage_tracker import LineageContext

    ctx = LineageContext.create("analyze_stock_workflow")
    child = ctx.child("fetch_kline")
    ...
    meta_lineage = ctx.to_meta()
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4


def _auto_run_id(prefix: str = "run") -> str:
    """Generate a unique run ID."""
    return f"{prefix}:{int(time.time() * 1000)}:{uuid4().hex[:8]}"


@dataclass
class LineageContext:
    """Tracks lineage relationships for a single workflow execution.

    Attributes
    ----------
    run_id:
        Unique ID for this execution run.
    parent_run_id:
        ID of the parent run (if this is a child/sub-step).
    workflow:
        Name of the workflow or tool.
    dataset_id:
        Associated dataset identifier (if applicable).
    model_id:
        Associated model identifier (if applicable).
    strategy_id:
        Associated strategy identifier (if applicable).
    factor_candidate_id:
        Associated factor candidate identifier (if applicable).
    validation_run_id:
        Associated validation run (if applicable).
    promotion_review_id:
        Associated promotion review (if applicable).
    artifact_id:
        Associated artifact identifier (if applicable).
    children:
        List of child LineageContext instances (sub-steps).
    extra:
        Additional lineage metadata.
    """

    run_id: str = ""
    parent_run_id: str | None = None
    workflow: str = ""
    dataset_id: str | None = None
    model_id: str | None = None
    strategy_id: str | None = None
    factor_candidate_id: str | None = None
    validation_run_id: str | None = None
    promotion_review_id: str | None = None
    artifact_id: str | None = None
    children: list[LineageContext] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def create(
        cls,
        workflow: str,
        *,
        parent_run_id: str | None = None,
        dataset_id: str | None = None,
        model_id: str | None = None,
        strategy_id: str | None = None,
        factor_candidate_id: str | None = None,
        artifact_id: str | None = None,
        **extra: Any,
    ) -> LineageContext:
        """Create a new lineage context for a workflow execution."""
        return cls(
            run_id=_auto_run_id(workflow),
            parent_run_id=parent_run_id,
            workflow=workflow,
            dataset_id=dataset_id,
            model_id=model_id,
            strategy_id=strategy_id,
            factor_candidate_id=factor_candidate_id,
            artifact_id=artifact_id,
            extra=dict(extra),
        )

    def child(self, step_name: str, **extra: Any) -> LineageContext:
        """Create a child lineage context for a sub-step."""
        child_ctx = LineageContext(
            run_id=_auto_run_id(step_name),
            parent_run_id=self.run_id,
            workflow=f"{self.workflow}.{step_name}",
            dataset_id=self.dataset_id,
            model_id=self.model_id,
            strategy_id=self.strategy_id,
            factor_candidate_id=self.factor_candidate_id,
            artifact_id=None,
            extra=dict(extra),
        )
        self.children.append(child_ctx)
        return child_ctx

    def set_artifact(self, artifact_id: str) -> None:
        """Record the artifact produced by this run."""
        self.artifact_id = artifact_id

    def set_dataset(self, dataset_id: str) -> None:
        """Record the dataset used/produced by this run."""
        self.dataset_id = dataset_id

    def set_model(self, model_id: str) -> None:
        """Record the model used/produced by this run."""
        self.model_id = model_id

    def set_validation(self, validation_run_id: str) -> None:
        """Record the validation run associated with this run."""
        self.validation_run_id = validation_run_id

    def to_meta(self) -> dict[str, Any]:
        """Export lineage as a dict suitable for meta.lineage."""
        result: dict[str, Any] = {
            "run_id": self.run_id,
            "workflow": self.workflow,
        }
        if self.parent_run_id:
            result["parent_run_id"] = self.parent_run_id
        if self.dataset_id:
            result["dataset_id"] = self.dataset_id
        if self.model_id:
            result["model_id"] = self.model_id
        if self.strategy_id:
            result["strategy_id"] = self.strategy_id
        if self.factor_candidate_id:
            result["factor_candidate_id"] = self.factor_candidate_id
        if self.validation_run_id:
            result["validation_run_id"] = self.validation_run_id
        if self.promotion_review_id:
            result["promotion_review_id"] = self.promotion_review_id
        if self.artifact_id:
            result["artifact_id"] = self.artifact_id
        if self.children:
            result["child_runs"] = [c.to_meta() for c in self.children]
        if self.extra:
            result.update(self.extra)
        return result
