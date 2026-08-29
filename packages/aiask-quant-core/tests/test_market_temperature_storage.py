from __future__ import annotations

import asyncio
import sqlite3

from aiask_quant_core.storage.sqlite import SQLiteAdapter


def _snapshot() -> dict:
    return {
        "contract_version": "market_temperature.v1",
        "as_of": "2026-06-08",
        "market": {
            "stock_count": 2,
            "above_ma20_count": 1,
            "ma20_breadth": 0.5,
            "advance_count": 1,
            "decline_count": 1,
            "temperature": 55.5,
            "state": "neutral",
        },
        "industries": [
            {
                "code": "bank",
                "name": "bank",
                "stock_count": 1,
                "temperature": 66.0,
                "state": "warm",
            }
        ],
        "hot_industries": [{"name": "bank", "temperature": 66.0}],
        "cold_industries": [{"name": "tech", "temperature": 30.0}],
        "quality": {
            "status": "healthy",
            "warnings": [],
            "industry_count": 1,
            "contract_version": "market_temperature.v1",
        },
        "source_chain": ["unit.test"],
    }


def test_market_temperature_snapshot_cache_round_trips(tmp_path) -> None:
    db_path = tmp_path / "market_temperature.sqlite3"
    db = SQLiteAdapter(path=db_path)

    async def _run() -> None:
        try:
            await db.initialize()
            saved = await db.save_market_temperature_snapshot(
                _snapshot(),
                request={"limit": 2, "top_n": 1},
                source_chain=["unit.test", "market_temperature_snapshots"],
            )
            cached = await db.get_market_temperature_snapshot_cache("2026-06-08")
            latest = await db.get_market_temperature_snapshot_cache()
            listed = await db.list_market_temperature_snapshot_cache(limit=5)

            assert saved["as_of"] == "2026-06-08"
            assert cached is not None
            assert latest is not None
            assert cached["snapshot"]["market"]["temperature"] == 55.5
            assert cached["snapshot"]["source_chain"] == ["unit.test", "market_temperature_snapshots"]
            assert cached["request"] == {"limit": 2, "top_n": 1}
            assert cached["source_chain"] == ["unit.test", "market_temperature_snapshots"]
            assert cached["quality_status"] == "healthy"
            assert cached["market_state"] == "neutral"
            assert cached["stock_count"] == 2
            assert listed[0]["as_of"] == "2026-06-08"
        finally:
            await db.close()

    asyncio.run(_run())

    conn = sqlite3.connect(str(db_path))
    try:
        names = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table', 'index')"
            ).fetchall()
        }
    finally:
        conn.close()

    assert "market_temperature_snapshots" in names
    assert "idx_market_temperature_quality" in names
