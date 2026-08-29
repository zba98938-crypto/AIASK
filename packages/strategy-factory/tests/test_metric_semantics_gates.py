"""Metric semantics contract for quality / admission gates.

None = missing (hard fail on critical incubation/live gates)
0.0  = real zero (compare against thresholds)
undefined excellence must be None, never 999.0
"""

from __future__ import annotations


def _semantic_strategy(strategy_type: str = "ma_cross") -> dict:
    return {
        "id": "metric_semantics_candidate",
        "name": "metric semantics candidate",
        "strategy_type": strategy_type,
        "params": {
            "evidence_chain": {"evidences": [{"id": "ev1", "source": "test"}]},
            "prediction_contract": {"claims": [{"id": "cl1", "evidence_ids": ["ev1"]}]},
            "confidence_contract": {"probability": 0.62, "support_samples": 24},
        },
    }


def _strong_trade_metrics(**overrides) -> dict:
    base = {
        "post_cost_sharpe": 1.1,
        "sharpe_ratio": 1.1,
        "total_return": 0.24,
        "target_layer_oos_return": 0.18,
        "max_drawdown": 0.08,
        "trade_count": 24,
        "trades_count": 24,
        "avg_holding_days": 12,
        "trade_density": 0.4,
        "win_rate": 0.62,
        "profit_factor": 2.1,
        "parameter_perturbation_trade_stability": 0.85,
        "expectancy": 10.0,
    }
    base.update(overrides)
    return base


def _risk() -> dict:
    return {"var_percent": 1.0, "cvar_percent": 1.5, "stress_loss_percent": -5.0}


def test_optional_metric_value_distinguishes_missing_and_zero():
    from strategy_factory.application.quality_reporting import (
        optional_metric_value,
        safe_metric_value,
    )

    assert optional_metric_value({}, "sharpe_ratio") is None
    assert optional_metric_value({"sharpe_ratio": None}, "sharpe_ratio") is None
    assert optional_metric_value({"sharpe_ratio": 0.0}, "sharpe_ratio") == 0.0
    assert optional_metric_value({"sharpe_ratio": 1.2}, "sharpe_ratio") == 1.2
    assert safe_metric_value({}, "sharpe_ratio") == 0.0


def test_missing_parameter_stability_fails_incubation_gate():
    from strategy_factory.application.submission_gate import runner

    metrics = _strong_trade_metrics()
    metrics.pop("parameter_perturbation_trade_stability", None)
    result = runner._evaluate_trade_profile(
        _semantic_strategy(),
        {"profile": "trade_rule_validation", "validation_focus": "target_plus_representative"},
        metrics,
        risk_report=_risk(),
        admission_level="incubation",
    )
    reasons = " ".join(result.get("reasons") or [])
    assert "missing_parameter_perturbation_trade_stability" in reasons
    assert result.get("passed") is False


def test_negative_oos_sharpe_fails_when_oos_keys_present():
    from strategy_factory.application.submission_gate import runner

    result = runner._evaluate_trade_profile(
        _semantic_strategy(),
        {"profile": "trade_rule_validation", "validation_focus": "target_plus_representative"},
        _strong_trade_metrics(
            walk_forward_efficiency=0.8,
            out_of_sample_sharpe=-0.5,
            out_of_sample_profit_factor=1.5,
        ),
        risk_report=_risk(),
        admission_level="incubation",
    )
    reasons = " ".join(result.get("reasons") or [])
    assert "out_of_sample_sharpe" in reasons
    assert result.get("passed") is False


def test_zero_profit_factor_fails_instead_of_skipping():
    from strategy_factory.application.submission_gate import runner

    result = runner._evaluate_trade_profile(
        _semantic_strategy(),
        {"profile": "trade_rule_validation", "validation_focus": "target_plus_representative"},
        _strong_trade_metrics(profit_factor=0.0),
        risk_report=_risk(),
        admission_level="incubation",
    )
    reasons = " ".join(result.get("reasons") or [])
    assert "profit_factor" in reasons
    assert result.get("passed") is False


def test_reporting_source_has_no_999_sentinel():
    from pathlib import Path

    src = (
        Path(__file__).resolve().parents[1]
        / "src/strategy_factory/application/backtest_filter.py"
    ).read_text(encoding="utf-8")
    assert "999.0" not in src
