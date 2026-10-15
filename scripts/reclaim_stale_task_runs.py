#!/usr/bin/env python3
"""Close task runs that are still marked ``running`` but cannot still be running.

Why this exists
---------------
Fifteen ``strategy_runtime_cycle`` rows sat in ``running`` from 2026-07-16, all
with ``result = {}``. Their processes died before the finaliser ran, so nothing
ever closed them. The cost is not disk: it is that ``running`` covered both "a
cycle is executing" and "a process is gone", and no reader of the table could
tell which. Every later cycle also inherited them as if they were live.

The runtime reclaims stale runs at the start of each cycle. This script exists
for the rows that predate it, and for inspecting them without changing anything.

A reclaimed run is marked ``timed_out``, never ``completed``: the outcome of a
run whose process vanished is unknown, and ``timed_out`` is the honest word.

Usage::

    python scripts/reclaim_stale_task_runs.py                 # report only
    python scripts/reclaim_stale_task_runs.py --apply         # close them
    python scripts/reclaim_stale_task_runs.py --max-age 3600  # tighten the cutoff
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
for _rel in ("packages/aiask-quant-core/src", "packages/strategy-factory/src"):
    _path = str(REPO_ROOT / _rel)
    if _path not in sys.path:
        sys.path.insert(0, _path)

from aiask_quant_core.storage import close_db, get_db  # noqa: E402
from strategy_factory.application.signal_tracker.orchestrator import (  # noqa: E402
    DEFAULT_STALE_TASK_RUN_SECONDS,
    _stale_task_run_seconds,
)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--apply",
        action="store_true",
        help="close the stale runs; without this flag nothing is written",
    )
    parser.add_argument(
        "--max-age",
        type=int,
        default=None,
        help=(
            "seconds a run may stay open before it counts as stale "
            f"(default: STRATEGY_TASK_RUN_STALE_SECONDS or {DEFAULT_STALE_TASK_RUN_SECONDS})"
        ),
    )
    parser.add_argument("--task-name", default="", help="limit to one task name")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    cutoff = int(args.max_age) if args.max_age is not None else _stale_task_run_seconds()
    task_name = args.task_name.strip() or None

    async def _run() -> int:
        db = get_db()
        try:
            await db.initialize()
            open_runs = await db.list_strategy_task_runs(status="running", limit=500)
            # The listing is a read; the decision to close belongs to the
            # reclaim, which is the only place that knows the age cutoff.
            print(f"sqlite           : {os.getenv('AIASK_SQLITE_PATH') or '(runtime default)'}")
            print(f"cutoff           : {cutoff}s")
            print(f"scope            : {task_name or 'all task names'}")
            print(f"rows marked running: {len(open_runs)}")
            for row in open_runs[:20]:
                print(f"  id={row['id']:<6} {row['task_name']:<28} started_at={row['started_at']}")
            if len(open_runs) > 20:
                print(f"  ... and {len(open_runs) - 20} more")

            if not args.apply:
                print()
                print("report only; re-run with --apply to close the stale rows")
                return 0

            reclaimed = await db.reclaim_stale_strategy_task_runs(
                max_age_seconds=cutoff, task_name=task_name
            )
            print()
            print(f"reclaimed: {len(reclaimed)}")
            for row in reclaimed:
                print(f"  id={row['id']:<6} {row['task_name']:<28} -> {row['status']}")
            return 0
        finally:
            await close_db()

    return asyncio.run(_run())


if __name__ == "__main__":
    raise SystemExit(main())
