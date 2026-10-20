"""Behavioral tests for the submission-side parameter-perturbation study."""

from __future__ import annotations

import math

from strategy_factory.application.param_perturbation_study import (
    build_perturbation_variants,
    direction_agreement,
    numeric_params,
    pearson_correlation,
    run_parameter_perturbation_study,
    trade_count_response_correlation,
    trade_count_stability,
)


def test_numeric_params_filters_non_numeric_and_zero():
    assert numeric_params({"a": 2, "b": 0.0, "c": "x", "d": True, "e": None, "f": float("nan")}) == {"a": 2.0}


def test_build_perturbation_variants_one_param_at_a_time_capped():
    variants = build_perturbation_variants({"fast": 5, "slow": 20}, cap=8)
    # deterministic: sorted param names, factors in fixed order, truncated to cap
    assert len(variants) == 8
    assert [v["param"] for v in variants] == ["fast"] * 4 + ["slow"] * 4
    assert [v["factor"] for v in variants] == [0.8, 0.9, 1.1, 1.2] * 2
    assert variants[0]["value"] == 4.0
    assert all(v["value"] > 0 for v in variants)


def test_trade_count_stability_exact_and_missing():
    assert trade_count_stability(10, [10, 10, 10, 10]) == 1.0
    assert trade_count_stability(10, [20, 20, 20, 20]) == 0.0
    assert trade_count_stability(1, [1, 2]) == 0.5
    assert trade_count_stability(10, []) is None


def test_pearson_needs_three_points_and_variance():
    assert pearson_correlation([0.1, 0.2, 0.3], [1.0, 2.0, 3.0]) == pytest_approx(1.0)
    assert pearson_correlation([0.1, 0.1], [1.0, 2.0]) is None  # too few
    assert pearson_correlation([0.1, 0.1, 0.1], [1.0, 2.0, 3.0]) is None  # no variance in x


def test_direction_agreement_signed_baseline():
    assert direction_agreement(0.05, [0.04, -0.01, 0.02]) == pytest_approx(2 / 3)
    assert direction_agreement(0.0, [0.04, -0.01]) is None  # zero baseline: direction undefined


def pytest_approx(x):
    class _Approx:
        def __init__(self, value):
            self.value = value
        def __eq__(self, other):
            return abs(other - self.value) < 1e-9
    return _Approx(x)


class _StableEngine:
    """Trades count and return barely move with params -> high stability."""

    def __init__(self):
        self.calls = 0

    def run_backtest(self, code, klines, strategy, params, return_trades):
        self.calls += 1
        return {
            "success": True,
            "data": {"trades_count": 20, "total_return": 0.12},
        }


def test_study_stable_params_high_stability_all_stats():
    engine = _StableEngine()
    klines = [{"close": 10.0, "volume": 1000, "time": "2026-01-05"}]
    out = _run_async(
        run_parameter_perturbation_study(
            engine=engine,
            code="000001",
            klines=klines,
            strategy_type="ma_cross",
            base_params={"fast": 5, "slow": 20, "initial_capital": 100000},
            baseline_result={"success": True, "data": {"trades_count": 20, "total_return": 0.12}},
        )
    )
    assert out["parameter_perturbation_trade_stability"] == 1.0
    # magnitudes [0.2,0.1,0.1,0.2]*k have variance; responses are 0 -> no variance -> None
    assert out["parameter_perturbation_trade_count_correlation"] is None
    assert out["parameter_perturbation_direction_agreement"] == 1.0
    study = out["parameter_perturbation_study"]
    assert study["variants_run"] == 8
    assert study["variants_failed"] == 0
    assert study["reason"] is None
    assert engine.calls == 8


class _SensitiveEngine:
    """Trade count scales with the perturbed param -> low stability, real correlation."""

    def run_backtest(self, code, klines, strategy, params, return_trades):
        fast = float(params["fast"])
        return {
            "success": True,
            "data": {"trades_count": int(fast), "total_return": 0.01 * fast},
        }


def test_study_sensitive_params_correlation_positive():
    engine = _SensitiveEngine()
    klines = [{"close": 10.0, "volume": 1000, "time": "2026-01-05"}]
    out = _run_async(
        run_parameter_perturbation_study(
            engine=engine,
            code="000001",
            klines=klines,
            strategy_type="ma_cross",
            base_params={"fast": 10},
            baseline_result={"success": True, "data": {"trades_count": 10, "total_return": 0.1}},
        )
    )
    # variant trades [8,9,11,12] vs baseline 10 -> mean relative deviation 0.15
    assert out["parameter_perturbation_trade_stability"] == pytest_approx(0.85)
    assert out["parameter_perturbation_trade_count_correlation"] == pytest_approx(1.0)
    assert out["parameter_perturbation_direction_agreement"] == 1.0


def test_study_no_numeric_params_fail_closed_no_zero_fill():
    engine = _StableEngine()
    out = _run_async(
        run_parameter_perturbation_study(
            engine=engine,
            code="000001",
            klines=[{"close": 10.0}],
            strategy_type="ma_cross",
            base_params={"target_symbols": ["000001"]},
            baseline_result={"success": True, "data": {"trades_count": 5}},
        )
    )
    assert out["parameter_perturbation_trade_stability"] is None
    assert out["parameter_perturbation_trade_count_correlation"] is None
    assert out["parameter_perturbation_direction_agreement"] is None
    assert out["parameter_perturbation_study"]["reason"] == "no_numeric_params"
    assert out["parameter_perturbation_study"]["variants_run"] == 0


def test_study_klines_unavailable_fail_closed():
    engine = _StableEngine()
    out = _run_async(
        run_parameter_perturbation_study(
            engine=engine,
            code="000001",
            klines=[],
            strategy_type="ma_cross",
            base_params={"fast": 5, "slow": 20},
            baseline_result={"success": True, "data": {"trades_count": 5}},
        )
    )
    assert out["parameter_perturbation_study"]["reason"] == "klines_unavailable"
    assert engine.calls == 0


class _AllFailEngine:
    def run_backtest(self, code, klines, strategy, params, return_trades):
        return {"success": False, "error": "boom"}


def test_study_all_variant_runs_failed_fail_closed():
    out = _run_async(
        run_parameter_perturbation_study(
            engine=_AllFailEngine(),
            code="000001",
            klines=[{"close": 10.0}],
            strategy_type="ma_cross",
            base_params={"fast": 5},
            baseline_result={"success": True, "data": {"trades_count": 5}},
        )
    )
    study = out["parameter_perturbation_study"]
    assert study["variants_failed"] == 4
    assert study["reason"] == "perturbation_runs_failed"
    assert out["parameter_perturbation_trade_stability"] is None


class _SlowEngine:
    """Each run sleeps past the per-run timeout -> every variant fails."""

    def __init__(self):
        self.calls = 0

    def run_backtest(self, code, klines, strategy, params, return_trades):
        import time as _time

        self.calls += 1
        _time.sleep(0.2)
        return {"success": True, "data": {"trades_count": 1, "total_return": 0.0}}


def test_study_per_run_timeout_fail_closed():
    engine = _SlowEngine()
    out = _run_async(
        run_parameter_perturbation_study(
            engine=engine,
            code="000001",
            klines=[{"close": 10.0}],
            strategy_type="ma_cross",
            base_params={"fast": 5},
            baseline_result={"success": True, "data": {"trades_count": 5}},
            variant_cap=4,
            budget_sec=30.0,
            code_timeout_sec=0.05,
        )
    )
    study = out["parameter_perturbation_study"]
    assert study["variants_failed"] == 4
    assert study["variants_run"] == 0  # timed-out runs are incomplete, counted as failed only
    assert study["reason"] == "perturbation_runs_failed"
    assert out["parameter_perturbation_trade_stability"] is None


def test_insufficient_successful_runs_only_stability_available():
    # 2 of 4 variants succeed -> stability measurable, correlation is not
    class _PartialEngine:
        def __init__(self):
            self.calls = 0

        def run_backtest(self, code, klines, strategy, params, return_trades):
            self.calls += 1
            if self.calls <= 2:
                return {"success": True, "data": {"trades_count": 10, "total_return": 0.1}}
            return {"success": False, "error": "failed"}

    engine = _PartialEngine()
    out = _run_async(
        run_parameter_perturbation_study(
            engine=engine,
            code="000001",
            klines=[{"close": 10.0}],
            strategy_type="ma_cross",
            base_params={"fast": 5},
            baseline_result={"success": True, "data": {"trades_count": 10, "total_return": 0.1}},
        )
    )
    study = out["parameter_perturbation_study"]
    assert out["parameter_perturbation_trade_stability"] == 1.0
    assert out["parameter_perturbation_trade_count_correlation"] is None
    assert out["parameter_perturbation_direction_agreement"] is None
    assert study["reason"] == "insufficient_runs_for_correlation"


def _run_async(coro):
    import asyncio
    return asyncio.run(coro)
