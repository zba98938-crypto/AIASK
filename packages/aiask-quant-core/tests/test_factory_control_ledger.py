from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from aiask_quant_core.storage.sqlite import SQLiteAdapter


def test_factory_control_ledger_idempotency_locks_and_recovery(tmp_path) -> None:
    db = SQLiteAdapter(path=tmp_path / "factory_control.sqlite3")

    async def _run() -> None:
        try:
            await db.initialize()
            first = await db.request_factory_run(
                {
                    "factory": "strategy",
                    "idempotency_key": "strategy:2026-08-18",
                    "params": {"trigger": "test"},
                }
            )
            duplicate = await db.request_factory_run(
                {
                    "factory": "strategy_factory",
                    "idempotency_key": "strategy:2026-08-18",
                    "params": {"trigger": "must-not-replace"},
                }
            )
            assert duplicate["run_id"] == first["run_id"]
            assert duplicate["deduplicated"] is True
            assert duplicate["params"] == {"trigger": "test"}

            claimed = await db.claim_factory_run(first["run_id"], owner_id="worker-a", pid=101)
            assert claimed is not None
            assert claimed["status"] == "running"
            assert claimed["attempt_count"] == 1

            second = await db.request_factory_run(
                {"factory": "strategy_factory", "idempotency_key": "strategy:second"}
            )
            assert await db.claim_factory_run(second["run_id"], owner_id="worker-b") is None

            factor = await db.request_factory_run(
                {"factory": "factor_mining", "idempotency_key": "factor:first"}
            )
            factor_claim = await db.claim_factory_run(factor["run_id"], owner_id="worker-b")
            assert factor_claim is not None
            assert factor_claim["status"] == "running"

            completed = await db.transition_factory_run(
                factor["run_id"],
                "partial",
                expected_status="running",
                raw_status="partial_infra",
                blockers=["sidecar_missing"],
            )
            assert completed is not None
            assert completed["status"] == "partial"
            assert completed["terminal"] is True
            assert completed["blockers"] == ["sidecar_missing"]

            with pytest.raises(ValueError, match="invalid factory run transition"):
                await db.transition_factory_run(factor["run_id"], "running")

            stale_at = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
            async with db.acquire() as conn:
                await conn.execute(
                    "UPDATE factory_run_ledger SET started_at = $2, heartbeat_at = $2 WHERE run_id = $1",
                    first["run_id"],
                    stale_at,
                )
                await conn.execute(
                    "UPDATE factory_run_locks SET expires_at_epoch = 0 WHERE run_id = $1",
                    first["run_id"],
                )
            recovered = await db.recover_stale_factory_runs(stale_after_seconds=60)
            assert [row["run_id"] for row in recovered] == [first["run_id"]]
            assert recovered[0]["status"] == "queued"
            assert recovered[0]["phase"] == "recovered_after_stale_worker"
            assert await db.claim_factory_run(first["run_id"], owner_id="worker-c") is not None
        finally:
            await db.close()

    asyncio.run(_run())
