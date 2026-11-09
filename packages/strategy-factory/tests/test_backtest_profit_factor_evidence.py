"""An undefined loss denominator must survive contract and admission reads."""
from types import SimpleNamespace
from unittest.mock import AsyncMock
import asyncio

import pytest

from strategy_factory.application import backtest_filter as module
from strategy_factory.application.backtest_filter import BacktestFilter
from strategy_factory.application.submission_gate import runner


@pytest.mark.parametrize('pnl,expected,reason', [
    ([100.0, 200.0], None, 'missing_profit_factor'),
    ([-100.0, -200.0], 0.0, 'profit_factor 0.000 < 1.800'),
    ([200.0, -100.0], 2.0, None),
])
def test_round_trip_profit_factor_preserves_missing_and_real_zero_through_admission(pnl, expected, reason):
    profile = BacktestFilter._trade_profile_from_round_trips([
        {'status':'closed', 'realized_pnl':value, 'hold_days':3} for value in pnl
    ])
    candidate = {'strategy_type':'ma_cross', 'target_symbols':['600519'], 'params':{}}
    metrics = {'sharpe_ratio':1.0, 'total_return':.1, 'max_drawdown':.1, **profile}
    contract = BacktestFilter._build_backtest_metrics_contract(candidate, {
        'metrics':metrics, 'layers':{'target':{'metrics':metrics}},
        'primary_layer':'target', 'target_codes':['600519'],
        'validation_focus':'candidate_target_only', 'code_source':'candidate_target_symbols',
    })
    assert profile['profit_factor'] == expected
    assert contract['profit_factor'] == expected
    materialized = runner._materialize_backtest_metrics_contract({**metrics, 'backtest_metrics_contract':contract})
    assert materialized['profit_factor'] == expected
    gate = runner._evaluate_trade_profile(candidate, {'profile':'trade_rule_validation'}, materialized, {}, admission_level='incubation')
    assert gate['profit_factor'] == expected
    pf_reasons = [item for item in gate['reasons'] if 'profit_factor' in item]
    assert pf_reasons == ([reason] if reason else [])
    if reason:
        assert gate['passed'] is False


@pytest.mark.parametrize('value', [None, float('nan'), float('inf'), 'invalid'])
def test_unmeasurable_primary_ratio_never_borrows_secondary_ratio(value):
    contract = BacktestFilter._build_backtest_metrics_contract({}, {
        'primary_layer':'target', 'layers':{'target':{'metrics':{'profit_factor':value}}},
        'metrics':{'profit_factor':8.0},
    })
    assert contract['profit_factor'] is None


@pytest.mark.asyncio
async def test_adaptive_audit_preserves_missing_ratio_without_awarding_points(monkeypatch):
    from datetime import date, timedelta

    class Db:
        async def get_klines(self, *args, **kwargs):
            return [{'date':str(date(2025, 1, 1) + timedelta(days=i)), 'close':10.0, 'open':10.0, 'high':10.0, 'low':10.0, 'volume':10000.0} for i in range(180)]

    class Engine:
        def run_backtest(self, *args):
            return {'success':True, 'data':{'code':'600519', 'sharpe_ratio':.1, 'total_return':.01, 'trades_count':1, 'max_drawdown':.08, 'win_rate':1.0, 'profit_factor':None}}

    subject = BacktestFilter()
    monkeypatch.setattr(module, '_get_strategy_factory_package', lambda: SimpleNamespace(asyncio=asyncio))
    monkeypatch.setattr(subject, '_resolve_backtest_plan', lambda c: (['600519'], [], ['600519'], 'candidate_target_symbols', 'candidate_target_only'))
    monkeypatch.setattr(subject, '_run_portfolio_engine_summary', AsyncMock(return_value={}))
    monkeypatch.setattr(subject, '_run_parameter_perturbation_study', AsyncMock(return_value={}))
    monkeypatch.setattr(subject, '_get_thresholds', lambda *a: {'sharpe_min':.5, 'trades_min':3, 'mdd_max':.3, 'min_samples':1})
    result = await subject._test_one({'strategy_type':'ma_cross','params':{},'target_symbols':['600519']}, Db(), Engine())
    assert result['metrics']['adaptive_gate_2']['profit_factor'] is None
    assert result['metrics']['adaptive_gate_2']['quality_score'] == 10.0  # win rate only
    assert result['backtest_metrics_contract']['profit_factor'] is None
    assert result['passed'] is False
