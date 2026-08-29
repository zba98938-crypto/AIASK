"""P1-2 单测：StockStrategyRouter 接入逐股矩阵 _intrinsic_families_for_row。

关联：开发周期计划-倒置架构与因子路由-2026-06-03.md · Phase 1 · P1-2
验收：toggle OFF → 维持 recommended_families/sector 偏置（不调用路由器）；
      toggle ON + 画像可用 → family 由路由器决定（趋势画像 → 趋势族 + 震荡排除趋势）。
"""

from __future__ import annotations

from strategy_factory.domain import constants as _matrix_const

import strategy_factory.application.stock_strategy_matrix as matrix_module
from strategy_factory.application.stock_strategy_matrix import StockStrategyMatrixPlanner


def _row_with_profile(trend_regime: str, *, trend_score: float, quality: str = "good") -> dict:
    return {
        "code": "600519",
        "industry": "白酒",
        "pe_ratio": 30.0,
        "pb_ratio": 8.0,
        "volume_ratio_5_20": 1.5,
        "stock_profile": {
            "metadata": {
                "profile_summary": {
                    "profile_quality": quality,
                    "factor_dimension_scores": {
                        "trend": trend_score,
                        "valuation": 0.1,
                        "quality": 0.2,
                    },
                    "recommended_families": ["multi_factor", "quality_factor"],
                    "feature_coverage": {"event_news_notice_research_theme": "missing"},
                    "regime": {"trend_regime": trend_regime, "vol_regime": "normal_vol"},
                    "holding_bucket_hint": "medium",
                }
            }
        },
    }


def _intrinsic(row):
    return StockStrategyMatrixPlanner._intrinsic_families_for_row(
        row,
        snapshot={"date": "2026-06-03"},
        hot_sectors=set(),
        cold_sectors=set(),
        active_factors=[],
    )


def _intrinsic_with_snapshot(row, snapshot):
    return StockStrategyMatrixPlanner._intrinsic_families_for_row(
        row,
        snapshot=snapshot,
        hot_sectors=set(),
        cold_sectors=set(),
        active_factors=[],
    )


def test_off_does_not_use_router(monkeypatch):
    monkeypatch.setattr(_matrix_const, "STOCK_FIRST_ROUTER_ENABLED", False)
    row = _row_with_profile("trend_up", trend_score=0.8)
    families = _intrinsic(row)
    # OFF：走 recommended_families（含 multi_factor），不强制只返回趋势族
    assert "multi_factor" in families


def test_on_trend_up_routes_to_trend_families(monkeypatch):
    monkeypatch.setattr(_matrix_const, "STOCK_FIRST_ROUTER_ENABLED", True)
    row = _row_with_profile("trend_up", trend_score=0.8)
    families = _intrinsic(row)
    assert "momentum" in families
    assert "ma_cross" in families


def test_stock_first_execution_mode_forces_strict_router(monkeypatch):
    monkeypatch.setattr(_matrix_const, "STOCK_FIRST_ROUTER_ENABLED", False)
    monkeypatch.setattr(_matrix_const, "STOCK_FIRST_ROUTER_STRICT", False)
    row = _row_with_profile("trend_up", trend_score=0.8)

    families = _intrinsic_with_snapshot(
        row,
        {"date": "2026-06-03", "factory_execution_mode": "stock_first_observe_primary"},
    )

    assert "momentum" in families
    assert "ma_cross" in families
    assert row["_stock_first_router"]["enabled"] is True
    assert row["_stock_first_router"]["strict"] is True
    assert row["_stock_first_router"]["status"] == "applied"


def test_stock_first_execution_mode_enables_matrix_when_toggle_off(monkeypatch):
    monkeypatch.setattr(_matrix_const, "STOCK_STRATEGY_MATRIX_ENABLED", False)
    assert StockStrategyMatrixPlanner._effective_stock_matrix_enabled(
        {"factory_execution_mode": "stock_first_observe_primary"}
    ) is True
    assert StockStrategyMatrixPlanner._effective_stock_matrix_enabled(
        {"factory_execution_mode": "legacy_primary"}
    ) is False


def test_on_range_excludes_trend_families(monkeypatch):
    monkeypatch.setattr(_matrix_const, "STOCK_FIRST_ROUTER_ENABLED", True)
    row = _row_with_profile("range", trend_score=0.9)
    families = _intrinsic(row)
    # 震荡股：趋势族不应出现
    assert "momentum" not in families
    assert "ma_cross" not in families


def test_on_failed_profile_falls_back_to_legacy(monkeypatch):
    monkeypatch.setattr(_matrix_const, "STOCK_FIRST_ROUTER_ENABLED", True)
    monkeypatch.setattr(_matrix_const, "STOCK_FIRST_ROUTER_STRICT", False)
    row = _row_with_profile("trend_up", trend_score=0.8, quality="failed")
    families = _intrinsic(row)
    # profile_quality=failed → 不走路由器，回退既有逻辑（仍返回非空 family 列表）
    assert isinstance(families, list)
    assert len(families) >= 1


def test_strict_missing_profile_blocks_legacy_fallback(monkeypatch):
    monkeypatch.setattr(_matrix_const, "STOCK_FIRST_ROUTER_ENABLED", True)
    monkeypatch.setattr(_matrix_const, "STOCK_FIRST_ROUTER_STRICT", True)
    row = {
        "code": "600519",
        "industry": "白酒",
        "pe_ratio": 30.0,
        "pb_ratio": 8.0,
    }
    families = _intrinsic(row)
    assert families == []
    assert row["_stock_first_router"]["status"] == "blocked"
    assert row["_stock_first_router"]["reason"] == "missing_profile_summary"


def test_strict_ignores_allocation_plans_without_profile(monkeypatch):
    monkeypatch.setattr(_matrix_const, "STOCK_FIRST_ROUTER_ENABLED", True)
    monkeypatch.setattr(_matrix_const, "STOCK_FIRST_ROUTER_STRICT", True)
    row = {
        "code": "600519",
        "industry": "白酒",
        "pe_ratio": 30.0,
        "pb_ratio": 8.0,
    }
    plans = StockStrategyMatrixPlanner._family_plans_for_row(
        row,
        snapshot={"date": "2026-06-03"},
        hot_sectors=set(),
        cold_sectors=set(),
        active_factors=[],
        allocation_item={"family_plans": [{"family": "macro_timing", "family_rank": 1}]},
    )
    assert plans == []
    assert row["_stock_first_router"]["reason"] == "missing_profile_summary"
