from __future__ import annotations


def test_readiness_warning_only_stage_is_completed():
    from strategy_factory.application.cycle_runner import FactoryCycleRunner
    from strategy_factory.application.run_models import StageStatus

    status = FactoryCycleRunner._resolve_readiness_stage_status(
        {
            "can_proceed": True,
            "critical_blockers": [],
            "blockers": [],
            "warnings": [
                "snapshot_degraded",
                "snapshot_completion_low",
                "governed_candidate_pool_blocked_ratio_high",
            ],
            "snapshot_degraded": True,
            "factor_research_degraded": True,
        }
    )

    assert status is StageStatus.COMPLETED


def test_readiness_soft_blocker_stage_is_partial_when_still_allowed():
    from strategy_factory.application.cycle_runner import FactoryCycleRunner
    from strategy_factory.application.run_models import StageStatus

    status = FactoryCycleRunner._resolve_readiness_stage_status(
        {
            "can_proceed": True,
            "critical_blockers": [],
            "blockers": ["governed_candidate_pool_required"],
            "warnings": ["governed_candidate_pool_inactive"],
        }
    )

    assert status is StageStatus.PARTIAL


def test_soft_governed_pool_gap_stage_is_completed_when_still_allowed(monkeypatch):
    monkeypatch.setenv("STRATEGY_FACTORY_READINESS_HARD_BLOCK", "0")

    from strategy_factory.application.cycle_runner import FactoryCycleRunner
    from strategy_factory.application.run_models import StageStatus
    from strategy_factory.application.services.readiness_service import ReadinessService

    readiness = ReadinessService().evaluate(
        {
            "degraded": False,
            "completeness": {"completion_ratio": 1.0},
            "sources": {"event_driven": {"status": "success"}},
            "event_driven": {"tasks_ready_count": 3},
        },
        {
            "summary": {
                "factor_source_mode": "seed_fallback",
                "active_candidate_count": 0,
                "governed_source_candidate_count": 0,
            },
            "freshness_repair": {
                "auto_refresh_enabled": False,
                "refresh_attempted": False,
                "refresh_status": "disabled",
            },
        },
    )

    assert readiness["can_proceed"] is True
    assert readiness["blockers"] == []
    assert FactoryCycleRunner._resolve_readiness_stage_status(readiness) is StageStatus.COMPLETED


def test_readiness_cannot_proceed_stage_is_failed():
    from strategy_factory.application.cycle_runner import FactoryCycleRunner
    from strategy_factory.application.run_models import StageStatus

    status = FactoryCycleRunner._resolve_readiness_stage_status(
        {
            "can_proceed": False,
            "critical_blockers": ["snapshot_completion_too_low"],
            "blockers": ["snapshot_completion_too_low"],
        }
    )

    assert status is StageStatus.FAILED


def test_warning_only_readiness_does_not_make_success_run_partial():
    from strategy_factory.application.run_models import (
        FactoryRunStatus,
        build_stage_result,
        resolve_run_status,
    )

    stages = {
        "collect": build_stage_result(
            "collect",
            "trace-1",
            {
                "completion_ratio": 1.0,
                "missing_sources": [],
                "optional_unavailable_sources": ["north_fund"],
            },
            status="completed",
        ),
        "readiness": build_stage_result(
            "readiness",
            "trace-1",
            {
                "can_proceed": True,
                "warnings": ["governed_candidate_pool_blocked_ratio_high"],
                "warning_count": 1,
                "blockers": [],
                "critical_blockers": [],
            },
            status="completed",
        ),
        "spawn": build_stage_result("spawn", "trace-1", {}, status="completed"),
        "autonomy": build_stage_result("autonomy", "trace-1", {}, status="completed"),
        "quality_gate": build_stage_result("quality_gate", "trace-1", {}, status="completed"),
        "backtest": build_stage_result("backtest", "trace-1", {}, status="completed"),
        "deduplicate": build_stage_result("deduplicate", "trace-1", {}, status="completed"),
        "submit": build_stage_result("submit", "trace-1", {}, status="completed"),
        "elimination": build_stage_result("elimination", "trace-1", {}, status="completed"),
    }

    assert resolve_run_status("success", stages) is FactoryRunStatus.SUCCESS
