from __future__ import annotations

import json

import pytest

from strategy_factory import mcp_control


RUN_ROW = {
    "run_id": "factory_run_test_1",
    "status": "success_observe_only",
    "started_at": "2026-10-20T08:56:18+08:00",
    "completed_at": "2026-10-20T08:57:33+08:00",
    "elapsed_seconds": 75.0,
    "execution_mode": "stock_first_observe_primary",
    "engine_version": "strategy_factory.stock_first_observe.primary",
    "summary": '{"candidates_spawned": 29}',
    "stages": "{}",
    "artifact_refs": "[]",
    "error": None,
}


class _FakeDB:
    async def initialize(self) -> None:
        return None

    async def list_factory_runs(self, **_kwargs):
        return []

    async def get_latest_factory_run(self, _factory):
        return None

    async def get_factory_run(self, _run_id):
        return None

    async def list_strategy_factory_runs(self, limit: int = 20):
        return [RUN_ROW][:limit]

    async def get_latest_strategy_factory_run(self):
        return RUN_ROW

    async def get_strategy_factory_run(self, run_id: str):
        return RUN_ROW if run_id == RUN_ROW["run_id"] else None


@pytest.fixture(autouse=True)
def _fake_db(monkeypatch):
    monkeypatch.setattr(mcp_control, "get_db", lambda: _FakeDB())


@pytest.mark.asyncio
async def test_factory_runs_reads_strategy_run_table_when_ledger_is_empty():
    result = await mcp_control.factory_runs(factory="strategy_factory", limit=20)

    assert result["count"] == 1
    assert result["runs"][0]["run_id"] == "factory_run_test_1"
    assert result["runs"][0]["status"] == "partial"
    assert result["runs"][0]["raw_status"] == "success_observe_only"
    assert result["runs"][0]["source"] == "strategy_factory_runs"
    assert "strategy_factory_runs" in result["sources"]


@pytest.mark.asyncio
async def test_factory_status_reads_latest_strategy_run():
    result = await mcp_control.factory_status("strategy_factory")

    assert result["status"] == "partial"
    assert result["latest_run"]["run_id"] == "factory_run_test_1"
    assert result["source"] == "strategy_factory_runs"


@pytest.mark.asyncio
async def test_factory_run_detail_falls_back_to_strategy_run_table():
    found = await mcp_control.factory_run_detail("factory_run_test_1")
    missing = await mcp_control.factory_run_detail("factory_run_absent")

    assert found["found"] is True
    assert found["run"]["run_id"] == "factory_run_test_1"
    assert missing["found"] is False
    assert missing["run"] is None


@pytest.mark.asyncio
async def test_factory_status_falls_back_to_supervisor_heartbeat(monkeypatch, tmp_path):
    runtime = tmp_path / "runtime"
    log_dir = runtime / "logs" / "four_runtime_supervisor"
    log_dir.mkdir(parents=True)
    (log_dir / "execution_records.jsonl").write_text(
        json.dumps({
            "ts": "2026-10-20T09:26:00+08:00",
            "event_type": "supervisor_heartbeat",
            "factories": [
                {
                    "factory": "factor_mining_factory",
                    "status": "running",
                    "pid": 1196,
                    "uptime_seconds": 100,
                    "restart_count": 0,
                    "last_exit_code": None,
                }
            ],
        }) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("AIASK_RUNTIME_HOME", str(runtime))

    result = await mcp_control.factory_status("factor_mining")

    assert result["status"] == "running"
    assert result["source"] == "supervisor_heartbeat"
    assert result["latest_run"]["pid"] == 1196


@pytest.mark.asyncio
async def test_factory_runs_replays_supervisor_start_exit_history(monkeypatch, tmp_path):
    runtime = tmp_path / "runtime"
    log_dir = runtime / "logs" / "four_runtime_supervisor"
    log_dir.mkdir(parents=True)
    records = [
        {
            "ts": "2026-10-20T09:00:00+08:00",
            "event_type": "factory_start",
            "factory": "factor_mining_factory",
            "pid": 123,
            "restart_count": 0,
        },
        {
            "ts": "2026-10-20T09:01:00+08:00",
            "event_type": "factory_exit",
            "factory": "factor_mining_factory",
            "exit_code": 0,
            "status": "exited",
            "uptime_seconds": 60,
        },
    ]
    (log_dir / "execution_records.jsonl").write_text(
        "\n".join(json.dumps(item) for item in records) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("AIASK_RUNTIME_HOME", str(runtime))

    result = await mcp_control.factory_runs(factory="factor_mining")

    assert result["count"] == 1
    run = result["runs"][0]
    assert run["status"] == "succeeded"
    assert run["started_at"] == "2026-10-20T09:00:00+08:00"
    assert run["finished_at"] == "2026-10-20T09:01:00+08:00"
    assert run["source"] == "supervisor_execution_records"
    assert "supervisor_execution_records" in result["sources"]


@pytest.mark.asyncio
async def test_factory_runs_status_filter_uses_raw_observe_only_value():
    result = await mcp_control.factory_runs(
        factory="strategy_factory", status="success_observe_only", limit=20
    )

    assert result["count"] == 1
    assert result["runs"][0]["raw_status"] == "success_observe_only"
