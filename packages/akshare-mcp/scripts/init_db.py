"""Initialize the AKShare MCP SQLite database."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path


def _ensure_src_path() -> None:
    root = Path(__file__).resolve().parents[1]
    src = root / "src"
    if str(src) not in sys.path:
        sys.path.insert(0, str(src))


async def init_database() -> None:
    _ensure_src_path()
    from akshare_mcp.storage import close_db, get_db

    db = get_db()
    await db.initialize()
    print(f"SQLite database initialized: {db.status()['path']}")
    await close_db()


if __name__ == "__main__":
    asyncio.run(init_database())
