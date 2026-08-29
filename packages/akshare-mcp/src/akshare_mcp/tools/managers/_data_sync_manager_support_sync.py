"""数据同步管理器 - MCP 工具层，供用户/AI 按需触发同步任务。

与 DataSyncScheduler (services/data_sync_scheduler.py) 的区别：
- DataSyncScheduler 是后台自动调度器（启动时 + 每日 15:30）
- 本模块是 MCP 工具，通过 ``data_sync_manager(action=...)`` 按需执行
- sync_daily/sync_init.py 是独立脚本，用于深度历史全量回填
"""

from typing import Any
import argparse
import contextlib
import importlib.util
import json
import logging
from io import StringIO
from pathlib import Path
from datetime import datetime, timedelta
from ...storage import get_db
from ...utils import ok, fail
from ...vector_collection_scope import normalize_market_doc_types, resolve_vector_collection_name
from ..manager_protocol import normalize_manager_payload
from ._data_sync_manager_support_core import (
    _as_bool,
    _core_market_script_path,
    _factor_context_script_path,
    _load_market_aux_status,
)

logger = logging.getLogger(__name__)

async def _sync_klines_now(codes: list) -> dict:
    """立即执行K线同步，使用 data_source.get_kline() 完整降级链"""
    from ...data_source import data_source
    from ...storage import get_db

    db = get_db()
    results = {'success': 0, 'failed': 0, 'errors': []}

    for code in codes:
        try:
            klines = data_source.get_kline(code, 'daily', 250)
            if klines:
                try:
                    await db.save_klines(code, klines)
                except Exception as e:
                    logger.warning(f"[DataSyncManager] {code} K线写入DB失败: {e}")
                results['success'] += 1
            else:
                results['failed'] += 1
                results['errors'].append(f"{code}: 所有数据源均无数据")
        except Exception as e:
            results['failed'] += 1
            results['errors'].append(f"{code}: {e}")

    # 截断错误列表避免过长
    if len(results['errors']) > 10:
        total = len(results['errors'])
        results['errors'] = results['errors'][:10] + [f'...及其他 {total - 10} 个错误']

    return results

async def _sync_quotes_now(codes: list) -> dict:
    """Explicitly refresh quote snapshots into stock_quotes."""
    from ...services.market_data_access import FALLBACK_LIVE_ONLY, get_quote_snapshot

    results = {'success': 0, 'failed': 0, 'errors': [], 'backend': 'data_source.realtime_quote'}
    for code in codes:
        try:
            access = await get_quote_snapshot(code, fallback_mode=FALLBACK_LIVE_ONLY)
            if access.get('success'):
                results['success'] += 1
            else:
                results['failed'] += 1
                results['errors'].append(f"{code}: {access.get('error') or 'quote unavailable'}")
        except Exception as e:
            results['failed'] += 1
            results['errors'].append(f"{code}: {e}")
    if len(results['errors']) > 10:
        total = len(results['errors'])
        results['errors'] = results['errors'][:10] + [f'...and {total - 10} more errors']
    return results

async def _sync_financials_check(codes: list) -> dict:
    """检查财务数据是否存在，不存在则提示用户运行 sync_init.py"""
    from ...storage import get_db

    db = get_db()
    results = {'success': 0, 'failed': 0, 'errors': [], 'message': ''}

    missing = []
    for code in codes:
        try:
            async with db.acquire() as conn:
                cnt = await conn.fetchval(
                    "SELECT COUNT(*) FROM financials WHERE stock_code = $1", code
                )
                if cnt and cnt > 0:
                    results['success'] += 1
                else:
                    missing.append(code)
                    results['failed'] += 1
        except Exception as e:
            results['failed'] += 1
            results['errors'].append(f"{code}: {e}")

    if missing:
        results['message'] = (
            f'{len(missing)} 只股票无财务数据，'
            f'建议运行 python sync_init.py 进行历史数据初始化'
        )

    return results

async def _sync_market_temperature_snapshot_cache_now(kwargs: dict) -> dict:
    """Refresh the durable market-temperature snapshot cache through the manager plane."""
    from .. import market_temperature as market_temperature_tools

    result = await market_temperature_tools.refresh_market_temperature_snapshot_cache(
        limit=int(kwargs.get("limit") or 1000),
        top_n=int(kwargs.get("top_n") or 20),
        as_of=str(kwargs.get("as_of") or "").strip() or None,
        min_bars=int(kwargs.get("min_bars") or 20),
    )
    success = bool(result.get("success"))
    data = result.get("data") if isinstance(result.get("data"), dict) else {}
    return {
        "success": 1 if success else 0,
        "failed": 0 if success else 1,
        "errors": [] if success else [str(result.get("error") or "market_temperature_cache_refresh_failed")],
        "cache": data.get("cache"),
        "snapshot": data.get("snapshot"),
        "source_chain": result.get("source_chain") or (result.get("meta") or {}).get("source_chain") or [],
        "message": "market_temperature_snapshot_cache refreshed" if success else "market_temperature_snapshot_cache refresh failed",
    }

async def _sync_core_market_now(kwargs: dict) -> dict:
    """调用核心市场审查补数脚本，补齐指数/北向/融资融券数据。"""
    script_path = _core_market_script_path()
    if not script_path.exists():
        return {'success': 0, 'failed': 1, 'errors': [f'脚本不存在: {script_path}']}

    spec = importlib.util.spec_from_file_location("audit_sync_core_market_data_runtime", script_path)
    if spec is None or spec.loader is None:
        return {'success': 0, 'failed': 1, 'errors': [f'无法加载脚本: {script_path}']}

    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    args = argparse.Namespace(
        years=max(int(kwargs.get('years', 1) or 1), 1),
        stock_codes=",".join([str(item).strip() for item in list(kwargs.get('codes') or kwargs.get('stock_codes') or []) if str(item).strip()])
        if isinstance(kwargs.get('codes') or kwargs.get('stock_codes'), list)
        else str(kwargs.get('stock_codes') or kwargs.get('codes') or ",".join(getattr(module, 'DEFAULT_STOCK_CODES', []))),
        calendar_year=int(kwargs.get('calendar_year') or datetime.now().year),
        north_days=max(int(kwargs.get('north_days', 365) or 365), 1),
        margin_days=max(int(kwargs.get('margin_days', 90) or 90), 1),
    )

    capture = StringIO()
    exit_code = 1
    with contextlib.redirect_stdout(capture):
        exit_code = await module._main(args)

    db = get_db()
    market_aux = await _load_market_aux_status(db)
    output_lines = [line for line in capture.getvalue().splitlines() if line.strip()]
    tail_lines = output_lines[-40:]
    result = {
        'success': 1 if exit_code == 0 else 0,
        'failed': 0 if exit_code == 0 else 1,
        'errors': [] if exit_code == 0 else [f'core_market_sync exit_code={exit_code}'],
        'exit_code': int(exit_code),
        'args': {
            'years': args.years,
            'stock_codes': args.stock_codes,
            'calendar_year': args.calendar_year,
            'north_days': args.north_days,
            'margin_days': args.margin_days,
        },
        'market_aux': market_aux,
        'stdout_tail': tail_lines,
    }
    return result

async def _sync_factor_context_now(kwargs: dict) -> dict:
    """调用因子上下文补数脚本，补齐新闻/公告/研报/个股资金流。"""
    script_path = _factor_context_script_path()
    if not script_path.exists():
        return {'success': 0, 'failed': 1, 'errors': [f'脚本不存在: {script_path}']}

    spec = importlib.util.spec_from_file_location("audit_sync_factor_context_runtime", script_path)
    if spec is None or spec.loader is None:
        return {'success': 0, 'failed': 1, 'errors': [f'无法加载脚本: {script_path}']}

    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    raw_codes = kwargs.get('codes') or kwargs.get('stock_codes')
    args = argparse.Namespace(
        codes=",".join([str(item).strip() for item in list(raw_codes) if str(item).strip()]) if isinstance(raw_codes, list) else str(raw_codes or ""),
        scope_sources=str(kwargs.get('scope_sources') or "explicit,representative,active_pool,factory_targets"),
        active_pool_limit=max(int(kwargs.get('active_pool_limit', 12) or 12), 1),
        task_run_limit=max(int(kwargs.get('task_run_limit', 50) or 50), 1),
        news_days=max(int(kwargs.get('news_days', 30) or 30), 1),
        notice_days=max(int(kwargs.get('notice_days', 30) or 30), 1),
        item_limit=max(int(kwargs.get('item_limit', 10) or 10), 1),
    )

    capture = StringIO()
    exit_code = 1
    with contextlib.redirect_stdout(capture):
        exit_code = await module._main(args)

    db = get_db()
    market_aux = await _load_market_aux_status(db)
    output_lines = [line for line in capture.getvalue().splitlines() if line.strip()]
    tail_lines = output_lines[-40:]
    return {
        'success': 1 if exit_code == 0 else 0,
        'failed': 0 if exit_code == 0 else 1,
        'errors': [] if exit_code == 0 else [f'factor_context_sync exit_code={exit_code}'],
        'exit_code': int(exit_code),
        'args': {
            'codes': args.codes,
            'scope_sources': args.scope_sources,
            'active_pool_limit': args.active_pool_limit,
            'task_run_limit': args.task_run_limit,
            'news_days': args.news_days,
            'notice_days': args.notice_days,
            'item_limit': args.item_limit,
        },
        'market_aux': market_aux,
        'stdout_tail': tail_lines,
    }

async def _sync_market_text_source_ingest_now(kwargs: dict) -> dict:
    """增量同步公开新闻/公告/研报，并重建市场文本向量 snapshot。"""
    from strategy_factory.runtime.default_bootstrap import ensure_default_runtime_services
    from strategy_factory.runtime.market_event_ingest import get_market_event_ingest_runtime

    db = get_db()
    ensure_default_runtime_services()
    result = await get_market_event_ingest_runtime().run_once(
        db=db,
        stock_codes=kwargs.get('stock_codes') or kwargs.get('codes'),
        doc_types=kwargs.get('doc_types'),
        news_limit=kwargs.get('news_limit', 50),
        notice_limit=kwargs.get('notice_limit', 80),
        official_notice_limit=kwargs.get('official_notice_limit', 30),
        notice_days=kwargs.get('notice_days', 30),
        code_notice_limit=kwargs.get('code_notice_limit', 2),
        code_notice_code_limit=kwargs.get('code_notice_code_limit', 20),
        research_code_limit=kwargs.get('research_code_limit', 30),
        research_per_code=kwargs.get('research_per_code', 2),
        chunk_size=kwargs.get('chunk_size', 1000),
        overlap=kwargs.get('overlap', 120),
        version=kwargs.get('version', 'v1'),
        embed=kwargs.get('embed', True),
        build_snapshot=kwargs.get('build_snapshot', True),
        activate_snapshot=kwargs.get('activate_snapshot', True),
        allow_network=kwargs.get('allow_network', True),
        dry_run=kwargs.get('dry_run', False),
    )
    market_aux = await _load_market_aux_status(db)
    totals = dict(result.get('totals') or {})
    error_count = int(totals.get('errors') or 0)
    saved_docs = int(totals.get('saved_docs') or 0)
    embedded_chunks = int(totals.get('embedded_chunks') or 0)
    snapshot_count = int(totals.get('snapshots') or 0)
    return {
        'success': 1 if error_count == 0 else 0,
        'failed': 0 if error_count == 0 else 1,
        'errors': [
            json.dumps(item, ensure_ascii=False, default=str)
            for item in list(result.get('errors') or [])[:10]
        ],
        'args': result.get('args') or {},
        'ingest': result,
        'market_aux': market_aux,
        'message': (
            f"market_text_source_ingest docs={saved_docs} "
            f"embedded={embedded_chunks} snapshots={snapshot_count}"
        ),
    }

async def _maybe_build_backfill_snapshot(
    db,
    *,
    kwargs: dict,
    collection_name: str,
    version: str | None,
    index_version: str | None = None,
    source: str,
    profile_type: str | None = None,
) -> dict | None:
    if not _as_bool(kwargs.get('build_snapshot', False), False):
        return None
    if _as_bool(kwargs.get('dry_run', False), False):
        return {
            'collection_name': collection_name,
            'profile_version': str(version or '').strip() or None,
            'index_version': str(index_version or kwargs.get('index_version') or '').strip() or None,
            'status': 'skipped',
            'reason': 'dry_run',
        }

    from ...services.unified_vector_governance import build_vector_collection_snapshot

    return await build_vector_collection_snapshot(
        db,
        collection_name=collection_name,
        version=str(version or '').strip() or None,
        index_version=str(index_version or kwargs.get('index_version') or '').strip() or None,
        profile_type=str(profile_type or '').strip() or None,
        activate=_as_bool(kwargs.get('activate_snapshot', True), True),
        source=source,
    )

async def _sync_vector_backfill_market_docs_now(kwargs: dict) -> dict:
    """回填历史市场文本到统一向量层。"""
    from ...services.vector_backfill import backfill_market_document_vectors

    db = get_db()
    result = await backfill_market_document_vectors(
        db,
        stock_codes=kwargs.get('stock_codes') or kwargs.get('codes'),
        doc_types=kwargs.get('doc_types'),
        start_date=kwargs.get('start_date'),
        end_date=kwargs.get('end_date'),
        limit=kwargs.get('limit', 500),
        batch_size=kwargs.get('batch_size', 100),
        embed=kwargs.get('embed', True),
        chunk_size=kwargs.get('chunk_size', 800),
        overlap=kwargs.get('overlap', 120),
        version=kwargs.get('version', 'v1'),
        rebuild_existing=kwargs.get('rebuild_existing', False),
        dry_run=kwargs.get('dry_run', False),
        include_legacy_research_docs=kwargs.get('include_legacy_research_docs', False),
    )
    snapshot_rows: list[dict] = []
    requested_doc_types = normalize_market_doc_types(result.get('doc_types') or kwargs.get('doc_types'))
    profile_versions_by_doc_type = dict(result.get('profile_version_counts_by_doc_type') or {})
    if _as_bool(kwargs.get('build_snapshot', False), False):
        for doc_type in requested_doc_types:
            requested_collection = resolve_vector_collection_name('market_doc_chunks', doc_type)
            version_rows = [
                str(profile_version or '').strip()
                for profile_version in dict(profile_versions_by_doc_type.get(doc_type) or {}).keys()
                if str(profile_version or '').strip()
            ]
            if not version_rows:
                version_rows = [str(result.get('version') or kwargs.get('version') or 'v1').strip()]
            explicit_index_version = str(kwargs.get('index_version') or '').strip() or None
            for profile_version in version_rows:
                resolved_index_version = explicit_index_version if len(version_rows) == 1 else profile_version
                snapshot = await _maybe_build_backfill_snapshot(
                    db,
                    kwargs=kwargs,
                    collection_name=requested_collection,
                    version=profile_version,
                    index_version=resolved_index_version,
                    source='data_sync_manager.vector_backfill_market_docs',
                    profile_type=doc_type,
                )
                if snapshot:
                    snapshot_rows.append(snapshot)
    snapshot = snapshot_rows[0] if len(snapshot_rows) == 1 else None
    market_aux = await _load_market_aux_status(db)
    return {
        'success': 1,
        'failed': 0,
        'errors': [],
        'args': {
            'stock_codes': result.get('stock_codes'),
            'doc_types': result.get('doc_types'),
            'start_date': result.get('start_date'),
            'end_date': result.get('end_date'),
            'limit': result.get('limit'),
            'batch_size': result.get('batch_size'),
            'embed': result.get('embed'),
            'chunk_size': result.get('chunk_size'),
            'overlap': result.get('overlap'),
            'version': result.get('version'),
            'rebuild_existing': result.get('rebuild_existing'),
            'dry_run': result.get('dry_run'),
            'include_legacy_research_docs': result.get('include_legacy_research_docs'),
            'build_snapshot': _as_bool(kwargs.get('build_snapshot', False), False),
            'activate_snapshot': _as_bool(kwargs.get('activate_snapshot', True), True),
            'index_version': (snapshot or {}).get('index_version') or kwargs.get('index_version'),
        },
        'backfill': result,
        'snapshot': snapshot,
        'snapshots': snapshot_rows,
        'market_aux': market_aux,
        'message': (
            f"market_doc_backfill docs={int(result.get('saved_docs') or 0)} "
            f"chunks={int(result.get('saved_chunks') or 0)} "
            f"embedded={int(result.get('embedded_chunks') or 0)}"
            + (f" snapshots={len(snapshot_rows)}" if snapshot_rows else "")
        ),
    }

async def _sync_vector_backfill_kline_patterns_now(kwargs: dict) -> dict:
    """回填 K 线模式窗口到统一向量层。"""
    from ...services.pattern_embedding_pipeline import backfill_kline_pattern_vectors

    db = get_db()
    result = await backfill_kline_pattern_vectors(
        db,
        stock_codes=kwargs.get('stock_codes') or kwargs.get('codes'),
        code_limit=kwargs.get('code_limit', 200),
        window_size=kwargs.get('window_size', kwargs.get('days', 20)),
        lookback_days=kwargs.get('lookback_days', 180),
        max_windows_per_code=kwargs.get('max_windows_per_code', 1),
        step_days=kwargs.get('step_days', 5),
        vector_method=kwargs.get('vector_method', 'returns'),
        period=kwargs.get('period', 'daily'),
        adjust=kwargs.get('adjust', ''),
        version=kwargs.get('version', 'v1'),
        rebuild_existing=kwargs.get('rebuild_existing', False),
        dry_run=kwargs.get('dry_run', False),
    )
    snapshot = await _maybe_build_backfill_snapshot(
        db,
        kwargs=kwargs,
        collection_name='kline_pattern_embeddings',
        version=result.get('version') or kwargs.get('version'),
        source='data_sync_manager.vector_backfill_kline_patterns',
    )
    market_aux = await _load_market_aux_status(db)
    return {
        'success': 1,
        'failed': 0,
        'errors': [],
        'args': {
            'stock_codes': result.get('stock_codes'),
            'code_limit': kwargs.get('code_limit', 200),
            'window_size': result.get('window_size'),
            'lookback_days': result.get('lookback_days'),
            'max_windows_per_code': result.get('max_windows_per_code'),
            'step_days': result.get('step_days'),
            'vector_method': result.get('vector_method'),
            'period': result.get('period'),
            'adjust': result.get('adjust'),
            'version': result.get('version'),
            'rebuild_existing': result.get('rebuild_existing'),
            'dry_run': result.get('dry_run'),
            'build_snapshot': _as_bool(kwargs.get('build_snapshot', False), False),
            'activate_snapshot': _as_bool(kwargs.get('activate_snapshot', True), True),
            'index_version': (snapshot or {}).get('index_version') or kwargs.get('index_version'),
        },
        'backfill': result,
        'snapshot': snapshot,
        'market_aux': market_aux,
        'message': (
            f"kline_pattern_backfill windows={int(result.get('saved_windows') or 0)} "
            f"profiles={int(result.get('saved_profiles') or 0)}"
            + (f" snapshot={snapshot.get('status')}" if snapshot else "")
        ),
    }

async def _sync_vector_backfill_stock_profiles_now(kwargs: dict) -> dict:
    """回填股票画像向量到统一向量层。"""
    from ...services.stock_profile_pipeline import backfill_stock_profile_vectors

    db = get_db()
    result = await backfill_stock_profile_vectors(
        db,
        stock_codes=kwargs.get('stock_codes') or kwargs.get('codes'),
        code_limit=kwargs.get('code_limit', 200),
        profile_types=kwargs.get('profile_types') or kwargs.get('similarity_types'),
        kline_limit=kwargs.get('kline_limit', 90),
        version=kwargs.get('version', 'v1'),
        rebuild_existing=kwargs.get('rebuild_existing', False),
        dry_run=kwargs.get('dry_run', False),
    )
    # 股票画像 collection 会同时承载多种 profile_type，自动快照默认按 collection+version 整体构建。
    snapshot = await _maybe_build_backfill_snapshot(
        db,
        kwargs=kwargs,
        collection_name='stock_profile_embeddings',
        version=result.get('version') or kwargs.get('version'),
        source='data_sync_manager.vector_backfill_stock_profiles',
    )
    market_aux = await _load_market_aux_status(db)
    return {
        'success': 1,
        'failed': 0,
        'errors': [],
        'args': {
            'stock_codes': result.get('stock_codes'),
            'code_limit': kwargs.get('code_limit', 200),
            'profile_types': result.get('profile_types'),
            'kline_limit': result.get('kline_limit'),
            'version': result.get('version'),
            'rebuild_existing': result.get('rebuild_existing'),
            'dry_run': result.get('dry_run'),
            'build_snapshot': _as_bool(kwargs.get('build_snapshot', False), False),
            'activate_snapshot': _as_bool(kwargs.get('activate_snapshot', True), True),
            'index_version': (snapshot or {}).get('index_version') or kwargs.get('index_version'),
        },
        'backfill': result,
        'snapshot': snapshot,
        'market_aux': market_aux,
        'message': (
            f"stock_profile_backfill profiles={int(result.get('saved_profiles') or 0)} "
            f"codes={int(result.get('processed_codes') or 0)}"
            + (f" snapshot={snapshot.get('status')}" if snapshot else "")
        ),
    }

async def _sync_vector_backfill_factor_candidates_now(kwargs: dict) -> dict:
    """回填因子研究记忆向量到统一向量层。"""
    from ...services.factor_candidate_seed import seed_factor_candidate_records
    from ...services.factor_candidate_vector_backfill import backfill_factor_candidate_vectors

    db = get_db()
    seed_result = None
    if _as_bool(kwargs.get('seed_if_empty', True), True):
        seed_result = await seed_factor_candidate_records(
            db,
            limit=kwargs.get('seed_limit') or kwargs.get('limit', 200),
            codes=kwargs.get('codes') or kwargs.get('stock_codes'),
            rebuild_existing=kwargs.get('seed_rebuild_existing', False),
            dry_run=kwargs.get('dry_run', False),
        )
    result = await backfill_factor_candidate_vectors(
        db,
        limit=kwargs.get('limit', 200),
        codes=kwargs.get('codes') or kwargs.get('stock_codes'),
        status=kwargs.get('status'),
        family=kwargs.get('family'),
        version=kwargs.get('version', 'v1'),
        rebuild_existing=kwargs.get('rebuild_existing', False),
        dry_run=kwargs.get('dry_run', False),
    )
    snapshot = await _maybe_build_backfill_snapshot(
        db,
        kwargs=kwargs,
        collection_name='factor_candidate_embeddings',
        version=result.get('version') or kwargs.get('version'),
        source='data_sync_manager.vector_backfill_factor_candidates',
    )
    market_aux = await _load_market_aux_status(db)
    return {
        'success': 1,
        'failed': 0,
        'errors': [],
        'args': {
            'limit': result.get('limit'),
            'codes': result.get('codes'),
            'status': result.get('status'),
            'family': result.get('family'),
            'version': result.get('version'),
            'rebuild_existing': result.get('rebuild_existing'),
            'dry_run': result.get('dry_run'),
            'build_snapshot': _as_bool(kwargs.get('build_snapshot', False), False),
            'activate_snapshot': _as_bool(kwargs.get('activate_snapshot', True), True),
            'index_version': (snapshot or {}).get('index_version') or kwargs.get('index_version'),
            'seed_if_empty': _as_bool(kwargs.get('seed_if_empty', True), True),
            'seed_limit': kwargs.get('seed_limit') or kwargs.get('limit', 200),
        },
        'seed': seed_result,
        'backfill': result,
        'snapshot': snapshot,
        'market_aux': market_aux,
        'message': (
            f"factor_candidate_backfill profiles={int(result.get('saved_profiles') or 0)} "
            f"records={int(result.get('processed_records') or 0)} "
            f"seeded={int((seed_result or {}).get('saved_records') or 0)}"
            + (f" snapshot={snapshot.get('status')}" if snapshot else "")
        ),
    }

async def _sync_factor_external_research_ingest_now(kwargs: dict) -> dict:
    """接入公开/授权外部因子研究，生成 review 候选并回填候选向量。"""
    from ...services.factor_candidate_vector_backfill import backfill_factor_candidate_vectors
    from ...services.factor_external_research import ingest_external_factor_research

    db = get_db()
    ingest_result = await ingest_external_factor_research(
        db,
        sources=kwargs.get('sources'),
        limit=kwargs.get('limit', 20),
        codes=kwargs.get('codes') or kwargs.get('stock_codes'),
        allow_network=kwargs.get('allow_network', True),
        timeout_sec=kwargs.get('timeout_sec', 8.0),
        create_candidates=kwargs.get('create_candidates', True),
        rebuild_existing=kwargs.get('rebuild_existing', False),
        dry_run=kwargs.get('dry_run', False),
    )
    backfill_result = None
    snapshot = None
    if _as_bool(kwargs.get('backfill_vectors', True), True):
        backfill_result = await backfill_factor_candidate_vectors(
            db,
            limit=kwargs.get('vector_limit') or kwargs.get('limit', 200),
            codes=kwargs.get('codes') or kwargs.get('stock_codes'),
            status=kwargs.get('status'),
            family=kwargs.get('family'),
            version=kwargs.get('version', 'v1'),
            rebuild_existing=kwargs.get('vector_rebuild_existing', kwargs.get('rebuild_existing', False)),
            dry_run=kwargs.get('dry_run', False),
        )
        snapshot = await _maybe_build_backfill_snapshot(
            db,
            kwargs=kwargs,
            collection_name='factor_candidate_embeddings',
            version=(backfill_result or {}).get('version') or kwargs.get('version'),
            source='data_sync_manager.factor_external_research_ingest',
        )
    market_aux = await _load_market_aux_status(db)
    return {
        'success': 1 if not ingest_result.get('errors') else (1 if ingest_result.get('saved_evidence_records') or ingest_result.get('saved_candidate_records') else 0),
        'failed': 0 if not ingest_result.get('errors') else (0 if ingest_result.get('saved_evidence_records') or ingest_result.get('saved_candidate_records') else 1),
        'errors': list(ingest_result.get('errors') or []),
        'args': {
            'limit': ingest_result.get('limit'),
            'allow_network': ingest_result.get('allow_network'),
            'create_candidates': ingest_result.get('create_candidates'),
            'dry_run': ingest_result.get('dry_run'),
            'backfill_vectors': _as_bool(kwargs.get('backfill_vectors', True), True),
            'version': kwargs.get('version', 'v1'),
            'build_snapshot': _as_bool(kwargs.get('build_snapshot', False), False),
            'activate_snapshot': _as_bool(kwargs.get('activate_snapshot', True), True),
        },
        'ingest': ingest_result,
        'backfill': backfill_result,
        'snapshot': snapshot,
        'market_aux': market_aux,
        'message': (
            f"factor_external_research evidence={int(ingest_result.get('saved_evidence_records') or 0)} "
            f"candidates={int(ingest_result.get('saved_candidate_records') or 0)} "
            f"vectors={int((backfill_result or {}).get('saved_profiles') or 0)}"
            + (f" snapshot={snapshot.get('status')}" if snapshot else "")
        ),
    }

async def _sync_vector_build_snapshot_now(kwargs: dict) -> dict:
    """构建统一向量 collection 的 snapshot 与 ANN 索引。"""
    from ...services.unified_vector_governance import build_vector_collection_snapshot

    db = get_db()
    collection_name = str(kwargs.get('collection_name') or kwargs.get('collection') or '').strip()
    if not collection_name:
        return {'success': 0, 'failed': 1, 'errors': ['缺少 collection_name']}

    result = await build_vector_collection_snapshot(
        db,
        collection_name=collection_name,
        version=kwargs.get('version'),
        index_version=kwargs.get('index_version'),
        profile_type=kwargs.get('profile_type'),
        limit_profiles=kwargs.get('limit_profiles', 5000),
        bucket_count=kwargs.get('bucket_count'),
        activate=kwargs.get('activate', True),
        source='data_sync_manager',
    )
    market_aux = await _load_market_aux_status(db)
    return {
        'success': 1 if str(result.get('status') or '').strip().lower() != 'failed' else 0,
        'failed': 0 if str(result.get('status') or '').strip().lower() != 'failed' else 1,
        'errors': [] if str(result.get('status') or '').strip().lower() != 'failed' else [f"vector_build_snapshot failed for {collection_name}"],
        'args': {
            'collection_name': collection_name,
            'profile_type': result.get('profile_type'),
            'version': result.get('profile_version'),
            'index_version': result.get('index_version'),
            'limit_profiles': kwargs.get('limit_profiles', 5000),
            'bucket_count': result.get('bucket_count'),
            'activate': kwargs.get('activate', True),
        },
        'snapshot': result,
        'market_aux': market_aux,
        'message': (
            f"vector_build_snapshot collection={collection_name} "
            f"status={result.get('status')} items={int(result.get('items_count') or 0)}"
        ),
    }

async def _sync_vector_benchmark_collection_now(kwargs: dict) -> dict:
    """运行统一向量 collection 的 exact-vs-ANN 检索基线评测。"""
    from ...services.unified_vector_benchmark import benchmark_vector_collection_search

    db = get_db()
    collection_name = str(kwargs.get('collection_name') or kwargs.get('collection') or '').strip()
    if not collection_name:
        return {'success': 0, 'failed': 1, 'errors': ['缺少 collection_name']}

    result = await benchmark_vector_collection_search(
        db,
        collection_name=collection_name,
        profile_type=kwargs.get('profile_type'),
        version=kwargs.get('version'),
        index_version=kwargs.get('index_version'),
        sample_size=kwargs.get('sample_size', 30),
        top_k=kwargs.get('top_k', 10),
        limit_profiles=kwargs.get('limit_profiles', 5000),
        metric=kwargs.get('metric', 'cosine'),
        persist_snapshot_metrics=kwargs.get('persist_snapshot_metrics', True),
    )
    market_aux = await _load_market_aux_status(db)
    retrieval_quality = dict(result.get('retrieval_quality') or {})
    latency_ms = dict(result.get('latency_ms') or {})
    return {
        'success': 1 if str(result.get('status') or '').strip().lower() != 'failed' else 0,
        'failed': 0 if str(result.get('status') or '').strip().lower() != 'failed' else 1,
        'errors': [] if str(result.get('status') or '').strip().lower() != 'failed' else [f"vector_benchmark_collection failed for {collection_name}"],
        'args': {
            'collection_name': collection_name,
            'profile_type': result.get('profile_type'),
            'version': result.get('profile_version'),
            'index_version': result.get('index_version'),
            'sample_size': kwargs.get('sample_size', 30),
            'top_k': kwargs.get('top_k', 10),
            'limit_profiles': kwargs.get('limit_profiles', 5000),
            'metric': kwargs.get('metric', 'cosine'),
            'persist_snapshot_metrics': _as_bool(kwargs.get('persist_snapshot_metrics', True), True),
        },
        'benchmark': result,
        'market_aux': market_aux,
        'message': (
            f"vector_benchmark collection={collection_name} "
            f"recall@k={retrieval_quality.get('recall_at_k')} "
            f"ann_p95={latency_ms.get('ann_p95')} "
            f"persisted={result.get('benchmark_persisted')}"
        ),
    }


async def _sync_vector_optimize_bootstrap_now(kwargs: dict) -> dict:
    """初始化并分批回填纯 SQLite 多维向量层。"""
    from ...services.vector_optimize_bootstrap import run_vector_optimize_bootstrap

    db = get_db()
    result = await run_vector_optimize_bootstrap(
        db,
        scope=kwargs.get('scope', 'full'),
        dry_run=kwargs.get('dry_run', False),
        resume=kwargs.get('resume', True),
        batch_size=kwargs.get('batch_size', 500),
        cursor=kwargs.get('cursor'),
        build_snapshot=kwargs.get('build_snapshot', True),
        activate_snapshot=kwargs.get('activate_snapshot', True),
    )
    failed = 1 if str(result.get('status') or '').strip().lower() == 'failed' else 0
    return {
        'success': 0 if failed else 1,
        'failed': failed,
        'errors': [str(result.get('stats', {}).get('error'))] if failed and result.get('stats', {}).get('error') else [],
        'args': {
            'scope': result.get('scope'),
            'dry_run': result.get('dry_run'),
            'resume': kwargs.get('resume', True),
            'batch_size': result.get('batch_size'),
            'cursor': result.get('cursor'),
            'build_snapshot': _as_bool(kwargs.get('build_snapshot', True), True),
            'activate_snapshot': _as_bool(kwargs.get('activate_snapshot', True), True),
        },
        'bootstrap': result,
        'run_id': result.get('run_id'),
        'next_cursor': result.get('next_cursor'),
        'message': (
            f"vector_optimize_bootstrap status={result.get('status')} "
            f"codes={int(result.get('processed_codes') or 0)} "
            f"next_cursor={result.get('next_cursor') or ''}"
        ),
    }


async def _sync_factor_validation_bootstrap_now(kwargs: dict) -> dict:
    """Run local IC / RankIC / OOS validation for factor candidates."""
    from ...services.factor_validation_bootstrap import run_factor_validation_bootstrap

    db = get_db()
    result = await run_factor_validation_bootstrap(
        db,
        status=kwargs.get('status', 'review'),
        max_candidates=kwargs.get('max_candidates', 50),
        horizon_days=kwargs.get('horizon_days', 10),
        max_dates=kwargs.get('max_dates', 60),
        lookback_bars=kwargs.get('lookback_bars', 220),
        min_cross_section=kwargs.get('min_cross_section', 100),
        promote=kwargs.get('promote', True),
        resume=kwargs.get('resume', True),
        dry_run=kwargs.get('dry_run', False),
        universe_limit=kwargs.get('universe_limit'),
        codes=kwargs.get('codes'),
        stock_codes=kwargs.get('stock_codes'),
        candidate_ids=kwargs.get('candidate_ids') or kwargs.get('artifact_ids'),
        family=kwargs.get('family'),
        persist_outputs=kwargs.get('persist_outputs', True),
    )
    failed = 1 if str(result.get('status') or '').strip().lower() == 'failed' else 0
    return {
        'success': 0 if failed else 1,
        'failed': failed,
        'errors': list(result.get('errors') or []),
        'args': {
            'status': kwargs.get('status', 'review'),
            'max_candidates': kwargs.get('max_candidates', 50),
            'horizon_days': kwargs.get('horizon_days', 10),
            'max_dates': kwargs.get('max_dates', 60),
            'lookback_bars': kwargs.get('lookback_bars', 220),
            'min_cross_section': kwargs.get('min_cross_section', 100),
            'promote': _as_bool(kwargs.get('promote', True), True),
            'resume': _as_bool(kwargs.get('resume', True), True),
            'dry_run': _as_bool(kwargs.get('dry_run', False), False),
            'universe_limit': kwargs.get('universe_limit'),
            'persist_outputs': _as_bool(kwargs.get('persist_outputs', True), True),
        },
        'bootstrap': result,
        'run_id': result.get('run_id'),
        'message': result.get('message') or (
            f"factor_validation_bootstrap status={result.get('status')} "
            f"processed={int(result.get('processed') or 0)} "
            f"factor_values={int(result.get('factor_value_rows') or 0)} "
            f"ic_rows={int(result.get('ic_history_rows') or 0)} "
            f"promoted={int(result.get('promoted') or 0)}"
        ),
    }
