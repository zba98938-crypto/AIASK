"""个股方向闸单测：legacy 选族路径下按 trend/reversal 方向剔除矛盾 family。

修复 momentum 策略被配到下跌标的导致方向命中率系统性偏低的问题。
"""

from __future__ import annotations

from strategy_factory.domain import constants as _matrix_const


def _gate():
    from strategy_factory.application.stock_strategy_matrix import (
        StockStrategyMatrixPlanner,
    )

    return StockStrategyMatrixPlanner._apply_direction_gate


def test_downtrend_stock_drops_trend_families():
    """下跌/弱势标的（trend 低 + reversal 高）剔除 momentum 等趋势族。"""
    gate = _gate()
    families = ["momentum", "ma_cross", "multi_factor", "value_factor"]
    profile = {"factor_dimension_scores": {"trend": 0.05, "reversal": 0.40}}
    result = gate(families, profile_summary=profile, row={"pct_chg": -2.5})
    assert "momentum" not in result
    assert "ma_cross" not in result
    # 中性/基本面族保留
    assert "multi_factor" in result
    assert "value_factor" in result


def test_downtrend_via_negative_pct_chg_drops_trend_families():
    """trend 低 + 当日下跌（即便 reversal 不高）也剔除趋势族。"""
    gate = _gate()
    families = ["momentum", "growth_factor", "multi_factor"]
    profile = {"factor_dimension_scores": {"trend": 0.10, "reversal": 0.10}}
    result = gate(families, profile_summary=profile, row={"pct_chg": -1.8})
    assert "momentum" not in result
    assert "growth_factor" not in result
    assert "multi_factor" in result


def test_uptrend_stock_drops_reversal_families():
    """上涨标的（trend 高）剔除纯反转族，保留趋势族。"""
    gate = _gate()
    families = ["momentum", "mean_reversion_short", "gap_fill", "multi_factor"]
    profile = {"factor_dimension_scores": {"trend": 0.50, "reversal": 0.05}}
    result = gate(families, profile_summary=profile, row={"pct_chg": 2.1})
    assert "mean_reversion_short" not in result
    assert "gap_fill" not in result
    assert "momentum" in result
    assert "multi_factor" in result


def test_missing_direction_signals_no_filter():
    """方向信号完全缺失时不过滤（诚实降级，不瞎剔）。"""
    gate = _gate()
    families = ["momentum", "rsi"]
    result = gate(families, profile_summary={}, row={})
    assert result == families


def test_mid_regime_no_filter():
    """中间地带（trend 0.15-0.35）不干预，避免误伤震荡标的。"""
    gate = _gate()
    families = ["momentum", "ma_cross"]
    profile = {"factor_dimension_scores": {"trend": 0.25, "reversal": 0.10}}
    result = gate(families, profile_summary=profile, row={"pct_chg": 0.3})
    assert result == families


def test_empty_after_drop_falls_back_to_multi_factor():
    """全部是趋势族且标的下跌，剔除后兜底 multi_factor，候选不枯竭。"""
    gate = _gate()
    families = ["momentum", "ma_cross", "growth_factor"]
    profile = {"factor_dimension_scores": {"trend": 0.02, "reversal": 0.50}}
    result = gate(families, profile_summary=profile, row={"pct_chg": -3.0})
    assert result == ["multi_factor"]


def test_empty_families_returns_empty():
    """空输入直接返回空。"""
    gate = _gate()
    assert gate([], profile_summary={"factor_dimension_scores": {"trend": 0.5}}, row={}) == []


def test_toggle_off_disables_gate(monkeypatch):
    """toggle 关闭时不过滤（紧急回退到旧逻辑）。"""
    import strategy_factory.application.stock_strategy_matrix as mod

    monkeypatch.setattr(_matrix_const, "STOCK_DIRECTION_GATE_ENABLED", False)
    gate = mod.StockStrategyMatrixPlanner._apply_direction_gate
    families = ["momentum", "ma_cross"]
    profile = {"factor_dimension_scores": {"trend": 0.02, "reversal": 0.50}}
    result = gate(families, profile_summary=profile, row={"pct_chg": -3.0})
    assert result == families


def test_router_path_gates_downtrend_growth_family(monkeypatch):
    import strategy_factory.application.stock_strategy_matrix as mod

    monkeypatch.setattr(_matrix_const, "STOCK_FIRST_ROUTER_ENABLED", True)
    monkeypatch.setattr(_matrix_const, "STOCK_FIRST_ROUTER_STRICT", False)
    row = {
        "code": "600001",
        "industry": "bank",
        "pe_ratio": 12.0,
        "pb_ratio": 1.2,
        "volume_ratio_5_20": 1.4,
        "stock_profile": {
            "metadata": {
                "profile_summary": {
                    "profile_quality": "good",
                    "factor_dimension_scores": {
                        "trend": 0.75,
                        "valuation": 0.65,
                        "quality": 0.55,
                    },
                    "regime": {"trend_regime": "trend_down", "vol_regime": "normal_vol"},
                }
            }
        },
    }

    families = mod.StockStrategyMatrixPlanner._intrinsic_families_for_row(
        row,
        snapshot={"date": "2026-06-13"},
        hot_sectors=set(),
        cold_sectors=set(),
        active_factors=[],
    )

    assert "growth_factor" not in families
    assert "value_factor" in families
    assert "quality_factor" in families
    gate_status = row["_stock_direction_gate"]
    assert gate_status["source"] == "router"
    assert gate_status["status"] == "applied"
    assert gate_status["direction"] == "downtrend"
    assert gate_status["dropped_families"] == ["growth_factor"]
    assert "growth_factor" in row["_stock_first_router"]["exclusions"]


def test_family_plan_path_preserves_router_gate_diagnostic(monkeypatch):
    import strategy_factory.application.stock_strategy_matrix as mod

    monkeypatch.setattr(_matrix_const, "STOCK_FIRST_ROUTER_ENABLED", True)
    monkeypatch.setattr(_matrix_const, "STOCK_FIRST_ROUTER_STRICT", False)
    row = {
        "code": "600010",
        "industry": "bank",
        "pe_ratio": 10.0,
        "pb_ratio": 1.1,
        "stock_profile": {
            "metadata": {
                "profile_summary": {
                    "profile_quality": "good",
                    "factor_dimension_scores": {
                        "trend": 0.70,
                        "valuation": 0.60,
                        "quality": 0.60,
                    },
                    "regime": {"trend_regime": "trend_down"},
                }
            }
        },
    }

    plans = mod.StockStrategyMatrixPlanner._family_plans_for_row(
        row,
        snapshot={"date": "2026-06-13"},
        hot_sectors=set(),
        cold_sectors=set(),
        active_factors=[],
    )

    families = [plan["family"] for plan in plans]
    assert "growth_factor" not in families
    assert row["_stock_direction_gate"]["status"] == "applied"
    assert row["_stock_direction_gate"]["source"] == "router"


def test_allocation_plan_gate_falls_back_safely():
    from strategy_factory.application.stock_strategy_matrix import StockStrategyMatrixPlanner

    row = {
        "code": "600002",
        "industry": "tech",
        "stock_profile": {
            "metadata": {
                "profile_summary": {
                    "profile_quality": "good",
                    "factor_dimension_scores": {"trend": 0.02, "reversal": 0.50},
                    "regime": {"trend_regime": "trend_down"},
                }
            }
        },
    }

    plans = StockStrategyMatrixPlanner._family_plans_for_row(
        row,
        snapshot={"date": "2026-06-13"},
        hot_sectors=set(),
        cold_sectors=set(),
        active_factors=[],
        allocation_item={
            "priority": 0.8,
            "family_plans": [
                {"family": "momentum", "family_rank": 1},
                {"family": "growth_factor", "family_rank": 2},
            ],
        },
    )

    assert [plan["family"] for plan in plans] == ["multi_factor"]
    gate_status = row["_stock_direction_gate"]
    assert gate_status["source"] == "allocation_plan"
    assert gate_status["fallback_family"] == "multi_factor"
    assert gate_status["dropped_families"] == ["momentum", "growth_factor"]


def test_direction_gate_telemetry_counts_drops_and_fallbacks():
    from strategy_factory.application.stock_strategy_matrix import StockStrategyMatrixPlanner

    row = {"code": "600003", "pct_chg": -2.0}
    result = StockStrategyMatrixPlanner._apply_direction_gate(
        ["momentum", "growth_factor"],
        profile_summary={"factor_dimension_scores": {"trend": 0.01, "reversal": 0.40}},
        row=row,
        source="legacy",
    )
    telemetry = StockStrategyMatrixPlanner._direction_gate_telemetry_for_rows(
        [row],
        selected_tasks=[{"stock_direction_gate": row["_stock_direction_gate"]}],
    )

    assert result == ["multi_factor"]
    assert telemetry["direction_gate_applied_count"] == 1
    assert telemetry["direction_gate_fallback_count"] == 1
    assert telemetry["direction_gate_dropped_family_counts"] == {
        "momentum": 1,
        "growth_factor": 1,
    }
    assert telemetry["selected_direction_gate_applied_count"] == 1
