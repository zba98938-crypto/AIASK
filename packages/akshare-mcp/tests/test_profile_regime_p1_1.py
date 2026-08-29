"""P1-1 单测：stock_profile_pipeline._build_profile_summary 增补 regime + holding_bucket_hint。

关联：开发周期计划-倒置架构与因子路由-2026-06-03.md · Phase 1 · P1-1
验收：profile_summary 向后兼容（旧字段保留）+ 新增 regime(trend/vol) 与 holding_bucket_hint；
      regime 派生与 P0-3 标签口径一致。
"""

from __future__ import annotations

from akshare_mcp.services import stock_profile_pipeline as spp


def _grouped(momentum_20d=None, momentum_60d=None, volatility_20d=None):
    g = {
        "price_trend_reversal": {},
        "volatility_risk": {},
        "volume_liquidity_microstructure": {},
        "valuation": {},
        "quality_growth_balance_sheet": {},
        "alternative_sentiment_capital_flow": {},
        "event_news_notice_research_theme": {},
    }
    # 真实 cell 结构为 {"value": x, "coverage": ..., ...}；派生函数兼容 dict/裸值两种。
    if momentum_20d is not None:
        g["price_trend_reversal"]["momentum_20d"] = {"value": momentum_20d}
    if momentum_60d is not None:
        g["price_trend_reversal"]["momentum_60d"] = {"value": momentum_60d}
    if volatility_20d is not None:
        g["volatility_risk"]["volatility_20d"] = {"value": volatility_20d}
    return g


def test_summary_has_regime_and_bucket_fields():
    summary = spp._build_profile_summary(
        _grouped(momentum_20d=0.12, momentum_60d=0.2, volatility_20d=0.015),
        market_cap=5e10, pe=20.0, volatility_20d=0.015,
    )
    # 向后兼容字段仍在
    assert "recommended_families" in summary
    assert "factor_dimension_scores" in summary
    # 新增字段
    assert "regime" in summary
    assert "holding_bucket_hint" in summary
    assert set(summary["regime"].keys()) == {"trend_regime", "vol_regime"}


def test_regime_trend_up():
    regime = spp._resolve_regime_from_features(
        _grouped(momentum_20d=0.12, momentum_60d=0.2), volatility_20d=0.010
    )
    assert regime["trend_regime"] == "trend_up"
    # 0.010 * sqrt(252) ≈ 0.159 <= 0.20 -> low_vol
    assert regime["vol_regime"] == "low_vol"


def test_regime_trend_down_and_high_vol():
    regime = spp._resolve_regime_from_features(
        _grouped(momentum_20d=-0.15, momentum_60d=-0.1), volatility_20d=0.04
    )
    assert regime["trend_regime"] == "trend_down"
    assert regime["vol_regime"] == "high_vol"  # 0.04*sqrt(252)≈0.635


def test_regime_range_when_flat():
    regime = spp._resolve_regime_from_features(
        _grouped(momentum_20d=0.0, momentum_60d=0.0), volatility_20d=0.02
    )
    assert regime["trend_regime"] == "range"


def test_regime_unknown_when_missing():
    regime = spp._resolve_regime_from_features(_grouped(), volatility_20d=None)
    assert regime["trend_regime"] == "unknown"
    assert regime["vol_regime"] == "unknown"


def test_holding_bucket_hint_mapping():
    assert spp._holding_bucket_hint_from_archetype("trend_following") == "medium"
    assert spp._holding_bucket_hint_from_archetype("mean_reversion") == "short"
    assert spp._holding_bucket_hint_from_archetype("value_oriented") == "long"
    assert spp._holding_bucket_hint_from_archetype("unknown_x") == "medium"
