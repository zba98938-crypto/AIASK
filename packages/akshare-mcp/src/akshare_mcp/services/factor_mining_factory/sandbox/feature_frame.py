"""扩展特征帧构建 — 在核心特征帧基础上添加扩展字段。"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from ...factor_candidate_compiler import build_factor_feature_frame as _core_build


def build_extended_feature_frame(
    frame: pd.DataFrame,
    *,
    fundamentals: dict[str, float] | None = None,
    alternative: dict[str, float] | None = None,
) -> pd.DataFrame:
    """构建扩展特征帧。

    在核心特征帧（13 字段）基础上添加：
    - 流动性字段：turnover, amihud_illiquidity, vwap_deviation
    - 基本面字段：pe_ratio, pb_ratio, roe 等（静态值广播）
    - 另类数据字段：news_sentiment, fund_flow_net 等（静态值广播）
    """
    # 核心帧
    base = _core_build(frame)

    # 扩展流动性字段
    if "turnover" in frame.columns:
        base["turnover"] = pd.to_numeric(frame["turnover"], errors="coerce").astype(float)
    elif "volume" in base.columns and "amount" in base.columns:
        # 近似换手率
        base["turnover"] = base["volume"] / base["volume"].rolling(20).mean().replace(0, np.nan)

    if "turnover" in base.columns:
        base["turnover_5d"] = base["turnover"].rolling(5, min_periods=5).mean()

    # Amihud 非流动性
    if "returns_1d" in base.columns and "amount" in base.columns:
        amount = base["amount"].replace(0, np.nan)
        base["amihud_illiquidity"] = (base["returns_1d"].abs() / amount).rolling(20, min_periods=20).mean()

    # VWAP 偏离
    if "close" in base.columns and "amount" in base.columns and "volume" in base.columns:
        vol = base["volume"].replace(0, np.nan)
        vwap = base["amount"] / vol
        base["vwap_deviation"] = (base["close"] - vwap) / vwap.replace(0, np.nan)

    # 基本面字段（静态值广播到所有行）
    fundamentals = fundamentals or {}
    for field in ("pe_ratio", "pb_ratio", "ps_ratio", "roe", "roa",
                  "gross_margin", "revenue_growth", "profit_growth",
                  "debt_ratio", "dividend_yield", "market_cap", "free_float_cap"):
        if field in fundamentals:
            base[field] = float(fundamentals[field])
        elif field in frame.columns:
            base[field] = pd.to_numeric(frame[field], errors="coerce").astype(float)

    # 另类数据字段（静态值广播）
    alternative = alternative or {}
    for field in ("news_sentiment", "fund_flow_net", "north_capital_net",
                  "margin_balance", "institutional_hold"):
        if field in alternative:
            base[field] = float(alternative[field])
        elif field in frame.columns:
            base[field] = pd.to_numeric(frame[field], errors="coerce").astype(float)

    return base
