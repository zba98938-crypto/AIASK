"""A `running` task run has to be distinguishable from a crashed one.

Fifteen `strategy_runtime_cycle` rows sat in `running` from 2026-07-16 onward
because a cycle that dies before its finaliser never closes its row. At that
point "running" covered two different facts — an active cycle and a dead
process — and no reader could tell them apart. These tests pin the reclaim that
restores the distinction, and pin the cases where reclaiming would be guessing.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable

from aiask_quant_core.storage import close_db, get_db
from aiask_quant_core.storage.sqlite.strategy_ai_repos.writes import _WritesMixin

#: The adapter is a process-wide singleton keyed by environment, so a case that
#: does not close it hands its data to the next case. That is not hypothetical:
#: an earlier version of this file reclaimed rows created by the tests that ran
#: before it.
CaseBody = Callable[[Any], Awaitable[None]]


def _run_case(tmp_path, monkeypatch, name: str, body: CaseBody) -> None:
    monkeypatch.setenv("AIASK_SQLITE_PATH", str(tmp_path / name))
    monkeypatch.setenv("AKSHARE_MCP_SQLITE_PATH", str(tmp_path / name))
    db = get_db()

    async def _runner() -> None:
        try:
            await db.initialize()
            await body(db)
        finally:
            await close_db()

    asyncio.run(_runner())


def _ago(**kwargs) -> str:
    return (datetime.now(timezone.utc) - timedelta(**kwargs)).isoformat()


def _running(**extra) -> dict:
    payload = {
        "task_name": "strategy_runtime_cycle",
        "status": "running",
        "started_at": _ago(hours=48),
    }
    payload.update(extra)
    return payload


def test_reclaim_closes_a_run_whose_process_died(tmp_path, monkeypatch) -> None:
    async def body(db) -> None:
        row = await db.save_strategy_task_run(_running())

        reclaimed = await db.reclaim_stale_strategy_task_runs(max_age_seconds=3600)

        assert [item["id"] for item in reclaimed] == [row["id"]]
        stored = await db.list_strategy_task_runs(task_name="strategy_runtime_cycle")
        assert len(stored) == 1
        # The outcome of a dead run is unknown, so it must not be recorded as a
        # success. `timed_out` is the honest word; `completed` would not be.
        assert stored[0]["status"] == "timed_out"
        assert stored[0]["completed_at"]
        assert "reclaimed" in str(stored[0]["error"])

    _run_case(tmp_path, monkeypatch, "reclaim_stale.sqlite3", body)


def test_reclaim_leaves_a_live_run_alone(tmp_path, monkeypatch) -> None:
    async def body(db) -> None:
        await db.save_strategy_task_run(_running(started_at=_ago(minutes=5)))

        reclaimed = await db.reclaim_stale_strategy_task_runs(max_age_seconds=3600)

        assert reclaimed == []
        stored = await db.list_strategy_task_runs(task_name="strategy_runtime_cycle")
        assert stored[0]["status"] == "running"
        assert stored[0]["completed_at"] is None

    _run_case(tmp_path, monkeypatch, "reclaim_live.sqlite3", body)


def test_reclaim_never_touches_a_finished_run(tmp_path, monkeypatch) -> None:
    async def body(db) -> None:
        row = await db.save_strategy_task_run(_running(status="completed"))

        reclaimed = await db.reclaim_stale_strategy_task_runs(max_age_seconds=60)

        assert reclaimed == []
        stored = await db.list_strategy_task_runs(task_name="strategy_runtime_cycle")
        assert stored[0]["id"] == row["id"]
        assert stored[0]["status"] == "completed"

    _run_case(tmp_path, monkeypatch, "reclaim_finished.sqlite3", body)


def test_reclaim_is_scoped_to_the_named_task(tmp_path, monkeypatch) -> None:
    async def body(db) -> None:
        await db.save_strategy_task_run(_running(task_name="strategy_runtime_cycle"))
        await db.save_strategy_task_run(_running(task_name="some_other_task"))

        reclaimed = await db.reclaim_stale_strategy_task_runs(
            max_age_seconds=3600, task_name="strategy_runtime_cycle"
        )

        assert [item["task_name"] for item in reclaimed] == ["strategy_runtime_cycle"]
        others = await db.list_strategy_task_runs(task_name="some_other_task")
        assert others[0]["status"] == "running"

    _run_case(tmp_path, monkeypatch, "reclaim_scoped.sqlite3", body)


def test_reclaim_is_idempotent(tmp_path, monkeypatch) -> None:
    async def body(db) -> None:
        await db.save_strategy_task_run(_running())

        first = await db.reclaim_stale_strategy_task_runs(max_age_seconds=3600)
        second = await db.reclaim_stale_strategy_task_runs(max_age_seconds=3600)

        assert len(first) == 1
        assert second == [], "a reclaimed run is not `running` any more, so it must not reclaim twice"

    _run_case(tmp_path, monkeypatch, "reclaim_twice.sqlite3", body)


def test_age_of_an_unparseable_timestamp_is_unknown_not_zero() -> None:
    """An unreadable `started_at` must not be read as 'just started', or as old.

    Returning a number here would let the reclaim decide on evidence it does not
    have. `None` leaves such a row untouched until a human looks at it.
    """

    now = datetime.now(timezone.utc)
    for bad in ("", "   ", None, "not-a-timestamp", "2026-13-45T99:99:99"):
        assert _WritesMixin._task_run_age_seconds(bad, now) is None


def test_naive_started_at_is_read_as_utc() -> None:
    """SQLite's CURRENT_TIMESTAMP is UTC and carries no offset.

    Reading it as local time would shift every age by the machine's UTC offset —
    eight hours here — which is the difference between a reclaim and a false
    reclaim.
    """

    now = datetime(2026, 10, 15, 12, 0, 0, tzinfo=timezone.utc)
    age = _WritesMixin._task_run_age_seconds("2026-10-15 10:00:00", now)
    assert age == 7200.0
