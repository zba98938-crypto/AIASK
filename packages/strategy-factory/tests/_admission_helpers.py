from __future__ import annotations

from typing import Any

import pytest


def _ready_trade_prediction_fields(strategy_id: str = "strategy-1") -> dict[str, Any]:
    from strategy_factory.application.trade_prediction_contract import freeze_trade_prediction_contract

    frozen = freeze_trade_prediction_contract(
        {
            "strategy_id": strategy_id,
            "stock_code": "600000",
            "prediction_as_of": "2026-06-05T09:30:00+08:00",
            "target_trading_date": "2026-06-08",
            "direction": "up",
            "confidence": 0.71,
            "horizon": "next_day",
            "evidence_refs": ["ev-1"],
        }
    )
    return {
        "trade_prediction_contract": frozen["contract"],
        "trade_prediction_contract_status": frozen["status"],
        "trade_prediction_contract_hash": frozen["contract_hash"],
        "trade_prediction_contract_missing_fields": list(frozen.get("missing_fields") or []),
        "trade_prediction_contract_reject_reasons": list(frozen.get("reject_reasons") or []),
    }


def _runtime_ready_candidate(
    strategy_id: str = "strategy-1",
    *,
    observe_first: bool = False,
    **overrides: Any,
) -> dict[str, Any]:
    params = {
        **_ready_trade_prediction_fields(strategy_id),
    }
    candidate = {
        "id": strategy_id,
        "strategy_type": "volatility_breakout",
        "target_symbols": ["600000"],
        "params": params,
    }
    if observe_first:
        candidate["observe_first_intake"] = True
        candidate["incubation_budget"] = {
            "track": "observe_incubation",
            "budget_tier": "micro",
            "observe_first_intake": True,
        }
        params["observe_first_intake"] = True
    candidate.update(overrides)
    return candidate


def _formal_runtime(_gate: dict[str, Any], *, candidate: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "runtime_bootstrap_eligible": True,
        "runtime_bootstrap_reason": "quality_passed_non_d_candidate_with_complete_runtime_contract",
        "runtime_bootstrap_budget_tier": "standard",
        "semantic_runtime_match": True,
        "proxy_runtime_used": False,
        "diagnostic_only": False,
        "execution_readiness_tier": "formal_runtime_ready",
        "execution_semantic_gap": False,
        "execution_semantic_gap_reasons": [],
    }


def _resolve(
    gate: dict[str, Any],
    *,
    track: str = "formal_incubation",
    read_only: bool = False,
) -> dict[str, Any]:
    from strategy_factory.application.services.admission_authority import (
        ADMISSION_DECISION_CONTRACT_VERSION,
        SubmissionAdmissionAuthority,
    )

    result = SubmissionAdmissionAuthority.resolve(
        gate,
        candidate={"strategy_type": "momentum"},
        refresh_existing=False,
        existing_status="draft",
        incubation_budget_track=track,
        runtime_bootstrap_resolver=_formal_runtime,
        read_only=read_only,
    )

    assert result["admission_decision_contract_version"] == ADMISSION_DECISION_CONTRACT_VERSION
    assert result["submission_action"]["admission_decision_contract_version"] == ADMISSION_DECISION_CONTRACT_VERSION
    return result


@pytest.fixture(autouse=True)
def _clear_dev_v1_env(monkeypatch):
    monkeypatch.delenv("STRATEGY_FACTORY_OBSERVE_D_GRADE_ENABLED", raising=False)
    monkeypatch.delenv("STRATEGY_FACTORY_GATE3_RECORD_ONLY_ENABLED", raising=False)
    yield


def _resolve_with_real_resolver(
    gate: dict[str, Any],
    candidate: dict[str, Any],
    *,
    track: str = "formal_incubation",
) -> dict[str, Any]:
    """直接用真实 _runtime_bootstrap_context (不再 mock)。

    这样能完整验证 toggle 与 _runtime_bootstrap_context 的集成。
    """
    from strategy_factory.application.services.admission_authority import (
        SubmissionAdmissionAuthority,
    )
    from strategy_factory.application.submitter import StrategySubmitter

    return SubmissionAdmissionAuthority.resolve(
        gate,
        candidate=candidate,
        refresh_existing=False,
        existing_status="draft",
        incubation_budget_track=track,
        runtime_bootstrap_resolver=StrategySubmitter._runtime_bootstrap_context,
    )
