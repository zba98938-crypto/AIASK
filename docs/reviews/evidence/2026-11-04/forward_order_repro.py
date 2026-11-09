import asyncio, json, tempfile
from pathlib import Path
from datetime import date, timedelta
from aiask_quant_core.testing.hermetic import install
install()
from aiask_quant_core.storage.sqlite import SQLiteAdapter
from akshare_mcp.services.incubation_factory.forward_verifier import ForwardVerifier

async def main():
    with tempfile.TemporaryDirectory(prefix='aiask-audit-forward-') as t:
        db=SQLiteAdapter(path=Path(t)/'probe.sqlite3')
        await db.initialize()
        try:
            for i in range(60):
                day=(date(2026,7,1)+timedelta(days=i)).isoformat()
                ret=.02 if i<40 else -.02
                await db.save_strategy_signal_evidence({'signal_id':str(i),'evidence_id':'e1','strategy_id':'audit','signal_date':day,'direction':'long','forward_returns':{'5':ret,'10':ret}})
            rows=await db.list_strategy_signal_evidence(strategy_id='audit',limit=500)
            result=await ForwardVerifier().verify(db,{'id':'audit','strategy_type':'momentum'})
            print(json.dumps({'first_date':rows[0]['signal_date'],'last_date':rows[-1]['signal_date'],'actual_latest_20_hit_rate':0.0,'reported_recent_primary_hit_rate':result['recent_primary_hit_rate'],'reported_recent_primary_skill_lcb':result['recent_primary_skill_lcb'],'primary_effective_n':result['primary_effective_n'],'primary_hit_rate':result['primary_hit_rate']},indent=2))
        finally:
            await db.close()
asyncio.run(main())
