"""SignalTracker intervals are serial and heartbeats retain business evidence."""

import argparse
import asyncio
import importlib.util
import json
from pathlib import Path
import sys

import pytest


def load_runner():
    path = Path(__file__).resolve().parents[3] / "scripts/factories/run_signal_tracker.py"
    spec = importlib.util.spec_from_file_location("signal_tracker_interval_runner", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.asyncio
async def test_interval_starts_immediately_waits_after_completion_and_retains_result(tmp_path, monkeypatch):
    runner = load_runner()
    status_path = tmp_path / "status.json"
    monkeypatch.setenv("AIASK_SIGNAL_TRACKER_STATUS_PATH", str(status_path))
    entered, release = asyncio.Event(), asyncio.Event()
    calls, sequence = [], []

    class Tracker:
        async def run_once(self):
            calls.append(len(calls) + 1)
            if len(calls) == 1:
                entered.set()
                await release.wait()
            sequence.append(f"finished:{len(calls)}")
            return {"forward_returns_computed": 40 + len(calls), "errors": []}

    async def fake_sleep(seconds):
        sequence.append(f"wait:{seconds}")
        payload = json.loads(status_path.read_text())
        assert payload["phase"] == "waiting"
        assert payload["last_run_status"] == "completed"
        assert payload["summary"]["forward_returns_computed"] == 40 + len(calls)
        assert payload["last_run_at"]
        if len(calls) == 2:
            raise asyncio.CancelledError

    monkeypatch.setattr(runner, "_build_runtime", Tracker)
    monkeypatch.setattr(runner.asyncio, "sleep", fake_sleep)
    task = asyncio.create_task(runner._run_daemon(argparse.Namespace(run_time="18:00", verbose=False, interval=15)))
    await entered.wait()
    assert calls == [1]
    assert not sequence  # No interval timer or next cycle while the first is blocked.
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert calls == [1, 2]
    assert sequence == ["finished:1", "wait:15", "finished:2", "wait:15"]
    payload = json.loads(status_path.read_text())
    assert payload["status"] == "stopped"
    assert payload["summary"]["forward_returns_computed"] == 42


@pytest.mark.asyncio
async def test_failed_cycle_retries_after_backoff_and_preserves_failure_during_wait(tmp_path, monkeypatch):
    runner = load_runner()
    status_path = tmp_path / "status.json"
    monkeypatch.setenv("AIASK_SIGNAL_TRACKER_STATUS_PATH", str(status_path))
    monkeypatch.setattr(runner, "ERROR_BACKOFF_SEC", 120)
    attempts, waits = [], []

    class Tracker:
        async def run_once(self):
            attempts.append(1)
            if len(attempts) == 1:
                raise RuntimeError("failed to backfill")
            return {"forward_returns_computed": 42, "errors": []}

    async def fake_sleep(seconds):
        payload = json.loads(status_path.read_text())
        waits.append((seconds, payload["phase"]))
        if payload["phase"] == "retry_backoff":
            assert len(attempts) == 1
            assert payload["last_run_status"] == "failed"
            assert payload["error"] == "RuntimeError: failed to backfill"
            assert payload["last_run_at"]
        else:
            assert payload["last_run_status"] == "completed"
            assert payload["summary"]["forward_returns_computed"] == 42
            raise asyncio.CancelledError

    monkeypatch.setattr(runner, "_build_runtime", Tracker)
    monkeypatch.setattr(runner.asyncio, "sleep", fake_sleep)
    with pytest.raises(asyncio.CancelledError):
        await runner._run_daemon(argparse.Namespace(run_time="18:00", verbose=False, interval=180))
    assert len(attempts) == 2
    assert waits == [(60.0, "retry_backoff"), (60.0, "retry_backoff"), (60.0, "waiting")]


@pytest.mark.asyncio
async def test_long_cycle_has_heartbeat_and_cancellation_cancels_business_task(tmp_path, monkeypatch):
    runner = load_runner()
    monkeypatch.setenv("AIASK_SIGNAL_TRACKER_STATUS_PATH", str(tmp_path / "status.json"))
    monkeypatch.setattr(runner, "HEARTBEAT_SECONDS", .01)
    entered, cancelled = asyncio.Event(), asyncio.Event()
    writes = []
    real_write = runner._write_status

    def capture(payload):
        writes.append(payload)
        real_write(payload)

    class Tracker:
        async def run_once(self):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

    monkeypatch.setattr(runner, "_build_runtime", Tracker)
    monkeypatch.setattr(runner, "_write_status", capture)
    task = asyncio.create_task(runner._run_daemon(argparse.Namespace(run_time="18:00", verbose=False, interval=5)))
    await entered.wait()
    await asyncio.sleep(.035)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cancelled.is_set()
    assert len([p for p in writes if p.get("phase") == "executing"]) >= 2
    assert writes[-1]["status"] == "stopped"


def test_cli_forwards_interval_without_touching_runtime_before_validation(monkeypatch):
    runner = load_runner()
    captured = []

    async def daemon(args):
        captured.append(args.interval)

    monkeypatch.setattr(runner, "_bootstrap_runtime", lambda: {})
    monkeypatch.setattr(runner, "_configure_stdio_utf8", lambda: None)
    monkeypatch.setattr(runner, "_setup_logging", lambda verbose: None)
    monkeypatch.setattr(runner, "_run_daemon", daemon)
    monkeypatch.setattr(sys, "argv", ["run_signal_tracker.py", "--daemon", "--interval", "1800"])
    assert runner.main() == 0
    assert captured == [1800]


@pytest.mark.parametrize("options", [["--interval", "-1"], ["--once", "--interval", "30"]])
def test_invalid_interval_cli_fails_before_runtime_setup(monkeypatch, options):
    runner = load_runner()
    monkeypatch.setattr(sys, "argv", ["run_signal_tracker.py", *options])

    def forbidden():
        raise AssertionError("invalid input must not bootstrap runtime")

    monkeypatch.setattr(runner, "_bootstrap_runtime", forbidden)
    with pytest.raises(SystemExit) as exc:
        runner.main()
    assert exc.value.code == 2
