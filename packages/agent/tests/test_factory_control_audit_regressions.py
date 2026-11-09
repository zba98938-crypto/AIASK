"""Real ledger and child-process regressions from the four-factory audit."""

from __future__ import annotations

import json
from pathlib import Path
import sys

import pytest
import pytest_asyncio

from aiask_agent import factory_worker as worker
from aiask_agent.factory_dispatcher import _mark_launch_failed
from aiask_quant_core.storage.sqlite import SQLiteAdapter
from strategy_factory.api.factory_runs import validate_factory_run_transition
from strategy_factory.infrastructure.factory_schema import register_factory_schema


@pytest_asyncio.fixture
async def db(tmp_path):
    register_factory_schema()
    adapter = SQLiteAdapter(path=tmp_path / "runs.sqlite3")
    await adapter.initialize()
    try:
        yield adapter
    finally:
        await adapter.close()


@pytest.mark.asyncio
async def test_unclaimed_startup_failure_is_terminal_with_diagnostics(db):
    validate_factory_run_transition("queued", "failed")
    run = await db.request_factory_run({"factory": "strategy_factory", "idempotency_key": "startup"})
    await _mark_launch_failed(
        db, run["run_id"], command=["python", "worker.py"], started_at="2026-11-04T00:00:00Z",
        failure_kind="worker_start_failed", error="could not import worker", stderr_tail="ModuleNotFoundError",
        exit_code=1,
    )
    result = await db.get_factory_run(run["run_id"])
    assert result["status"] == "failed" and result["terminal"]
    assert result["phase"] == "worker_launch_failed"
    assert result["failure_kind"] == "worker_start_failed"
    assert result["stderr_tail"] == "ModuleNotFoundError"
    assert result["error"] == "could not import worker"
    assert result["finished_at"] and result["attempt_count"] == 0


@pytest.mark.asyncio
async def test_launch_error_does_not_overwrite_an_existing_terminal_verdict(db):
    run = await db.request_factory_run({"factory": "strategy_factory", "idempotency_key": "terminal"})
    await db.claim_factory_run(run["run_id"], owner_id="test")
    await db.transition_factory_run(run["run_id"], "partial", error="provider partial")
    await _mark_launch_failed(
        db, run["run_id"], command=["python"], started_at="2026-11-04T00:00:00Z",
        failure_kind="worker_start_failed", error="late crash", stderr_tail="late",
    )
    result = await db.get_factory_run(run["run_id"])
    assert result["status"] == "partial" and result["error"] == "provider partial"


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["failed", "partial", "blocked"])
async def test_large_result_channel_preserves_top_level_verdict(db, tmp_path, monkeypatch, status):
    script = tmp_path / "runner.py"
    script.write_text(
        "from strategy_factory.api.runner_result import write_runner_result\n"
        f"write_runner_result('strategy_factory', {{'status': {status!r}, 'error': 'evidence gap', "
        "'results': [{'status': 'completed', 'text': 'x' * 1000}] * 200})\n"
        "print('{\"status\": \"succeeded\"}')\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(worker, "get_db", lambda: db)
    monkeypatch.setattr(worker, "_runner_command", lambda *args: [sys.executable, str(script)])
    run = await db.request_factory_run({"factory": "strategy_factory", "idempotency_key": status})
    await worker._execute(run["run_id"])
    result = await db.get_factory_run(run["run_id"])
    assert result["status"] == status
    assert result["raw_status"] == status
    assert result["error"] == "evidence gap"
    assert result["terminal"]


@pytest.mark.asyncio
@pytest.mark.parametrize("output", ["", "ordinary logs", '{"candidate_id":"x"}', '{"status":"mystery"}'])
async def test_missing_or_invalid_runner_result_fails_closed(db, tmp_path, monkeypatch, output):
    script = tmp_path / "runner.py"
    script.write_text(f"print({output!r})\n")
    monkeypatch.setattr(worker, "get_db", lambda: db)
    monkeypatch.setattr(worker, "_runner_command", lambda *args: [sys.executable, str(script)])
    run = await db.request_factory_run({"factory": "strategy_factory", "idempotency_key": "missing"})
    assert await worker._execute(run["run_id"]) == 1
    result = await db.get_factory_run(run["run_id"])
    assert result["status"] == "failed"
    assert result["failure_kind"] == "runner_invalid_result"


@pytest.mark.asyncio
async def test_heartbeat_does_not_cancel_partial_output_read(db, tmp_path, monkeypatch):
    script = tmp_path / "runner.py"
    script.write_text(
        "import sys,time\nsys.stdout.write('{\"status\":');sys.stdout.flush()\n"
        "time.sleep(0.12)\nprint('\"partial\", \"error\": \"provider gap\"}')\n"
    )
    monkeypatch.setattr(worker, "get_db", lambda: db)
    monkeypatch.setattr(worker, "_runner_command", lambda *args: [sys.executable, str(script)])
    monkeypatch.setattr(worker, "WORKER_HEARTBEAT_SECONDS", 0.02)
    run = await db.request_factory_run({"factory": "strategy_factory", "idempotency_key": "slow-output"})
    assert await worker._execute(run["run_id"]) == 0
    result = await db.get_factory_run(run["run_id"])
    assert result["status"] == "partial" and result["error"] == "provider gap"


def test_stdout_parser_uses_last_standalone_object_not_largest_nested_dict():
    earlier = json.dumps({"status": "succeeded", "long": "x" * 1000})
    final = json.dumps({"status": "failed", "child": {"status": "succeeded"}}, indent=2)
    assert worker._last_json(earlier + "\nlog message\n" + final)["status"] == "failed"
    fragment = '  {"status":"succeeded"}\n ]}'
    assert worker._last_json(fragment) == {}


def test_invalid_or_wrong_scope_result_file_does_not_fall_back_to_success(tmp_path):
    path = tmp_path / "result.json"
    path.write_text('{"contract_version":"aiask.factory_result.v1","factory":"incubation","result":{"status":"succeeded"}}')
    with pytest.raises(worker.InvalidRunnerResult, match="scope mismatch"):
        worker._read_result(path, "strategy_factory", '{"status":"succeeded"}')


@pytest.mark.parametrize("raw,expected", [("partial_llm", "partial"), ("skipped", "blocked")])
def test_product_success_false_preserves_explicit_partial_or_blocked(raw, expected):
    assert worker._outcome({"status": raw, "success": False, "product_success": False}, 0)[0] == expected
    assert worker._outcome({"status": raw, "success": False}, 1)[0] == "failed"


@pytest.mark.asyncio
@pytest.mark.parametrize("factory", ["strategy_factory", "factor_mining", "incubation", "market_event_ingest"])
async def test_each_real_runner_publishes_its_result_to_the_worker(db, tmp_path, monkeypatch, factory):
    """Exercise the actual runner branch with only its business runtime stubbed."""
    script_path = Path(__file__).resolve().parents[3] / "scripts/factories" / worker.RUNNER_FILES[factory]
    body = (
        "import asyncio,importlib.util,sys\nfrom types import SimpleNamespace\n"
        f"spec=importlib.util.spec_from_file_location('test_real_runner', {str(script_path)!r})\n"
        "module=importlib.util.module_from_spec(spec);sys.modules[spec.name]=module;spec.loader.exec_module(module)\n"
        "class FakeRuntime:\n"
        " async def run_once(self,*args,**kwargs):\n"
        "  return {'status':'partial','success':False,'error':'provider unavailable','errors':['provider unavailable']}\n"
    )
    if factory == "strategy_factory":
        body += (
            "module.StrategyFactoryRunner=lambda **kwargs: FakeRuntime()\n"
            "assert asyncio.run(module._run_async(module.parse_args(['--once','--json']))) == 0\n"
        )
    elif factory == "factor_mining":
        body += (
            "module._get_factor_mining_runtime=lambda: FakeRuntime()\n"
            "asyncio.run(module.FactorMiningFactoryRunner(mode='once')._run_mining_cycle('test'))\n"
        )
    elif factory == "incubation":
        body += (
            "module._build_runtime=lambda args: FakeRuntime()\n"
            "asyncio.run(module._run_once(SimpleNamespace(dry_run=False,json=True)))\n"
        )
    else:
        body += (
            "from strategy_factory.runtime import default_bootstrap,market_event_ingest\n"
            "default_bootstrap.ensure_default_runtime_services=lambda: None\n"
            "market_event_ingest.get_market_event_ingest_runtime=lambda: FakeRuntime()\n"
            "asyncio.run(module._run_one_round(module.parse_args(['--once','--no-network']),1))\n"
        )
    shim = tmp_path / "actual_runner_branch.py"
    shim.write_text(body, encoding="utf-8")
    monkeypatch.setattr(worker, "get_db", lambda: db)
    monkeypatch.setattr(worker, "_runner_command", lambda *args: [sys.executable, str(shim)])
    run = await db.request_factory_run({"factory": factory, "idempotency_key": "real-runner"})
    assert await worker._execute(run["run_id"]) == 0
    result = await db.get_factory_run(run["run_id"])
    assert result["status"] == "partial", result
    assert result["raw_status"] == "partial"
    assert result["error"] == "provider unavailable"
