"""Target risk cannot borrow peer positions, weights, or evidence."""
from datetime import date, timedelta
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import numpy as np
import pytest

from strategy_factory.application import _panels_evaluation as panels
from strategy_factory.application.submitter import StrategySubmitter
from strategy_factory.application.submission_gate import runner


def contract(codes, weights):
    return {
        'version':'target_risk.v1', 'target_codes':codes, 'primary_layer':'target',
        'tested_object_hash':'object-one', 'current_tested_object_hash':'object-one',
        'allocation_weights':weights, 'allocation_source':'target_backtest_allocation_weights',
        'benchmark_code':'sh000300', 'missing_execution_fields':[],
        'execution_kwargs':{'initial_capital':100000., 'commission':.001, 'slippage':.0007,
            'execution_price_policy':'next_open', 'market_ruleset':'cn_equity',
            'sell_tax_rate':.001, 'min_trade_lot':100, 't_plus_one':True},
    }


def bars():
    return [{'date':str(date(2025,1,1)+timedelta(days=i)), 'open':float(10*np.exp(.001*i+.02*np.sin(i/5))),
             'close':float(10*np.exp(.001*i+.02*np.sin(i/5))), 'high':12., 'low':9., 'volume':10000000}
            for i in range(220)]


class Db:
    def __init__(self, missing=None): self.calls=[]; self.missing=missing
    async def get_klines(self, code, limit):
        self.calls.append(code)
        if code==self.missing: raise ValueError('missing target')
        return bars()


def test_report_params_carry_actual_allocation_and_costs():
    candidate={'strategy_type':'ma_cross','params':{},'target_symbols':['000001','000002'],
        'backtest_outcome':{'primary_layer':'target','layers':{'target':{'metrics':{'allocation_weights':{'000001':.8,'000002':.2}}}},
            'backtest_assumptions':{'initial_capital':123456.,'commission_rate':.0012,'slippage_bps':7.,'execution_price_policy':'next_close_legacy','market_ruleset':'cn_equity','sell_tax_rate':.001,'min_trade_lot':100,'t_plus_one':True}}}
    binding=StrategySubmitter._candidate_report_params(candidate)['target_risk_contract']
    assert binding['allocation_weights']=={'000001':.8,'000002':.2}
    assert binding['execution_kwargs']['commission']==.0012
    assert binding['execution_kwargs']['slippage']==.0007
    assert binding['execution_kwargs']['execution_price_policy']=='next_close_legacy'


def test_metrics_only_shared_cash_backtest_is_not_replayed_as_sleeves():
    candidate={'strategy_type':'ma_cross','params':{},'target_symbols':['000001','000002'],
               'backtest_metrics':{'primary_validation_layer':'target','portfolio_engine_used':True,
                                   'allocation_weights':{'000001':.5,'000002':.5}}}
    assert StrategySubmitter._candidate_report_params(candidate)['target_risk_contract']['portfolio_engine_used'] is True


@pytest.mark.asyncio
async def test_target_risk_uses_recorded_weights_and_never_reads_peers(monkeypatch):
    calls=[]
    def executed(stype,params,code,rows):
        calls.append((code,deepcopy(params)))
        growth=.002 if code=='000001' else -.001
        equity=100000*np.exp(growth*np.arange(len(rows)))
        return {'equity':equity,'quantity':100,'value':10000. if code=='000001' else 20000.}
    monkeypatch.setattr(panels,'_execution_panel_series',executed)
    db=Db();params={'target_risk_contract':contract(['000001','000002'],{'000001':.8,'000002':.2})}
    result=await panels._build_target_risk_panels('ma_cross',params,db)
    assert result['available']
    assert db.calls==['000001','000002','sh000300']
    expected=.8*100000*np.exp(.002*219)+.2*100000*np.exp(-.001*219)
    assert result['portfolio_value']==pytest.approx(expected)
    assert [h['value'] for h in result['holdings']]==[8000.,4000.]
    assert all(p['commission']==.001 and p['slippage']==.0007 and p['execution_price_policy']=='next_open' for _,p in calls)


@pytest.mark.asyncio
@pytest.mark.parametrize('change,missing,reason',[
    ({'allocation_weights':{}},None,'target_risk_allocation_missing_or_mismatched'),
    ({'allocation_weights':{'000001':1.}},None,'target_risk_allocation_missing_or_mismatched'),
    ({'execution_kwargs':{}},None,'target_risk_execution_assumptions_missing'),
    ({'current_tested_object_hash':'new-object'},None,'target_risk_tested_object_mismatch'),
    ({},'000002','target_risk_target_data_unavailable'),
])
async def test_missing_binding_or_target_is_not_reweighted(change,missing,reason):
    binding=contract(['000001','000002'],{'000001':.8,'000002':.2});binding.update(change)
    result=await panels._build_target_risk_panels('ma_cross',{'target_risk_contract':binding},Db(missing))
    assert result['available'] is False
    assert result['reason']==reason
    assert 'returns' not in result


@pytest.mark.asyncio
async def test_peer_exposure_cannot_fill_an_empty_target_book(monkeypatch):
    async def target(*args):
        return {'available':True,'target_codes':['000001'],'used_codes':['000001'],'allocation_weights':{'000001':1.},'portfolio_value':100000.,'returns':np.zeros(120),'holdings':[]}
    async def peer(*args):
        return {'strategy_returns':np.ones(120)*.001,'portfolio_value':100000.,'holdings':[{'code':'600519','value':80000.,'beta':1.}],'used_codes':['600519']}
    monkeypatch.setattr(panels,'_build_target_risk_panels',target);monkeypatch.setattr(panels,'_build_strategy_panels',peer)
    report=await panels._run_risk_report('ma_cross',{},None)
    assert report['stress_loss_percent'] is None and report['var_percent'] is None
    assert report['stress_reason']=='no_positions'
    assert report['supplemental_peer_risk']['stress_loss_percent']==-16.
    assert report['supplemental_peer_risk']['binding']=='advisory_only'


@pytest.mark.asyncio
async def test_failed_peer_diagnostic_keeps_target_failure_contract(monkeypatch):
    monkeypatch.setattr(panels,'_build_target_risk_panels',AsyncMock(return_value={'available':False,'reason':'target_risk_target_data_unavailable','target_codes':['000001']}))
    monkeypatch.setattr(panels,'_build_strategy_panels',AsyncMock(side_effect=RuntimeError('peer unavailable')))
    report=await panels._run_risk_report('ma_cross',{},None)
    assert report['target_risk_binding']['reason']=='target_risk_target_data_unavailable'
    assert report['stress_loss_percent'] is None
    assert report['supplemental_peer_risk']['reason']=='supplemental_peer_risk_unavailable'


@pytest.mark.asyncio
async def test_partial_target_sensitivity_is_missing_not_partial_loss(monkeypatch):
    monkeypatch.setattr(panels,'_build_target_risk_panels',AsyncMock(return_value={'available':True,'target_codes':['000001','000002'],'used_codes':['000001','000002'],'allocation_weights':{'000001':.5,'000002':.5},'portfolio_value':100000.,'returns':np.ones(120)*.001,'holdings':[{'code':'000001','value':10000.,'beta':1.},{'code':'000002','value':10000.}]}))
    monkeypatch.setattr(panels,'_build_strategy_panels',AsyncMock(return_value={}))
    report=await panels._run_risk_report('ma_cross',{},None)
    assert report['stress_loss_percent'] is None
    assert report['stress_reason']=='target_risk_incomplete_sensitivity'
    assert report['target_risk_binding']['status']=='missing'


@pytest.mark.asyncio
@pytest.mark.parametrize('failure',[None,RuntimeError('risk failed')])
async def test_real_submitter_risk_gateway_failure_keeps_binding(monkeypatch,failure):
    subject=StrategySubmitter()
    risk=AsyncMock(side_effect=failure) if failure else AsyncMock(return_value=None)
    monkeypatch.setattr(subject,'_get_risk_gateway',lambda:SimpleNamespace(run_risk_report=risk))
    monkeypatch.setattr(subject,'_get_validation_gateway',lambda:SimpleNamespace(run_validation_report=AsyncMock(return_value=None)))
    candidate={'strategy_type':'ma_cross','target_symbols':['000001'],'params':{}}
    _,report=await subject._evaluate_reports(candidate,None)
    assert report is not None and report['stress_loss_percent'] is None
    assert report['target_risk_binding']['reason']==('target_risk_report_failed' if failure else 'target_risk_report_missing')
    assert 'missing_stress_loss_percent' in runner._target_risk_binding_reasons(candidate,report)


@pytest.mark.parametrize('profile',['trade_rule_validation','factor_rank_validation'])
def test_target_risk_missing_blocks_both_admission_profiles(monkeypatch,profile):
    monkeypatch.setattr(runner,'_evaluate_trade_profile_for_admission',lambda *a,**kw:{'passed':True,'reasons':[]})
    monkeypatch.setattr(runner,'_evaluate_statistical_admission',lambda *a,**kw:{'passed':True,'reasons':[]})
    monkeypatch.setattr(runner,'_review_stage_blockers',lambda *a,**kw:([],{}))
    strategy={'strategy_type':'ma_cross','target_symbols':['000001'],'params':{}}
    risk={'stress_loss_percent':None,'var_percent':None,'target_risk_binding':{'status':'measured','scope':'actual_target_portfolio','target_codes':['000001'],'used_target_codes':['000001'],'allocation_weights':{'000001':1.}}}
    result=runner._attach_admission_evaluations(strategy,{'profile':profile},{'passed':True},risk_report=risk)
    for stage in ['incubation','live']:
        evaluation=result['admission_evaluations'][stage]
        assert evaluation['passed'] is False
        assert 'missing_stress_loss_percent' in evaluation['reasons']
        assert 'missing_var_percent' in evaluation['reasons']
    assert runner._target_risk_binding_reasons(strategy,{'stress_loss_percent':-2.})==[]


@pytest.mark.parametrize('field,value,reason',[
    ('tested_object_hash','other-object','target_risk_tested_object_mismatch'),
    ('allocation_weights',{'000001':.2,'000002':.8},'target_risk_allocation_mismatch'),
    ('execution_policy','next_close_legacy','target_risk_execution_assumptions_mismatch'),
    ('commission',.02,'target_risk_execution_assumptions_mismatch'),
    ('market_impact_bps',50.,'target_risk_execution_assumptions_mismatch'),
])
def test_measured_risk_cannot_be_reused_for_a_different_object(field,value,reason):
    assumptions={'initial_capital':100000.,'commission_rate':.001,'slippage_bps':7.,'execution_price_policy':'next_open','market_ruleset':'cn_equity','sell_tax_rate':.001,'min_trade_lot':100,'t_plus_one':True,'market_impact_bps':5.}
    strategy={'tested_object_hash':'object-one','target_symbols':['000001','000002'],'backtest_outcome':{'primary_layer':'target','layers':{'target':{'metrics':{'allocation_weights':{'000001':.8,'000002':.2}}}},'backtest_assumptions':assumptions}}
    binding={'contract_version':'target_risk.v1','status':'measured','scope':'actual_target_portfolio','target_codes':['000001','000002'],'used_target_codes':['000001','000002'],'allocation_weights':{'000001':.8,'000002':.2},'tested_object_hash':'object-one','execution_kwargs':{**assumptions,'commission':.001,'slippage':.0007}}
    risk={'stress_loss_percent':-2.,'var_percent':1.,'stress_measurable':True,'var_measurable':True,'target_risk_binding':binding}
    assert runner._target_risk_binding_reasons(strategy,risk)==[]
    if field=='execution_policy':binding['execution_kwargs']['execution_price_policy']=value
    elif field in ['commission','market_impact_bps']:binding['execution_kwargs'][field]=value
    else:binding[field]=value
    assert reason in runner._target_risk_binding_reasons(strategy,risk)


@pytest.mark.parametrize('profile',['trade_rule_validation','factor_rank_validation'])
def test_submitter_data_shape_uses_explicit_backtest_metrics_for_binding(monkeypatch,profile):
    monkeypatch.setattr(runner,'_evaluate_trade_profile_for_admission',lambda *a,**kw:{'passed':True,'reasons':[]})
    monkeypatch.setattr(runner,'_evaluate_statistical_admission',lambda *a,**kw:{'passed':True,'reasons':[]})
    monkeypatch.setattr(runner,'_review_stage_blockers',lambda *a,**kw:([],{}))
    assumptions={'initial_capital':100000.,'commission_rate':.001,'slippage_bps':7.,'execution_price_policy':'next_open','market_ruleset':'cn_equity','sell_tax_rate':.001,'min_trade_lot':100,'t_plus_one':True}
    metrics={'primary_validation_layer':'target','allocation_weights':{'000001':.8,'000002':.2},'backtest_assumptions':assumptions}
    # This is the production submission shape: metrics are a separate argument,
    # while the persisted strategy contains target/hash in params.
    data={'strategy_type':'ma_cross','params':{'target_symbols':['000001','000002'],'tested_object_hash':'object-one'}}
    binding={'contract_version':'target_risk.v1','status':'measured','scope':'actual_target_portfolio','target_codes':['000001','000002'],'used_target_codes':['000001','000002'],'allocation_weights':{'000001':.8,'000002':.2},'tested_object_hash':'object-one','execution_kwargs':{**assumptions,'commission':.001,'slippage':.0007}}
    risk={'stress_loss_percent':-2.,'var_percent':1.,'stress_measurable':True,'var_measurable':True,'target_risk_binding':binding}
    assert runner._target_risk_binding_reasons(data,risk,metrics)==[]
    result=runner._attach_admission_evaluations(data,{'profile':profile},{'passed':True},risk_report=risk,backtest_metrics=metrics)
    for stage in ['incubation','live']:
        assert not any('target_risk' in reason for reason in result['admission_evaluations'][stage]['reasons'])
