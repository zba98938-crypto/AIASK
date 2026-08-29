"""Shared tracking for short-lived background tasks."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable
from contextlib import suppress
from typing import Any

logger = logging.getLogger(__name__)

_BACKGROUND_TASKS: set[asyncio.Task[Any]] = set()


def _discard_task(task: asyncio.Task[Any]) -> None:
    _BACKGROUND_TASKS.discard(task)
    if task.cancelled():
        return
    try:
        exc = task.exception()
    except asyncio.CancelledError:
        return
    except Exception as exc:
        logger.warning("[background_tasks] failed to inspect completed task: %s", exc)
        return
    if exc is not None:
        logger.warning("[background_tasks] task finished with unhandled error: %s", exc)


def track_background_task(
    awaitable: Awaitable[Any],
    *,
    name: str | None = None,
) -> asyncio.Task[Any]:
    """Create and track a task so shutdown can drain it."""
    task = asyncio.create_task(awaitable, name=name)
    _BACKGROUND_TASKS.add(task)
    task.add_done_callback(_discard_task)
    return task


async def drain_background_tasks(*, timeout_seconds: float = 3.0) -> None:
    """Wait for short-lived tracked tasks, cancelling leftovers on timeout."""
    current_task = asyncio.current_task()
    tasks = [
        task
        for task in list(_BACKGROUND_TASKS)
        if task is not current_task and not task.done()
    ]
    if not tasks:
        return
    try:
        await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=max(0.0, timeout_seconds))
    except asyncio.TimeoutError:
        for task in tasks:
            if not task.done():
                task.cancel()
        for task in tasks:
            with suppress(asyncio.CancelledError):
                await task


def pending_background_task_count() -> int:
    return sum(1 for task in _BACKGROUND_TASKS if not task.done())


__all__ = [
    "drain_background_tasks",
    "pending_background_task_count",
    "track_background_task",
]
