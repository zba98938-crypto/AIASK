"""Run result domain models for the strategy factory.

Provides a typed wrapper around the raw run result dict produced by
FactoryCycleRunner, enabling stable access to key fields without
relying on free-form dict indexing.

P4/P5 implementation: bridges the cycle runner output with the DTO layer.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from ..application.run_models import FactoryRunStatus, StageStatus, normalize_run_status, normalize_stage_status


@dataclass
class StageResultView:
    """Read-only view of a single stage result dict."""

    stage: str
    status: StageStatus
    ok: bool
    hard_failure: bool
    degraded: bool
    skip_reason: Optional[str]
    warning_count: int
    blocker_count: int
    persistence_failure_count: int
    payload: dict[str, Any]

    @classmethod
    def from_dict(cls, stage: str, data: dict[str, Any]) -> "StageResultView":
        d = dict(data or {})
        return cls(
            stage=stage,
            status=normalize_stage_status(d.get("status")),
            ok=bool(d.get("ok", True)),
            hard_failure=bool(d.get("hard_failure")),
            degraded=bool(d.get("degraded")),
            skip_reason=d.get("skip_reason"),
            warning_count=int(d.get("warning_count") or 0),
            blocker_count=int(d.get("blocker_count") or 0),
            persistence_failure_count=int(d.get("persistence_failure_count") or 0),
            payload=d,
        )


@dataclass
class FactoryRunResultView:
    """Typed, read-only view of a factory cycle run result.

    Wraps the raw dict produced by ``FactoryCycleRunner.run()`` so that
    callers can access key fields without fragile dict lookups.
    """

    run_id: str
    trace_id: str
    status: FactoryRunStatus
    started_at: str
    completed_at: Optional[str]
    elapsed_seconds: float
    stages: dict[str, StageResultView]
    summary: dict[str, Any]
    error: Optional[str]
    persistence_failure_count: int
    hard_failure_count: int

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "FactoryRunResultView":
        d = dict(data or {})
        raw_stages = dict(d.get("stages") or {})
        stages = {
            name: StageResultView.from_dict(name, payload)
            for name, payload in raw_stages.items()
        }
        audit = dict(d.get("_run_audit") or {})
        return cls(
            run_id=str(d.get("run_id") or ""),
            trace_id=str(d.get("trace_id") or ""),
            status=normalize_run_status(d.get("status"), default=FactoryRunStatus.FAILED),
            started_at=str(d.get("started_at") or ""),
            completed_at=d.get("completed_at"),
            elapsed_seconds=float(d.get("elapsed_seconds") or 0.0),
            stages=stages,
            summary=dict(d.get("summary") or {}),
            error=d.get("error"),
            persistence_failure_count=int(
                audit.get("persistence_failure_count") or 0
            ),
            hard_failure_count=int(
                audit.get("hard_failure_count") or 0
            ),
        )

    @property
    def succeeded(self) -> bool:
        return self.status == FactoryRunStatus.SUCCESS

    @property
    def skipped(self) -> bool:
        return self.status == FactoryRunStatus.SKIPPED

    @property
    def failed(self) -> bool:
        return self.status == FactoryRunStatus.FAILED

    @property
    def partial(self) -> bool:
        return self.status == FactoryRunStatus.PARTIAL

    def get_stage(self, name: str) -> Optional[StageResultView]:
        return self.stages.get(name)

    def failed_stages(self) -> list[str]:
        return [
            name for name, s in self.stages.items()
            if s.status == StageStatus.FAILED
        ]

    def partial_stages(self) -> list[str]:
        return [
            name for name, s in self.stages.items()
            if s.status == StageStatus.PARTIAL
        ]


__all__ = [
    "StageResultView",
    "FactoryRunResultView",
]
