"""Archived SQLite schema verification helper."""

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
        table_count = await conn.fetchval("SELECT COUNT(*) FROM sqlite_master WHERE type = $1", "table")
    print(f"SQLite schema verification OK: {table_count} tables")
    await close_db()


if __name__ == "__main__":
    asyncio.run(main())
