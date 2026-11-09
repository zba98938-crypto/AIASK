import asyncio, importlib.util, json, os, runpy, sqlite3, sys, tempfile
from pathlib import Path
ROOT=Path('/Users/mac/Desktop/张勇/AIASK')
runpy.run_path(str(ROOT/'conftest.py'))
from aiask_quant_core.storage import get_db, close_db
from aiask_agent.factory_dispatcher import _mark_launch_failed
from aiask_agent.factory_worker import _last_json, _outcome
from strategy_factory.infrastructure.factory_schema import register_factory_schema
register_factory_schema()

def load(name, rel):
 spec=importlib.util.spec_from_file_location(name,ROOT/rel)
 mod=importlib.util.module_from_spec(spec);sys.modules[name]=mod;spec.loader.exec_module(mod);return mod

async def main():
 db=get_db();await db.initialize()
 run=await db.request_factory_run({'factory':'strategy_factory','idempotency_key':'audit-startup-failure'})
 await _mark_launch_failed(db,run['run_id'],command=['python','fake.py'],started_at='2026-11-04T00:00:00+00:00',failure_kind='worker_start_failed',error='synthetic import crash',stderr_tail='ModuleNotFoundError',exit_code=1)
 current=await db.get_factory_run(run['run_id'])
 print('STARTUP_FAILURE',json.dumps({k:current.get(k) for k in ['status','phase','failure_kind','error','stderr_tail']}))
 await close_db()
 payload=json.dumps({'status':'failed','error':'upstream failed','results':[{'candidate_id':str(i),'description':'x'*100} for i in range(1000)]})
 parsed=_last_json(payload[-65536:])
 print('TRUNCATED_RESULT',json.dumps({'original_bytes':len(payload),'parsed_keys':list(parsed),'actual_outcome':_outcome(parsed,0),'expected_status':'failed'}))
 supervisor=load('audit_supervisor','scripts/factories/run_three_factories.py')
 state=supervisor.FactoryState(name='synthetic')
 spec=supervisor.FactorySpec(name='synthetic',command=(sys.executable,'-c',"print('x'*70000)"),log_name='synthetic.log')
 try:
  await asyncio.wait_for(supervisor._run_factory(spec,env=os.environ.copy(),log_dir=Path(tempfile.mkdtemp(prefix='aiask-audit-supervisor-')),restart_delay_sec=1,restart=True,quiet=True,stop_event=asyncio.Event(),state=state),timeout=5)
 except Exception as e:
  print('SUPERVISOR_LONG_LINE',json.dumps({'error_type':type(e).__name__,'error':str(e),'status':state.status,'restart_count':state.restart_count}))
asyncio.run(main())
diag=load('audit_diag','scripts/factories/diagnose_factory_health.py')
conn=sqlite3.connect(':memory:')
class FakeDb:
 connection=conn
 async def initialize(self): print('DIAGNOSTIC_CALLED_GLOBAL_INITIALIZE',True)
diag.get_db=lambda: FakeDb();diag.StrategyLifecycleLedger=None
obj=diag.FactoryHealthDiagnostics(db_path='/tmp/explicit-audit-db.sqlite3')
print('DIAGNOSTIC_IGNORES_PATH',obj._connect_db() and obj.conn is conn)
conn.close()
