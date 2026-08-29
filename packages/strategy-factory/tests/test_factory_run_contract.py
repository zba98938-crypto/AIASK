from __future__ import annotations

import pytest

from strategy_factory.api.factory_runs import (
    FactoryName,
    FactoryRunContract,
    FactoryRunState,
    normalize_factory_name,
    normalize_factory_run_state,
    validate_factory_run_transition,
)


def test_factory_aliases_and_domain_statuses_normalize_to_control_contract() -> None:
    assert normalize_factory_name("factor_factory") is FactoryName.FACTOR_MINING
    assert normalize_factory_run_state("success_observe_only") is FactoryRunState.SUCCEEDED
    assert normalize_factory_run_state("partial_infra") is FactoryRunState.PARTIAL
    assert normalize_factory_run_state("skipped") is FactoryRunState.BLOCKED


def test_contract_round_trip_is_stable_and_marks_terminal_state() -> None:
    contract = FactoryRunContract.from_mapping(
        {
            "run_id": "run_1",
            "factory_name": "market_event_ingest",
            "status": "partial",
            "owner_id": "worker-1",
            "params": {"codes": ["600000.SH"]},
            "quality_flags": ["source_degraded"],
            "summary": {"saved": 2},
        }
    )
    payload = contract.to_dict()
    assert payload["factory"] == "market_event_ingest"
    assert payload["status"] == "partial"
    assert payload["terminal"] is True
    assert payload["owner_id"] == "worker-1"
    assert payload["params"] == {"codes": ["600000.SH"]}
    assert payload["summary"] == {"saved": 2}


def test_transition_contract_rejects_terminal_state_rewrite() -> None:
    assert validate_factory_run_transition("queued", "running") == (
        FactoryRunState.QUEUED,
        FactoryRunState.RUNNING,
    )
    with pytest.raises(ValueError, match="invalid factory run transition"):
        validate_factory_run_transition("succeeded", "running")
