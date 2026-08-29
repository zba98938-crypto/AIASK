"""Factory worker launch must fail loudly rather than hang.

The dispatcher spawned ``python -m aiask_agent.factory_worker`` with a bare
``os.environ.copy()``. When the packages are consumed from source (the
pytest.ini ``pythonpath`` layout) the child had no import path, so it died
instantly with ModuleNotFoundError — and all three streams went to DEVNULL, so
nothing recorded it and the run stayed ``queued`` until the stale sweep noticed
minutes later. That is what made the factory e2e test time out.

These tests pin: the child environment is importable, a startup crash is
reported as a failed run with its stderr, and a healthy launch records
diagnostics without disturbing the worker-owned state machine.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from aiask_agent import factory_dispatcher
from aiask_agent.factory_dispatcher import _startup_failure, _worker_env


def test_worker_env_makes_the_package_importable() -> None:
    """The whole defect in one assertion: the child must be able to import."""
    env = _worker_env()
    assert "PYTHONPATH" in env
    completed = subprocess.run(
        [sys.executable, "-c", "import aiask_agent.factory_worker as w; print(w.__name__)"],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert "aiask_agent.factory_worker" in completed.stdout


def test_worker_env_preserves_existing_pythonpath(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PYTHONPATH", "/already/present")
    env = _worker_env()
    entries = env["PYTHONPATH"].split(os.pathsep)
    assert entries[0] == "/already/present", "caller-supplied path must keep priority"
    assert len(entries) > 1, "package roots must still be appended"


def test_worker_module_runs_as_a_subprocess() -> None:
    """`-m aiask_agent.factory_worker` must start; rc=3 means 'no such run'."""
    completed = subprocess.run(
        [sys.executable, "-m", "aiask_agent.factory_worker", "--run-id", "factory_run_missing"],
        env=_worker_env(),
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert "ModuleNotFoundError" not in completed.stderr, completed.stderr
    assert completed.returncode == 3, f"rc={completed.returncode} stderr={completed.stderr}"


def test_startup_failure_reports_a_crashing_child() -> None:
    process = subprocess.Popen(
        [sys.executable, "-c", "import sys; sys.stderr.write('kaboom\\n'); raise SystemExit(7)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    outcome = _startup_failure(process)
    assert outcome is not None, "a child that exits non-zero must be reported"
    exit_code, stderr_tail = outcome
    assert exit_code == 7
    assert "kaboom" in stderr_tail


def test_startup_failure_ignores_a_healthy_long_running_child() -> None:
    process = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    try:
        assert _startup_failure(process) is None
    finally:
        process.kill()
        process.wait()


def test_startup_failure_ignores_a_fast_clean_exit() -> None:
    """A worker that finds nothing to do exits 0 quickly; that is not a failure."""
    process = subprocess.Popen(
        [sys.executable, "-c", "raise SystemExit(0)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    assert _startup_failure(process) is None


class _FakeDb:
    """Minimal stand-in recording how the dispatcher writes to the ledger."""

    def __init__(self, run: dict[str, Any]) -> None:
        self.run = dict(run)
        self.launches: list[dict[str, Any]] = []
        self.transitions: list[dict[str, Any]] = []

    async def initialize(self) -> None:
        return None

    async def request_factory_run(self, payload: dict[str, Any]) -> dict[str, Any]:
        return dict(self.run)

    async def get_factory_run(self, run_id: str) -> dict[str, Any]:
        return dict(self.run)

    async def record_factory_worker_launch(self, run_id: str, **kwargs: Any) -> dict[str, Any]:
        self.launches.append({"run_id": run_id, **kwargs})
        return dict(self.run)

    async def transition_factory_run(self, run_id: str, target: str, **kwargs: Any) -> dict[str, Any]:
        self.transitions.append({"run_id": run_id, "target": target, **kwargs})
        self.run["status"] = target
        return dict(self.run)


@pytest.fixture()
def _stub_dispatch(monkeypatch: pytest.MonkeyPatch) -> _FakeDb:
    run = {"run_id": "factory_run_probe", "status": "queued", "terminal": False}
    db = _FakeDb(run)
    monkeypatch.setattr(factory_dispatcher, "get_db", lambda: db)
    monkeypatch.setattr(factory_dispatcher, "_verified_intent", lambda *a, **k: {"status": "executing"})
    return db


def _dispatch() -> dict[str, Any]:
    return asyncio.run(
        factory_dispatcher.dispatch_factory_run(
            {
                "factory": "strategy_factory",
                "idempotency_key": "probe",
                "_intent_id": "intent_probe",
                "_intent_db_path": "/tmp/intents.sqlite3",
            }
        )
    )


def test_startup_crash_marks_the_run_failed_instead_of_leaving_it_queued(
    monkeypatch: pytest.MonkeyPatch, _stub_dispatch: _FakeDb
) -> None:
    monkeypatch.setattr(
        factory_dispatcher, "_startup_failure", lambda process: (1, "ModuleNotFoundError: no module")
    )
    result = _dispatch()

    assert result["success"] is False
    assert result["error_code"] == "FACTORY_WORKER_START_FAILED"
    assert "ModuleNotFoundError" in result["data"]["worker_stderr_tail"]

    failed = [item for item in _stub_dispatch.transitions if item["target"] == "failed"]
    assert failed, "a startup crash must transition the run to failed, not leave it queued"
    assert failed[0]["failure_kind"] == "worker_start_failed"
    assert "ModuleNotFoundError" in failed[0]["stderr_tail"]
    assert _stub_dispatch.launches, "the attempted command must be recorded"
    assert "factory_worker" in _stub_dispatch.launches[0]["worker_command"]


def test_healthy_launch_records_diagnostics_without_a_transition(
    monkeypatch: pytest.MonkeyPatch, _stub_dispatch: _FakeDb
) -> None:
    monkeypatch.setattr(factory_dispatcher, "_startup_failure", lambda process: None)
    result = _dispatch()

    assert result["success"] is True
    assert result["data"]["worker_started"] is True
    assert isinstance(result["data"]["worker_pid"], int)

    assert _stub_dispatch.launches, "launch metadata must be persisted"
    launch = _stub_dispatch.launches[0]
    assert launch["worker_started_at"]
    assert launch["pid"] == result["data"]["worker_pid"]
    assert _stub_dispatch.transitions == [], "the worker owns the state machine"


def test_a_worker_that_already_finished_keeps_its_own_verdict(
    monkeypatch: pytest.MonkeyPatch, _stub_dispatch: _FakeDb
) -> None:
    """A more precise terminal verdict from the worker must not be overwritten."""
    _stub_dispatch.run.update({"status": "partial", "terminal": True})
    monkeypatch.setattr(factory_dispatcher, "_startup_failure", lambda process: (1, "late crash"))
    result = _dispatch()

    assert result["success"] is False
    assert _stub_dispatch.transitions == [], "must not clobber the worker's terminal status"
    assert _stub_dispatch.launches, "diagnostics are still recorded"


def test_spawn_failure_is_reported(monkeypatch: pytest.MonkeyPatch, _stub_dispatch: _FakeDb) -> None:
    def _boom(*args: Any, **kwargs: Any) -> Any:
        raise OSError("cannot fork")

    monkeypatch.setattr(factory_dispatcher.subprocess, "Popen", _boom)
    result = _dispatch()

    assert result["success"] is False
    assert result["error_code"] == "FACTORY_WORKER_SPAWN_FAILED"
    failed = [item for item in _stub_dispatch.transitions if item["target"] == "failed"]
    assert failed and failed[0]["failure_kind"] == "worker_spawn_failed"


def test_failure_kind_classifies_terminal_outcomes() -> None:
    from aiask_agent.factory_worker import _failure_kind

    assert _failure_kind("failed", 1) == "runner_nonzero_exit"
    assert _failure_kind("failed", 0) == "runner_reported_failure"
    assert _failure_kind("partial", 0) == "runner_partial"
    assert _failure_kind("blocked", 0) == "runner_blocked"
    assert _failure_kind("succeeded", 0) is None


def test_runner_command_resolution_uses_the_standalone_root() -> None:
    """_project_root must resolve to this tree, not the old nested layout."""
    from aiask_agent.factory_worker import RUNNER_FILES, _project_root

    root = _project_root()
    for filename in RUNNER_FILES.values():
        assert (root / "scripts" / "factories" / filename).is_file(), (
            f"runner not found under {root}; factory root resolution is wrong"
        )
