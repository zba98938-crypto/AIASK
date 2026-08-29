from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from typing import Any


def _stable_hash(value: Any) -> str:
    try:
        raw = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    except Exception:
        raw = repr(value)
    return hashlib.sha256(raw.encode("utf-8", errors="replace")).hexdigest()[:16]


@dataclass(frozen=True)
class ToolGuardrailDecision:
    action: str
    code: str
    message: str
    count: int
    signature: str

    def to_metadata(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "code": self.code,
            "message": self.message,
            "count": self.count,
            "signature": self.signature,
        }


class ToolLoopGuardrails:
    def __init__(
        self,
        *,
        failure_warn_after: int | None = None,
        failure_halt_after: int | None = None,
        no_progress_warn_after: int | None = None,
        no_progress_halt_after: int | None = None,
    ) -> None:
        self.failure_warn_after = max(1, int(failure_warn_after or os.getenv("AIASK_TOOL_GUARDRAIL_FAILURE_WARN_AFTER", "2")))
        self.failure_halt_after = max(
            self.failure_warn_after,
            int(failure_halt_after or os.getenv("AIASK_TOOL_GUARDRAIL_FAILURE_HALT_AFTER", "3")),
        )
        self.no_progress_warn_after = max(1, int(no_progress_warn_after or os.getenv("AIASK_TOOL_GUARDRAIL_NO_PROGRESS_WARN_AFTER", "3")))
        self.no_progress_halt_after = max(
            self.no_progress_warn_after,
            int(no_progress_halt_after or os.getenv("AIASK_TOOL_GUARDRAIL_NO_PROGRESS_HALT_AFTER", "4")),
        )
        self._failure_counts: dict[str, int] = {}
        self._result_counts: dict[str, int] = {}

    def observe(self, tool_name: str, arguments: dict[str, Any], result: dict[str, Any]) -> ToolGuardrailDecision | None:
        signature = f"{tool_name}:{_stable_hash(arguments)}"
        result_hash = _stable_hash(
            {
                "success": bool(result.get("success")),
                "data": result.get("data"),
                "error": result.get("error"),
                "error_code": result.get("error_code"),
            }
        )
        progress_key = f"{signature}:{result_hash}"
        progress_count = self._result_counts.get(progress_key, 0) + 1
        self._result_counts[progress_key] = progress_count

        if result.get("success") is False:
            failure_key = f"{signature}:{result.get('error_code') or ''}:{result.get('error') or ''}"
            failure_count = self._failure_counts.get(failure_key, 0) + 1
            self._failure_counts[failure_key] = failure_count
            if failure_count >= self.failure_halt_after:
                return ToolGuardrailDecision(
                    action="halt",
                    code="repeated_tool_failure_halt",
                    message=f"{tool_name} failed {failure_count} times with the same arguments and error.",
                    count=failure_count,
                    signature=signature,
                )
            if failure_count >= self.failure_warn_after:
                return ToolGuardrailDecision(
                    action="warn",
                    code="repeated_tool_failure_warning",
                    message=f"{tool_name} is repeating the same failure; change approach before calling it again.",
                    count=failure_count,
                    signature=signature,
                )

        if progress_count >= self.no_progress_halt_after:
            return ToolGuardrailDecision(
                action="halt",
                code="repeated_no_progress_halt",
                message=f"{tool_name} returned the same result {progress_count} times for the same arguments.",
                count=progress_count,
                signature=signature,
            )
        if progress_count >= self.no_progress_warn_after:
            return ToolGuardrailDecision(
                action="warn",
                code="repeated_no_progress_warning",
                message=f"{tool_name} returned the same result repeatedly; use a different tool or revise inputs.",
                count=progress_count,
                signature=signature,
            )
        return None


def attach_guardrail_metadata(result: dict[str, Any], decision: ToolGuardrailDecision) -> dict[str, Any]:
    payload = dict(result or {})
    meta = dict(payload.get("meta") or {})
    meta["guardrail"] = decision.to_metadata()
    payload["meta"] = meta
    return payload

