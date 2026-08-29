"""Probe whether SQLiteAdapter.initialize() really creates the 5 event-driven tables.

Read-only check: creates a fresh tmp SQLite, runs the real adapter init,
then queries sqlite_master for the 5 expected tables. No code changes.
"""

from __future__ import annotations

import asyncio
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for pkg_src in [
    ROOT / "packages" / "aiask-quant-core" / "src",
    ROOT / "packages" / "akshare-mcp" / "src",
]:
    if str(pkg_src) not in sys.path:
        sys.path.insert(0, str(pkg_src))


EXPECTED_TABLES = [
    "strategy_factory_theme_nodes",
    "strategy_factory_theme_edges",
    "strategy_factory_event_injections",
    "strategy_factory_event_task_lineage",
    "strategy_factory_theme_exposure",
]


async def main() -> int:
    tmp_dir = Path(tempfile.mkdtemp(prefix="aiask_probe_"))
    db_path = tmp_dir / "probe.sqlite3"
    os.environ["AKSHARE_MCP_SQLITE_PATH"] = str(db_path)
    os.environ["AIASK_SQLITE_PATH"] = str(db_path)

    print(f"[probe] tmp db: {db_path}")

    from aiask_quant_core.storage.sqlite import SQLiteAdapter

    adapter = SQLiteAdapter(path=db_path)
    try:
        await adapter.initialize()
        print("[probe] adapter.initialize() OK")
    except Exception as exc:
        print(f"[probe] adapter.initialize() FAILED: {exc}")
        return 1

    conn = sqlite3.connect(str(db_path))
    try:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
        names = {r[0] for r in rows}

        print(f"[probe] total tables created: {len(names)}")

        ok = True
        for table in EXPECTED_TABLES:
            present = table in names
            print(f"  {'OK' if present else 'MISSING':<8} {table}")
            ok = ok and present

        if ok:
            print("[probe] PASS: all 5 event-driven tables present after real adapter init")
            # Sanity: schema_factory_company_mainbz must NOT exist (Phase 0.5 forbids it)
            forbidden = "strategy_factory_company_mainbz"
            if forbidden in names:
                print(f"[probe] WARN: forbidden {forbidden} exists")
            return 0
        else:
            print("[probe] FAIL: at least one expected table missing")
            return 2
    finally:
        conn.close()
        try:
            await adapter.close()
        except Exception:
            pass


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
