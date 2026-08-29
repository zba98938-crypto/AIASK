"""
Market data table DDL — kline, financials, portfolios, alerts, etc.

All ~30 market-data tables are created via a single entry point:

    await init_market_tables(conn)

The function is idempotent (CREATE TABLE IF NOT EXISTS / ADD COLUMN IF NOT EXISTS).
"""

import logging

logger = logging.getLogger(__name__)
_MARKET_SCHEMA_MIGRATION_TABLE = "market_schema_migrations"


async def _keep_time_series_table(conn, table_name: str, time_column: str = "time") -> None:
    """SQLite stores time-series data as regular indexed tables."""
    del conn, table_name, time_column


async def _ensure_foreign_key(
    conn,
    *,
    table_name: str,
    constraint_name: str,
    definition: str,
) -> None:
    """Keep the old schema hook as a no-op for SQLite migrations."""
    del conn, table_name, constraint_name, definition


async def _ensure_market_schema_migration_table(conn) -> None:
    await conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {_MARKET_SCHEMA_MIGRATION_TABLE} (
            migration_key TEXT PRIMARY KEY,
            applied_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        """
    )


async def _table_columns(conn, table_name: str) -> set[str]:
    rows = await conn.fetch("SELECT name FROM pragma_table_info($1)", table_name)
    return {str(row.get("name") or "") for row in rows}


async def _run_market_migration_once(conn, migration_key: str, statement: str) -> bool:
    await _ensure_market_schema_migration_table(conn)
    already_applied = await conn.fetchval(
        f"SELECT 1 FROM {_MARKET_SCHEMA_MIGRATION_TABLE} WHERE migration_key = $1",
        migration_key,
    )
    if already_applied:
        return False
    await conn.execute(statement)
    await conn.execute(
        f"""
        INSERT INTO {_MARKET_SCHEMA_MIGRATION_TABLE} (migration_key, applied_at)
        VALUES ($1, CURRENT_TIMESTAMP)
        ON CONFLICT (migration_key) DO NOTHING
        """,
        migration_key,
    )
    return True
