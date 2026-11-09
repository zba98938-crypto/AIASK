"""Read-only diagnostic connections cannot bootstrap or mutate the store."""
import hashlib
import sqlite3

import pytest
from aiask_quant_core.storage.sqlite import SQLiteAdapter


@pytest.mark.asyncio
async def test_read_only_adapter_does_not_migrate_and_rejects_writes(tmp_path):
    path = tmp_path / "existing.sqlite3"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE evidence (value INTEGER)")
        conn.execute("INSERT INTO evidence VALUES (7)")
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    adapter = SQLiteAdapter(path=path, read_only=True)
    try:
        await adapter.initialize()
        async with adapter.acquire() as conn:
            assert (await conn.fetchrow("SELECT value FROM evidence"))["value"] == 7
            with pytest.raises(sqlite3.OperationalError, match="readonly"):
                await conn.execute("INSERT INTO evidence VALUES (8)")
        tables = [r[0] for r in adapter.connection.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        assert tables == ["evidence"]
    finally:
        await adapter.close()
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before


@pytest.mark.asyncio
async def test_read_only_adapter_does_not_create_missing_db_or_directory(tmp_path):
    path = tmp_path / "missing" / "new.sqlite3"
    adapter = SQLiteAdapter(path=path, read_only=True)
    with pytest.raises(sqlite3.OperationalError):
        await adapter.initialize()
    assert not path.parent.exists()
