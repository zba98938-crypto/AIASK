from __future__ import annotations

import math

from akshare_mcp.services.strategy_spec.runtime_contracts import (
    _build_parameter_coherence_audit,
    _default_runtime_playbook,
)


def _assert_all_finite(value) -> None:
    if isinstance(value, dict):
        for nested in value.values():
            _assert_all_finite(nested)
        return
    if isinstance(value, list):
        for nested in value:
            _assert_all_finite(nested)
        return
    if isinstance(value, float):
        assert math.isfinite(value)


def test_default_runtime_playbook_uses_density_aware_warmup() -> None:
    playbook = _default_runtime_playbook(
        "value_factor",
        holding_horizon={"min_days": 30, "max_days": 84, "cooldown_window_days": 7},
        backtest_metrics={"trade_count": 4},
    )
    incubation_policy = playbook["incubation_policy"]

    assert 4 <= incubation_policy["warmup_target_signals"] <= 8
    assert incubation_policy["warmup_target_signals"] != 20

    audit = _build_parameter_coherence_audit(
        "value_factor",
        holding_horizon={"min_days": 30, "max_days": 84},
        rebalance_rule={},
        runtime_playbook=playbook,
        instrument_profile={},
        backtest_metrics={"trade_count": 4},
    )

    assert "warmup_target_exceeds_signal_density" not in audit["blockers"]
    assert "warmup_target_exceeds_signal_density" not in audit["warnings"]


def test_default_runtime_playbook_sanitizes_non_finite_numeric_inputs() -> None:
    playbook = _default_runtime_playbook(
        "momentum",
        holding_horizon={"max_days": "inf", "cooldown_window_days": float("nan")},
        risk_rules={
            "atr_multiplier": "inf",
            "stop_loss_pct": float("nan"),
            "take_profit_pct": "-inf",
            "max_holding_days": float("inf"),
        },
        portfolio_spec={"max_position_pct": "inf"},
        execution_assumptions={"slippage_bps": float("nan"), "max_slippage_bps": "inf"},
        instrument_profile={
            "atr14_pct": "inf",
            "annual_volatility": float("nan"),
            "gap_p95": "-inf",
        },
        backtest_metrics={"trade_count": "inf"},
    )

    assert playbook["entry_policy"]["max_slippage_bps"] == 5.0
    assert playbook["exit_policy"]["time_stop_days"] == 20
    assert playbook["exit_policy"]["atr_multiplier"] == 2.0
    assert playbook["position_policy"]["max_position_pct"] == 0.18
    assert 4 <= playbook["incubation_policy"]["warmup_target_signals"] <= 8
    _assert_all_finite(playbook)
