import asyncio
from datetime import datetime, timedelta

from akshare_mcp.services import market_data_access
from akshare_mcp.tools.market import order_book as order_book_module


class FakeQuoteDB:
    def __init__(self, quote=None):
        self.quote = quote
        self.saved = []

    async def get_latest_quote(self, code):
        return self.quote

    async def save_quote(self, payload):
        self.saved.append(dict(payload))


def test_get_quote_snapshot_uses_fresh_db_without_live(monkeypatch):
    now = datetime.now().astimezone().isoformat()
    fake_db = FakeQuoteDB(
        {
            "code": "600519",
            "name": "Kweichow Moutai",
            "price": 1700.0,
            "change_pct": 1.2,
            "time": now,
        }
    )
    monkeypatch.setattr(market_data_access, "get_db", lambda: fake_db)

    def _unexpected_live(_code):
        raise AssertionError("live quote should not be called for a fresh DB snapshot")

    monkeypatch.setattr(market_data_access.data_source, "get_realtime_quote", _unexpected_live)

    result = asyncio.run(
        market_data_access.get_quote_snapshot(
            "600519",
            freshness_ttl=60,
            fallback_mode=market_data_access.FALLBACK_DB_FIRST_LIVE,
        )
    )

    assert result["success"] is True
    assert result["backend_used"] == "db.stock_quotes"
    assert result["fallback_used"] is False
    assert result["data"]["price"] == 1700.0
    assert fake_db.saved == []


def test_get_quote_snapshot_falls_back_and_persists_when_db_missing(monkeypatch):
    fake_db = FakeQuoteDB(None)
    monkeypatch.setattr(market_data_access, "get_db", lambda: fake_db)
    monkeypatch.setattr(
        market_data_access.data_source,
        "get_realtime_quote",
        lambda code: {"code": code, "name": "Ping An Bank", "price": 10.0, "source": "tqcenter"},
    )

    result = asyncio.run(
        market_data_access.get_quote_snapshot(
            "000001",
            freshness_ttl=60,
            fallback_mode=market_data_access.FALLBACK_DB_FIRST_LIVE,
        )
    )

    assert result["success"] is True
    assert result["backend_used"] == "tqcenter"
    assert result["fallback_used"] is True
    assert result["fallback_reason"] == ["db_snapshot_missing"]
    assert fake_db.saved and fake_db.saved[0]["code"] == "000001"


def test_get_quote_snapshot_db_only_returns_stale_db_without_live(monkeypatch):
    stale_time = (datetime.now().astimezone() - timedelta(hours=1)).isoformat()
    fake_db = FakeQuoteDB({"code": "300750", "price": 200.0, "time": stale_time})
    monkeypatch.setattr(market_data_access, "get_db", lambda: fake_db)

    def _unexpected_live(_code):
        raise AssertionError("db_only must not call live quote")

    monkeypatch.setattr(market_data_access.data_source, "get_realtime_quote", _unexpected_live)

    result = asyncio.run(
        market_data_access.get_quote_snapshot(
            "300750",
            freshness_ttl=1,
            fallback_mode=market_data_access.FALLBACK_DB_ONLY,
        )
    )

    assert result["success"] is True
    assert result["backend_used"] == "db.stock_quotes"
    assert result["stale"] is True
    assert result["fallback_reason"] == ["db_snapshot_stale"]


def test_get_order_book_defaults_to_db_degraded_depth(monkeypatch):
    monkeypatch.setattr(
        order_book_module,
        "get_quote_snapshot_sync",
        lambda code, **_kwargs: {
            "success": True,
            "data": {"code": code, "price": 1700.0, "source": "db.stock_quotes"},
            "source_chain": ["db.stock_quotes"],
            "backend_requested": "db.stock_quotes",
            "backend_used": "db.stock_quotes",
            "fallback_used": False,
            "fallback_reason": None,
            "db_snapshot_time": "2026-05-19T09:30:00+08:00",
            "data_freshness_seconds": 1.0,
            "stale": False,
        },
    )

    def _unexpected_live(_code):
        raise AssertionError("default order book should not call live quote")

    monkeypatch.setattr(order_book_module.data_source, "get_realtime_quote", _unexpected_live)

    result = order_book_module.get_order_book("600519")

    assert result["success"] is True
    assert result["data"]["source"] == "db.stock_quotes"
    assert result["data"]["depth_degraded"] is True
    assert result["backend_used"] == "db.stock_quotes"
