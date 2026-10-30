from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from aiask_agent.adapters import strategy_factory as strategy_factory_adapter
from aiask_agent.factory_dispatcher import FactoryDispatchRejected, dispatch_factory_run
from aiask_agent.intents import ActionIntentStore, IntentExecutor
from aiask_quant_core.storage import SQLiteAdapter, close_db


# tests/ -> agent/ -> packages/ -> <aiask root>. This tree used to be nested at
# <repo>/integrations/aiask, and the old parents[5] + "integrations/aiask" pair
# resolved to a directory that no longer exists now that it is standalone, so the
# subprocess got an unusable PYTHONPATH.
AIASK_ROOT = Path(__file__).resolve().parents[3]
ROOT = AIASK_ROOT
SOURCE_PATHS = [
    AIASK_ROOT / "packages" / "agent" / "src",
    AIASK_ROOT / "packages" / "aiask-quant-core" / "src",
    AIASK_ROOT / "packages" / "strategy-factory" / "src",
    AIASK_ROOT / "packages" / "akshare-mcp" / "src",
]


def _env(tmp_path: Path) -> dict[str, str]:
    runtime_home = tmp_path / "runtime"
    hermes_home = tmp_path / "hermes"
    env = os.environ.copy()
    env.update(
        {
            "HERMES_HOME": str(hermes_home),
            "AIASK_RUNTIME_HOME": str(runtime_home),
            "AIASK_PROJECT_ROOT": str(AIASK_ROOT),
            "AIASK_SQLITE_PATH": str(runtime_home / "data" / "factory.sqlite3"),
            "AKSHARE_MCP_SQLITE_PATH": str(runtime_home / "data" / "factory.sqlite3"),
            "AIASK_AGENT_HOME": str(runtime_home / "agent"),
            "AIASK_AGENT_INTENT_DB": str(runtime_home / "agent" / "intents.sqlite3"),
            "AIASK_FACTORY_TEST_MODE": "1",
            "AIASK_FACTORY_STALE_AFTER_SECONDS": "30",
            "PYTHONPATH": os.pathsep.join([*(str(path) for path in SOURCE_PATHS), env.get("PYTHONPATH", "")]),
        }
    )
    return env


async def _wait_run(db: SQLiteAdapter, run_id: str, *, timeout: float = 15) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        row = await db.get_factory_run(run_id)
        if row and row.get("terminal"):
            return row
        await asyncio.sleep(0.05)
    raise AssertionError(f"factory run did not finish: {run_id}")


async def _mcp_probe(env: dict[str, str], run_id: str) -> tuple[list[str], dict, dict]:
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "strategy_factory.mcp_server"],
        env=env,
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            readiness_result = await session.call_tool("factory_readiness_read", {})
            detail_result = await session.call_tool("factory_run_read", {"run_id": run_id})
            resource = await session.read_resource("resource://factories/readiness")
            run_resource = await session.read_resource(f"resource://factory-runs/{run_id}")
            # MCP 1.x used camelCase; MCP 2.x uses the Pydantic snake_case
            # spelling. Keep the contract test valid across both wire clients.
            readiness_payload = getattr(readiness_result, "structured_content", None)
            if readiness_payload is None:
                readiness_payload = getattr(readiness_result, "structuredContent", None)
            readiness = readiness_payload or json.loads(readiness_result.content[0].text)
            detail_payload = getattr(detail_result, "structured_content", None)
            if detail_payload is None:
                detail_payload = getattr(detail_result, "structuredContent", None)
            detail = detail_payload or json.loads(detail_result.content[0].text)
            resource_payload = json.loads(resource.contents[0].text)
            run_resource_payload = json.loads(run_resource.contents[0].text)
            assert resource_payload["read_only"] is True
            assert run_resource_payload["run"]["status"] == "partial"
            return [item.name for item in tools.tools], readiness, detail


def test_factory_control_real_stdio_sqlite_subprocess_e2e(tmp_path, monkeypatch) -> None:
    env = _env(tmp_path)
    for key, value in env.items():
        if key.startswith("AIASK_") or key in {"HERMES_HOME", "AKSHARE_MCP_SQLITE_PATH", "PYTHONPATH"}:
            monkeypatch.setenv(key, value)

    db_path = Path(env["AIASK_SQLITE_PATH"])
    intent_path = Path(env["AIASK_AGENT_INTENT_DB"])

    async def _run() -> None:
        db = SQLiteAdapter(path=db_path)
        try:
            await db.initialize()

            with pytest.raises(FactoryDispatchRejected, match="executing ActionIntent"):
                await dispatch_factory_run(
                    {"factory": "strategy_factory", "idempotency_key": "bypass"}
                )

            store = ActionIntentStore(intent_path)
            intent = store.create(
                action="factory.run",
                params={
                    "factory": "strategy_factory",
                    "idempotency_key": "e2e:partial",
                    "params": {
                        "_test_result": {
                            "status": "partial_infra",
                            "quality_flags": ["event_sidecar_unavailable"],
                            "blockers": ["sidecar_missing"],
                            "produced": 3,
                        }
                    },
                },
                rationale="E2E partial factory run",
            )
            with pytest.raises(FactoryDispatchRejected, match="not executing"):
                await dispatch_factory_run(
                    {
                        "factory": "strategy_factory",
                        "idempotency_key": "must-not-run-before-confirmation",
                        "_intent_id": intent["intent_id"],
                        "_intent_db_path": str(intent_path),
                    }
                )
            confirmed = await IntentExecutor(store).confirm(intent["intent_id"])
            assert confirmed["success"] is True
            dispatch_result = confirmed["data"]["execution_result"]
            run_id = dispatch_result["data"]["run"]["run_id"]
            partial = await _wait_run(db, run_id)
            assert partial["status"] == "partial"
            assert partial["raw_status"] == "partial_infra"
            assert partial["attempt_count"] == 1
            assert partial["blockers"] == ["sidecar_missing"]

            duplicate_intent = store.create(
                action="factory.run",
                params={
                    "factory": "strategy_factory",
                    "idempotency_key": "e2e:partial",
                    "params": {"_test_result": {"status": "succeeded", "produced": 99}},
                },
            )
            duplicate_confirm = await IntentExecutor(store).confirm(duplicate_intent["intent_id"])
            duplicate_run = duplicate_confirm["data"]["execution_result"]["data"]["run"]
            assert duplicate_run["run_id"] == run_id
            assert duplicate_run["deduplicated"] is True
            await asyncio.sleep(0.2)
            unchanged = await db.get_factory_run(run_id)
            assert unchanged is not None
            assert unchanged["attempt_count"] == 1
            assert unchanged["summary"]["produced"] == 3

            stale = await db.request_factory_run(
                {
                    "factory": "factor_mining",
                    "idempotency_key": "e2e:restart",
                    "params": {"_test_result": {"status": "succeeded", "admitted": 2}},
                }
            )
            claimed = await db.claim_factory_run(stale["run_id"], owner_id="dead-worker", pid=999999)
            assert claimed is not None
            old = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
            async with db.acquire() as conn:
                await conn.execute(
                    "UPDATE factory_run_ledger SET started_at = $2, heartbeat_at = $2 WHERE run_id = $1",
                    stale["run_id"],
                    old,
                )
                await conn.execute(
                    "UPDATE factory_run_locks SET expires_at_epoch = 0 WHERE run_id = $1",
                    stale["run_id"],
                )

            completed = subprocess.run(
                [sys.executable, "-m", "aiask_agent.factory_worker", "--drain"],
                env=env,
                cwd=str(ROOT),
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            assert completed.returncode == 0, completed.stderr
            recovered = await db.get_factory_run(stale["run_id"])
            assert recovered is not None
            assert recovered["status"] == "succeeded"
            assert recovered["attempt_count"] == 2

            for factory in ("factor_mining", "incubation", "market_event_ingest"):
                facade = await strategy_factory_adapter.factory_status({"factory": factory})
                assert facade["success"] is True
                assert facade["data"]["factory"] == factory
                assert facade["meta"]["side_effect"]["level"] == "read_only"
            detail_facade = await strategy_factory_adapter.factory_runs({"run_id": run_id})
            assert detail_facade["success"] is True
            assert detail_facade["data"]["run"]["status"] == "partial"

            tools, readiness, detail = await _mcp_probe(env, run_id)
            assert set(tools) == {
                "factory_list",
                "factory_readiness_read",
                "factory_run_read",
                "factory_runs_read",
                "factory_status_read",
                "factory_tradeability_certificates_read",
            }
            assert readiness["ready"] is True
            assert readiness["warnings"] == [
                "signal_tracker_sidecar_missing",
                "daily_market_sync_sidecar_missing",
            ]
            assert detail["run"]["status"] == "partial"
            assert detail["read_only"] is True
        finally:
            await db.close()
            await close_db()

    asyncio.run(_run())
