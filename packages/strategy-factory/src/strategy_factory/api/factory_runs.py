"""Public cross-factory run contracts.

The factory runtimes keep their domain-specific result statuses.  This module
defines the smaller lifecycle vocabulary used by control planes, ledgers and
MCP clients across all four runtimes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping


FACTORY_RUN_CONTRACT_VERSION = "aiask.factory_run.v1"


class FactoryName(str, Enum):
    STRATEGY = "strategy_factory"
    FACTOR_MINING = "factor_mining"
    INCUBATION = "incubation"
    MARKET_EVENT_INGEST = "market_event_ingest"


class FactoryRunState(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    PARTIAL = "partial"
    BLOCKED = "blocked"
    FAILED = "failed"
    CANCELLED = "cancelled"


TERMINAL_FACTORY_RUN_STATES = frozenset(
    {
        FactoryRunState.SUCCEEDED,
        FactoryRunState.PARTIAL,
        FactoryRunState.BLOCKED,
        FactoryRunState.FAILED,
        FactoryRunState.CANCELLED,
    }
)

FACTORY_RUN_TRANSITIONS: dict[FactoryRunState, frozenset[FactoryRunState]] = {
    FactoryRunState.QUEUED: frozenset(
        {FactoryRunState.RUNNING, FactoryRunState.BLOCKED, FactoryRunState.CANCELLED}
    ),
    FactoryRunState.RUNNING: frozenset(
        {
            FactoryRunState.QUEUED,
            FactoryRunState.SUCCEEDED,
            FactoryRunState.PARTIAL,
            FactoryRunState.BLOCKED,
            FactoryRunState.FAILED,
            FactoryRunState.CANCELLED,
        }
    ),
    FactoryRunState.SUCCEEDED: frozenset(),
    FactoryRunState.PARTIAL: frozenset(),
    FactoryRunState.BLOCKED: frozenset(),
    FactoryRunState.FAILED: frozenset(),
    FactoryRunState.CANCELLED: frozenset(),
}

_FACTORY_ALIASES = {
    "strategy": FactoryName.STRATEGY,
    "strategy_factory": FactoryName.STRATEGY,
    "factor": FactoryName.FACTOR_MINING,
    "factor_factory": FactoryName.FACTOR_MINING,
    "factor_mining": FactoryName.FACTOR_MINING,
    "factor_mining_factory": FactoryName.FACTOR_MINING,
    "incubation": FactoryName.INCUBATION,
    "incubation_factory": FactoryName.INCUBATION,
    "event": FactoryName.MARKET_EVENT_INGEST,
    "event_ingest": FactoryName.MARKET_EVENT_INGEST,
    "market_event_ingest": FactoryName.MARKET_EVENT_INGEST,
}

_STATE_ALIASES = {
    "pending": FactoryRunState.QUEUED,
    "accepted": FactoryRunState.QUEUED,
    "queued": FactoryRunState.QUEUED,
    "running": FactoryRunState.RUNNING,
    "executing": FactoryRunState.RUNNING,
    "success": FactoryRunState.SUCCEEDED,
    "succeeded": FactoryRunState.SUCCEEDED,
    "completed": FactoryRunState.SUCCEEDED,
    "success_observe_only": FactoryRunState.SUCCEEDED,
    "success_no_submission": FactoryRunState.SUCCEEDED,
    "success_no_strategy": FactoryRunState.SUCCEEDED,
    "partial": FactoryRunState.PARTIAL,
    "partial_infra": FactoryRunState.PARTIAL,
    "partial_llm": FactoryRunState.PARTIAL,
    "degraded": FactoryRunState.PARTIAL,
    "skipped": FactoryRunState.BLOCKED,
    "blocked": FactoryRunState.BLOCKED,
    "failed": FactoryRunState.FAILED,
    "error": FactoryRunState.FAILED,
    "cancelled": FactoryRunState.CANCELLED,
    "canceled": FactoryRunState.CANCELLED,
}


def normalize_factory_name(value: FactoryName | str) -> FactoryName:
    if isinstance(value, FactoryName):
        return value
    token = str(value or "").strip().lower().replace("-", "_")
    try:
        return _FACTORY_ALIASES[token]
    except KeyError as exc:
        raise ValueError(f"unsupported factory: {value}") from exc


def normalize_factory_run_state(
    value: FactoryRunState | str,
    *,
    default: FactoryRunState | None = None,
) -> FactoryRunState:
    if isinstance(value, FactoryRunState):
        return value
    token = str(value or "").strip().lower()
    resolved = _STATE_ALIASES.get(token)
    if resolved is not None:
        return resolved
    if default is not None:
        return default
    raise ValueError(f"unsupported factory run state: {value}")


def validate_factory_run_transition(
    current: FactoryRunState | str,
    target: FactoryRunState | str,
) -> tuple[FactoryRunState, FactoryRunState]:
    source = normalize_factory_run_state(current)
    destination = normalize_factory_run_state(target)
    if source == destination:
        return source, destination
    if destination not in FACTORY_RUN_TRANSITIONS[source]:
        raise ValueError(f"invalid factory run transition: {source.value} -> {destination.value}")
    return source, destination


@dataclass(slots=True)
class FactoryRunContract:
    run_id: str
    factory: FactoryName
    status: FactoryRunState
    mode: str = "once"
    request_id: str | None = None
    idempotency_key: str | None = None
    execution_owner: str = "aiask_factory_worker"
    owner_id: str | None = None
    phase: str | None = None
    requested_at: str | None = None
    started_at: str | None = None
    finished_at: str | None = None
    heartbeat_at: str | None = None
    attempt_count: int = 0
    pid: int | None = None
    exit_code: int | None = None
    params: dict[str, Any] = field(default_factory=dict)
    summary: dict[str, Any] = field(default_factory=dict)
    evidence_refs: list[dict[str, Any]] = field(default_factory=list)
    quality_flags: list[str] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)
    error: str | None = None
    raw_status: str | None = None
    created_at: str | None = None
    updated_at: str | None = None
    contract_version: str = FACTORY_RUN_CONTRACT_VERSION

    def __post_init__(self) -> None:
        self.run_id = str(self.run_id or "").strip()
        if not self.run_id:
            raise ValueError("run_id is required")
        self.factory = normalize_factory_name(self.factory)
        self.status = normalize_factory_run_state(self.status)
        self.mode = str(self.mode or "once").strip() or "once"
        self.attempt_count = max(0, int(self.attempt_count or 0))

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> "FactoryRunContract":
        data = dict(payload or {})
        return cls(
            run_id=data.get("run_id"),
            factory=data.get("factory") or data.get("factory_name"),
            status=data.get("status"),
            mode=data.get("mode") or data.get("execution_mode") or "once",
            request_id=data.get("request_id"),
            idempotency_key=data.get("idempotency_key"),
            execution_owner=data.get("execution_owner") or "aiask_factory_worker",
            owner_id=data.get("owner_id"),
            phase=data.get("phase"),
            requested_at=data.get("requested_at"),
            started_at=data.get("started_at"),
            finished_at=data.get("finished_at") or data.get("completed_at"),
            heartbeat_at=data.get("heartbeat_at"),
            attempt_count=data.get("attempt_count") or 0,
            pid=data.get("pid"),
            exit_code=data.get("exit_code"),
            params=dict(data.get("params") or {}),
            summary=dict(data.get("summary") or {}),
            evidence_refs=list(data.get("evidence_refs") or []),
            quality_flags=[str(item) for item in list(data.get("quality_flags") or [])],
            blockers=[str(item) for item in list(data.get("blockers") or [])],
            error=data.get("error"),
            raw_status=data.get("raw_status"),
            created_at=data.get("created_at"),
            updated_at=data.get("updated_at"),
            contract_version=data.get("contract_version") or FACTORY_RUN_CONTRACT_VERSION,
        )

    @property
    def terminal(self) -> bool:
        return self.status in TERMINAL_FACTORY_RUN_STATES

    def to_dict(self) -> dict[str, Any]:
        return {
            "contract_version": self.contract_version,
            "run_id": self.run_id,
            "factory": self.factory.value,
            "status": self.status.value,
            "mode": self.mode,
            "request_id": self.request_id,
            "idempotency_key": self.idempotency_key,
            "execution_owner": self.execution_owner,
            "owner_id": self.owner_id,
            "phase": self.phase,
            "requested_at": self.requested_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "heartbeat_at": self.heartbeat_at,
            "attempt_count": self.attempt_count,
            "pid": self.pid,
            "exit_code": self.exit_code,
            "params": dict(self.params),
            "summary": dict(self.summary),
            "evidence_refs": list(self.evidence_refs),
            "quality_flags": list(self.quality_flags),
            "blockers": list(self.blockers),
            "error": self.error,
            "raw_status": self.raw_status,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "terminal": self.terminal,
        }


__all__ = [
    "FACTORY_RUN_CONTRACT_VERSION",
    "FACTORY_RUN_TRANSITIONS",
    "TERMINAL_FACTORY_RUN_STATES",
    "FactoryName",
    "FactoryRunContract",
    "FactoryRunState",
    "normalize_factory_name",
    "normalize_factory_run_state",
    "validate_factory_run_transition",
]
