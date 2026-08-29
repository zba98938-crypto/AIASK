"""P1-2 单测：StockStrategyRouter 诊断→类型路由（透明 if/score 规则）。

关联：开发周期计划-倒置架构与因子路由-2026-06-03.md · Phase 1 · P1-2
验收：趋势股→趋势族；超卖股→均值回归族；低估值→价值族；震荡股不被套趋势；
      低流动性排除短线高频；无命中→fallback multi_factor；profile 映射正确。
"""

from __future__ import annotations

from strategy_factory.application.stock_strategy_router import (
    RouteResult,
    StockRegimeProfile,
    route_strategies,
)


def test_trend_up_routes_to_trend_families():
    p = StockRegimeProfile(
        code="600519", trend_regime="trend_up", vol_regime="normal_vol",
        momentum_score=0.7, volume_ratio=1.5, profile_quality="good",
    )
    r = route_strategies(p)
    assert "momentum" in r.families
    assert "ma_cross" in r.families
    assert r.holding_period_bucket == "medium"
    assert r.confidence > 0.0


def test_oversold_routes_to_mean_reversion():
    p = StockRegimeProfile(
        code="000001", trend_regime="range", rsi=25.0, reversal_score=0.5,
        volume_ratio=0.7, profile_quality="good",
    )
    r = route_strategies(p)
    assert "mean_reversion_short" in r.families or "rsi" in r.families
    assert r.holding_period_bucket == "short"


def test_low_valuation_quality_routes_to_value():
    p = StockRegimeProfile(
        code="601318", trend_regime="range", valuation_score=0.6, quality_score=0.5,
        profile_quality="good",
    )
    r = route_strategies(p)
    assert "value_factor" in r.families
    assert "quality_factor" in r.families
    assert r.holding_period_bucket == "long"


def test_range_excludes_trend_families():
    """震荡股：趋势族必须被排除，不能被套趋势策略。"""
    p = StockRegimeProfile(
        code="600000", trend_regime="range", momentum_score=0.9, volume_ratio=2.0,
        valuation_score=0.5, quality_score=0.5, profile_quality="good",
    )
    r = route_strategies(p)
    for fam in ("momentum", "ma_cross", "volatility_breakout", "event_structure_breakout"):
        assert fam not in r.families
        assert fam in r.exclusions


def test_downtrend_excludes_trend_families():
    p = StockRegimeProfile(
        code="600001", trend_regime="trend_down", momentum_score=0.8, volume_ratio=1.5,
        profile_quality="partial",
    )
    r = route_strategies(p)
    assert "momentum" not in r.families


def test_low_liquidity_excludes_short_highfreq():
    p = StockRegimeProfile(
        code="600002", trend_regime="range", rsi=20.0, reversal_score=0.6,
        liquidity_low=True, profile_quality="good",
    )
    r = route_strategies(p)
    assert "gap_fill" in r.exclusions
    assert "mean_reversion_short" in r.exclusions
    assert "gap_fill" not in r.families
    assert "mean_reversion_short" not in r.families


def test_event_catalyst_routes_to_event_rotation():
    p = StockRegimeProfile(
        code="600003", trend_regime="trend_up", momentum_score=0.4,
        event_catalyst=True, sentiment_regime="greed", profile_quality="good",
    )
    r = route_strategies(p)
    assert "sector_rotation" in r.families or "event_structure_breakout" in r.families


def test_no_rule_matched_falls_back_to_multi_factor():
    p = StockRegimeProfile(code="600004", trend_regime="unknown", profile_quality="failed")
    r = route_strategies(p)
    assert r.families == ["multi_factor"]
    assert any("fallback" in reason for reason in r.rationale)


def test_low_confidence_profile_discounts_confidence():
    good = route_strategies(StockRegimeProfile(
        code="x", trend_regime="trend_up", momentum_score=0.8, volume_ratio=1.5, profile_quality="good",
    ))
    weak = route_strategies(StockRegimeProfile(
        code="x", trend_regime="trend_up", momentum_score=0.8, volume_ratio=1.5, profile_quality="low_confidence",
    ))
    assert weak.confidence < good.confidence


def test_from_profile_summary_mapping():
    profile_summary = {
        "factor_dimension_scores": {"trend": 0.7, "valuation": 0.1, "quality": 0.2},
        "profile_quality": "good",
    }
    regime = {"trend_regime": "trend_up", "vol_regime": "normal_vol", "sentiment_regime": "neutral"}
    p = StockRegimeProfile.from_profile_summary(
        "600519", profile_summary, regime, extras={"rsi": 60.0, "volume_ratio": 1.3}
    )
    assert p.trend_regime == "trend_up"
    assert p.momentum_score == 0.7
    assert p.rsi == 60.0
    assert p.volume_ratio == 1.3
    r = route_strategies(p)
    assert isinstance(r, RouteResult)
    assert "momentum" in r.families
