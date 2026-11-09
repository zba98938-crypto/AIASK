import asyncio, json, os
from datetime import date
from unittest.mock import AsyncMock
from types import SimpleNamespace
from aiask_quant_core.testing.hermetic import install
install()
os.environ['STRATEGY_FACTORY_FORWARD_DAYS']='1,5,10,20,40'
from akshare_mcp.services.signal_tracker import FORWARD_DAYS
from strategy_factory.application.signal_tracker.orchestrator import SignalTrackerOrchestrator

async def main():
    p=SimpleNamespace(get_db=AsyncMock(return_value=object()),phase_timeout_seconds=lambda *_:1,get_default_universe=lambda:[])
    for n in ('load_executable_strategies','load_runtime_submitted_strategies','load_runtime_observation_strategies'):
        setattr(p,n,AsyncMock(return_value=[]))
    for n in ('run_runtime_risk_scan','run_lifecycle_scan','reconcile_vector_registry','snapshot_domain_projections'):
        setattr(p,n,AsyncMock(return_value={}))
    p.backfill_forward_returns=AsyncMock(return_value={'computed':42,'windows':{}})
    result=await SignalTrackerOrchestrator(p).run_cycle(as_of=date(2026,10,30))
    print(json.dumps({'configured_forward_days':FORWARD_DAYS,'actual_forward_days':p.backfill_forward_returns.call_args.kwargs['forward_days_list'],'actual_computed':42,'reported_forward_returns_computed':result['forward_returns_computed'],'errors':result['errors']},indent=2))
asyncio.run(main())
