import importlib.util
from pathlib import Path
spec = importlib.util.spec_from_file_location('_audit_hermetic', Path('/Users/mac/Desktop/张勇/AIASK/packages/aiask-quant-core/src/aiask_quant_core/testing/hermetic.py'))
hermetic = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hermetic)
hermetic.install()
import asyncio
import json
from datetime import date, timedelta
from unittest.mock import patch, AsyncMock
from types import SimpleNamespace
import numpy as np
from aiask_quant_core.backtest import StrategyRegistry
from strategy_factory.application import _panels_evaluation as pe
from strategy_factory.application.backtest_filter import BacktestFilter
from strategy_factory.application.submission_gate import runner as sg

class DB:
    def __init__(self, dates=None, failed=None):
        self.dates = dates or {}
        self.failed = failed
    async def get_klines(self, code, limit=220):
        if code == self.failed:
            raise RuntimeError('synthetic unavailable symbol')
        start = self.dates.get(code, date(2025, 1, 1))
        return [{'date': str(start + timedelta(days=i)), 'close': 10*(1.001**i), 'volume': 100000} for i in range(220)]

def selection(*args, **kwargs):
    return {'sample_codes': ['A', 'B', 'C'], 'requested_sample_size': 3, 'effective_sample_size': 3}

async def main():
    out = {}
    with patch.object(pe, 'get_strategy_registry', return_value=StrategyRegistry), patch.object(pe, 'get_normalize_klines', return_value=lambda rows: rows), patch.object(pe, '_resolve_strategy_sample_selection', side_effect=selection):
        panels = await pe._build_strategy_panels('buy_and_hold', {}, DB())
        out['buy_hold_panel'] = {'bars': len(panels['strategy_returns']), 'nonzero_return_days': int(np.count_nonzero(panels['strategy_returns'])), 'reported_holdings': panels['holdings'], 'actual_last120_buy_hold_return': round(1.001**120-1, 6)}
        shifted = await pe._build_strategy_panels('buy_and_hold', {}, DB({'A':date(2025,1,1),'B':date(2023,1,1),'C':date(2021,1,1)}))
        out['disjoint_date_panel'] = {'shape': list(shifted['return_panel'].shape), 'used': shifted['panel_coverage']['used'], 'has_dates': any('date' in key for key in shifted), 'common_dates': 0}
    validation = await sg._run_statistical_gate(None, {'strategy_type':'value_factor', 'params':{}}, profile={'profile':'factor_rank_validation'}, validation_report={'walk_forward':{'oos_rank_ic_ir':0.35}, 'purged_kfold':{'oos_rank_ic_mean':0.03}, 'bootstrap_ci':{'ci_lower':0.02}}, backtest_metrics={'parameter_perturbation_trade_stability':1.0})
    out['perfect_stability'] = {'passed':validation.get('passed'),'param_sensitivity':validation.get('param_sensitivity'),'reasons':validation.get('reasons'), 'source':validation.get('metric_source_audit',{}).get('param_sensitivity')}
    derived = BacktestFilter._derive_trade_validation_metrics({'strategy_type':'ma_cross','params':{}}, {'metrics':{'total_return':0.42}, 'layers':{'target':{'metrics':{'total_return':0.42}}}})
    out['oos_alias'] = {'full_period_total_return':0.42, 'target_layer_oos_return':derived['target_layer_oos_return']}
    print(json.dumps(out, ensure_ascii=False, indent=2))
asyncio.run(main())
