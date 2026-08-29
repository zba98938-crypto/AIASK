"""Run incubation factory with fix verification."""
import asyncio
import sys
import json
from datetime import datetime

# Add paths
sys.path.insert(0, 'packages/akshare-mcp/src')
sys.path.insert(0, 'packages/strategy-factory/src')

async def run_factory():
    from akshare_mcp.services.incubation_factory.runner import IncubationFactoryRunner
    from strategy_factory.db.connection import get_connection

    print(f"=== STARTING INCUBATION FACTORY RUN ===")
    print(f"Time: {datetime.now().isoformat()}")
    print(f"Dry run: False (REAL RUN)\n")

    db = await get_connection()

    # Run factory
    runner = IncubationFactoryRunner(dry_run=False)
    result = await runner.run(db)

    # Extract key metrics
    print("\n=== PHASE 2: STRATEGY LOADING ===")
    incubating_count = result.get('strategy_counts', {}).get('incubating', 0)
    paper_count = result.get('strategy_counts', {}).get('paper_observation', 0)
    diagnostic_count = result.get('strategy_counts', {}).get('diagnostic_observation', 0)
    print(f"  Incubating: {incubating_count}")
    print(f"  Paper observation: {paper_count}")
    print(f"  Diagnostic observation: {diagnostic_count}")
    print(f"  Total (all_strategies): {incubating_count + paper_count + diagnostic_count}")

    print("\n=== PHASE 3: SIGNAL GENERATION ===")
    print(f"  Signals generated: {result.get('signals_generated', 0)}")
    print(f"  Orders filled: {result.get('orders_filled', 0)}")
    print(f"  Orders rejected: {result.get('orders_rejected', 0)}")

    print("\n=== PHASE 3c2: EXIT SIGNAL PAPER EXECUTION ===")
    exit_result = result.get('exit_signal_paper_execution', {})
    print(f"  Status: {exit_result.get('status')}")
    print(f"  Selected count: {exit_result.get('selected_count', 0)}")
    print(f"  Exit backlog: {exit_result.get('exit_signal_backlog_count', 0)}")
    print(f"  Exit orders created: {exit_result.get('exit_orders_created', 0)}")
    print(f"  Exit orders filled: {exit_result.get('exit_orders_filled', 0)}")
    print(f"  Positions closed: {exit_result.get('positions_closed', 0)}")
    if exit_result.get('items'):
        print(f"  Strategies processed: {len(exit_result['items'])}")

    print("\n=== PHASE 3d: STALE POSITION CLOSURE ===")
    stale_result = result.get('stale_paper_position_closure', {})
    print(f"  Status: {stale_result.get('status')}")
    print(f"  Evaluated: {stale_result.get('evaluated', 0)}")
    print(f"  Closed: {stale_result.get('closed', 0)}")
    print(f"  Orders created: {stale_result.get('orders_created', 0)}")

    # Save full result to file for analysis
    with open('factory_run_result.json', 'w', encoding='utf-8') as f:
        json.dump(result, f, indent=2, default=str, ensure_ascii=False)
    print("\n=== Full result saved to factory_run_result.json ===")

    await db.close()
    print("\n=== RUN COMPLETE ===")

if __name__ == '__main__':
    try:
        asyncio.run(run_factory())
    except KeyboardInterrupt:
        print("\n\nRun interrupted by user")
        sys.exit(1)
    except Exception as e:
        print(f"\n\nERROR: {type(e).__name__}: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
