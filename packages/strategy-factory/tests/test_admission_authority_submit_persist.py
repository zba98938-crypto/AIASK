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
async def test_submit_preserves_planned_formal_budget_for_observe_first_candidate(monkeypatch) -> None:
    from strategy_factory.application._submitter_actions import runner as submitter_runner
    from strategy_factory.application.submitter import StrategySubmitter

    captured_candidates: list[dict[str, Any]] = []

    def _fake_budget_plan(candidates: list[dict[str, Any]], _snapshot: dict[str, Any]) -> dict[str, Any]:
        marker = int(id(candidates[0]))
        return {
            "plans": {
                marker: {
                    "track": "formal_incubation",
                    "budget_tier": "standard",
                    "rank": 1,
                    "priority_score": 99.0,
                }
            },
            "summary": {
                "track_counts": {
                    "formal_incubation": 1,
                    "observe_incubation": 0,
                    "deferred_budget_queue": 0,
                }
            },
        }

    async def _submit_one(self, candidate: dict[str, Any], snapshot: dict[str, Any], db: Any, *, read_only: bool = False):
        captured_candidates.append(dict(candidate))
        return {
            "created_total": True,
            "created_strategy_pool": True,
            "submitted": True,
            "passed": True,
            "gate_3": {
                "passed": True,
                "provisional_pass": False,
            },
            "summary": {
                "strategy_id": candidate.get("id"),
                "submission_lane": "formal_incubation",
                "submission_action_type": "incubation",
                "admission_decision": "accept",
                "strict_incubation_ready": True,
            },
        }

    monkeypatch.setattr(
        submitter_runner.IncubationBudgeter,
        "plan",
        staticmethod(_fake_budget_plan),
    )
    monkeypatch.setattr(StrategySubmitter, "_submit_one", _submit_one)

    submitter = StrategySubmitter()
    candidate = _runtime_ready_candidate("submit-batch-formal-preserved", observe_first=True)

    result = await submitter.submit(
        [candidate],
        {"date": "2026-06-05", "factory_run_id": "run-batch-formal-preserved"},
        object(),
    )

    assert captured_candidates
    merged_budget = dict(captured_candidates[0].get("incubation_budget") or {})
    assert merged_budget["track"] == "formal_incubation"
    assert merged_budget["budget_tier"] == "standard"
    assert merged_budget["observe_first_intake"] is True
    assert result["formal_incubation_count"] == 1
    assert result["observe_incubation_count"] == 0
    assert result["strict_incubation_ready_count"] == 1


@pytest.mark.asyncio
async def test_submit_real_budgeter_prioritizes_formal_ready_observe_first_candidate(monkeypatch) -> None:
    from strategy_factory.application import incubation_budgeter as budgeter_module
    from strategy_factory.application.submitter import StrategySubmitter

    monkeypatch.setattr(budgeter_module, "FACTORY_INCUBATION_FORMAL_SLOT_COUNT", 1)
    monkeypatch.setattr(budgeter_module, "FACTORY_INCUBATION_OBSERVE_SLOT_COUNT", 1)
    monkeypatch.setattr(budgeter_module, "FACTORY_INCUBATION_EXPLORATION_RATIO", 0.0)

    captured_candidates: list[dict[str, Any]] = []

    async def _submit_one(self, candidate: dict[str, Any], snapshot: dict[str, Any], db: Any, *, read_only: bool = False):
        captured_candidates.append(dict(candidate))
        track = str(dict(candidate.get("incubation_budget") or {}).get("track") or "observe_incubation")
        return {
            "created_total": True,
            "created_strategy_pool": track == "formal_incubation",
            "submitted": track != "formal_incubation",
            "passed": True,
            "gate_3": {"passed": True, "provisional_pass": False},
            "summary": {
                "strategy_id": candidate.get("id"),
                "submission_lane": track,
                "submission_action_type": "incubation" if track == "formal_incubation" else "paper",
                "admission_decision": "accept" if track == "formal_incubation" else "observe_only",
                "strict_incubation_ready": track == "formal_incubation",
            },
        }

    monkeypatch.setattr(StrategySubmitter, "_submit_one", _submit_one)

    high_score_observe_only = _runtime_ready_candidate("submit-budget-high-score", observe_first=True)
    high_score_observe_only.update(
        {
            "strategy_type": "ma_cross",
            "backtest_metrics": {
                "sharpe_ratio": 2.8,
                "total_return": 0.45,
                "max_drawdown": 0.04,
            },
        }
    )
    high_score_observe_only["params"]["candidate_validation_score"] = 92.0

    lower_score_formal_ready = _runtime_ready_candidate("submit-budget-formal-ready", observe_first=True)
    lower_score_formal_ready.update(
        {
            "strategy_type": "multi_factor",
            "backtest_metrics": {
                "sharpe_ratio": 0.7,
                "total_return": 0.08,
                "max_drawdown": 0.04,
            },
        }
    )
    lower_score_formal_ready["params"].update(
        {
            "candidate_validation_score": 45.0,
            "execution_readiness_tier": "formal_runtime_ready",
            "trade_prediction_contract_status": "ready",
            "trade_prediction_contract_observation_gap": False,
            "semantic_runtime_match": True,
            "proxy_runtime_used": False,
            "diagnostic_only": False,
            "execution_semantic_gap": False,
        }
    )

    result = await StrategySubmitter().submit(
        [high_score_observe_only, lower_score_formal_ready],
        {"date": "2026-06-17", "factory_run_id": "run-real-budget-formal-ready"},
        object(),
    )

    captured_by_id = {item["id"]: item for item in captured_candidates}
    assert captured_by_id["submit-budget-formal-ready"]["incubation_budget"]["track"] == "formal_incubation"
    assert captured_by_id["submit-budget-high-score"]["incubation_budget"]["track"] == "observe_incubation"
    assert result["formal_incubation_count"] == 1
    assert result["observe_incubation_count"] == 1
    assert result["incubation_budget_summary"]["formal_runtime_ready_candidate_count"] == 1
    assert result["incubation_budget_summary"]["formal_runtime_ready_selected_count"] == 1


@pytest.mark.asyncio
async def test_submit_reconciles_budget_summary_with_final_formal_lane(monkeypatch) -> None:
    from strategy_factory.application._submitter_actions import runner as submitter_runner
    from strategy_factory.application.submitter import StrategySubmitter

    captured_candidates: list[dict[str, Any]] = []

    def _fake_budget_plan(candidates: list[dict[str, Any]], _snapshot: dict[str, Any]) -> dict[str, Any]:
        return {
            "plans": {
                int(id(candidates[0])): {
                    "track": "deferred_budget_queue",
                    "rank": 1,
                    "priority_score": 40.0,
                }
            },
            "summary": {
                "track_counts": {
                    "formal_incubation": 0,
                    "observe_incubation": 0,
                    "deferred_budget_queue": 1,
                }
            },
        }

    async def _submit_one(self, candidate: dict[str, Any], snapshot: dict[str, Any], db: Any, *, read_only: bool = False):
        captured_candidates.append(dict(candidate))
        return {
            "created_total": True,
            "created_strategy_pool": True,
            "submitted": False,
            "passed": True,
            "gate_3": {"passed": True, "provisional_pass": False},
            "summary": {
                "strategy_id": candidate.get("id"),
                "submission_lane": "formal_incubation",
                "submission_action_type": "incubation",
                "admission_decision": "accept",
                "strict_incubation_ready": True,
            },
        }

    monkeypatch.setattr(
        submitter_runner.IncubationBudgeter,
        "plan",
        staticmethod(_fake_budget_plan),
    )
    monkeypatch.setattr(StrategySubmitter, "_submit_one", _submit_one)

    candidate = _runtime_ready_candidate("submit-budget-reconcile", observe_first=False)
    result = await StrategySubmitter().submit(
        [candidate],
        {"date": "2026-06-18", "factory_run_id": "run-budget-reconcile"},
        object(),
    )

    summary = result["incubation_budget_summary"]
    assert captured_candidates[0]["incubation_budget"]["track"] == "deferred_budget_queue"
    assert summary["planned_track_counts"]["deferred_budget_queue"] == 1
    assert summary["final_lane_counts"]["formal_incubation"] == 1
    assert summary["effective_track_counts"]["formal_incubation"] == 1
    assert summary["track_counts"]["formal_incubation"] == 1
    assert summary["track_counts_reconciled"] is True
    assert summary["auto_promoted_formal_count"] == 1


@pytest.mark.asyncio
async def test_submit_preserves_deferred_budget_for_observe_first_candidate(monkeypatch) -> None:
    from strategy_factory.application._submitter_actions import runner as submitter_runner
    from strategy_factory.application.submitter import StrategySubmitter

    captured_candidates: list[dict[str, Any]] = []

    def _fake_budget_plan(candidates: list[dict[str, Any]], _snapshot: dict[str, Any]) -> dict[str, Any]:
        return {
            "plans": {
                int(id(candidates[0])): {
                    "track": "deferred_budget_queue",
                    "rank": 1,
                    "priority_score": 12.0,
                    "feedback_control_mode": "suppress",
                    "feedback_suppressed": True,
                }
            },
            "summary": {
                "track_counts": {
                    "formal_incubation": 0,
                    "observe_incubation": 0,
                    "deferred_budget_queue": 1,
                }
            },
        }

    async def _submit_one(self, candidate: dict[str, Any], snapshot: dict[str, Any], db: Any, *, read_only: bool = False):
        captured_candidates.append(dict(candidate))
        return {
            "created_total": True,
            "created_strategy_pool": False,
            "submitted": False,
            "passed": False,
            "gate_3": {"passed": False, "reason_codes": ["feedback_suppressed"]},
            "summary": {
                "strategy_id": candidate.get("id"),
                "submission_lane": "deferred_budget_queue",
                "submission_action_type": "deferred",
                "admission_decision": "defer",
            },
        }

    monkeypatch.setattr(
        submitter_runner.IncubationBudgeter,
        "plan",
        staticmethod(_fake_budget_plan),
    )
    monkeypatch.setattr(StrategySubmitter, "_submit_one", _submit_one)

    candidate = _runtime_ready_candidate("submit-budget-deferred-observe-first", observe_first=True)
    result = await StrategySubmitter().submit(
        [candidate],
        {"date": "2026-06-19", "factory_run_id": "run-deferred-observe-first"},
        object(),
    )

    assert captured_candidates[0]["incubation_budget"]["track"] == "deferred_budget_queue"
    assert captured_candidates[0]["incubation_budget"].get("observe_first_intake") is True
    assert result["incubation_budget_summary"]["planned_track_counts"]["deferred_budget_queue"] == 1
    assert result["incubation_budget_summary"]["track_counts"]["deferred_budget_queue"] == 1


@pytest.mark.asyncio
async def test_submit_planned_deferred_overrides_existing_observe_budget(monkeypatch) -> None:
    from strategy_factory.application._submitter_actions import runner as submitter_runner
    from strategy_factory.application.submitter import StrategySubmitter

    captured_candidates: list[dict[str, Any]] = []

    def _fake_budget_plan(candidates: list[dict[str, Any]], _snapshot: dict[str, Any]) -> dict[str, Any]:
        return {
            "plans": {
                int(id(candidates[0])): {
                    "track": "deferred_budget_queue",
                    "rank": 1,
                    "priority_score": 8.0,
                    "feedback_skill_control_mode": "freeze",
                    "feedback_skill_suppressed": True,
                }
            },
            "summary": {
                "track_counts": {
                    "formal_incubation": 0,
                    "observe_incubation": 0,
                    "deferred_budget_queue": 1,
                }
            },
        }

    async def _submit_one(self, candidate: dict[str, Any], snapshot: dict[str, Any], db: Any, *, read_only: bool = False):
        captured_candidates.append(dict(candidate))
        return {
            "created_total": True,
            "created_strategy_pool": False,
            "submitted": False,
            "passed": False,
            "gate_3": {"passed": False, "reason_codes": ["feedback_skill_suppressed"]},
            "summary": {
                "strategy_id": candidate.get("id"),
                "submission_lane": "deferred_budget_queue",
                "submission_action_type": "deferred",
                "admission_decision": "defer",
            },
        }

    monkeypatch.setattr(
        submitter_runner.IncubationBudgeter,
        "plan",
        staticmethod(_fake_budget_plan),
    )
    monkeypatch.setattr(StrategySubmitter, "_submit_one", _submit_one)

    candidate = _runtime_ready_candidate("submit-budget-existing-observe", observe_first=False)
    candidate["incubation_budget"] = {"track": "observe_incubation", "budget_tier": "micro"}
    result = await StrategySubmitter().submit(
        [candidate],
        {"date": "2026-06-19", "factory_run_id": "run-existing-observe-deferred"},
        object(),
    )

    merged_budget = dict(captured_candidates[0]["incubation_budget"])
    assert merged_budget["track"] == "deferred_budget_queue"
    assert merged_budget["budget_tier"] == "micro"
    assert result["incubation_budget_summary"]["planned_track_counts"]["deferred_budget_queue"] == 1
    assert result["incubation_budget_summary"]["track_counts"]["deferred_budget_queue"] == 1


@pytest.mark.asyncio
async def test_submit_one_auto_promotes_deferred_strict_ready_candidate_to_formal(monkeypatch) -> None:
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
            "admission_block_reasons": [],
            "hard_fail_reasons": [],
        }

    class _Gateway:
        async def run_validation_report(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
            return {}

        async def run_risk_report(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
            return {}

    class _DB:
        def __init__(self) -> None:
            self.strategies: list[dict[str, Any]] = []
            self.status_updates: list[tuple[str, str]] = []
            self.quality_reports: list[tuple[str, str, dict[str, Any]]] = []

        async def save_strategy(self, payload: dict[str, Any]) -> dict[str, Any]:
            self.strategies.append(dict(payload))
            return dict(payload)

        async def save_strategy_metrics(
            self,
            strategy_id: str,
            period: str,
            payload: dict[str, Any],
        ) -> dict[str, Any]:
            return {"strategy_id": strategy_id, "period": period, **dict(payload)}

        async def save_strategy_quality_report(
            self,
            strategy_id: str,
            report_type: str,
            quality_report: dict[str, Any],
        ) -> dict[str, Any]:
            self.quality_reports.append((strategy_id, report_type, dict(quality_report)))
            return {"strategy_id": strategy_id, "report_type": report_type}

    class _IncubationGateway:
        async def ensure_account(self, _db: Any, strategy: dict[str, Any], **_kwargs: Any) -> dict[str, Any]:
            return {
                "account": {
                    "id": f"inc-{strategy['id']}",
                    "status": "active",
                    "stage": "incubating",
                },
                "binding": {"account_id": f"inc-{strategy['id']}"},
            }

        async def run_pipeline(self, _db: Any, strategy: dict[str, Any], **_kwargs: Any) -> dict[str, Any]:
            return {
                "task_run_id": f"task-{strategy['id']}",
                "status": "completed",
                "snapshot": {
                    "pipeline_stage": "formal_incubation",
                    "pipeline_status": "active",
                    "readiness_score": 0.72,
                },
            }

    async def _status_update(db: _DB, strategy_id: str, status: str, **_kwargs: Any) -> dict[str, Any]:
        db.status_updates.append((strategy_id, status))
        return {"strategy_id": strategy_id, "status": status}

    async def _vector_profile(_db: Any, strategy: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": f"vec-{strategy['id']}",
            "backend": "test",
            "metadata": {"audit": {"backend_used": "test"}},
        }

    monkeypatch.setattr(submitter_runner, "_local_run_submission_quality_gate", _gate3_strict_ready)
    monkeypatch.setattr(submitter_runner, "_local_update_strategy_status", _status_update)
    monkeypatch.setattr(
        "strategy_factory.application.services.lifecycle_coordinator.build_strategy_vector_profile",
        _vector_profile,
    )

    submitter = StrategySubmitter(
        validation_gateway=_Gateway(),
        risk_gateway=_Gateway(),
        incubation_gateway=_IncubationGateway(),
    )
    candidate = {
        "id": "submit-one-deferred-strict-ready",
        "name": "deferred strict ready candidate",
        "strategy_type": "momentum",
        "target_symbols": ["600000"],
        "incubation_budget": {"track": "deferred_budget_queue"},
        "params": {
            "target_symbols": ["600000"],
            **_ready_trade_prediction_fields("submit-one-deferred-strict-ready"),
            "holding_horizon": {"horizon": "next_day"},
            "trade_plan": {"holding_horizon": "next_day", "entry_bias": "trend_follow_long"},
            "risk_rules": {"stop_loss_pct": 0.08, "max_holding_days": 5},
            "execution_assumptions": {"order_style": "paper"},
            "prediction_contract": {
                "claims": [
                    {
                        "claim_id": "claim-1",
                        "target_trading_date": "2026-06-08",
                        "horizon": "next_day",
                        "evidence_ids": ["ev-1"],
                    }
                ]
            },
            "evidence_chain": {
                "evidences": [{"evidence_id": "ev-1", "source": "market_data_runtime"}]
            },
        },
        "dedup_result": {"duplicate": False, "refresh_existing": False},
        "backtest_metrics": {"trade_count": 12, "trades_count": 12, "max_drawdown": 0.08},
        "backtest_outcome": {"passed": True, "reason_code": "passed"},
    }

    db = _DB()
    result = await submitter._submit_one(
        candidate,
        {"date": "2026-06-05", "factory_run_id": "run-deferred-strict-ready"},
        db,
    )

    stored_params = dict(db.strategies[-1]["params"] or {})
    assert result["summary"]["submission_lane"] == "formal_incubation"
    assert result["summary"]["final_status"] == "incubating"
    assert result["summary"]["formal_track_requested"] is True
    assert result["summary"]["formal_track_eligible"] is True
    assert result["summary"]["admission_decision"] == "accept"
    assert stored_params["incubation_budget"]["track"] == "formal_incubation"
    assert stored_params["incubation_budget"]["auto_promoted_from_track"] == "deferred_budget_queue"
    assert stored_params["formal_track_requested"] is True
    assert stored_params["incubation_budget_track"] == "formal_incubation"
    assert db.status_updates[-1][1] == "incubating"


@pytest.mark.asyncio
async def test_default_observe_handoff_creates_paper_account_and_quality_summary(monkeypatch) -> None:
    from strategy_factory.application._submitter_actions import runner as submitter_runner
    from strategy_factory.application.submitter import StrategySubmitter

    monkeypatch.delenv("STRATEGY_FACTORY_GATE3_RECORD_ONLY_ENABLED", raising=False)

    async def _gate3_audit_failure(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {
            "passed": False,
            "passed_strict": False,
            "provisional_pass": False,
            "validation_grade": "C",
            "research_candidate_ready": True,
            "live_candidate_ready": False,
            "strict_incubation_ready": False,
            "reason_codes": ["weak_bootstrap_ci_lower"],
            "admission_block_reasons": ["weak_bootstrap_ci_lower"],
        }

    class _Gateway:
        async def run_validation_report(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
            return {}

        async def run_risk_report(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
            return {}

    class _IncubationGateway:
        def __init__(self) -> None:
            self.accounts: list[dict[str, Any]] = []

        async def ensure_account(self, _db: Any, strategy: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
            account = {
                "account_id": f"paper-{strategy['id']}",
                "status": "active",
                "strategy_id": strategy["id"],
                "kwargs": dict(kwargs),
            }
            self.accounts.append(account)
            return account

        async def update_account_stage(
            self,
            _db: Any,
            *,
            account_id: str,
            stage: str,
            metadata: dict[str, Any] | None = None,
            **_kwargs: Any,
        ) -> dict[str, Any]:
            return {
                "account_id": account_id,
                "stage": stage,
                "status": "active",
                "metadata": dict(metadata or {}),
            }

    class _DB:
        def __init__(self) -> None:
            self.strategies: list[dict[str, Any]] = []
            self.status_updates: list[tuple[str, str]] = []
            self.quality_reports: list[tuple[str, str, dict[str, Any]]] = []

        async def save_strategy(self, payload: dict[str, Any]) -> dict[str, Any]:
            self.strategies.append(dict(payload))
            return dict(payload)

        async def save_strategy_metrics(
            self,
            strategy_id: str,
            period: str,
            payload: dict[str, Any],
        ) -> dict[str, Any]:
            return {"strategy_id": strategy_id, "period": period, **dict(payload)}

        async def save_strategy_quality_report(
            self,
            strategy_id: str,
            report_type: str,
            quality_report: dict[str, Any],
        ) -> dict[str, Any]:
            self.quality_reports.append((strategy_id, report_type, dict(quality_report)))
            return {"strategy_id": strategy_id, "report_type": report_type}

    async def _status_update(db: _DB, strategy_id: str, status: str, **_kwargs: Any) -> dict[str, Any]:
        db.status_updates.append((strategy_id, status))
        return {"strategy_id": strategy_id, "status": status}

    monkeypatch.setattr(submitter_runner, "_local_run_submission_quality_gate", _gate3_audit_failure)
    monkeypatch.setattr(submitter_runner, "_local_update_strategy_status", _status_update)

    incubation_gateway = _IncubationGateway()
    submitter = StrategySubmitter(
        validation_gateway=_Gateway(),
        risk_gateway=_Gateway(),
        incubation_gateway=incubation_gateway,
    )
    candidate = {
        "id": "submit-one-default-observe",
        "name": "default observe handoff candidate",
        "strategy_type": "volatility_breakout",
        "target_symbols": ["600000"],
        "observe_first_intake": True,
        "incubation_budget": {
            "track": "observe_incubation",
            "budget_tier": "micro",
            "observe_first_intake": True,
        },
        "params": {
            "target_symbols": ["600000"],
            "as_of_date": "2026-06-05",
            "observe_first_intake": True,
            "holding_horizon": {"horizon": "next_day"},
            "trade_plan": {"holding_horizon": "next_day"},
            "risk_rules": {"max_drawdown": 0.12},
            "execution_assumptions": {"order_style": "paper"},
            "prediction_contract": {
                "claims": [
                    {
                        "claim_id": "claim-1",
                        "target_trading_date": "2026-06-08",
                        "horizon": "next_day",
                        "evidence_ids": ["ev-1"],
                    }
                ]
            },
            "evidence_chain": {
                "evidences": [{"evidence_id": "ev-1", "source": "pure_backtest"}]
            },
        },
        "dedup_result": {"duplicate": False, "refresh_existing": False},
        "backtest_metrics": {"trade_count": 12, "trades_count": 12, "max_drawdown": 0.08},
        "backtest_outcome": {"passed": True, "reason_code": "passed"},
    }

    db = _DB()
    result = await submitter._submit_one(
        candidate,
        {"date": "2026-06-05", "factory_run_id": "run-default-observe"},
        db,
    )

    assert result["record_only"] is False
    assert result["created_strategy_pool"] is True
    assert result["gate_3_recorded"] is False
    assert result["admission_decision"] == "observe_only"
    assert result["summary"]["submission_lane"] == "observe_incubation"
    assert result["summary"]["status"] == "submitted"
    assert result["summary"]["final_status"] == "submitted"
    assert result["summary"]["paper_lane_ready"] is True
    assert result["summary"]["paper_account_id"].startswith("paper-factory_")
    assert result["summary"]["paper_account_status"] == "active"
    assert result["summary"]["incubation_factory_required"] is True
    assert result["summary"]["paper_observation_backlog_status"] == "queued"
    assert result["summary"].get("paper_observation_handoff_warning") is None
    assert result["summary"]["trade_prediction_contract_observation_gap"] is True
    assert result["summary"]["diagnostic_only"] is True
    assert result["summary"]["execution_readiness_tier"] == "observe_diagnostic_only"
    assert db.strategies and db.strategies[0]["status"] == "submitted"
    assert db.status_updates[-1][1] == "submitted"
    assert incubation_gateway.accounts
    report_summary = db.quality_reports[-1][2]["summary"]
    assert report_summary["submission_lane"] == "observe_incubation"
    assert report_summary["final_status"] == "submitted"
    assert report_summary["paper_lane_ready"] is True
    assert report_summary["paper_account_id"] == result["summary"]["paper_account_id"]
    assert report_summary["incubation_factory_required"] is True
    assert report_summary["diagnostic_only"] is True
    assert report_summary["execution_readiness_tier"] == "observe_diagnostic_only"


@pytest.mark.asyncio
async def test_submit_one_persists_gate_runtime_context_into_strategy_and_quality_summary(monkeypatch) -> None:
    from strategy_factory.application._submitter_actions import runner as submitter_runner
    from strategy_factory.application.submitter import StrategySubmitter

    monkeypatch.delenv("STRATEGY_FACTORY_GATE3_RECORD_ONLY_ENABLED", raising=False)

    async def _gate3_pass_with_runtime_blockers(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {
            "passed": True,
            "passed_strict": True,
            "provisional_pass": False,
            "validation_grade": "A",
            "research_candidate_ready": True,
            "live_candidate_ready": False,
            "strict_incubation_ready": False,
            "profile": "trade_rule_validation",
            "validation_focus": "candidate_target_only",
            "primary_validation_layer": "target",
            "semantic_runtime_match": False,
            "runtime_family_data_source": "price_proxy_runtime",
            "proxy_runtime_used": True,
            "diagnostic_only": True,
            "execution_readiness_tier": "observe_diagnostic_only",
            "admission_block_reasons": [
                "runtime_family_semantic_mismatch",
                "proxy_runtime_not_allowed_for_formal_incubation",
                "diagnostic_only_not_allowed_for_incubation",
                "execution_readiness_tier:observe_diagnostic_only",
            ],
            "hard_fail_reasons": [
                "runtime_family_semantic_mismatch",
                "proxy_runtime_not_allowed_for_formal_incubation",
            ],
        }

    class _Gateway:
        async def run_validation_report(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
            return {}

        async def run_risk_report(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
            return {}

    class _IncubationGateway:
        def __init__(self) -> None:
            self.accounts: list[dict[str, Any]] = []

        async def ensure_account(self, _db: Any, strategy: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
            account = {
                "account_id": f"paper-{strategy['id']}",
                "status": "active",
                "strategy_id": strategy["id"],
                "kwargs": dict(kwargs),
            }
            self.accounts.append(account)
            return account

        async def update_account_stage(
            self,
            _db: Any,
            *,
            account_id: str,
            stage: str,
            metadata: dict[str, Any] | None = None,
            **_kwargs: Any,
        ) -> dict[str, Any]:
            return {
                "account_id": account_id,
                "stage": stage,
                "status": "active",
                "metadata": dict(metadata or {}),
            }

    class _DB:
        def __init__(self) -> None:
            self.strategies: list[dict[str, Any]] = []
            self.status_updates: list[tuple[str, str]] = []
            self.quality_reports: list[tuple[str, str, dict[str, Any]]] = []

        async def save_strategy(self, payload: dict[str, Any]) -> dict[str, Any]:
            self.strategies.append(dict(payload))
            return dict(payload)

        async def save_strategy_metrics(
            self,
            strategy_id: str,
            period: str,
            payload: dict[str, Any],
        ) -> dict[str, Any]:
            return {"strategy_id": strategy_id, "period": period, **dict(payload)}

        async def save_strategy_quality_report(
            self,
            strategy_id: str,
            report_type: str,
            quality_report: dict[str, Any],
        ) -> dict[str, Any]:
            self.quality_reports.append((strategy_id, report_type, dict(quality_report)))
            return {"strategy_id": strategy_id, "report_type": report_type}

    async def _status_update(db: _DB, strategy_id: str, status: str, **_kwargs: Any) -> dict[str, Any]:
        db.status_updates.append((strategy_id, status))
        return {"strategy_id": strategy_id, "status": status}

    monkeypatch.setattr(submitter_runner, "_local_run_submission_quality_gate", _gate3_pass_with_runtime_blockers)
    monkeypatch.setattr(submitter_runner, "_local_update_strategy_status", _status_update)

    incubation_gateway = _IncubationGateway()
    submitter = StrategySubmitter(
        validation_gateway=_Gateway(),
        risk_gateway=_Gateway(),
        incubation_gateway=incubation_gateway,
    )
    candidate = {
        "id": "submit-one-runtime-context",
        "name": "runtime context candidate",
        "strategy_type": "quality_factor",
        "target_symbols": ["600000"],
        "incubation_budget": {
            "track": "formal_incubation",
            "budget_tier": "standard",
        },
        "params": {
            "target_symbols": ["600000"],
            **_ready_trade_prediction_fields("submit-one-runtime-context"),
            "holding_horizon": {"min_days": 30, "max_days": 84, "cooldown_window_days": 7},
            "trade_plan": {
                "entry_bias": "cross_sectional_rank",
                "exit_bias": "rank_decay_or_periodic_rebalance",
            },
            "risk_rules": {
                "stop_loss_pct": 0.08,
                "take_profit_pct": 0.18,
                "max_holding_days": 60,
                "cooldown_days": 7,
            },
            "execution_assumptions": {"order_style": "paper"},
            "prediction_contract": {
                "claims": [
                    {
                        "claim_id": "claim-1",
                        "target_trading_date": "2026-06-08",
                        "horizon": "next_day",
                        "evidence_ids": ["ev-1"],
                    }
                ]
            },
            "evidence_chain": {
                "evidences": [{"evidence_id": "ev-1", "source": "fundamental_runtime"}]
            },
        },
        "dedup_result": {"duplicate": False, "refresh_existing": False},
        "backtest_metrics": {"trade_count": 12, "trades_count": 12, "max_drawdown": 0.08},
        "backtest_outcome": {"passed": True, "reason_code": "passed"},
    }

    db = _DB()
    result = await submitter._submit_one(
        candidate,
        {"date": "2026-06-05", "factory_run_id": "run-runtime-context"},
        db,
    )

    stored_params = dict(db.strategies[-1]["params"] or {})
    assert stored_params["submission_lane"] == result["summary"]["submission_lane"]
    assert stored_params["planned_submission_lane"] == result["summary"]["planned_submission_lane"]
    assert stored_params["final_status"] == result["summary"]["final_status"]
    assert stored_params["planned_final_status"] == result["summary"]["planned_final_status"]
    assert stored_params["formal_track_requested"] is True
    assert stored_params["formal_track_eligible"] is False
    assert stored_params["incubation_budget_track"] == "formal_incubation"
    assert stored_params["runtime_family_data_source"] == "price_proxy_runtime"
    assert stored_params["proxy_runtime_used"] is True
    assert stored_params["diagnostic_only"] is True
    assert stored_params["execution_readiness_tier"] == "observe_diagnostic_only"
    assert result["summary"]["runtime_family_data_source"] == "price_proxy_runtime"
    assert result["summary"]["proxy_runtime_used"] is True
    assert result["summary"]["diagnostic_only"] is True
    assert result["summary"]["execution_readiness_tier"] == "observe_diagnostic_only"
    assert result["summary"]["runtime_bootstrap_reason"] == "proxy_runtime_observe_only"
    assert result["summary"]["formal_track_requested"] is True
    assert result["summary"]["formal_track_eligible"] is False
    assert "proxy_runtime_not_allowed_for_formal_incubation" in result["summary"]["formal_track_blockers"]
    report_summary = db.quality_reports[-1][2]["summary"]
    assert report_summary["runtime_family_data_source"] == "price_proxy_runtime"
    assert report_summary["proxy_runtime_used"] is True
    assert report_summary["diagnostic_only"] is True
    assert report_summary["execution_readiness_tier"] == "observe_diagnostic_only"
    assert report_summary["trade_prediction_contract_status"] == "ready"
    assert report_summary["runtime_bootstrap_reason"] == "proxy_runtime_observe_only"
    assert report_summary["formal_track_requested"] is True
    assert report_summary["incubation_budget_track"] == "formal_incubation"
    assert "proxy_runtime_not_allowed_for_formal_incubation" in report_summary["formal_track_blockers"]


@pytest.mark.asyncio
async def test_submit_one_persists_runtime_structural_contract_fields(monkeypatch) -> None:
    """P0-a: candidate 携带的 runtime 语义/结构字段必须落到 strategies.params,
    否则 reviewer 重算时读空退化成 missing_executable_contract / price_proxy_runtime。"""
    from strategy_factory.application._submitter_actions import runner as submitter_runner
    from strategy_factory.application.submitter import StrategySubmitter

    monkeypatch.delenv("STRATEGY_FACTORY_GATE3_RECORD_ONLY_ENABLED", raising=False)

    async def _gate3_pass(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {
            "passed": True,
            "passed_strict": True,
            "provisional_pass": False,
            "validation_grade": "A",
            "research_candidate_ready": True,
            "live_candidate_ready": False,
            "strict_incubation_ready": False,
            "profile": "trade_rule_validation",
            "execution_semantic_mode": "compiled_dsl",
            "dsl_compiled": True,
            "semantic_runtime_match": True,
            "runtime_family_data_source": "market_data_runtime",
        }

    class _Gateway:
        async def run_validation_report(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
            return {}

        async def run_risk_report(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
            return {}

    class _IncubationGateway:
        def __init__(self) -> None:
            self.accounts: list[dict[str, Any]] = []

        async def ensure_account(self, _db: Any, strategy: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
            account = {
                "account_id": f"paper-{strategy['id']}",
                "status": "active",
                "strategy_id": strategy["id"],
                "kwargs": dict(kwargs),
            }
            self.accounts.append(account)
            return account

        async def update_account_stage(
            self,
            _db: Any,
            *,
            account_id: str,
            stage: str,
            metadata: dict[str, Any] | None = None,
            **_kwargs: Any,
        ) -> dict[str, Any]:
            return {"account_id": account_id, "stage": stage, "status": "active"}

    class _DB:
        def __init__(self) -> None:
            self.strategies: list[dict[str, Any]] = []

        async def save_strategy(self, payload: dict[str, Any]) -> dict[str, Any]:
            self.strategies.append(dict(payload))
            return dict(payload)

        async def save_strategy_metrics(self, strategy_id, period, payload) -> dict[str, Any]:
            return {"strategy_id": strategy_id, "period": period, **dict(payload)}

        async def save_strategy_quality_report(self, strategy_id, report_type, quality_report) -> dict[str, Any]:
            return {"strategy_id": strategy_id, "report_type": report_type}

    async def _status_update(db: _DB, strategy_id: str, status: str, **_kwargs: Any) -> dict[str, Any]:
        return {"strategy_id": strategy_id, "status": status}

    monkeypatch.setattr(submitter_runner, "_local_run_submission_quality_gate", _gate3_pass)
    monkeypatch.setattr(submitter_runner, "_local_update_strategy_status", _status_update)

    submitter = StrategySubmitter(
        validation_gateway=_Gateway(),
        risk_gateway=_Gateway(),
        incubation_gateway=_IncubationGateway(),
    )
    instrument_profile = {"measurement_source": "measured_runtime", "measured_profile_complete": True}
    trade_plan_to_dsl_map = {"entry_1": "dsl_node_1", "exit_1": "dsl_node_2"}
    execution_semantic_contract = {"execution_semantic_mode": "compiled_dsl", "dsl_compiled": True}
    fundamental_runtime_contract = {"runtime_family_data_source": "fundamental_runtime"}
    candidate = {
        "id": "submit-one-structural-fields",
        "name": "structural fields candidate",
        "strategy_type": "ma_cross",
        "target_symbols": ["600000"],
        "incubation_budget": {"track": "observe_incubation", "budget_tier": "micro"},
        "instrument_profile": instrument_profile,
        "trade_plan_to_dsl_map": trade_plan_to_dsl_map,
        "execution_semantic_contract": execution_semantic_contract,
        "fundamental_runtime_contract": fundamental_runtime_contract,
        "params": {
            "target_symbols": ["600000"],
            **_ready_trade_prediction_fields("submit-one-structural-fields"),
            "trade_plan": {"entry_bias": "ma_cross_up", "exit_bias": "ma_cross_down"},
            "risk_rules": {"stop_loss_pct": 0.06, "take_profit_pct": 0.15},
        },
        "dedup_result": {"duplicate": False, "refresh_existing": False},
        "backtest_metrics": {"trade_count": 12, "trades_count": 12, "max_drawdown": 0.08},
        "backtest_outcome": {"passed": True, "reason_code": "passed"},
    }

    db = _DB()
    await submitter._submit_one(
        candidate,
        {"date": "2026-06-05", "factory_run_id": "run-structural-fields"},
        db,
    )

    stored_params = dict(db.strategies[-1]["params"] or {})
    # gate 注入 candidate 的标量 runtime 字段必须落盘
    assert stored_params["execution_semantic_mode"] == "compiled_dsl"
    assert stored_params["dsl_compiled"] is True
    # 结构字段必须从 candidate 顶层落到 params,供 reviewer 重算
    assert stored_params["instrument_profile"] == instrument_profile
    assert stored_params["trade_plan_to_dsl_map"] == trade_plan_to_dsl_map
    assert stored_params["execution_semantic_contract"] == execution_semantic_contract
    assert stored_params["fundamental_runtime_contract"] == fundamental_runtime_contract
