from __future__ import annotations

import hashlib
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

_SCHEMA_MIGRATION_TABLE = "mcp_schema_migrations"
_APP_CORE_NAMESPACE = "app_core"
_APP_CORE_SOURCE_MODULE = "packages/akshare-mcp/app_core_migrations"
_APP_CORE_MIGRATIONS_DIR = Path(__file__).resolve().parent / "app_core_migrations"


async def ensure_schema_migration_table(conn) -> None:
    await conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {_SCHEMA_MIGRATION_TABLE} (
            id INTEGER PRIMARY KEY,
            namespace TEXT NOT NULL,
            migration_key TEXT NOT NULL,
            checksum TEXT NOT NULL,
            source_module TEXT NOT NULL,
            applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(namespace, migration_key)
        );
        CREATE INDEX IF NOT EXISTS idx_mcp_schema_migrations_namespace_applied
            ON {_SCHEMA_MIGRATION_TABLE}(namespace, applied_at DESC);
        """
    )


async def record_schema_namespace_checkpoint(
    conn,
    *,
    namespace: str,
    migration_key: str,
    source_module: str,
) -> None:
    await ensure_schema_migration_table(conn)
    checksum = hashlib.sha256(f"{namespace}:{migration_key}:{source_module}".encode("utf-8")).hexdigest()
    existing = await conn.fetchrow(
        f"""
        SELECT checksum
          FROM {_SCHEMA_MIGRATION_TABLE}
         WHERE namespace = $1
           AND migration_key = $2
         LIMIT 1
        """,
        namespace,
        migration_key,
    )
    if existing and str(existing["checksum"]) == checksum:
        return
    if existing and str(existing["checksum"]) != checksum:
        # 与 run_app_core_migrations 的策略保持一致：checkpoint 仅记录
        # (namespace, migration_key, source_module) 的字符串签名，发生漂移时
        # 直接刷新 checksum，而不阻塞启动。任何真正的 schema 变更都应通过
        # bumping migration_key 而非更换 source_module 字符串来触发。
        logger.info(
            "Refreshing schema namespace checkpoint checksum: %s/%s",
            namespace,
            migration_key,
        )
        await conn.execute(
            f"""
            UPDATE {_SCHEMA_MIGRATION_TABLE}
               SET checksum = $3,
                   source_module = $4
             WHERE namespace = $1
               AND migration_key = $2
            """,
            namespace,
            migration_key,
            checksum,
            source_module,
        )
        return
    await conn.execute(
        f"""
        INSERT INTO {_SCHEMA_MIGRATION_TABLE} (namespace, migration_key, checksum, source_module)
        VALUES ($1, $2, $3, $4)
        """,
        namespace,
        migration_key,
        checksum,
        source_module,
    )


async def run_app_core_migrations(conn) -> None:
    await ensure_schema_migration_table(conn)
    if not _APP_CORE_MIGRATIONS_DIR.exists():
      logger.info("app_core migrations directory missing, skip MCP-owned app_core bootstrap")
      return

    for path in sorted(_APP_CORE_MIGRATIONS_DIR.glob("*.sql")):
        sql = path.read_text(encoding="utf-8")
        migration_key = path.stem
        checksum = hashlib.sha256(sql.encode("utf-8")).hexdigest()
        existing = await conn.fetchrow(
            f"""
            SELECT checksum
              FROM {_SCHEMA_MIGRATION_TABLE}
             WHERE namespace = $1
               AND migration_key = $2
             LIMIT 1
            """,
            _APP_CORE_NAMESPACE,
            migration_key,
        )
        if existing and str(existing["checksum"]) == checksum:
            continue
        if existing and str(existing["checksum"]) != checksum:
            logger.info("Refreshing normalized SQLite app_core migration checksum: %s", migration_key)
            await conn.execute(
                f"""
                UPDATE {_SCHEMA_MIGRATION_TABLE}
                   SET checksum = $3,
                       source_module = $4
                 WHERE namespace = $1
                   AND migration_key = $2
                """,
                _APP_CORE_NAMESPACE,
                migration_key,
                checksum,
                _APP_CORE_SOURCE_MODULE,
            )
            continue

        logger.info("Applying MCP-owned app_core migration: %s", migration_key)
        async with conn.transaction():
            await conn.execute(sql)
            await conn.execute(
                f"""
                INSERT INTO {_SCHEMA_MIGRATION_TABLE} (namespace, migration_key, checksum, source_module)
                VALUES ($1, $2, $3, $4)
                """,
                _APP_CORE_NAMESPACE,
                migration_key,
                checksum,
                _APP_CORE_SOURCE_MODULE,
            )
