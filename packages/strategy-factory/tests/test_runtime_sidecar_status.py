import argparse
import asyncio
import importlib.util
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path


def test_sidecar_status_reader_marks_dead_and_stale(tmp_path, monkeypatch):
    from strategy_factory import mcp_control

    status_path = tmp_path / "signal_tracker.status.json"
    monkeypatch.setattr(mcp_control, "_sidecar_status_path", lambda: status_path)
    status_path.write_text(json.dumps({"status": "running", "pid": 2**31 - 1}), encoding="utf-8")
    assert mcp_control._read_sidecar_status()["status"] == "dead"
    assert mcp_control._read_sidecar_status()["last_reported_status"] == "running"

    status_path.write_text(json.dumps({
        "status": "running", "pid": os.getpid(), "heartbeat_period_seconds": 60,
        "updated_at": (datetime.now(timezone.utc) - timedelta(seconds=181)).isoformat(),
    }), encoding="utf-8")
    assert mcp_control._read_sidecar_status()["status"] == "stale"


def test_sidecar_status_reader_keeps_live_heartbeat_running(tmp_path, monkeypatch):
    from strategy_factory import mcp_control

    status_path = tmp_path / "signal_tracker.status.json"
    monkeypatch.setattr(mcp_control, "_sidecar_status_path", lambda: status_path)
    status_path.write_text(json.dumps({
        "status": "running", "pid": os.getpid(), "heartbeat_period_seconds": 60,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }), encoding="utf-8")
    assert mcp_control._read_sidecar_status()["status"] == "running"


def _load_signal_tracker_runner():
    root = Path(__file__).resolve().parents[3]
    path = root / "scripts" / "factories" / "run_signal_tracker.py"
    spec = importlib.util.spec_from_file_location("aiask_test_signal_tracker_runner", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_signal_tracker_daemon_publishes_start_heartbeat_and_shutdown(tmp_path, monkeypatch):
    from strategy_factory import mcp_control

    status_path = tmp_path / "signal_tracker.status.json"
    monkeypatch.setenv("AIASK_SIGNAL_TRACKER_STATUS_PATH", str(status_path))
    runner = _load_signal_tracker_runner()
    writes: list[dict] = []
    write_status = runner._write_status

    def capture_status(payload):
        writes.append(dict(payload))
        write_status(payload)

    async def interrupt_on_heartbeat(_seconds):
        raise KeyboardInterrupt

    monkeypatch.setattr(runner, "_write_status", capture_status)
    monkeypatch.setattr(runner.asyncio, "sleep", interrupt_on_heartbeat)

    asyncio.run(runner._run_daemon(argparse.Namespace(run_time="18:00", verbose=False)))

    assert [payload["status"] for payload in writes] == ["running", "running", "stopped"]
    assert writes[0]["pid"] == os.getpid()
    assert writes[1]["updated_at"]
    assert writes[2]["stopped_at"]
    assert json.loads(status_path.read_text(encoding="utf-8"))["status"] == "stopped"
    assert mcp_control._read_sidecar_status()["status"] == "stopped"


def test_signal_tracker_run_once_publishes_completion_and_summary(tmp_path, monkeypatch):
    status_path = tmp_path / "signal_tracker.status.json"
    monkeypatch.setenv("AIASK_SIGNAL_TRACKER_STATUS_PATH", str(status_path))
    runner = _load_signal_tracker_runner()

    class _Tracker:
        async def run_once(self):
            return {
                "signals_generated": 3,
                "signal_event_snapshots": 2,
                "forward_returns_computed": 1,
                "incubation_orders": 4,
                "incubation_metrics": 5,
                "risk_events": 0,
                "errors": [],
            }

    monkeypatch.setattr(runner, "_build_runtime", lambda: _Tracker())
    result = asyncio.run(runner._run_once_inner())
    payload = json.loads(status_path.read_text(encoding="utf-8"))

    assert result["signals_generated"] == 3
    assert payload["status"] == "completed"
    assert payload["pid"] == os.getpid()
    assert payload["summary"] == {
        "signals_generated": 3,
        "signal_event_snapshots": 2,
        "forward_returns_computed": 1,
        "incubation_orders": 4,
        "incubation_metrics": 5,
        "risk_events": 0,
        "errors": 0,
    }
