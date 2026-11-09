"""Prospective factor evidence cannot inherit another formula's history."""
from copy import deepcopy
from datetime import date, timedelta
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from aiask_quant_core.storage.sqlite import SQLiteAdapter
from akshare_mcp.services.factor_candidate_compiler import compile_factor_candidate
from akshare_mcp.services.factor_evidence_identity import build_factor_evidence_identity, resolve_factor_evidence_identity
from akshare_mcp.services.factor_validation_pipeline import validate_factor_candidate_pipeline
from akshare_mcp.services.factor_mining_factory.engines.base import FactorCandidate
from akshare_mcp.services.factor_mining_factory.pool.active_pool import ActiveFactorPool
from akshare_mcp.services.factor_mining_factory.pool.storage import save_factor_to_pool, save_mining_run
from akshare_mcp.services.factor_mining_factory.reappraise import reappraise_quarantine_factors, _load_factor_ic_rows
from akshare_mcp.services.factor_mining_factory.feedback.decay_monitor import DecayMonitor
from strategy_factory.application.factor_mining.quality_summary import build_quality_summary


def identity_for(payload, **kwargs):
    return build_factor_evidence_identity(compile_factor_candidate(payload), codes=['000001','000002','000003'],
        horizon_days=10, lookback_bars=220, max_dates=60, min_cross_section=30, **kwargs)


def candidate(expression='ts_mean(close, 5) / ts_mean(close, 20) - 1'):
    item = FactorCandidate(name='gp_factor_6', hypothesis='isolated regression', family='momentum',
        inputs=['close'], expression_dsl=expression, generation_engine='gp_classic')
    identity = identity_for(item.to_validation_dict())
    item.validation_result = {
        'success':True, 'metrics':{'rank_ic_mean':.05,'rank_ic_ir':.4,'positive_ratio':.7,'sample_dates':60},
        'coverage':{'avg_cross_section_n':100}, 'lookahead_audit':{'risk_level':'low'}, 'rating':{'grade':'A'},
        'persisted_outputs':{'enabled':True,'ic_history_rows':60,'factor_key':identity['factor_key'],'evidence_identity':identity},
    }
    return item


def record(item, status='quarantine'):
    return {'factor_id':'factor-one','name':item.name,'family':item.family,'inputs':item.inputs,
            'expression_dsl':item.expression_dsl,'status':status,'generation_engine':item.generation_engine,
            'validation_summary':{'quality_status':status,'persisted_outputs':item.validation_result['persisted_outputs']}}


@pytest.mark.asyncio
async def test_real_pipeline_separates_same_name_formulas_and_repeats_idempotently(tmp_path):
    db = SQLiteAdapter(path=tmp_path/'evidence.sqlite3')
    await db.initialize()
    try:
        codes = ['000001','000002','000003','000004']
        for offset,code in enumerate(codes):
            rows=[]
            for i in range(100):
                close=10 + offset + i*(.03+offset*.004) + ((i*(offset+1))%7)*.02
                rows.append({'date':str(date(2025,1,1)+timedelta(days=i)), 'open':close-.02,
                    'high':close+.1,'low':close-.1,'close':close,'volume':100000+i*11+offset*100,'amount':close*100000})
            await db.save_klines(code,rows)
        await db.save_factor_ic('gp_factor_6','2','2024-01-01',.9,.9,100)
        await db.save_factor_values_batch([('000001','2024-01-01','gp_factor_6',999.)])
        payload=candidate().to_validation_dict()
        kwargs=dict(codes=codes,lookback_bars=90,horizon_days=2,max_dates=20,min_cross_section=3,
                    persist_outputs=True,persist_ic_history=True,bind_evidence_identity=True)
        first=await validate_factor_candidate_pipeline(db,payload,**kwargs)
        assert first['success']
        key1=first['persisted_outputs']['factor_key']
        assert key1 != 'gp_factor_6' and first['persisted_outputs']['ic_history_rows']==20
        async with db.acquire() as conn:
            saved1=[dict(r) for r in await conn.fetch('SELECT * FROM factor_values WHERE factor_name=$1 ORDER BY stock_code,factor_date',key1)]
        payload2={**payload,'expression_dsl':'ts_mean(close, 3) / ts_mean(close, 15) - 1'}
        second=await validate_factor_candidate_pipeline(db,payload2,**kwargs)
        key2=second['persisted_outputs']['factor_key']
        assert key2!=key1 and second['persisted_outputs']['ic_history_rows_total']==20
        async with db.acquire() as conn:
            assert saved1==[dict(r) for r in await conn.fetch('SELECT * FROM factor_values WHERE factor_name=$1 ORDER BY stock_code,factor_date',key1)]
        repeated=await validate_factor_candidate_pipeline(db,payload,**kwargs)
        assert repeated['persisted_outputs']['factor_key']==key1
        assert repeated['persisted_outputs']['ic_history_rows_total']==20
        async with db.acquire() as conn:
            assert await conn.fetchval('SELECT COUNT(*) FROM factor_ic_history')==41
            assert await conn.fetchval("SELECT factor_value FROM factor_values WHERE factor_name='gp_factor_6'")==999.
            assert await conn.fetchval("SELECT rank_ic FROM factor_ic_history WHERE factor_name='gp_factor_6'")==.9
        # Names are labels; executable changes and evaluation horizon are identity.
        renamed=await validate_factor_candidate_pipeline(db,{**payload,'name':'new label'},**kwargs)
        assert renamed['persisted_outputs']['factor_key']==key1
        changed=await validate_factor_candidate_pipeline(db,payload,**{**kwargs,'horizon_days':3})
        assert changed['persisted_outputs']['factor_key'] not in {key1,key2}
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_actual_pool_decisions_survive_same_name_candidates_and_run_persistence(tmp_path):
    good=candidate();bad=candidate('ts_mean(close, 7) / ts_mean(close, 21) - 1')
    pool=ActiveFactorPool();pool._knowledge_graph=False
    pool.correlation_evaluator=AsyncMock(return_value={'measurable':False,'reason':'incomplete_correlation_universe','failed_codes':[{'code':'000003','reason':'missing'}]})
    admitted=await pool.admit_batch([good,bad])
    assert len(admitted)==1
    decision=bad.generation_trace['pool_admission']
    assert decision['reason']=='orthogonality_unmeasurable'
    assert decision['orthogonality']['failed_codes']==[{'code':'000003','reason':'missing'}]
    assert good.generation_trace['pool_admission']['candidate_expression_sha256']!=decision['candidate_expression_sha256']
    summary=build_quality_summary([good,bad],[good,bad],[good,bad],[good,bad],admitted,SimpleNamespace())
    decisions=[r['admission_decision'] for r in summary['strict_candidate_results']]
    assert [d['admitted'] for d in decisions]==[True,False]
    assert decisions[1]['blockers']==['orthogonality_unmeasurable']
    assert summary['pool_reject_reasons']=={'orthogonality_unmeasurable':1}
    db=SQLiteAdapter(path=tmp_path/'reports.sqlite3');await db.initialize()
    try:
        await save_mining_run(db,{'run_id':'identity-test','success':True,'quality_summary':summary})
        async with db.acquire() as conn:
            loaded=json.loads(await conn.fetchval("SELECT report FROM factor_mining_runs WHERE run_id='identity-test'"))
        assert loaded['quality_summary']['strict_candidate_results'][1]['admission_decision']['pool_admission']==decision
    finally:await db.close()


@pytest.mark.asyncio
async def test_missing_or_mismatched_identity_cannot_newly_admit_or_reappraise(tmp_path):
    item=candidate();legacy=record(item);legacy['validation_summary']={}
    item.validation_result['persisted_outputs'].pop('evidence_identity')
    decision=await ActiveFactorPool().admit(item)
    assert decision['reason']=='factor_evidence_identity_missing' and not decision['admitted']
    bound=candidate();wrong=record(bound);wrong['expression_dsl']='ts_mean(close, 30)'
    assert resolve_factor_evidence_identity(wrong)['reason']=='factor_evidence_formula_mismatch'
    db=SQLiteAdapter(path=tmp_path/'legacy.sqlite3');await db.initialize()
    try:
        await save_factor_to_pool(db,legacy)
        for i in range(65):await db.save_factor_ic('gp_factor_6','10',str(date(2025,1,1)+timedelta(days=i)),.1,.1,200)
        report=await reappraise_quarantine_factors(db)
        assert report['promoted']==0
        assert report['items'][0]['reasons']==['factor_evidence_identity_missing']
        async with db.acquire() as conn:
            assert await conn.fetchval("SELECT status FROM factor_pool_active WHERE factor_id='factor-one'")=='quarantine'
            assert await conn.fetchval('SELECT COUNT(*) FROM factor_ic_history')==65
    finally:await db.close()


@pytest.mark.asyncio
async def test_bound_history_reads_only_its_key_and_period(tmp_path):
    item=candidate();row=record(item);identity=resolve_factor_evidence_identity(row)
    db=SQLiteAdapter(path=tmp_path/'period.sqlite3');await db.initialize()
    try:
        await db.save_factor_ic(identity['factor_key'],'10','2025-01-01',.1,.1,100)
        await db.save_factor_ic(identity['factor_key'],'20','2025-01-01',.9,.9,100)
        await db.save_factor_ic(item.name,'10','2025-01-01',-.9,-.9,100)
        rows=await _load_factor_ic_rows(db,identity['factor_key'],identity['period'])
        assert [r['rank_ic'] for r in rows]==[.1]
        monitor=DecayMonitor()
        monitor._load_ic_history=AsyncMock(return_value=[])
        assert await monitor._measure_factor(db,row) is None
        monitor._load_ic_history.assert_awaited_once_with(db,identity['factor_key'],period='10')
        monitor._load_ic_history.reset_mock()
        assert await monitor._measure_factor(db,{**row,'validation_summary':{}}) is None
        monitor._load_ic_history.assert_not_awaited()
    finally:await db.close()


@pytest.mark.asyncio
async def test_admission_error_is_visible_and_missing_legacy_decision_is_unknown():
    item=candidate();pool=ActiveFactorPool();pool.admit=AsyncMock(side_effect=RuntimeError('correlation unavailable'))
    assert await pool.admit_batch([item])==[]
    decision=item.generation_trace['pool_admission']
    assert decision['reason']=='pool_admission_error' and decision['error']=='correlation unavailable'
    old=candidate()
    summary=build_quality_summary([old],[old],[old],[old],[],SimpleNamespace())
    assert summary['strict_candidate_results'][0]['admission_decision']['pool_admission']['status']=='unknown_legacy'
    assert summary['pool_reject_reasons']=={'pool_admission_decision_missing':1}
