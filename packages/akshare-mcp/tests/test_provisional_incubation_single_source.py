"""The manager-side provisional-incubation path must stay fail-closed.

The local 0.0-default copy of ``maybe_grant_provisional_incubation`` was
removed from strategy_mgr_helpers: a strategy with missing backtest metrics
used to read as "zero drawdown" and could pass risk ceilings. The manager
now delegates to the strategy-factory implementation, where missing
required metrics deny the provisional pass.
"""

from __future__ import annotations


def _candidate() -> dict:
    return {
        "id": "proto-1",
        "name": "ai prototype",
        "strategy_type": "dsl_rule",
        "tags": ["factory", "ai_generated"],
    }


def _failed_statistical_gate() -> dict:
    return {
        "passed": False,
        "reason_codes": ["walk_forward_ic_ir_too_low"],
        "reasons": ["walk_forward_ic_ir too low"],
        "wf_ic_ir": 0.5,
        "pkf_ic": 0.1,
        "bootstrap_ci_lower": 0.05,
        "param_sensitivity": 0.1,
    }


def _passing_inputs() -> dict:
    return {
        "validation_report": None,
        "risk_report": {
            "var_percent": 1.0,
            "cvar_percent": 1.5,
            "stress_loss_percent": -5.0,
        },
        "backtest_metrics": {
            "sharpe_ratio": 0.5,
            "max_drawdown": 0.1,
            "trade_count": 10,
        },
    }


def test_manager_path_delegates_to_factory_implementation() -> None:
    from strategy_factory.api.quality_reporting import maybe_grant_provisional_incubation
    from akshare_mcp.tools.managers.strategy_mgr_helpers import (
        maybe_grant_provisional_incubation as manager_impl,
    )

    assert manager_impl is not maybe_grant_provisional_incubation  # wrapper, not alias
    # Same behavior: passing inputs grant the provisional pass on both sides.
    for impl in (maybe_grant_provisional_incubation, manager_impl):
        result = impl(_candidate(), _failed_statistical_gate(), **_passing_inputs())
        assert result.get("passed") is True
        assert result.get("provisional_pass") is True


def test_missing_backtest_metrics_are_denied_not_zero_filled() -> None:
    from akshare_mcp.tools.managers.strategy_mgr_helpers import (
        maybe_grant_provisional_incubation,
    )

    kwargs = _passing_inputs()
    kwargs["backtest_metrics"] = {}  # no sharpe / mdd / trades at all
    result = maybe_grant_provisional_incubation(
        _candidate(), _failed_statistical_gate(), **kwargs
    )
    assert result.get("passed") is False
    assert result.get("provisional_pass") in (False, None)


def test_missing_risk_metrics_are_denied_not_zero_filled() -> None:
    from akshare_mcp.tools.managers.strategy_mgr_helpers import (
        maybe_grant_provisional_incubation,
    )

    kwargs = _passing_inputs()
    # cvar/stress missing: the old 0.0-default copy treated this as
    # "no stress loss" and passed the ceilings.
    kwargs["risk_report"] = {"var_percent": 1.0}
    result = maybe_grant_provisional_incubation(
        _candidate(), _failed_statistical_gate(), **kwargs
    )
    assert result.get("passed") is False
    assert result.get("provisional_pass") in (False, None)
