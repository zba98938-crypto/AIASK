from __future__ import annotations

from datetime import datetime, timezone

import pytest


@pytest.mark.asyncio
async def test_sqlite_diagnostic_observation_methods_accept_diagnostic_status(tmp_path):
    from aiask_quant_core.storage.sqlite import SQLiteAdapter

    db = SQLiteAdapter(path=tmp_path / "diagnostic_methods.sqlite3")
    try:
        await db.initialize()
        await db.save_strategy(
            {
                "id": "diag-1",
                "name": "Diagnostic Strategy",
                "status": "diagnostic",
                "strategy_type": "momentum",
                "params": {"diagnostic_fingerprint": "diag_same"},
                "tags": ["factory"],
            }
        )
        await db.save_strategy_incubation_account(
            "diag-1",
            "acc-1",
            stage="diagnostic",
            status="active",
            metadata={"diagnostic_fingerprint": "diag_same"},
        )

        rows = await db.list_diagnostic_observation_strategies(limit=5)
        duplicate = await db.find_active_diagnostic_observation_by_fingerprint(
            "diag_same",
            ttl_days=7,
        )

        assert [row["id"] for row in rows] == ["diag-1"]
        assert duplicate is not None
        assert duplicate["id"] == "diag-1"
        assert duplicate["diagnostic_account_id"] == "acc-1"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_sqlite_incubation_factory_health_uses_recent_events(tmp_path):
    from aiask_quant_core.storage.sqlite import SQLiteAdapter

    db = SQLiteAdapter(path=tmp_path / "diagnostic_health.sqlite3")
    try:
        await db.initialize()
        initial = await db.get_incubation_factory_health(max_age_hours=24)
        assert initial["healthy"] is False
        assert initial["stale_reason"] == "no_incubation_activity"

        await db.save_strategy_domain_event(
            {
                "strategy_id": None,
                "aggregate_type": "incubation_factory",
                "aggregate_id": "heartbeat",
                "event_type": "incubation_factory.heartbeat",
                "source": "test",
                "payload": {"ok": True},
                "created_at": datetime.now(timezone.utc),
            }
        )
        health = await db.get_incubation_factory_health(max_age_hours=24)

        assert health["healthy"] is True
        assert health["latest_event_type"] == "incubation_factory.heartbeat"
        assert health["stale_reason"] is None
    finally:
        await db.close()
