from __future__ import annotations

import asyncio
import math

from akshare_mcp.services.market_temperature import build_market_temperature_snapshot
from akshare_mcp.tools import market_temperature as market_temperature_tool
from akshare_mcp.tools.tool_catalog import get_tool_contract


def _assert_all_finite(value) -> None:
    if isinstance(value, dict):
        for nested in value.values():
            _assert_all_finite(nested)
        return
    if isinstance(value, list):
        for nested in value:
            _assert_all_finite(nested)
        return
    if isinstance(value, float):
        assert math.isfinite(value)


def test_market_temperature_snapshot_ranks_hot_and_cold_industries():
    rows = [
        {"code": "000001", "date": "2026-06-08", "industry": "银行", "close": 11.0, "pct_change": 1.2, "ma20": 10.0, "amount": 100, "market_cap": 1000},
        {"code": "000002", "date": "2026-06-08", "industry": "银行", "close": 8.0, "pct_change": 0.4, "ma20": 7.5, "amount": 80, "market_cap": 800},
        {"code": "300001", "date": "2026-06-08", "industry": "计算机", "close": 20.0, "pct_change": -2.0, "ma20": 22.0, "amount": 60, "market_cap": 500},
        {"code": "300002", "date": "2026-06-08", "industry": "计算机", "close": 19.0, "pct_change": -1.0, "ma20": 21.0, "amount": 55, "market_cap": 400},
    ]

    snapshot = build_market_temperature_snapshot(rows, top_n=1)

    assert snapshot["contract_version"] == "market_temperature.v1"
    assert snapshot["market"]["stock_count"] == 4
    assert snapshot["market"]["above_ma20_count"] == 2
    assert snapshot["market"]["ma20_breadth"] == 0.5
    assert snapshot["hot_industries"][0]["name"] == "银行"
    assert snapshot["cold_industries"][0]["name"] == "计算机"
    assert snapshot["hot_industries"][0]["temperature"] > snapshot["cold_industries"][0]["temperature"]
    assert snapshot["quality"]["status"] == "healthy"


def test_market_temperature_snapshot_marks_missing_ma20_as_degraded():
    rows = [
        {"code": "000001", "date": "2026-06-08", "industry": "银行", "close": 11.0, "pct_change": 1.2},
        {"code": "000002", "date": "2026-06-08", "close": 8.0, "pct_change": -0.2},
    ]

    snapshot = build_market_temperature_snapshot(rows)

    assert snapshot["market"]["trend_known_count"] == 0
    assert snapshot["quality"]["status"] == "degraded"
    assert "low_ma20_coverage" in snapshot["quality"]["warnings"]
    assert "unknown_industry_rows" in snapshot["quality"]["warnings"]


def test_market_temperature_tool_keeps_explicit_zero_pct_change():
    latest = {"change_pct": 0, "pct_change": 1.5, "close": 10.0}
    previous = {"close": 9.0}

    assert market_temperature_tool._pct_change(latest, previous) == 0.0


class _FakeDb:
    async def list_stock_universe(self, limit: int = 300):
        return [
            {"code": "000001", "name": "平安银行", "industry": "银行", "market_cap": 1000},
            {"code": "300001", "name": "科技样本", "industry": "计算机", "market_cap": 500},
        ][:limit]

    async def get_klines(self, code: str, end_date: str | None = None, limit: int | None = None):
        base = 10.0 if code == "000001" else 20.0
        drift = 0.05 if code == "000001" else -0.08
        rows = []
        for idx in range(21):
            close = base + drift * idx
            rows.append(
                {
                    "code": code,
                    "date": f"2026-05-{10 + idx:02d}" if idx < 21 else "2026-06-08",
                    "close": close,
                    "amount": 100 + idx,
                    "turnover": 1.0,
                }
            )
        rows[-1]["date"] = "2026-06-08"
        return rows[-limit:] if limit else rows


def test_market_temperature_tool_builds_snapshot_from_db(monkeypatch):
    monkeypatch.setattr(market_temperature_tool, "get_db", lambda: _FakeDb())

    result = asyncio.run(market_temperature_tool.get_market_temperature_snapshot(limit=2, top_n=2))

    assert result["success"] is True
    assert result["data"]["market"]["stock_count"] == 2
    assert result["data"]["quality"]["loaded_stock_rows"] == 2
    assert result["meta"]["side_effect"]["level"] == "read_only"


def test_market_temperature_tool_contract_is_registered():
    contract = get_tool_contract("get_market_temperature_snapshot")

    assert contract is not None
    assert contract["side_effect"]["level"] == "read_only"
    assert contract["category"] == "market"
    assert contract["input_schema"]["properties"]["limit"]["maximum"] == 1000
    assert contract["input_schema"]["properties"]["use_cache"]["default"] is True

    refresh_contract = get_tool_contract("refresh_market_temperature_snapshot_cache")

    assert refresh_contract is not None
    assert refresh_contract["side_effect"]["level"] == "stateful"
    assert refresh_contract["category"] == "market"
    assert refresh_contract["freshness"]["expectation"] == "local_sqlite_daily_snapshot_cache_refresh"

    list_contract = get_tool_contract("list_market_temperature_snapshot_cache")

    assert list_contract is not None
    assert list_contract["side_effect"]["level"] == "read_only"
    assert list_contract["input_schema"]["properties"]["limit"]["maximum"] == 365

    industry_history_contract = get_tool_contract("list_market_temperature_industry_history")

    assert industry_history_contract is not None
    assert industry_history_contract["side_effect"]["level"] == "read_only"
    assert industry_history_contract["input_schema"]["properties"]["limit"]["maximum"] == 365
    assert industry_history_contract["input_schema"]["properties"]["top_n"]["maximum"] == 50

    constituents_contract = get_tool_contract("list_market_temperature_industry_constituents")

    assert constituents_contract is not None
    assert constituents_contract["side_effect"]["level"] == "read_only"
    assert constituents_contract["required_params"] == ["industry"]
    assert constituents_contract["input_schema"]["properties"]["limit"]["maximum"] == 1000
    assert constituents_contract["input_schema"]["required"] == ["industry"]

    forward_validation_contract = get_tool_contract("get_market_temperature_forward_validation")

    assert forward_validation_contract is not None
    assert forward_validation_contract["side_effect"]["level"] == "read_only"
    assert forward_validation_contract["input_schema"]["properties"]["limit"]["maximum"] == 365
    assert forward_validation_contract["input_schema"]["properties"]["target_field"]["default"] == "weighted_pct_change"
    assert "benchmark_return" in forward_validation_contract["input_schema"]["properties"]["target_field"]["enum"]
    assert forward_validation_contract["input_schema"]["properties"]["benchmark_code"]["default"] == "000300"


class _CacheHitDb:
    def __init__(self):
        self.cache_requests = []

    async def get_market_temperature_snapshot_cache(self, as_of=None):
        self.cache_requests.append(as_of)
        return {
            "as_of": "2026-06-08",
            "created_at": "2026-06-08T15:00:00",
            "updated_at": "2026-06-08T15:05:00",
            "snapshot": {
                "contract_version": "market_temperature.v1",
                "as_of": "2026-06-08",
                "market": {"stock_count": 2, "temperature": 60.0, "state": "neutral"},
                "industries": [
                    {"name": "bank", "temperature": 70.0, "market_cap": 1000.0},
                    {"name": "tech", "temperature": 30.0, "market_cap": 500.0},
                ],
                "quality": {"status": "healthy", "warnings": [], "industry_count": 2},
                "source_chain": ["unit.snapshot"],
            },
        }

    async def list_stock_universe(self, limit: int = 300):
        raise AssertionError("cache hits should not rebuild from the stock universe")


def test_market_temperature_tool_reads_cached_snapshot(monkeypatch):
    fake_db = _CacheHitDb()
    monkeypatch.setattr(market_temperature_tool, "get_db", lambda: fake_db)

    result = asyncio.run(
        market_temperature_tool.get_market_temperature_snapshot(
            limit=2,
            top_n=1,
            as_of="2026-06-08",
            use_cache=True,
        )
    )

    assert result["success"] is True
    assert fake_db.cache_requests == ["2026-06-08"]
    assert result["cached"] is True
    assert result["data"]["cache"]["status"] == "hit"
    assert result["data"]["quality"]["cache_status"] == "hit"
    assert [item["name"] for item in result["data"]["hot_industries"]] == ["bank"]
    assert [item["name"] for item in result["data"]["cold_industries"]] == ["tech"]
    assert result["meta"]["side_effect"]["level"] == "read_only"


class _RefreshDb(_FakeDb):
    def __init__(self):
        self.saved_snapshot = None
        self.saved_request = None
        self.saved_source_chain = None

    async def save_market_temperature_snapshot(self, snapshot, *, request=None, source_chain=None):
        self.saved_snapshot = snapshot
        self.saved_request = request
        self.saved_source_chain = source_chain
        return {
            "as_of": snapshot.get("as_of"),
            "created_at": "2026-06-08T15:00:00",
            "updated_at": "2026-06-08T15:05:00",
            "snapshot": snapshot,
            "request": request or {},
            "source_chain": source_chain or [],
        }


def test_market_temperature_refresh_writes_local_cache(monkeypatch):
    fake_db = _RefreshDb()
    monkeypatch.setattr(market_temperature_tool, "get_db", lambda: fake_db)

    result = asyncio.run(
        market_temperature_tool.refresh_market_temperature_snapshot_cache(
            limit=2,
            top_n=1,
            as_of="2026-06-08",
            min_bars=20,
        )
    )

    assert result["success"] is True
    assert fake_db.saved_snapshot is not None
    assert fake_db.saved_request == {"limit": 2, "top_n": 1, "as_of": "2026-06-08", "min_bars": 20}
    assert fake_db.saved_source_chain[-1] == "market_temperature_snapshots"
    assert result["data"]["cache"]["status"] == "written"
    assert result["data"]["quality"]["cache_status"] == "written"
    assert result["meta"]["side_effect"]["level"] == "local_state"
    assert result["meta"]["side_effect"]["target"] == "market_temperature_snapshots"


class _ListCacheDb:
    def __init__(self):
        self.requests = []

    async def list_market_temperature_snapshot_cache(self, limit: int = 30):
        self.requests.append(limit)
        return [
            {
                "as_of": "2026-06-08",
                "contract_version": "market_temperature.v1",
                "market_temperature": 55.5,
                "market_state": "neutral",
                "stock_count": 300,
                "industry_count": 5,
                "quality_status": "healthy",
                "warnings": [],
                "created_at": "2026-06-08T15:00:00",
                "updated_at": "2026-06-08T15:05:00",
                "snapshot": {
                    "as_of": "2026-06-08",
                    "market": {"temperature": 55.5, "state": "neutral"},
                    "industries": [
                        {"code": "bank", "name": "bank", "temperature": 60.0, "state": "neutral", "ma20_breadth": 0.5, "stock_count": 3},
                        {"code": "tech", "name": "tech", "temperature": 72.0, "state": "warm", "ma20_breadth": 0.75, "stock_count": 2},
                    ],
                    "quality": {"status": "healthy", "warnings": [], "industry_count": 2},
                    "source_chain": ["unit.snapshot"],
                },
            },
            {
                "as_of": "2026-06-07",
                "market_temperature": 43.2,
                "market_state": "cool",
                "stock_count": 300,
                "industry_count": 5,
                "quality_status": "degraded",
                "warnings": ["partial_kline_coverage"],
                "snapshot": {
                    "as_of": "2026-06-07",
                    "market": {"temperature": 43.2, "state": "cool"},
                    "industries": [
                        {"code": "bank", "name": "bank", "temperature": 45.0, "state": "neutral", "ma20_breadth": 0.4, "stock_count": 3},
                        {"code": "tech", "name": "tech", "temperature": 38.0, "state": "cool", "ma20_breadth": 0.3, "stock_count": 2},
                    ],
                    "quality": {"status": "degraded", "warnings": ["partial_kline_coverage"], "industry_count": 2},
                    "source_chain": ["unit.snapshot"],
                },
            },
        ][:limit]


def test_market_temperature_lists_compact_cache_history(monkeypatch):
    fake_db = _ListCacheDb()
    monkeypatch.setattr(market_temperature_tool, "get_db", lambda: fake_db)

    result = asyncio.run(
        market_temperature_tool.list_market_temperature_snapshot_cache(
            limit=1,
            include_snapshot=False,
        )
    )

    assert result["success"] is True
    assert fake_db.requests == [1]
    assert result["cached"] is True
    assert result["data"]["count"] == 1
    assert result["data"]["items"][0]["as_of"] == "2026-06-08"
    assert result["data"]["items"][0]["market_temperature"] == 55.5
    assert "snapshot" not in result["data"]["items"][0]
    assert result["meta"]["side_effect"]["level"] == "read_only"

    full = asyncio.run(
        market_temperature_tool.list_market_temperature_snapshot_cache(
            limit=1,
            include_snapshot=True,
        )
    )

    assert full["data"]["items"][0]["snapshot"]["as_of"] == "2026-06-08"


def test_market_temperature_compact_cache_rows_reject_non_finite_numbers(monkeypatch):
    class NonFiniteCacheDb:
        async def list_market_temperature_snapshot_cache(self, limit: int = 30):
            return [
                {
                    "as_of": "2026-06-08",
                    "market_temperature": "inf",
                    "stock_count": "nan",
                    "industry_count": float("inf"),
                    "snapshot": {
                        "as_of": "2026-06-08",
                        "market": {"temperature": float("nan"), "stock_count": 20},
                        "quality": {"industry_count": 2},
                    },
                }
            ]

    monkeypatch.setattr(market_temperature_tool, "get_db", lambda: NonFiniteCacheDb())

    result = asyncio.run(market_temperature_tool.list_market_temperature_snapshot_cache(limit=1))

    assert result["success"] is True
    item = result["data"]["items"][0]
    assert item["market_temperature"] is None
    assert item["stock_count"] == 20
    assert item["industry_count"] == 2
    _assert_all_finite(result)


def test_market_temperature_lists_industry_history_from_cache(monkeypatch):
    fake_db = _ListCacheDb()
    monkeypatch.setattr(market_temperature_tool, "get_db", lambda: fake_db)

    result = asyncio.run(
        market_temperature_tool.list_market_temperature_industry_history(
            industry="bank",
            limit=2,
            match_mode="exact",
            include_source_chain=True,
        )
    )

    assert result["success"] is True
    assert fake_db.requests == [2]
    assert result["cached"] is True
    assert result["data"]["count"] == 2
    assert [item["as_of"] for item in result["data"]["items"]] == ["2026-06-07", "2026-06-08"]
    assert [item["temperature"] for item in result["data"]["items"]] == [45.0, 60.0]
    assert result["data"]["items"][0]["market_temperature"] == 43.2
    assert result["data"]["items"][0]["quality_status"] == "degraded"
    assert result["data"]["items"][0]["source_chain"] == ["unit.snapshot"]
    assert result["meta"]["side_effect"]["level"] == "read_only"

    top = asyncio.run(
        market_temperature_tool.list_market_temperature_industry_history(
            limit=1,
            top_n=1,
        )
    )

    assert top["success"] is True
    assert top["data"]["count"] == 1
    assert top["data"]["items"][0]["name"] == "tech"


class _IndustryConstituentDb:
    def __init__(self):
        self.requests = []

    async def list_stock_universe(self, limit: int = 200, offset: int = 0, industry: str | None = None):
        self.requests.append({"limit": limit, "offset": offset, "industry": industry})
        rows = [
            {
                "code": "000001",
                "name": "Ping An Bank",
                "industry": "bank",
                "sector": "bank",
                "market": "SZ",
                "market_cap": 3120.8,
                "pe_ratio": 5.4,
                "pb_ratio": 0.62,
                "list_date": "1991-04-03",
            },
            {
                "code": "600036",
                "name": "CMB",
                "industry": "bank",
                "sector": "bank",
                "market": "SH",
                "market_cap": 4010.2,
                "pe_ratio": 6.1,
                "pb_ratio": 0.81,
                "list_date": "2002-04-09",
            },
            {
                "code": "300001",
                "name": "Tech Sample",
                "industry": "computer",
                "sector": "computer",
                "market": "SZ",
                "market_cap": 500.0,
            },
        ]
        if industry:
            needle = industry.lower()
            rows = [row for row in rows if needle in str(row.get("industry") or "").lower()]
        return rows[:limit]


def test_market_temperature_lists_industry_constituents_from_stock_universe(monkeypatch):
    fake_db = _IndustryConstituentDb()
    monkeypatch.setattr(market_temperature_tool, "get_db", lambda: fake_db)

    result = asyncio.run(
        market_temperature_tool.list_market_temperature_industry_constituents(
            industry="bank",
            limit=1,
            offset=1,
            match_mode="contains",
            include_source_chain=True,
        )
    )

    assert result["success"] is True
    assert fake_db.requests == [{"limit": 6, "offset": 0, "industry": "bank"}]
    assert result["cached"] is True
    assert result["data"]["count"] == 1
    assert result["data"]["total_matches"] == 2
    assert result["data"]["items"][0]["code"] == "600036"
    assert result["data"]["items"][0]["industry"] == "bank"
    assert result["data"]["items"][0]["source_chain"] == ["db.stocks"]
    assert result["meta"]["side_effect"]["level"] == "read_only"

    missing = asyncio.run(
        market_temperature_tool.list_market_temperature_industry_constituents(
            industry="",
        )
    )

    assert missing["success"] is False
    assert missing["error_code"] == "PARAM_ERROR"


class _ForwardValidationDb:
    def __init__(self):
        self.benchmark_requests = []

    async def list_market_temperature_snapshot_cache(self, limit: int = 30):
        rows = []
        specs = [
            ("2026-06-01", "warm", 70.0, 0.8),
            ("2026-06-02", "warm", 68.0, 0.4),
            ("2026-06-03", "neutral", 55.0, 0.1),
            ("2026-06-04", "cool", 35.0, -0.5),
            ("2026-06-05", "cool", 30.0, -0.2),
            ("2026-06-06", "neutral", 50.0, 0.0),
        ]
        for as_of, state, temperature, weighted_pct_change in specs:
            rows.append(
                {
                    "as_of": as_of,
                    "market_temperature": temperature,
                    "market_state": state,
                    "quality_status": "healthy",
                    "warnings": [],
                    "snapshot": {
                        "as_of": as_of,
                        "market": {
                            "temperature": temperature,
                            "state": state,
                            "weighted_pct_change": weighted_pct_change,
                            "avg_pct_change": weighted_pct_change / 2,
                        },
                        "quality": {"status": "healthy", "warnings": []},
                    },
                }
            )
        return list(reversed(rows))[:limit]

    async def get_index_klines(self, code: str, start_date: str | None = None, end_date: str | None = None):
        self.benchmark_requests.append({"code": code, "start_date": start_date, "end_date": end_date})
        closes = [100.0, 102.0, 104.0, 103.0, 101.0, 100.0, 99.0, 98.0]
        return [
            {"date": f"2026-06-{index + 1:02d}", "close": close}
            for index, close in enumerate(closes)
        ]


def test_market_temperature_forward_validation_matrix_from_cache(monkeypatch):
    monkeypatch.setattr(market_temperature_tool, "get_db", lambda: _ForwardValidationDb())

    result = asyncio.run(
        market_temperature_tool.get_market_temperature_forward_validation(
            limit=6,
            horizons=[1, 2],
            min_samples=2,
            neutral_band_pct=0.2,
            include_samples=True,
        )
    )

    assert result["success"] is True
    assert result["cached"] is True
    assert result["data"]["snapshot_count"] == 6
    assert result["data"]["horizons"] == [1, 2]
    assert result["data"]["target_field"] == "weighted_pct_change"
    assert result["data"]["matrix"]["warm"]["1d"]["sample_n"] == 2
    assert result["data"]["matrix"]["warm"]["1d"]["hit_rate"] == 1.0
    assert result["data"]["matrix"]["cool"]["1d"]["sample_n"] == 2
    assert result["data"]["matrix"]["cool"]["1d"]["hit_rate"] == 0.5
    assert result["data"]["matrix"]["neutral"]["1d"]["direction_hits"] == 0
    assert result["data"]["samples"][0]["as_of"] == "2026-06-01"
    assert result["meta"]["side_effect"]["level"] == "read_only"


def test_market_temperature_forward_validation_uses_benchmark_klines(monkeypatch):
    fake_db = _ForwardValidationDb()
    monkeypatch.setattr(market_temperature_tool, "get_db", lambda: fake_db)

    result = asyncio.run(
        market_temperature_tool.get_market_temperature_forward_validation(
            limit=6,
            horizons=[1],
            target_field="benchmark_return",
            benchmark_code="000300",
            min_samples=2,
            include_samples=True,
        )
    )

    assert result["success"] is True
    assert fake_db.benchmark_requests[0]["code"] == "000300"
    assert fake_db.benchmark_requests[0]["start_date"] == "2026-06-01"
    assert result["data"]["target_field"] == "benchmark_return"
    assert result["data"]["requested_target_field"] == "benchmark_return"
    assert result["data"]["benchmark_status"] == "available"
    assert result["data"]["benchmark_bar_count"] == 8
    assert result["data"]["matrix"]["warm"]["1d"]["hit_rate"] == 1.0
    assert result["data"]["matrix"]["cool"]["1d"]["hit_rate"] == 1.0
    assert result["data"]["samples"][0]["benchmark_as_of"] == "2026-06-01"
    assert "db.kline_1d" in result["data"]["source_chain"]
    assert result["meta"]["quality"]["benchmark_status"] == "available"


def test_market_temperature_forward_validation_reports_unsupported_target_fallback(monkeypatch):
    monkeypatch.setattr(market_temperature_tool, "get_db", lambda: _ForwardValidationDb())

    result = asyncio.run(
        market_temperature_tool.get_market_temperature_forward_validation(
            limit=6,
            horizons=[1],
            target_field="unsupported_signal",
            min_samples=2,
        )
    )

    assert result["success"] is True
    assert result["data"]["target_field"] == "weighted_pct_change"
    assert result["data"]["requested_target_field"] == "unsupported_signal"
    assert result["meta"]["degraded"] is True
    assert "unsupported_target_field_fallback_to_weighted_pct_change" in result["meta"]["quality"]["warnings"]


def test_market_temperature_forward_validation_filters_non_finite_samples(monkeypatch):
    class NonFiniteForwardDb:
        async def list_market_temperature_snapshot_cache(self, limit: int = 30):
            return [
                {
                    "as_of": "2026-06-03",
                    "market_temperature": 55.0,
                    "market_state": "warm",
                    "snapshot": {
                        "as_of": "2026-06-03",
                        "market": {"temperature": 55.0, "state": "warm", "weighted_pct_change": 0.5},
                    },
                },
                {
                    "as_of": "2026-06-02",
                    "market_temperature": "inf",
                    "market_state": "warm",
                    "snapshot": {
                        "as_of": "2026-06-02",
                        "market": {"temperature": float("nan"), "state": "warm", "weighted_pct_change": "inf"},
                    },
                },
                {
                    "as_of": "2026-06-01",
                    "market_temperature": 50.0,
                    "market_state": "warm",
                    "snapshot": {
                        "as_of": "2026-06-01",
                        "market": {"temperature": 50.0, "state": "warm", "weighted_pct_change": 0.3},
                    },
                },
            ]

    monkeypatch.setattr(market_temperature_tool, "get_db", lambda: NonFiniteForwardDb())

    result = asyncio.run(
        market_temperature_tool.get_market_temperature_forward_validation(
            limit=3,
            horizons=[1, float("inf"), "nan"],
            neutral_band_pct=float("inf"),
            include_samples=True,
        )
    )

    assert result["success"] is True
    assert result["data"]["horizons"] == [1]
    assert result["data"]["neutral_band_pct"] == 0.0
    assert result["data"]["snapshot_count"] == 2
    assert result["data"]["samples"][0]["forward_return"] == 0.5
    _assert_all_finite(result)
