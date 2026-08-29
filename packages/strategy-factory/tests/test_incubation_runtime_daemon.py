"""Daemon-mode contract for the incubation runtime.

``run_incubation_factory.py --daemon`` — the mode both factory supervisors use
to launch incubation — calls ``IncubationRuntime.run_daemon()``. These tests pin
that the method exists, owns the paper-trading engine lifecycle, and keeps the
daily ``run_time`` scheduling the legacy host runner provided.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, time as dt_time

import pytest

from strategy_factory.infrastructure.runtime_services import (
    clear_runtime_services,
    configure_runtime_services,
)
from strategy_factory.runtime.incubation import (
    DEFAULT_DAEMON_RUN_TIME,
    build_incubation_runtime,
)


@pytest.fixture(autouse=True)
def _reset_runtime_services():
    clear_runtime_services()
    yield
    clear_runtime_services()


async def _noop(*_args, **_kwargs):
    return None


def _stub(method_name: str, impl):
    return type("Stub", (), {method_name: staticmethod(impl)})()


class _DaemonSupport:
    """Support object satisfying the orchestrator's full provider surface.

    Mirrors ``test_incubation_runtime_contract._Support`` and adds cycle
    counters plus a failure switch so the daemon loop can be observed.
    """

    def __init__(self, **kwargs):
        self.run_time = kwargs.get("run_time")
        self.dry_run = kwargs.get("dry_run", False)
        self.owns_paper_trading = kwargs.get("owns_paper_trading")
        self.started = 0
        self.stopped = 0
        self.cycles: list[str] = []
        self.fail_times = 0
        self._strategy_timeout_sec = 1
        self._batch_timeout_sec = 1
        self._error_count = 0
        self._run_count = 0
        self._last_run_at = None
        self._last_result = None

        async def _scan_and_accept(_db):
            return {"accepted": 0}

        async def _generate(_db, _strategy):
            return {"signals_generated": 0}

        async def _verify(_db, _strategy):
            return {}

        async def _record(_db, _strategy, _verification):
            return None

        async def _verify_pending(_db, **_kwargs):
            return {}

        async def _generate_report(_db, _all, _verifications, _pipeline, **_kwargs):
            return {"hit_rate_dashboard": {"overall": {}}}

        async def _write_feedback(_db, _report):
            return {}

        async def _evaluate_batch(_db, _incubating, _verifications):
            return {}

        async def _check_alerts(_db, run_result=None):
            del run_result
            return {}

        self._intake = _stub("scan_and_accept", _scan_and_accept)
        self._signal_generator = _stub("generate", _generate)
        self._forward_verifier = _stub("verify", _verify)
        self._metrics_recorder = _stub("record", _record)
        self._trade_prediction_verifier = _stub("verify_pending", _verify_pending)
        self._reporter = _stub("generate", _generate_report)
        self._feedback_writer = _stub("write", _write_feedback)
        self._accelerator = _stub("evaluate_batch", _evaluate_batch)
        self._alert_monitor = _stub("check", _check_alerts)

    def status(self):
        return {"dry_run": self.dry_run, "owns_paper_trading": self.owns_paper_trading}

    async def _get_db(self):
        # Called once per cycle; the failure switch simulates a broken cycle.
        self.cycles.append("daemon")
        if self.fail_times > 0:
            self.fail_times -= 1
            raise RuntimeError("cycle boom")
        return object()

    _close_db = _noop
    _run_recompile_remediation = _noop
    _heartbeat = _noop

    async def _list_incubating(self, _db):
        return []

    async def _list_paper_observation(self, _db):
        return []

    async def _list_diagnostic_observation(self, _db):
        return []

    async def _run_signal_only_paper_execution_backlog(self, _db, *, strategies=None):
        return {}

    async def _run_exit_signal_paper_execution(self, _db, *, strategies=None, as_of=None):
        return {}

    async def _run_stale_paper_position_closure(self, _db, *, strategies=None):
        return {}

    async def _run_native_execution_evidence_backfill(self, _db, *, strategies=None):
        return {}

    async def _run_execution_audit_acceptance(self, _db, *, strategies=None):
        return {"status": "pending_evidence", "blockers": []}

    async def _run_execution_audit_remediation(self, _db, *, strategies=None, acceptance_result=None):
        return {}

    async def _run_pipeline(self, _db, *, strategies=None):
        return {"count": 0, "auto_promoted": 0, "stage_counts": {}}

    # --- paper-trading engine lifecycle -------------------------------------------
    async def _start_paper_trading_daemons(self):
        self.started += 1

    async def _stop_paper_trading_daemons(self):
        self.stopped += 1


def _build(support: _DaemonSupport, **kwargs):
    configure_runtime_services(
        incubation_runtime_factory=lambda **_k: support,
        incubation_runtime_support_factory=lambda **_k: support,
    )
    return build_incubation_runtime(**kwargs)


def test_incubation_runtime_exposes_run_daemon() -> None:
    """The supervisors launch --daemon; a missing method is an AttributeError."""
    runtime = _build(_DaemonSupport())
    assert callable(getattr(runtime, "run_daemon", None)), (
        "IncubationRuntime must expose run_daemon(); "
        "run_incubation_factory.py --daemon calls it directly"
    )


@pytest.mark.asyncio
async def test_run_daemon_runs_cycles_and_owns_engine_lifecycle(monkeypatch) -> None:
    support = _DaemonSupport()
    runtime = _build(support, run_time=dt_time(18, 30))

    slept: list[float] = []

    async def _fake_sleep(seconds):
        slept.append(seconds)

    monkeypatch.setattr(asyncio, "sleep", _fake_sleep)

    result = await runtime.run_daemon(max_cycles=2)

    assert result == {"success": True, "cycles": 2, "failures": 0}
    assert support.cycles == ["daemon", "daemon"]
    # Engines start once for the daemon's lifetime, not once per cycle.
    assert support.started == 1
    assert support.stopped == 1
    assert len(slept) == 2


@pytest.mark.asyncio
async def test_run_daemon_backs_off_after_a_failed_cycle(monkeypatch) -> None:
    support = _DaemonSupport()
    support.fail_times = 1
    runtime = _build(support, run_time=dt_time(18, 30))

    slept: list[float] = []

    async def _fake_sleep(seconds):
        slept.append(seconds)

    monkeypatch.setattr(asyncio, "sleep", _fake_sleep)

    result = await runtime.run_daemon(max_cycles=1)

    assert result == {"success": False, "cycles": 1, "failures": 1}
    # One scheduling wait plus one error backoff.
    assert 300 in slept
    assert support.stopped == 1, "engines must stop even when a cycle fails"


@pytest.mark.asyncio
async def test_run_daemon_stops_engines_on_cancellation(monkeypatch) -> None:
    support = _DaemonSupport()
    runtime = _build(support, run_time=dt_time(18, 30))

    async def _cancel_sleep(_seconds):
        raise asyncio.CancelledError

    monkeypatch.setattr(asyncio, "sleep", _cancel_sleep)

    with pytest.raises(asyncio.CancelledError):
        await runtime.run_daemon()

    assert support.started == 1
    assert support.stopped == 1, "Ctrl+C must not leak paper-trading engines"


@pytest.mark.asyncio
async def test_run_daemon_reports_missing_support_instead_of_raising() -> None:
    configure_runtime_services(
        incubation_runtime_factory=lambda **_k: None,
        incubation_runtime_support_factory=lambda **_k: None,
    )
    runtime = build_incubation_runtime()

    result = await runtime.run_daemon(max_cycles=1)

    assert result["success"] is False
    assert result["error"] == "incubation_support_missing"


def test_run_time_is_honoured_and_defaults_are_stable() -> None:
    support = _DaemonSupport()
    runtime = _build(support, run_time=dt_time(9, 15))

    # 08:00 -> next 09:15 is the same day, one hour and fifteen minutes out.
    wait = runtime._seconds_until_next_run(now=datetime(2026, 8, 28, 8, 0, 0))
    assert wait == pytest.approx(75 * 60)

    # 10:00 -> already past 09:15, so roll to tomorrow.
    wait_next_day = runtime._seconds_until_next_run(now=datetime(2026, 8, 28, 10, 0, 0))
    assert wait_next_day == pytest.approx((23 * 60 + 15) * 60)

    assert DEFAULT_DAEMON_RUN_TIME == dt_time(18, 30)


def test_run_time_falls_back_to_the_support_object() -> None:
    """Hosts that already carry scheduling stay authoritative."""
    support = _DaemonSupport(run_time=dt_time(7, 45))
    support.run_time = dt_time(7, 45)
    runtime = _build(support)

    wait = runtime._seconds_until_next_run(now=datetime(2026, 8, 28, 7, 0, 0))
    assert wait == pytest.approx(45 * 60)
