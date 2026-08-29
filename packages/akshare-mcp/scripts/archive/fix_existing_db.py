"""Archived SQLite schema repair entry point.

The live schema is initialized by packages/akshare-mcp/scripts/init_db.py.
This archived helper now delegates to that path so old runbooks still land on
the SQLite database.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path


async def main() -> None:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
    from akshare_mcp.storage import close_db, get_db

    db = get_db()
    await db.initialize()
    print(f"SQLite schema is initialized: {db.status()['path']}")
    await close_db()


if __name__ == "__main__":
    asyncio.run(main())
