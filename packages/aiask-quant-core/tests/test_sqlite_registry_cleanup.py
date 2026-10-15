from __future__ import annotations

import asyncio

from aiask_quant_core.storage import close_db, get_db


def test_close_db_clears_thread_fallback_created_before_event_loop(tmp_path, monkeypatch) -> None:
    """A DB acquired before asyncio must not survive loop cleanup."""
    path = tmp_path / "registry_cleanup.sqlite3"
    monkeypatch.setenv("AIASK_SQLITE_PATH", str(path))
    monkeypatch.setenv("AKSHARE_MCP_SQLITE_PATH", str(path))

    thread_db = get_db()

    async def _run() -> None:
        await thread_db.initialize()
        await close_db()

    asyncio.run(_run())

    fresh_db = get_db()
    try:
        assert fresh_db is not thread_db
        assert fresh_db.path == thread_db.path
    finally:
        asyncio.run(close_db())
