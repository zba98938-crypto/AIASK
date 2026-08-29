"""Print SQLite database status and table counts."""

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
        rows = await conn.fetch(
            """
            SELECT name
              FROM sqlite_master
             WHERE type = 'table'
             ORDER BY name
            """
        )
        status = db.status()
        print(f"SQLite database: {status['path']}")
        print(f"Writable: {status['writable']}  Journal: {status['journal_mode']}  Busy timeout: {status['busy_timeout_ms']}ms")
        print(f"Tables: {len(rows)}")
        for row in rows[:40]:
            name = row["name"]
            count = await conn.fetchval(f"SELECT COUNT(*) FROM {name}")
            print(f"- {name}: {count}")
    await close_db()


if __name__ == "__main__":
    asyncio.run(main())
