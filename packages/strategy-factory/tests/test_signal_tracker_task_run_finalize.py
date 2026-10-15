"""A finished signal-tracking cycle must not leave its own row `running`.

The cycle used to close itself with ``save_strategy_task_run``, which is a plain
INSERT whose column list has no ``id``. The id it was handed was dropped, so the
original `running` row was never closed and the final status landed in a second
row. Fifteen such rows sat open in the runtime database from 2026-07-16 with
``result = {}``; "running" stopped meaning anything.

``update_strategy_task_run`` exists for this and is used by the other task-run
writers in the tree. This test drives a real cycle against a real SQLite file,
so it fails if the finaliser ever goes back to inserting.
"""

from __future__ import annotations

import asyncio

from aiask_quant_core.storage import close_db, get_db
from strategy_factory.application.signal_tracker.orchestrator import SignalTrackerOrchestrator


class _EmptyPhaseProvider:
    """Provider whose every phase succeeds and produces nothing.

    The point of the test is the task-run bookkeeping, not phase behaviour, so
    phases return empty payloads rather than realistic ones.
    """

    def __init__(self, db) -> None:
        self._db = db

    async def get_db(self):
        return self._db

    def get_default_universe(self) -> list:
        return []

    def phase_timeout_seconds(self, phase_name: str) -> float:
        del phase_name
        return 30.0

    def __getattr__(self, name: str):
        async def _phase(*_args, **_kwargs):
            return {}

        return _phase


def test_completed_cycle_closes_its_own_task_run(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("AIASK_SQLITE_PATH", str(tmp_path / "cycle_finalize.sqlite3"))
    monkeypatch.setenv("AKSHARE_MCP_SQLITE_PATH", str(tmp_path / "cycle_finalize.sqlite3"))
    db = get_db()

    async def _run() -> None:
        try:
            await db.initialize()
            result = await SignalTrackerOrchestrator(_EmptyPhaseProvider(db)).run_cycle()

            rows = await db.list_strategy_task_runs(task_name="strategy_runtime_cycle", limit=50)
            assert len(rows) == 1, (
                "closing a cycle must update the row it opened, not append a second one: "
                f"{[(row['id'], row['status']) for row in rows]}"
            )
            row = rows[0]
            assert row["id"] == result["task_run_id"]
            assert row["status"] != "running", "the row the cycle opened was left open"
            assert row["result"], "the finaliser never wrote the cycle result"
        finally:
            await close_db()

    asyncio.run(_run())


def test_a_dead_cycle_row_is_reclaimed_by_the_next_cycle(tmp_path, monkeypatch) -> None:
    """A cycle must not inherit the previous run's open row as a live one."""

    monkeypatch.setenv("AIASK_SQLITE_PATH", str(tmp_path / "cycle_reclaim.sqlite3"))
    monkeypatch.setenv("AKSHARE_MCP_SQLITE_PATH", str(tmp_path / "cycle_reclaim.sqlite3"))
    monkeypatch.setenv("STRATEGY_TASK_RUN_STALE_SECONDS", "60")
    db = get_db()

    async def _run() -> None:
        try:
            await db.initialize()
            # Stand in for a cycle whose process died: an open row that is older
            # than the cutoff.
            dead = await db.save_strategy_task_run(
                {
                    "task_name": "strategy_runtime_cycle",
                    "status": "running",
                    "started_at": "2026-07-16 01:55:59",
                }
            )

            await SignalTrackerOrchestrator(_EmptyPhaseProvider(db)).run_cycle()

            rows = await db.list_strategy_task_runs(task_name="strategy_runtime_cycle", limit=50)
            by_id = {row["id"]: row for row in rows}
            assert by_id[dead["id"]]["status"] == "timed_out"
            assert by_id[dead["id"]]["completed_at"]
            assert sum(1 for row in rows if row["status"] == "running") == 0
        finally:
            await close_db()

    asyncio.run(_run())
