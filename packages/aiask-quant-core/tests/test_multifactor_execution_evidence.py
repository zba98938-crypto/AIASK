from datetime import date, timedelta

import numpy as np
import pytest

from aiask_quant_core.backtest.engine import BacktestEngine
from aiask_quant_core.backtest.multi_factor_strategy import MultiFactorStrategy


def prices(count=500):
    rng = np.random.default_rng(91)
    drift = np.concatenate([np.full(200, .001), np.full(count - 200, -.0006)])
    return 20 * np.exp(np.cumsum(rng.normal(drift, .015, count)))


@pytest.mark.parametrize('end', [150, 250, 350, 450])
def test_appending_future_prices_never_changes_existing_signals(end):
    values = prices()
    strategy = MultiFactorStrategy()
    before = strategy.generate_signals(values[:end])
    after = strategy.generate_signals(values)
    np.testing.assert_array_equal(before, after[:end])


def test_future_factor_variance_cannot_create_earlier_trades():
    values = np.r_[np.full(150, 10.), prices(350)]
    strategy = MultiFactorStrategy()
    before = strategy.generate_signals(values[:150])
    after = strategy.generate_signals(values)
    assert np.count_nonzero(before) == 0
    np.testing.assert_array_equal(before, after[:150])


@pytest.mark.parametrize('weights', [
    {'gp_factor_6': 1.}, {'value': .5, 'gp_factor_6': .5},
])
def test_unknown_factor_does_not_become_a_zero_or_partial_backtest(weights):
    values = prices()
    rows = [{'date': str(date(2025, 1, 1) + timedelta(days=i)),
             'open': float(value), 'high': float(value * 1.01),
             'low': float(value * .99), 'close': float(value), 'volume': 1000000.}
            for i, value in enumerate(values)]
    with pytest.raises(ValueError, match='unsupported_factor_weights:gp_factor_6'):
        BacktestEngine.run_backtest('600690', rows, 'multi_factor', {
            'factor_weights': weights, 'factor_dsl': 'ts_rank(momentum_60d, 60)',
            'lookback': 60})


def test_supported_factor_weights_still_generate_measured_trades():
    values = prices()
    strategy = MultiFactorStrategy()
    strategy.set_parameters({'factor_weights': {'value': .4, 'quality': .3, 'growth': .3}})
    signals = strategy.generate_signals(values)
    assert set(signals) == {-1, 0, 1}
    assert not signals[:69].any()  # ten causal factor observations are required
