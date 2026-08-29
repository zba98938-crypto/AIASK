"""新闻/研报工具包 — 拆分自 news.py

子模块:
- helpers: 私有辅助函数（去重、映射、数据源尝试）
- notices: 公告事件 (get_stock_notices)
- research: 研报/分析师/盈利预测 (get_stock_research, search_research, get_analyst_ranking, get_research_reports, get_profit_forecast)
- news_feed: 个股新闻 & 市场新闻 (get_stock_news, get_market_news)
"""

from .news_feed import get_market_news, get_stock_news
from .notices import get_stock_notices
from .research import (
    get_analyst_ranking,
    get_profit_forecast,
    get_research_reports,
    get_stock_research,
    search_research,
)


def register(mcp):
    """注册所有新闻/研报相关 MCP 工具"""
    mcp.tool()(get_stock_notices)
    mcp.tool()(get_stock_research)
    mcp.tool()(search_research)
    mcp.tool()(get_analyst_ranking)
    mcp.tool()(get_research_reports)
    mcp.tool()(get_profit_forecast)
    mcp.tool()(get_stock_news)
    mcp.tool()(get_market_news)


__all__ = [
    "register",
    "get_stock_notices",
    "get_stock_research",
    "search_research",
    "get_analyst_ranking",
    "get_research_reports",
    "get_profit_forecast",
    "get_stock_news",
    "get_market_news",
]
