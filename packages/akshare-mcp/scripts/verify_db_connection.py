"""Verify AKShare MCP SQLite connectivity."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path


def _ensure_src_path() -> None:
    src = Path(__file__).resolve().parents[1] / "src"
    if str(src) not in sys.path:
        sys.path.insert(0, str(src))


async def main() -> None:
    _ensure_src_path()
    from akshare_mcp.storage import close_db, get_db

    db = get_db()
    await db.initialize()
    async with db.acquire() as conn:
        value = await conn.fetchval("SELECT 1")
        table_count = await conn.fetchval("SELECT COUNT(*) FROM sqlite_master WHERE type = $1", "table")
    status = db.status()
    print(f"SQLite OK: path={status['path']} writable={status['writable']} tables={table_count} probe={value}")
    await close_db()


if __name__ == "__main__":
    asyncio.run(main())
