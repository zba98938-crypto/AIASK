"""语义分析工具包 — 拆分自 semantic.py

子模块:
- query_parser: 自然语言选股查询解析 (parse_selection_query)
- industry_chain: 产业链信息 (get_industry_chain)
- diagnosis: 智能股票诊断 (smart_stock_diagnosis)
- daily_report: 每日市场报告 (generate_daily_report)
"""

from .query_parser import parse_selection_query
from .industry_chain import get_industry_chain
from .diagnosis import smart_stock_diagnosis
from .daily_report import generate_daily_report


def register(mcp):
    """注册所有语义分析相关 MCP 工具"""
    mcp.tool()(parse_selection_query)
    mcp.tool()(get_industry_chain)
    mcp.tool()(smart_stock_diagnosis)
    mcp.tool()(generate_daily_report)


__all__ = [
    "register",
    "parse_selection_query",
    "get_industry_chain",
    "smart_stock_diagnosis",
    "generate_daily_report",
]
