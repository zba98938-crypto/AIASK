from __future__ import annotations


def _candidate(
    strategy_id: str,
    *,
    strategy_type: str,
    sharpe: float,
    total_return: float,
    validation_score: float,
    formal_runtime_ready: bool = False,
) -> dict:
    params = {
        "candidate_validation_score": validation_score,
    }
    candidate = {
        "id": strategy_id,
        "strategy_type": strategy_type,
        "target_symbols": ["600000"],
        "backtest_metrics": {
            "sharpe_ratio": sharpe,
            "total_return": total_return,
            "max_drawdown": 0.04,
        },
        "params": params,
    }
    if formal_runtime_ready:
        params.update(
            {
                "execution_readiness_tier": "formal_runtime_ready",
                "trade_prediction_contract_status": "ready",
                "trade_prediction_contract_observation_gap": False,
                "semantic_runtime_match": True,
                "proxy_runtime_used": False,
                "diagnostic_only": False,
                "execution_semantic_gap": False,
            }
        )
    return candidate


def test_budgeter_prioritizes_formal_runtime_ready_candidate_for_formal_slot(monkeypatch) -> None:
    from strategy_factory.application import incubation_budgeter as budgeter_module
    from strategy_factory.application.incubation_budgeter import IncubationBudgeter

    monkeypatch.setattr(budgeter_module, "FACTORY_INCUBATION_FORMAL_SLOT_COUNT", 1)
    monkeypatch.setattr(budgeter_module, "FACTORY_INCUBATION_OBSERVE_SLOT_COUNT", 1)
    monkeypatch.setattr(budgeter_module, "FACTORY_INCUBATION_EXPLORATION_RATIO", 0.0)

    high_score_observe_only = _candidate(
        "observe-only-high-score",
        strategy_type="ma_cross",
        sharpe=2.8,
        total_return=0.45,
        validation_score=92.0,
        formal_runtime_ready=False,
    )
    lower_score_formal_ready = _candidate(
        "formal-ready-lower-score",
        strategy_type="multi_factor",
        sharpe=0.7,
        total_return=0.08,
        validation_score=45.0,
        formal_runtime_ready=True,
    )

    plan = IncubationBudgeter.plan(
        [high_score_observe_only, lower_score_formal_ready],
        {"date": "2026-06-17", "factor_research": {"summary": {}}},
    )

    plans = plan["plans"]
    formal_plan = plans[id(lower_score_formal_ready)]
    observe_plan = plans[id(high_score_observe_only)]

    assert formal_plan["track"] == "formal_incubation"
    assert observe_plan["track"] == "observe_incubation"
    assert plan["summary"]["formal_runtime_ready_candidate_count"] == 1
    assert plan["summary"]["formal_runtime_ready_selected_count"] == 1


def test_budgeter_does_not_mark_observe_diagnostic_candidate_as_formal_ready(monkeypatch) -> None:
    from strategy_factory.application import incubation_budgeter as budgeter_module
    from strategy_factory.application.incubation_budgeter import IncubationBudgeter

    monkeypatch.setattr(budgeter_module, "FACTORY_INCUBATION_FORMAL_SLOT_COUNT", 1)
    monkeypatch.setattr(budgeter_module, "FACTORY_INCUBATION_OBSERVE_SLOT_COUNT", 0)
    monkeypatch.setattr(budgeter_module, "FACTORY_INCUBATION_EXPLORATION_RATIO", 0.0)

    diagnostic_candidate = _candidate(
        "diagnostic-runtime",
        strategy_type="ma_cross",
        sharpe=1.5,
        total_return=0.2,
        validation_score=70.0,
        formal_runtime_ready=True,
    )
    diagnostic_candidate["params"]["diagnostic_only"] = True
    diagnostic_candidate["params"]["execution_readiness_tier"] = "observe_diagnostic_only"

    plan = IncubationBudgeter.plan(
        [diagnostic_candidate],
        {"date": "2026-06-17", "factor_research": {"summary": {}}},
    )

    assert plan["summary"]["formal_runtime_ready_candidate_count"] == 0
    assert plan["summary"]["formal_runtime_ready_selected_count"] == 0


def test_budgeter_defers_skill_controlled_candidates(monkeypatch) -> None:
    from strategy_factory.application import incubation_budgeter as budgeter_module
    from strategy_factory.application.incubation_budgeter import IncubationBudgeter

    monkeypatch.setattr(budgeter_module, "FACTORY_INCUBATION_FORMAL_SLOT_COUNT", 1)
    monkeypatch.setattr(budgeter_module, "FACTORY_INCUBATION_OBSERVE_SLOT_COUNT", 1)
    monkeypatch.setattr(budgeter_module, "FACTORY_INCUBATION_EXPLORATION_RATIO", 0.0)

    skill_frozen = _candidate(
        "skill-frozen-high-score",
        strategy_type="multi_factor",
        sharpe=2.8,
        total_return=0.45,
        validation_score=92.0,
    )
    skill_frozen["research_task"] = {
        "feedback_control_mode": "normal",
        "feedback_skill_control_mode": "freeze",
        "feedback_skill_suppressed": True,
        "feedback_skill_control_reasons": ["skill_paper_hit_ratio_collapse"],
    }
    normal_candidate = _candidate(
        "normal-lower-score",
        strategy_type="value_factor",
        sharpe=0.6,
        total_return=0.05,
        validation_score=45.0,
    )

    plan = IncubationBudgeter.plan(
        [skill_frozen, normal_candidate],
        {"date": "2026-06-19", "factor_research": {"summary": {}}},
    )

    assert plan["plans"][id(skill_frozen)]["track"] == "deferred_budget_queue"
    assert plan["plans"][id(skill_frozen)]["feedback_skill_control_mode"] == "freeze"
    assert plan["plans"][id(normal_candidate)]["track"] == "formal_incubation"
    assert plan["summary"]["feedback_skill_freeze_count"] == 1


def test_budgeter_defers_all_suppressed_or_frozen_candidates(monkeypatch) -> None:
    from strategy_factory.application import incubation_budgeter as budgeter_module
    from strategy_factory.application.incubation_budgeter import IncubationBudgeter

    monkeypatch.setattr(budgeter_module, "FACTORY_INCUBATION_FORMAL_SLOT_COUNT", 1)
    monkeypatch.setattr(budgeter_module, "FACTORY_INCUBATION_OBSERVE_SLOT_COUNT", 1)
    monkeypatch.setattr(budgeter_module, "FACTORY_INCUBATION_EXPLORATION_RATIO", 0.0)

    suppressed = _candidate(
        "suppressed",
        strategy_type="ma_cross",
        sharpe=1.4,
        total_return=0.16,
        validation_score=65.0,
    )
    suppressed["research_task"] = {
        "feedback_control_mode": "suppress",
        "feedback_suppressed": True,
    }
    skill_frozen = _candidate(
        "skill-frozen",
        strategy_type="multi_factor",
        sharpe=1.5,
        total_return=0.18,
        validation_score=68.0,
    )
    skill_frozen["research_task"] = {
        "feedback_control_mode": "normal",
        "feedback_skill_control_mode": "freeze",
        "feedback_skill_suppressed": True,
    }

    plan = IncubationBudgeter.plan(
        [suppressed, skill_frozen],
        {"date": "2026-06-19", "factor_research": {"summary": {}}},
    )

    assert plan["plans"][id(suppressed)]["track"] == "deferred_budget_queue"
    assert plan["plans"][id(skill_frozen)]["track"] == "deferred_budget_queue"
    assert plan["summary"]["track_counts"] == {
        "formal_incubation": 0,
        "observe_incubation": 0,
        "deferred_budget_queue": 2,
    }
