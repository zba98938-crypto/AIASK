"""PR-A smoke tests: theme graph schema + seed idempotency.

This test file is part of the event-driven theme-linkage upgrade plan
(see ``docs/event-driven/事件驱动主题联动-结合当前代码升级方案-2026-05-24.md`` Phase 0).

Design principle (Phase 0 verification line 1):
    "新建临时 SQLite 后,真实 adapter 初始化能创建全部事件驱动表"

That is, the test must drive the real ``SQLiteAdapter`` so that any future
DDL change is exercised. Earlier revisions of this file embedded raw
``CREATE TABLE`` strings copied from ``schema_strategy_parts/queries.py``.
That bypassed the very migration code we want to validate, so the
embedded DDL was removed. If you find yourself adding ``CREATE TABLE`` to
this file again, stop — extend the real schema module instead.
"""

from __future__ import annotations

import asyncio
import os
import sqlite3
import sys
from pathlib import Path

import pytest

# Make local ``akshare_mcp`` (compat shell that aliases the canonical
# ``aiask_quant_core.storage.sqlite`` package) and ``aiask_quant_core``
# both importable when this test is run via ``pytest`` from any cwd.
ROOT = Path(__file__).resolve().parents[1]
WORKSPACE_ROOT = Path(__file__).resolve().parents[3]
SRC = ROOT / "src"
QUANT_CORE_SRC = WORKSPACE_ROOT / "packages" / "aiask-quant-core" / "src"
for candidate in (SRC, QUANT_CORE_SRC):
    if candidate.exists() and str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

SCRIPTS_DIR = WORKSPACE_ROOT / "scripts"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def tmp_db_path(tmp_path: Path) -> Path:
    """Return a fresh tmp path used by both the real adapter and seed runs."""

    return tmp_path / "test_theme_graph.sqlite3"


@pytest.fixture
def initialized_db(tmp_db_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Drive the real ``SQLiteAdapter`` so it creates every strategy table.

    Returns the path of the initialized database. Subsequent tests can
    open it via raw ``sqlite3.connect`` for assertions.
    """

    monkeypatch.setenv("AKSHARE_MCP_SQLITE_PATH", str(tmp_db_path))
    monkeypatch.setenv("AIASK_SQLITE_PATH", str(tmp_db_path))

    from aiask_quant_core.storage.sqlite import SQLiteAdapter

    adapter = SQLiteAdapter(path=tmp_db_path)

    async def _setup() -> None:
        try:
            await adapter.initialize()
        finally:
            try:
                await adapter.close()
            except Exception:
                pass

    asyncio.run(_setup())
    return tmp_db_path


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


# These five tables are the minimum surface area that the event-driven
# theme-linkage plan requires (§2 of the upgrade plan).
