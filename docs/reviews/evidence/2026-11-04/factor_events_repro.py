import sys, importlib.util, asyncio, sqlite3, json
from pathlib import Path
from types import SimpleNamespace
root=Path('/Users/mac/Desktop/张勇/AIASK')
spec=importlib.util.spec_from_file_location('_aiask_test_hermetic',root/'packages/aiask-quant-core/src/aiask_quant_core/testing/hermetic.py')
mod=importlib.util.module_from_spec(spec);sys.modules[spec.name]=mod;spec.loader.exec_module(mod);mod.install()
from akshare_mcp.services.factor_mining_factory.factory import FactorMiningFactory
from akshare_mcp.services.factor_mining_factory.pool.active_pool import ActiveFactorPool
from akshare_mcp.services.factor_mining_factory.pool.storage import save_factor_to_pool,ensure_factor_pool_tables
from akshare_mcp.services.factor_mining_factory.api import FactorPoolGateway
from akshare_mcp.services.factor_mining_factory.engines.base import FactorCandidate
from strategy_factory.application.factor_mining.orchestrator import FactorMiningOrchestrator
from akshare_mcp.services.market_event_sources import normalize_market_text_events,persist_normalized_events,bridge_normalized_events_to_strategy_factory
class EventDb:
 def __init__(self): self.events=[];self.signals=[];self.clusters=[]
 async def list_market_events_normalized(self, status=None, event_signature=None, limit=50):
  return [x for x in self.events if (status is None or x['status']==status) and (event_signature is None or x['metadata']['event_signature']==event_signature)][:limit]
 async def upsert_market_event_normalized(self,item): self.events.append(item);return item
 async def save_factory_event_cluster(self,item): self.clusters.append(item);return item
 async def save_factory_event_signal(self,item): self.signals.append(item);return item
async def main():
 db=SimpleNamespace(conn=sqlite3.connect(':memory:'))
 await ensure_factor_pool_tables(db)
 expr='ts_mean(close, 5) / ts_mean(close, 20) - 1'
 base={'family':'momentum','expression_dsl':expr,'inputs':['close'],'status':'active','fitness':80,'validation_summary':{'quality_status':'promoted'}}
 await save_factor_to_pool(db,{**base,'factor_id':'old','name':'old'})
 factory=FactorMiningFactory();factory._initialized=True;factory._active_pool=ActiveFactorPool()
 async def get_db(): return db
 factory._get_db=get_db
 gw=FactorPoolGateway();gw._factory=factory
 first=await gw.get_active_factors()
 await save_factor_to_pool(db,{**base,'factor_id':'old','name':'old','status':'retired','validation_summary':{'quality_status':'retired'}})
 await save_factor_to_pool(db,{**base,'factor_id':'new','name':'new'})
 second=await gw.get_active_factors()
 print('STALE_POOL',json.dumps({'first':[x['factor_id'] for x in first],'after_DB_retire_old_add_new':[x['factor_id'] for x in second]},ensure_ascii=False))
 candidate=FactorCandidate(name='one')
 result=FactorMiningOrchestrator(None)._build_quality_summary([candidate]*4,[candidate]*4,[candidate],[{'admitted':True,'promoted_active':True,'record':{**base,'factor_id':'one'}}],SimpleNamespace())
 print('SUMMARY',json.dumps(result,ensure_ascii=False))
 edb=EventDb()
 await persist_normalized_events(edb,'600519','notice',[{'title':'2026年三季度业绩预减公告：预计净利润同比下降80%','date':'2026-11-04','source_tier':'tier_a','provider':'cninfo','code':'600519'}])
 await bridge_normalized_events_to_strategy_factory(edb)
 print('NEGATIVE_EARNINGS',json.dumps({'normalized':[(x['event_type'],x['direction'],x['status']) for x in edb.events],'signals':[(x['symbol'],x['direction']) for x in edb.signals]},ensure_ascii=False))
 pool=ActiveFactorPool();pool._factors={'old':{'factor_id':'old','expression_dsl':'ts_mean(close, 5)','fitness':80}}
 print('ORTHOGONALITY',pool._expression_similarity_details('ts_sum(close, 5) / 5'))
 db.conn.close()
asyncio.run(main())
