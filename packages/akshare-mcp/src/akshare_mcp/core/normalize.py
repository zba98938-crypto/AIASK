"""
统一字段归一化模块
将 MCP 工具输出的各种字段名（snake_case / camelCase / 中文）归一化为 camelCase。
BFF 层可直接透传，无需二次归一化。
"""

from __future__ import annotations
from typing import Any

# ── 别名映射：{canonical_camelCase: [可能的源字段名]} ──

QUOTE_ALIASES: dict[str, list[str]] = {
    "code": ["code", "stock_code", "symbol"],
    "name": ["name", "stock_name"],
    "price": ["price", "current", "close", "Close", "\u6700\u65b0\u4ef7"],
    "change": ["change", "price_change", "change_amt"],
    "changePercent": [
        "changePercent", "change_percent", "pct_change",
        "pct_chg", "change_pct", "\u6da8\u8dcc\u5e45",
    ],
    "volume": ["volume", "vol", "Volume", "\u6210\u4ea4\u91cf"],
    "amount": ["amount", "turnover", "Amount", "\u6210\u4ea4\u989d"],
    "high": ["high", "High", "\u6700\u9ad8"],
    "low": ["low", "Low", "\u6700\u4f4e"],
    "open": ["open", "Open", "\u4eca\u5f00"],
    "prevClose": ["prevClose", "preClose", "prev_close", "pre_close", "\u6628\u6536"],
    "timestamp": ["timestamp", "date", "trade_date"],
}

KLINE_ALIASES: dict[str, list[str]] = {
    "date": ["date", "Date", "trade_date"],
    "open": ["open", "Open"],
    "close": ["close", "Close"],
    "high": ["high", "High"],
    "low": ["low", "Low"],
    "volume": ["volume", "vol", "Volume"],
}

FUND_FLOW_ALIASES: dict[str, list[str]] = {
    "date": ["date", "trade_date", "Date"],
    "name": ["name", "sector_name", "concept_name"],
    "netInflow": ["netInflow", "net_inflow", "net_amount", "value", "mainNetInflow"],
    "mainInflow": ["mainInflow", "main_inflow", "main_net_inflow"],
    "mainOutflow": ["mainOutflow", "main_outflow"],
    "retailInflow": ["retailInflow", "retail_inflow"],
    "retailOutflow": ["retailOutflow", "retail_outflow"],
}
BLOCK_ALIASES: dict[str, list[str]] = {
    "code": ["block_code", "code"],
    "name": ["block_name", "name"],
    "stockCount": ["stock_count", "stockCount", "stocks_count"],
    "avgChange": ["avg_change_pct", "avgChange"],
    "leaderCode": ["leader_code", "leaderCode"],
    "leaderName": ["leader_name", "leaderName"],
    # P3-5.11 fix: 双命名空间 alias 映射(诊断报告 §5.11)
    # 历史问题:不同 manager 用 blockCode 或 block_code 不一致,AI 调 get_block_stocks 时常 422
    # 修复:输出层同时暴露 camelCase + snake_case 字段
    "blockCode": ["block_code", "code", "blockCode"],
    "blockName": ["block_name", "name", "blockName"],
    "blockType": ["block_type", "blockType"],
}

BLOCK_STOCK_ALIASES: dict[str, list[str]] = {
    "code": ["stock_code", "code"],
    "name": ["stock_name", "name"],
    "price": ["price"],
    "changePercent": ["change_pct", "changePercent"],
}

DRAGON_TIGER_ALIASES: dict[str, list[str]] = {
    "code": ["code"],
    "name": ["name"],
    "closePrice": ["closePrice", "close_price", "price"],
    "changePercent": ["changePercent", "change_percent", "change_pct"],
    "reason": ["reason"],
    "buyAmount": ["buyAmount", "buy_amount"],
    "sellAmount": ["sellAmount", "sell_amount"],
    "netAmount": ["netAmount", "net_amount", "net_buy"],
}

MARGIN_ALIASES: dict[str, list[str]] = {
    "date": ["date"],
    "code": ["code"],
    "name": ["name"],
    "marginBalance": ["marginBalance", "margin_balance"],
    "marginBuy": ["marginBuy", "margin_buy"],
    "shortBalance": ["shortBalance", "short_balance"],
    "totalBalance": ["totalBalance", "total_balance"],
}

LIMIT_UP_ALIASES: dict[str, list[str]] = {
    "code": ["code"],
    "name": ["name"],
    "price": ["price"],
    "changePercent": ["changePercent", "change_percent", "pct_chg"],
    "continuousDays": ["continuousDays", "continuous_days"],
    "industry": ["industry"],
}

LIMIT_UP_STAT_ALIASES: dict[str, list[str]] = {
    "totalLimitUp": ["totalLimitUp", "total_limit_up"],
    "firstBoard": ["firstBoard", "first_board"],
    "secondBoard": ["secondBoard", "second_board"],
    "failedBoard": ["failedBoard", "failed_board"],
    "limitDown": ["limitDown", "limit_down"],
    "successRate": ["successRate", "success_rate"],
    "date": ["date"],
}


# ── 通用归一化函数 ──

def normalize_record(record: Any, alias_map: dict[str, list[str]]) -> dict[str, Any]:
    """将 record 按 alias_map 归一化为 camelCase 字段名。"""
    if not isinstance(record, dict):
        return {}
    out: dict[str, Any] = {}
    for canonical, aliases in alias_map.items():
        for alias in aliases:
            if alias in record and record[alias] is not None:
                out[canonical] = record[alias]
                break
        if canonical not in out:
            out[canonical] = None
    return out


def normalize_list(records: Any, alias_map: dict[str, list[str]]) -> list[dict[str, Any]]:
    """对列表中每条记录执行 normalize_record。"""
    if not isinstance(records, list):
        return []
    return [normalize_record(r, alias_map) for r in records]


# ── 便捷函数 ──

def normalize_quote(data: Any) -> dict[str, Any]:
    return normalize_record(data, QUOTE_ALIASES)

def normalize_kline(data: Any) -> dict[str, Any]:
    return normalize_record(data, KLINE_ALIASES)

def normalize_kline_list(data_list: Any) -> list[dict[str, Any]]:
    return normalize_list(data_list, KLINE_ALIASES)

def normalize_fund_flow_list(data_list: Any) -> list[dict[str, Any]]:
    return normalize_list(data_list, FUND_FLOW_ALIASES)

def normalize_block(data: Any) -> dict[str, Any]:
    return normalize_record(data, BLOCK_ALIASES)

def normalize_block_list(data_list: Any) -> list[dict[str, Any]]:
    return normalize_list(data_list, BLOCK_ALIASES)

def normalize_block_stock_list(data_list: Any) -> list[dict[str, Any]]:
    return normalize_list(data_list, BLOCK_STOCK_ALIASES)

def normalize_dragon_tiger_list(data_list: Any) -> list[dict[str, Any]]:
    return normalize_list(data_list, DRAGON_TIGER_ALIASES)

def normalize_margin_list(data_list: Any) -> list[dict[str, Any]]:
    return normalize_list(data_list, MARGIN_ALIASES)

def normalize_limit_up_list(data_list: Any) -> list[dict[str, Any]]:
    return normalize_list(data_list, LIMIT_UP_ALIASES)

def normalize_limit_up_stat(data: Any) -> dict[str, Any]:
    return normalize_record(data, LIMIT_UP_STAT_ALIASES)
