from __future__ import annotations

import sys
import types

import pytest

from aiask_agent.adapters import akshare as akshare_adapter
from aiask_agent.session_store import AgentSessionStore
from aiask_agent.tool_registry import build_default_tool_registry
from aiask_agent.tools.schemas import TOOL_SCHEMAS


@pytest.mark.asyncio
async def test_stock_live_quote_facade_calls_akshare_quote(monkeypatch) -> None:
    quote_module = types.ModuleType("akshare_mcp.tools.market.quote")

    def get_realtime_quote(code: str = "", **kwargs):
        return {
            "success": True,
            "data": {
                "code": code,
                "price": 123.45,
                "data_timestamp": "2026-06-12T10:00:00+08:00",
                "source_chain": ["akshare", "sina"],
            },
            "error": None,
            "meta": {"source_chain": ["akshare_mcp.tools.market.quote"]},
        }

    quote_module.get_realtime_quote = get_realtime_quote
    monkeypatch.setitem(sys.modules, "akshare_mcp.tools.market.quote", quote_module)

    result = await akshare_adapter.stock_live_quote({"ticker": "600519"})

    assert result["success"] is True
    assert result["data"]["code"] == "600519"
    assert result["data"]["source_chain"] == ["akshare", "sina"]


@pytest.mark.asyncio
async def test_stock_news_digest_facade_calls_stock_or_market_news(monkeypatch) -> None:
    news_module = types.ModuleType("akshare_mcp.tools.news.news_feed")

    def get_stock_news(code: str = "", limit: int = 20, **kwargs):
        return {
            "success": True,
            "data": [{"title": "Stock news", "url": "https://example.com/stock", "provider": "example"}],
            "error": None,
        }

    def get_market_news(limit: int = 20, **kwargs):
        return {
            "success": True,
            "data": [{"title": "Market news", "url": "https://example.com/market", "provider": "example"}],
            "error": None,
        }

    news_module.get_stock_news = get_stock_news
    news_module.get_market_news = get_market_news
    monkeypatch.setitem(sys.modules, "akshare_mcp.tools.news.news_feed", news_module)

    stock = await akshare_adapter.stock_news_digest({"code": "600519", "limit": 1})
    market = await akshare_adapter.stock_news_digest({"limit": 1})

    assert stock["data"][0]["title"] == "Stock news"
    assert market["data"][0]["title"] == "Market news"


def test_realtime_finance_facades_are_registered_in_finance_safe_catalog(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("AIASK_AGENT_HOME", str(tmp_path / "home"))
    registry = build_default_tool_registry(session_store=AgentSessionStore(tmp_path / "state.sqlite3"))

    assert registry.get("agent_stock_live_quote") is not None
    assert registry.get("agent_stock_news_digest") is not None
    assert "agent_stock_live_quote" in TOOL_SCHEMAS
    assert "agent_stock_news_digest" in TOOL_SCHEMAS
