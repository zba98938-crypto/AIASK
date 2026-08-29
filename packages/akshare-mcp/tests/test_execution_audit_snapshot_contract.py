from __future__ import annotations

from akshare_mcp.services.strategy_lifecycle_shared.execution_audit_snapshot import (
    build_execution_audit_snapshot_payload,
    snapshot_verdict_payload,
    with_execution_audit_snapshot_metadata,
)


def test_execution_audit_snapshot_payload_persists_unified_verdict_and_trace_metadata():
    snapshot = build_execution_audit_snapshot_payload(
        strategy_id="strategy-1",
        quality_gate={
            "execution_audit_gate_status": "passed",
            "execution_audit_gate_reasons": [],
            "execution_hard_gate_passed": True,
        },
        audit_summary={"realized_trade_count": 24},
        verification={"status": "verified"},
        acceptance={"status": "ready"},
        snapshot={"date": "2026-04-20"},
        as_of="2026-04-20",
        factory_run_id="factory-run-1",
        correlation_id="corr-1",
        trace_id="trace-1",
        submission_lane="formal_incubation",
        parent_task_run_id="task-1",
        source_action="strategy_factory_submit",
    )

    assert snapshot["strategy_id"] == "strategy-1"
    assert snapshot["snapshot_id"].startswith("eas_")
    assert snapshot["as_of"] == "2026-04-20"
    assert snapshot["factory_run_id"] == "factory-run-1"
    assert snapshot["correlation_id"] == "corr-1"
    assert snapshot["trace_id"] == "trace-1"
    assert snapshot["submission_lane"] == "formal_incubation"
    assert snapshot["verdict"] == {
        "status": "passed",
        "reasons": [],
        "hard_gate_passed": True,
    }
    assert snapshot["audit_summary"]["execution_audit_gate_status"] == "passed"
    assert snapshot["audit_summary"]["audit_ready_for_hard_gate"] is True
    assert snapshot["acceptance"] == {"status": "ready"}


def test_snapshot_helpers_surface_single_source_of_truth_fields():
    snapshot = {
        "snapshot_id": "eas_contract",
        "as_of": "2026-04-21",
        "correlation_id": "corr-2",
        "factory_run_id": "factory-run-2",
        "verdict": {
            "status": "bootstrap_ready",
            "reasons": ["realized_trade_count<20"],
            "hard_gate_passed": False,
        },
    }

    assert snapshot_verdict_payload(snapshot) == {
        "status": "bootstrap_ready",
        "reasons": ["realized_trade_count<20"],
        "hard_gate_passed": False,
    }

    payload = with_execution_audit_snapshot_metadata(
        {"status": "needs_attention"},
        snapshot=snapshot,
    )

    assert payload["execution_audit_snapshot_id"] == "eas_contract"
    assert payload["execution_audit_gate_status"] == "bootstrap_ready"
    assert payload["execution_audit_gate_reasons"] == ["realized_trade_count<20"]
    assert payload["execution_hard_gate_passed"] is False
    assert payload["execution_audit_as_of"] == "2026-04-21"
    assert payload["correlation_id"] == "corr-2"
    assert payload["factory_run_id"] == "factory-run-2"
