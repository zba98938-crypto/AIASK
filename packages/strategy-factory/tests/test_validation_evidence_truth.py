"""Behavior regressions for the November evidence audit (S1-S5)."""
from datetime import date, timedelta

import numpy as np
import pytest

from aiask_quant_core.backtest import BacktestEngine
from strategy_factory.application import _panels_evaluation as panels
from strategy_factory.application.backtest_filter import BacktestFilter
from strategy_factory.application.oos_replay import holdout_identity, replay_frozen_holdout
from strategy_factory.application.submission_gate import runner as gate


def bars(count=220, start=date(2025, 1, 1), growth=0.001):
    return [{"date": str(start + timedelta(days=i)), "open": 10 * (1 + growth) ** i, "close": 10 * (1 + growth) ** i,
             "high": 10 * (1 + growth) ** i, "low": 10 * (1 + growth) ** i, "volume": 10000000} for i in range(count)]


class Db:
    def __init__(self, rows): self.rows = rows
    async def get_klines(self, code, limit): return self.rows[code]


@pytest.fixture
def panel_selection(monkeypatch):
    monkeypatch.setattr(panels, "_resolve_strategy_sample_selection", lambda *a, **kw: {"sample_codes": ["000001", "000002", "000003"]})


@pytest.mark.asyncio
async def test_daily_returns_reconcile_to_fill_engine_and_hold_during_zero_events(panel_selection):
    rows = bars()
    params = {"commission": 0.001}
    result = await panels._build_strategy_panels("buy_and_hold", params, Db({c: rows for c in ["000001", "000002", "000003"]}))
    replay = BacktestEngine.run_backtest("000001", rows, "buy_and_hold", {
        "initial_capital": 100000.0, "commission": 0.001, "market_ruleset": "cn_equity", "execution_price_policy": "next_open",
        "use_daily_execution_replay": True, "force_liquidate_at_end": False,
    }, True)["data"]
    equity = np.asarray(replay["equity_curve"])
    np.testing.assert_allclose(result["strategy_returns"], (equity[1:] / equity[:-1] - 1)[-120:])
    assert np.prod(1 + result["strategy_returns"]) - 1 > .12
    assert len(result["holdings"]) == 3
    assert all(row["quantity"] > 0 for row in result["holdings"])
    assert sum(row["weight"] for row in result["holdings"]) < 1.0  # unspent cash survives lot rounding


@pytest.mark.asyncio
async def test_disjoint_dates_are_unmeasurable(panel_selection):
    result = await panels._build_strategy_panels("buy_and_hold", {}, Db({"000001": bars(start=date(2021, 1, 1)), "000002": bars(start=date(2023, 1, 1)), "000003": bars(start=date(2025, 1, 1))}))
    assert result["reason"] == "insufficient_common_trade_intervals"
    assert "return_panel" not in result


@pytest.mark.asyncio
async def test_matching_end_date_does_not_mix_one_day_and_two_day_returns(panel_selection):
    rows = bars()
    missing_day = rows[150]["date"].replace("-", "")
    after_missing = rows[151]["date"].replace("-", "")
    result = await panels._build_strategy_panels("buy_and_hold", {}, Db({"000001": rows, "000002": rows, "000003": rows[:150] + rows[151:]}))
    assert missing_day not in result["dates"]
    assert after_missing not in result["dates"]
    np.testing.assert_allclose(result["return_panel"][:, 0], result["return_panel"][:, 2])


def test_full_backtest_return_is_not_oos():
    result = BacktestFilter._derive_trade_validation_metrics({"params": {}}, {"metrics": {"total_return": .42}, "layers": {"target": {"metrics": {"total_return": .42}}}})
    assert result["target_layer_oos_return"] is None
    assert result["target_backtest_return"] == .42
    assert result["oos_validation"]["reason"] == "missing_oos_validation_contract"
    evaluation = gate._evaluate_trade_profile({"strategy_type": "ma_cross", "params": {}}, {"profile": "trade_rule_validation"}, {"total_return": .42}, {})
    assert "missing_target_layer_oos_return" in evaluation["reasons"]
    assert not evaluation["passed"]


@pytest.mark.asyncio
async def test_holdout_replays_only_post_seal_trades_and_binds_params():
    rows = bars(count=180)
    candidate = {"strategy_type": "momentum", "params": {"lookback": 5, "threshold": .001}, "target_symbols": ["000001"]}
    execution = {**candidate["params"], "market_ruleset": "cn_equity", "execution_price_policy": "next_open", "commission": .001}
    candidate["oos_validation_contract"] = {
        **holdout_identity(candidate, execution), "sealed": True, "excluded_from_selection": True,
        "frozen_at": rows[139]["date"], "selection_data_end": rows[139]["date"], "start": rows[140]["date"], "end": rows[-1]["date"],
    }
    result = await replay_frozen_holdout(candidate, {"000001": rows}, BacktestEngine, execution, codes=["000001"], timeout=10)
    assert result["status"] == "measured"
    expected = BacktestEngine.run_backtest("000001", rows, "momentum", {**execution, "trade_start_index": 139, "use_daily_execution_replay": True, "force_liquidate_at_end": True}, True)["data"]
    assert result["total_return"] == pytest.approx(expected["total_return"])
    assert all(fill["index"] >= 140 for fill in expected["fills"])
    assert result["sessions"] == 40
    assert result["total_return"] < .06  # full 180-day rise is about 20%
    changed_costs = await replay_frozen_holdout(candidate, {"000001": rows}, BacktestEngine, {**execution, "commission": .002}, codes=["000001"], timeout=10)
    assert changed_costs["reason"] == "holdout_candidate_hash_mismatch"
    changed_targets = await replay_frozen_holdout({**candidate, "target_symbols": ["000002"]}, {"000001": rows}, BacktestEngine, execution, codes=["000001"], timeout=10)
    assert changed_targets["reason"] == "holdout_candidate_hash_mismatch"
    candidate["params"]["lookback"] = 10
    invalid = await replay_frozen_holdout(candidate, {"000001": rows}, BacktestEngine, execution, codes=["000001"], timeout=10)
    assert invalid["reason"] == "holdout_candidate_hash_mismatch"
    assert invalid["total_return"] is None


@pytest.mark.asyncio
async def test_real_zero_parameter_sensitivity_is_accepted():
    result = await gate._run_statistical_gate(None, {"strategy_type": "value_factor", "params": {}}, profile={"profile": "factor_rank_validation"}, validation_report={
        "walk_forward": {"oos_rank_ic_ir": .35}, "purged_kfold": {"oos_rank_ic_mean": .03}, "bootstrap_ci": {"ci_lower": .02},
    }, backtest_metrics={"parameter_perturbation_trade_stability": 1.0})
    assert result["param_sensitivity"] == 0
    assert result["passed"]
    assert "missing_param_sensitivity" not in result["reasons"]

@pytest.mark.asyncio
async def test_gate2_uses_flattened_benchmark_returns(monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    import asyncio
    from strategy_factory.application import backtest_filter as module
    codes = ["000001", "000002", "000003"]
    db = Db({code: bars() for code in codes})
    class Engine:
        def __init__(self, benchmark): self.benchmark = benchmark
        def run_backtest(self, code, *args):
            return {"success": True, "data": {"code": code, "sharpe_ratio": .1, "total_return": .01, "trades_count": 12, "max_drawdown": .08, "win_rate": .5, "benchmark_return": self.benchmark}}
    monkeypatch.setattr(module, "_get_strategy_factory_package", lambda: SimpleNamespace(asyncio=asyncio))
    results = []
    for benchmark in [-.3, .3, 0.0, None, float("nan"), "invalid"]:
        subject = BacktestFilter()
        monkeypatch.setattr(subject, "_resolve_backtest_plan", lambda c: (codes, [], codes, "representative_only", "target_plus_representative"))
        monkeypatch.setattr(subject, "_run_portfolio_engine_summary", AsyncMock(return_value={}))
        monkeypatch.setattr(subject, "_run_parameter_perturbation_study", AsyncMock(return_value={}))
        monkeypatch.setattr(subject, "_get_thresholds", lambda *a: {"sharpe_min": .5, "trades_min": 3, "mdd_max": .3, "min_samples": 1})
        output = await subject._test_one({"strategy_type": "ma_cross", "params": {"short_period": 5, "long_period": 20}}, db, Engine(benchmark))
        results.append(output["metrics"]["adaptive_gate_2"])
    assert [result["regime"] for result in results] == ["bear", "bull", "neutral", "unknown", "unknown", "unknown"]
    assert results[0]["multi_score"] != results[1]["multi_score"]
    assert results[2]["benchmark_return"] == 0
    for missing in results[3:]:
        assert missing["benchmark_return"] is None
        assert not missing["passed_relative"]
        assert not missing["passed_regime"]
        assert missing["multi_score"] < results[2]["multi_score"]

@pytest.mark.asyncio
async def test_holdout_evidence_survives_real_filter_submission_path(monkeypatch):
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from strategy_factory.application import backtest_filter as module
    from strategy_factory.application.candidate_contract import apply_resolved_candidate_envelope
    rows = bars()
    codes = ["000001", "000002", "000003"]
    subject = BacktestFilter()
    candidate = apply_resolved_candidate_envelope({"strategy_type": "momentum", "params": {"lookback": 5, "threshold": .001}, "target_symbols": codes})
    execution = {**module._ensure_strategy_params("momentum", candidate["params"], research_task=candidate.get("research_task")), **subject._build_backtest_assumptions(candidate).to_backtest_kwargs()}
    candidate["oos_validation_contract"] = {
        **holdout_identity(candidate, execution), "sealed": True, "excluded_from_selection": True,
        "frozen_at": rows[179]["date"], "selection_data_end": rows[179]["date"], "start": rows[180]["date"], "end": rows[-1]["date"],
    }
    monkeypatch.setattr(module, "_get_strategy_factory_package", lambda: SimpleNamespace(asyncio=asyncio))
    monkeypatch.setattr(subject, "_resolve_backtest_plan", lambda c: (codes, codes, [], "explicit", "target_only"))
    monkeypatch.setattr(subject, "_run_portfolio_engine_summary", AsyncMock(return_value={}))
    monkeypatch.setattr(subject, "_run_parameter_perturbation_study", AsyncMock(return_value={}))
    output = await subject._test_one(candidate, Db({c: rows for c in codes}), BacktestEngine)
    assert output["oos_validation"]["status"] == "measured", output["oos_validation"]
    subject._apply_result_to_candidate(candidate, output, [], [])
    metrics = candidate["backtest_metrics"]
    assert metrics["oos_validation"]["status"] == "measured"
    assert metrics["target_layer_oos_return"] == pytest.approx(output["oos_validation"]["total_return"], abs=1e-4)
    profile = gate._evaluate_trade_profile(candidate, {"profile": "trade_rule_validation"}, metrics, {})
    assert "unverified_target_layer_oos_return" not in profile["reasons"]
    assert "missing_target_layer_oos_return" not in profile["reasons"]
    assert profile["oos_validation"]["sealed"] is True


def test_an_unsealed_legacy_oos_number_cannot_pass_as_independent_evidence():
    result = gate._evaluate_trade_profile({"strategy_type": "ma_cross"}, {"profile": "trade_rule_validation"}, {"target_layer_oos_return": .42}, {})
    assert result["target_layer_oos_return"] is None
    assert "unverified_target_layer_oos_return" in result["reasons"]


@pytest.mark.asyncio
async def test_explicit_unavailable_zero_remains_missing():
    result = await gate._run_statistical_gate(None, {"strategy_type": "value_factor", "params": {}}, profile={"profile": "factor_rank_validation"}, validation_report={
        "statistical_metrics": {
            "wf_ic_ir": {"value": .35}, "pkf_ic": {"value": .03}, "bootstrap_ci_lower": {"value": .02},
            "param_sensitivity": {"value": 0.0, "available": False},
        },
    })
    assert "missing_param_sensitivity" in result["reasons"]
    assert not result["passed"]


def sealed_holdout(candidate, execution, rows, first=180):
    return {**candidate, "oos_validation_contract": {
        **holdout_identity(candidate, execution), "sealed": True, "excluded_from_selection": True,
        "frozen_at": rows[first - 1]["date"], "selection_data_end": rows[first - 1]["date"],
        "start": rows[first]["date"], "end": rows[-1]["date"],
    }}


@pytest.mark.asyncio
async def test_buy_and_hold_enters_at_holdout_open_instead_of_losing_warmup_entry():
    rows = bars()
    execution = {"market_ruleset": "cn_equity", "execution_price_policy": "next_open", "commission": .001}
    candidate = sealed_holdout({"strategy_type": "buy_and_hold", "params": {}, "target_symbols": ["000001"]}, execution, rows)
    result = await replay_frozen_holdout(candidate, {"000001": rows}, BacktestEngine, execution, codes=["000001"], timeout=10)
    assert result["status"] == "measured"
    assert result["initial_state"] == "cash_at_holdout_start"
    assert .03 < result["total_return"] < .05
    assert result["per_code"][0]["trades_count"] == 2
    expected = BacktestEngine.run_backtest("000001", rows[179:], "buy_and_hold", {
        **execution, "use_daily_execution_replay": True, "force_liquidate_at_end": True,
    }, True)["data"]
    assert result["total_return"] == pytest.approx(expected["total_return"])
    assert expected["fills"][0]["time"] == rows[180]["date"]


@pytest.mark.asyncio
async def test_stateful_dsl_resets_position_before_holdout_with_warm_indicators():
    from aiask_quant_core.backtest.dsl_strategy import DslRuleStrategy

    rows = bars()
    dsl = {
        "entry": {"op": "gt", "left": {"field": "close"}, "right": {"indicator": "sma", "field": "close", "window": 20}},
        "exit": {"op": "lt", "left": {"field": "close"}, "right": {"value": 0}},
    }
    execution = {"dsl": dsl, "market_ruleset": "cn_equity", "execution_price_policy": "next_open", "commission": .001}
    candidate = sealed_holdout({"strategy_type": "momentum", "params": {"dsl": dsl}, "target_symbols": ["000001"]}, execution, rows)
    result = await replay_frozen_holdout(candidate, {"000001": rows}, BacktestEngine, execution, codes=["000001"], timeout=10)
    assert result["status"] == "measured"
    assert .03 < result["total_return"] < .05
    assert result["per_code"][0]["trades_count"] == 2
    runtime = DslRuleStrategy()
    runtime.set_parameters({**execution, "trade_start_index": 179})
    events = runtime.generate_signal_events_from_klines(rows)
    assert events == [{"index": 179, "signal": 1, "action": "enter", "units": 1.0}]


@pytest.mark.asyncio
async def test_unsupported_stateful_holdout_is_unavailable_instead_of_measured_zero():
    rows = bars()
    execution = {"market_ruleset": "cn_equity", "execution_price_policy": "next_open"}
    candidate = sealed_holdout({"strategy_type": "rsi", "params": {}, "target_symbols": ["000001"]}, execution, rows)
    result = await replay_frozen_holdout(candidate, {"000001": rows}, BacktestEngine, execution, codes=["000001"], timeout=10)
    assert result["status"] == "unavailable"
    assert result["total_return"] is None
    assert result["reason"] == "holdout_strategy_state_reset_unsupported"


@pytest.mark.asyncio
async def test_reset_dsl_without_entry_opportunity_can_measure_real_zero():
    rows = bars()
    dsl = {
        "entry": {"op": "lt", "left": {"field": "close"}, "right": {"value": 0}},
        "exit": {"op": "lt", "left": {"field": "close"}, "right": {"value": 0}},
    }
    execution = {"dsl": dsl, "market_ruleset": "cn_equity", "execution_price_policy": "next_open"}
    candidate = sealed_holdout({"strategy_type": "momentum", "params": {"dsl": dsl}, "target_symbols": ["000001"]}, execution, rows)
    result = await replay_frozen_holdout(candidate, {"000001": rows}, BacktestEngine, execution, codes=["000001"], timeout=10)
    assert result["status"] == "measured"
    assert result["total_return"] == 0
    assert result["per_code"][0]["trades_count"] == 0
