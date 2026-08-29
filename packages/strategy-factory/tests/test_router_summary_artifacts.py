from __future__ import annotations

from strategy_factory.application._cycle_success_summary import build_success_run_summary
from strategy_factory.application._combined_scan_report import build_combined_scan_report
from strategy_factory.application.factory_execution import build_run_artifacts
from strategy_factory.application.research_plane_contract import build_task_artifact


ROUTER_SUMMARY = {
    "router_enabled": True,
    "router_strict": True,
    "router_telemetry_enabled": True,
    "router_candidate_stock_count": 2,
    "router_applied_count": 2,
    "router_status_counts": {"applied": 2},
    "router_fallback_reason_counts": {},
    "router_family_counts": {"trend_following": 1, "quality": 1},
    "router_holding_bucket_counts": {"swing": 2},
    "profile_summary_present_count": 2,
    "profile_summary_missing_count": 0,
    "profile_summary_generated_count": 2,
    "selected_task_count": 2,
    "selected_router_applied_count": 2,
    "selected_profile_summary_missing_count": 0,
}


def test_success_summary_lifts_router_telemetry_from_task_scan_summary() -> None:
    summary = build_success_run_summary(
        trace_id="trace_router",
        snapshot={},
        candidates=[],
        passed=[],
        unique=[],
        eliminated=[],
        spawn_report={},
        submit_result={},
        quality_gate_report={},
        backtest_report={},
        autonomy_summary={},
        task_scan_summary=ROUTER_SUMMARY,
        task_source_counts={},
        bulk_stock_matrix_family_counts={},
        bulk_stock_matrix_allocation_pass_counts={},
        factor_research_summary={},
        factor_refresh_summary={},
        readiness_summary={},
        warmup_summary={},
        backtest_audit_summary={},
        submission_audit_summary={},
        vector_summary={},
        elapsed=1.0,
    )

    assert summary["router_enabled"] is True
    assert summary["router_strict"] is True
    assert summary["router_applied_count"] == 2
    assert summary["profile_summary_generated_count"] == 2
    assert summary["selected_router_applied_count"] == 2


def test_success_summary_keeps_selected_event_driven_tasks_visible() -> None:
    summary = build_success_run_summary(
        trace_id="trace_events",
        snapshot={},
        candidates=[],
        passed=[],
        unique=[],
        eliminated=[],
        spawn_report={},
        submit_result={},
        quality_gate_report={},
        backtest_report={},
        autonomy_summary={"event_task_count": 0},
        task_scan_summary={},
        task_source_counts={"event_driven": 3, "snapshot": 1},
        bulk_stock_matrix_family_counts={},
        bulk_stock_matrix_allocation_pass_counts={},
        factor_research_summary={},
        factor_refresh_summary={},
        readiness_summary={},
        warmup_summary={},
        backtest_audit_summary={},
        submission_audit_summary={},
        vector_summary={},
        elapsed=1.0,
    )

    assert summary["event_task_count"] == 3
    assert summary["task_source_counts"] == {"event_driven": 3, "snapshot": 1}
    assert summary["event_snapshot_mixed"] is True


def test_success_summary_lifts_submission_lane_counts() -> None:
    summary = build_success_run_summary(
        trace_id="trace_lanes",
        snapshot={},
        candidates=[],
        passed=[],
        unique=[],
        eliminated=[],
        spawn_report={},
        submit_result={
            "created_total": 4,
            "gate_3_input": 4,
            "gate_3_passed": 0,
            "gate_3_failed": 4,
            "incubation_budget_summary": {
                "formal_runtime_ready_candidate_count": 2,
                "formal_runtime_ready_selected_count": 1,
            },
            "strategies": [
                {"submission_lane": "observe_incubation"},
                {"submission_lane": "observe_incubation"},
                {"submission_lane": "diagnostic_observation"},
                {
                    "submission_lane": "live_ready_review",
                    "submission_action_type": "research_only",
                },
            ],
        },
        quality_gate_report={},
        backtest_report={},
        autonomy_summary={},
        task_scan_summary={},
        task_source_counts={},
        bulk_stock_matrix_family_counts={},
        bulk_stock_matrix_allocation_pass_counts={},
        factor_research_summary={},
        factor_refresh_summary={},
        readiness_summary={},
        warmup_summary={},
        backtest_audit_summary={},
        submission_audit_summary={},
        vector_summary={},
        elapsed=1.0,
    )

    assert summary["observe_incubation_count"] == 2
    assert summary["diagnostic_observation_count"] == 1
    assert summary["observe_admitted_count"] == 3
    assert summary["live_ready_review_count"] == 1
    assert summary["research_only_count"] == 1
    assert summary["submission_lane_counts"]["observe_incubation"] == 2
    assert summary["incubation_budget_formal_runtime_ready_candidate_count"] == 2
    assert summary["incubation_budget_formal_runtime_ready_selected_count"] == 1


def test_task_artifact_persists_router_telemetry() -> None:
    artifact = build_task_artifact(
        {
            "task_scan": {
                "summary": ROUTER_SUMMARY,
                "router_artifact": {
                    "contract_version": "strategy_factory.router_artifact.v1",
                    **ROUTER_SUMMARY,
                },
                "tasks": [{"task_source": "bulk_stock_matrix"}],
            }
        }
    )

    assert artifact["router_artifact_contract_version"] == "strategy_factory.router_artifact.v1"
    assert artifact["router_enabled"] is True
    assert artifact["router_strict"] is True
    assert artifact["router_telemetry"]["router_applied_count"] == 2
    assert artifact["router_telemetry"]["profile_summary_generated_count"] == 2
    assert artifact["router_status_counts"] == {"applied": 2}
    assert artifact["selected_router_applied_count"] == 2


def test_combined_scan_report_lifts_bulk_router_telemetry() -> None:
    report = build_combined_scan_report(
        scan_summary={},
        tasks=[{"opportunity_type": "stock_first"}],
        task_source_counts={"bulk_stock_matrix": 1},
        event_task_count=0,
        bulk_tasks=[{"task_source": "bulk_stock_matrix"}],
        bulk_report={
            "summary": {
                "enabled": True,
                "stock_count": 2,
                **ROUTER_SUMMARY,
            },
            "tasks": [{"task_source": "bulk_stock_matrix"}],
            "router_artifact": {
                "contract_version": "strategy_factory.router_artifact.v1",
                **ROUTER_SUMMARY,
            },
        },
        bulk_cursor={},
        task_budget_meta={},
        external_provider_control={},
        generator_mode_controls={},
        opportunity_scan={},
    )

    assert report["summary"]["router_enabled"] is True
    assert report["summary"]["router_strict"] is True
    assert report["summary"]["router_applied_count"] == 2
    assert report["summary"]["profile_summary_generated_count"] == 2
    assert report["router_artifact"]["contract_version"] == "strategy_factory.router_artifact.v1"


def test_run_task_artifact_compaction_keeps_router_telemetry_block() -> None:
    task_artifact = build_task_artifact(
        {
            "task_scan": {
                "summary": ROUTER_SUMMARY,
                "router_artifact": {
                    "contract_version": "strategy_factory.router_artifact.v1",
                    **ROUTER_SUMMARY,
                },
                "tasks": [{"task_source": "bulk_stock_matrix"}],
            }
        }
    )
    artifacts = build_run_artifacts({"research_plane": {"task_artifact": task_artifact}})
    compact_task = next(
        item["payload_json"]
        for item in artifacts
        if item["artifact_type"] == "task_artifact"
    )

    assert compact_task["router_telemetry"]["router_enabled"] is True
    assert compact_task["router_telemetry"]["router_status_counts"] == {"applied": 2}
    assert compact_task["router_telemetry"]["profile_summary_generated_count"] == 2
