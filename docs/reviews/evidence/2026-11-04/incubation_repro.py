import asyncio, json, os
from types import SimpleNamespace
from unittest.mock import AsyncMock
from aiask_quant_core.testing.hermetic import install
install()
from akshare_mcp.integration.strategy_factory_support import IncubationFactoryRunnerAdapter
from strategy_factory.runtime.incubation import build_incubation_runtime

for name in ('INCUBATION_DAILY_PREDICTIONS_ENABLED','INCUBATION_HORIZON_SCORING_ENABLED','INCUBATION_CALIBRATION_ENABLED','INCUBATION_SELECTIVE_GATE_ENABLED'):
    os.environ[name]='0'

def make_runner(fail_signal=False):
    r=SimpleNamespace(dry_run=True, run_time=None)
    r._get_db=AsyncMock(return_value=object())
    r._intake=SimpleNamespace(scan_and_accept=AsyncMock(return_value={'accepted':1}))
    r._signal_generator=SimpleNamespace(generate=AsyncMock(side_effect=RuntimeError('signal generation failed')) if fail_signal else AsyncMock(return_value={'signals_generated':1,'orders_created':1}))
    r._forward_verifier=SimpleNamespace(verify=AsyncMock(return_value={'verified':1}))
    r._metrics_recorder=SimpleNamespace(record=AsyncMock(return_value={'metrics_recorded':1}))
    r._trade_prediction_verifier=SimpleNamespace(verify_pending=AsyncMock(return_value={'verified':1}))
    r._reporter=SimpleNamespace(generate=AsyncMock(return_value={}))
    r._feedback_writer=SimpleNamespace(write=AsyncMock(return_value={}))
    r._accelerator=SimpleNamespace(evaluate_batch=AsyncMock(return_value={}))
    r._alert_monitor=SimpleNamespace(check=AsyncMock(return_value={}))
    r._list_incubating=AsyncMock(return_value=[{'id':'s1'}])
    r._list_paper_observation=AsyncMock(return_value=[])
    r._list_diagnostic_observation=AsyncMock(return_value=[])
    for name in ('_run_recompile_remediation','_run_rejected_strategy_position_cleanup','_run_stale_paper_position_closure','_run_execution_audit_acceptance','_run_pipeline','_start_paper_trading_daemons','_stop_paper_trading_daemons'):
        setattr(r,name,AsyncMock(return_value={}))
    r._settle_strategy_orders=AsyncMock(return_value={'filled_count':1})
    return r

async def run_case(explicit_dry_run=None, fail_signal=False):
    r=make_runner(fail_signal)
    rt=build_incubation_runtime(support=IncubationFactoryRunnerAdapter(r),dry_run=True)
    args={} if explicit_dry_run is None else {'dry_run':explicit_dry_run}
    result=await rt.run_once(**args)
    return {'explicit_dry_run':explicit_dry_run,'fail_signal':fail_signal,'result_status':result['status'],'phase_failures':result['phase_failures'],'phase_names':[x['phase_name'] for x in result['phases']], 'calls':{'intake':r._intake.scan_and_accept.await_count,'signal_order_generation':r._signal_generator.generate.await_count,'settlement':r._settle_strategy_orders.await_count,'metrics':r._metrics_recorder.record.await_count,'trade_prediction_verifier':r._trade_prediction_verifier.verify_pending.await_count,'pipeline':r._run_pipeline.await_count,'report':r._reporter.generate.await_count,'feedback':r._feedback_writer.write.await_count,'acceleration':r._accelerator.evaluate_batch.await_count}}

async def main():
    out=[]
    for args in ({},{'explicit_dry_run':True},{'fail_signal':True}):
        out.append(await run_case(**args))
    print(json.dumps(out,indent=2))
asyncio.run(main())
