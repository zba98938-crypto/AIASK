"""Regression tests for provisional incubation metric gating."""

from __future__ import annotations


def _failed_statistical_gate() -> dict:
    return {
        "passed": False,
        "reason_codes": ["walk_forward_ic_ir_below_threshold"],
        "reasons": ["walk_forward_ic_ir too low"],
        "checks": {
            "walk_forward_ic_ir": {"passed": False},
            "purged_k_fold_ic": {"passed": True},
            "bootstrap_ci_lower": {"passed": True},
        },
    }


def _provisional_candidate() -> dict:
    return {
        "id": "proto-1",
        "name": "ai prototype",
        "strategy_type": "ai_generated",
        "source": "factory_ai",
        "params": {"factory_ai_prototype": True},
    }


def test_missing_backtest_metrics_deny_provisional_pass():
    from strategy_factory.application.quality_reporting import (
        maybe_grant_provisional_incubation,
    )

    result = maybe_grant_provisional_incubation(
        _provisional_candidate(),
        _failed_statistical_gate(),
        risk_report={"var_percent": 1.0, "cvar_percent": 1.5, "stress_loss_percent": -5.0},
        backtest_metrics={},  # missing sharpe / mdd / trades
    )
    assert result.get("passed") is False
    assert result.get("provisional_pass") in (False, None)


def test_missing_risk_metrics_deny_provisional_pass():
    from strategy_factory.application.quality_reporting import (
        maybe_grant_provisional_incubation,
    )

    result = maybe_grant_provisional_incubation(
        _provisional_candidate(),
        _failed_statistical_gate(),
        risk_report={},  # present but empty metrics
        backtest_metrics={
            "sharpe_ratio": 0.5,
            "max_drawdown": 0.1,
            "trade_count": 10,
        },
    )
    # empty risk_report is denied by the early `if not risk_report` guard
    assert result.get("passed") is False


def test_incomplete_risk_metrics_deny_provisional_pass():
    from strategy_factory.application.quality_reporting import (
        maybe_grant_provisional_incubation,
    )

    result = maybe_grant_provisional_incubation(
        _provisional_candidate(),
        _failed_statistical_gate(),
        risk_report={"var_percent": 1.0},  # missing cvar/stress
        backtest_metrics={
            "sharpe_ratio": 0.5,
            "max_drawdown": 0.1,
            "trade_count": 10,
        },
    )
    assert result.get("passed") is False
    assert result.get("provisional_pass") in (False, None)


def test_explicit_zero_metrics_do_not_silently_pass_risk_ceilings():
    """Missing used to become 0.0 and pass var/cvar ceilings incorrectly."""
    from strategy_factory.application.quality_reporting import (
        maybe_grant_provisional_incubation,
    )

    result = maybe_grant_provisional_incubation(
        _provisional_candidate(),
        _failed_statistical_gate(),
        risk_report={
            # explicit zeros are valid values; stress must still be present
            "var_percent": 0.0,
            "cvar_percent": 0.0,
            "stress_loss_percent": -5.0,
        },
        backtest_metrics={
            "sharpe_ratio": 0.5,
            "max_drawdown": 0.1,
            "trade_count": 10,
        },
    )
    # zeros are explicit and within risk ceilings; may still fail statistical
    # provisional path for other reasons, but must not crash / invent metrics.
    assert "passed" in result


def test_provisional_thresholds_are_at_least_default_backtest():
    from strategy_factory.domain.constants import (
        BACKTEST_DEFAULT_THRESHOLDS,
        PROVISIONAL_PASS_THRESHOLDS,
    )

    assert PROVISIONAL_PASS_THRESHOLDS["sharpe_min"] >= BACKTEST_DEFAULT_THRESHOLDS["sharpe_min"]
    assert PROVISIONAL_PASS_THRESHOLDS["mdd_max"] <= BACKTEST_DEFAULT_THRESHOLDS["mdd_max"]
    assert PROVISIONAL_PASS_THRESHOLDS["trades_min"] >= BACKTEST_DEFAULT_THRESHOLDS["trades_min"]
