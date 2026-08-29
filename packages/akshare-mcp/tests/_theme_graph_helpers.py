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

def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return {row[1] for row in rows}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def _build_adapter(db_path: Path):
    """Build a fresh ``SQLiteAdapter`` for tests that need DAO calls."""

    from aiask_quant_core.storage.sqlite import SQLiteAdapter

    return SQLiteAdapter(path=db_path)


def _run(coro):
    return asyncio.run(coro)


async def _seed_tdx_only_exposure_fixture(adapter) -> None:
    """Seed local TDX/cache tables used by the Phase 6 v1 exposure path."""

    async with adapter.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO stocks
                (stock_code, stock_name, market, sector, industry, list_date,
                 market_cap, tdx_industry, list_status)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
            ON CONFLICT(stock_code) DO UPDATE SET
                stock_name = EXCLUDED.stock_name,
                market = EXCLUDED.market,
                sector = EXCLUDED.sector,
                industry = EXCLUDED.industry,
                market_cap = EXCLUDED.market_cap,
                tdx_industry = EXCLUDED.tdx_industry,
                list_status = EXCLUDED.list_status
            """,
            "600100",
            "AI Chip Co",
            "SH",
            "Technology",
            "Semiconductor",
            "2020-01-01",
            120.0,
            "Semiconductor",
            "L",
        )
        await conn.execute(
            """
            INSERT INTO stocks
                (stock_code, stock_name, market, sector, industry, list_date,
                 market_cap, tdx_industry, list_status)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
            ON CONFLICT(stock_code) DO UPDATE SET
                stock_name = EXCLUDED.stock_name,
                market = EXCLUDED.market,
                sector = EXCLUDED.sector,
                industry = EXCLUDED.industry,
                market_cap = EXCLUDED.market_cap,
                tdx_industry = EXCLUDED.tdx_industry,
                list_status = EXCLUDED.list_status
            """,
            "600200",
            "Low Liquidity Co",
            "SH",
            "Technology",
            "Semiconductor",
            "2021-01-01",
            1.0,
            "Semiconductor",
            "L",
        )
        await conn.execute(
            """
            INSERT INTO tdx_relation (code, block_code, block_name, block_type, gp_num)
            VALUES ($1, $2, $3, $4, $5)
            ON CONFLICT(code, block_code) DO UPDATE SET
                block_name = EXCLUDED.block_name,
                block_type = EXCLUDED.block_type,
                gp_num = EXCLUDED.gp_num
            """,
            "600100",
            "C001",
            "AI concept",
            "concept",
            50,
        )
        await conn.execute(
            """
            INSERT INTO tdx_relation (code, block_code, block_name, block_type, gp_num)
            VALUES ($1, $2, $3, $4, $5)
            ON CONFLICT(code, block_code) DO UPDATE SET
                block_name = EXCLUDED.block_name,
                block_type = EXCLUDED.block_type,
                gp_num = EXCLUDED.gp_num
            """,
            "600100",
            "I001",
            "Semiconductor",
            "industry",
            70,
        )
        await conn.execute(
            """
            INSERT INTO market_blocks (block_code, block_name, block_type, stock_count)
            VALUES ($1, $2, $3, $4)
            ON CONFLICT(block_code, block_type) DO UPDATE SET
                block_name = EXCLUDED.block_name,
                stock_count = EXCLUDED.stock_count
            """,
            "C002",
            "AI block",
            "tdx",
            20,
        )
        await conn.execute(
            """
            INSERT INTO tdx_relation (code, block_code, block_name, block_type, gp_num)
            VALUES ($1, $2, $3, $4, $5)
            ON CONFLICT(code, block_code) DO UPDATE SET
                block_name = EXCLUDED.block_name,
                block_type = EXCLUDED.block_type,
                gp_num = EXCLUDED.gp_num
            """,
            "600999",
            "C002",
            "AI block",
            "concept",
            20,
        )
        await conn.execute(
            """
            INSERT INTO block_stocks (block_code, stock_code, stock_name)
            VALUES ($1, $2, $3)
            ON CONFLICT(block_code, stock_code) DO UPDATE SET
                stock_name = EXCLUDED.stock_name
            """,
            "C002",
            "600100",
            "AI Chip Co",
        )
        await conn.execute(
            """
            INSERT INTO tdx_stock_extra
                (code, trade_date, turnover_rate, zsz, ltsz, tp_flag)
            VALUES ($1, $2, $3, $4, $5, $6)
            ON CONFLICT(code, trade_date) DO UPDATE SET
                turnover_rate = EXCLUDED.turnover_rate,
                zsz = EXCLUDED.zsz,
                ltsz = EXCLUDED.ltsz,
                tp_flag = EXCLUDED.tp_flag
            """,
            "600100",
            "2026-05-24",
            2.5,
            120.0,
            80.0,
            "",
        )
        await conn.execute(
            """
            INSERT INTO tdx_stock_extra
                (code, trade_date, turnover_rate, zsz, ltsz, tp_flag)
            VALUES ($1, $2, $3, $4, $5, $6)
            ON CONFLICT(code, trade_date) DO UPDATE SET
                turnover_rate = EXCLUDED.turnover_rate,
                zsz = EXCLUDED.zsz,
                ltsz = EXCLUDED.ltsz,
                tp_flag = EXCLUDED.tp_flag
            """,
            "600200",
            "2026-05-24",
            0.01,
            1.0,
            1.0,
            "",
        )


EXPECTED_TABLES = (
    "strategy_factory_theme_nodes",
    "strategy_factory_theme_edges",
    "strategy_factory_event_injections",
    "strategy_factory_event_task_lineage",
    "strategy_factory_theme_exposure",
)

# Legacy ``LocalEventDrivenResearchEngine`` writes here. The plan keeps
# them around during the migration window, so the real adapter must keep
# creating them too.
LEGACY_TABLES = (
    "strategy_factory_event_clusters",
    "strategy_factory_event_signals",
)

# The plan §4.3/§6 Phase 0.5 forbids any Tushare-shaped local cache. If
# a future PR accidentally re-introduces this table, this test must fail.
FORBIDDEN_TABLES = (
    "strategy_factory_company_mainbz",
)
