"""Quant factor definitions, constants, and configuration utilities."""

import math
import os
from typing import Any, Dict


# ── Supported factor registry ──────────────────────────────────

SUPPORTED_FACTORS: Dict[str, Dict[str, Any]] = {
    "momentum": {
        "category": "technical",
        "description": "动量因子",
        "requires_financials": False,
        "sub_factors": ["return_20d", "return_60d", "trend_strength"],
        "aliases": ["mom", "mtm", "price_momentum"],
    },
    "trend": {
        "category": "technical",
        "description": "趋势因子",
        "requires_financials": False,
        "sub_factors": ["ma20_slope", "ma60_slope", "price_above_ma"],
        "aliases": ["ma_trend", "trend_strength", "moving_trend"],
    },
    "reversal": {
        "category": "technical",
        "description": "反转因子",
        "requires_financials": False,
        "sub_factors": ["short_term_reversal", "oversold_rebound"],
        "aliases": ["mean_reversion", "rev", "revert"],
    },
    "volatility": {
        "category": "risk",
        "description": "波动率因子",
        "requires_financials": False,
        "sub_factors": ["realized_vol_20d", "atr_proxy"],
        "aliases": ["vol", "risk_volatility", "sigma"],
    },
    "value": {
        "category": "fundamental",
        "description": "价值因子",
        "requires_financials": True,
        "sub_factors": ["pe", "pb", "ps"],
        "aliases": ["valuation", "cheapness", "value_score"],
    },
    "quality": {
        "category": "fundamental",
        "description": "质量因子",
        "requires_financials": True,
        "sub_factors": ["roe", "debt_ratio", "profit_growth"],
        "aliases": ["profitability", "quality_score", "high_quality"],
    },
    "growth": {
        "category": "fundamental",
        "description": "成长因子",
        "requires_financials": True,
        "sub_factors": ["revenue_growth", "profit_growth", "eps_growth"],
        "aliases": ["growth_score", "earnings_growth", "sales_growth"],
    },
    "size": {
        "category": "fundamental",
        "description": "规模因子",
        "requires_financials": False,
        "sub_factors": ["market_cap", "float_market_cap"],
        "aliases": ["market_cap", "small_cap", "size_score"],
    },
    # ── 技术类扩展 ──
    "mom_1d": {
        "category": "technical",
        "description": "1日动量",
        "requires_financials": False,
        "sub_factors": ["return_1d"],
        "aliases": ["daily_return"],
    },
    "mom_5d": {
        "category": "technical",
        "description": "5日动量",
        "requires_financials": False,
        "sub_factors": ["return_5d"],
        "aliases": ["weekly_momentum"],
    },
    "mom_10d": {
        "category": "technical",
        "description": "10日动量",
        "requires_financials": False,
        "sub_factors": ["return_10d"],
        "aliases": ["biweekly_momentum"],
    },
    "mom_60d": {
        "category": "technical",
        "description": "60日动量",
        "requires_financials": False,
        "sub_factors": ["return_60d"],
        "aliases": ["quarterly_momentum"],
    },
    "rsi_14": {
        "category": "technical",
        "description": "14日RSI",
        "requires_financials": False,
        "sub_factors": ["rsi"],
        "aliases": ["rsi", "relative_strength"],
    },
    "rsi_6": {
        "category": "technical",
        "description": "6日RSI",
        "requires_financials": False,
        "sub_factors": ["rsi_short"],
        "aliases": ["rsi_fast"],
    },
    "macd_signal": {
        "category": "technical",
        "description": "MACD信号线",
        "requires_financials": False,
        "sub_factors": ["macd", "signal_line"],
        "aliases": ["macd"],
    },
    "macd_histogram": {
        "category": "technical",
        "description": "MACD柱状图",
        "requires_financials": False,
        "sub_factors": ["macd_hist"],
        "aliases": ["macd_bar"],
    },
    "willr_14": {
        "category": "technical",
        "description": "14日威廉指标",
        "requires_financials": False,
        "sub_factors": ["williams_r"],
        "aliases": ["williams_r", "wr"],
    },
    "cci_20": {
        "category": "technical",
        "description": "20日CCI",
        "requires_financials": False,
        "sub_factors": ["cci"],
        "aliases": ["commodity_channel"],
    },
    "mfi_14": {
        "category": "technical",
        "description": "14日资金流量指标",
        "requires_financials": False,
        "sub_factors": ["money_flow"],
        "aliases": ["money_flow_index"],
    },
    "stoch_k": {
        "category": "technical",
        "description": "随机指标K值",
        "requires_financials": False,
        "sub_factors": ["stochastic_k"],
        "aliases": ["kdj_k"],
    },
    "stoch_d": {
        "category": "technical",
        "description": "随机指标D值",
        "requires_financials": False,
        "sub_factors": ["stochastic_d"],
        "aliases": ["kdj_d"],
    },
    "roc_10": {
        "category": "technical",
        "description": "10日变动率",
        "requires_financials": False,
        "sub_factors": ["rate_of_change"],
        "aliases": ["roc"],
    },
    "roc_20": {
        "category": "technical",
        "description": "20日变动率",
        "requires_financials": False,
        "sub_factors": ["rate_of_change_20"],
        "aliases": ["roc_monthly"],
    },
    # ── 波动类扩展 ──
    "vol_5d": {
        "category": "risk",
        "description": "5日波动率",
        "requires_financials": False,
        "sub_factors": ["realized_vol_5d"],
        "aliases": ["short_vol"],
    },
    "vol_10d": {
        "category": "risk",
        "description": "10日波动率",
        "requires_financials": False,
        "sub_factors": ["realized_vol_10d"],
        "aliases": ["biweekly_vol"],
    },
    "vol_60d": {
        "category": "risk",
        "description": "60日波动率",
        "requires_financials": False,
        "sub_factors": ["realized_vol_60d"],
        "aliases": ["quarterly_vol"],
    },
    "atr_14": {
        "category": "risk",
        "description": "14日ATR",
        "requires_financials": False,
        "sub_factors": ["average_true_range"],
        "aliases": ["atr"],
    },
    "atr_20": {
        "category": "risk",
        "description": "20日ATR",
        "requires_financials": False,
        "sub_factors": ["average_true_range_20"],
        "aliases": ["atr_monthly"],
    },
    "bollinger_width": {
        "category": "risk",
        "description": "布林带宽度",
        "requires_financials": False,
        "sub_factors": ["boll_width"],
        "aliases": ["bband_width", "bb_width"],
    },
    "downside_vol": {
        "category": "risk",
        "description": "下行波动率",
        "requires_financials": False,
        "sub_factors": ["downside_deviation"],
        "aliases": ["downside_risk"],
    },
    # ── 量价类扩展 ──
    "volume_ratio": {
        "category": "volume",
        "description": "量比(5日/20日)",
        "requires_financials": False,
        "sub_factors": ["vol_ratio_5_20"],
        "aliases": ["vol_ratio"],
    },
    "obv_slope": {
        "category": "volume",
        "description": "OBV斜率",
        "requires_financials": False,
        "sub_factors": ["on_balance_volume_slope"],
        "aliases": ["obv_trend"],
    },
    "vwap_deviation": {
        "category": "volume",
        "description": "VWAP偏离度",
        "requires_financials": False,
        "sub_factors": ["vwap_dev"],
        "aliases": ["vwap_diff"],
    },
    "turnover_5d": {
        "category": "volume",
        "description": "5日换手率",
        "requires_financials": False,
        "sub_factors": ["turnover_rate_5d"],
        "aliases": ["short_turnover"],
    },
    "turnover_20d": {
        "category": "volume",
        "description": "20日换手率",
        "requires_financials": False,
        "sub_factors": ["turnover_rate_20d"],
        "aliases": ["monthly_turnover"],
    },
    # ── 基本面扩展 ──
    "pe_ttm": {
        "category": "fundamental",
        "description": "市盈率TTM",
        "requires_financials": True,
        "sub_factors": ["pe_ratio"],
        "aliases": ["pe", "price_earnings"],
    },
    "pb_mrq": {
        "category": "fundamental",
        "description": "市净率MRQ",
        "requires_financials": True,
        "sub_factors": ["pb_ratio"],
        "aliases": ["pb", "price_book"],
    },
    "ps_ttm": {
        "category": "fundamental",
        "description": "市销率TTM",
        "requires_financials": True,
        "sub_factors": ["ps_ratio"],
        "aliases": ["ps", "price_sales"],
    },
    "roe_ttm": {
        "category": "fundamental",
        "description": "净资产收益率TTM",
        "requires_financials": True,
        "sub_factors": ["roe"],
        "aliases": ["return_on_equity"],
    },
    "roa_ttm": {
        "category": "fundamental",
        "description": "总资产收益率TTM",
        "requires_financials": True,
        "sub_factors": ["roa"],
        "aliases": ["return_on_assets"],
    },
    "gross_margin": {
        "category": "fundamental",
        "description": "毛利率",
        "requires_financials": True,
        "sub_factors": ["gross_profit_margin"],
        "aliases": ["gpm"],
    },
    "net_margin": {
        "category": "fundamental",
        "description": "净利率",
        "requires_financials": True,
        "sub_factors": ["net_profit_margin"],
        "aliases": ["npm"],
    },
    "debt_to_equity": {
        "category": "fundamental",
        "description": "资产负债率",
        "requires_financials": True,
        "sub_factors": ["debt_ratio"],
        "aliases": ["leverage", "d_e_ratio"],
    },
    "revenue_growth_yoy": {
        "category": "fundamental",
        "description": "营收同比增长率",
        "requires_financials": True,
        "sub_factors": ["revenue_growth"],
        "aliases": ["sales_growth"],
    },
    "dividend_yield": {
        "category": "fundamental",
        "description": "股息率",
        "requires_financials": True,
        "sub_factors": ["div_yield"],
        "aliases": ["yield", "dps"],
    },
    # ── 另类因子 ──
    "sentiment_score": {
        "category": "alternative",
        "description": "情绪得分",
        "requires_financials": False,
        "sub_factors": ["news_sentiment", "social_sentiment"],
        "aliases": ["sentiment"],
    },
    "capital_flow": {
        "category": "alternative",
        "description": "资金流向得分",
        "requires_financials": False,
        "sub_factors": ["net_inflow", "main_force_flow"],
        "aliases": ["money_flow"],
    },
    "north_flow": {
        "category": "alternative",
        "description": "北向资金得分",
        "requires_financials": False,
        "sub_factors": ["northbound_net"],
        "aliases": ["northbound"],
    },
    "institutional_flow": {
        "category": "alternative",
        "description": "机构资金流向",
        "requires_financials": False,
        "sub_factors": ["inst_net_buy"],
        "aliases": ["institution_flow"],
    },
    "event_intensity": {
        "category": "alternative",
        "description": "事件强度",
        "requires_financials": False,
        "sub_factors": ["event_count", "event_impact"],
        "aliases": ["event_score"],
    },
}


# ── Default constants ──

DEFAULT_FACTOR_LOOKBACK = 20

REVENUE_GROWTH_KEYS = (
    "revenue_growth",
    "revenue_growth_yoy",
    "sales_growth",
    "sales_growth_yoy",
    "operating_revenue_growth",
    "or_yoy",
    "total_revenue_growth",
)
PROFIT_GROWTH_KEYS = (
    "profit_growth",
    "profit_growth_yoy",
    "net_profit_growth",
    "netprofit_yoy",
    "np_growth",
    "earnings_growth",
    "eps_growth",
    "eps_growth_yoy",
)
MARKET_CAP_KEYS = (
    "market_cap",
    "total_market_cap",
    "total_mv",
    "circ_mv",
    "float_market_cap",
    "mkt_cap",
    "totalMarketCap",
    "floatMarketCap",
    "总市值",
    "流通市值",
)

_PERF_STAGE_KEYS = ("fetch", "factor", "ic", "oos", "robust", "backtest", "serialize")


# ── Environment / configuration helpers ──

def _env_flag(name: str, default: bool = True) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return bool(default)
    return str(raw).strip().lower() not in {"0", "false", "off", "no", "n"}


def _env_int(name: str, default: int, *, min_value: int = 1, max_value: int = 20) -> int:
    raw = os.getenv(name)
    try:
        val = int(str(raw).strip()) if raw is not None else int(default)
    except Exception:
        val = int(default)
    return max(min_value, min(max_value, val))


_QUANT_BATCH_FETCH_ENABLED = _env_flag("QUANT_BATCH_FETCH_ENABLED", True)
_QUANT_PREFETCH_CONCURRENCY = _env_int("QUANT_PREFETCH_CONCURRENCY", 8, min_value=1, max_value=20)
_QUANT_PERF_BREAKDOWN_ENABLED = _env_flag("QUANT_PERF_BREAKDOWN_ENABLED", True)


# ── Type-coercion helpers ──

def _to_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return bool(default)
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        v = value.strip().lower()
        if v in {"1", "true", "yes", "y", "on"}:
            return True
        if v in {"0", "false", "no", "n", "off"}:
            return False
        return bool(default)
    return bool(value)


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        parsed = float(value)
        if not math.isfinite(parsed):
            return default
        return parsed
    except (TypeError, ValueError):
        return default


# ── Factor name resolution ──

def _normalize_factor_name(factor: str) -> str:
    raw = str(factor or "").strip().lower()
    if not raw:
        return raw
    if raw in SUPPORTED_FACTORS:
        return raw

    alias_map: Dict[str, str] = {}
    for canonical_name, meta in SUPPORTED_FACTORS.items():
        alias_map.setdefault(str(canonical_name).strip().lower(), canonical_name)
        for alias in meta.get("aliases", []):
            alias_key = str(alias or "").strip().lower()
            if alias_key:
                alias_map.setdefault(alias_key, canonical_name)

    synthetic_aliases = {
        "momentum_1d": "mom_1d",
        "momentum_5d": "mom_5d",
        "momentum_10d": "mom_10d",
        "momentum_20d": "momentum",
        "momentum_60d": "mom_60d",
    }
    if raw in synthetic_aliases:
        return synthetic_aliases[raw]

    return alias_map.get(raw, raw)
