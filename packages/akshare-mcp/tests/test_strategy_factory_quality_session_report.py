from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "scripts" / "factories"))

from _quality_session_modes import apply_quality_mode_env, resolve_quality_session_modes  # noqa: E402
from _quality_session_render import _render_report, _sample_strategy_table  # noqa: E402
from _quality_session_report import _compact_run_detail, _extract_issue_flags  # noqa: E402
import run_strategy_factory_quality_session as quality_session_runner  # noqa: E402


def test_quality_session_flags_split_infra_contract_and_true_quality_failures() -> None:
    flags, notes = _extract_issue_flags(
        {
            "status": "partial",
            "summary": {
                "pipeline_fallback_counts": {"cooldown_skip": 1},
                "submission_lane_counts": {"observe_incubation": 1},
                "submitted": 1,
                "gate_3_passed": 1,
            },
        },
        [
            {
                "admission_block_reasons": [
                    "execution_readiness_tier:missing_executable_contract",
                    "profit_factor 1.2 < 1.8",
                ],
                "quality_report_execution_readiness_tier": "missing_executable_contract",
            }
        ],
    )

    assert "infra_degraded" in flags
    assert "formal_contract_blocked" in flags
    assert "true_quality_blocked" in flags
    assert notes


def test_quality_session_does_not_label_formal_contract_ready_sample_as_contract_blocked() -> None:
    flags, _notes = _extract_issue_flags(
        {
            "status": "success",
            "summary": {
                "submission_lane_counts": {"formal_incubation": 1},
                "submitted": 1,
                "gate_3_passed": 1,
            },
        },
        [
            {
                "admission_block_reasons": ["profit_factor 1.2 < 1.8"],
                "quality_report_execution_readiness_tier": "formal_runtime_ready",
            }
        ],
    )

    assert "formal_contract_blocked" not in flags
    assert "true_quality_blocked" in flags


def test_quality_session_treats_formal_incubation_as_concrete_output() -> None:
    flags, notes = _extract_issue_flags(
        {
            "status": "success",
            "candidates_spawned": 2,
            "submitted": 0,
            "summary": {
                "submission_lane_counts": {"formal_incubation": 2, "rejected": 1},
                "submitted": 0,
                "gate_3_passed": 2,
            },
            "submission_artifact": {
                "incubation_budget_summary": {
                    "track_counts": {
                        "formal_incubation": 2,
                        "observe_incubation": 0,
                        "deferred_budget_queue": 0,
                    },
                    "planned_track_counts": {
                        "formal_incubation": 0,
                        "observe_incubation": 0,
                        "deferred_budget_queue": 2,
                    },
                    "effective_track_counts": {
                        "formal_incubation": 2,
                        "observe_incubation": 0,
                        "deferred_budget_queue": 0,
                    },
                    "track_counts_reconciled": True,
                }
            },
        },
        [],
    )

    assert "no_submission_after_generation" not in flags
    assert "budget_summary_final_lane_mismatch" not in flags
    assert not any("no submissions" in note for note in notes)


def test_quality_session_compact_detail_splits_submission_channels() -> None:
    detail = _compact_run_detail(
        {
            "summary": {
                "submission_lane_counts": {
                    "formal_incubation": 2,
                    "observe_incubation": 3,
                    "diagnostic_observation": 4,
                    "live_ready_review": 1,
                },
            },
            "submission_artifact": {"strategy_status_counts": {}},
        }
    )

    assert detail["live_submitted"] == 1
    assert detail["formal_incubation_created"] == 2
    assert detail["observe_paper_created"] == 3
    assert detail["audit_only_created"] == 4


def test_quality_session_splits_readiness_evidence_debt_from_infra_failure() -> None:
    flags, notes = _extract_issue_flags(
        {
            "status": "partial_infra",
            "summary": {
                "pipeline_fallback_counts": {},
                "factory_readiness_can_proceed": True,
                "factory_readiness_blocker_count": 1,
                "factory_readiness_warning_count": 5,
            },
            "stages": {
                "readiness": {
                    "status": "partial",
                    "can_proceed": True,
                    "blocker_count": 1,
                    "warning_count": 5,
                    "blockers": ["cohort_promotion_ready_complete_failure"],
                }
            },
        },
        [],
    )

    assert "factory_runtime_degraded" in flags
    assert "readiness_evidence_debt" in flags
    assert "infra_degraded" not in flags
    assert any("evidence debt" in note for note in notes)


def test_quality_session_flags_factor_research_active_pool_fallback() -> None:
    flags, notes = _extract_issue_flags(
        {
            "status": "success",
            "summary": {"pipeline_fallback_counts": {}},
            "stages": {
                "factor_research": {
                    "status": "completed",
                    "factor_source_mode": "active_factor_pool_fallback",
                    "governed_candidate_pool_mode": "active_factor_pool_fallback",
                    "refresh_status": "disabled",
                }
            },
        },
        [],
    )

    assert "factor_research_active_pool_fallback" in flags
    assert "factor_research_refresh_disabled" in flags
    assert any("existing active factor pool fallback" in note for note in notes)


def test_quality_session_splits_bootstrap_pending_from_missing_audit_attention() -> None:
    flags, notes = _extract_issue_flags(
        {
            "status": "success",
            "summary": {"submission_lane_counts": {"formal_incubation": 1}},
        },
        [
            {
                "strategy_id": "formal-bootstrap",
                "audit_status": "needs_attention",
                "execution_audit_gate_status": "bootstrap_pending",
                "audit_signal_evidence_count": 1,
            }
        ],
    )

    assert "execution_audit_bootstrap_pending" in flags
    assert "execution_audit_needs_attention" not in flags
    assert any("bootstrap_pending" in note for note in notes)


def test_quality_session_table_prefers_execution_audit_gate_status() -> None:
    table = "\n".join(
        _sample_strategy_table(
            [
                {
                    "strategy_id": "formal-bootstrap",
                    "strategy_type": "momentum",
                    "validation_grade": "A",
                    "validation_total_score": 80,
                    "review_passed": True,
                    "signal_coverage_ratio": 0.0,
                    "audit_status": "needs_attention",
                    "execution_audit_gate_status": "bootstrap_pending",
                    "status_after_review": "incubating",
                }
            ]
        )
    )

    assert "bootstrap_pending/needs_attention" in table


def test_quality_session_compare_mode_resolves_observe_and_strict_paths(monkeypatch) -> None:
    monkeypatch.delenv("STRATEGY_QUALITY_SESSION_MODES", raising=False)
    modes = resolve_quality_session_modes("compare")

    assert [mode.mode_id for mode in modes] == ["observe_first", "strict_gated"]
    assert modes[0].execution_mode == "stock_first_observe_primary"
    assert modes[0].observe_first_enabled is True
    assert modes[0].wide_intake_observe_enabled is True
    assert modes[1].execution_mode == "legacy_primary"
    assert modes[1].observe_first_enabled is False
    assert modes[1].wide_intake_observe_enabled is False


def test_quality_session_strict_mode_applies_no_wide_intake_env() -> None:
    keys = [
        "STRATEGY_FACTORY_EXECUTION_MODE",
        "STRATEGY_FACTORY_OBSERVE_FIRST_ENABLED",
        "STRATEGY_FACTORY_WIDE_INTAKE_OBSERVE_ENABLED",
        "STRATEGY_FACTORY_MIN_VALIDATION_GRADE",
        "STRATEGY_FACTORY_GATE3_RECORD_ONLY_ENABLED",
        "INCUBATION_FACTORY_GATE3_RECORD_ONLY_INTAKE_ENABLED",
    ]
    previous = {key: os.environ.get(key) for key in keys}
    try:
        strict = resolve_quality_session_modes("strict-gated")[0]
        applied = apply_quality_mode_env(
            strict,
            runtime_controls={
                "min_validation_grade": "B",
                "gate3_record_only_enabled": False,
                "gate3_record_only_intake_enabled": False,
            },
        )

        assert applied["STRATEGY_FACTORY_EXECUTION_MODE"] == "legacy_primary"
        assert applied["STRATEGY_FACTORY_OBSERVE_FIRST_ENABLED"] == "0"
        assert applied["STRATEGY_FACTORY_WIDE_INTAKE_OBSERVE_ENABLED"] == "0"
        assert applied["STRATEGY_FACTORY_MIN_VALIDATION_GRADE"] == "B"
        assert os.environ["STRATEGY_FACTORY_WIDE_INTAKE_OBSERVE_ENABLED"] == "0"
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def test_quality_session_runtime_defaults_bound_llm_long_tail() -> None:
    keys = [
        "STRATEGY_PIPELINE_STAGE_TIMEOUT_SEC",
        "STRATEGY_LLM_TIMEOUT_SEC",
        "STRATEGY_LLM_STAGE_RETRY_COUNT",
        "STRATEGY_LLM_RECENT_TIMEOUT_MINIMAL_STREAK",
        "STRATEGY_LLM_RECENT_TIMEOUT_COOLDOWN_SEC",
        "STRATEGY_LLM_RECENT_CONNECTIVITY_MINIMAL_STREAK",
        "STRATEGY_LLM_RECENT_CONNECTIVITY_COOLDOWN_SEC",
        "STRATEGY_LLM_RECENT_OVERLOAD_MINIMAL_STREAK",
        "STRATEGY_LLM_RECENT_OVERLOAD_COOLDOWN_SEC",
        "FACTOR_LLM_TIMEOUT_SEC",
        "FACTOR_LLM_CONNECT_TIMEOUT_SEC",
        "FACTOR_LLM_WRITE_TIMEOUT_SEC",
        "FACTOR_LLM_POOL_TIMEOUT_SEC",
        "STRATEGY_FACTORY_FACTOR_AUTO_REFRESH",
        "STRATEGY_FACTORY_FACTOR_REFRESH_TIMEOUT_SEC",
        "STRATEGY_FACTORY_FACTOR_REFRESH_SELF_HEAL",
        "STRATEGY_QUALITY_FACTOR_MINING_ENGINES",
        "STRATEGY_QUALITY_FACTOR_MINING_CANDIDATE_COUNT",
        "STRATEGY_QUALITY_FACTOR_MINING_EVOLUTION_GENERATIONS",
        "FACTOR_MINING_STRICT_VALIDATION_CANDIDATE_LIMIT",
        "STRATEGY_QUALITY_FACTOR_MAINTENANCE_AFTER_MINING",
        "STRATEGY_QUALITY_FACTOR_MAINTENANCE_TIMEOUT_SEC",
        "STRATEGY_QUALITY_SIGNAL_TRACKER_TIMEOUT_SEC",
    ]
    previous = {key: os.environ.get(key) for key in keys}
    try:
        for key in keys:
            os.environ.pop(key, None)
        os.environ["STRATEGY_LLM_TIMEOUT_SEC"] = "60"
        os.environ["STRATEGY_PIPELINE_STAGE_TIMEOUT_SEC"] = "90"

        quality_session_runner._apply_quality_session_runtime_defaults()

        assert os.environ["STRATEGY_PIPELINE_STAGE_TIMEOUT_SEC"] == "25"
        assert os.environ["STRATEGY_LLM_TIMEOUT_SEC"] == "25"
        assert os.environ["STRATEGY_LLM_STAGE_RETRY_COUNT"] == "0"
        assert os.environ["STRATEGY_LLM_RECENT_TIMEOUT_MINIMAL_STREAK"] == "1"
        assert os.environ["STRATEGY_LLM_RECENT_TIMEOUT_COOLDOWN_SEC"] == "180"
        assert os.environ["STRATEGY_LLM_RECENT_CONNECTIVITY_MINIMAL_STREAK"] == "1"
        assert os.environ["STRATEGY_LLM_RECENT_CONNECTIVITY_COOLDOWN_SEC"] == "180"
        assert os.environ["STRATEGY_LLM_RECENT_OVERLOAD_MINIMAL_STREAK"] == "1"
        assert os.environ["STRATEGY_LLM_RECENT_OVERLOAD_COOLDOWN_SEC"] == "180"
        assert os.environ["FACTOR_LLM_TIMEOUT_SEC"] == "25"
        assert os.environ["FACTOR_LLM_CONNECT_TIMEOUT_SEC"] == "8"
        assert os.environ["FACTOR_LLM_WRITE_TIMEOUT_SEC"] == "10"
        assert os.environ["FACTOR_LLM_POOL_TIMEOUT_SEC"] == "5"
        assert os.environ["STRATEGY_FACTORY_FACTOR_AUTO_REFRESH"] == "1"
        assert os.environ["STRATEGY_FACTORY_FACTOR_REFRESH_TIMEOUT_SEC"] == "60"
        assert os.environ["STRATEGY_FACTORY_FACTOR_REFRESH_SELF_HEAL"] == "1"
        assert os.environ["STRATEGY_QUALITY_FACTOR_MINING_ENGINES"] == "rule_seed"
        assert os.environ["STRATEGY_QUALITY_FACTOR_MINING_CANDIDATE_COUNT"] == "4"
        assert os.environ["STRATEGY_QUALITY_FACTOR_MINING_EVOLUTION_GENERATIONS"] == "1"
        assert os.environ["FACTOR_MINING_STRICT_VALIDATION_CANDIDATE_LIMIT"] == "1"
        assert os.environ["STRATEGY_QUALITY_FACTOR_MAINTENANCE_AFTER_MINING"] == "1"
        assert os.environ["STRATEGY_QUALITY_FACTOR_MAINTENANCE_TIMEOUT_SEC"] == "60"
        assert os.environ["STRATEGY_QUALITY_SIGNAL_TRACKER_TIMEOUT_SEC"] == "90"
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def test_quality_session_collects_snapshot_after_signal_and_incubation(monkeypatch) -> None:
    calls: list[str] = []
    strict = resolve_quality_session_modes("strict-gated")[0]

    monkeypatch.setattr(
        quality_session_runner,
        "apply_quality_mode_env",
        lambda *args, **kwargs: {"mode": "strict-gated"},
    )

    async def fake_factor_mining(enabled: bool, *, round_no: int) -> None:
        calls.append("factor")
        return None

    async def fake_factory(**kwargs) -> dict:
        calls.append("factory")
        return {
            "started_at": "2026-06-18T00:00:00+08:00",
            "completed_at": "2026-06-18T00:00:01+08:00",
            "run_ids": ["run-fresh"],
            "result": {
                "success": True,
                "data": {
                    "status": "success",
                    "run_id": "run-fresh",
                    "elapsed_seconds": 1.0,
                },
            },
        }

    async def fake_signal(enabled: bool) -> dict:
        calls.append("signal")
        return {
            "started_at": "2026-06-18T00:00:01+08:00",
            "completed_at": "2026-06-18T00:00:02+08:00",
            "result": {"signals_generated": 1},
        }

    async def fake_incubation(enabled: bool) -> dict:
        calls.append("incubation")
        return {
            "started_at": "2026-06-18T00:00:02+08:00",
            "completed_at": "2026-06-18T00:00:03+08:00",
            "result": {
                "status": "completed",
                "intake": {},
                "verification": {},
                "pipeline": {},
                "execution_audit_acceptance": {
                    "status": "ok",
                    "saved_signal_evidence_count": 1,
                    "gate_status_counts": {"bootstrap_pending": 1},
                },
            },
        }

    async def fake_shelf_status() -> dict:
        calls.append("shelf")
        return {}

    async def fake_backlog() -> dict:
        calls.append("backlog")
        return {}

    async def fake_snapshot(run_id: str, strategy_sample_limit: int) -> dict:
        calls.append("snapshot")
        assert calls.index("signal") < calls.index("snapshot")
        assert calls.index("incubation") < calls.index("snapshot")
        return {
            "run_id": run_id,
            "detail": {},
            "strategy_ids": [],
            "sampled_strategies": [
                {
                    "strategy_id": "strategy-fresh",
                    "audit_status": "ok",
                    "execution_audit_gate_status": "bootstrap_pending",
                    "audit_signal_evidence_count": 1,
                }
            ],
            "representative_samples": [],
            "blocker_summary": {},
            "issue_flags": [],
            "issue_notes": [],
        }

    monkeypatch.setattr(quality_session_runner, "_run_factor_mining_once", fake_factor_mining)
    monkeypatch.setattr(quality_session_runner, "_run_strategy_factory_once", fake_factory)
    monkeypatch.setattr(quality_session_runner, "_run_signal_tracker_once", fake_signal)
    monkeypatch.setattr(quality_session_runner, "_run_incubation_once", fake_incubation)
    monkeypatch.setattr(quality_session_runner, "_collect_factor_shelf_status", fake_shelf_status)
    monkeypatch.setattr(quality_session_runner, "_collect_paper_observation_backlog", fake_backlog)
    monkeypatch.setattr(quality_session_runner, "_collect_run_snapshot", fake_snapshot)

    entry = asyncio.run(
        quality_session_runner._run_round(
            round_no=1,
            factory_mod=object(),
            codes=["601288"],
            mode_config=strict,
            runtime_controls={
                "min_validation_grade": "C",
                "gate3_record_only_enabled": False,
                "gate3_record_only_intake_enabled": False,
            },
            with_incubation=True,
            run_factor_mining=False,
            strategy_sample_limit=3,
        )
    )

    assert calls == ["factor", "factory", "signal", "incubation", "shelf", "snapshot", "backlog"]
    assert entry["quality_snapshot"]["sampled_strategies"][0]["audit_signal_evidence_count"] == 1
    assert entry["incubation_result"]["result"]["execution_audit_acceptance"]["saved_signal_evidence_count"] == 1


def test_quality_session_signal_tracker_preserves_phase_timeout_result(monkeypatch) -> None:
    import strategy_factory.runtime.signal_tracker as signal_tracker_runtime_module
    import akshare_mcp.runtime.strategy_factory_bootstrap as bootstrap_module

    class FakeTracker:
        async def run_once(self):
            return {
                "signals_generated": 999,
                "incubation_orders": 3,
                "incubation_orders_filled": 2,
                "forward_returns_computed": 7,
                "timeout": True,
                "phase_timeout_count": 1,
                "phase_timeouts": ["C"],
                "phase_results": {"C": {"status": "timeout"}},
                "runtime_universe": {"paper_runtime": 4},
                "errors": ["phase_C_timeout"],
            }

    monkeypatch.setattr(bootstrap_module, "configure_local_strategy_factory_runtime", lambda: None)
    monkeypatch.setattr(signal_tracker_runtime_module, "get_signal_tracker_runtime", lambda: FakeTracker())

    result = asyncio.run(quality_session_runner._run_signal_tracker_once(True))

    payload = result["result"]
    assert payload["signals_generated"] == 999
    assert payload["incubation_orders"] == 3
    assert payload["incubation_orders_filled"] == 2
    assert payload["forward_returns_computed"] == 7
    assert payload["timeout"] is True
    assert payload["phase_timeout_count"] == 1
    assert payload["phase_timeouts"] == ["C"]
    assert payload["phase_results"]["C"]["status"] == "timeout"
    assert payload["runtime_universe"]["paper_runtime"] == 4
    assert payload["errors"] == ["phase_C_timeout"]


def test_quality_session_round_flags_factor_mining_no_admissions() -> None:
    snapshot = {"issue_flags": [], "issue_notes": []}

    quality_session_runner._augment_quality_snapshot_with_factor_evidence(
        snapshot,
        {
            "result": {
                "success": True,
                "raw_candidate_count": 4,
                "evolved_count": 4,
                "admitted_count": 0,
            }
        },
        run_factor_mining=True,
    )

    assert "factor_mining_no_admissions" in snapshot["issue_flags"]
    assert any("admitted none" in note for note in snapshot["issue_notes"])


def test_quality_session_runtime_health_marks_phase_timeout_and_zero_evidence_unhealthy() -> None:
    snapshot = {"issue_flags": [], "issue_notes": []}

    quality_session_runner._augment_quality_snapshot_with_runtime_health(
        snapshot,
        factory_result={"data": {"status": "partial_infra"}},
        signal_tracker_run={
            "result": {
                "signals_generated": 2,
                "incubation_orders": 0,
                "timeout": True,
                "phase_timeout_count": 1,
                "phase_timeouts": ["C"],
            }
        },
        incubation_result={
            "verification": {"verified": 10},
            "pipeline": {"auto_promoted": 0},
            "execution_audit_acceptance": {
                "evaluated": 3,
                "saved_signal_evidence_count": 0,
                "hard_gate_passed_count": 0,
            },
            "settlement": {},
        },
    )

    assert snapshot["healthy"] is False
    assert "factory_result_not_healthy" in snapshot["issue_flags"]
    assert "signal_tracker_phase_timeout" in snapshot["issue_flags"]
    assert "paper_signal_not_converted_to_orders" in snapshot["issue_flags"]
    assert "signal_evidence_unavailable" in snapshot["issue_flags"]


def test_quality_session_runtime_health_treats_bootstrap_pending_as_sample_debt() -> None:
    snapshot = {"issue_flags": [], "issue_notes": []}

    quality_session_runner._augment_quality_snapshot_with_runtime_health(
        snapshot,
        factory_result={"data": {"status": "success"}},
        signal_tracker_run={"result": {"signals_generated": 0}},
        incubation_result={
            "verification": {"verified": 10},
            "pipeline": {"auto_promoted": 0},
            "execution_audit_acceptance": {
                "status": "pending_evidence",
                "evaluated": 57,
                "available_signal_evidence_count": 70,
                "saved_signal_evidence_count": 0,
                "hard_gate_passed_count": 0,
                "gate_status_counts": {"bootstrap_pending": 57},
                "sample_blockers": [
                    "execution_hard_gate_pending",
                    "trade_evidence_not_ready",
                ],
                "awaiting_paper_execution_count": 23,
                "real_paper_round_trip_count": 1,
                "bootstrap_round_trip_count": 4,
                "closed_round_trip_count": 5,
                "open_position_count": 12,
                "estimated_round_trip_sample_debt": 19,
            },
            "paper_execution_backlog": {
                "signal_only_backlog_count": 5438,
                "selected_count": 200,
                "orders_created": 120,
                "orders_filled": 100,
                "skip_reason_counts": {"duplicate_order": 7},
            },
            "native_execution_evidence_backfill": {
                "trades_without_signal_evidence_count": 45,
                "saved_signal_evidence_count": 20,
            },
            "settlement": {},
        },
    )

    assert snapshot["healthy"] is True
    assert snapshot["health_summary"]["healthy"] is True
    assert "incubation_zero_promotion_and_zero_hard_gate" not in snapshot["issue_flags"]
    assert "execution_audit_gate_missing" not in snapshot["issue_flags"]
    assert "incubation_pending_execution_sample_evidence" in snapshot["issue_flags"]
    assert "execution_audit_awaiting_paper_execution" in snapshot["issue_flags"]
    assert snapshot["health_summary"]["available_signal_evidence"] == 70
    assert snapshot["health_summary"]["signal_only_backlog"] == 5438
    assert snapshot["health_summary"]["real_paper_round_trips"] == 1
    assert snapshot["health_summary"]["bootstrap_round_trips"] == 4
    assert snapshot["health_summary"]["closed_round_trips"] == 5
    assert snapshot["health_summary"]["open_positions"] == 12
    assert snapshot["health_summary"]["estimated_round_trip_sample_debt"] == 19


def test_quality_session_round_flags_factor_pool_promoted_for_consumption() -> None:
    snapshot = {"issue_flags": [], "issue_notes": []}

    quality_session_runner._augment_quality_snapshot_with_factor_evidence(
        snapshot,
        {
            "result": {
                "success": True,
                "raw_candidate_count": 0,
                "evolved_count": 0,
                "admitted_count": 0,
                "active_promoted_count": 1,
                "maintenance": {"promoted_count": 2},
            }
        },
        run_factor_mining=True,
    )

    assert "factor_pool_promoted_for_research_consumption" in snapshot["issue_flags"]
    assert any("promoted factors" in note for note in snapshot["issue_notes"])


def _quality_entry(
    *,
    round_no: int,
    mode,
    run_id: str,
    submitted: int,
    gate_passed: int,
    gate_input: int,
    lanes: dict[str, int],
    issue_flags: list[str] | None = None,
) -> dict:
    return {
        "round": round_no,
        "quality_mode": mode.mode_id,
        "quality_mode_label": mode.label,
        "mode_config": mode.as_state_dict(),
        "factory_result": {
            "success": True,
            "data": {"status": "success", "run_id": run_id, "elapsed_seconds": 1.0},
        },
        "quality_snapshot": {
            "detail": {
                "run_id": run_id,
                "status": "success",
                "execution_mode": mode.execution_mode,
                "candidates_spawned": gate_input,
                "submitted": submitted,
                "summary": {
                    "gate_3_input": gate_input,
                    "gate_3_passed": gate_passed,
                    "gate_3_failed": max(gate_input - gate_passed, 0),
                    "submitted": submitted,
                    "submission_lane_counts": lanes,
                    "pipeline_fallback_counts": {},
                    "gate_3_failure_topn": [],
                },
                "dedup_artifact": {},
                "submission_artifact": {
                    "incubation_budget_summary": {"track_counts": {}},
                    "strategy_status_counts": {},
                },
            },
            "sampled_strategies": [],
            "representative_samples": [],
            "blocker_summary": {},
            "issue_flags": issue_flags or [],
            "issue_notes": [],
        },
    }


def test_quality_session_report_renders_mode_comparison_structure() -> None:
    observe, strict = resolve_quality_session_modes("compare")
    text = _render_report(
        {
            "updated_at": "2026-06-18T00:00:00+08:00",
            "session": {
                "session_id": "compare-test",
                "started_at": "2026-06-18T00:00:00+08:00",
                "hours": 1,
                "pause_sec": 0,
                "codes": ["601288"],
                "quality_session_mode": "compare",
                "quality_modes": [observe.as_state_dict(), strict.as_state_dict()],
                "execution_mode": "stock_first_observe_primary,legacy_primary",
                "runtime_controls": {"min_validation_grade": "C"},
                "python_executable": sys.executable,
                "sqlite_path": "test.sqlite3",
                "report_path": "test.md",
            },
            "entries": [
                _quality_entry(
                    round_no=1,
                    mode=observe,
                    run_id="run-observe",
                    submitted=2,
                    gate_passed=1,
                    gate_input=3,
                    lanes={"observe_incubation": 2},
                    issue_flags=["observe_only_submission"],
                ),
                _quality_entry(
                    round_no=1,
                    mode=strict,
                    run_id="run-strict",
                    submitted=1,
                    gate_passed=1,
                    gate_input=3,
                    lanes={"formal_incubation": 1},
                ),
            ],
        }
    )

    assert "## Mode comparison" in text
    assert "### Round matrix" in text
    assert "`observe-first`" in text
    assert "`strict-gated`" in text
    assert "| `strict-gated` | 1 | `legacy_primary` | false | false |" in text
    assert "`run-observe`" in text
    assert "`run-strict`" in text
