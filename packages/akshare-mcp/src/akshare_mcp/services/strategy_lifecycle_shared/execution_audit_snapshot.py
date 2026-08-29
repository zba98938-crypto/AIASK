"""Execution-audit snapshot helpers and shared DTOs."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
import hashlib
from typing import Any, Optional


def _string(value: Any) -> str:
    return str(value or "").strip()


def _coerce_iso_date(value: Any) -> Optional[str]:
    if value in (None, ""):
        return None
    if isinstance(value, date):
        return value.isoformat()
    text = _string(value)
    if not text:
        return None
    if len(text) >= 10:
        return text[:10]
    return None


def _coerce_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return _string(value).lower() in {"1", "true", "yes", "y", "passed", "ready"}


def _unique_strings(values: Any) -> list[str]:
    items: list[str] = []
    for value in list(values or []):
        token = _string(value)
        if token and token not in items:
            items.append(token)
    return items


def build_execution_audit_snapshot_id(
    *,
    strategy_id: str,
    as_of: Optional[str],
    source_action: Optional[str],
    factory_run_id: Optional[str],
    correlation_id: Optional[str],
) -> str:
    digest = hashlib.sha1(
        "|".join(
            [
                _string(strategy_id) or "missing_strategy",
                _string(as_of) or "no_as_of",
                _string(source_action) or "unknown_action",
                _string(factory_run_id) or "no_factory_run",
                _string(correlation_id) or "no_correlation",
            ]
        ).encode("utf-8")
    ).hexdigest()[:16]
    return f"eas_{digest}"


@dataclass(slots=True)
class ExecutionAuditVerdict:
    status: str = "missing"
    reasons: list[str] = field(default_factory=list)
    hard_gate_passed: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": _string(self.status) or "missing",
            "reasons": _unique_strings(self.reasons),
            "hard_gate_passed": bool(self.hard_gate_passed),
        }


@dataclass(slots=True)
class ExecutionAuditSnapshot:
    strategy_id: str
    snapshot_id: str
    as_of: Optional[str] = None
    source_run_id: Optional[str] = None
    factory_run_id: Optional[str] = None
    correlation_id: Optional[str] = None
    trace_id: Optional[str] = None
    submission_lane: Optional[str] = None
    parent_task_run_id: Optional[str] = None
    source_action: Optional[str] = None
    verdict: ExecutionAuditVerdict = field(default_factory=ExecutionAuditVerdict)
    verification: dict[str, Any] = field(default_factory=dict)
    acceptance: dict[str, Any] = field(default_factory=dict)
    audit_summary: dict[str, Any] = field(default_factory=dict)
    snapshot: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: Optional[str] = None
    updated_at: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["strategy_id"] = _string(self.strategy_id)
        payload["snapshot_id"] = _string(self.snapshot_id)
        payload["as_of"] = _coerce_iso_date(self.as_of)
        payload["source_run_id"] = _string(self.source_run_id) or None
        payload["factory_run_id"] = _string(self.factory_run_id) or None
        payload["correlation_id"] = _string(self.correlation_id) or None
        payload["trace_id"] = _string(self.trace_id) or None
        payload["submission_lane"] = _string(self.submission_lane) or None
        payload["parent_task_run_id"] = _string(self.parent_task_run_id) or None
        payload["source_action"] = _string(self.source_action) or None
        payload["verdict"] = self.verdict.to_dict()
        payload["verification"] = dict(self.verification or {})
        payload["acceptance"] = dict(self.acceptance or {})
        payload["audit_summary"] = dict(self.audit_summary or {})
        payload["snapshot"] = dict(self.snapshot or {})
        payload["metadata"] = dict(self.metadata or {})
        return payload


def snapshot_verdict_payload(snapshot: Optional[dict[str, Any]]) -> dict[str, Any]:
    payload = dict(snapshot or {})
    verdict = dict(payload.get("verdict") or {})
    status = (
        _string(verdict.get("status"))
        or _string(payload.get("verdict_status"))
        or _string(payload.get("execution_audit_gate_status"))
        or "missing"
    )
    reasons = _unique_strings(
        verdict.get("reasons")
        or payload.get("verdict_reasons")
        or payload.get("execution_audit_gate_reasons")
        or dict(payload.get("audit_summary") or {}).get("execution_audit_gate_reasons")
        or []
    )
    hard_gate_passed = _coerce_bool(
        verdict.get("hard_gate_passed")
        if isinstance(verdict, dict)
        else None,
        default=_coerce_bool(
            payload.get("execution_hard_gate_passed"),
            default=_coerce_bool(
                dict(payload.get("audit_summary") or {}).get("audit_ready_for_hard_gate"),
                default=False,
            ),
        ),
    )
    return {
        "status": status,
        "reasons": reasons,
        "hard_gate_passed": hard_gate_passed,
    }


def _trim_audit_snapshot(snapshot: Optional[dict[str, Any]]) -> dict[str, Any]:
    """PR-S1: 裁剪传入的 cycle 全量 snapshot，只保留审计相关的轻量字段。

    原 ExecutionAuditSnapshot.snapshot 字段会把整个工厂 cycle snapshot
    （包含 stages / quality_gate / backtest_report / candidate 等大对象，
    单条 ~125 MB）原样存入 strategy_execution_audit_snapshots.snapshot 列。
    本函数只保留审计判定真正需要的字段，预期单条 ≤ 50 KB。

    审计相关字段已通过 verification / acceptance / verdict / audit_summary
    四个独立 DTO 字段单独承载；这里的 snapshot 字段只保留 trace 标识与
    顶层 verdict 摘要供溯源使用。
    """
    snap = dict(snapshot or {})
    if not snap:
        return {}
    return {
        # trace / 标识
        "snapshot_id": snap.get("snapshot_id") or snap.get("trace_id"),
        "as_of": snap.get("as_of") or snap.get("date"),
        "factory_run_id": snap.get("factory_run_id"),
        "correlation_id": snap.get("correlation_id"),
        "trace_id": snap.get("trace_id"),
        # 审计判定结果（轻量 scalar）
        "execution_audit_gate_status": snap.get("execution_audit_gate_status"),
        "execution_audit_gate_reasons": list(snap.get("execution_audit_gate_reasons") or [])[:20],
        "execution_hard_gate_passed": snap.get("execution_hard_gate_passed"),
        "audit_ready_for_hard_gate": snap.get("audit_ready_for_hard_gate"),
        # 顶层 verdict 摘要
        "verdict": dict(snap.get("verdict") or {}),
    }


def build_execution_audit_snapshot_payload(
    *,
    strategy_id: str,
    quality_gate: Optional[dict[str, Any]] = None,
    audit_summary: Optional[dict[str, Any]] = None,
    verification: Optional[dict[str, Any]] = None,
    acceptance: Optional[dict[str, Any]] = None,
    snapshot: Optional[dict[str, Any]] = None,
    verdict_status: Optional[str] = None,
    verdict_reasons: Optional[list[str]] = None,
    execution_hard_gate_passed: Optional[bool] = None,
    as_of: Any = None,
    source_run_id: Optional[str] = None,
    factory_run_id: Optional[str] = None,
    correlation_id: Optional[str] = None,
    trace_id: Optional[str] = None,
    submission_lane: Optional[str] = None,
    parent_task_run_id: Optional[str] = None,
    source_action: Optional[str] = None,
    metadata: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    gate = dict(quality_gate or {})
    audit = dict(audit_summary or {})
    verification_payload = dict(verification or {})
    acceptance_payload = dict(acceptance or {})
    snapshot_payload = _trim_audit_snapshot(snapshot)

    resolved_status = (
        _string(verdict_status)
        or _string(gate.get("execution_audit_gate_status"))
        or _string(snapshot_payload.get("execution_audit_gate_status"))
        or _string(audit.get("execution_audit_gate_status"))
        or "missing"
    )
    resolved_reasons = _unique_strings(
        verdict_reasons
        or gate.get("execution_audit_gate_reasons")
        or snapshot_payload.get("execution_audit_gate_reasons")
        or audit.get("execution_audit_gate_reasons")
        or []
    )
    resolved_hard_gate = (
        _coerce_bool(execution_hard_gate_passed)
        if execution_hard_gate_passed is not None
        else _coerce_bool(
            gate.get("execution_hard_gate_passed"),
            default=_coerce_bool(
                snapshot_payload.get("execution_hard_gate_passed"),
                default=_coerce_bool(audit.get("audit_ready_for_hard_gate"), default=False),
            ),
        )
    )
    as_of_value = _coerce_iso_date(
        as_of
        or snapshot_payload.get("date")
        or snapshot_payload.get("as_of")
        or acceptance_payload.get("as_of")
        or verification_payload.get("as_of")
    )
    source_action_value = _string(source_action) or "execution_audit_snapshot"
    snapshot_id = build_execution_audit_snapshot_id(
        strategy_id=_string(strategy_id),
        as_of=as_of_value,
        source_action=source_action_value,
        factory_run_id=_string(factory_run_id) or None,
        correlation_id=_string(correlation_id) or None,
    )
    dto = ExecutionAuditSnapshot(
        strategy_id=_string(strategy_id),
        snapshot_id=snapshot_id,
        as_of=as_of_value,
        source_run_id=_string(source_run_id) or None,
        factory_run_id=_string(factory_run_id) or None,
        correlation_id=_string(correlation_id) or None,
        trace_id=_string(trace_id) or None,
        submission_lane=_string(submission_lane) or None,
        parent_task_run_id=_string(parent_task_run_id) or None,
        source_action=source_action_value,
        verdict=ExecutionAuditVerdict(
            status=resolved_status,
            reasons=resolved_reasons,
            hard_gate_passed=resolved_hard_gate,
        ),
        verification=verification_payload,
        acceptance=acceptance_payload,
        audit_summary={
            **audit,
            "execution_audit_gate_status": resolved_status,
            "execution_audit_gate_reasons": resolved_reasons,
            "audit_ready_for_hard_gate": resolved_hard_gate,
        },
        snapshot=snapshot_payload,
        metadata=dict(metadata or {}),
    )
    return dto.to_dict()


def with_execution_audit_snapshot_metadata(
    payload: Optional[dict[str, Any]],
    *,
    snapshot: Optional[dict[str, Any]],
) -> dict[str, Any]:
    result = dict(payload or {})
    snapshot_payload = dict(snapshot or {})
    if not snapshot_payload:
        return result
    verdict = snapshot_verdict_payload(snapshot_payload)
    result.setdefault("execution_audit_snapshot_id", snapshot_payload.get("snapshot_id"))
    result.setdefault("execution_audit_gate_status", verdict.get("status"))
    result.setdefault("execution_audit_gate_reasons", verdict.get("reasons"))
    result.setdefault("execution_hard_gate_passed", verdict.get("hard_gate_passed"))
    result.setdefault("execution_audit_as_of", snapshot_payload.get("as_of"))
    result.setdefault("correlation_id", snapshot_payload.get("correlation_id"))
    result.setdefault("factory_run_id", snapshot_payload.get("factory_run_id"))
    return result


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
