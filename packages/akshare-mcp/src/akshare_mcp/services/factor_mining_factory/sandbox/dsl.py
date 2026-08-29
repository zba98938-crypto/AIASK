"""扩展 DSL 定义 — 从 13 字段 × 13 函数扩展到 35+ × 30+。

保持向后兼容：现有 SUPPORTED_FACTOR_FIELDS/FUNCTIONS 不变，
新增字段/函数在因子挖掘工厂内部使用。
"""

from __future__ import annotations

from typing import Any

# ═══════════════════════════════════════════════════════════════════════════════
# 现有字段（保持兼容）
# ═══════════════════════════════════════════════════════════════════════════════

CORE_PRICE_VOLUME_FIELDS = {
    "open", "high", "low", "close", "volume", "amount",
    "returns_1d", "return_5d", "return_20d",
    "momentum_20d", "momentum_60d", "volatility_20d", "volume_ratio_5_20",
}

# ═══════════════════════════════════════════════════════════════════════════════
# 扩展字段（Phase 2+）
# ═══════════════════════════════════════════════════════════════════════════════

EXTENDED_LIQUIDITY_FIELDS = {
    "turnover",             # 换手率
    "turnover_5d",          # 5日平均换手率
    "amihud_illiquidity",   # Amihud 非流动性
    "vwap_deviation",       # VWAP 偏离
}

EXTENDED_FUNDAMENTAL_FIELDS = {
    "pe_ratio",             # 市盈率
    "pb_ratio",             # 市净率
    "ps_ratio",             # 市销率
    "roe",                  # 净资产收益率
    "roa",                  # 总资产收益率
    "gross_margin",         # 毛利率
    "revenue_growth",       # 营收增长率
    "profit_growth",        # 利润增长率
    "debt_ratio",           # 资产负债率
    "dividend_yield",       # 股息率
    "market_cap",           # 总市值
    "free_float_cap",       # 流通市值
}

EXTENDED_ALTERNATIVE_FIELDS = {
    "news_sentiment",       # 新闻情绪分
    "fund_flow_net",        # 资金净流入
    "north_capital_net",    # 北向资金净买入
    "margin_balance",       # 融资余额
    "institutional_hold",   # 机构持仓比例
}

EXTENDED_FIELDS = (
    CORE_PRICE_VOLUME_FIELDS
    | EXTENDED_LIQUIDITY_FIELDS
    | EXTENDED_FUNDAMENTAL_FIELDS
    | EXTENDED_ALTERNATIVE_FIELDS
)

# ═══════════════════════════════════════════════════════════════════════════════
# 现有函数（保持兼容）
# ═══════════════════════════════════════════════════════════════════════════════

CORE_FUNCTIONS = {
    "abs", "clip", "delta", "delay", "log1p", "max", "min",
    "rank", "sign", "ts_mean", "ts_rank", "ts_std", "zscore",
}

# ═══════════════════════════════════════════════════════════════════════════════
# 扩展函数（Phase 2+）
# ═══════════════════════════════════════════════════════════════════════════════

EXTENDED_TS_FUNCTIONS = {
    "ts_max",       # 滚动最大值
    "ts_min",       # 滚动最小值
    "ts_corr",      # 滚动相关系数
    "ts_cov",       # 滚动协方差
    "ts_skew",      # 滚动偏度
    "ts_kurt",      # 滚动峰度
    "ewma",         # 指数加权移动平均
    "ts_argmax",    # 滚动窗口内最大值位置
    "ts_argmin",    # 滚动窗口内最小值位置
    "ts_decay",     # 线性衰减加权
}

EXTENDED_CS_FUNCTIONS = {
    "cs_rank",      # 横截面排名
    "cs_zscore",    # 横截面标准化
    "cs_demean",    # 横截面去均值
}

EXTENDED_LOGIC_FUNCTIONS = {
    "if_else",      # 条件表达式
    "greater",      # 大于比较
    "less",         # 小于比较
}

EXTENDED_FUNCTIONS = (
    CORE_FUNCTIONS
    | EXTENDED_TS_FUNCTIONS
    | EXTENDED_CS_FUNCTIONS
    | EXTENDED_LOGIC_FUNCTIONS
)

# ═══════════════════════════════════════════════════════════════════════════════
# DSL 规范
# ═══════════════════════════════════════════════════════════════════════════════

DSL_SPEC = {
    "version": "2.0",
    "core_fields": sorted(CORE_PRICE_VOLUME_FIELDS),
    "extended_fields": sorted(EXTENDED_FIELDS - CORE_PRICE_VOLUME_FIELDS),
    "core_functions": sorted(CORE_FUNCTIONS),
    "extended_functions": sorted(EXTENDED_FUNCTIONS - CORE_FUNCTIONS),
    "all_fields": sorted(EXTENDED_FIELDS),
    "all_functions": sorted(EXTENDED_FUNCTIONS),
    "max_complexity_score": 120,
    "max_depth": 8,
    "max_execution_time_ms": 500,
    "operators": ["+", "-", "*", "/"],
    "constants": "numeric only (int, float)",
    "evaluation_semantics": [
        "expression_dsl is evaluated on a single-stock daily time-series frame",
        "ts_* operators are time-series operators over one stock history",
        "cs_* operators are cross-sectional operators (require multi-stock context)",
        "if_else(cond, true_val, false_val) for conditional expressions",
    ],
}
