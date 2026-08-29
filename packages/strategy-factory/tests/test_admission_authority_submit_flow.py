from __future__ import annotations

from typing import Any

import pytest

from _admission_helpers import (
    _clear_dev_v1_env,
    _formal_runtime,
    _ready_trade_prediction_fields,
    _resolve,
    _resolve_with_real_resolver,
    _runtime_ready_candidate,
)

@pytest.mark.asyncio
async def test_submit_one_keeps_wide_intake_in_observe_lane(monkeypatch) -> None:
    from strategy_factory.application._submitter_actions import runner as submitter_runner
    from strategy_factory.application.submitter import StrategySubmitter

    monkeypatch.setenv("STRATEGY_FACTORY_DIAGNOSTIC_OBSERVATION_ENABLED", "1")
    monkeypatch.setenv("STRATEGY_FACTORY_DIAGNOSTIC_OBSERVATION_HEALTH_GUARD_ENABLED", "0")
    monkeypatch.setenv("STRATEGY_FACTORY_GATE3_RECORD_ONLY_ENABLED", "1")
    monkeypatch.setenv("STRATEGY_FACTORY_MIN_VALIDATION_GRADE", "S")

    async def _gate3_audit_failure(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {
            "passed": False,
            "passed_strict": False,
            "validation_grade": "C",
            "research_candidate_ready": True,
            "live_candidate_ready": False,
            "strict_incubation_ready": False,
            "reason_codes": [
                "weak_bootstrap_ci_lower",
                "factory_policy_backtest_trade_count_0_4",
            ],
            "admission_block_reasons": ["weak_bootstrap_ci_lower"],
        }

    class _Gateway:
        async def run_validation_report(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
            return {}

        async def run_risk_report(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
            return {}

    class _Coordinator:
        def __init__(self) -> None:
            self.persisted: list[dict[str, Any]] = []
            self.handled: list[dict[str, Any]] = []
            self.reports: list[dict[str, Any]] = []

        async def persist_candidate(self, **kwargs: Any) -> bool:
            self.persisted.append(dict(kwargs))
            return True

        async def handle_new_candidate(self, **kwargs: Any) -> dict[str, Any]:
            self.handled.append(dict(kwargs))
            action = dict(kwargs.get("submission_action") or {})
            return {
                "submission_lane": kwargs.get("submission_lane"),
                "final_status": action.get("final_status") or "submitted",
                "submission_action": dict(action.get("submission_action") or {}),
                "submission_action_type": action.get("submission_action_type"),
                "submission_action_trigger": action.get("submission_action_trigger"),
                "submission_action_gaps": list(action.get("submission_action_gaps") or []),
                "submission_action_fallback_conditions": list(
                    action.get("submission_action_fallback_conditions") or []
                ),
                "submission_action_next_step": action.get("submission_action_next_step"),
                "submission_action_completed": bool(action.get("submission_action_completed")),
                "runtime_bootstrap_reason": action.get("runtime_bootstrap_reason"),
                "wide_intake_admitted": bool(action.get("wide_intake_admitted")),
                "formal_track_requested": bool(action.get("formal_track_requested")),
                "formal_track_eligible": bool(action.get("formal_track_eligible")),
                "formal_track_blockers": list(action.get("formal_track_blockers") or []),
            }

        async def save_quality_report(self, *_args: Any, **_kwargs: Any) -> None:
            import copy

            self.reports.append(
                {
                    "strategy_id": _args[1],
                    "quality_report": copy.deepcopy(dict(_args[2] or {})),
                    "kwargs": copy.deepcopy(_kwargs),
                }
            )
            return None

    class _DB:
        def __init__(self) -> None:
            self.evidence: list[dict[str, Any]] = []
            self.experiments: list[dict[str, Any]] = []

        async def save_factory_task_evidence(self, payload: dict[str, Any]) -> dict[str, Any]:
            row = {"id": f"ev-{len(self.evidence) + 1}", **payload}
            self.evidence.append(row)
            return row

        async def save_strategy_generation_experiment(self, payload: dict[str, Any]) -> dict[str, Any]:
            self.experiments.append(dict(payload))
            return dict(payload)

    monkeypatch.setattr(submitter_runner, "_local_run_submission_quality_gate", _gate3_audit_failure)

    submitter = StrategySubmitter(validation_gateway=_Gateway(), risk_gateway=_Gateway())
    coordinator = _Coordinator()
    submitter._submission_coordinator = coordinator
    candidate = _runtime_ready_candidate("submit-one-wide-intake", observe_first=True)
    candidate.update(
        {
            "name": "wide intake candidate",
            "dedup_result": {"duplicate": False, "refresh_existing": False},
            "backtest_metrics": {
                "trade_count": 12,
                "trades_count": 12,
                "max_drawdown": 0.12,
            },
            "backtest_outcome": {"passed": True, "reason_code": "passed"},
            "experiment_id": "exp-gate3-record-only",
        }
    )

    db = _DB()
    result = await submitter._submit_one(
        candidate,
        {"date": "2026-06-05", "factory_run_id": "run-gate3-audit"},
        db,
    )

    assert result["created_strategy_pool"] is False
    assert result["created_audit_only"] is True
    assert result["gate_3_recorded"] is True
    assert result["gate3_quality_recorded"] is False
    assert result["gate3_record_quality_qualified"] is False
    assert result["gate3_record_diagnostic_only"] is True
    assert result["gate3_record_only_min_grade"] == "S"
    assert result["gate3_record_grade"] == "C"
    assert result["record_only"] is True
    assert result["submitted"] is False
    assert result["admission_decision"] == "observe_only"
    assert result["summary"]["submission_lane"] == "gate3_record_only"
    assert result["summary"]["planned_submission_lane"] == "observe_incubation"
    assert result["summary"]["status"] == "gate3_recorded"
    assert result["summary"]["runtime_bootstrap_reason"] == "wide_intake_observe_gate3_not_required"
    assert result["summary"]["wide_intake_admitted"] is True
    assert result["summary"]["gate_3_recorded"] is True
    assert result["summary"]["gate3_quality_recorded"] is False
    assert result["summary"]["gate3_record_quality_qualified"] is False
    assert result["summary"]["gate3_record_diagnostic_only"] is True
    assert result["summary"]["submission_action_type"] == "gate3_record_only"
    assert result["summary"]["submission_action_next_step"] == "await_manual_record_review"
    assert result["summary"]["strategy_created"] is False
    assert result["summary"]["lifecycle_action_executed"] is False
    assert result["summary"]["automatic_downstream_action"] is False
    assert result["summary"]["quality_report_persisted"] is False
    assert result["summary"]["gate3_audit_evidence_persisted"] is True
    assert not result["summary"].get("diagnostic_observation")
    assert coordinator.persisted == []
    assert coordinator.handled == []
    assert len(coordinator.reports) == 1
    assert coordinator.reports[0]["kwargs"]["options"].record_only is True
    assert db.evidence[0]["evidence_type"] == "gate3_record_only_audit"
    assert db.evidence[0]["evidence_payload"]["strategy_created"] is False
    assert db.evidence[0]["evidence_payload"]["lifecycle_action_executed"] is False
    assert db.evidence[0]["evidence_payload"]["quality_report_persisted"] is False
    assert db.evidence[0]["evidence_payload"]["gate3_audit_evidence_persisted"] is True
    assert db.evidence[0]["evidence_payload"]["automatic_downstream_action"] is False
    assert db.evidence[0]["evidence_payload"]["gate3_quality_recorded"] is False
    assert db.evidence[0]["evidence_payload"]["gate3_record_diagnostic_only"] is True
    assert db.evidence[0]["weight"] == 0.0
    assert db.evidence[0]["evidence_payload"]["quality_report"]
    assert result["summary"]["gate3_audit_evidence_id"] == "ev-1"
    assert db.experiments[0]["strategy_id"] is None
    assert db.experiments[0]["generated_strategy_id"] is None
    assert db.experiments[0]["result"]["candidate_id"] == result["summary"]["strategy_id"]
    assert db.experiments[0]["result"]["strategy_id"] is None
    assert db.experiments[0]["result"]["generated_strategy_id"] is None
    assert db.experiments[0]["result"]["record_only"] is True


@pytest.mark.asyncio
async def test_gate3_record_only_counts_s_grade_as_quality_record_without_lifecycle_action(monkeypatch) -> None:
    from strategy_factory.application._submitter_actions import runner as submitter_runner
    from strategy_factory.application.submitter import StrategySubmitter

    monkeypatch.setenv("STRATEGY_FACTORY_GATE3_RECORD_ONLY_ENABLED", "1")
    monkeypatch.setenv("STRATEGY_FACTORY_MIN_VALIDATION_GRADE", "S")

    async def _gate3_s_pass(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {
            "passed": True,
            "passed_strict": True,
            "validation_grade": "S",
            "effective_validation_grade": "S",
            "validation_total_score": 82.0,
            "research_candidate_ready": True,
            "incubation_candidate_ready": True,
            "live_candidate_ready": False,
            "strict_incubation_ready": True,
            "incubation_pass_mode": "strict",
            "semantic_runtime_match": True,
            "runtime_family_data_source": "market_data_runtime",
            "proxy_runtime_used": False,
            "diagnostic_only": False,
            "execution_readiness_tier": "formal_runtime_ready",
            "trade_prediction_contract_status": "ready",
            "trade_prediction_contract_observation_gap": False,
        }

    class _Gateway:
        async def run_validation_report(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
            return {}

        async def run_risk_report(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
            return {}

    class _Coordinator:
        def __init__(self) -> None:
            self.persisted: list[dict[str, Any]] = []
            self.handled: list[dict[str, Any]] = []
            self.reports: list[dict[str, Any]] = []

        async def persist_candidate(self, **kwargs: Any) -> bool:
            self.persisted.append(dict(kwargs))
            return True

        async def handle_new_candidate(self, **kwargs: Any) -> dict[str, Any]:
            self.handled.append(dict(kwargs))
            return {"submission_lane": kwargs.get("submission_lane"), "final_status": "incubating"}

        async def save_quality_report(self, *_args: Any, **_kwargs: Any) -> None:
            self.reports.append({"args": _args, "kwargs": _kwargs})

    class _DB:
        def __init__(self) -> None:
            self.evidence: list[dict[str, Any]] = []
            self.experiments: list[dict[str, Any]] = []

        async def save_factory_task_evidence(self, payload: dict[str, Any]) -> dict[str, Any]:
            row = {"id": f"ev-{len(self.evidence) + 1}", **payload}
            self.evidence.append(row)
            return row

        async def save_strategy_generation_experiment(self, payload: dict[str, Any]) -> dict[str, Any]:
            self.experiments.append(dict(payload))
            return dict(payload)

    monkeypatch.setattr(submitter_runner, "_local_run_submission_quality_gate", _gate3_s_pass)

    submitter = StrategySubmitter(validation_gateway=_Gateway(), risk_gateway=_Gateway())
    coordinator = _Coordinator()
    submitter._submission_coordinator = coordinator
    candidate = _runtime_ready_candidate(
        "submit-one-s-record-only",
        name="s record only candidate",
        dedup_result={"duplicate": False, "refresh_existing": False},
        backtest_metrics={"trade_count": 24, "trades_count": 24, "max_drawdown": 0.05},
        backtest_outcome={"passed": True, "reason_code": "passed"},
        experiment_id="exp-gate3-s-record-only",
    )

    db = _DB()
    result = await submitter._submit_one(
        candidate,
        {"date": "2026-06-05", "factory_run_id": "run-gate3-s-audit"},
        db,
    )

    assert result["created_strategy_pool"] is False
    assert result["submitted"] is False
    assert result["gate_3_recorded"] is True
    assert result["gate3_quality_recorded"] is True
    assert result["gate3_record_quality_qualified"] is True
    assert result["gate3_record_diagnostic_only"] is False
    assert result["gate3_record_grade"] == "S"
    assert result["summary"]["gate3_quality_recorded"] is True
    assert result["summary"]["strategy_created"] is False
    assert result["summary"]["lifecycle_action_executed"] is False
    assert result["summary"]["gate3_audit_evidence_persisted"] is True
    assert coordinator.persisted == []
    assert coordinator.handled == []
    assert db.evidence[0]["weight"] == 1.0
    assert db.evidence[0]["evidence_payload"]["gate3_record_quality_qualified"] is True
    assert db.evidence[0]["evidence_payload"]["validation_grade"] == "S"
    assert db.evidence[0]["evidence_payload"]["gate3_audit_evidence_persisted"] is True
    assert db.evidence[0]["evidence_payload"]["automatic_downstream_action"] is False
    assert db.experiments[0]["strategy_id"] is None
    assert db.experiments[0]["generated_strategy_id"] is None
    assert db.experiments[0]["evaluation"]["gate3_record_quality_qualified"] is True
    assert db.experiments[0]["result"]["gate3_record_quality_qualified"] is True
    assert db.experiments[0]["result"]["automatic_downstream_action"] is False


@pytest.mark.asyncio
async def test_submit_one_can_opt_out_of_gate3_record_only(monkeypatch) -> None:
    from strategy_factory.application._submitter_actions import runner as submitter_runner
    from strategy_factory.application.submitter import StrategySubmitter

    monkeypatch.setenv("STRATEGY_FACTORY_GATE3_RECORD_ONLY_ENABLED", "0")

    async def _gate3_pass(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {
            "passed": True,
            "passed_strict": True,
            "validation_grade": "S",
            "research_candidate_ready": True,
            "incubation_candidate_ready": True,
            "live_candidate_ready": False,
            "strict_incubation_ready": True,
            "incubation_pass_mode": "strict",
            "semantic_runtime_match": True,
            "runtime_family_data_source": "market_data_runtime",
            "proxy_runtime_used": False,
            "diagnostic_only": False,
            "execution_readiness_tier": "formal_runtime_ready",
            "trade_prediction_contract_status": "ready",
            "trade_prediction_contract_observation_gap": False,
        }

    class _Gateway:
        async def run_validation_report(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
            return {}

        async def run_risk_report(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
            return {}

    class _Coordinator:
        def __init__(self) -> None:
            self.persisted: list[dict[str, Any]] = []
            self.handled: list[dict[str, Any]] = []

        async def persist_candidate(self, **kwargs: Any) -> bool:
            self.persisted.append(dict(kwargs))
            return True

        async def handle_new_candidate(self, **kwargs: Any) -> dict[str, Any]:
            self.handled.append(dict(kwargs))
            action = dict(kwargs.get("submission_action") or {})
            return {
                "submission_lane": kwargs.get("submission_lane"),
                "final_status": action.get("final_status") or "incubating",
                "submission_action": dict(action.get("submission_action") or {}),
                "submission_action_type": action.get("submission_action_type"),
                "submission_action_trigger": action.get("submission_action_trigger"),
                "submission_action_completed": True,
                "formal_track_requested": bool(action.get("formal_track_requested")),
                "formal_track_eligible": bool(action.get("formal_track_eligible")),
                "formal_track_blockers": list(action.get("formal_track_blockers") or []),
            }

        async def save_quality_report(self, *_args: Any, **_kwargs: Any) -> None:
            return None

    class _DB:
        pass

    monkeypatch.setattr(submitter_runner, "_local_run_submission_quality_gate", _gate3_pass)

    submitter = StrategySubmitter(validation_gateway=_Gateway(), risk_gateway=_Gateway())
    coordinator = _Coordinator()
    submitter._submission_coordinator = coordinator
    candidate = _runtime_ready_candidate("submit-one-opt-out")
    candidate.update(
        {
            "name": "opt out candidate",
            "dedup_result": {"duplicate": False, "refresh_existing": False},
            "backtest_metrics": {"trade_count": 12, "trades_count": 12, "max_drawdown": 0.08},
            "backtest_outcome": {"passed": True, "reason_code": "passed"},
        }
    )

    result = await submitter._submit_one(
        candidate,
        {"date": "2026-06-05", "factory_run_id": "run-gate3-opt-out"},
        _DB(),
    )

    assert result["record_only"] is False
    assert result["created_strategy_pool"] is True
    assert coordinator.persisted
    assert coordinator.handled
    lane = coordinator.handled[0]["submission_lane"]
    assert lane in {"formal_incubation", "observe_incubation"}
    if lane == "formal_incubation":
        assert result["submitted"] is False
        assert result["summary"]["status"] == "incubating"
    else:
        assert result["submitted"] is True


@pytest.mark.asyncio
async def test_submit_one_auto_corrects_strict_ready_observe_first_candidate_to_formal(monkeypatch) -> None:
    from strategy_factory.application._submitter_actions import runner as submitter_runner
    from strategy_factory.application.submitter import StrategySubmitter

    monkeypatch.delenv("STRATEGY_FACTORY_GATE3_RECORD_ONLY_ENABLED", raising=False)

    async def _gate3_strict_ready(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {
            "passed": True,
            "passed_strict": True,
            "provisional_pass": False,
            "validation_grade": "A",
            "research_candidate_ready": True,
            "live_candidate_ready": False,
            "strict_incubation_ready": True,
            "incubation_pass_mode": "strict",
            "semantic_runtime_match": True,
            "runtime_family_data_source": "market_data_runtime",
            "proxy_runtime_used": False,
            "diagnostic_only": False,
            "execution_readiness_tier": "formal_runtime_ready",
            "trade_prediction_contract_status": "ready",
            "trade_prediction_contract_observation_gap": False,
        }

    class _Gateway:
        async def run_validation_report(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
            return {}

        async def run_risk_report(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
            return {}

    class _Coordinator:
        def __init__(self) -> None:
            self.persisted: list[dict[str, Any]] = []
            self.handled: list[dict[str, Any]] = []
            self.reports: list[dict[str, Any]] = []

        async def persist_candidate(self, **kwargs: Any) -> bool:
            self.persisted.append(dict(kwargs))
            return True

        async def handle_new_candidate(self, **kwargs: Any) -> dict[str, Any]:
            self.handled.append(dict(kwargs))
            action = dict(kwargs.get("submission_action") or {})
            return {
                "submission_lane": kwargs.get("submission_lane"),
                "final_status": action.get("final_status") or "submitted",
                "submission_action": dict(action.get("submission_action") or {}),
                "submission_action_type": action.get("submission_action_type"),
                "submission_action_trigger": action.get("submission_action_trigger"),
                "submission_action_gaps": list(action.get("submission_action_gaps") or []),
                "submission_action_fallback_conditions": list(
                    action.get("submission_action_fallback_conditions") or []
                ),
                "submission_action_next_step": action.get("submission_action_next_step"),
                "submission_action_completed": bool(action.get("submission_action_completed")),
                "runtime_bootstrap_reason": action.get("runtime_bootstrap_reason"),
                "wide_intake_admitted": bool(action.get("wide_intake_admitted")),
                "formal_track_requested": bool(action.get("formal_track_requested")),
                "formal_track_auto_corrected": bool(action.get("formal_track_auto_corrected")),
                "formal_track_eligible": bool(action.get("formal_track_eligible")),
                "formal_track_blockers": list(action.get("formal_track_blockers") or []),
            }

        async def save_quality_report(self, *_args: Any, **_kwargs: Any) -> None:
            import copy

            self.reports.append(
                {
                    "strategy_id": _args[1],
                    "quality_report": copy.deepcopy(dict(_args[2] or {})),
                    "kwargs": copy.deepcopy(_kwargs),
                }
            )
            return None

    class _DB:
        def __init__(self) -> None:
            self.evidence: list[dict[str, Any]] = []
            self.experiments: list[dict[str, Any]] = []

        async def save_factory_task_evidence(self, payload: dict[str, Any]) -> dict[str, Any]:
            row = {"id": f"ev-{len(self.evidence) + 1}", **payload}
            self.evidence.append(row)
            return row

        async def save_strategy_generation_experiment(self, payload: dict[str, Any]) -> dict[str, Any]:
            self.experiments.append(dict(payload))
            return dict(payload)

    monkeypatch.setattr(submitter_runner, "_local_run_submission_quality_gate", _gate3_strict_ready)

    submitter = StrategySubmitter(validation_gateway=_Gateway(), risk_gateway=_Gateway())
    coordinator = _Coordinator()
    submitter._submission_coordinator = coordinator
    candidate = _runtime_ready_candidate("submit-one-observe-first-formal", observe_first=True)
    candidate.update(
        {
            "name": "observe first strict ready candidate",
            "dedup_result": {"duplicate": False, "refresh_existing": False},
            "backtest_metrics": {
                "trade_count": 12,
                "trades_count": 12,
                "max_drawdown": 0.08,
            },
            "backtest_outcome": {"passed": True, "reason_code": "passed"},
        }
    )

    db = _DB()
    result = await submitter._submit_one(
        candidate,
        {"date": "2026-06-10", "factory_run_id": "run-observe-first-formal"},
        db,
    )

    assert coordinator.persisted
    assert coordinator.handled
    assert coordinator.handled[0]["submission_lane"] == "formal_incubation"
    assert result["created_strategy_pool"] is True
    assert result["submitted"] is False
    assert result["summary"]["submission_lane"] == "formal_incubation"
    assert result["summary"]["status"] == "incubating"
    assert result["summary"]["formal_track_requested"] is True
    assert result["summary"]["formal_track_auto_corrected"] is True
    assert result["summary"]["formal_track_eligible"] is True
    assert result["summary"]["submission_action_trigger"] == "strict_incubation_ready_and_observe_first_formal_correction"
    assert len(coordinator.reports) == 1
    persisted_quality_report = dict(coordinator.reports[0]["quality_report"] or {})
    persisted_summary = dict(persisted_quality_report.get("summary") or {})
    assert persisted_summary["submission_lane"] == "formal_incubation"
    assert persisted_summary["incubation_budget_track"] == "observe_incubation"
    assert persisted_summary["formal_track_requested"] is True
    assert persisted_summary["formal_track_auto_corrected"] is True
    assert persisted_summary["observe_first_intake_requested"] is True
    assert persisted_summary["runtime_family_data_source"] == "market_data_runtime"
    assert persisted_summary["execution_readiness_tier"] == "formal_runtime_ready"
    assert persisted_summary["trade_prediction_contract_status"] == "ready"
