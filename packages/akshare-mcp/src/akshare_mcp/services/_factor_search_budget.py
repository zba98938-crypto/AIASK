"""Cooperative deadlines shared by CPU search and its validation work.

Cancellation is checked between bounded pieces of synchronous work. The
scheduler joins the worker before returning; a blocking external call can
overrun the budget, but it can never become an abandoned background search.
"""

from __future__ import annotations

import asyncio
import time
from contextvars import ContextVar
from dataclasses import dataclass, field
from functools import wraps
from threading import Event
from typing import Any


class SearchBudgetExceeded(asyncio.CancelledError):
    """Budget stop, deliberately not swallowed by candidate error handlers."""


@dataclass
class SearchRunControl:
    max_time_sec: float
    started: float = field(default_factory=time.monotonic)
    cancelled: Event = field(default_factory=Event)
    stop_reason: str | None = None
    candidates: dict[str, Any] = field(default_factory=dict)

    def remaining(self) -> float:
        return max(0.0, self.max_time_sec - (time.monotonic() - self.started))

    def stop(self, reason: str = "search_time_budget_exhausted") -> None:
        self.stop_reason = self.stop_reason or reason
        self.cancelled.set()

    def checkpoint(self) -> None:
        if self.cancelled.is_set() or self.remaining() <= 0:
            self.stop()
            raise SearchBudgetExceeded(self.stop_reason)

    def remember(self, candidates: list[Any]) -> None:
        for candidate in candidates:
            key = str(candidate.expression_dsl or candidate.name)
            self.candidates[key] = candidate


active_search_control: ContextVar[SearchRunControl | None] = ContextVar("factor_search_control", default=None)


def search_checkpoint() -> None:
    control = active_search_control.get()
    if control is not None:
        control.checkpoint()


def remember_search_candidates(candidates: list[Any]) -> None:
    control = active_search_control.get()
    if control is not None:
        control.remember(candidates)


def bounded_search(generate):
    """Also enforce deadlines for direct engine calls outside the scheduler."""
    @wraps(generate)
    async def run(self, context, budget):
        control = active_search_control.get() or SearchRunControl(max(0.0, float(budget.max_time_sec)))
        token = active_search_control.set(control)
        try:
            control.checkpoint()
            return await generate(self, context, budget)
        except asyncio.CancelledError:
            if not control.cancelled.is_set():
                raise
            from datetime import datetime, timezone

            self._failure_count += 1
            self._last_error = control.stop_reason
            self._last_run_at = datetime.now(timezone.utc).isoformat()
            candidates = list(control.candidates.values())
            candidates.sort(key=lambda candidate: (
                bool(candidate.quick_evidence.get("passed")),
                float(candidate.quick_evidence.get("quality_score") or 0.0),
                float(candidate.fitness or 0.0),
            ), reverse=True)
            result = candidates[:budget.candidate_count]
            for candidate in result:
                candidate.generation_trace["search_budget"] = {
                    "status": "partial", "reason": control.stop_reason,
                    "quick_evaluated": bool(candidate.quick_evidence),
                }
            return result
        finally:
            active_search_control.reset(token)
    return run
