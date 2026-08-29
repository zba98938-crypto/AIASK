from __future__ import annotations

import asyncio
import sqlite3

import pytest

from akshare_mcp.storage import close_db, get_db


def test_paper_trading_bridge_partial_unique_index(tmp_path, monkeypatch) -> None:
    db_path = str(tmp_path / "bridge_orders.sqlite3")
    monkeypatch.setenv("AKSHARE_MCP_SQLITE_PATH", db_path)
    monkeypatch.setenv("AIASK_SQLITE_PATH", db_path)

    async def _run() -> None:
        db = get_db()
        try:
            await db.initialize()
            async with db.acquire() as conn:
                index_sql = await conn.fetchval(
                    """
                    SELECT sql FROM sqlite_master
                    WHERE type = 'index' AND name = $1
                    """,
                    "idx_paper_orders_bridge_signal_unique",
                )
                assert index_sql is not None
                assert "WHERE source = 'paper_trading_bridge'" in index_sql

                await conn.execute(
                    """
                    INSERT INTO paper_accounts
                        (id, user_id, name, initial_capital, current_capital, total_value)
                    VALUES ($1, 'system', 'Bridge Test', 100000, 100000, 100000)
                    """,
                    "paper-bridge-1",
                )
                await conn.execute(
                    """
                    INSERT INTO paper_orders
                        (account_id, code, direction, shares, strategy_id, signal_date, source)
                    VALUES ($1, $2, $3, 100, $4, $5, 'paper_trading_bridge')
                    """,
                    "paper-bridge-1",
                    "600519",
                    "buy",
                    "strategy-bridge-1",
                    "2026-05-21",
                )
                with pytest.raises(sqlite3.IntegrityError):
                    await conn.execute(
                        """
                        INSERT INTO paper_orders
                            (account_id, code, direction, shares, strategy_id, signal_date, source)
                        VALUES ($1, $2, $3, 100, $4, $5, 'paper_trading_bridge')
                        """,
                        "paper-bridge-1",
                        "600519",
                        "buy",
                        "strategy-bridge-1",
                        "2026-05-21",
                    )
                await conn.execute(
                    """
                    INSERT INTO paper_orders
                        (account_id, code, direction, shares, strategy_id, signal_date, source)
                    VALUES ($1, $2, $3, 100, $4, $5, 'manual')
                    """,
                    "paper-bridge-1",
                    "600519",
                    "buy",
                    "strategy-bridge-1",
                    "2026-05-21",
                )
        finally:
            await close_db()

    asyncio.run(_run())
