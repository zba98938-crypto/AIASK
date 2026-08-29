"""Verify the exit pathway fix."""
import asyncio
import sys
sys.path.insert(0, 'packages/akshare-mcp/src')
sys.path.insert(0, 'packages/strategy-factory/src')

from akshare_mcp.services.incubation_factory.runner import IncubationFactoryRunner
from strategy_factory.db.connection import get_connection

async def test_run():
    db = await get_connection()
    runner = IncubationFactoryRunner(dry_run=True)
    result = await runner.run(db)

    exit_signal_result = result.get('exit_signal_paper_execution', {})
    stale_closure_result = result.get('stale_paper_position_closure', {})

    print('=== PHASE 3c2: EXIT SIGNAL EXECUTION ===')
    print(f"  Status: {exit_signal_result.get('status')}")
    print(f"  Selected strategies: {exit_signal_result.get('selected_count', 0)}")
    print(f"  Exit orders created: {exit_signal_result.get('exit_orders_created', 0)}")
    print(f"  Positions closed: {exit_signal_result.get('positions_closed', 0)}")

    print('\n=== PHASE 3d: STALE POSITION CLOSURE ===')
    print(f"  Status: {stale_closure_result.get('status')}")
    print(f"  Evaluated: {stale_closure_result.get('evaluated', 0)}")
    print(f"  Closed: {stale_closure_result.get('closed', 0)}")

    await db.close()

asyncio.run(test_run())
