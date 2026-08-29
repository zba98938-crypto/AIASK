"""市值字段标准化(诊断报告 §4.2.4 P2-4.2.4 修复)。

历史问题:同股 601318 在不同工具的市值字段 / 单位不一致:
- get_realtime_quote: mkt_cap=5722.32 (亿元?)
- get_north_fund_top: marketCap=286.68 亿元 (元)
- get_stock_info: totalMarketCap=2091.96 亿元 (元)

修复:统一以 yuan 为 canonical 单位,响应同时返回 yuan + yi (亿元) + unit 标注。
"""
from __future__ import annotations

from typing import Any


_UNIT_MULTIPLIERS = {
    "yuan": 1.0,
    "cny": 1.0,
    "rmb": 1.0,
    "yi_yuan": 1e8,
    "亿元": 1e8,
    "yi": 1e8,
    "wan_yuan": 1e4,
    "万元": 1e4,
    "million_yuan": 1e6,
    "billion_yuan": 1e9,
}


def normalize_market_cap(
    value: Any,
    *,
    input_unit: str = "yuan",
    cap_type: str = "total",
) -> dict[str, Any]:
    """标准化市值字段。

    Args:
        value: 原始市值数值(可能是元、亿元、万元等)
        input_unit: 输入单位,默认 'yuan'
        cap_type: 市值类型,'total' / 'float' / 'north_holding' / 'free_float'

    Returns:
        dict: {
            'market_cap_yuan': float,    # canonical 元单位
            'market_cap_yi': float,      # 亿元(显示用)
            'market_cap_unit': 'yuan',
            'market_cap_type': cap_type,
            'raw_value': float,
            'raw_unit': input_unit,
        }
    """
    if value is None or value == "":
        return {
            "market_cap_yuan": None,
            "market_cap_yi": None,
            "market_cap_unit": "yuan",
            "market_cap_type": cap_type,
            "raw_value": None,
            "raw_unit": input_unit,
        }
    try:
        raw = float(value)
    except (TypeError, ValueError):
        return {
            "market_cap_yuan": None,
            "market_cap_yi": None,
            "market_cap_unit": "yuan",
            "market_cap_type": cap_type,
            "raw_value": value,
            "raw_unit": input_unit,
            "error": "value_not_numeric",
        }
    multiplier = _UNIT_MULTIPLIERS.get(str(input_unit).strip().lower(), 1.0)
    yuan = raw * multiplier
    return {
        "market_cap_yuan": round(yuan, 2),
        "market_cap_yi": round(yuan / 1e8, 4),
        "market_cap_unit": "yuan",
        "market_cap_type": cap_type,
        "raw_value": raw,
        "raw_unit": input_unit,
    }


def detect_market_cap_unit(value: Any, *, hint: str | None = None) -> str:
    """启发式检测市值单位(用于无 schema 的旧字段).

    Args:
        value: 数值
        hint: 字段名 / source 提示,如 'mkt_cap_yi' / 'totalMarketCap_yuan'

    Returns:
        unit: 'yuan' / 'yi_yuan' / 'wan_yuan'
    """
    if hint:
        h = str(hint).lower()
        if "yi" in h or "亿" in h:
            return "yi_yuan"
        if "wan" in h or "万" in h:
            return "wan_yuan"
        if "yuan" in h or "cny" in h:
            return "yuan"
    try:
        v = float(value)
    except (TypeError, ValueError):
        return "yuan"
    # A 股市值典型分布:大型 1e10~1e13 元(100亿~10万亿)
    # 若数值在 [10, 100000] → 大概率是亿元单位(10亿~10万亿)
    if 1.0 <= v <= 1e5:
        return "yi_yuan"
    if 1e9 <= v <= 1e14:
        return "yuan"
    if v > 1e6 and v < 1e9:
        return "wan_yuan"  # 万元单位
    return "yuan"  # default
