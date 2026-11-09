"""Evidence consumed by target risk must survive the real report boundary."""
from copy import deepcopy
from datetime import date, timedelta

import numpy as np
import pytest

from strategy_factory.application.backtest_filter import BacktestFilter
from strategy_factory.application.candidate_contract import apply_resolved_candidate_envelope
from strategy_factory.application import _panels_evaluation as panels
from strategy_factory.application.submitter import StrategySubmitter
from strategy_factory.application.submission_gate import runner


def attach_real_execution_contract():
    candidate = apply_resolved_candidate_envelope({
        'strategy_type': 'ma_cross', 'params': {'short_period': 5, 'long_period': 20},
        'target_symbols': ['600690']})
    assumptions = BacktestFilter._build_backtest_assumptions(candidate).to_audit_dict()
    metrics = {'sharpe_ratio': .4, 'total_return': .06, 'max_drawdown': .1,
               'trade_count': 8, 'allocation_weights': {'600690': 1.0}}
    result = {'passed': False, 'reason_code': 'weak_sharpe', 'primary_layer': 'target',
              'primary_validation_layer': 'target', 'tested_object_hash': candidate['tested_object_hash'],
              'target_codes': ['600690'], 'backtest_assumptions': assumptions,
              'metrics': metrics, 'layers': {'target': {'metrics': metrics}}}
    before = deepcopy(result)
    passed, failed = [], []
    BacktestFilter()._apply_result_to_candidate(candidate, result, passed, failed)
    assert result == before
    assert passed == [] and failed == [candidate]
    return candidate, assumptions


def test_execution_contract_survives_backtest_attachment_and_gate_shape():
    candidate, assumptions = attach_real_execution_contract()
    # This is the engine's actual audit DTO, including rules near its end and
    # explicitly missing optional inputs; dictionary order is not evidence.
    assert candidate['backtest_outcome']['backtest_assumptions'] == assumptions
    assert candidate['backtest_metrics']['backtest_assumptions'] == assumptions
    binding = StrategySubmitter._candidate_report_params(candidate)['target_risk_contract']
    assert binding['missing_execution_fields'] == []
    assert all(binding['execution_kwargs'][key] == value for key, value in assumptions.items())
    assert binding['execution_kwargs']['commission'] == assumptions['commission_rate']
    assert binding['execution_kwargs']['slippage'] == assumptions['slippage_bps'] / 10000
    measured = {'stress_loss_percent': -2., 'var_percent': 1., 'stress_measurable': True,
                'var_measurable': True, 'target_risk_binding': {
                    'status': 'measured', 'scope': 'actual_target_portfolio',
                    'target_codes': ['600690'], 'used_target_codes': ['600690'],
                    'allocation_weights': {'600690': 1.0},
                    'tested_object_hash': candidate['tested_object_hash'],
                    'execution_kwargs': binding['execution_kwargs']}}
    assert runner._target_risk_binding_reasons(candidate, measured) == []
    data = {'strategy_type': 'ma_cross', 'params': {
        'tested_object_hash': candidate['tested_object_hash'], 'target_symbols': ['600690']}}
    assert runner._target_risk_binding_reasons(data, measured, candidate['backtest_metrics']) == []
    measured['target_risk_binding']['execution_kwargs']['sell_tax_rate'] = .25
    assert 'target_risk_execution_assumptions_mismatch' in runner._target_risk_binding_reasons(
        data, measured, candidate['backtest_metrics'])


@pytest.mark.asyncio
async def test_attached_contract_reaches_target_replay_without_defaulting_missing_costs(monkeypatch):
    candidate, assumptions = attach_real_execution_contract()
    calls = []
    dates = [str(date(2025, 1, 1) + timedelta(days=i)) for i in range(220)]

    class Db:
        async def get_klines(self, code, limit):
            return [{'date': day, 'open': 10. + i / 100, 'close': 10. + i / 100,
                     'high': 13., 'low': 9., 'volume': 1000000.}
                    for i, day in enumerate(dates)]

    def executed(strategy_type, params, code, rows):
        calls.append((code, deepcopy(params)))
        return {'equity': 100000. * np.exp(.001 * np.arange(len(rows))),
                'quantity': 100, 'value': 11000.}

    monkeypatch.setattr(panels, '_execution_panel_series', executed)
    params = StrategySubmitter._candidate_report_params(candidate)
    replay = await panels._build_target_risk_panels('ma_cross', params, Db())
    assert replay['available'] is True, replay.get('reason')
    assert [code for code, _ in calls] == ['600690']
    assert all(calls[0][1][key] == value for key, value in assumptions.items())
    # A genuinely absent required assumption still blocks before any replay.
    candidate['backtest_outcome']['backtest_assumptions']['sell_tax_rate'] = None
    params = StrategySubmitter._candidate_report_params(candidate)
    missing = await panels._build_target_risk_panels('ma_cross', params, Db())
    assert missing['available'] is False
    assert missing['reason'] == 'target_risk_execution_assumptions_missing'
    assert len(calls) == 1
