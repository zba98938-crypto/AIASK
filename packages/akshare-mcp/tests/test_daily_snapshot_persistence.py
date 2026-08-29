from __future__ import annotations

import asyncio
from datetime import date

from akshare_mcp.storage.sqlite._strategy_crud_market import _StrategyCrudMarketMixin
from akshare_mcp.storage.sqlite._strategy_crud_utils import _StrategyCrudUtilsMixin


class _FakeAcquire:
    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _FakeSnapshotConn:
    def __init__(self):
        self.row = None

    async def execute(self, query: str, *args):
        if "INSERT INTO daily_snapshot_history" not in query:
            return None
        keys = (
            "snapshot_date",
            "fear_greed_index",
            "fg_components",
            "factor_ic",
            "factor_ic_trend",
            "factor_research",
            "north_fund_3d_net",
            "margin_5d_change_pct",
            "hot_sectors",
            "cold_sectors",
            "listed_count",
            "category_counts",
            "summary",
            "completeness",
            "sources",
            "parameter_distribution_samples",
            "parameter_distribution_summary",
            "failure_reasons",
            "missing_fields",
            "degraded",
        )
        self.row = dict(zip(keys, args, strict=False))
        return "INSERT 0 1"

    async def fetchrow(self, query: str, *args):
        if self.row is None:
            return None
        requested_date = args[0] if args else self.row.get("snapshot_date")
        if str(requested_date) != str(self.row.get("snapshot_date")):
            return None
        return dict(self.row)


class _FakeSnapshotDb(_StrategyCrudMarketMixin, _StrategyCrudUtilsMixin):
    def __init__(self):
        self._conn = _FakeSnapshotConn()

    def acquire(self):
        return _FakeAcquire(self._conn)


def test_daily_snapshot_roundtrip_preserves_parameter_distribution_fields():
    db = _FakeSnapshotDb()
    snapshot_date = date(2026, 4, 22)

    asyncio.run(
        db.save_daily_snapshot(
            snapshot_date,
            {
                "fear_greed_index": 61,
                "summary": {"loaded_stock_count": 5505},
                "parameter_distribution_samples": [
                    {"strategy_id": "s1", "grade": "A", "score": 0.91},
                    {"strategy_id": "s2", "grade": "B", "score": 0.72},
                ],
                "parameter_distribution_summary": {
                    "sample_count": 2,
                    "quality_query_error_count": 1,
                    "signal_query_error_count": 0,
                    "query_concurrency": 6,
                },
            },
        )
    )

    snapshot = asyncio.run(db.get_daily_snapshot(snapshot_date))

    assert snapshot is not None
    assert snapshot["fear_greed_index"] == 61
    assert snapshot["parameter_distribution_summary"]["sample_count"] == 2
    assert snapshot["parameter_distribution_summary"]["quality_query_error_count"] == 1
    assert snapshot["parameter_distribution_summary"]["query_concurrency"] == 6
    assert len(snapshot["parameter_distribution_samples"]) == 2
    assert snapshot["parameter_distribution_samples"][0]["strategy_id"] == "s1"
    assert snapshot["summary"]["loaded_stock_count"] == 5505

