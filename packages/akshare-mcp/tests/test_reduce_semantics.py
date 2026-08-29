from __future__ import annotations

from akshare_mcp.services.backtest.dsl_strategy import DslRuleStrategy
from akshare_mcp.services.incubation import _runtime_action_lineage


def test_reduce_action_stays_partial_in_backtest_and_runtime_lineage():
    runtime_playbook = {
        "adverse_move_policy": {
            "loss_bands": [
                {"threshold_pct": 0.05, "action": "reduce", "label": "trim_half"},
            ]
        },
        "_provenance": {
            "source_trade_step_ids": ["tp_exit_reduce"],
        },
    }
    strategy = DslRuleStrategy(
        dsl={
            "entry": {"op": "gt", "left": {"field": "close"}, "right": {"value": 0}},
            "exit": {"op": "lt", "left": {"field": "close"}, "right": {"value": 0}},
        },
        runtime_playbook=runtime_playbook,
    )

    events = strategy.generate_signal_events_from_klines(
        [
            {"open": 100.0, "high": 100.0, "low": 100.0, "close": 100.0, "volume": 1000},
            {"open": 94.0, "high": 94.0, "low": 94.0, "close": 94.0, "volume": 1000},
            {"open": 93.0, "high": 93.0, "low": 93.0, "close": 93.0, "volume": 1000},
        ]
    )

    reduce_events = [event for event in events if event.get("action") == "reduce"]
    assert len(reduce_events) == 1
    assert reduce_events[0]["units"] == 0.5
    assert reduce_events[0]["remaining_units"] == 0.5

    lineage = _runtime_action_lineage(
        {
            "params": {
                "runtime_playbook": runtime_playbook,
                "claim_to_trade_plan_map": {
                    "trade_step_to_claim_ids": {
                        "tp_exit_reduce": ["claim_up"],
                    }
                },
                "trade_plan_to_dsl_map": {
                    "trade_step_to_dsl_sections": {
                        "tp_exit_reduce": ["exit"],
                    }
                },
            }
        },
        "runtime_playbook_trim_half",
    )

    assert lineage["runtime_action_reason"] == "reduce"
    assert lineage["applied_trade_step_id"] == "tp_exit_reduce"
    assert lineage["applied_claim_id"] == "claim_up"
