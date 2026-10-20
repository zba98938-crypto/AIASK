from __future__ import annotations

from akshare_mcp.services.incubation_factory.runner import IncubationFactoryRunner


def test_strategy_max_holding_days_reads_params_hold_days():
    strategy = {"params": {"hold_days": 10}}
    assert IncubationFactoryRunner._strategy_max_holding_days(strategy) == 10


def test_strategy_max_holding_days_reads_mapping_search_hold():
    strategy = {"params": {"mapping_search": {"hold": 20}}}
    assert IncubationFactoryRunner._strategy_max_holding_days(strategy) == 20


def test_strategy_max_holding_days_prefers_explicit_exit_policy():
    strategy = {
        "params": {"hold_days": 10},
        "runtime_playbook": {"exit_policy": {"time_stop_days": 5}},
    }
    assert IncubationFactoryRunner._strategy_max_holding_days(strategy) == 5


def test_strategy_max_holding_days_ignores_missing_values_silently():
    assert IncubationFactoryRunner._strategy_max_holding_days({"params": {}}) == 0
