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


# --- BacktestFilter wiring --------------------------------------------------
# Regression: _test_one stores each code's engine ``data`` payload directly in
# primary_results, but the baseline index read a nested ``data`` key and got {}
# for every code. 3,943/3,943 submission reports (2026-10-30..11-01) therefore
# carried reason=baseline_trades_unavailable with trades_count present.


def test_per_code_baseline_reads_flat_engine_payload():
    from strategy_factory.application.backtest_filter import BacktestFilter

    primary_results = [
        {"code": "000006", "trades_count": 31, "total_return": 0.085},
        {"code": "000007", "trades_count": 12, "total_return": -0.01},
    ]
    index = BacktestFilter._per_code_baseline_metrics(primary_results)

    assert index["000006"]["trades_count"] == 31
    assert index["000007"]["total_return"] == -0.01


class _RecordingEngine:
    def __init__(self):
        self.params_seen = []

    def run_backtest(self, code, klines, strategy, params, return_trades):
        self.params_seen.append(dict(params))
        return {"success": True, "data": {"trades_count": 30, "total_return": 0.08}}


def test_filter_study_measures_from_real_baseline_and_keeps_assumptions_fixed():
    import asyncio

    from strategy_factory.application.backtest_filter import BacktestFilter

    bf = BacktestFilter()
    bf._kline_cache["000006"] = [{"close": 10.0, "volume": 1000, "time": "2026-01-05"}] * 120
    engine = _RecordingEngine()
    assumptions = {"commission": 0.0003, "adv_ratio_limit": 0.1, "initial_capital": 100000.0}
    baseline = BacktestFilter._per_code_baseline_metrics(
        [{"code": "000006", "trades_count": 31, "total_return": 0.085}]
    )["000006"]

    out = asyncio.run(
        bf._run_parameter_perturbation_study(
            {"strategy_type": "momentum", "params": {"lookback": 20, "threshold": 0.02}},
            engine,
            primary_code="000006",
            strategy_type="momentum",
            assumptions_kwargs=assumptions,
            baseline_metrics=baseline,
        )
    )

    study = out["parameter_perturbation_study"]
    assert study["reason"] != "baseline_trades_unavailable"
    assert study["variants_run"] > 0
    assert out["parameter_perturbation_trade_stability"] is not None
    # Every variant runs with the real execution assumptions, unperturbed.
    assert engine.params_seen
    for seen in engine.params_seen:
        for key, value in assumptions.items():
            assert seen[key] == value
    # Only strategy params were perturbed.
    perturbed = {k for seen in engine.params_seen for k in ("lookback", "threshold") if seen[k] not in (20, 0.02)}
    assert perturbed == {"lookback", "threshold"}


class _RoundTripEngine:
    """Raw fills (18) differ from closed round trips (9), like the real engine."""

    def __init__(self):
        self.params_seen = []

    def run_backtest(self, code, klines, strategy, params, return_trades):
        self.params_seen.append(dict(params))
        trips = [
            {"status": "closed", "realized_pnl": 10.0, "realized_return": 0.01, "hold_days": 3}
            for _ in range(9)
        ]
        return {
            "success": True,
            "data": {"trades_count": 18, "total_return": 0.05, "round_trip_positions": trips, "initial_capital": 100000},
        }


def test_filter_study_ignores_metadata_and_compares_like_with_like():
    import asyncio

    from strategy_factory.application.backtest_filter import BacktestFilter

    bf = BacktestFilter()
    bf._kline_cache["000006"] = [{"close": 10.0, "volume": 1000, "time": "2026-01-05"}] * 120
    engine = _RoundTripEngine()
    raw = engine.run_backtest("000006", [], "momentum", {}, True)["data"]
    baseline = BacktestFilter._merge_trade_profile_metrics(raw)  # what _test_one stores
    engine.params_seen.clear()

    out = asyncio.run(
        bf._run_parameter_perturbation_study(
            {
                "strategy_type": "momentum",
                # numeric metadata that sorts before the real tunables
                "params": {"lookback": 20, "threshold": 0.02, "commission": 0.00025, "confidence": 0.77},
            },
            engine,
            primary_code="000006",
            strategy_type="momentum",
            assumptions_kwargs={"commission": 0.00025},
            baseline_metrics=baseline,
        )
    )

    # Unchanged strategy -> unchanged closed-trade count -> fully stable.
    assert out["parameter_perturbation_trade_stability"] == 1.0
    changed = {
        k for seen in engine.params_seen for k in ("lookback", "threshold", "confidence")
        if seen.get(k) not in (20, 0.02, 0.77)
    }
    assert changed == {"lookback", "threshold"}
