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
from ...services.market_data_access import quote_max_stale_seconds
from ...storage import get_db
from ...utils import ok, fail
from ..manager_protocol import normalize_manager_payload

logger = logging.getLogger(__name__)

from . import _data_sync_manager_support_core as _data_sync_core_mod
from . import _data_sync_manager_support_sync as _data_sync_sync_mod

_normalize_codes = _data_sync_core_mod._normalize_codes
_decode_json_obj = _data_sync_core_mod._decode_json_obj
_as_bool = _data_sync_core_mod._as_bool
_compute_next_run = _data_sync_core_mod._compute_next_run
_build_schedule_params = _data_sync_core_mod._build_schedule_params
_build_task_payload = _data_sync_core_mod._build_task_payload
_execute_sync_task = _data_sync_core_mod._execute_sync_task
_run_due_schedules = _data_sync_core_mod._run_due_schedules
_load_market_aux_status = _data_sync_core_mod._load_market_aux_status

_core_market_script_path = _data_sync_core_mod._core_market_script_path
_factor_context_script_path = _data_sync_core_mod._factor_context_script_path
_sync_klines_now = _data_sync_sync_mod._sync_klines_now
_sync_quotes_now = _data_sync_sync_mod._sync_quotes_now
_sync_financials_check = _data_sync_sync_mod._sync_financials_check
_sync_market_temperature_snapshot_cache_now = _data_sync_sync_mod._sync_market_temperature_snapshot_cache_now
_sync_core_market_now = _data_sync_sync_mod._sync_core_market_now
_sync_factor_context_now = _data_sync_sync_mod._sync_factor_context_now
_sync_market_text_source_ingest_now = _data_sync_sync_mod._sync_market_text_source_ingest_now
_sync_vector_backfill_market_docs_now = _data_sync_sync_mod._sync_vector_backfill_market_docs_now
_sync_vector_backfill_kline_patterns_now = _data_sync_sync_mod._sync_vector_backfill_kline_patterns_now
_sync_vector_backfill_stock_profiles_now = _data_sync_sync_mod._sync_vector_backfill_stock_profiles_now
_sync_vector_backfill_factor_candidates_now = _data_sync_sync_mod._sync_vector_backfill_factor_candidates_now
_sync_factor_external_research_ingest_now = _data_sync_sync_mod._sync_factor_external_research_ingest_now
_sync_vector_build_snapshot_now = _data_sync_sync_mod._sync_vector_build_snapshot_now
_sync_vector_benchmark_collection_now = _data_sync_sync_mod._sync_vector_benchmark_collection_now
_sync_vector_optimize_bootstrap_now = _data_sync_sync_mod._sync_vector_optimize_bootstrap_now
_sync_factor_validation_bootstrap_now = _data_sync_sync_mod._sync_factor_validation_bootstrap_now


def _sync_data_sync_support_overrides() -> None:
    """Keep core/sync support modules aligned with top-level monkeypatches."""
    _data_sync_core_mod.get_db = get_db
    _data_sync_sync_mod.get_db = get_db

    _data_sync_core_mod._sync_klines_now = _sync_klines_now
    _data_sync_core_mod._sync_quotes_now = _sync_quotes_now
    _data_sync_core_mod._sync_financials_check = _sync_financials_check
    _data_sync_core_mod._sync_market_temperature_snapshot_cache_now = _sync_market_temperature_snapshot_cache_now
    _data_sync_core_mod._sync_core_market_now = _sync_core_market_now
    _data_sync_core_mod._sync_factor_context_now = _sync_factor_context_now
    _data_sync_core_mod._sync_market_text_source_ingest_now = _sync_market_text_source_ingest_now
    _data_sync_core_mod._sync_vector_backfill_market_docs_now = _sync_vector_backfill_market_docs_now
    _data_sync_core_mod._sync_vector_backfill_kline_patterns_now = _sync_vector_backfill_kline_patterns_now
    _data_sync_core_mod._sync_vector_backfill_stock_profiles_now = _sync_vector_backfill_stock_profiles_now
    _data_sync_core_mod._sync_vector_backfill_factor_candidates_now = _sync_vector_backfill_factor_candidates_now
    _data_sync_core_mod._sync_factor_external_research_ingest_now = _sync_factor_external_research_ingest_now
    _data_sync_core_mod._sync_vector_build_snapshot_now = _sync_vector_build_snapshot_now
    _data_sync_core_mod._sync_vector_benchmark_collection_now = _sync_vector_benchmark_collection_now
    _data_sync_core_mod._sync_vector_optimize_bootstrap_now = _sync_vector_optimize_bootstrap_now
    _data_sync_core_mod._sync_factor_validation_bootstrap_now = _sync_factor_validation_bootstrap_now

    _data_sync_sync_mod._as_bool = _as_bool
    _data_sync_sync_mod._load_market_aux_status = _load_market_aux_status
    _data_sync_sync_mod._core_market_script_path = _core_market_script_path
    _data_sync_sync_mod._factor_context_script_path = _factor_context_script_path


async def run_runtime_data_warmup(*args, **kwargs):
    """Run runtime warmup with top-level monkeypatches propagated to support modules."""
    _sync_data_sync_support_overrides()
    return await _data_sync_core_mod.run_runtime_data_warmup(*args, **kwargs)

def register_data_sync_manager(mcp):
    """注册数据同步管理器工具"""
    
    @mcp.tool()
    async def data_sync_manager(action: str, params: dict | None = None, kwargs: Any = None, codes: list[str] | None = None, task_id: str | None = None, task_type: str | None = None, period: str | None = None, status: str | None = None, schedule: str | None = None, force: bool | None = None, limit: int | None = None, priority: str | None = None):
        """数据同步管理器（统一 action + kwargs 协议）

        Args:
            action (str, required): 操作类型，可选 help/status/sync/get_task/list_tasks/cancel_task/schedule
            kwargs: JSON 字符串或关键字参数，不同 action 所需参数:
                - help: 无需额外参数
                - status: 无需额外参数（查看同步状态）
                - sync: codes(list[str]), period(str, optional)
                - get_task: task_id(str)
                - list_tasks: status(str, optional)
                - cancel_task: task_id(str)
                - schedule: codes(list[str]), cron(str, optional)

        Returns:
            dict: {"success": bool, "data": {...}, "error": str|None}

        Examples:
            # 查看帮助
            data_sync_manager(action="help", kwargs="{}")
            # 查看同步状态
            data_sync_manager(action="status", kwargs="{}")
            # 同步指定股票K线
            data_sync_manager(action="sync", kwargs='{"codes":["600519","000001"],"period":"daily"}')
            # 列出同步任务
            data_sync_manager(action="list_tasks", kwargs="{}")
        """
        try:
            _sync_data_sync_support_overrides()
            db = get_db()
            kwargs = normalize_manager_payload(
                params=params,
                kwargs=kwargs,
                extra={
                    "codes": codes,
                    "task_id": task_id,
                    "task_type": task_type,
                    "period": period,
                    "status": status,
                    "schedule": schedule,
                    "force": force,
                    "limit": limit,
                    "priority": priority,
                },
            )

            if action == 'list':
                action = 'list_tasks'

            if action == 'help':
                return ok({
                    'supported_actions': {
                        'status': '数据同步状态',
                        'sync': '执行数据同步（K线/财务需 codes；core_market/market_text_source_ingest/factor_external_research_ingest/vector_optimize_bootstrap/factor_validation_bootstrap/vector_backfill_market_docs/vector_backfill_kline_patterns/vector_backfill_stock_profiles/vector_backfill_factor_candidates/vector_build_snapshot/vector_benchmark_collection 可直接运行）',
                        'get_task': '获取任务详情（需要 task_id）',
                        'list_tasks': '列出同步任务',
                        'cancel_task': '取消任务（需要 task_id）',
                        'schedule': '创建调度任务',
                        'list_schedules': '列出已登记调度',
                        'cancel_schedule': '禁用调度任务（需要 schedule_id）',
                        'run_due_schedules': '执行到期调度（force=true 可强制执行）',
                        'help': '显示帮助信息',
                    }
                })
            
            elif action == 'status':
                async with db.acquire() as conn:
                    async def _max_ts(c, table_cols):
                        for table, col in table_cols:
                            try:
                                return await c.fetchval(f"SELECT MAX({col}) FROM {table}")
                            except Exception:
                                continue
                        return None
                    kline_sync = await _max_ts(conn, [('kline_1d', 'updated_at'), ('kline_1d', 'time')])
                    quote_sync = await _max_ts(conn, [('stock_quotes', 'updated_at'), ('stock_quotes', 'time')])
                    financial_sync = await _max_ts(conn, [('financials', 'updated_at'), ('financials', 'report_date')])
                    pending_tasks = await conn.fetchval(
                        "SELECT COUNT(*) FROM sync_tasks WHERE status = 'pending'"
                    ) or 0
                    running_tasks = await conn.fetchval(
                        "SELECT COUNT(*) FROM sync_tasks WHERE status = 'running'"
                    ) or 0
                    due_schedule_count = await conn.fetchval(
                        """
                        SELECT COUNT(*)
                        FROM sync_schedules
                        WHERE enabled = true
                          AND (next_run IS NULL OR next_run <= CURRENT_TIMESTAMP)
                        """
                    ) or 0
                    next_schedule_run = await conn.fetchval(
                        """
                        SELECT MIN(next_run)
                        FROM sync_schedules
                        WHERE enabled = true AND next_run IS NOT NULL
                        """
                    )
                    quote_ttl = quote_max_stale_seconds()
                    quote_fresh_cutoff = datetime.now() - timedelta(seconds=quote_ttl)
                    stock_count = await conn.fetchval("SELECT COUNT(*) FROM stocks") or 0
                    quote_count = await conn.fetchval("SELECT COUNT(*) FROM stock_quotes") or 0
                    quote_unique_codes = await conn.fetchval("SELECT COUNT(DISTINCT code) FROM stock_quotes") or 0
                    fresh_quote_count = await conn.fetchval(
                        """
                        SELECT COUNT(DISTINCT code)
                        FROM stock_quotes
                        WHERE time >= $1 OR updated_at >= $1
                        """,
                        quote_fresh_cutoff,
                    ) or 0
                    stale_quote_count = max(int(quote_unique_codes) - int(fresh_quote_count), 0)

                    # P0-1 fix: critical_table_stale_alerts
                    # 监控关键数据表的 max_date 是否过期(北向资金/融资融券/龙虎榜/指数)
                    # 任何一张 stale > 阈值都 emit alert,让 AI Agent 和监控系统看到底层数据问题
                    # P3-B6 fix: 修正 column 名 + 表名(对话式复测发现 5/5 query_failed)
                    # 历史问题:date / period 列名错误,dragon_tiger_list 表不存在
                    # 真实 schema:
                    #   north_fund_flow.trade_date / north_fund_holding.trade_date
                    #   margin_market_flow.trade_date / margin_detail.trade_date
                    #   dragon_tiger.trade_date(无 _list 后缀)/ kline_1d.time
                    critical_tables = [
                        # (table, date_col, max_stale_days, severity_threshold_days)
                        ('north_fund_flow', 'trade_date', 7, 30),
                        ('north_fund_holding', 'trade_date', 14, 60),
                        ('margin_market_flow', 'trade_date', 3, 14),
                        ('margin_detail', 'trade_date', 3, 14),
                        ('dragon_tiger', 'trade_date', 3, 14),
                        ('kline_1d', 'time', 2, 14),  # 个股 K 线 (跳过周末为 ~3 天)
                    ]
                    critical_table_alerts: list[dict] = []
                    for table, col, warn_days, high_days in critical_tables:
                        # P3-B6: 先校验表是否存在,避免传播 OperationalError
                        try:
                            tbl_check = await conn.fetchval(
                                "SELECT name FROM sqlite_master WHERE type='table' AND name = $1",
                                table,
                            )
                        except Exception:
                            tbl_check = None
                        if not tbl_check:
                            critical_table_alerts.append({
                                'table': table,
                                'severity': 'unknown',
                                'reason': 'table_missing',
                                'max_date': None,
                                'days_stale': None,
                            })
                            continue
                        try:
                            max_d = await conn.fetchval(f"SELECT MAX({col}) FROM {table}")
                            if max_d is None:
                                critical_table_alerts.append({
                                    'table': table,
                                    'severity': 'high',
                                    'reason': 'no_data',
                                    'max_date': None,
                                    'days_stale': None,
                                })
                                continue
                            now = datetime.now()
                            if hasattr(max_d, 'date'):
                                last_dt = max_d
                            elif isinstance(max_d, str):
                                try:
                                    last_dt = datetime.fromisoformat(max_d.replace('Z', '+00:00'))
                                except Exception:
                                    last_dt = datetime.strptime(max_d[:10], '%Y-%m-%d')
                            else:
                                last_dt = now  # 无法解析,跳过
                            try:
                                if hasattr(last_dt, 'tzinfo') and last_dt.tzinfo is not None:
                                    last_dt = last_dt.replace(tzinfo=None)
                                days_stale = max(0, (now - last_dt).days)
                            except Exception:
                                days_stale = 0
                            if days_stale > high_days:
                                severity = 'high'
                            elif days_stale > warn_days:
                                severity = 'warning'
                            else:
                                continue  # 不报警
                            critical_table_alerts.append({
                                'table': table,
                                'severity': severity,
                                'max_date': str(max_d),
                                'days_stale': int(days_stale),
                                'threshold_warn_days': warn_days,
                                'threshold_high_days': high_days,
                            })
                        except Exception as exc:
                            critical_table_alerts.append({
                                'table': table,
                                'severity': 'unknown',
                                'reason': f'query_failed:{type(exc).__name__}',
                                'max_date': None,
                                'days_stale': None,
                            })
                def _ts_iso(ts):
                    if ts is None:
                        return None
                    return ts.isoformat() if hasattr(ts, 'isoformat') else str(ts)
                return ok({
                    'last_sync': {
                        'kline': _ts_iso(kline_sync),
                        'quote': _ts_iso(quote_sync),
                        'financial': _ts_iso(financial_sync),
                    },
                    'market_aux': await _load_market_aux_status(db),
                    'status': 'running' if running_tasks > 0 else 'idle',
                    'pending_tasks': int(pending_tasks),
                    'running_tasks': int(running_tasks),
                    'due_schedule_count': int(due_schedule_count),
                    'next_schedule_run': _ts_iso(next_schedule_run),
                    'quote_snapshot': {
                        'row_count': int(quote_count),
                        'covered_code_count': int(quote_unique_codes),
                        'fresh_code_count': int(fresh_quote_count),
                        'stale_code_count': int(stale_quote_count),
                        'universe_stock_count': int(stock_count),
                        'coverage_ratio': round((int(quote_unique_codes) / int(stock_count)), 4) if int(stock_count) > 0 else None,
                        'freshness_ttl_seconds': quote_ttl,
                        'latest_quote_time': _ts_iso(quote_sync),
                    },
                    'critical_table_stale_alerts': critical_table_alerts,
                    'critical_alerts_summary': {
                        'total': len(critical_table_alerts),
                        'high': sum(1 for a in critical_table_alerts if a.get('severity') == 'high'),
                        'warning': sum(1 for a in critical_table_alerts if a.get('severity') == 'warning'),
                        'unknown': sum(1 for a in critical_table_alerts if a.get('severity') == 'unknown'),
                    },
                })
            
            elif action == 'sync':
                task_type = kwargs.get('type') or kwargs.get('task_type') or 'kline'
                codes = _normalize_codes(kwargs.get('codes') or kwargs.get('stock_codes'))
                priority = kwargs.get('priority', 'normal')

                if task_type not in {
                    'core_market',
                    'factor_context',
                    'market_temperature_snapshot_cache',
                    'market_text_source_ingest',
                    'vector_backfill_market_docs',
                    'vector_backfill_kline_patterns',
                    'vector_backfill_stock_profiles',
                    'vector_backfill_factor_candidates',
                    'factor_external_research_ingest',
                    'vector_build_snapshot',
                    'vector_benchmark_collection',
                    'vector_optimize_bootstrap',
                    'factor_validation_bootstrap',
                } and not codes:
                    return fail('需要提供codes参数')
                payload = _build_task_payload(task_type, codes, kwargs)
                result = await _execute_sync_task(
                    db,
                    task_type=task_type,
                    codes=codes,
                    priority=str(priority),
                    payload=payload,
                )
                return ok(result)
            
            elif action == 'get_task':
                task_id = kwargs.get('task_id')

                if not task_id:
                    return fail('需要提供task_id参数')

                if str(task_id).startswith('schedule_'):
                    async with db.acquire() as conn:
                        schedule_row = await conn.fetchrow(
                            "SELECT * FROM sync_schedules WHERE schedule_id = $1",
                            str(task_id),
                        )
                    if not schedule_row:
                        return fail(f'璋冨害浠诲姟涓嶅瓨鍦? {task_id}')
                    schedule_data = dict(schedule_row)
                    schedule_data['params'] = _decode_json_obj(schedule_data.get('params'))
                    schedule_data['target_type'] = 'schedule'
                    schedule_data['task_id'] = str(task_id)
                    schedule_data.setdefault('schedule_id', str(task_id))
                    schedule_data.setdefault(
                        'status',
                        'scheduled' if _as_bool(schedule_data.get('enabled'), True) else 'cancelled',
                    )
                    return ok(schedule_data)

                async with db.acquire() as conn:
                    task = await conn.fetchrow(
                        "SELECT * FROM sync_tasks WHERE task_id = $1",
                        task_id
                    )
                    
                    if not task:
                        return fail(f'未找到任务: {task_id}')
                    
                    task_data = dict(task)

                return ok(task_data)
            
            elif action == 'list_tasks':
                status = kwargs.get('status')
                limit = kwargs.get('limit', 20)

                async with db.acquire() as conn:
                    if status:
                        rows = await conn.fetch(
                            "SELECT * FROM sync_tasks WHERE status = $1 ORDER BY created_at DESC LIMIT $2",
                            status, limit
                        )
                    else:
                        rows = await conn.fetch(
                            "SELECT * FROM sync_tasks ORDER BY created_at DESC LIMIT $1",
                            limit
                        )
                    
                    tasks = [dict(row) for row in rows]
                
                # P3-5.5 fix: codes=空且非全市场任务标 status='skipped'(诊断报告 §5.5)
                # 历史问题:codes 为空时 sync 直接 fail,但若历史已落库 task 在 list_tasks 中无法识别
                # 修复:list_tasks 出来后,对 codes 字段空的非全市场 task 派生 derived_status='skipped'
                _full_market_tasks = {
                    'core_market', 'factor_context', 'market_text_source_ingest',
                    'vector_backfill_market_docs', 'vector_backfill_kline_patterns',
                    'vector_backfill_stock_profiles', 'vector_backfill_factor_candidates',
                    'factor_external_research_ingest', 'vector_build_snapshot',
                    'vector_benchmark_collection', 'vector_optimize_bootstrap',
                    'factor_validation_bootstrap',
                }
                skipped_count = 0
                for task in tasks:
                    task_type = str(task.get('task_type') or task.get('type') or '')
                    raw_codes = task.get('codes') or task.get('stock_codes') or []
                    has_codes = False
                    if isinstance(raw_codes, list):
                        has_codes = any(str(c or '').strip() for c in raw_codes)
                    elif isinstance(raw_codes, str):
                        has_codes = bool(raw_codes.strip())
                    if not has_codes and task_type not in _full_market_tasks:
                        task['derived_status'] = 'skipped'
                        task['skip_reason'] = 'codes_empty_for_per_stock_task'
                        skipped_count += 1

                return ok({
                    'tasks': tasks,
                    'count': len(tasks),
                    'skipped_count': skipped_count,
                })
            
            elif action == 'cancel_task':
                task_id = kwargs.get('task_id')
                
                if not task_id:
                    return fail('需要提供task_id参数')

                if str(task_id).startswith('schedule_'):
                    async with db.acquire() as conn:
                        try:
                            result = await conn.execute(
                                "UPDATE sync_schedules SET enabled = false, updated_at = CURRENT_TIMESTAMP WHERE schedule_id = $1",
                                str(task_id),
                            )
                        except Exception:
                            result = await conn.execute(
                                "UPDATE sync_schedules SET enabled = false WHERE schedule_id = $1",
                                str(task_id),
                            )
                    if result == 'UPDATE 0':
                        return fail(f'调度任务不存在: {task_id}')
                    return ok({
                        'task_id': task_id,
                        'schedule_id': task_id,
                        'status': 'cancelled',
                        'enabled': False,
                        'target_type': 'schedule',
                    })
                
                async with db.acquire() as conn:
                    try:
                        result = await conn.execute(
                            "UPDATE sync_tasks SET status = 'cancelled', updated_at = CURRENT_TIMESTAMP WHERE task_id = $1 AND status IN ('pending', 'running')",
                            task_id
                        )
                    except Exception:
                        result = await conn.execute(
                            "UPDATE sync_tasks SET status = 'cancelled' WHERE task_id = $1 AND status IN ('pending', 'running')",
                            task_id
                        )
                    if result == 'UPDATE 0':
                        return fail('任务不存在或无法取消（已完成或已失败）')
                
                return ok({
                    'task_id': task_id,
                    'status': 'cancelled',
                })

            elif action == 'cancel_schedule':
                schedule_id = kwargs.get('schedule_id') or kwargs.get('task_id')
                if not schedule_id:
                    return fail('需要提供 schedule_id 参数')
                # P3-5.6 fix: 支持 hard_delete=true 物理删除(诊断报告 §5.6)
                # 历史问题:cancel_schedule 仅 enabled=false,sync_schedules 表不断膨胀,运维难清理
                hard_delete = _as_bool(kwargs.get('hard_delete', False), False)
                async with db.acquire() as conn:
                    if hard_delete:
                        result = await conn.execute(
                            "DELETE FROM sync_schedules WHERE schedule_id = $1",
                            str(schedule_id),
                        )
                    else:
                        try:
                            result = await conn.execute(
                                "UPDATE sync_schedules SET enabled = false, updated_at = CURRENT_TIMESTAMP WHERE schedule_id = $1",
                                str(schedule_id),
                            )
                        except Exception:
                            result = await conn.execute(
                                "UPDATE sync_schedules SET enabled = false WHERE schedule_id = $1",
                                str(schedule_id),
                            )
                # 兼容 UPDATE 0 / DELETE 0 形式的 result
                empty_marker = 'DELETE 0' if hard_delete else 'UPDATE 0'
                if result == empty_marker:
                    return fail(f'调度任务不存在: {schedule_id}')
                return ok({
                    'schedule_id': str(schedule_id),
                    'status': 'deleted' if hard_delete else 'cancelled',
                    'enabled': False,
                    'hard_deleted': hard_delete,
                    'target_type': 'schedule',
                })
            
            elif action == 'schedule':
                task_type = kwargs.get('type') or kwargs.get('task_type') or 'kline'
                codes = _normalize_codes(kwargs.get('codes') or kwargs.get('stock_codes'))
                schedule = str(kwargs.get('schedule', 'daily')).strip().lower()
                enabled = _as_bool(kwargs.get('enabled', True), True)
                
                if task_type not in {
                    'core_market',
                    'factor_context',
                    'market_temperature_snapshot_cache',
                    'market_text_source_ingest',
                    'vector_backfill_market_docs',
                    'vector_backfill_kline_patterns',
                    'vector_backfill_stock_profiles',
                    'vector_backfill_factor_candidates',
                    'factor_external_research_ingest',
                    'vector_build_snapshot',
                    'vector_benchmark_collection',
                    'vector_optimize_bootstrap',
                    'factor_validation_bootstrap',
                } and not codes:
                    return fail('需要提供codes参数')
                
                # P3-5.6 fix: 创建去重(诊断报告 §5.6)
                # 历史问题:重复调用 schedule 同 task_type+codes 会创建多个 schedule_id,运维难清理
                # 修复:先检查是否存在同 task_type + codes_signature 的 enabled schedule,有则更新而非新建
                codes_signature = ",".join(sorted(codes or []))
                # FIX-15: 去重比较改为 Python 端，避免 PostgreSQL 专有的数组拼接函数
                # 在 SQLite 上报 'no such function'。codes 列的序列化形式（list / JSON /
                # 逗号串）统一交给 _normalize_codes 解码后再比较 signature，跨方言安全。
                existing = None
                async with db.acquire() as conn:
                    candidate_rows = await conn.fetch(
                        """
                        SELECT schedule_id, codes FROM sync_schedules
                        WHERE task_type = $1 AND enabled = true
                        ORDER BY created_at DESC
                        """,
                        task_type,
                    ) if hasattr(conn, 'fetch') else []
                for row in candidate_rows or []:
                    row_codes = _normalize_codes(row["codes"] if "codes" in row.keys() else None)
                    if ",".join(sorted(row_codes)) == codes_signature:
                        existing = row
                        break

                if existing:
                    # 已存在同任务,更新 schedule + next_run
                    next_run = _compute_next_run(schedule)
                    params = _build_schedule_params(task_type, kwargs, codes)
                    schedule_id = str(existing['schedule_id'])
                    async with db.acquire() as conn:
                        await conn.execute(
                            """
                            UPDATE sync_schedules
                            SET schedule = $2, params = $3, enabled = $4, next_run = $5,
                                updated_at = CURRENT_TIMESTAMP
                            WHERE schedule_id = $1
                            """,
                            schedule_id,
                            schedule,
                            json.dumps(params, ensure_ascii=False, default=str),
                            enabled,
                            next_run,
                        )
                    return ok({
                        'schedule_id': schedule_id,
                        'task_type': task_type,
                        'schedule': schedule,
                        'codes_count': len(codes),
                        'enabled': enabled,
                        'params': params,
                        'next_run': next_run.isoformat(),
                        'reused_existing': True,
                        'message': f'已存在同 task_type + codes 的 schedule_id={schedule_id},已更新而非新建',
                    })

                schedule_id = f'schedule_{task_type}_{int(datetime.now().timestamp())}'
                next_run = _compute_next_run(schedule)
                params = _build_schedule_params(task_type, kwargs, codes)
                
                async with db.acquire() as conn:
                    await conn.execute(
                        """
                        INSERT INTO sync_schedules (schedule_id, task_type, codes, schedule, params, enabled, next_run, created_at)
                        VALUES ($1, $2, $3, $4, $5, $6, $7, CURRENT_TIMESTAMP)
                        """,
                        schedule_id,
                        task_type,
                        codes,
                        schedule,
                        json.dumps(params, ensure_ascii=False, default=str),
                        enabled,
                        next_run,
                    )
                
                return ok({
                    'schedule_id': schedule_id,
                    'task_type': task_type,
                    'schedule': schedule,
                    'codes_count': len(codes),
                    'enabled': enabled,
                    'params': params,
                    'next_run': next_run.isoformat(),
                    'reused_existing': False,
                })

            elif action == 'list_schedules':
                enabled = kwargs.get('enabled')
                schedule_id = kwargs.get('schedule_id')
                limit = int(kwargs.get('limit', 20) or 20)

                async with db.acquire() as conn:
                    if schedule_id and enabled is None:
                        rows = await conn.fetch(
                            "SELECT * FROM sync_schedules WHERE schedule_id = $1 ORDER BY created_at DESC LIMIT $2",
                            str(schedule_id),
                            limit,
                        )
                    elif schedule_id and enabled is not None:
                        rows = await conn.fetch(
                            "SELECT * FROM sync_schedules WHERE schedule_id = $1 AND enabled = $2 ORDER BY created_at DESC LIMIT $3",
                            str(schedule_id),
                            _as_bool(enabled),
                            limit,
                        )
                    elif enabled is None:
                        rows = await conn.fetch(
                            "SELECT * FROM sync_schedules ORDER BY created_at DESC LIMIT $1",
                            limit,
                        )
                    else:
                        rows = await conn.fetch(
                            "SELECT * FROM sync_schedules WHERE enabled = $1 ORDER BY created_at DESC LIMIT $2",
                            _as_bool(enabled),
                            limit,
                        )

                schedules = []
                for row in rows:
                    item = dict(row)
                    item['params'] = _decode_json_obj(item.get('params'))
                    schedules.append(item)

                return ok({
                    'schedules': schedules,
                    'count': len(schedules),
                })

            elif action == 'run_due_schedules':
                force = _as_bool(kwargs.get('force', False), False)
                schedule_id = kwargs.get('schedule_id')
                task_type = kwargs.get('task_type')
                limit = int(kwargs.get('limit', 20) or 20)
                result = await _run_due_schedules(
                    db,
                    force=force,
                    limit=limit,
                    schedule_id=str(schedule_id).strip() if schedule_id else None,
                    task_type=str(task_type).strip() if task_type else None,
                )
                return ok(result)
            
            else:
                return fail(f'Unknown action: {action}. Supported: help, status, sync, get_task, list_tasks, cancel_task, schedule, list_schedules, cancel_schedule, run_due_schedules')
        except Exception as e:
            return fail(str(e))
