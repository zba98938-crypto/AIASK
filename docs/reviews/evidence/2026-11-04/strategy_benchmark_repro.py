exec(open('/tmp/aiask_strategy_audit_repro_20261104.py').read().split('asyncio.run(main())')[0])
from strategy_factory.application import backtest_filter as bfmod

class Engine:
    def __init__(self, benchmark): self.benchmark=benchmark
    def run_backtest(self, code, klines, strategy_type, params, return_trades):
        return {'success': True, 'data': {'code':code, 'sharpe_ratio':0.1, 'total_return':0.01, 'trades_count':12, 'max_drawdown':0.08, 'win_rate':0.5, 'benchmark_return':self.benchmark, 'buy_hold_return':self.benchmark}}

async def compare():
    outputs=[]
    for benchmark in (-0.3, 0.3):
        bf=BacktestFilter()
        for code in ['000001','000002','000003']: bf._kline_cache[code]=await DB().get_klines(code)
        with patch.object(bfmod,'_get_strategy_factory_package',return_value=SimpleNamespace(asyncio=asyncio)), patch.object(bf,'_resolve_backtest_plan',return_value=(['000001','000002','000003'], [], ['000001','000002','000003'],'representative_only','target_plus_representative')), patch.object(bf,'_run_portfolio_engine_summary',new=AsyncMock(return_value={})), patch.object(bf,'_run_parameter_perturbation_study',new=AsyncMock(return_value={})), patch.object(bf,'_get_thresholds',return_value={'sharpe_min':0.5,'trades_min':3,'mdd_max':0.3,'min_samples':1}):
            result=await bf._test_one({'strategy_type':'ma_cross','params':{'short_period':5,'long_period':20}},DB(),Engine(benchmark))
        outputs.append({'input_benchmark_return':benchmark,'passed':result['passed'],'regime':result['metrics'].get('adaptive_gate_2',{}).get('regime'),'adaptive_gate_2':result['metrics'].get('adaptive_gate_2')})
    print(json.dumps(outputs,ensure_ascii=False,indent=2))
asyncio.run(compare())
