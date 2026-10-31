from __future__ import annotations

import asyncio
import importlib.util
import logging
import sqlite3
from argparse import Namespace
from contextlib import asynccontextmanager
from pathlib import Path

import pytest


SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "audit_sync_factor_context_data.py"
SPEC = importlib.util.spec_from_file_location("audit_sync_factor_context_data", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(module)


class _SQLiteConnection:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    async def fetch(self, sql: str, *params: object) -> list[dict[str, object]]:
        query = sql.replace("$1", "?")
        cursor = self.connection.execute(query, params)
        return [dict(row) for row in cursor.fetchall()]


class _SQLiteDatabase:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection
        self.adapter = _SQLiteConnection(connection)

    @asynccontextmanager
    async def acquire(self):
        yield self.adapter


def _patch_dispatch_db(monkeypatch, tmp_path: Path, rows: list[tuple[str, str]] | None = None):
    import akshare_mcp.storage as storage

    db_path = tmp_path / "dispatch.sqlite3"
    db_path.unlink(missing_ok=True)
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    connection.execute(
        "CREATE TABLE strategy_factory_dispatches(metadata TEXT, requested_at TEXT)"
    )
    if rows:
        connection.executemany(
            "INSERT INTO strategy_factory_dispatches(metadata, requested_at) VALUES (?, ?)",
            rows,
        )
    monkeypatch.setattr(storage, "get_db", lambda: _SQLiteDatabase(connection))
    return connection


def test_dispatch_target_codes_accepts_mapping_and_positional_rows() -> None:
    result = module._dispatch_target_codes(
        [
            {"codes": '["000001", "600519"]'},
            ('["600519", "300750"]',),
        ],
        limit=10,
    )
    assert result == ["000001", "600519", "300750"]


def test_dispatch_target_codes_accepts_object_with_target_codes() -> None:
    assert module._dispatch_target_codes(
        [{"codes": '{"target_codes": ["000001", "600519"]}'}],
        limit=1,
    ) == ["000001"]


def test_dispatch_target_codes_empty_result_is_explicit_empty_list() -> None:
    assert module._dispatch_target_codes([{"codes": "[]"}], limit=12) == []


def test_dispatch_target_codes_rejects_malformed_json() -> None:
    with pytest.raises(ValueError, match="invalid JSON"):
        module._dispatch_target_codes([{"codes": "not-json"}], limit=12)


def test_dispatch_target_codes_rejects_non_array_payload() -> None:
    with pytest.raises(ValueError, match="must be a JSON array"):
        module._dispatch_target_codes([{"codes": '"000001"'}], limit=12)


def test_dispatch_target_codes_skips_null_rows_and_deduplicates() -> None:
    assert module._dispatch_target_codes(
        [{"codes": None}, (None,), {"codes": '["000001", "000001"]'}],
        limit=12,
    ) == ["000001"]


def test_factory_target_resolver_merges_recent_dispatch_metadata(tmp_path, monkeypatch) -> None:
    connection = _patch_dispatch_db(
        monkeypatch,
        tmp_path,
        [
            ('{"target_codes":["000001","600519"]}', "2026-10-31T10:00:00+08:00"),
            ('{"target_codes":["600519","300750"]}', "2026-10-30T10:00:00+08:00"),
        ],
    )
    try:
        result = asyncio.run(module._resolve_factory_target_codes(10))
    finally:
        connection.close()
    assert result == ["000001", "600519", "300750"]


def test_factory_target_resolver_distinguishes_no_targets_and_bad_json(tmp_path, monkeypatch) -> None:
    connection = _patch_dispatch_db(
        monkeypatch,
        tmp_path,
        [('{"target_codes":[]}', "2026-10-31T10:00:00+08:00")],
    )
    try:
        assert asyncio.run(module._resolve_factory_target_codes(10)) == []
    finally:
        connection.close()

    connection = _patch_dispatch_db(
        monkeypatch,
        tmp_path,
        [('{"target_codes":"not-json"}', "2026-10-31T10:00:00+08:00")],
    )
    try:
        with pytest.raises(ValueError, match="invalid JSON"):
            asyncio.run(module._resolve_factory_target_codes(10))
    finally:
        connection.close()


def test_factory_target_resolver_surfaces_dispatch_query_failure(tmp_path, monkeypatch) -> None:
    connection = _patch_dispatch_db(monkeypatch, tmp_path)
    connection.execute("DROP TABLE strategy_factory_dispatches")
    try:
        with pytest.raises(RuntimeError, match="dispatch read failed"):
            asyncio.run(module._resolve_factory_target_codes(10))
    finally:
        connection.close()


def test_resolve_codes_preserves_fallback_and_marks_source_failure(
    monkeypatch, caplog
) -> None:
    async def _fail(_limit: int) -> list[str]:
        raise RuntimeError("dispatch offline")

    monkeypatch.setattr(module, "_resolve_factory_target_codes", _fail)
    args = Namespace(
        codes="",
        scope_sources="representative,factory_targets",
        active_pool_limit=10,
    )
    with caplog.at_level(logging.WARNING):
        codes, failures = asyncio.run(module._resolve_codes(args))

    assert codes == module.DEFAULT_REPRESENTATIVE_CODES
    assert len(failures) == 1
    assert "factory_targets_read_failed" in failures[0]
    assert "fallback_source=representative" in failures[0]
    assert "degraded=true fallback_source=representative" in caplog.text


def test_main_returns_failure_when_factory_targets_cannot_be_read(monkeypatch) -> None:
    async def _resolve(_args: Namespace) -> tuple[list[str], list[str]]:
        return module.DEFAULT_REPRESENTATIVE_CODES, [
            "[audit_sync_target_resolution] reason=factory_targets_read_failed"
        ]

    async def _ingest(_args: Namespace, _codes: list[str]) -> dict[str, object]:
        return {"fetched": {}, "saved": {}, "errors": []}

    async def _fund_flow(_codes: list[str]) -> dict[str, object]:
        return {"updated": 0}

    async def _table_counts(_tables: list[str]) -> dict[str, int]:
        return {}

    monkeypatch.setattr(module, "_resolve_codes", _resolve)
    monkeypatch.setattr(module, "_ingest_market_text", _ingest)
    monkeypatch.setattr(module, "_refresh_stock_fund_flow", _fund_flow)
    monkeypatch.setattr(module, "_table_counts", _table_counts)
    args = Namespace(
        full=False,
        warmup_mode=True,
        scope_sources="representative,factory_targets",
        news_days=30,
        notice_days=30,
        item_limit=1,
    )

    assert asyncio.run(module._main(args)) == 1
