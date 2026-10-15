"""Stored quality reports must say which measurement contract produced them.

The column exists because the 179 rows already in the runtime database were
written by code since fixed, and nothing distinguished them from a fresh
measurement. Two places matter and both are easy to get wrong:

* the INSERT must stamp the current contract, and
* the ``ON CONFLICT DO UPDATE`` list must include it — an upsert that omits the
  column leaves a re-computed report advertising the contract of the code that
  first wrote it, which is the opposite of what the marker is for.
"""

from __future__ import annotations

import asyncio

import pytest

from aiask_quant_core.storage.sqlite import SQLiteAdapter
from aiask_quant_core.storage.sqlite._strategy_crud_quality import (
    QUALITY_REPORT_MEASUREMENT_CONTRACT,
)


async def _adapter(path) -> SQLiteAdapter:
    db = SQLiteAdapter(path=path)
    await db.initialize()
    return db


async def _raw(db: SQLiteAdapter, sql: str, *args) -> None:
    """Run raw SQL through the adapter's connection context.

    ``SQLiteAdapter`` exposes ``acquire()``, not ``execute()``; the CRUD mixins
    use the same pattern.
    """

    async with db.acquire() as conn:
        await conn.execute(sql, *args)


async def _seed_strategy(db: SQLiteAdapter, strategy_id: str) -> None:
    """Create the row the quality report's foreign key points at."""

    await _raw(
        db,
        """
        INSERT INTO strategies (id, name, strategy_type, params, status)
        VALUES ($1, $2, $3, $4, $5)
        """,
        strategy_id,
        f"probe-{strategy_id}",
        "ma_cross",
        "{}",
        "submitted",
    )


async def _stored_contract(db: SQLiteAdapter, strategy_id: str) -> str:
    async with db.acquire() as conn:
        rows = await conn.fetch(
            "SELECT measurement_contract FROM strategy_quality_reports WHERE strategy_id = $1",
            strategy_id,
        )
    return str(rows[0]["measurement_contract"])


def test_new_report_carries_the_current_contract(tmp_path) -> None:
    async def _run() -> str:
        db = await _adapter(tmp_path / "provenance.sqlite3")
        await _seed_strategy(db, "s-1")
        await db.save_strategy_quality_report(
            "s-1", "submission", {"passed": False, "risk_report": {"var_percent": 1.0}}
        )
        return await _stored_contract(db, "s-1")

    assert asyncio.run(_run()) == QUALITY_REPORT_MEASUREMENT_CONTRACT


def test_recomputed_report_updates_the_contract(tmp_path) -> None:
    """The upsert path, which is where a missing DO UPDATE entry hides."""

    async def _run() -> tuple[str, str]:
        db = await _adapter(tmp_path / "provenance.sqlite3")
        await _seed_strategy(db, "s-2")
        await db.save_strategy_quality_report("s-2", "submission", {"passed": False})
        # Simulate a row written by older code, then re-saved by this one.
        await _raw(
            db,
            "UPDATE strategy_quality_reports SET measurement_contract = $1 WHERE strategy_id = $2",
            "pre_fix_unverified",
            "s-2",
        )
        stale = await _stored_contract(db, "s-2")
        await db.save_strategy_quality_report(
            "s-2",
            "submission",
            {"passed": True, "measurement_contract": QUALITY_REPORT_MEASUREMENT_CONTRACT},
        )
        return stale, await _stored_contract(db, "s-2")

    stale, refreshed = asyncio.run(_run())
    assert stale == "pre_fix_unverified"
    assert refreshed == QUALITY_REPORT_MEASUREMENT_CONTRACT, (
        "re-saving a report must refresh its provenance; an upsert that omits "
        "measurement_contract leaves it advertising the code that first wrote it"
    )


def test_reader_surfaces_the_contract(tmp_path) -> None:
    async def _run() -> dict:
        db = await _adapter(tmp_path / "provenance.sqlite3")
        await _seed_strategy(db, "s-3")
        await db.save_strategy_quality_report("s-3", "submission", {"passed": False})
        return await db.get_strategy_quality_report("s-3")

    report = asyncio.run(_run())
    assert report is not None
    assert report["measurement_contract"] == QUALITY_REPORT_MEASUREMENT_CONTRACT
    # The JSON columns must still decode; the new column is plain TEXT.
    assert isinstance(report["risk_report"], dict)


def test_existing_rows_default_to_unverified(tmp_path) -> None:
    """The migration default, which is what marks the historical 179 rows."""

    async def _run() -> str:
        db = await _adapter(tmp_path / "provenance.sqlite3")
        await _seed_strategy(db, "s-4")
        await _raw(
            db,
            """
            INSERT INTO strategy_quality_reports
                (strategy_id, report_type, passed, created_at, updated_at)
            VALUES ($1, $2, $3, $4, $4)
            """,
            "s-4",
            "submission",
            False,
            "2026-07-16T00:00:00+00:00",
        )
        return await _stored_contract(db, "s-4")

    assert asyncio.run(_run()) == "pre_fix_unverified", (
        "a row inserted without a contract must be marked unverified rather than "
        "silently assumed current"
    )
