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

from .profile_features import (
    _COVERAGE_MISSING,
    _COVERAGE_OK,
    _COVERAGE_PARTIAL,
    _DIMENSION_ORDER,
    _DIMENSION_TO_FAMILIES,
    _FEATURE_ORDER,
    _FEATURE_SET_BY_TYPE,
    _PROFILE_TYPES,
    _aggregate_dimension_coverage,
    _build_raw_features_grouped,
    _clip_feature,
    _coerce_optional_float,
    _coerce_ratio,
    _coverage_for,
    _extended_technical_features,
    _extract_fundamental_features,
    _extract_technical_features,
    _make_feature_cell,
    _normalize_codes,
    _normalize_positive_int,
    _normalize_profile_types,
    _normalize_ratio,
    _normalize_vector,
    build_stock_profile_features,
)


def _safe_pos(value: Optional[float]) -> float:
    return max(0.0, float(value or 0.0))


def _normalize_score(value: float) -> float:
    return round(max(0.0, min(1.0, value)), 4)


def _build_factor_dimension_scores(
    grouped: dict[str, dict[str, Any]],
) -> dict[str, float]:
    """把 9 大维度 cell 数据折算成 0~1 因子维度分数（key 与方案 §3.2.2 一致的 9 维短标签）。"""

    def _v(group: str, key: str) -> Optional[float]:
        cell = (grouped.get(group) or {}).get(key) or {}
        val = cell.get("value")
        if isinstance(val, (int, float)):
            return float(val)
        return None

    momentum_20d = _v("price_trend_reversal", "momentum_20d") or 0.0
    momentum_60d = _v("price_trend_reversal", "momentum_60d") or 0.0
    trend = (
        abs(momentum_20d) * 0.6
        + abs(momentum_60d) * 0.4
    )

    reversal_5d = _v("price_trend_reversal", "reversal_5d") or 0.0
    rsi = _v("price_trend_reversal", "rsi_14") or 50.0
    rsi_extreme = max(0.0, abs(rsi - 50.0) / 50.0)
    reversal = 0.5 * abs(reversal_5d) * 5.0 + 0.5 * rsi_extreme

    vol_20d = _v("volatility_risk", "volatility_20d") or 0.0
    atr_pct = _v("volatility_risk", "atr_14_pct") or 0.0
    risk = vol_20d * 5.0 + atr_pct * 10.0

    volume_ratio = _v("volume_liquidity_microstructure", "volume_ratio_5_20") or 0.0
    volume = max(0.0, volume_ratio - 1.0) * 0.7 + min(volume_ratio, 1.0) * 0.3

    pe = _v("valuation", "pe_ratio")
    pb = _v("valuation", "pb_ratio")
    valuation_score = 0.0
    if pe is not None and 0 < pe <= 80:
        valuation_score += max(0.0, (80.0 - pe) / 80.0) * 0.6
    if pb is not None and 0 < pb <= 12:
        valuation_score += max(0.0, (12.0 - pb) / 12.0) * 0.4

    roe = _v("quality_growth_balance_sheet", "roe") or 0.0
    gm = _v("quality_growth_balance_sheet", "gross_margin") or 0.0
    debt = _v("quality_growth_balance_sheet", "debt_ratio") or 0.0
    quality = (
        _safe_pos(roe) / 25.0 * 0.55
        + _safe_pos(gm) / 0.6 * 0.30
        + max(0.0, 1.0 - _safe_pos(debt) / 1.0) * 0.15
    )

    rev_g = _v("quality_growth_balance_sheet", "revenue_growth_yoy") or 0.0
    profit_g = _v("quality_growth_balance_sheet", "profit_growth_yoy") or 0.0
    growth = (max(0.0, rev_g) * 0.5 + max(0.0, profit_g) * 0.5) / 0.4

    alt = 0.0  # 暂无可用数据，coverage missing 时统一为 0
    event = 0.0

    return {
        "trend": _normalize_score(trend),
        "reversal": _normalize_score(reversal),
        "risk": _normalize_score(risk),
        "volume": _normalize_score(volume),
        "valuation": _normalize_score(valuation_score),
        "quality": _normalize_score(quality),
        "growth": _normalize_score(growth),
        "alternative": _normalize_score(alt),
        "event": _normalize_score(event),
    }


def _resolve_archetypes(
    dimension_scores: Mapping[str, float],
    *,
    market_cap: Optional[float],
    pe: Optional[float],
    volatility_20d: Optional[float],
) -> tuple[str, list[str]]:
    """根据维度分数和粗特征推断 primary / secondary archetype。"""

    quality = float(dimension_scores.get("quality") or 0.0)
    valuation = float(dimension_scores.get("valuation") or 0.0)
    trend = float(dimension_scores.get("trend") or 0.0)
    growth = float(dimension_scores.get("growth") or 0.0)
    reversal = float(dimension_scores.get("reversal") or 0.0)
    risk = float(dimension_scores.get("risk") or 0.0)
    volume = float(dimension_scores.get("volume") or 0.0)

    archetype_scores = {
        "fundamental_quality": quality * 0.6 + valuation * 0.4,
        "value_oriented": valuation * 0.7 + quality * 0.3,
        "growth_oriented": growth * 0.7 + trend * 0.3,
        "trend_following": trend * 0.7 + volume * 0.3,
        "mean_reversion": reversal * 0.7 + (1.0 - trend) * 0.3,
        "high_volatility_trader": risk * 0.7 + volume * 0.3,
        "balanced_multi_factor": 0.4 + 0.1 * (quality + valuation + trend + growth),
    }

    ranked = sorted(archetype_scores.items(), key=lambda kv: kv[1], reverse=True)
    primary = ranked[0][0] if ranked else "balanced_multi_factor"
    secondary: list[str] = []
    for name, _ in ranked[1:4]:
        secondary.append(name)
    # 加 size 标签
    if market_cap and market_cap > 0:
        cap_yi = market_cap / 1e8 if market_cap > 1e6 else market_cap
        if cap_yi >= 2000:
            secondary.append("large_cap_liquid")
        elif cap_yi >= 200:
            secondary.append("mid_cap")
        else:
            secondary.append("small_cap")
    if pe is not None and 0 < pe <= 15:
        secondary.append("low_pe")
    if volatility_20d is not None and volatility_20d <= 0.018:
        secondary.append("low_beta_candidate")
    return primary, list(dict.fromkeys(secondary))[:5]


def _candidate_factor_families_from_scores(
    dimension_scores: Mapping[str, float],
    *,
    threshold: float = 0.30,
) -> list[str]:
    """挑选 dimension_scores >= threshold 的维度并映射到 family。"""

    score_to_dim = {
        "trend": "price_trend_reversal",
        "reversal": "price_trend_reversal",
        "risk": "volatility_risk",
        "volume": "volume_liquidity_microstructure",
        "valuation": "valuation",
        "quality": "quality_growth_balance_sheet",
        "growth": "quality_growth_balance_sheet",
        "alternative": "alternative_sentiment_capital_flow",
        "event": "event_news_notice_research_theme",
    }
    families: list[str] = []
    for score_name, value in sorted(dimension_scores.items(), key=lambda kv: kv[1], reverse=True):
        if value < threshold:
            continue
        dim = score_to_dim.get(score_name)
        if not dim:
            continue
        for fam in _DIMENSION_TO_FAMILIES.get(dim, ()):
            if fam not in families:
                families.append(fam)
    return families[:6]


def _recommended_families(
    candidate_factor_families: list[str],
    archetype: str,
    profile_quality: str,
) -> list[str]:
    """在 candidate 基础上 + archetype 偏置，给出排序后的推荐 family。"""

    archetype_pref = {
        "fundamental_quality": ["quality_factor", "value_factor", "multi_factor"],
        "value_oriented": ["value_factor", "quality_factor", "multi_factor"],
        "growth_oriented": ["growth_factor", "momentum", "multi_factor"],
        "trend_following": ["momentum", "ma_cross", "multi_factor"],
        "mean_reversion": ["mean_reversion_short", "rsi", "value_factor"],
        "high_volatility_trader": ["momentum", "rsi", "ma_cross"],
        "balanced_multi_factor": ["multi_factor", "quality_factor", "momentum"],
    }
    base = list(archetype_pref.get(archetype, ["multi_factor"]))
    out: list[str] = []
    for fam in base + candidate_factor_families:
        if fam not in out:
            out.append(fam)
    if profile_quality == "failed":
        # 低置信仅返回第一个推荐，避免误导下游
        return out[:1]
    return out[:5]


def _resolve_profile_quality(
    grouped: dict[str, dict[str, Any]],
) -> tuple[str, dict[str, str]]:
    """根据每个维度的 cell coverage 聚合维度级 coverage，得 profile_quality 等级。"""

    feature_coverage: dict[str, str] = {}
    for dim in _DIMENSION_ORDER:
        cells = grouped.get(dim) or {}
        feature_coverage[dim] = _aggregate_dimension_coverage(cells)

    ok = sum(1 for v in feature_coverage.values() if v == _COVERAGE_OK)
    partial = sum(1 for v in feature_coverage.values() if v == _COVERAGE_PARTIAL)
    total = len(_DIMENSION_ORDER)

    if ok >= total - 1:
        quality = "good"
    elif ok + partial >= total // 2 + 1:
        quality = "partial"
    elif ok + partial >= 2:
        quality = "low_confidence"
    else:
        quality = "failed"
    return quality, feature_coverage


def _resolve_regime_from_features(
    raw_features_grouped: dict[str, dict[str, Any]],
    *,
    volatility_20d: Optional[float],
) -> dict[str, str]:
    """P1-1: 从画像原始特征派生 trend/vol regime（与 P0-3 标签口径一致）。

    输出 token：
    - trend_regime: trend_up / trend_down / range
    - vol_regime:   high_vol / normal_vol / low_vol
    特征缺失时对应维度返回 "unknown"（不阻断）。sentiment 维度此处不推断（由信号侧 fear_greed 决定）。
    """
    def _cell(group: str, key: str) -> Optional[float]:
        cell = (raw_features_grouped.get(group) or {}).get(key)
        if isinstance(cell, Mapping):
            val = cell.get("value")
        else:
            val = cell
        if isinstance(val, (int, float)):
            return float(val)
        return None

    labels = {"trend_regime": "unknown", "vol_regime": "unknown"}

    mom_20 = _cell("price_trend_reversal", "momentum_20d")
    mom_60 = _cell("price_trend_reversal", "momentum_60d")
    if mom_20 is not None:
        # momentum_20d 已是收益率口径（约 -1..1）。结合 60 日方向确认。
        if mom_20 > 0.05 and (mom_60 is None or mom_60 >= 0):
            labels["trend_regime"] = "trend_up"
        elif mom_20 < -0.05 and (mom_60 is None or mom_60 <= 0):
            labels["trend_regime"] = "trend_down"
        else:
            labels["trend_regime"] = "range"

    vol = volatility_20d
    if vol is None:
        vol = _cell("volatility_risk", "volatility_20d")
    if vol is not None:
        # volatility_20d 为日波动率，年化 ≈ vol * sqrt(252)。
        ann = float(vol) * (252 ** 0.5)
        if ann >= 0.45:
            labels["vol_regime"] = "high_vol"
        elif ann <= 0.20:
            labels["vol_regime"] = "low_vol"
        else:
            labels["vol_regime"] = "normal_vol"
    return labels


def _holding_bucket_hint_from_archetype(archetype: str) -> str:
    """P1-1: 由 archetype 给出 holding_period_bucket 倾向（供 SR-1 路由 + 矩阵消费）。"""
    return {
        "trend_following": "medium",
        "growth_oriented": "medium",
        "mean_reversion": "short",
        "high_volatility_trader": "short",
        "value_oriented": "long",
        "fundamental_quality": "long",
        "balanced_multi_factor": "medium",
    }.get(str(archetype or "").strip().lower(), "medium")


def _build_profile_summary(
    raw_features_grouped: dict[str, dict[str, Any]],
    *,
    market_cap: Optional[float],
    pe: Optional[float],
    volatility_20d: Optional[float],
) -> dict[str, Any]:
    dimension_scores = _build_factor_dimension_scores(raw_features_grouped)
    profile_quality, feature_coverage = _resolve_profile_quality(raw_features_grouped)
    primary_archetype, secondary_archetypes = _resolve_archetypes(
        dimension_scores,
        market_cap=market_cap,
        pe=pe,
        volatility_20d=volatility_20d,
    )
    candidate_factor_families = _candidate_factor_families_from_scores(dimension_scores)
    recommended_families = _recommended_families(
        candidate_factor_families,
        primary_archetype,
        profile_quality,
    )
    # P1-1：补 regime（trend/vol）+ holding_bucket 倾向，供 SR-0/SR-1 路由器消费。
    regime = _resolve_regime_from_features(raw_features_grouped, volatility_20d=volatility_20d)
    holding_bucket_hint = _holding_bucket_hint_from_archetype(primary_archetype)

    return {
        "primary_archetype": primary_archetype,
        "secondary_archetypes": secondary_archetypes,
        "candidate_factor_families": candidate_factor_families,
        "factor_dimension_scores": dimension_scores,
        "recommended_families": recommended_families,
        "profile_quality": profile_quality,
        "feature_coverage": feature_coverage,
        # P1-1 新增字段（向后兼容：旧消费者忽略即可）。
        "regime": regime,
        "holding_bucket_hint": holding_bucket_hint,
    }


def build_stock_profile_embedding(
    features: dict[str, float],
    *,
    profile_type: str,
) -> list[float]:
    normalized_type = str(profile_type or "both").strip().lower()
    allowed_features = _FEATURE_SET_BY_TYPE.get(normalized_type, _FEATURE_SET_BY_TYPE["both"])
    raw_vector: list[float] = []
    for feature_name in _FEATURE_ORDER:
        if feature_name not in allowed_features:
            raw_vector.append(0.0)
            continue
        value = float(features.get(feature_name) or 0.0)
        if feature_name == "pe_ratio":
            transformed = _clip_feature(math.log1p(max(value, 0.0)), scale=2.4)
        elif feature_name == "pb_ratio":
            transformed = _clip_feature(value, scale=5.0)
        elif feature_name == "market_cap_log10":
            transformed = _clip_feature(value - 9.0, scale=3.0)
        elif feature_name == "roe":
            transformed = _clip_feature(value, scale=25.0)
        elif feature_name == "debt_ratio":
            transformed = _clip_feature(value, scale=0.8)
        elif feature_name in {"revenue_growth", "profit_growth"}:
            transformed = _clip_feature(value, scale=0.4)
        elif feature_name == "momentum_20d":
            transformed = _clip_feature(value, scale=0.35)
        elif feature_name == "trend_20d":
            transformed = _clip_feature(value, scale=0.2)
        elif feature_name == "volatility_20d":
            transformed = _clip_feature(value, scale=0.08)
        elif feature_name == "volume_ratio_20d":
            transformed = _clip_feature(value - 1.0, scale=1.5)
        else:
            transformed = _clip_feature(value, scale=1.0)
        raw_vector.append(transformed)
    return _normalize_vector(raw_vector)


def build_stock_profile_summary(
    stock_info: dict[str, Any],
    features: dict[str, float],
    *,
    profile_type: str,
) -> str:
    name = stock_info.get("name") or stock_info.get("stock_name") or ""
    industry = stock_info.get("industry") or stock_info.get("sector") or ""
    return "\n".join(
        [
            "股票画像向量摘要",
            f"代码: {stock_info.get('code') or ''}",
            f"名称: {name}",
            f"行业: {industry}",
            f"画像类型: {profile_type}",
            f"PE: {float(features.get('pe_ratio') or 0.0):.4f}",
            f"PB: {float(features.get('pb_ratio') or 0.0):.4f}",
            f"市值log10: {float(features.get('market_cap_log10') or 0.0):.4f}",
            f"ROE: {float(features.get('roe') or 0.0):.4f}",
            f"资产负债率: {float(features.get('debt_ratio') or 0.0):.4f}",
            f"营收增速: {float(features.get('revenue_growth') or 0.0):.4f}",
            f"利润增速: {float(features.get('profit_growth') or 0.0):.4f}",
            f"20日动量: {float(features.get('momentum_20d') or 0.0):.4f}",
            f"20日趋势偏离: {float(features.get('trend_20d') or 0.0):.4f}",
            f"20日波动率: {float(features.get('volatility_20d') or 0.0):.4f}",
            f"20日量比: {float(features.get('volume_ratio_20d') or 0.0):.4f}",
        ]
    )


def build_stock_profile_vector_payload(
    *,
    stock_info: dict[str, Any],
    financial_row: Optional[dict[str, Any]] = None,
    klines: Optional[list[dict[str, Any]]] = None,
    profile_type: str = "both",
    version: str = "v1",
    market_regime: Optional[str] = None,
    active_factor_families: Optional[list[str]] = None,
) -> Optional[dict[str, Any]]:
    code = str(stock_info.get("code") or "").strip()
    if not code:
        return None
    normalized_profile_type = str(profile_type or "both").strip().lower()
    if normalized_profile_type not in _PROFILE_TYPES:
        normalized_profile_type = "both"
    klines_list = list(klines or [])
    features = build_stock_profile_features(stock_info, financial_row, klines_list)
    embedding = build_stock_profile_embedding(features, profile_type=normalized_profile_type)
    if not embedding:
        return None
    summary = build_stock_profile_summary(stock_info, features, profile_type=normalized_profile_type)
    entity_id = f"{code}|{normalized_profile_type}"

    raw_features_grouped = _build_raw_features_grouped(
        stock_info,
        financial_row,
        klines_list,
        features,
        market_regime=market_regime,
        active_factor_families=active_factor_families,
    )
    profile_summary = _build_profile_summary(
        raw_features_grouped,
        market_cap=_coerce_optional_float(stock_info.get("market_cap")),
        pe=_coerce_optional_float(stock_info.get("pe_ratio")),
        volatility_20d=features.get("volatility_20d"),
    )

    flat_raw_features = {key: round(float(features.get(key) or 0.0), 8) for key in _FEATURE_ORDER}
    signature_basis = json.dumps(
        {
            "entity_id": entity_id,
            "version": str(version or "v1"),
            "features": flat_raw_features,
            "summary_archetype": profile_summary.get("primary_archetype"),
            "summary_quality": profile_summary.get("profile_quality"),
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    return {
        "collection_name": "stock_profile_embeddings",
        "entity_type": "stock_profile",
        "entity_id": entity_id,
        "stock_code": code,
        "profile_type": normalized_profile_type,
        "model_id": "stock-profile-v1",
        "vector_dim": len(embedding),
        "metric": "cosine",
        "version": str(version or "v1"),
        "signature": hashlib.sha1(signature_basis.encode("utf-8")).hexdigest(),
        "status": "active",
        "embedding": embedding,
        "metadata": {
            "stock_name": stock_info.get("name") or stock_info.get("stock_name") or "",
            "industry": stock_info.get("industry") or stock_info.get("sector") or "",
            "summary_text": summary,
            "feature_order": list(_FEATURE_ORDER),
            # 向后兼容：feature_coverage 仍是扁平 feature 名列表
            "feature_coverage": sorted(list(_FEATURE_SET_BY_TYPE.get(normalized_profile_type, set()))),
            # 向后兼容：raw_features 保留扁平结构
            "raw_features": flat_raw_features,
            # PR-S18 新增：9 大维度 + cell-level coverage
            "raw_features_grouped": raw_features_grouped,
            # PR-S18 新增：profile_summary（archetype/factor_dimension_scores/recommended_families/quality）
            "profile_summary": profile_summary,
            "profile_version": "stock_profile_v2",
        },
    }


async def load_stock_profile_context(
    db,
    code: str,
    *,
    kline_limit: int = 90,
) -> Optional[dict[str, Any]]:
    stock_info = dict(await db.get_stock_info(code) or {})
    if not stock_info:
        return None
    stock_info["code"] = str(stock_info.get("code") or code).strip()
    financial_row = None
    try:
        financials = await db.get_financials(code, limit=1)
        if financials:
            financial_row = dict(financials[0] or {})
    except Exception:
        financial_row = None
    klines = []
    try:
        klines = list(await db.get_klines(code, limit=max(30, int(kline_limit or 90))) or [])
    except Exception:
        klines = []
    return {
        "stock_info": stock_info,
        "financial_row": financial_row,
        "klines": klines,
    }


async def build_stock_profile_payload(
    db,
    code: str,
    *,
    profile_type: str = "both",
    kline_limit: int = 90,
    version: str = "v1",
    market_regime: Optional[str] = None,
    active_factor_families: Optional[list[str]] = None,
) -> Optional[dict[str, Any]]:
    context = await load_stock_profile_context(db, code, kline_limit=kline_limit)
    if not context:
        return None
    return build_stock_profile_vector_payload(
        stock_info=context.get("stock_info") or {},
        financial_row=context.get("financial_row"),
        klines=context.get("klines") or [],
        profile_type=profile_type,
        version=version,
        market_regime=market_regime,
        active_factor_families=active_factor_families,
    )


async def _load_candidate_rows(
    db,
    *,
    stock_codes: list[str],
    code_limit: int,
) -> list[dict[str, Any]]:
    if stock_codes:
        rows: list[dict[str, Any]] = []
        for code in stock_codes:
            info = dict(await db.get_stock_info(code) or {})
            info["code"] = str(info.get("code") or code).strip()
            rows.append(info)
        return [row for row in rows if row.get("code")]
    if hasattr(db, "list_stock_universe"):
        rows = await db.list_stock_universe(limit=code_limit)
        return [dict(row or {}) for row in rows if str(dict(row or {}).get("code") or "").strip()]
    return []


async def _profile_exists(db, *, entity_id: str, version: str) -> bool:
    if not hasattr(db, "list_vector_profiles"):
        return False
    try:
        rows = await db.list_vector_profiles(
            collection_name="stock_profile_embeddings",
            entity_id=entity_id,
            version=version,
            limit=1,
        )
    except Exception:
        return False
    return bool(rows)


async def backfill_stock_profile_vectors(
    db,
    *,
    stock_codes: Any = None,
    code_limit: Any = 200,
    profile_types: Any = None,
    kline_limit: Any = 90,
    version: str = "v1",
    rebuild_existing: Any = False,
    dry_run: Any = False,
    market_regime: Optional[str] = None,
    active_factor_families: Optional[list[str]] = None,
) -> dict[str, Any]:
    resolved_codes = _normalize_codes(stock_codes)
    resolved_code_limit = _normalize_positive_int(code_limit, 200, minimum=1, maximum=10000)
    resolved_profile_types = _normalize_profile_types(profile_types)
    resolved_kline_limit = _normalize_positive_int(kline_limit, 90, minimum=30, maximum=500)
    resolved_rebuild_existing = bool(rebuild_existing)
    resolved_dry_run = bool(dry_run)

    candidate_rows = await _load_candidate_rows(
        db,
        stock_codes=resolved_codes,
        code_limit=resolved_code_limit,
    )
    quality_distribution: dict[str, int] = {}
    archetype_distribution: dict[str, int] = {}

    results = {
        "stock_codes": [str(row.get("code") or "").strip() for row in candidate_rows if str(row.get("code") or "").strip()],
        "code_count": len(candidate_rows),
        "profile_types": list(resolved_profile_types),
        "kline_limit": resolved_kline_limit,
        "version": str(version or "v1"),
        "rebuild_existing": resolved_rebuild_existing,
        "dry_run": resolved_dry_run,
        "processed_codes": 0,
        "skipped_codes": 0,
        "candidate_profiles": 0,
        "skipped_existing_profiles": 0,
        "saved_profiles": 0,
        "errors": [],
        # PR-S18 新增：可观测分布
        "profile_quality_distribution": quality_distribution,
        "profile_archetype_distribution": archetype_distribution,
    }
    collection_saved = False

    for row in candidate_rows:
        code = str(row.get("code") or "").strip()
        if not code:
            continue
        context = await load_stock_profile_context(db, code, kline_limit=resolved_kline_limit)
        if not context:
            results["skipped_codes"] += 1
            continue
        results["processed_codes"] += 1
        for profile_type in resolved_profile_types:
            payload = build_stock_profile_vector_payload(
                stock_info=context.get("stock_info") or {},
                financial_row=context.get("financial_row"),
                klines=context.get("klines") or [],
                profile_type=profile_type,
                version=str(version or "v1"),
                market_regime=market_regime,
                active_factor_families=active_factor_families,
            )
            if not payload:
                continue
            results["candidate_profiles"] += 1
            summary = dict((payload.get("metadata") or {}).get("profile_summary") or {})
            quality = str(summary.get("profile_quality") or "unknown")
            archetype = str(summary.get("primary_archetype") or "unknown")
            quality_distribution[quality] = quality_distribution.get(quality, 0) + 1
            archetype_distribution[archetype] = archetype_distribution.get(archetype, 0) + 1

            if not resolved_rebuild_existing and await _profile_exists(
                db,
                entity_id=str(payload.get("entity_id") or ""),
                version=str(payload.get("version") or "v1"),
            ):
                results["skipped_existing_profiles"] += 1
                continue
            if resolved_dry_run:
                results["saved_profiles"] += 1
                continue
            try:
                if not collection_saved and hasattr(db, "save_vector_collection"):
                    await db.save_vector_collection(
                        {
                            "collection_name": "stock_profile_embeddings",
                            "entity_family": "stock_profile",
                            "backend": str(getattr(db, "get_vector_backend", lambda: "sqlite_python")() or "sqlite_python"),
                            "metric": str(payload.get("metric") or "cosine"),
                            "model_id": str(payload.get("model_id") or "stock-profile-v1"),
                            "vector_dim": int(payload.get("vector_dim") or len(payload.get("embedding") or [])),
                            "status": "active",
                            "metadata": {
                                "domain": "market-quant",
                                "notes": "derived stock profile vectors",
                            },
                        }
                    )
                    collection_saved = True
                await db.save_vector_profile(payload)
                results["saved_profiles"] += 1
            except Exception as exc:
                results["errors"].append(f"{code}:{profile_type}:{type(exc).__name__}")

    if len(results["errors"]) > 20:
        total = len(results["errors"])
        results["errors"] = list(results["errors"][:20]) + [f"...及其他 {total - 20} 个错误"]
    return results
