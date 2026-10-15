"""Stable ``--json`` envelope shared by every factory control-plane surface.

Before this module each runner invented its own JSON shape (or emitted none at
all), so no caller could parse factory output generically. The governed fields
below are the contract the CLI, the MCP query surface and the Hermes adapter all
agree on.

Design rules:

* ``protocol_version`` is bumped only on a breaking change to these fields.
* ``status`` is a small closed vocabulary — callers branch on it, so it must not
  grow ad-hoc values.
* Every field is always present. A caller must never have to guess whether a key
  was omitted because it was empty or because the runner forgot it.
* ``degraded_reasons`` being non-empty means "ran, but do not treat as clean".
  That is distinct from ``errors``, which means something actually failed.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal
import logging

logger = logging.getLogger(__name__)


PROTOCOL_VERSION = "1.0"

RunStatus = Literal["success", "partial", "degraded", "failed", "blocked", "skipped"]

#: Statuses a caller may treat as "the work happened and is trustworthy".
SUCCESSFUL_STATUSES: frozenset[str] = frozenset({"success"})

#: Statuses that completed but must not be reported as clean success.
INCOMPLETE_STATUSES: frozenset[str] = frozenset({"partial", "degraded", "skipped"})

#: Statuses that did not produce usable output.
FAILED_STATUSES: frozenset[str] = frozenset({"failed", "blocked"})

_ALL_STATUSES: frozenset[str] = SUCCESSFUL_STATUSES | INCOMPLETE_STATUSES | FAILED_STATUSES


def utc_now_iso() -> str:
    """Timestamp in the single format the envelope uses everywhere."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_run_id(runtime: str) -> str:
    """Correlation id for one control-plane invocation.

    Prefixed with the runtime name so an id is self-describing in logs and in
    the run ledger without needing a join.
    """
    return f"{runtime}-{uuid.uuid4().hex[:16]}"


def normalize_status(value: Any) -> RunStatus:
    """Coerce an arbitrary runner status into the closed vocabulary.

    Fails closed: anything unrecognised becomes ``failed`` rather than being
    optimistically reported as success.
    """
    text = str(value or "").strip().lower()
    if text in _ALL_STATUSES:
        return text  # type: ignore[return-value]
    aliases = {
        "ok": "success",
        "completed": "success",
        "complete": "success",
        "succeeded": "success",
        "success_observe_only": "partial",
        "success_no_submission": "partial",
        "warn": "degraded",
        "warning": "degraded",
        "error": "failed",
        "exception": "failed",
        "cancelled": "blocked",
        "canceled": "blocked",
        "timeout": "failed",
        "noop": "skipped",
        "disabled": "skipped",
    }
    return aliases.get(text, "failed")  # type: ignore[return-value]


@dataclass
class RunEnvelope:
    """One control-plane result in the governed shape.

    Construct via :meth:`start` so ``run_id`` and ``started_at`` are always set,
    then :meth:`finish` to stamp the terminal state.
    """

    runtime: str
    run_id: str
    status: RunStatus = "failed"
    started_at: str = field(default_factory=utc_now_iso)
    finished_at: str | None = None
    counts: dict[str, int] = field(default_factory=dict)
    degraded_reasons: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    evidence_refs: list[str] = field(default_factory=list)
    data: dict[str, Any] = field(default_factory=dict)
    protocol_version: str = PROTOCOL_VERSION

    @classmethod
    def start(cls, runtime: str, *, run_id: str | None = None) -> RunEnvelope:
        return cls(runtime=runtime, run_id=run_id or new_run_id(runtime))

    def add_count(self, key: str, value: Any) -> None:
        """Record a countable outcome, ignoring values that are not integers."""
        try:
            self.counts[key] = int(value)
        except (TypeError, ValueError) as _swallowed:
            logger.warning("packages/strategy-factory/src/strategy_factory/contracts/run_envelope.py:115: swallowed %s and continued: %s", type(_swallowed).__name__, _swallowed)
            return

    def degrade(self, reason: str) -> None:
        if reason and reason not in self.degraded_reasons:
            self.degraded_reasons.append(reason)

    def fail(self, error: str) -> None:
        if error and error not in self.errors:
            self.errors.append(error)

    def add_evidence(self, ref: str) -> None:
        if ref and ref not in self.evidence_refs:
            self.evidence_refs.append(ref)

    def finish(self, status: Any = None) -> RunEnvelope:
        """Stamp ``finished_at`` and resolve the terminal status.

        When no explicit status is given it is derived: errors mean ``failed``,
        degraded reasons mean ``degraded``, otherwise ``success``. An explicit
        status is still normalized, but is never silently upgraded to success
        while errors are recorded.
        """
        self.finished_at = utc_now_iso()
        if status is None:
            if self.errors:
                self.status = "failed"
            elif self.degraded_reasons:
                self.status = "degraded"
            else:
                self.status = "success"
            return self
        resolved = normalize_status(status)
        if resolved in SUCCESSFUL_STATUSES:
            # An explicit success claim never overrides recorded problems.
            if self.errors:
                resolved = "partial"
            elif self.degraded_reasons:
                resolved = "degraded"
        self.status = resolved
        return self

    @property
    def ok(self) -> bool:
        """True only for a clean success. ``partial``/``degraded`` are not ok."""
        return self.status in SUCCESSFUL_STATUSES

    def exit_code(self) -> int:
        """Process exit code: 0 clean, 1 failed, 2 completed-but-not-clean."""
        if self.status in SUCCESSFUL_STATUSES:
            return 0
        if self.status in FAILED_STATUSES:
            return 1
        return 2

    def to_dict(self) -> dict[str, Any]:
        """The wire shape. Key order is stable for readable diffs."""
        return {
            "protocol_version": self.protocol_version,
            "runtime": self.runtime,
            "run_id": self.run_id,
            "status": self.status,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "counts": dict(self.counts),
            "degraded_reasons": list(self.degraded_reasons),
            "errors": list(self.errors),
            "evidence_refs": list(self.evidence_refs),
            "data": dict(self.data),
        }


#: Fields every governed payload must carry, for use by conformance tests.
REQUIRED_ENVELOPE_FIELDS: tuple[str, ...] = (
    "protocol_version",
    "runtime",
    "run_id",
    "status",
    "started_at",
    "finished_at",
    "counts",
    "degraded_reasons",
    "errors",
    "evidence_refs",
    "data",
)


def envelope_from_runtime_result(
    runtime: str,
    result: Any,
    *,
    run_id: str | None = None,
    started_at: str | None = None,
) -> RunEnvelope:
    """Wrap a raw runtime ``run_once()`` payload in the governed envelope.

    Lifts the fields runtimes already produce (``status``, ``errors``,
    ``quality_flags``, ``run_id``) into their governed slots and keeps the
    original payload under ``data`` so nothing is lost.
    """
    is_mapping = isinstance(result, dict)
    payload = dict(result) if is_mapping else {}
    envelope = RunEnvelope.start(runtime, run_id=run_id or payload.get("run_id"))
    if started_at:
        envelope.started_at = started_at
    envelope.data = payload

    if not is_mapping:
        # A runtime that returned something other than a payload did not report a
        # result at all. Fail closed rather than deriving success from emptiness.
        envelope.fail(f"runtime_returned_non_mapping:{type(result).__name__}")
        return envelope.finish("failed")

    for error in payload.get("errors") or []:
        envelope.fail(str(error))
    for flag in payload.get("quality_flags") or []:
        envelope.degrade(str(flag))
    if payload.get("error"):
        envelope.fail(str(payload["error"]))

    totals = payload.get("totals")
    if isinstance(totals, dict):
        for key, value in totals.items():
            envelope.add_count(str(key), value)

    raw_status = payload.get("status")
    if raw_status is None and "success" in payload:
        raw_status = "success" if payload.get("success") else "failed"
    if raw_status is None and not payload:
        # An empty payload carries no evidence that anything ran.
        envelope.fail("runtime_returned_empty_payload")
        return envelope.finish("failed")
    return envelope.finish(raw_status)


__all__ = [
    "FAILED_STATUSES",
    "INCOMPLETE_STATUSES",
    "PROTOCOL_VERSION",
    "REQUIRED_ENVELOPE_FIELDS",
    "RunEnvelope",
    "RunStatus",
    "SUCCESSFUL_STATUSES",
    "envelope_from_runtime_result",
    "new_run_id",
    "normalize_status",
    "utc_now_iso",
]
