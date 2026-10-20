"""SchemaBase adapter-level fetch/execute proxies.

Regression test for the live four-runtime log flooding:

    'SQLiteAdapter' object has no attribute 'fetch'

The strategy-factory paper-trading scheduler (and the execution-universe
contract) call ``await db.fetch(...)``, ``await db.fetchrow(...)`` and
``cursor = await db.execute(...)`` directly on the adapter instance, but
the implementations only existed on the ``SQLiteConnection`` wrapper
returned by ``acquire()``. The scheduler fell back to a swallow-and-continue
warning on every dispatch cycle. The proxies must keep the documented
contract: ``fetch``/``fetchrow``/``fetchval`` return ``dict`` rows, while
``execute`` returns a handle whose ``await fetchall()`` yields **tuple**
rows (raw sqlite3-cursor shape).
"""

from __future__ import annotations

import asyncio

from aiask_quant_core.storage.sqlite import SQLiteAdapter


def _make_db(tmp_path):
    return SQLiteAdapter(path=tmp_path / "proxies.sqlite3")


def test_fetch_proxy_returns_dict_rows_without_prior_acquire(tmp_path):
    db = _make_db(tmp_path)

    async def run():
        await db.initialize()
        try:
            await db.execute("CREATE TABLE proxy_probe (id TEXT PRIMARY KEY, note TEXT)")
            await db.execute(
                "INSERT INTO proxy_probe (id, note) VALUES (?, ?)",
                "run-1", "success",
            )
            rows = await db.fetch(
                "SELECT id, note FROM proxy_probe WHERE id = ?",
                "run-1",
            )
            return rows
        finally:
            await db.close()

    rows = asyncio.run(run())
    assert rows == [{"id": "run-1", "note": "success"}]
    assert isinstance(rows[0], dict)


def test_fetchrow_and_fetchval_proxies(tmp_path):
    db = _make_db(tmp_path)

    async def run():
        await db.initialize()
        try:
            await db.execute("CREATE TABLE proxy_probe (id TEXT PRIMARY KEY, note TEXT)")
            await db.execute(
                "INSERT INTO proxy_probe (id, note) VALUES (?, ?)",
                "run-2", "blocked",
            )
            row = await db.fetchrow(
                "SELECT id, note FROM proxy_probe WHERE id = ?",
                "run-2",
            )
            value = await db.fetchval(
                "SELECT note FROM proxy_probe WHERE id = ?",
                "run-2",
            )
            missing = await db.fetchrow(
                "SELECT id FROM proxy_probe WHERE id = ?",
                "does-not-exist",
            )
            return row, value, missing
        finally:
            await db.close()

    row, value, missing = asyncio.run(run())
    assert row == {"id": "run-2", "note": "blocked"}
    assert value == "blocked"
    assert missing is None


def test_execute_handle_yields_tuple_rows_for_select(tmp_path):
    db = _make_db(tmp_path)

    async def run():
        await db.initialize()
        try:
            await db.execute("CREATE TABLE proxy_probe (id TEXT PRIMARY KEY, note TEXT)")
            await db.execute(
                "INSERT INTO proxy_probe (id, note) VALUES (?, ?)",
                "run-3", "success",
            )
            handle = await db.execute(
                "SELECT id, note FROM proxy_probe WHERE id = ? ORDER BY id",
                "run-3",
            )
            rows = await handle.fetchall()
            return rows
        finally:
            await db.close()

    rows = asyncio.run(run())
    assert rows == [("run-3", "success")]
    # tuple rows: the contract the execution_universe adapters index with row[0]
    assert isinstance(rows[0], tuple)
    assert rows[0][0] == "run-3"


def test_execute_handle_commits_write_statements(tmp_path):
    db = _make_db(tmp_path)

    async def run():
        await db.initialize()
        try:
            await db.execute("CREATE TABLE proxy_probe (id TEXT PRIMARY KEY, note TEXT)")
            handle = await db.execute(
                "INSERT INTO proxy_probe (id, note) VALUES (?, ?)",
                "run-4", "success",
            )
            rows = await handle.fetchall()
            # A second adapter on the same file must see the committed row,
            # proving the write executed (not lazy-dead handle) and committed.
            await db.close()
            db2 = SQLiteAdapter(path=db.path)
            try:
                await db2.initialize()
                seen = await db2.fetch(
                    "SELECT id FROM proxy_probe WHERE id = ?",
                    "run-4",
                )
                return rows, seen
            finally:
                await db2.close()
        finally:
            pass

    rows, seen = asyncio.run(run())
    assert rows == []
    assert seen == [{"id": "run-4"}]
