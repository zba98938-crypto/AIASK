"""A phase may commit work before timing out; no result is not a zero count."""

import asyncio
import importlib.util
import json
from pathlib import Path
import sqlite3

import pytest

from strategy_factory.application.signal_tracker.orchestrator import SignalTrackerOrchestrator


class Provider:
    def __init__(self, database, outcome):
        self.database = database
        self.outcome = outcome
        self.saved_task = None

    async def get_db(self):
        return self

    async def save_strategy_task_run(self, payload):
        return {"id": 1}

    async def update_strategy_task_run(self, task_id, **payload):
        self.saved_task = payload

    def get_default_universe(self):
        return []

    def phase_timeout_seconds(self, phase_name):
        return 0.02

    async def load_executable_strategies(self, *args, **kwargs):
        return [{"id": "synthetic", "status": "submitted"}]

    async def generate_signals(self, *args, **kwargs):
        return {"signals_generated": 7}

    async def create_signal_event_snapshot(self, *args, **kwargs):
        return {"snapshot_created": True}

    async def _commit_then_finish(self, phase, count, response):
        with sqlite3.connect(self.database) as conn:
            conn.execute("INSERT INTO committed_work VALUES (?, ?)", (phase, count))
        if self.outcome == "timeout":
            await asyncio.Event().wait()
        if self.outcome == "error":
            raise RuntimeError("failure after a committed partial batch")
        return response

    async def backfill_forward_returns(self, *args, **kwargs):
        return await self._commit_then_finish("forward", 6, {"computed": 6})

    async def run_lifecycle_scan(self, *args, **kwargs):
        return await self._commit_then_finish("lifecycle", 2, {"transitions": [{"id": "a"}, {"id": "b"}]})

    async def run_runtime_risk_scan(self, *args, **kwargs):
        return {"events_detected": 3, "actions_taken": 1}

    async def reconcile_vector_registry(self, *args, **kwargs):
        return {"updates": 4}

    async def snapshot_domain_projections(self, *args, **kwargs):
        return {"snapshots_created": 5}


@pytest.mark.asyncio
async def test_lifecycle_partial_coverage_does_not_report_completed_cycle(tmp_path):
    database = tmp_path / "partial-lifecycle.sqlite3"
    with sqlite3.connect(database) as conn:
        conn.execute("CREATE TABLE committed_work (phase TEXT, count INTEGER)")
    provider = Provider(database, "completed")
    async def partial(*args):
        return {"status": "partial", "reason": "lifecycle_scan_budget_exhausted",
                "remaining": 235, "unresolved": 1, "coverage_complete": False,
                "transitions": None, "transitions_reason": "lifecycle_scan_partial_writes_unknown"}
    provider.run_lifecycle_scan = partial
    result = await SignalTrackerOrchestrator(provider).run_cycle()
    assert provider.saved_task["status"] == "partial"
    assert result["transitions"] is None
    assert result["transitions_reason"] == "lifecycle_scan_partial_writes_unknown"
    phase = result["phase_results"]["phase_g_lifecycle_scan"]
    assert phase["status"] == "partial"
    assert phase["payload"]["remaining"] == 235
    assert phase["delta"]["transitions"] is None
    assert "lifecycle_scan_budget_exhausted" in result["errors"]


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["timeout", "error", "completed"])
async def test_partial_commits_do_not_become_zero_counts(tmp_path, monkeypatch, outcome):
    database = tmp_path / "partial.sqlite3"
    with sqlite3.connect(database) as conn:
        conn.execute("CREATE TABLE committed_work (phase TEXT, count INTEGER)")
    provider = Provider(database, outcome)
    result = await SignalTrackerOrchestrator(provider).run_cycle()
    with sqlite3.connect(database) as conn:
        committed = dict(conn.execute("SELECT phase,count FROM committed_work"))
    assert committed == {"forward": 6, "lifecycle": 2}
    for metric, phase, actual in [
        ("forward_returns_computed", "phase_c_forward_returns", 6),
        ("transitions", "phase_g_lifecycle_scan", 2),
    ]:
        if outcome == "completed":
            assert result[metric] == actual
            assert result[f"{metric}_reason"] is None
        else:
            assert result[metric] is None
            assert result[f"{metric}_reason"] == f"{phase}_{outcome}"
            assert result["phase_results"][phase]["delta"][metric] is None
        assert provider.saved_task["result"][metric] == result[metric]
    assert result["signals_generated"] == 7
    assert result["signal_event_snapshots"] == 1
    assert result["risk_events"] == 3
    assert result["vector_registry_updates"] == 4
    assert result["projection_snapshots"] == 5
    assert provider.saved_task["status"] == ("completed" if outcome == "completed" else "partial")

    path = Path(__file__).resolve().parents[3] / "scripts/factories/run_signal_tracker.py"
    spec = importlib.util.spec_from_file_location("partial_count_runner", path)
    runner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runner)
    status_path = tmp_path / "status.json"
    monkeypatch.setenv("AIASK_SIGNAL_TRACKER_STATUS_PATH", str(status_path))

    class Runtime:
        async def run_once(self):
            return result

    monkeypatch.setattr(runner, "_build_runtime", Runtime)
    await runner._run_once_inner()
    status = json.loads(status_path.read_text())
    for metric in ("forward_returns_computed", "transitions"):
        assert status["summary"][metric] == result[metric]
        assert status["summary"][f"{metric}_reason"] == result[f"{metric}_reason"]


@pytest.mark.asyncio
async def test_completed_zero_remains_measured_zero(tmp_path):
    provider = Provider(tmp_path / "unused.sqlite3", "completed")

    async def no_forward(*args, **kwargs):
        return {"computed": 0}

    async def no_transition(*args, **kwargs):
        return {"transitions": []}

    provider.backfill_forward_returns = no_forward
    provider.run_lifecycle_scan = no_transition
    result = await SignalTrackerOrchestrator(provider).run_cycle()
    assert result["forward_returns_computed"] == result["transitions"] == 0
    assert result["forward_returns_computed_reason"] is None
    assert result["transitions_reason"] is None
    assert not result["errors"]
