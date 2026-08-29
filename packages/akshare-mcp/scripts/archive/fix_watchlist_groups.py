"""Archived watchlist-group repair helper for the SQLite runtime."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path


async def main() -> None:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
    from akshare_mcp.storage import close_db, get_db

    db = get_db()
    await db.initialize()
    async with db.acquire() as conn:
        if await conn.fetchval("SELECT EXISTS (SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'watchlists')"):
            await conn.execute("UPDATE watchlists SET group_name = COALESCE(NULLIF(group_name, ''), 'default')")
    print(f"SQLite watchlist repair completed: {db.status()['path']}")
    await close_db()


if __name__ == "__main__":
    asyncio.run(main())
