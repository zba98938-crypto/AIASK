"""Stock profile vector backfill pipeline for unified vector storage.

PR-S18 (策略工厂跑偏修复方案 P1)：本文件在原 11 维扁平特征向量的基础上，
按方案 §3.2.2 / §3.2.2.1 扩展为 9 大维度的 ``raw_features_grouped`` 与
``profile_summary``，并对每个特征声明 ``coverage / status / source``，
让下游策略工厂矩阵 planner 真正消费多维画像而不是只看几个粗类。

向后兼容承诺：
    - 顶层 ``metadata.raw_features`` 仍保留扁平 11 字段结构（供现有
      ``strategy_pipeline.py`` / ``_vector_search_similar.py`` /
      ``stock_and_watchlist.py`` 与既有测试继续按 ``raw_features.pe_ratio``
      之类的方式读取）。
    - 嵌入向量 ``embedding`` 维度与归一化方式保持不变。
    - ``metadata.feature_coverage`` 旧消费者拿到的是已覆盖的扁平特征名列表，
      新增的"分维度状态"挂在 ``metadata.profile_summary.feature_coverage``。
"""

from __future__ import annotations

import hashlib
import json
import math
from typing import Any, Mapping, Optional

_PROFILE_TYPES = ("fundamental", "technical", "both")
_FEATURE_ORDER = (
    "pe_ratio",
    "pb_ratio",
    "market_cap_log10",
    "roe",
    "debt_ratio",
    "revenue_growth",
    "profit_growth",
    "momentum_20d",
    "trend_20d",
    "volatility_20d",
    "volume_ratio_20d",
)
_FEATURE_SET_BY_TYPE = {
    "fundamental": {
        "pe_ratio",
        "pb_ratio",
        "market_cap_log10",
        "roe",
        "debt_ratio",
        "revenue_growth",
        "profit_growth",
    },
    "technical": {
        "momentum_20d",
        "trend_20d",
        "volatility_20d",
        "volume_ratio_20d",
    },
    "both": set(_FEATURE_ORDER),
}

# 9 大维度词典对齐 §3.2.2.1：
# 与 ``factor_prompt_builder.py`` / ``factor_calculator/*`` /
# ``factor_candidate_seed.py`` / ``MiningContext`` 中真实因子分类对齐。
_DIMENSION_ORDER = (
    "price_trend_reversal",
    "volatility_risk",
    "volume_liquidity_microstructure",
    "valuation",
    "quality_growth_balance_sheet",
    "style_exposure",
    "alternative_sentiment_capital_flow",
    "event_news_notice_research_theme",
    "regime_and_factor_pool_context",
)

# 维度 -> 候选 family（每只股票画像归类后会推荐这些 family）
_DIMENSION_TO_FAMILIES: dict[str, tuple[str, ...]] = {
    "price_trend_reversal": ("momentum", "ma_cross", "mean_reversion_short", "rsi"),
    "volatility_risk": ("quality_factor", "value_factor"),
    "volume_liquidity_microstructure": ("momentum", "ma_cross"),
    "valuation": ("value_factor", "quality_factor"),
    "quality_growth_balance_sheet": ("quality_factor", "growth_factor"),
    "style_exposure": ("multi_factor", "value_factor"),
    "alternative_sentiment_capital_flow": ("event_structure_breakout", "momentum"),
    "event_news_notice_research_theme": ("event_structure_breakout",),
    "regime_and_factor_pool_context": ("multi_factor",),
}

# Coverage 标签语义
_COVERAGE_OK = "ok"
_COVERAGE_PARTIAL = "partial"
_COVERAGE_MISSING = "missing"


def _normalize_codes(raw: Any) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, (list, tuple, set)):
        items = raw
    else:
        items = str(raw).replace(";", ",").split(",")
    normalized: list[str] = []
    seen: set[str] = set()
    for item in items:
        token = str(item or "").strip()
        if not token or token in seen:
            continue
        seen.add(token)
        normalized.append(token)
    return normalized


def _normalize_positive_int(value: Any, default: int, *, minimum: int = 1, maximum: int = 5000) -> int:
    try:
        resolved = int(default if value is None or value == "" else value)
    except (TypeError, ValueError):
        resolved = int(default)
    return max(minimum, min(resolved, maximum))


def _normalize_profile_types(raw: Any) -> list[str]:
    if raw is None:
        return list(_PROFILE_TYPES)
    resolved: list[str] = []
    seen: set[str] = set()
    for item in _normalize_codes(raw):
        token = str(item or "").strip().lower()
        if token not in _PROFILE_TYPES or token in seen:
            continue
        seen.add(token)
        resolved.append(token)
    return resolved or list(_PROFILE_TYPES)


def _normalize_ratio(value: Any) -> float:
    try:
        resolved = float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0
    if abs(resolved) > 1.5:
        return resolved / 100.0
    return resolved


def _coerce_optional_float(value: Any) -> Optional[float]:
    """转 float 但区分"真 0"和"缺失"。"""
    if value is None or value == "":
        return None
    try:
        resolved = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(resolved) or math.isinf(resolved):
        return None
    return resolved


def _coerce_ratio(value: Any) -> Optional[float]:
    """把百分比/比率统一到 [-1, 1] 量级，缺失返回 None。"""
    raw = _coerce_optional_float(value)
    if raw is None:
        return None
    if abs(raw) > 1.5:
        return raw / 100.0
    return raw


def _normalize_vector(values: list[float]) -> list[float]:
    vector = [float(item) for item in list(values or [])]
    if not vector:
        return []
    norm = math.sqrt(sum(item * item for item in vector))
    if norm <= 0:
        return vector
    return [round(item / norm, 10) for item in vector]


def _clip_feature(value: float, *, scale: float) -> float:
    return round(math.tanh(float(value or 0.0) / max(float(scale or 1.0), 1e-6)), 8)


def _make_feature_cell(
    value: Any,
    *,
    coverage: str,
    source: str,
    status: Optional[str] = None,
) -> dict[str, Any]:
    """单个维度子特征的标准 cell 结构。"""

    cell: dict[str, Any] = {
        "value": value,
        "coverage": coverage,
        "source": source,
    }
    if status:
        cell["status"] = status
    return cell


def _coverage_for(value: Any) -> str:
    if value is None:
        return _COVERAGE_MISSING
    if isinstance(value, (list, tuple, set, dict)) and not value:
        return _COVERAGE_MISSING
    return _COVERAGE_OK


def _aggregate_dimension_coverage(cells: Mapping[str, dict[str, Any]]) -> str:
    """根据子特征的 coverage 聚合维度级 coverage。"""

    if not cells:
        return _COVERAGE_MISSING
    statuses = [str(cell.get("coverage") or _COVERAGE_MISSING) for cell in cells.values()]
    ok = sum(1 for s in statuses if s == _COVERAGE_OK)
    if ok == len(statuses):
        return _COVERAGE_OK
    if ok == 0 and _COVERAGE_PARTIAL not in statuses:
        return _COVERAGE_MISSING
    return _COVERAGE_PARTIAL


def _extract_fundamental_features(
    stock_info: dict[str, Any],
    financial_row: Optional[dict[str, Any]],
) -> dict[str, float]:
    financials = dict(financial_row or {})
    market_cap = float(stock_info.get("market_cap") or 0.0)
    return {
        "pe_ratio": float(stock_info.get("pe_ratio") or 0.0),
        "pb_ratio": float(stock_info.get("pb_ratio") or 0.0),
        "market_cap_log10": math.log10(max(market_cap, 1.0)) if market_cap > 0 else 0.0,
        "roe": float(financials.get("roe") or 0.0),
        "debt_ratio": _normalize_ratio(financials.get("debt_ratio")),
        "revenue_growth": _normalize_ratio(financials.get("revenue_growth")),
        "profit_growth": _normalize_ratio(financials.get("profit_growth")),
    }


def _extract_technical_features(klines: list[dict[str, Any]]) -> dict[str, float]:
    closes = [float(row.get("close") or 0.0) for row in list(klines or []) if row.get("close") is not None]
    volumes = [float(row.get("volume") or 0.0) for row in list(klines or []) if row.get("volume") is not None]
    if len(closes) < 20:
        return {}
    recent_closes = closes[-20:]
    returns = []
    for idx in range(1, len(recent_closes)):
        prev_close = float(recent_closes[idx - 1] or 0.0)
        close = float(recent_closes[idx] or 0.0)
        if prev_close <= 0:
            continue
        returns.append((close - prev_close) / prev_close)
    ma20 = sum(recent_closes) / len(recent_closes)
    avg_volume_20 = sum(volumes[-20:]) / max(len(volumes[-20:]), 1) if volumes else 0.0
    avg_volume_5 = sum(volumes[-5:]) / max(len(volumes[-5:]), 1) if volumes else 0.0
    return {
        "momentum_20d": ((recent_closes[-1] - recent_closes[0]) / recent_closes[0]) if recent_closes[0] > 0 else 0.0,
        "trend_20d": ((recent_closes[-1] - ma20) / ma20) if ma20 > 0 else 0.0,
        "volatility_20d": math.sqrt(sum(item * item for item in returns) / max(len(returns), 1)) if returns else 0.0,
        "volume_ratio_20d": (avg_volume_5 / avg_volume_20) if avg_volume_20 > 0 else 0.0,
    }


def build_stock_profile_features(
    stock_info: dict[str, Any],
    financial_row: Optional[dict[str, Any]] = None,
    klines: Optional[list[dict[str, Any]]] = None,
) -> dict[str, float]:
    return {
        **_extract_fundamental_features(stock_info, financial_row),
        **_extract_technical_features(list(klines or [])),
    }


def _extended_technical_features(klines: list[dict[str, Any]]) -> dict[str, Any]:
    """额外的技术/波动/量价指标，用于 9 大维度 raw_features_grouped 的子特征。

    所有指标缺失（K 线不足）时返回 None，上层再标 coverage=missing。
    """

    rows = list(klines or [])
    closes = [_coerce_optional_float(r.get("close")) for r in rows]
    closes = [c for c in closes if c is not None and c > 0]
    highs = [_coerce_optional_float(r.get("high")) for r in rows]
    lows = [_coerce_optional_float(r.get("low")) for r in rows]
    volumes = [_coerce_optional_float(r.get("volume")) for r in rows]
    amounts = [_coerce_optional_float(r.get("amount")) for r in rows]

    out: dict[str, Optional[float]] = {
        "momentum_60d": None,
        "rsi_14": None,
        "reversal_5d": None,
        "atr_14": None,
        "downside_vol_20d": None,
        "amihud_illiquidity_20d": None,
        "vwap_deviation_5d": None,
        "turnover_proxy_20d": None,
    }
    if not closes:
        return out

    if len(closes) >= 60:
        first = closes[-60]
        if first > 0:
            out["momentum_60d"] = round((closes[-1] - first) / first, 8)

    if len(closes) >= 6:
        prev = closes[-6]
        if prev > 0:
            out["reversal_5d"] = round((closes[-1] - prev) / prev, 8)

    # RSI(14)
    if len(closes) >= 15:
        gains = []
        losses = []
        for idx in range(-14, 0):
            change = closes[idx] - closes[idx - 1]
            if change >= 0:
                gains.append(change)
            else:
                losses.append(-change)
        avg_gain = sum(gains) / 14
        avg_loss = sum(losses) / 14
        if avg_loss <= 1e-9:
            out["rsi_14"] = 100.0 if avg_gain > 0 else 50.0
        else:
            rs = avg_gain / avg_loss
            out["rsi_14"] = round(100.0 - (100.0 / (1.0 + rs)), 4)

    # ATR(14) 标准化为 % of price
    if len(rows) >= 15 and all(h is not None for h in highs[-15:]) and all(l is not None for l in lows[-15:]):
        trs = []
        for idx in range(-14, 0):
            high = highs[idx]
            low = lows[idx]
            prev_close = _coerce_optional_float(rows[idx - 1].get("close"))
            if high is None or low is None or prev_close is None:
                continue
            tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
            trs.append(tr)
        if trs and closes[-1] > 0:
            out["atr_14"] = round((sum(trs) / len(trs)) / closes[-1], 6)

    # 20d downside volatility
    if len(closes) >= 21:
        rets = []
        for idx in range(-20, 0):
            prev = closes[idx - 1]
            if prev > 0:
                rets.append((closes[idx] - prev) / prev)
        downs = [r for r in rets if r < 0]
        if downs:
            out["downside_vol_20d"] = round(math.sqrt(sum(r * r for r in downs) / len(downs)), 6)
        elif rets:
            out["downside_vol_20d"] = 0.0

    # Amihud illiquidity 20d ≈ mean(|ret|/amount)
    if len(rows) >= 21 and all(a is not None and a > 0 for a in amounts[-20:]):
        ill_values = []
        for idx in range(-20, 0):
            prev = closes[idx - 1] if abs(idx - 1) <= len(closes) else None
            amt = amounts[idx]
            if prev is None or prev <= 0 or amt is None or amt <= 0:
                continue
            ret = abs((closes[idx] - prev) / prev)
            ill_values.append(ret / amt * 1e8)  # 放大到便于观察
        if ill_values:
            out["amihud_illiquidity_20d"] = round(sum(ill_values) / len(ill_values), 8)

    # 5d VWAP deviation = (close - vwap) / vwap, vwap=Σamount/Σvolume
    if len(rows) >= 5:
        recent = rows[-5:]
        vols_5 = [_coerce_optional_float(r.get("volume")) for r in recent]
        amts_5 = [_coerce_optional_float(r.get("amount")) for r in recent]
        if all(v is not None and v > 0 for v in vols_5) and all(a is not None and a > 0 for a in amts_5):
            total_amount = sum(amts_5)
            total_volume = sum(vols_5)
            if total_volume > 0:
                vwap = total_amount / total_volume
                if vwap > 0:
                    out["vwap_deviation_5d"] = round((closes[-1] - vwap) / vwap, 6)

    # 20d 平均成交量作为换手代理（无 turnover_rate 字段时退化）
    if len(volumes) >= 20 and all(v is not None for v in volumes[-20:]):
        avg = sum(volumes[-20:]) / 20
        if avg > 0:
            out["turnover_proxy_20d"] = round(avg, 4)

    return out


def _build_raw_features_grouped(
    stock_info: dict[str, Any],
    financial_row: Optional[dict[str, Any]],
    klines: list[dict[str, Any]],
    flat_features: dict[str, float],
    *,
    market_regime: Optional[str] = None,
    active_factor_families: Optional[list[str]] = None,
) -> dict[str, dict[str, Any]]:
    """产出 9 大维度分组的 raw_features_grouped，每个子特征带 coverage/source/status。"""

    fin = dict(financial_row or {})
    has_klines = len(klines or []) >= 20
    has_klines_60 = len(klines or []) >= 60
    extras = _extended_technical_features(klines or [])

    market_cap = _coerce_optional_float(stock_info.get("market_cap"))

    # 1. price_trend_reversal
    momentum_20d = flat_features.get("momentum_20d") if has_klines else None
    trend_20d = flat_features.get("trend_20d") if has_klines else None
    price_trend = {
        "momentum_20d": _make_feature_cell(
            momentum_20d if has_klines else None,
            coverage=_COVERAGE_OK if has_klines else _COVERAGE_MISSING,
            source="kline_1d",
            status=None if has_klines else "insufficient_kline",
        ),
        "momentum_60d": _make_feature_cell(
            extras.get("momentum_60d"),
            coverage=_COVERAGE_OK if has_klines_60 and extras.get("momentum_60d") is not None else _COVERAGE_MISSING,
            source="kline_1d",
            status=None if extras.get("momentum_60d") is not None else "insufficient_kline",
        ),
        "rsi_14": _make_feature_cell(
            extras.get("rsi_14"),
            coverage=_COVERAGE_OK if extras.get("rsi_14") is not None else _COVERAGE_MISSING,
            source="factor_calculator.technical",
            status=None if extras.get("rsi_14") is not None else "insufficient_kline",
        ),
        "reversal_5d": _make_feature_cell(
            extras.get("reversal_5d"),
            coverage=_COVERAGE_OK if extras.get("reversal_5d") is not None else _COVERAGE_MISSING,
            source="factor_calculator.technical",
        ),
        "trend_20d": _make_feature_cell(
            trend_20d,
            coverage=_COVERAGE_OK if has_klines else _COVERAGE_MISSING,
            source="kline_1d",
        ),
    }

    # 2. volatility_risk
    vol_20d = flat_features.get("volatility_20d") if has_klines else None
    volatility = {
        "volatility_20d": _make_feature_cell(
            vol_20d,
            coverage=_COVERAGE_OK if has_klines else _COVERAGE_MISSING,
            source="kline_1d",
        ),
        "atr_14_pct": _make_feature_cell(
            extras.get("atr_14"),
            coverage=_COVERAGE_OK if extras.get("atr_14") is not None else _COVERAGE_MISSING,
            source="factor_calculator.volatility",
        ),
        "downside_vol_20d": _make_feature_cell(
            extras.get("downside_vol_20d"),
            coverage=_COVERAGE_OK if extras.get("downside_vol_20d") is not None else _COVERAGE_MISSING,
            source="factor_calculator.volatility",
        ),
        # beta 暂无可靠输入，先 placeholder
        "beta_csi300": _make_feature_cell(
            None,
            coverage=_COVERAGE_MISSING,
            source="factor_calculator.fundamental",
            status="not_available",
        ),
    }

    # 3. volume_liquidity_microstructure
    volume_ratio = flat_features.get("volume_ratio_20d") if has_klines else None
    volume_liq = {
        "volume_ratio_5_20": _make_feature_cell(
            volume_ratio,
            coverage=_COVERAGE_OK if has_klines and volume_ratio is not None else _COVERAGE_MISSING,
            source="kline_1d",
        ),
        "turnover_proxy_20d": _make_feature_cell(
            extras.get("turnover_proxy_20d"),
            coverage=_COVERAGE_PARTIAL if extras.get("turnover_proxy_20d") is not None else _COVERAGE_MISSING,
            source="factor_calculator.volume",
            status="best_effort_no_turnover_rate",
        ),
        "amihud_illiquidity_20d": _make_feature_cell(
            extras.get("amihud_illiquidity_20d"),
            coverage=_COVERAGE_PARTIAL if extras.get("amihud_illiquidity_20d") is not None else _COVERAGE_MISSING,
            source="factor_calculator.volume",
        ),
        "vwap_deviation_5d": _make_feature_cell(
            extras.get("vwap_deviation_5d"),
            coverage=_COVERAGE_PARTIAL if extras.get("vwap_deviation_5d") is not None else _COVERAGE_MISSING,
            source="factor_calculator.volume",
        ),
    }

    # 4. valuation
    pe = _coerce_optional_float(stock_info.get("pe_ratio"))
    pb = _coerce_optional_float(stock_info.get("pb_ratio"))
    valuation = {
        "pe_ratio": _make_feature_cell(
            pe,
            coverage=_coverage_for(pe),
            source="valuation_snapshot",
        ),
        "pb_ratio": _make_feature_cell(
            pb,
            coverage=_coverage_for(pb),
            source="valuation_snapshot",
        ),
        "ps_ratio": _make_feature_cell(
            None,
            coverage=_COVERAGE_MISSING,
            source="valuation_snapshot",
            status="not_available",
        ),
    }

    # 5. quality_growth_balance_sheet
    roe = _coerce_optional_float(fin.get("roe"))
    gross_margin = _coerce_optional_float(fin.get("gross_margin"))
    debt_ratio = _coerce_ratio(fin.get("debt_ratio"))
    rev_growth = _coerce_ratio(fin.get("revenue_growth"))
    profit_growth = _coerce_ratio(fin.get("profit_growth"))
    quality = {
        "roe": _make_feature_cell(
            roe,
            coverage=_COVERAGE_PARTIAL if roe is not None else _COVERAGE_MISSING,
            source="financials",
        ),
        "gross_margin": _make_feature_cell(
            gross_margin,
            coverage=_COVERAGE_PARTIAL if gross_margin is not None else _COVERAGE_MISSING,
            source="financials",
        ),
        "revenue_growth_yoy": _make_feature_cell(
            rev_growth,
            coverage=_COVERAGE_PARTIAL if rev_growth is not None else _COVERAGE_MISSING,
            source="financials",
        ),
        "profit_growth_yoy": _make_feature_cell(
            profit_growth,
            coverage=_COVERAGE_PARTIAL if profit_growth is not None else _COVERAGE_MISSING,
            source="financials",
        ),
        "debt_ratio": _make_feature_cell(
            debt_ratio,
            coverage=_COVERAGE_PARTIAL if debt_ratio is not None else _COVERAGE_MISSING,
            source="financials",
        ),
    }

    # 6. style_exposure
    size_log = math.log10(market_cap) if market_cap and market_cap > 0 else None
    style = {
        "size_log10": _make_feature_cell(
            size_log,
            coverage=_COVERAGE_OK if size_log is not None else _COVERAGE_MISSING,
            source="factor_calculator.fundamental",
        ),
        "beta_factor": _make_feature_cell(
            None,
            coverage=_COVERAGE_MISSING,
            source="factor_calculator.fundamental",
            status="not_available",
        ),
        "liquidity_factor": _make_feature_cell(
            extras.get("turnover_proxy_20d"),
            coverage=_COVERAGE_PARTIAL if extras.get("turnover_proxy_20d") is not None else _COVERAGE_MISSING,
            source="factor_calculator.fundamental",
        ),
    }

    # 7. alternative_sentiment_capital_flow
    alternative = {
        "news_sentiment_score": _make_feature_cell(
            None,
            coverage=_COVERAGE_MISSING,
            source="factor_prompt_builder.alternative_factors",
            status="not_available",
        ),
        "capital_flow_score": _make_feature_cell(
            None,
            coverage=_COVERAGE_MISSING,
            source="stock_fund_flow",
            status="insufficient_coverage",
        ),
        "retail_intensity": _make_feature_cell(
            None,
            coverage=_COVERAGE_MISSING,
            source="stock_fund_flow",
            status="insufficient_coverage",
        ),
    }

    # 8. event_news_notice_research_theme
    event = {
        "announcement_frequency": _make_feature_cell(
            None,
            coverage=_COVERAGE_MISSING,
            source="event_or_theme_tables",
            status="not_enough_events",
        ),
        "research_heat": _make_feature_cell(
            None,
            coverage=_COVERAGE_MISSING,
            source="research_reports",
            status="not_available",
        ),
        "theme_exposure": _make_feature_cell(
            [],
            coverage=_COVERAGE_MISSING,
            source="theme_graph",
            status="not_available",
        ),
    }

    # 9. regime_and_factor_pool_context
    regime_cells = {
        "market_regime": _make_feature_cell(
            market_regime if market_regime else None,
            coverage=_COVERAGE_PARTIAL if market_regime else _COVERAGE_MISSING,
            source="MiningContext",
            status=None if market_regime else "best_effort",
        ),
        "active_factor_families": _make_feature_cell(
            list(active_factor_families or []),
            coverage=_COVERAGE_PARTIAL if active_factor_families else _COVERAGE_MISSING,
            source="MiningContext",
        ),
    }

    return {
        "price_trend_reversal": price_trend,
        "volatility_risk": volatility,
        "volume_liquidity_microstructure": volume_liq,
        "valuation": valuation,
        "quality_growth_balance_sheet": quality,
        "style_exposure": style,
        "alternative_sentiment_capital_flow": alternative,
        "event_news_notice_research_theme": event,
        "regime_and_factor_pool_context": regime_cells,
    }
