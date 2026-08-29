"""Normalized lifecycle tracking for strategy autonomy cycles."""

from __future__ import annotations

from collections import Counter
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any, Optional

AUTONOMY_PHASE_ORDER = (
    "prepared",
    "generating",
    "reviewing",
    "recording",
    "submitting",
    "completed",
)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _utc_iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def _error_payload(error: Any) -> dict[str, Optional[str]]:
    if error is None:
        return {"type": None, "message": None}
    if isinstance(error, BaseException):
        return {"type": error.__class__.__name__, "message": str(error)}
    return {"type": "Error", "message": str(error)}


class AutonomyLifecycleTracker:
    """Tracks a stable autonomy lifecycle with observable phases."""

    def __init__(
        self,
        *,
        scope: str = "strategy_ai_cycle",
        auto_submit: bool = False,
        phase_order: tuple[str, ...] = AUTONOMY_PHASE_ORDER,
    ):
        self.scope = str(scope or "strategy_ai_cycle")
        self.auto_submit = bool(auto_submit)
        self.phase_order = tuple(phase_order or AUTONOMY_PHASE_ORDER)
        self.started_at = _utc_now()
        self.completed_at: Optional[datetime] = None
        self.state = "pending"
        self.current_phase: Optional[str] = None
        self.failed_phase: Optional[str] = None
        self.terminal_phase: Optional[str] = None
        self._events: list[dict[str, Any]] = []
        self._phases: dict[str, dict[str, Any]] = {
            phase: {
                "name": phase,
                "order": idx + 1,
                "status": "pending",
                "started_at": None,
                "completed_at": None,
                "duration_ms": None,
                "metrics": {},
                "detail": {},
                "reason": None,
                "error": None,
            }
            for idx, phase in enumerate(self.phase_order)
        }

    def _phase(self, phase: str) -> dict[str, Any]:
        name = str(phase or "").strip().lower()
        if name not in self._phases:
            raise ValueError(f"unknown autonomy phase: {phase}")
        return self._phases[name]

    def _append_event(
        self,
        *,
        phase: str,
        status: str,
        occurred_at: datetime,
        metrics: Optional[dict[str, Any]] = None,
        detail: Optional[dict[str, Any]] = None,
        reason: Optional[str] = None,
        error: Any = None,
    ) -> None:
        payload = {
            "phase": phase,
            "status": status,
            "timestamp": _utc_iso(occurred_at),
        }
        if metrics:
            payload["metrics"] = dict(metrics)
        if detail:
            payload["detail"] = dict(detail)
        if reason:
            payload["reason"] = reason
        error_payload = _error_payload(error)
        if error_payload["type"] or error_payload["message"]:
            payload["error"] = error_payload
        self._events.append(payload)

    @staticmethod
    def _duration_ms(started_at: Optional[str], completed_at: Optional[str]) -> Optional[float]:
        if not started_at or not completed_at:
            return None
        try:
            start = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
            end = datetime.fromisoformat(completed_at.replace("Z", "+00:00"))
        except Exception:
            return None
        return round((end - start).total_seconds() * 1000, 3)

    def enter_phase(self, phase: str, *, detail: Optional[dict[str, Any]] = None) -> None:
        item = self._phase(phase)
        now = _utc_now()
        if item["started_at"] is None:
            item["started_at"] = _utc_iso(now)
        if detail:
            item["detail"] = {**dict(item.get("detail") or {}), **dict(detail)}
        item["status"] = "running"
        self.current_phase = item["name"]
        self.state = "running"
        self._append_event(phase=item["name"], status="running", occurred_at=now, detail=detail)

    def complete_phase(
        self,
        phase: Optional[str] = None,
        *,
        metrics: Optional[dict[str, Any]] = None,
        detail: Optional[dict[str, Any]] = None,
    ) -> None:
        name = str(phase or self.current_phase or "").strip().lower()
        item = self._phase(name)
        now = _utc_now()
        if item["started_at"] is None:
            item["started_at"] = _utc_iso(now)
        if metrics:
            item["metrics"] = {**dict(item.get("metrics") or {}), **dict(metrics)}
        if detail:
            item["detail"] = {**dict(item.get("detail") or {}), **dict(detail)}
        item["status"] = "completed"
        item["completed_at"] = _utc_iso(now)
        item["duration_ms"] = self._duration_ms(item.get("started_at"), item.get("completed_at"))
        self.current_phase = item["name"]
        if item["name"] == "completed":
            self.state = "completed"
            self.terminal_phase = "completed"
            self.completed_at = now
        else:
            self.state = "running"
        self._append_event(
            phase=item["name"],
            status="completed",
            occurred_at=now,
            metrics=metrics,
            detail=detail,
        )

    def skip_phase(
        self,
        phase: str,
        *,
        reason: str,
        metrics: Optional[dict[str, Any]] = None,
        detail: Optional[dict[str, Any]] = None,
    ) -> None:
        item = self._phase(phase)
        now = _utc_now()
        if item["started_at"] is None:
            item["started_at"] = _utc_iso(now)
        if metrics:
            item["metrics"] = {**dict(item.get("metrics") or {}), **dict(metrics)}
        if detail:
            item["detail"] = {**dict(item.get("detail") or {}), **dict(detail)}
        item["status"] = "skipped"
        item["reason"] = reason
        item["completed_at"] = _utc_iso(now)
        item["duration_ms"] = self._duration_ms(item.get("started_at"), item.get("completed_at"))
        self._append_event(
            phase=item["name"],
            status="skipped",
            occurred_at=now,
            metrics=metrics,
            detail=detail,
            reason=reason,
        )

    def fail_phase(
        self,
        phase: Optional[str] = None,
        *,
        error: Any,
        metrics: Optional[dict[str, Any]] = None,
        detail: Optional[dict[str, Any]] = None,
    ) -> None:
        name = str(phase or self.current_phase or "prepared").strip().lower()
        item = self._phase(name)
        now = _utc_now()
        if item["started_at"] is None:
            item["started_at"] = _utc_iso(now)
        if metrics:
            item["metrics"] = {**dict(item.get("metrics") or {}), **dict(metrics)}
        if detail:
            item["detail"] = {**dict(item.get("detail") or {}), **dict(detail)}
        item["status"] = "failed"
        item["error"] = _error_payload(error)
        item["completed_at"] = _utc_iso(now)
        item["duration_ms"] = self._duration_ms(item.get("started_at"), item.get("completed_at"))
        self.state = "failed"
        self.current_phase = item["name"]
        self.failed_phase = item["name"]
        self.terminal_phase = "failed"
        self.completed_at = now
        self._append_event(
            phase=item["name"],
            status="failed",
            occurred_at=now,
            metrics=metrics,
            detail=detail,
            error=error,
        )

    def snapshot(self) -> dict[str, Any]:
        phases = [deepcopy(self._phases[phase]) for phase in self.phase_order]
        status_counts = Counter(str(item.get("status") or "pending") for item in phases)
        return {
            "scope": self.scope,
            "auto_submit": self.auto_submit,
            "state": self.state,
            "current_phase": self.current_phase,
            "failed_phase": self.failed_phase,
            "terminal_phase": self.terminal_phase,
            "phase_order": list(self.phase_order),
            "phases": phases,
            "phase_status_counts": dict(status_counts),
            "completed_phase_count": int(status_counts.get("completed", 0)),
            "event_count": len(self._events),
            "events": list(self._events),
            "started_at": _utc_iso(self.started_at),
            "completed_at": _utc_iso(self.completed_at) if self.completed_at else None,
        }


def summarize_autonomy_lifecycle(lifecycle: Optional[dict[str, Any]]) -> dict[str, Any]:
    payload = dict(lifecycle or {})
    return {
        "state": payload.get("state"),
        "current_phase": payload.get("current_phase"),
        "failed_phase": payload.get("failed_phase"),
        "terminal_phase": payload.get("terminal_phase"),
        "phase_status_counts": dict(payload.get("phase_status_counts") or {}),
        "completed_phase_count": int(payload.get("completed_phase_count") or 0),
        "event_count": int(payload.get("event_count") or len(payload.get("events") or [])),
        "phase_order": list(payload.get("phase_order") or AUTONOMY_PHASE_ORDER),
    }
