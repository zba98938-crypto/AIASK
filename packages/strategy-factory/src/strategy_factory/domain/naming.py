"""策略工厂命名工具。"""

from __future__ import annotations

from typing import Any


def _resolve_target_stock_label(params: dict[str, Any]) -> str:
    """从参数中提取目标股票标签。"""
    # 尝试从 research_task 获取股票名
    research_task = params.get("research_task") or {}
    title = str(research_task.get("title") or "").strip()
    # "逐股策略矩阵·中兴通讯·multi_factor" → "中兴通讯"
    if "·" in title:
        parts = title.split("·")
        if len(parts) >= 2:
            stock_name = parts[1].strip()
            if stock_name and len(stock_name) <= 8:
                return stock_name

    # 尝试从 target_symbols 获取
    target_symbols = params.get("target_symbols") or []
    if isinstance(target_symbols, list) and len(target_symbols) == 1:
        return target_symbols[0]
    if isinstance(target_symbols, list) and 1 < len(target_symbols) <= 3:
        return "+".join(target_symbols[:2])

    # 尝试从 stock_pool 获取
    stock_pool = params.get("stock_pool") or {}
    symbols = stock_pool.get("symbols") or []
    if isinstance(symbols, list) and len(symbols) == 1:
        return symbols[0]

    return ""


def _resolve_style_label(stype: str, params: dict[str, Any]) -> str:
    """从因子权重推断策略风格。"""
    factor_weights = params.get("factor_weights") or {}
    if not factor_weights:
        return ""

    # 找主导因子
    top_factor = max(factor_weights, key=factor_weights.get, default="")
    top_weight = factor_weights.get(top_factor, 0)

    style_map = {
        "momentum": "动量",
        "value": "价值",
        "quality": "质量",
        "growth": "成长",
        "volatility": "波动",
        "reversal": "反转",
    }

    if top_weight >= 0.4:
        return f"{style_map.get(top_factor, top_factor)}主导"
    return "均衡"


def _auto_name(stype: str, params: dict) -> str:
    """从策略类型+参数生成中文名。包含目标股票和策略特征。"""
    target = _resolve_target_stock_label(params)
    prefix = f"{target}·" if target else ""

    if stype == "ma_cross":
        return f"{prefix}均线交叉·快{params.get('short_period', 5)}慢{params.get('long_period', 20)}"
    if stype == "momentum":
        lookback = params.get("lookback", 20)
        return f"{prefix}动量突破·{lookback}日"
    if stype == "rsi":
        return f"{prefix}RSI反转·{params.get('period', params.get('rsi_period', 14))}日"
    if stype == "volatility_breakout":
        return f"{prefix}波动突破·{params.get('vol_window', params.get('lookback', 20))}日"
    if stype == "event_structure_breakout":
        return f"{prefix}事件结构突破·{params.get('breakout_window', params.get('lookback', 16))}日"
    if stype == "gap_fill":
        return f"{prefix}缺口回补·{params.get('rsi_period', 5)}日"
    if stype == "mean_reversion_short":
        return f"{prefix}短反转·{params.get('rsi_period', params.get('lookback', 6))}日"
    if stype == "value_factor":
        return f"{prefix}价值精选·{params.get('rebalance_days', 20)}日轮动"
    if stype == "quality_factor":
        return f"{prefix}质量优选·{params.get('rebalance_days', 20)}日轮动"
    if stype == "growth_factor":
        return f"{prefix}成长优选·{params.get('rebalance_days', 20)}日轮动"
    if stype == "multi_factor":
        style = _resolve_style_label(stype, params)
        lookback = params.get("lookback", "")
        period_label = f"({lookback}日)" if lookback else ""
        return f"{prefix}多因子{style}{period_label}"
    if stype == "macro_timing":
        return f"{prefix}宏观择时·恐贪({params.get('fear_threshold', 35)}/{params.get('greed_threshold', 65)})"
    if stype == "sector_rotation":
        return f"{prefix}板块轮动·{params.get('lookback', 20)}日"
    if stype == "north_capital_track":
        return f"{prefix}北向跟踪·{params.get('lookback', 15)}日"
    if stype == "margin_divergence":
        return f"{prefix}两融背离·{params.get('lookback', 12)}日"
    if stype == "topn_equity_portfolio":
        return f"全市场 Top {params.get('top_n', 20)} 组合策略"
    return f"{prefix}{stype}策略"
