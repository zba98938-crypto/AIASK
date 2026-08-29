"""Canonical incubation runtime owned by Strategy Factory.

Phase 2B complete: Orchestration logic now lives in application layer.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from datetime import datetime, time as dt_time, timedelta
from typing import Any

from ..application.incubation import IncubationOrchestrator
from ..infrastructure.runtime_services import get_incubation_runtime_support_factory
from .provider_adapters import LegacyIncubationProvider


logger = logging.getLogger(__name__)

# Wait time after a failed cycle before the daemon retries. Mirrors the legacy
# ``incubation_factory.runner.ERROR_BACKOFF_SEC`` so daemon pacing does not
# change as ownership moves into strategy-factory.
DEFAULT_ERROR_BACKOFF_SEC = 300

DEFAULT_DAEMON_RUN_TIME = dt_time(18, 30)


class IncubationRuntime:
    """Host-neutral incubation runtime with Strategy Factory-owned orchestration.

    Phase 2B: Delegates to application-layer orchestrator for cycle coordination.
    """

    def __init__(
        self,
        support: Any,
        *,
        run_time: dt_time | None = None,
        error_backoff_sec: int = DEFAULT_ERROR_BACKOFF_SEC,
    ):
        self._support = support
        provider = support
        if support is not None and not callable(getattr(support, "scan_and_accept_strategies", None)):
            provider = LegacyIncubationProvider(support)
        self._provider = provider
        self._orchestrator = IncubationOrchestrator(provider) if provider else None
        # Prefer an explicit run_time, then whatever the support object was
        # constructed with, so hosts that already carry scheduling stay authoritative.
        resolved_run_time = run_time
        if resolved_run_time is None:
            support_run_time = getattr(support, "run_time", None)
            if isinstance(support_run_time, dt_time):
                resolved_run_time = support_run_time
        self._run_time = resolved_run_time or DEFAULT_DAEMON_RUN_TIME
        self._error_backoff_sec = int(error_backoff_sec)

    def preflight(self) -> dict[str, Any]:
        return {
            "available": self._support is not None,
            "runtime_type": type(self._support).__name__ if self._support is not None else None,
            "orchestrator_ready": self._orchestrator is not None,
        }

    def status(self) -> dict[str, Any]:
        status = getattr(self._support, "status", None)
        if callable(status):
            return dict(status() or {})
        return self.preflight()

    async def start(self) -> None:
        """Start paper runtime engines (MatchingEngine + NavEngine)."""
        start_method = getattr(self._support, "_start_paper_trading_daemons", None)
        if callable(start_method):
            await start_method()

    async def stop(self) -> None:
        """Stop paper runtime engines."""
        stop_method = getattr(self._support, "_stop_paper_trading_daemons", None)
        if callable(stop_method):
            await stop_method()

    async def run_once(
        self,
        *,
        trigger: str = "scheduled",
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Execute one complete incubation cycle.

        Phase 2B: Delegates to application-layer orchestrator.
        """
        if self._orchestrator is None:
            return {
                "success": False,
                "error": "incubation_support_missing",
                "trigger": trigger,
            }

        result = await self._orchestrator.run_cycle(
            trigger=trigger,
            dry_run=dry_run,
        )
        # Preserve the established facade keys while legacy hosts migrate the
        # remaining audit/pipeline operations to the public provider protocol.
        result.setdefault("pipeline", {})
        result.setdefault("execution_audit_acceptance", {})
        settlements = getattr(self._provider, "settlements", None)
        if isinstance(settlements, dict):
            items = list(settlements.values())
            result.setdefault(
                "settlement",
                {
                    "evaluated": len(items),
                    "filled": sum(int(item.get("filled_count") or 0) for item in items),
                    "rejected": sum(int(item.get("rejected_count") or 0) for item in items),
                    "errors": 0,
                    "items": items[:50],
                },
            )
        return result

    def _seconds_until_next_run(self, *, now: datetime | None = None) -> float:
        """Seconds to wait until the next daily ``run_time`` boundary."""
        current = now or datetime.now()
        target = current.replace(
            hour=self._run_time.hour,
            minute=self._run_time.minute,
            second=0,
            microsecond=0,
        )
        if current >= target:
            target += timedelta(days=1)
        return (target - current).total_seconds()

    async def run_daemon(self, *, max_cycles: int | None = None) -> dict[str, Any]:
        """Run one incubation cycle per day at ``run_time`` until cancelled.

        Owns the paper-trading engine lifecycle for the duration of the daemon:
        engines are started before the first wait and always stopped on exit.
        ``max_cycles`` bounds the loop for tests; production passes ``None``.
        """
        if self._orchestrator is None:
            return {
                "success": False,
                "error": "incubation_support_missing",
                "cycles": 0,
            }

        cycles = 0
        failures = 0
        await self.start()
        try:
            while max_cycles is None or cycles < max_cycles:
                wait_seconds = self._seconds_until_next_run()
                logger.info(
                    "IncubationRuntime: next cycle at %02d:%02d (waiting %.0fs)",
                    self._run_time.hour,
                    self._run_time.minute,
                    wait_seconds,
                )
                await asyncio.sleep(wait_seconds)
                cycles += 1
                try:
                    await self.run_once(trigger="daemon")
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    failures += 1
                    logger.exception("IncubationRuntime: cycle failed: %s", exc)
                    logger.warning(
                        "IncubationRuntime: backing off %ds before the next cycle",
                        self._error_backoff_sec,
                    )
                    await asyncio.sleep(self._error_backoff_sec)
        finally:
            await self.stop()

        return {"success": failures == 0, "cycles": cycles, "failures": failures}


def build_incubation_runtime(
    *,
    run_time: dt_time | None = None,
    dry_run: bool = False,
    auto_apply_review: bool = True,
    owns_paper_trading: bool | None = None,
    support: Any | None = None,
) -> IncubationRuntime:
    if support is None:
        factory = get_incubation_runtime_support_factory()
        kwargs = {
            "run_time": run_time,
            "dry_run": dry_run,
            "auto_apply_review": auto_apply_review,
            "owns_paper_trading": owns_paper_trading,
        }
        signature = inspect.signature(factory)
        accepts_kwargs = any(
            parameter.kind is inspect.Parameter.VAR_KEYWORD
            for parameter in signature.parameters.values()
        )
        if not accepts_kwargs:
            kwargs = {key: value for key, value in kwargs.items() if key in signature.parameters}
        support = factory(**kwargs)
    resolved_support = support
    return IncubationRuntime(resolved_support, run_time=run_time)


def get_incubation_runtime() -> IncubationRuntime:
    return build_incubation_runtime()


def get_incubation_factory() -> IncubationRuntime:
    return get_incubation_runtime()


__all__ = [
    "IncubationRuntime",
    "build_incubation_runtime",
    "get_incubation_factory",
    "get_incubation_runtime",
]
