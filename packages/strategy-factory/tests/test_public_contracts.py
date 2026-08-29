from __future__ import annotations

import math
from dataclasses import asdict

import pytest


def _assert_all_finite(value) -> None:
    if isinstance(value, dict):
        for nested in value.values():
            _assert_all_finite(nested)
        return
    if isinstance(value, list):
        for nested in value:
            _assert_all_finite(nested)
        return
    if isinstance(value, float):
        assert math.isfinite(value)


def test_package_facade_exposes_lazy_exports() -> None:
    import strategy_factory

    exported = set(strategy_factory.__all__)
    assert "StrategyFactoryScheduler" in exported
    assert "get_factory_constants" in exported
    assert "auto_name" in exported
    assert "STRATEGY_FACTORY_CONFIDENCE_DIAGNOSTICS_ENABLED" in exported
    assert isinstance(strategy_factory.STRATEGY_FACTORY_CONFIDENCE_DIAGNOSTICS_ENABLED, bool)


def test_strategy_naming_is_deterministic() -> None:
    from strategy_factory.api.facade import auto_name

    assert auto_name("ma_cross", {"short_period": 5, "long_period": 20}) == "均线交叉·快5慢20"
    assert auto_name("unknown_family", {}) == "unknown_family策略"


def test_run_model_status_helpers() -> None:
    from strategy_factory.application.run_models import (
        FactoryRunStatus,
        StageStatus,
        build_stage_result,
        normalize_run_status,
        normalize_stage_status,
        resolve_run_status,
        summarize_stage_results,
    )

    assert normalize_stage_status("done") is StageStatus.COMPLETED
    assert normalize_stage_status("warning") is StageStatus.PARTIAL
    assert normalize_run_status("success") is FactoryRunStatus.SUCCESS
    assert normalize_run_status("unknown") is FactoryRunStatus.FAILED
    assert normalize_run_status(None) is FactoryRunStatus.FAILED

    stages = {
        "spawn": build_stage_result("spawn", "trace_1", {"warnings": ["low sample"]}, status="partial"),
        "submit": build_stage_result("submit", "trace_1", status="completed"),
    }
    summary = summarize_stage_results(stages)
    assert summary["partial_stage_count"] == 1
    assert summary["degraded_stage_count"] == 1
    assert resolve_run_status("success", stages) is FactoryRunStatus.PARTIAL


def test_contract_normalization_helpers() -> None:
    from strategy_factory.api.contracts import normalize_execution_assumptions, normalize_strategy_preferences

    assert normalize_strategy_preferences(["Momentum", "momentum", "Value"], "Quality") == [
        "momentum",
        "value",
        "quality",
    ]

    normalized = normalize_execution_assumptions(
        {"slippage_bps": "8", "commission_rate": "0.0003"},
        holding_horizon={"max_days": 6},
        capacity_assumption={"capacity_bucket": "small"},
    )
    assert normalized["slippage_bps"] == 8.0
    assert normalized["commission_rate"] == 0.0003
    assert normalized["expected_turnover_band"] == "high"
    assert normalized["turnover_cost_class"] == "medium_touch"


def test_execution_assumption_normalization_rejects_non_finite_values() -> None:
    from strategy_factory.api.contracts import normalize_execution_assumptions

    normalized = normalize_execution_assumptions(
        {
            "slippage_bps": "inf",
            "slippage": float("nan"),
            "commission_rate": "-inf",
            "market_impact_bps": float("inf"),
            "capacity_participation_rate": "nan",
            "adv_ratio_limit": "inf",
            "margin_rate": "-inf",
            "contract_multiplier": "inf",
            "max_contracts_per_rebalance": float("nan"),
        },
        holding_horizon={"max_days": "inf"},
        capacity_assumption={"capacity_bucket": "small", "capacity_participation_rate": "inf"},
        cost_sensitivity_grid={
            "base_case": {
                "slippage_bps": 6.0,
                "commission_rate": 0.0002,
                "market_impact_bps": 1.5,
            }
        },
    )

    assert normalized["slippage_bps"] == pytest.approx(6.0)
    assert normalized["commission_rate"] == 0.0002
    assert normalized["market_impact_bps"] == 1.5
    assert normalized["capacity_participation_rate"] == 0.0
    assert normalized["adv_ratio_limit"] == 0.0
    assert normalized["margin_rate"] == 0.0
    assert normalized["contract_multiplier"] == 0
    assert normalized["max_contracts_per_rebalance"] == 0
    _assert_all_finite(normalized)


def test_semantic_contract_exports_target_alignment_helper() -> None:
    from strategy_factory.api.semantic_contract import (
        _apply_target_symbol_policy,
        _build_target_alignment_contract,
        _normalize_research_task_contract,
    )

    task = _normalize_research_task_contract(
        {
            "task_source": "snapshot",
            "target_symbols": ["600519", "000001"],
            "candidate_family": "momentum",
        }
    )
    contract = _build_target_alignment_contract(task, candidate={"strategy_type": "momentum"})

    assert callable(_apply_target_symbol_policy)
    assert contract["quality_gate_enabled"] is True
    assert contract["max_candidate_target_symbols"] >= 1


def test_stage_result_dto_round_trip() -> None:
    from strategy_factory.api.dto import StageResultDTO

    dto = StageResultDTO.from_dict(
        "deduplicate",
        {
            "status": "warning",
            "ok": True,
            "degraded": True,
            "warning_count": 2,
            "blocker_count": 0,
            "persistence_failure_count": 1,
        },
    )
    assert asdict(dto)["status"] == "partial"
    assert dto.to_dict()["stage"] == "deduplicate"
    assert dto.to_dict()["warning_count"] == 2


def test_factory_run_summary_dto_exposes_formal_runtime_ready_budget_counts() -> None:
    from strategy_factory.api.dto import FactoryRunSummaryDTO

    dto = FactoryRunSummaryDTO.from_dict(
        {
            "run_id": "run-formal-budget",
            "trace_id": "trace-formal-budget",
            "status": "partial",
            "started_at": "2026-06-18T09:00:00+08:00",
            "summary": {
                "incubation_budget_formal_runtime_ready_candidate_count": 3,
                "incubation_budget_formal_runtime_ready_selected_count": 1,
            },
        }
    )

    payload = dto.to_dict()

    assert payload["incubation_budget_formal_runtime_ready_candidate_count"] == 3
    assert payload["incubation_budget_formal_runtime_ready_selected_count"] == 1


def test_factory_status_dto_preserves_strict_incubation_blocker_summary() -> None:
    from strategy_factory.api.dto import FactoryStatusDTO

    dto = FactoryStatusDTO.from_dict(
        {
            "running": False,
            "runtime_enabled": True,
            "last_result": {
                "status": "success",
                "summary": {
                    "strict_incubation_ready_count": 0,
                    "raw_b_or_above_count": 3,
                    "incubation_budget_formal_runtime_ready_candidate_count": 4,
                    "incubation_budget_formal_runtime_ready_selected_count": 2,
                },
            },
            "strict_incubation_blocker_summary": {
                "status": "blocked",
                "top_blockers": [
                    {
                        "reason_code": "diagnostic_only_not_allowed_for_incubation",
                        "count": 3,
                    }
                ],
            },
        }
    )

    payload = dto.to_dict()

    assert payload["last_incubation_budget_formal_runtime_ready_candidate_count"] == 4
    assert payload["last_incubation_budget_formal_runtime_ready_selected_count"] == 2
    assert payload["strict_incubation_blocker_summary"]["status"] == "blocked"
    assert payload["strict_incubation_blocker_summary"]["top_blockers"][0]["reason_code"] == (
        "diagnostic_only_not_allowed_for_incubation"
    )
