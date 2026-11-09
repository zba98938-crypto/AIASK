"""Perturb parameters actually consumed by the native runtime, not metadata."""
import asyncio

import pytest

from aiask_quant_core.backtest import StrategyRegistry
from strategy_factory.application.backtest_filter import BacktestFilter


class Engine:
    def __init__(self):
        self.seen = []

    def run_backtest(self, code, klines, strategy, params, return_trades):
        instance, _ = StrategyRegistry.create_runtime_strategy(strategy, params)
        self.seen.append(instance.get_parameters())
        return {'success': True, 'data': {'trades_count': 10, 'total_return': 0.1}}


@pytest.mark.parametrize('strategy,params,key', [
    ('volatility_breakout', {'vol_window': 20, 'breakout_mult': 2.0, 'confidence': 0.8}, 'lookback'),
    ('event_structure_breakout', {'lookback': 20, 'confidence': 0.8}, 'breakout_buffer_pct'),
    ('multi_factor', {'rebalance_days': 20, 'top_n': 10, '_adaptive': True}, 'lookback'),
])
def test_missing_canonical_keys_use_effective_runtime_defaults(strategy, params, key):
    bf = BacktestFilter()
    bf._kline_cache['000001'] = [{'close': 10.0}] * 120
    engine = Engine()
    before = dict(params)
    original = StrategyRegistry.create_runtime_strategy(strategy, params)[0].get_parameters()
    out = asyncio.run(bf._run_parameter_perturbation_study(
        {'strategy_type': strategy, 'params': params}, engine,
        primary_code='000001', strategy_type=strategy, assumptions_kwargs={},
        baseline_metrics={'trades_count': 10, 'total_return': 0.1, '_resolved_strategy_params': params},
    ))
    assert out['parameter_perturbation_study']['variants_run'] > 0
    assert out['parameter_perturbation_study']['reason'] is None
    assert any(seen[key] != original[key] for seen in engine.seen)
    assert params == before  # callers retain their original contract


def test_captured_baseline_params_not_replaced_by_fresh_spawner_draw():
    bf = BacktestFilter()
    bf._kline_cache['000001'] = [{'close': 10.0}] * 120
    engine = Engine()
    out = asyncio.run(bf._run_parameter_perturbation_study(
        {'strategy_type': 'volatility_breakout', 'params': {'lookback': 100, 'threshold': 0.1}}, engine,
        primary_code='000001', strategy_type='volatility_breakout', assumptions_kwargs={},
        baseline_metrics={'trades_count': 10, 'total_return': 0.1,
                          '_resolved_strategy_params': {'lookback': 20, 'threshold': 0.025}},
    ))
    assert out['parameter_perturbation_study']['variants_run'] > 0
    assert all(10 <= seen['lookback'] <= 30 for seen in engine.seen)
