from __future__ import annotations

import asyncio
from datetime import datetime, timedelta


class FakeAcquire:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, *_exc):
        return False


class FakeMcp:
    def __init__(self):
        self.tools = {}

    def tool(self):
        def _register(fn):
            self.tools[fn.__name__] = fn
            return fn

        return _register


class FreshnessConn:
    def __init__(self, *, codes=None, factor_ic_date=None, factor_row_count=1, factor_count=1):
        self.codes = list(codes or [])
        self.factor_ic_date = factor_ic_date
        self.factor_row_count = factor_row_count
        self.factor_count = factor_count
        self.fetch_queries = []
        self.fetchrow_queries = []

    async def fetch(self, query: str):
        self.fetch_queries.append(query)
        return [{"code": code} for code in self.codes]

    async def fetchrow(self, query: str):
        self.fetchrow_queries.append(query)
        assert "factor_name" in query
        assert "WHERE factor =" not in query
        return {
            "max_date": self.factor_ic_date,
            "row_count": self.factor_row_count,
            "factor_count": self.factor_count,
        }


class FreshnessDb:
    def __init__(self, *, klines, conn: FreshnessConn):
        self.klines = klines
        self.conn = conn

    async def get_klines(self, code: str, limit: int = 1):
        return list(self.klines.get(code, []))[:limit]

    def acquire(self):
        return FakeAcquire(self.conn)


def _today() -> str:
    return datetime.now().date().isoformat()


def _days_ago(days: int) -> str:
    return (datetime.now() - timedelta(days=days)).date().isoformat()


def test_p0_1_readiness_ready_case_is_read_only() -> None:
    from akshare_mcp.tools.db_freshness import assess_p0_1_data_readiness

    conn = FreshnessConn(factor_ic_date=_today(), factor_row_count=4, factor_count=2)
    db = FreshnessDb(
        klines={"600519": [{"date": _today(), "close": 100.0}]},
        conn=conn,
    )

    result = asyncio.run(assess_p0_1_data_readiness(["600519"], db=db))

    assert result["ready"] is True
    assert result["kline_ready"] is True
    assert result["factor_ic_ready"] is True
    assert result["read_only"] is True
    assert result["blockers"] == []
    assert result["checked_code_count"] == 1
    assert result["factor_ic"]["row_count"] == 4
    assert conn.fetch_queries == []
    assert conn.fetchrow_queries


def test_p0_1_readiness_reports_stale_factor_ic_and_sample_blockers() -> None:
    from akshare_mcp.tools.db_freshness import assess_p0_1_data_readiness

    conn = FreshnessConn(
        codes=["600519", "000001"],
        factor_ic_date=_days_ago(30),
        factor_row_count=2,
        factor_count=1,
    )
    db = FreshnessDb(
        klines={
            "600519": [{"date": _days_ago(30), "close": 100.0}],
            "000001": [{"date": _today(), "close": 10.0}],
        },
        conn=conn,
    )

    result = asyncio.run(
        assess_p0_1_data_readiness(
            None,
            max_stale_days=5,
            factor_ic_max_stale_days=5,
            max_codes=1,
            db=db,
        )
    )

    assert result["ready"] is False
    assert result["checked_code_count"] == 1
    assert result["total_tracked_codes"] == 2
    assert result["truncated"] is True
    assert "stale_or_missing_klines" in result["blockers"]
    assert "factor_ic_stale" in result["blockers"]
    assert "sample_only" in result["blockers"]
    assert result["freshness"]["stale_count"] == 1


def test_p0_1_readiness_exempts_tdx_confirmed_nontrading_stale(monkeypatch) -> None:
    from akshare_mcp.data_source import tdx_local
    from akshare_mcp.tools.db_freshness import assess_p0_1_data_readiness

    stale_date = _days_ago(30)

    class Source:
        def get_kline(self, code: str, period: str = "daily", limit: int = 1):
            assert code == "000638"
            return [{"date": stale_date, "close": 0.89}]

    monkeypatch.setattr(tdx_local, "get_tdx_local_source", lambda: Source())

    conn = FreshnessConn(factor_ic_date=_today(), factor_row_count=2, factor_count=1)
    db = FreshnessDb(
        klines={"000638": [{"date": stale_date, "close": 0.89}]},
        conn=conn,
    )

    result = asyncio.run(
        assess_p0_1_data_readiness(
            ["000638"],
            max_stale_days=5,
            factor_ic_max_stale_days=5,
            db=db,
        )
    )

    assert result["ready"] is True
    assert result["kline_ready"] is True
    assert result["freshness"]["stale_count"] == 1
    assert result["nonblocking_stale_count"] == 1
    assert result["blocking_stale_count"] == 0
    assert result["nonblocking_stale"][0]["nonblocking_reason"] == "tdx_local_has_no_newer_bar"
    assert "stale_or_missing_klines" not in result["blockers"]


def test_check_p0_1_data_readiness_mcp_registration_and_validation(monkeypatch) -> None:
    from akshare_mcp.tools import db_freshness

    captured = {}

    async def fake_assess(codes=None, **kwargs):
        captured["codes"] = codes
        captured.update(kwargs)
        return {"ready": True, "read_only": True, "checked_code_count": len(codes or [])}

    monkeypatch.setattr(db_freshness, "assess_p0_1_data_readiness", fake_assess)

    mcp = FakeMcp()
    db_freshness.register(mcp)

    assert "check_p0_1_data_readiness" in mcp.tools
    result = asyncio.run(
        mcp.tools["check_p0_1_data_readiness"](
            codes=["600519"],
            max_stale_days=5,
            factor_ic_max_stale_days=5,
            max_codes=10,
        )
    )

    assert result["success"] is True
    assert captured["codes"] == ["600519"]
    assert captured["max_codes"] == 10

    invalid = asyncio.run(mcp.tools["check_p0_1_data_readiness"](codes=["bad-code"]))
    assert invalid["success"] is False
    assert "Invalid stock code" in invalid["error"]


class MarketTemperatureCacheDb:
    def __init__(self, cached):
        self.cached = cached
        self.requests = []

    async def get_market_temperature_snapshot_cache(self, as_of=None):
        self.requests.append(as_of)
        return self.cached


def test_market_temperature_cache_readiness_reports_fresh_cache() -> None:
    from akshare_mcp.tools.db_freshness import assess_market_temperature_cache_readiness

    db = MarketTemperatureCacheDb(
        {
            "as_of": _today(),
            "created_at": f"{_today()}T15:00:00",
            "updated_at": f"{_today()}T15:05:00",
            "market_temperature": 55.5,
            "market_state": "neutral",
            "stock_count": 300,
            "industry_count": 5,
            "quality_status": "healthy",
            "warnings": [],
            "snapshot": {
                "as_of": _today(),
                "quality": {"status": "healthy", "warnings": [], "industry_count": 5},
                "market": {"temperature": 55.5, "state": "neutral", "stock_count": 300},
            },
        }
    )

    result = asyncio.run(
        assess_market_temperature_cache_readiness(
            as_of=_today(),
            max_stale_days=1,
            db=db,
        )
    )

    assert result["ready"] is True
    assert result["status"] == "fresh"
    assert result["read_only"] is True
    assert result["quality_status"] == "healthy"
    assert result["stock_count"] == 300
    assert result["market_temperature"] == 55.5
    assert result["blockers"] == []
    assert db.requests == [_today()]


def test_market_temperature_cache_readiness_reports_stale_and_missing_cache() -> None:
    from akshare_mcp.tools.db_freshness import assess_market_temperature_cache_readiness

    stale_db = MarketTemperatureCacheDb(
        {
            "as_of": _days_ago(10),
            "market_temperature": 40.0,
            "market_state": "cool",
            "stock_count": 300,
            "industry_count": 5,
            "quality_status": "healthy",
            "warnings": [],
            "snapshot": {"quality": {"status": "healthy"}, "market": {"stock_count": 300}},
        }
    )
    stale = asyncio.run(assess_market_temperature_cache_readiness(max_stale_days=1, db=stale_db))

    assert stale["ready"] is False
    assert stale["status"] == "stale"
    assert "market_temperature_cache_stale" in stale["blockers"]

    missing = asyncio.run(
        assess_market_temperature_cache_readiness(
            as_of="2026-06-08",
            db=MarketTemperatureCacheDb(None),
        )
    )

    assert missing["ready"] is False
    assert missing["status"] == "missing"
    assert missing["blockers"] == ["market_temperature_cache_missing"]


def test_market_temperature_cache_readiness_rejects_non_finite_temperature() -> None:
    from akshare_mcp.tools.db_freshness import assess_market_temperature_cache_readiness

    db = MarketTemperatureCacheDb(
        {
            "as_of": _today(),
            "market_temperature": "inf",
            "market_state": "neutral",
            "stock_count": 300,
            "industry_count": 5,
            "quality_status": "healthy",
            "warnings": [],
            "snapshot": {
                "as_of": _today(),
                "quality": {"status": "healthy", "warnings": [], "industry_count": 5},
                "market": {"temperature": 52.5, "state": "neutral", "stock_count": 300},
            },
        }
    )

    result = asyncio.run(assess_market_temperature_cache_readiness(db=db))

    assert result["ready"] is True
    assert result["market_temperature"] == 52.5

    invalid = asyncio.run(
        assess_market_temperature_cache_readiness(
            db=MarketTemperatureCacheDb(
                {
                    "as_of": _today(),
                    "market_temperature": "inf",
                    "market_state": "neutral",
                    "stock_count": 300,
                    "industry_count": 5,
                    "quality_status": "healthy",
                    "warnings": [],
                    "snapshot": {
                        "as_of": _today(),
                        "quality": {"status": "healthy", "warnings": [], "industry_count": 5},
                        "market": {"temperature": float("nan"), "state": "neutral", "stock_count": 300},
                    },
                }
            )
        )
    )

    assert invalid["ready"] is True
    assert invalid["market_temperature"] is None


def test_check_market_temperature_cache_readiness_mcp_registration_and_validation(monkeypatch) -> None:
    from akshare_mcp.tools import db_freshness

    captured = {}

    async def fake_assess(**kwargs):
        captured.update(kwargs)
        return {"ready": True, "read_only": True, "status": "fresh"}

    monkeypatch.setattr(db_freshness, "assess_market_temperature_cache_readiness", fake_assess)

    mcp = FakeMcp()
    db_freshness.register(mcp)

    assert "check_market_temperature_cache_readiness" in mcp.tools
    result = asyncio.run(
        mcp.tools["check_market_temperature_cache_readiness"](
            as_of=_today(),
            max_stale_days=1,
        )
    )

    assert result["success"] is True
    assert captured["as_of"] == _today()
    assert captured["max_stale_days"] == 1

    invalid = asyncio.run(mcp.tools["check_market_temperature_cache_readiness"](as_of="not-a-date"))
    assert invalid["success"] is False
    assert "Invalid as_of date" in invalid["error"]
