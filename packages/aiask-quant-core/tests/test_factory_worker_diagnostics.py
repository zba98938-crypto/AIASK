"""Factory ledger worker-diagnostics columns.

The worker subprocess used to send stdout and stderr to DEVNULL, so a worker
that died during import left no trace and its run sat in ``queued`` until the
stale sweep noticed. stderr was also persisted only when the runner produced no
parsable JSON, so a failing-but-chatty run discarded the one thing needed to
diagnose it.

These tests pin the ledger side of that: the diagnostic columns exist, they
survive a transition, launch metadata can be recorded without driving the state
machine, and the existing idempotency / recovery semantics still hold.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from aiask_quant_core.storage.sqlite import SQLiteAdapter


def test_diagnostic_columns_exist_and_round_trip(tmp_path) -> None:
    db = SQLiteAdapter(path=tmp_path / "factory.sqlite3")

    async def _run() -> None:
        try:
            await db.initialize()
            async with db.acquire() as conn:
                rows = await conn.fetch("PRAGMA table_info(factory_run_ledger)")
            columns = {str(row["name"]) for row in rows}
            for column in ("worker_started_at", "worker_command", "failure_kind", "stderr_tail"):
                assert column in columns, f"missing diagnostic column: {column}"

            run = await db.request_factory_run(
                {"factory": "strategy_factory", "idempotency_key": "diag:1", "params": {}}
            )
            claimed = await db.claim_factory_run(run["run_id"], owner_id="worker-a", pid=4242)
            assert claimed is not None
            # claim_factory_run stamps the start time so "when did this begin"
            # is answerable even if the worker never reports back.
            assert claimed["worker_started_at"]

            terminal = await db.transition_factory_run(
                run["run_id"],
                "failed",
                expected_status="running",
                exit_code=1,
                failure_kind="runner_nonzero_exit",
                stderr_tail="Traceback (most recent call last):\nBoom",
                error="runner exited with code 1",
            )
            assert terminal is not None
            assert terminal["failure_kind"] == "runner_nonzero_exit"
            assert "Boom" in terminal["stderr_tail"]
            assert terminal["exit_code"] == 1

            reread = await db.get_factory_run(run["run_id"])
            assert reread is not None
            assert reread["failure_kind"] == "runner_nonzero_exit"
            assert "Boom" in reread["stderr_tail"]
        finally:
            await db.close()

    asyncio.run(_run())


def test_stderr_tail_is_bounded(tmp_path) -> None:
    """A chatty runner must not be able to bloat the ledger."""
    db = SQLiteAdapter(path=tmp_path / "factory.sqlite3")

    async def _run() -> None:
        try:
            await db.initialize()
            run = await db.request_factory_run(
                {"factory": "factor_mining", "idempotency_key": "diag:big", "params": {}}
            )
            await db.claim_factory_run(run["run_id"], owner_id="worker-a")
            marker = "TAIL-MARKER-KEPT"
            noisy = ("x" * 200_000) + marker
            terminal = await db.transition_factory_run(
                run["run_id"],
                "failed",
                expected_status="running",
                stderr_tail=noisy,
                failure_kind="runner_nonzero_exit",
            )
            assert terminal is not None
            stored = terminal["stderr_tail"]
            assert len(stored) < 10_000, "stderr tail must be clipped"
            # The tail is what matters: the traceback lands at the end.
            assert stored.endswith(marker)
        finally:
            await db.close()

    asyncio.run(_run())


def test_launch_metadata_does_not_drive_the_state_machine(tmp_path) -> None:
    """A fast worker can finish before the dispatcher records how it launched.

    Routing that record through transition_factory_run would attempt an illegal
    ``partial -> queued`` move, so it must write the diagnostic columns only.
    """
    db = SQLiteAdapter(path=tmp_path / "factory.sqlite3")

    async def _run() -> None:
        try:
            await db.initialize()
            run = await db.request_factory_run(
                {"factory": "incubation", "idempotency_key": "diag:race", "params": {}}
            )
            await db.claim_factory_run(run["run_id"], owner_id="worker-a")
            done = await db.transition_factory_run(
                run["run_id"], "partial", expected_status="running", raw_status="partial_infra"
            )
            assert done is not None and done["status"] == "partial"

            updated = await db.record_factory_worker_launch(
                run["run_id"],
                worker_started_at="2026-08-29T00:00:00+00:00",
                worker_command="python -m aiask_agent.factory_worker --run-id x",
                pid=777,
            )
            assert updated is not None
            assert updated["status"] == "partial", "launch metadata must not change status"
            assert updated["raw_status"] == "partial_infra"
            assert updated["worker_command"].endswith("--run-id x")
            assert updated["pid"] == 777
        finally:
            await db.close()

    asyncio.run(_run())


def test_recovery_and_idempotency_still_hold(tmp_path) -> None:
    """The new columns must not disturb stale recovery or dedupe semantics."""
    db = SQLiteAdapter(path=tmp_path / "factory.sqlite3")

    async def _run() -> None:
        try:
            await db.initialize()
            run = await db.request_factory_run(
                {"factory": "strategy_factory", "idempotency_key": "diag:recover", "params": {"a": 1}}
            )
            duplicate = await db.request_factory_run(
                {"factory": "strategy_factory", "idempotency_key": "diag:recover", "params": {"a": 999}}
            )
            assert duplicate["run_id"] == run["run_id"]
            assert duplicate["deduplicated"] is True
            assert duplicate["params"] == {"a": 1}

            claimed = await db.claim_factory_run(run["run_id"], owner_id="worker-a", pid=11)
            assert claimed is not None and claimed["attempt_count"] == 1

            stale = (datetime.now(timezone.utc) - timedelta(seconds=600)).isoformat()
            async with db.acquire() as conn:
                await conn.execute(
                    "UPDATE factory_run_ledger SET started_at = $2, heartbeat_at = $2 WHERE run_id = $1",
                    run["run_id"],
                    stale,
                )
                await conn.execute(
                    "UPDATE factory_run_locks SET expires_at_epoch = 0 WHERE run_id = $1",
                    run["run_id"],
                )

            recovered = await db.recover_stale_factory_runs(stale_after_seconds=60)
            assert [item["run_id"] for item in recovered] == [run["run_id"]]
            row = await db.get_factory_run(run["run_id"])
            assert row is not None
            assert row["status"] == "queued"
            assert row["phase"] == "recovered_after_stale_worker"

            reclaimed = await db.claim_factory_run(run["run_id"], owner_id="worker-b", pid=12)
            assert reclaimed is not None
            assert reclaimed["attempt_count"] == 2, "retry count must survive recovery"
        finally:
            await db.close()

    asyncio.run(_run())
