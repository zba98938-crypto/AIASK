"""Schema bootstrap must not invalidate healthy FTS readers when unchanged."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import sqlite3
import threading

import pytest

from aiask_quant_core.storage.sqlite import SQLiteAdapter
from aiask_quant_core.storage.sqlite.schema_base import SQLiteConnection, ensure_schema_object


@pytest.mark.asyncio
async def test_unchanged_bootstrap_preserves_schema_version(tmp_path):
    path = tmp_path / "stable.sqlite3"
    first = SQLiteAdapter(path)
    await first.initialize()
    initial = first.connection.execute("PRAGMA schema_version").fetchone()[0]
    await first.close()
    second = SQLiteAdapter(path)
    await second.initialize()
    assert second.connection.execute("PRAGMA schema_version").fetchone()[0] == initial
    await second.close()


@pytest.mark.asyncio
async def test_changed_view_and_unique_index_upgrade_and_preserve_literal_spaces(tmp_path):
    raw = sqlite3.connect(tmp_path / "objects.sqlite3")
    raw.row_factory = sqlite3.Row
    conn = SQLiteConnection(raw, threading.RLock())
    await conn.execute("CREATE TABLE source(id INTEGER, other INTEGER); INSERT INTO source VALUES(1,10);")
    await ensure_schema_object(conn, "VIEW", "visible", "CREATE VIEW IF NOT EXISTS visible AS SELECT id, 'a  b' AS label FROM source;")
    version = await conn.fetchval("PRAGMA schema_version")
    await ensure_schema_object(conn, "VIEW", "visible", "create view visible as\n SELECT id, 'a  b' as label FROM source")
    assert await conn.fetchval("PRAGMA schema_version") == version
    await ensure_schema_object(conn, "VIEW", "visible", "CREATE VIEW visible AS SELECT other, 'a b' AS label FROM source")
    assert await conn.fetchrow("SELECT * FROM visible") == {"other": 10, "label": "a b"}
    await ensure_schema_object(conn, "INDEX", "unique_source", "CREATE UNIQUE INDEX IF NOT EXISTS unique_source ON source(id)")
    await ensure_schema_object(conn, "INDEX", "unique_source", "CREATE UNIQUE INDEX unique_source ON source(other)")
    await conn.execute("INSERT INTO source VALUES(1,20)")
    with pytest.raises(sqlite3.IntegrityError):
        await conn.execute("INSERT INTO source VALUES(2,20)")
    raw.rollback()
    raw.close()


@pytest.mark.asyncio
async def test_failed_replacement_restores_original_and_does_not_swallow_error(tmp_path):
    raw = sqlite3.connect(tmp_path / "rollback.sqlite3")
    raw.row_factory = sqlite3.Row
    conn = SQLiteConnection(raw, threading.RLock())
    await conn.execute("CREATE TABLE source(id INTEGER); INSERT INTO source VALUES(1);")
    await ensure_schema_object(conn, "INDEX", "source_idx", "CREATE INDEX source_idx ON source(id)")
    for nested in (False, True):
        if nested:
            conn._begin()
        with pytest.raises(sqlite3.OperationalError, match="no such column"):
            await ensure_schema_object(conn, "INDEX", "source_idx", "CREATE INDEX source_idx ON source(missing)")
        assert (await conn.fetchrow("PRAGMA index_info(source_idx)"))["name"] == "id"
        if nested:
            conn._commit()
    raw.close()


def test_actual_bootstraps_concurrent_with_first_fts_queries(tmp_path):
    path = tmp_path / "concurrent.sqlite3"

    async def initialize():
        db = SQLiteAdapter(path)
        try:
            await db.initialize()
        finally:
            await db.close()

    asyncio.run(initialize())
    with sqlite3.connect(path) as conn:
        conn.execute("INSERT INTO market_documents(id,doc_uid,source,doc_type) VALUES(1,'fixture','fixture','notice')")
        conn.execute("INSERT INTO market_doc_chunks(id,doc_id,chunk_no,doc_type,source,chunk_text) VALUES(1,1,0,'notice','fixture','公告')")
    barrier = threading.Barrier(3)

    def writer():
        barrier.wait()
        for _ in range(8):
            asyncio.run(initialize())

    def reader():
        barrier.wait()
        for _ in range(100):
            with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as conn:
                conn.execute("PRAGMA query_only=ON")
                assert conn.execute("SELECT COUNT(*) FROM market_doc_chunks_fts").fetchone()[0] == 1
                assert conn.execute("SELECT COUNT(*) FROM market_doc_chunks_fts WHERE market_doc_chunks_fts MATCH '公告'").fetchone()[0] == 1

    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = [pool.submit(writer), pool.submit(writer), pool.submit(reader)]
        for future in futures:
            future.result(timeout=30)
