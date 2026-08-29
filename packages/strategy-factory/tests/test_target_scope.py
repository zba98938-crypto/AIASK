from __future__ import annotations

from strategy_factory.domain import constants as _matrix_const

import asyncio


class _UniverseDb:
    def __init__(self):
        self.rows = [
            {
                "code": "600519",
                "name": "Kweichow Moutai",
                "industry": "consumer",
                "market_cap": 2_000_000_000_000,
                "pe_ratio": 25,
                "pb_ratio": 8,
            },
            {
                "code": "000001",
                "name": "Ping An Bank",
                "industry": "bank",
                "market_cap": 300_000_000_000,
                "pe_ratio": 6,
                "pb_ratio": 0.8,
            },
            {
                "code": "300750",
                "name": "CATL",
                "industry": "new energy",
                "market_cap": 900_000_000_000,
                "pe_ratio": 30,
                "pb_ratio": 5,
            },
        ]

    def list_stock_universe(self, *, limit: int, offset: int = 0):
        return self.rows[offset : offset + limit]


def _targeted_snapshot() -> dict:
    return {
        "date": "2026-05-19",
        "candidate_codes": ["600519", "000001"],
        "fear_greed_index": 55,
        "fg_level": "neutral",
        "hot_sectors": ["consumer"],
        "cold_sectors": [],
        "factor_ic_trend": {},
        "factor_research": {},
    }


def _task_codes(report: dict) -> set[str]:
    codes: set[str] = set()
    for task in list(report.get("tasks") or []):
        codes.update(str(code) for code in list(task.get("target_symbols") or []) if str(code))
    return codes


def test_opportunity_scanner_respects_explicit_candidate_codes():
    from strategy_factory.application.opportunity import MarketOpportunityScanner

    report = asyncio.run(MarketOpportunityScanner().scan(_UniverseDb(), _targeted_snapshot()))

    summary = dict(report.get("summary") or {})
    assert summary["target_code_filter_applied"] is True
    assert summary["requested_target_codes"] == ["600519", "000001"]
    assert summary["universe_row_count"] == 2
    assert _task_codes(report) <= {"600519", "000001"}


def test_stock_strategy_matrix_respects_explicit_candidate_codes(monkeypatch):
    import strategy_factory.application.stock_strategy_matrix as matrix_module

    monkeypatch.setattr(_matrix_const, "STOCK_STRATEGY_MATRIX_ENABLED", True)
    monkeypatch.setattr(_matrix_const, "STOCK_STRATEGY_MATRIX_UNIVERSE_LIMIT", 10)
    monkeypatch.setattr(_matrix_const, "STOCK_STRATEGY_MATRIX_MAX_TASKS_PER_RUN", 20)
    monkeypatch.setattr(_matrix_const, "STOCK_STRATEGY_MATRIX_MAX_CANDIDATES_PER_RUN", 20)
    monkeypatch.setattr(_matrix_const, "STRATEGY_FACTORY_VECTOR_REUSE_ENABLED", False)
    monkeypatch.setattr(_matrix_const, "STRATEGY_FACTORY_VECTOR_SIMILAR_PROFILE_ENABLED", False)

    report = asyncio.run(
        matrix_module.StockStrategyMatrixPlanner().plan(_UniverseDb(), _targeted_snapshot())
    )

    summary = dict(report.get("summary") or {})
    assert summary["target_code_filter_applied"] is True
    assert summary["requested_target_codes"] == ["600519", "000001"]
    assert summary["loaded_stock_count"] == 2
    assert _task_codes(report) <= {"600519", "000001"}


def test_stock_strategy_matrix_strict_generates_lightweight_profiles(monkeypatch):
    import strategy_factory.application.stock_strategy_matrix as matrix_module

    monkeypatch.setattr(_matrix_const, "STOCK_STRATEGY_MATRIX_ENABLED", True)
    monkeypatch.setattr(_matrix_const, "STOCK_FIRST_ROUTER_ENABLED", True)
    monkeypatch.setattr(_matrix_const, "STOCK_FIRST_ROUTER_STRICT", True)
    monkeypatch.setattr(_matrix_const, "STOCK_STRATEGY_MATRIX_UNIVERSE_LIMIT", 10)
    monkeypatch.setattr(_matrix_const, "STOCK_STRATEGY_MATRIX_MAX_TASKS_PER_RUN", 20)
    monkeypatch.setattr(_matrix_const, "STOCK_STRATEGY_MATRIX_MAX_CANDIDATES_PER_RUN", 20)
    monkeypatch.setattr(_matrix_const, "STRATEGY_FACTORY_VECTOR_REUSE_ENABLED", False)
    monkeypatch.setattr(_matrix_const, "STRATEGY_FACTORY_VECTOR_SIMILAR_PROFILE_ENABLED", False)

    report = asyncio.run(
        matrix_module.StockStrategyMatrixPlanner().plan(_UniverseDb(), _targeted_snapshot())
    )

    summary = dict(report.get("summary") or {})
    assert summary["router_enabled"] is True
    assert summary["router_strict"] is True
    assert summary["profile_summary_generated_count"] == 2
    assert summary["profile_summary_missing_count"] == 0
    assert summary["router_applied_count"] == 2
    assert summary["selected_router_applied_count"] >= 1
    assert _task_codes(report) <= {"600519", "000001"}
    assert all(task.get("stock_first_router", {}).get("status") == "applied" for task in report["tasks"])


def test_factor_rank_validation_expands_sample_panel_for_statistical_gate():
    from strategy_factory.domain.targets import _resolve_strategy_sample_selection

    selection = _resolve_strategy_sample_selection(
        "value_factor",
        {
            "target_symbols": ["603979", "603993", "688009", "688187"],
            "stock_pool": {
                "selection_mode": "explicit",
                "symbols": ["603979", "603993", "688009", "688187"],
            },
            "validation_profile": {
                "profile": "factor_rank_validation",
                "validation_focus": "candidate_target_only",
                "primary_validation_layer": "target",
            },
        },
    )

    assert selection["requested_sample_size"] == 6
    assert selection["effective_sample_size"] == 12
    assert selection["statistical_sample_min"] == 12
    assert selection["statistical_sample_expanded"] is True
    assert selection["sample_code_count"] >= 10
    assert selection["sample_codes"][:4] == ["603979", "603993", "688009", "688187"]
    assert selection["sample_selection_mode"] == "target_plus_dynamic_family_peer"


def test_non_factor_profile_panel_floors_at_statistical_minimum():
    """策略工厂零产出主因回归：trade_rule_validation（momentum/ma_cross 用）等
    非 factor_rank 档位，旧实现 effective_sample_size 直接等于 requested(6)，
    导致面板 < IC 引擎 min_samples(10) → OOS 证据恒空 → Gate-3 全拒。
    修复后任何 profile 的验证面板都被抬到统计可行下限 12。"""
    from strategy_factory.domain.targets import (
        _STATISTICAL_PANEL_MIN_STOCKS,
        _resolve_strategy_sample_selection,
    )

    assert _STATISTICAL_PANEL_MIN_STOCKS > 10  # 必须严格大于 IC 引擎 min_samples_per_period

    for sample_size in (4, 6):
        selection = _resolve_strategy_sample_selection(
            "momentum",
            {
                "validation_profile": {
                    "profile": "trade_rule_validation",
                    "validation_focus": "target_plus_family_peer",
                },
            },
            sample_size=sample_size,
        )
        assert selection["requested_sample_size"] == sample_size
        assert selection["effective_sample_size"] >= _STATISTICAL_PANEL_MIN_STOCKS
        assert selection["statistical_sample_min"] == _STATISTICAL_PANEL_MIN_STOCKS
        assert selection["statistical_sample_expanded"] is True
        # 面板有效股票数必须达到 IC 引擎要求，否则 n_folds 恒为 0
        assert selection["sample_code_count"] >= 10


def test_large_requested_sample_size_is_not_capped_by_floor():
    """请求样本数已超过下限时，floor 不应反向压低它。"""
    from strategy_factory.domain.targets import _resolve_strategy_sample_selection

    selection = _resolve_strategy_sample_selection(
        "momentum",
        {
            "validation_profile": {
                "profile": "trade_rule_validation",
                "validation_focus": "target_plus_family_peer",
            },
        },
        sample_size=30,
    )
    assert selection["requested_sample_size"] == 30
    assert selection["effective_sample_size"] == 30
    assert selection["statistical_sample_expanded"] is False
