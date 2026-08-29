from __future__ import annotations

from copy import deepcopy

import pytest


class _FakeDB:
    def __init__(self):
        self.submitted_query_called = False
        self.legacy_submitted_called = False

    async def list_strategies(self, status, limit=500, offset=0):
        if status == "submitted":
            self.legacy_submitted_called = True
            raise AssertionError("legacy submitted scan should not be used when the dedup-specific query exists")
        return []

    async def list_submitted_strategies_for_dedup(self, limit=500, offset=0):
        self.submitted_query_called = True
        return []


def _candidate() -> dict:
    return {
        "id": "candidate-1",
        "name": "601288·均线交叉·快6慢30",
        "strategy_type": "ma_cross",
        "params": {
            "short_period": 6,
            "long_period": 30,
            "target_symbols": ["601288"],
            "stock_pool": {"symbols": ["601288"]},
            "target_pool_source": "explicit",
            "target_symbol_count": 1,
            "holding_period_bucket": "medium",
            "generator_mode": "snapshot",
            "candidate_family": "ma_cross",
            "task_signature": "snapshot||||candidate_target_only|601288",
            "tested_object_hash": "observe-backlog-hash",
            "candidate_identity_signature": "observe-backlog-signature",
        },
        "target_symbols": ["601288"],
        "stock_pool": {"symbols": ["601288"]},
        "tags": [],
    }


@pytest.mark.asyncio
async def test_deduplicator_uses_submitted_query_that_excludes_active_observe_backlog(monkeypatch):
    from strategy_factory.application.deduplicator import Deduplicator

    async def _noop_prewarm(self, candidates, db):
        return None

    monkeypatch.setattr(Deduplicator, "_prewarm_candidate_behaviors", _noop_prewarm)

    db = _FakeDB()
    dedup = Deduplicator()

    result = await dedup.deduplicate([deepcopy(_candidate())], db)

    assert len(result) == 1
    assert db.submitted_query_called is True
    assert db.legacy_submitted_called is False
    assert result[0]["dedup_result"]["duplicate"] is False
    assert dedup.last_report["summary"]["kept_count"] == 1
    assert dedup.last_report["summary"]["dropped_count"] == 0
