import asyncio
import inspect
from typing import get_args

import akshare_mcp.services.data_sync as data_sync_module
from akshare_mcp.tools import _decision_common as decision_common_module
from akshare_mcp.tools import portfolio as portfolio_module
from akshare_mcp.tools import sentiment as sentiment_module
from akshare_mcp.tools import valuation_peer as valuation_peer_module
from akshare_mcp.tools.market import kline as kline_module


def test_resolve_security_code_is_exposed_to_failed_tool_modules():
    assert callable(decision_common_module.resolve_security_code)
    assert callable(sentiment_module.resolve_security_code)


def test_analyze_portfolio_risk_accepts_numeric_portfolio_id():
    captured = {}

    class FakeMcp:
        def tool(self):
            def _register(fn):
                captured[fn.__name__] = fn
                return fn
            return _register

    portfolio_module.register(FakeMcp())
    annotation = inspect.signature(captured["analyze_portfolio_risk"]).parameters["portfolio_id"].annotation
    args = get_args(annotation)
    assert int in args
    assert str in args


def test_relative_valuation_accepts_short_metric_aliases(monkeypatch):
    class FakeDB:
        async def get_stock_info(self, code: str):
            payloads = {
                "600519": {
                    "code": "600519",
                    "name": "贵州茅台",
                    "industry": "白酒",
                    "market_cap": 2.2e12,
                    "pe_ratio": 25.0,
                    "pb_ratio": 8.0,
                },
                "000858": {
                    "code": "000858",
                    "name": "五粮液",
                    "industry": "白酒",
                    "market_cap": 9.0e11,
                    "pe_ratio": 18.0,
                    "pb_ratio": 4.2,
                },
                "600809": {
                    "code": "600809",
                    "name": "山西汾酒",
                    "industry": "白酒",
                    "market_cap": 8.5e11,
                    "pe_ratio": 22.0,
                    "pb_ratio": 5.1,
                },
            }
            return payloads.get(code)

        async def get_financials(self, code: str, limit: int = 1):
            return []

    monkeypatch.setattr(valuation_peer_module, "get_db", lambda: FakeDB())

    result = asyncio.run(
        valuation_peer_module._relative_valuation_impl(
            "600519",
            metrics=["pe", "pb"],
            peers=["000858", "600809"],
        )
    )

    assert result["success"] is True
    assert result["data"]["target_metrics"]["pe_ratio"] == 25.0
    assert result["data"]["target_metrics"]["pb_ratio"] == 8.0


def test_get_minute_kline_uses_intraday_sources_only(monkeypatch):
    def _unexpected_data_source(*_args, **_kwargs):
        raise AssertionError("data_source.get_kline should not be used for intraday fetches")

    monkeypatch.setattr(kline_module.data_source, "get_kline", _unexpected_data_source)
    monkeypatch.setattr(kline_module, "_get_minute_kline_from_akshare", lambda code, minutes, limit: [])
    monkeypatch.setattr(
        kline_module,
        "_get_minute_kline_from_sina",
        lambda code, minutes, limit: [
            {
                "date": "2026-04-23 09:31:00",
                "open": 10.0,
                "close": 10.2,
                "high": 10.3,
                "low": 9.9,
                "volume": 1000,
                "amount": 100000.0,
                "source": "sina",
            }
        ],
    )

    result = kline_module.get_minute_kline(code="600519", period="1m", limit=1)

    assert result["success"] is True
    assert result["source"] == "sina"
    assert result["source_chain"] == ["akshare.stock_zh_a_hist_min_em", "sina.getKLineData"]


def test_batch_sync_klines_times_out_per_code(monkeypatch):
    service = data_sync_module.DataSyncService()
    monkeypatch.setattr(data_sync_module, "BATCH_SYNC_PER_CODE_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(data_sync_module, "BATCH_SYNC_CONCURRENCY", 2)

    async def _fake_get_kline_with_cache(*, stock_code: str, **_kwargs):
        if stock_code == "000001":
            await asyncio.sleep(0.02)
            return {"success": True, "data": [], "source": "api"}
        await asyncio.sleep(0)
        return {"success": True, "data": [], "source": "api"}

    monkeypatch.setattr(service, "get_kline_with_cache", _fake_get_kline_with_cache)

    result = asyncio.run(service.sync_stock_klines(["600519", "000001"]))

    assert result["success"] is True
    assert result["data"]["success"] == 1
    assert result["data"]["failed"] == 1
    assert result["data"]["errors"][0]["code"] == "000001"
