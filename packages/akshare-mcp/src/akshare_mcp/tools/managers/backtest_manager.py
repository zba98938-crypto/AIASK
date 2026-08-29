"""回测管理器 - 运行、保存、查询回测结果（增强版）"""

from typing import Any
import uuid
import json
import time
from datetime import date, datetime, timezone
from ...storage import get_db
from ...utils import normalize_code, parse_date_input
from ...services.cost_model import build_cost_model
from ...services.artifact_registry import register_artifact
from ...services.signal_dsl import build_signal_definition
from ..manager_protocol import (
    fail_with_meta,
    normalize_manager_code,
    normalize_manager_kwargs,
    normalize_manager_payload,
    ok_with_meta,
)
import logging

logger = logging.getLogger(__name__)


def _safe_json_dumps(obj: dict) -> str:
    try:
        return json.dumps(obj, ensure_ascii=False)
    except Exception:
        return str(obj)


def _build_unified_cost_model(kwargs: dict, initial_capital: float, klines: list[dict]) -> dict:
    """构建回测侧统一成本口径（统一委托到 services.cost_model）。"""
    last_close = 0.0
    if klines:
        try:
            last_close = float((klines[-1] or {}).get("close") or 0.0)
        except Exception:
            last_close = 0.0

    return build_cost_model(
        kwargs,
        notional=float(initial_capital),
        default_mode="backtest",
        reference_price_fallback=last_close,
    )


def _build_strategy_artifact(
    code: str,
    strategy: str,
    params: dict,
    kwargs: dict,
    klines: list[dict],
    initial_capital: float,
) -> dict:
    """构建最小可用策略工件（P0）。"""
    artifact_id = str(kwargs.get("artifact_id") or f"art_{uuid.uuid4().hex[:12]}")
    strategy_version = str(kwargs.get("strategy_version") or f"{strategy}_v1")

    def _safe_date_str(val) -> str:
        if isinstance(val, date):
            return val.isoformat()
        if val is None:
            return ""
        return str(val)

    start_date = _safe_date_str((klines[0] or {}).get("date") if klines else "")
    end_date = _safe_date_str((klines[-1] or {}).get("date") if klines else "")

    return {
        "artifact_id": artifact_id,
        "strategy_version": strategy_version,
        "code": code,
        "strategy": strategy,
        "params": params,
        "signal_definition": build_signal_definition(strategy=strategy, params=params),
        "data_window": {
            "period": "daily",
            "start_date": start_date,
            "end_date": end_date,
            "data_points": len(klines or []),
        },
        "cost_model": _build_unified_cost_model(kwargs, initial_capital, klines),
        "risk_evidence": {
            "min_required_bars": 50,
            "actual_bars": len(klines or []),
            "benchmark": str(params.get("benchmark") or ""),
            "checks": [
                {"name": "kline_length", "passed": len(klines or []) >= 50},
            ],
        },
        "evidence_refs": [
            "tools/managers/backtest_manager.py:run",
            "tools/managers/execution_manager.py:_build_cost_model",
        ],
        "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }


def _normalize_kwargs(kwargs: dict) -> dict:
    return normalize_manager_kwargs(kwargs)


def _dedupe_chain(values: list[str]) -> list[str]:
    chain = []
    seen = set()
    for value in values:
        label = str(value or "").strip()
        if not label or label in seen:
            continue
        chain.append(label)
        seen.add(label)
    return chain


def _normalize_date_arg(value) -> str | None:
    if value in (None, ""):
        return None
    parsed = parse_date_input(str(value).strip())
    return parsed.isoformat() if parsed else None


def _normalize_klines(rows: list[dict] | None) -> list[dict]:
    normalized: list[dict] = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        raw_date = row.get("date") or row.get("trade_date") or row.get("time") or row.get("datetime")
        parsed = None
        if isinstance(raw_date, datetime):
            parsed = raw_date.date()
        elif isinstance(raw_date, date):
            parsed = raw_date
        elif raw_date:
            parsed = parse_date_input(str(raw_date)[:10])
        normalized.append(
            {
                **row,
                "date": parsed.isoformat() if parsed else str(raw_date or "")[:10],
            }
        )
    normalized.sort(key=lambda item: str(item.get("date") or ""))
    return normalized


def _filter_klines_by_window(
    rows: list[dict] | None,
    start_date: str | None,
    end_date: str | None,
) -> list[dict]:
    normalized = _normalize_klines(rows)
    if not start_date and not end_date:
        return normalized

    filtered: list[dict] = []
    for row in normalized:
        row_date = str(row.get("date") or "")[:10]
        if not row_date:
            continue
        if start_date and row_date < start_date:
            continue
        if end_date and row_date > end_date:
            continue
        filtered.append(row)
    return filtered


def _estimate_limit_from_window(start_date: str | None, end_date: str | None, default: int = 250) -> int:
    start = parse_date_input(start_date) if start_date else None
    end = parse_date_input(end_date) if end_date else None
    if start and end:
        days = abs((end - start).days) + 1
        return min(max(days + 30, 60), 1000)
    return max(50, int(default or 250))


async def _get_db_klines_compatible(
    db,
    code: str,
    *,
    start_date: str | None,
    end_date: str | None,
    limit: int | None,
) -> list[dict]:
    try:
        return await db.get_klines(code, start_date=start_date, end_date=end_date, limit=limit)
    except TypeError:
        if start_date or end_date:
            try:
                return await db.get_klines(code, start_date, end_date, limit)
            except TypeError:
                try:
                    return await db.get_klines(code, limit=limit)
                except TypeError:
                    return await db.get_klines(code, limit)
        try:
            return await db.get_klines(code, limit=limit)
        except TypeError:
            return await db.get_klines(code, limit)


def _safe_float_metric(payload: dict, key: str, default: float = 0.0) -> float:
    try:
        value = payload.get(key, default)
        if value is None:
            return float(default)
        return float(value)
    except Exception:
        return float(default)


def _safe_int_metric(payload: dict, key: str, default: int = 0) -> int:
    try:
        value = payload.get(key, default)
        if value is None:
            return int(default)
        return int(value)
    except Exception:
        return int(default)


def _safe_date_value(value) -> date:
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    parsed = _normalize_date_arg(value)
    if parsed:
        return datetime.strptime(parsed, "%Y-%m-%d").date()
    return date.today()


async def _insert_backtest_result(
    conn,
    *,
    backtest_id: str,
    code: str,
    strategy: str,
    params_payload: str,
    start_dt: date,
    end_dt: date,
    initial_capital: float,
    result: dict,
) -> None:
    await conn.execute(
        """INSERT INTO backtest_results
           (id, code, strategy, params, start_date, end_date, initial_capital, final_capital,
            total_return, annual_return, max_drawdown, sharpe_ratio, sortino_ratio, win_rate,
            trades_count, created_at)
           VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, CURRENT_TIMESTAMP)""",
        backtest_id,
        code,
        strategy,
        params_payload,
        start_dt,
        end_dt,
        float(initial_capital),
        _safe_float_metric(result, 'final_capital', initial_capital),
        _safe_float_metric(result, 'total_return', 0.0),
        _safe_float_metric(result, 'annual_return', 0.0),
        _safe_float_metric(result, 'max_drawdown', 0.0),
        _safe_float_metric(result, 'sharpe_ratio', 0.0),
        _safe_float_metric(result, 'sortino_ratio', 0.0),
        _safe_float_metric(result, 'win_rate', 0.0),
        _safe_int_metric(result, 'trades_count', 0),
    )


def register_backtest_manager(mcp):
    """注册回测管理器工具"""
    
    @mcp.tool()
    async def backtest_manager(action: str, params: dict | None = None, kwargs: Any = None):
        """回测管理器（统一 action + kwargs 协议）

        Args:
            action (str, required): 操作类型，可选 help/run/save/list/get/compare
            kwargs: 支持 structured ``params``、JSON 字符串 ``kwargs`` 或关键字参数，不同 action 所需参数:
                - help: 无需额外参数
                - run: code(str), strategy(str, "ma_cross"等), start_date(str, optional), end_date(str, optional), artifact_id(str, optional)
                - save: backtest_id(str), name(str, optional)
                - list: limit(int, optional)
                - get: backtest_id(str)
                - compare: backtest_ids(list[str])

        Returns:
            dict: {"success": bool, "data": {...}, "error": str|None}

        Examples:
            # 查看帮助
            backtest_manager(action="help", kwargs="{}")
            # 运行均线交叉回测（返回 artifact_id）
            backtest_manager(action="run", kwargs='{"code":"600519","strategy":"ma_cross","short_period":5,"long_period":20}')
            # 指定 artifact_id（便于跨 manager 追踪）
            backtest_manager(action="run", kwargs='{"code":"000001","strategy":"momentum","artifact_id":"art_demo_001"}')
            # 列出回测记录
            backtest_manager(action="list", kwargs='{"limit":10}')
            # 对比多个回测
            backtest_manager(action="compare", kwargs='{"backtest_ids":["abc123","def456"]}')
        """
        start_time = time.perf_counter()
        try:
            db = get_db()
            kwargs = normalize_manager_payload(params=params, kwargs=kwargs)
            code, kwargs = normalize_manager_code(None, kwargs)
            if code:
                kwargs["code"] = code

            def _ok(data: dict, source_chain=None):
                return ok_with_meta(
                    data,
                    tool_name="backtest_manager",
                    action=action,
                    started_at=start_time,
                    source_chain=source_chain,
                )

            def _fail(message: str, source_chain=None):
                return fail_with_meta(
                    message,
                    tool_name="backtest_manager",
                    action=action,
                    started_at=start_time,
                    source_chain=source_chain,
                )
            
            if action == 'help':
                return _ok({
                    'supported_actions': {
                        'run': '运行回测（需要 code, 可选 strategy/start_date/end_date；返回 artifact_id）',
                        'save': '保存回测结果',
                        'list': '列出回测记录',
                        'get': '获取回测详情（需要 backtest_id）',
                        'compare': '对比回测（需要 backtest_ids）',
                        'help': '显示帮助信息',
                    }
                }, source_chain=['backtest_manager'])
            
            elif action == 'run':
                code = kwargs.get('code')
                strategy = kwargs.get('strategy', 'ma_cross')
                source_chain = ['backtest_manager']
                
                if not code:
                    return _fail('需要提供股票代码', source_chain=source_chain)
                
                code = normalize_code(code)
                requested_start = kwargs.get('start_date')
                requested_end = kwargs.get('end_date')
                start_date = _normalize_date_arg(requested_start)
                end_date = _normalize_date_arg(requested_end)
                if requested_start and not start_date:
                    return _fail('start_date 格式无效，支持 YYYY-MM-DD / YYYYMMDD / YYYY', source_chain=source_chain)
                if requested_end and not end_date:
                    return _fail('end_date 格式无效，支持 YYYY-MM-DD / YYYYMMDD / YYYY', source_chain=source_chain)
                if start_date and end_date and start_date > end_date:
                    start_date, end_date = end_date, start_date
                
                # 1. 获取K线数据
                limit = _estimate_limit_from_window(start_date, end_date, kwargs.get('limit', 250))
                db_limit = None if (start_date or end_date) else limit
                klines = await _get_db_klines_compatible(
                    db,
                    code,
                    start_date=start_date,
                    end_date=end_date,
                    limit=db_limit,
                )
                if klines:
                    source_chain.append('db.get_klines')
                    klines = _filter_klines_by_window(klines, start_date, end_date)
                
                if not klines or len(klines) < 50:
                    return _fail(
                        f'K线数据不足，无法回测（需要至少50天数据，当前{len(klines) if klines else 0}天）',
                        source_chain=_dedupe_chain(source_chain),
                    )
                
                # 2. 运行回测
                from ...services.backtest import backtest_engine
                
                initial_capital = kwargs.get('initial_capital', 100000)
                commission_rate = kwargs.get('commission_rate', kwargs.get('commission', 0.0003))
                slippage = kwargs.get('slippage', 0.0)
                benchmark_code = (kwargs.get('benchmark') or '000300').strip()

                benchmark_klines = []
                if benchmark_code:
                    benchmark_normalized = normalize_code(benchmark_code)
                    benchmark_klines = await _get_db_klines_compatible(
                        db,
                        benchmark_normalized,
                        start_date=start_date,
                        end_date=end_date,
                        limit=db_limit,
                    )
                    if benchmark_klines:
                        source_chain.append('db.get_klines')
                        benchmark_klines = _filter_klines_by_window(benchmark_klines, start_date, end_date)
                    if not benchmark_klines and hasattr(db, "get_index_klines"):
                        benchmark_klines = await db.get_index_klines(
                            benchmark_code,
                            start_date=start_date,
                            end_date=end_date,
                            limit=db_limit,
                        )
                        if benchmark_klines:
                            source_chain.append('db.get_index_klines')
                            benchmark_klines = _filter_klines_by_window(benchmark_klines, start_date, end_date)

                supported_strategies = ['ma_cross', 'buy_and_hold', 'momentum', 'rsi', 'volatility_breakout', 'event_structure_breakout', 'margin_divergence']
                if strategy not in supported_strategies:
                    return _fail(
                        f'不支持的策略: {strategy}，支持: {", ".join(supported_strategies)}',
                        source_chain=_dedupe_chain(source_chain),
                    )

                params = {
                    'initial_capital': initial_capital,
                    'commission': commission_rate,
                    'slippage': slippage,
                    'benchmark': benchmark_code,
                    'benchmark_klines': benchmark_klines,
                    'short_period': kwargs.get('short_period', 5),
                    'long_period': kwargs.get('long_period', 20),
                    'lookback': kwargs.get('lookback', 20),
                    'threshold': kwargs.get('threshold', 0.02),
                    'rsi_period': kwargs.get('rsi_period', 12),
                    'oversold': kwargs.get('oversold', 18),
                    'overbought': kwargs.get('overbought', 64),
                    'regime_filter_enabled': kwargs.get('regime_filter_enabled', True),
                    'allowed_entry_regimes': kwargs.get('allowed_entry_regimes', ['bear_calm', 'bear_volatile']),
                    'noise_filter_enabled': kwargs.get('noise_filter_enabled', True),
                    'noise_window': kwargs.get('noise_window', 6),
                    'noise_ceiling': kwargs.get('noise_ceiling', 6.0),
                    'repair_confirmation_enabled': kwargs.get('repair_confirmation_enabled', True),
                    'repair_confirmation_window': kwargs.get('repair_confirmation_window', 6),
                    'repair_confirmation_rebound_pct': kwargs.get('repair_confirmation_rebound_pct', 0.008),
                    'repair_confirmation_rsi_reclaim': kwargs.get('repair_confirmation_rsi_reclaim', 24.0),
                    'liquidity_confirmation_enabled': kwargs.get('liquidity_confirmation_enabled', True),
                    'liquidity_window': kwargs.get('liquidity_window', 8),
                    'liquidity_volume_floor_ratio': kwargs.get('liquidity_volume_floor_ratio', 0.8),
                    'structure_confirmation_enabled': kwargs.get('structure_confirmation_enabled', True),
                    'structure_window': kwargs.get('structure_window', 4),
                    'structure_close_location_min': kwargs.get('structure_close_location_min', 0.55),
                    'structure_body_return_min': kwargs.get('structure_body_return_min', 0.0015),
                    'breakout_window': kwargs.get('breakout_window', kwargs.get('lookback', 12 if strategy == 'event_structure_breakout' else 16)),
                    'breakout_buffer_pct': kwargs.get('breakout_buffer_pct', kwargs.get('threshold', 0.002 if strategy == 'event_structure_breakout' else 0.006)),
                    'contraction_window': kwargs.get('contraction_window', 5),
                    'contraction_max_range_ratio': kwargs.get('contraction_max_range_ratio', 0.06 if strategy == 'event_structure_breakout' else 0.045),
                    'volume_window': kwargs.get('volume_window', 8),
                    'breakout_volume_ratio_min': kwargs.get('breakout_volume_ratio_min', 1.0 if strategy == 'event_structure_breakout' else 1.15),
                    'event_impulse_window': kwargs.get('event_impulse_window', 5),
                    'event_impulse_threshold': kwargs.get('event_impulse_threshold', 0.015 if strategy == 'event_structure_breakout' else 0.03),
                    'max_active_symbols': kwargs.get('max_active_symbols', 3 if strategy == 'event_structure_breakout' else 2),
                    'universe_selection_profile': kwargs.get('universe_selection_profile', 'event_structure_breakout_fit_v1' if strategy == 'event_structure_breakout' else 'repair_liquidity_fit_v1'),
                    'mean_reversion_exit_min_hold_bars': kwargs.get('mean_reversion_exit_min_hold_bars', 4),
                    'mean_reversion_exit_buffer': kwargs.get('mean_reversion_exit_buffer', -0.002),
                    'max_hold_bars': kwargs.get('max_hold_bars', 8 if strategy == 'event_structure_breakout' else 6),
                    'breakout_failure_close_buffer': kwargs.get('breakout_failure_close_buffer', -0.012),
                    'adverse_volume_ratio_max': kwargs.get('adverse_volume_ratio_max', 0.85),
                    'adverse_regime_exit_enabled': kwargs.get('adverse_regime_exit_enabled', True),
                    'adverse_exit_regimes': kwargs.get('adverse_exit_regimes', ['range_volatile']),
                    'adverse_noise_ceiling': kwargs.get('adverse_noise_ceiling', 6.0),
                    'fear_threshold': kwargs.get('fear_threshold', 43),
                    'greed_threshold': kwargs.get('greed_threshold', 60),
                    'rebound_window': kwargs.get('rebound_window', 3),
                    'repair_drawdown_floor': kwargs.get('repair_drawdown_floor', -0.06),
                    'repair_rebound_pct': kwargs.get('repair_rebound_pct', 0.012),
                    'dryup_window': kwargs.get('dryup_window', 3),
                    'dryup_max_ratio': kwargs.get('dryup_max_ratio', 0.9),
                    'entry_volume_floor_ratio': kwargs.get('entry_volume_floor_ratio', 1.0),
                    'adverse_volume_break_ratio': kwargs.get('adverse_volume_break_ratio', 0.72),
                    'adverse_close_break_pct': kwargs.get('adverse_close_break_pct', -0.012),
                }

                artifact = _build_strategy_artifact(
                    code=code,
                    strategy=strategy,
                    params=params,
                    kwargs=kwargs,
                    klines=klines,
                    initial_capital=initial_capital,
                )
                artifact_id = artifact['artifact_id']

                params_with_artifact = {
                    **params,
                    'artifact_id': artifact_id,
                    'strategy_version': artifact.get('strategy_version'),
                    'signal_definition': artifact.get('signal_definition', {}),
                    'cost_model': artifact.get('cost_model', {}),
                }

                try:
                    register_artifact(artifact)
                    source_chain.append('services.artifact_registry')
                except Exception as e:
                    logger.warning(f"[BacktestManager] register_artifact failed: {e}")

                result = backtest_engine.run_backtest(
                    code=code,
                    klines=klines,
                    strategy=strategy,
                    params=params_with_artifact,
                )
                source_chain.append('services.backtest.backtest_engine')
                
                if not result.get('success'):
                    return _fail(result.get('error', '回测执行失败'), source_chain=_dedupe_chain(source_chain))
                
                result = result.get('data', result)
                
                # 3. 保存回测结果
                backtest_id = str(uuid.uuid4())[:8]
                
                # 解析日期，兼容 date 对象和字符串
                start_dt = _safe_date_value(klines[0].get('date')) if klines else date.today()
                end_dt = _safe_date_value(klines[-1].get('date')) if klines else date.today()
                
                # 确保 start_date <= end_date（防止K线未排序导致日期反转）
                if start_dt > end_dt:
                    start_dt, end_dt = end_dt, start_dt
                
                async with db.acquire() as conn:
                    await _insert_backtest_result(
                        conn,
                        backtest_id=backtest_id,
                        code=code,
                        strategy=strategy,
                        params_payload=_safe_json_dumps(params_with_artifact),
                        start_dt=start_dt,
                        end_dt=end_dt,
                        initial_capital=initial_capital,
                        result=result,
                    )
                source_chain.append('db.backtest_results')
                
                return _ok({
                    'backtest_id': backtest_id,
                    'artifact_id': artifact_id,
                    'artifact': artifact,
                    'code': code,
                    'strategy': strategy,
                    'cost_model': artifact.get('cost_model', {}),
                    'result': result,
                    'data_points': len(klines)
                }, source_chain=_dedupe_chain(source_chain))
            
            elif action == 'save':
                source_chain = ['backtest_manager', 'db.backtest_results']
                code = kwargs.get('code')
                strategy = kwargs.get('strategy')
                params = kwargs.get('params', {})
                result = kwargs.get('result', {})
                
                backtest_id = str(uuid.uuid4())[:8]
                start_dt = _safe_date_value(result.get('start_date', date.today()))
                end_dt = _safe_date_value(result.get('end_date', date.today()))
                if start_dt > end_dt:
                    start_dt, end_dt = end_dt, start_dt
                
                async with db.acquire() as conn:
                    await _insert_backtest_result(
                        conn,
                        backtest_id=backtest_id,
                        code=code,
                        strategy=strategy,
                        params_payload=_safe_json_dumps(params),
                        start_dt=start_dt,
                        end_dt=end_dt,
                        initial_capital=_safe_float_metric(result, 'initial_capital', 100000.0),
                        result=result,
                    )
                return _ok({'backtest_id': backtest_id}, source_chain=source_chain)
            
            elif action == 'list':
                source_chain = ['backtest_manager', 'db.backtest_results']
                code = kwargs.get('code')
                limit = kwargs.get('limit', 20)
                
                async with db.acquire() as conn:
                    if code:
                        rows = await conn.fetch(
                            "SELECT * FROM backtest_results WHERE code = $1 ORDER BY created_at DESC LIMIT $2",
                            code, limit
                        )
                    else:
                        rows = await conn.fetch(
                            "SELECT * FROM backtest_results ORDER BY created_at DESC LIMIT $1",
                            limit
                        )
                    results = []
                    for row in rows:
                        r = dict(row)
                        # 序列化 date/datetime 对象为字符串
                        for k, v in r.items():
                            if isinstance(v, datetime):
                                r[k] = v.isoformat()
                            elif isinstance(v, date):
                                r[k] = v.isoformat()
                        # 确保 start_date <= end_date
                        sd = r.get('start_date', '')
                        ed = r.get('end_date', '')
                        if sd and ed and str(sd) > str(ed):
                            r['start_date'], r['end_date'] = ed, sd
                        results.append(r)
                
                return _ok({'results': results, 'count': len(results)}, source_chain=source_chain)
            
            elif action == 'get':
                source_chain = ['backtest_manager', 'db.backtest_results']
                backtest_id = kwargs.get('backtest_id')
                async with db.acquire() as conn:
                    row = await conn.fetchrow(
                        "SELECT * FROM backtest_results WHERE id = $1",
                        backtest_id
                    )
                    if not row:
                        return _fail('回测结果不存在', source_chain=source_chain)
                    result = dict(row)
                
                return _ok(result, source_chain=source_chain)
            
            elif action == 'compare':
                source_chain = ['backtest_manager', 'db.backtest_results']
                backtest_ids = kwargs.get('backtest_ids', [])
                if isinstance(backtest_ids, str):
                    try:
                        backtest_ids = json.loads(backtest_ids)
                    except Exception:
                        backtest_ids = [item.strip() for item in backtest_ids.split(',') if item.strip()]
                if not backtest_ids:
                    return _fail('需要提供回测ID列表', source_chain=source_chain)
                
                comparison = []
                async with db.acquire() as conn:
                    for bid in backtest_ids[:5]:  # 限制最多5个
                        row = await conn.fetchrow(
                            "SELECT * FROM backtest_results WHERE id = $1",
                            bid
                        )
                        if row:
                            comparison.append(dict(row))
                
                return _ok({
                    'comparison': comparison,
                    'count': len(comparison)
                }, source_chain=source_chain)
            
            else:
                return _fail(
                    f'Unknown action: {action}. Supported: help, run, save, list, get, compare',
                    source_chain=['backtest_manager'],
                )
        
        except Exception as e:
            logger.error(f"[BacktestManager] Error: {e}")
            return fail_with_meta(
                str(e),
                tool_name='backtest_manager',
                action=action,
                started_at=start_time,
                source_chain=['backtest_manager'],
            )
