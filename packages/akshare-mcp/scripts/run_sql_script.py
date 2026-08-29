"""Run a SQL file against the AKShare MCP SQLite database."""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path


def _ensure_src_path() -> None:
    src = Path(__file__).resolve().parents[1] / "src"
    if str(src) not in sys.path:
        sys.path.insert(0, str(src))


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("sql_file", help="Path to a SQLite-compatible SQL file")
    args = parser.parse_args()
    sql_path = Path(args.sql_file).expanduser()
    sql = sql_path.read_text(encoding="utf-8")

    _ensure_src_path()
    from akshare_mcp.storage import close_db, get_db

    db = get_db()
    await db.initialize()
    async with db.acquire() as conn:
        await conn.execute(sql)
    print(f"Executed {sql_path} against {db.status()['path']}")
    await close_db()


if __name__ == "__main__":
    asyncio.run(main())
