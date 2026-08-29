from __future__ import annotations

import pytest

from strategy_factory.application.submitter import StrategySubmitter
from strategy_factory.application._submitter_actions.runner import (
    _diagnostic_observation_fingerprint,
    _diagnostic_observation_submission_action,
    _is_diagnostic_observation_candidate,
)


def _candidate(**overrides):
    payload = {
        "strategy_type": "momentum",
        "dedup_result": {
            "duplicate": False,
            "refresh_existing": False,
        },
        "backtest_metrics": {
            "trade_count": 4,
            "max_drawdown": 0.12,
        },
        "backtest_outcome": {
            "passed": True,
            "reason_code": "passed",
        },
    }
    payload.update(overrides)
    return payload


def _allowed(gate, candidate=None, metrics=None, *, refresh_existing=False, read_only=False):
    return _is_diagnostic_observation_candidate(
        gate,
        candidate or _candidate(),
        metrics or {},
        refresh_existing=refresh_existing,
        read_only=read_only,
    )


def test_low_win_rate_no_longer_enters_diagnostic_observation_by_default():
    allowed, reason = _allowed(
        {"passed": False, "reason_codes": ["win_rate_0_312_0_400"]},
    )

    assert allowed is False
    assert reason is None


def test_win_rate_near_threshold_can_enter_diagnostic_observation():
    allowed, reason = _allowed(
        {"passed": False, "reason_codes": ["win_rate_0_372_0_400"]},
    )

    assert allowed is True
    assert reason == "win_rate_0_372_0_400"


@pytest.mark.parametrize(
    "reason",
    [
        "weak_wf_ic_ir",
        "weak_pkf_ic",
        "weak_bootstrap_ci_lower",
        "period_robustness_below_threshold",
        "trade_count_3_10",
    ],
)
def test_allowed_diagnostic_reasons_can_enter(reason):
    allowed, resolved_reason = _allowed(
        {"passed": False, "reason_codes": [reason]},
    )

    assert allowed is True
    assert resolved_reason == reason


@pytest.mark.parametrize(
    "gate,candidate,kwargs",
    [
        ({"passed": True, "reason_codes": ["weak_wf_ic_ir"]}, _candidate(), {}),
        ({"passed": False, "gate_a_decision": "reject", "reason_codes": ["weak_wf_ic_ir"]}, _candidate(), {}),
        ({"passed": False, "gate_a_decision": "revise", "reason_codes": ["weak_wf_ic_ir"]}, _candidate(), {}),
        ({"passed": False, "reason_codes": ["runtime_contract_missing"]}, _candidate(), {}),
        ({"passed": False, "reason_codes": ["data_missing"]}, _candidate(), {}),
        ({"passed": False, "reason_codes": ["multiple_testing_high_risk"]}, _candidate(), {}),
        ({"passed": False, "reason_codes": ["weak_wf_ic_ir"]}, _candidate(backtest_metrics={"trade_count": 1, "max_drawdown": 0.12}), {}),
        ({"passed": False, "reason_codes": ["trade_count_3_10"]}, _candidate(backtest_metrics={"trade_count": 3, "max_drawdown": 0.12}), {}),
        ({"passed": False, "reason_codes": ["weak_wf_ic_ir"]}, _candidate(backtest_metrics={"trade_count": 3, "max_drawdown": 0.50}), {}),
        ({"passed": False, "reason_codes": ["weak_wf_ic_ir"]}, _candidate(dedup_result={"duplicate": True}), {}),
        ({"passed": False, "reason_codes": ["weak_wf_ic_ir"]}, _candidate(dedup_result={}), {}),
        ({"passed": False, "reason_codes": ["weak_wf_ic_ir"]}, _candidate(backtest_outcome={}), {}),
        ({"passed": False, "reason_codes": ["weak_wf_ic_ir"]}, _candidate(), {"refresh_existing": True}),
        ({"passed": False, "reason_codes": ["weak_wf_ic_ir"]}, _candidate(), {"read_only": True}),
    ],
)
def test_hard_exclusions_do_not_enter_diagnostic_observation(gate, candidate, kwargs):
    allowed, reason = _allowed(gate, candidate, **kwargs)

    assert allowed is False
    assert reason is None


def test_diagnostic_submission_action_marks_isolated_lane(monkeypatch):
    monkeypatch.setenv("STRATEGY_FACTORY_DIAGNOSTIC_OBSERVATION_TTL_DAYS", "9")

    action = _diagnostic_observation_submission_action(
        {
            "submission_action": {
                "type": "reject",
                "final_status": "rejected",
            },
            "submission_lane": "rejected",
            "final_status": "rejected",
        },
        reason="weak_wf_ic_ir",
    )

    assert action["submission_lane"] == "diagnostic_observation"
    assert action["final_status"] == "diagnostic"
    assert action["admission_decision"] == "diagnostic"
    assert action["admission_layer"] == "diagnostic"
    assert action["diagnostic_observation"] is True
    assert action["diagnostic_reason"] == "weak_wf_ic_ir"
    assert action["diagnostic_ttl_days"] == 9
    assert action["submission_action"]["submission_lane"] == "diagnostic_observation"
    assert action["submission_action"]["final_status"] == "diagnostic"


def test_diagnostic_submission_action_can_rollback_status(monkeypatch):
    monkeypatch.setenv("STRATEGY_FACTORY_DIAGNOSTIC_OBSERVATION_STATUS", "submitted")

    action = _diagnostic_observation_submission_action({}, reason="weak_wf_ic_ir")

    assert action["final_status"] == "submitted"
    assert action["submission_action"]["final_status"] == "submitted"


def test_diagnostic_fingerprint_is_stable_for_same_semantic_candidate():
    candidate = _candidate(
        strategy_type="momentum",
        target_symbols=["688313.SH", "688387.SH"],
        params={"lookback": 20, "target_symbols": ["688313.SH", "688387.SH"]},
    )
    replayed = {
        **candidate,
        "id": "fresh-id",
        "trace_id": "fresh-trace",
        "params": {**candidate["params"], "task_run_id": "fresh-task"},
    }

    assert _diagnostic_observation_fingerprint(candidate, "win_rate_0_372_0_400") == (
        _diagnostic_observation_fingerprint(replayed, "win_rate_0_372_0_400")
    )


@pytest.mark.asyncio
async def test_diagnostic_guard_blocks_when_incubation_factory_is_stale():
    class DB:
        async def get_incubation_factory_health(self, *, max_age_hours):
            return {
                "healthy": False,
                "max_age_hours": max_age_hours,
                "stale_reason": "incubation_activity_stale",
            }

        async def find_active_diagnostic_observation_by_fingerprint(self, fingerprint, *, ttl_days):
            return None

    guard = await StrategySubmitter()._diagnostic_observation_admission_guard(
        DB(),
        candidate=_candidate(),
        reason="win_rate_0_372_0_400",
        fingerprint="diag_same",
    )

    assert guard["allowed"] is False
    assert guard["reason"] == "incubation_factory_stale"
    assert guard["incubation_factory_health"]["stale_reason"] == "incubation_activity_stale"


@pytest.mark.asyncio
async def test_diagnostic_guard_blocks_duplicate_fingerprint(monkeypatch):
    monkeypatch.setenv("STRATEGY_FACTORY_DIAGNOSTIC_OBSERVATION_HEALTH_GUARD_ENABLED", "0")

    class DB:
        async def find_active_diagnostic_observation_by_fingerprint(self, fingerprint, *, ttl_days):
            return {
                "id": "existing-strategy",
                "diagnostic_account_id": "existing-account",
            }

    guard = await StrategySubmitter()._diagnostic_observation_admission_guard(
        DB(),
        candidate=_candidate(),
        reason="win_rate_0_372_0_400",
        fingerprint="diag_same",
    )

    assert guard["allowed"] is False
    assert guard["reason"] == "diagnostic_fingerprint_duplicate"
    assert guard["existing_strategy_id"] == "existing-strategy"
    assert guard["existing_account_id"] == "existing-account"


@pytest.mark.asyncio
async def test_diagnostic_slot_claim_dedupes_same_batch_fingerprint(monkeypatch):
    monkeypatch.setenv("STRATEGY_FACTORY_DIAGNOSTIC_OBSERVATION_ENABLED", "1")
    monkeypatch.setenv("STRATEGY_FACTORY_DIAGNOSTIC_OBSERVATION_BATCH_LIMIT", "5")
    submitter = StrategySubmitter()

    assert await submitter._claim_diagnostic_observation_slot(fingerprint="diag_same") is True
    assert await submitter._claim_diagnostic_observation_slot(fingerprint="diag_same") is False
    assert await submitter._claim_diagnostic_observation_slot(fingerprint="diag_other") is True
