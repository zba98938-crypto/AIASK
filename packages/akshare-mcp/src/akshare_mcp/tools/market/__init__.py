"""市场数据工具模块 - 统一导出"""

from .quote import get_realtime_quote, get_batch_quotes, get_batch_quotes_compat, get_index_quote
from .kline import get_kline, get_minute_kline, get_kline_data
from .stock_list import get_stock_list
from .order_book import get_order_book, get_trade_details
from .limit_up import get_limit_up_stocks, get_limit_up_statistics

__all__ = [
    'get_stock_list',
    'get_realtime_quote',
    'get_batch_quotes',
    'get_batch_quotes_compat',
    'get_index_quote',
    'get_kline',
    'get_minute_kline',
    'get_kline_data',
    'get_order_book',
    'get_trade_details',
    'get_limit_up_stocks',
    'get_limit_up_statistics',
    'register'
]


def register(mcp):
    """注册所有市场数据工具"""
    mcp.tool()(get_stock_list)
    mcp.tool()(get_realtime_quote)
    mcp.tool()(get_batch_quotes)
    mcp.tool()(get_kline)
    mcp.tool()(get_minute_kline)
    mcp.tool()(get_index_quote)
    mcp.tool()(get_order_book)
    mcp.tool()(get_trade_details)
    mcp.tool()(get_limit_up_stocks)
    mcp.tool()(get_limit_up_statistics)
    # 兼容Node.js版本的工具
    mcp.tool()(get_kline_data)
    mcp.tool()(get_batch_quotes_compat)
