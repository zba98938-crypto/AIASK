"""涨停板管理器"""

from typing import Any
from datetime import datetime
import logging
from ...data_source import data_source
import time

from ..manager_protocol import (
    normalize_manager_payload,
    fail_with_meta,
    normalize_manager_kwargs,
    ok_with_meta,
)

logger = logging.getLogger(__name__)


def _normalize_date(raw) -> str:
    """统一日期格式为 YYYYMMDD"""
    return str(raw).replace('-', '').replace('/', '').replace(' ', '')[:8]


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


def register_limit_up_manager(mcp):
    """注册涨停板管理器工具"""
    
    @mcp.tool()
    async def limit_up_manager(action: str, params: dict | None = None, kwargs: Any = None):
        """涨停板管理器（统一 action + kwargs 协议）

        Args:
            action (str, required): 操作类型，可选 help/list/statistics
            kwargs: JSON 字符串或关键字参数，不同 action 所需参数:
                - help: 无需额外参数
                - list: date(str, optional, "YYYY-MM-DD"，默认最近交易日)
                - statistics: date(str, optional)

        Returns:
            dict: {"success": bool, "data": {...}, "error": str|None}

        Examples:
            # 查看帮助
            limit_up_manager(action="help", kwargs="{}")
            # 获取今日涨停板列表
            limit_up_manager(action="list", kwargs="{}")
            # 获取指定日期涨停统计
            limit_up_manager(action="statistics", kwargs='{"date":"2025-01-15"}')
        """
        start_time = time.perf_counter()
        try:
            kwargs = normalize_manager_kwargs(kwargs)

            def _ok(data: dict, source_chain=None, data_timestamp: str | None = None):
                return ok_with_meta(
                    data,
                    tool_name="limit_up_manager",
                    action=action,
                    started_at=start_time,
                    source_chain=source_chain,
                    data_timestamp=data_timestamp,
                )

            def _fail(message: str, source_chain=None, data_timestamp: str | None = None):
                return fail_with_meta(
                    message,
                    tool_name="limit_up_manager",
                    action=action,
                    started_at=start_time,
                    source_chain=source_chain,
                    data_timestamp=data_timestamp,
                )
            
            if action == 'help':
                return _ok({
                    'supported_actions': {
                        'list': '涨停板列表（可选 date，格式 YYYYMMDD 或 YYYY-MM-DD）',
                        'statistics': '涨停统计（可选 date）',
                        'help': '显示帮助信息',
                    }
                }, source_chain=['limit_up_manager'])
            
            elif action == 'list':
                raw_date = kwargs.get('date') or kwargs.get('trade_date') or kwargs.get('Date') or ''
                date = _normalize_date(raw_date) if raw_date else datetime.now().strftime('%Y%m%d')
                request_date = date
                source_chain = ['limit_up_manager']

                # 委托给 get_limit_up_stocks 工具（内部走 Tushare stk_limit）
                limit_up_stocks = []
                try:
                    from ..market.limit_up import get_limit_up_stocks as _get_zt
                    zt_res = _get_zt(date=date)
                    if zt_res.get('success') and zt_res.get('data'):
                        source_chain.extend(zt_res.get('source_chain') or ['market.limit_up.get_limit_up_stocks'])
                        zt_data = zt_res['data']
                        if isinstance(zt_data, dict):
                            zt_items = (
                                zt_data.get('stocks')
                                or zt_data.get('limit_up_stocks')
                                or zt_data.get('data')
                                or []
                            )
                        else:
                            zt_items = zt_data
                        if isinstance(zt_items, list):
                            for item in zt_items:
                                if not isinstance(item, dict):
                                    continue
                                limit_up_stocks.append({
                                    'code': item.get('code', ''),
                                    'name': item.get('name', ''),
                                    'price': float(item.get('price', 0) or 0),
                                    'change_pct': float(item.get('changePercent', item.get('change_pct', 0)) or 0),
                                    'turnover_rate': float(item.get('turnoverRate', item.get('turnover_rate', 0)) or 0),
                                    'reason': item.get('industry', '') or item.get('concept', ''),
                                    'first_limit_up_time': item.get('firstLimitTime', item.get('first_limit_up_time', '')),
                                    'last_limit_up_time': item.get('lastLimitTime', item.get('last_limit_up_time', '')),
                                    'limit_up_count': int(item.get('continuousDays', item.get('limit_up_count', 0)) or 0),
                                })
                except Exception as e:
                    logger.warning(f"[LimitUp] get_limit_up_stocks 失败: {e}")

                # 降级：Tushare stk_limit（也尝试最近10个交易日）
                if not limit_up_stocks:
                    try:
                        ts_pro = data_source.get_tushare_pro()
                        if ts_pro:
                            from datetime import timedelta
                            base = datetime.strptime(date, '%Y%m%d')
                            for days_back in range(10):
                                check_date = (base - timedelta(days=days_back)).strftime('%Y%m%d')
                                try:
                                    df = ts_pro.stk_limit(trade_date=check_date)
                                    if df is not None and not df.empty:
                                        # stk_limit 返回所有涨跌停，筛选涨停
                                        if 'limit' in df.columns:
                                            up_df = df[df['limit'] == 'U']
                                        elif 'pct_chg' in df.columns:
                                            up_df = df[df['pct_chg'].astype(float, errors='ignore') >= 9.5]
                                        else:
                                            up_df = df.head(0)
                                        for _, row in up_df.iterrows():
                                            ts_code = str(row.get('ts_code', ''))
                                            code_val = ts_code.split('.')[0] if ts_code else ''
                                            limit_up_stocks.append({
                                                'code': code_val,
                                                'name': str(row.get('name', '')),
                                                'price': float(row.get('close', 0) or 0),
                                                'change_pct': float(row.get('pct_chg', 0) or 0),
                                                'turnover_rate': 0,
                                                'reason': '',
                                                'first_limit_up_time': '',
                                                'last_limit_up_time': '',
                                                'limit_up_count': 0,
                                            })
                                        if limit_up_stocks:
                                            source_chain.append('tushare.stk_limit')
                                            date = check_date  # 更新为实际有数据的日期
                                            break
                                except Exception:
                                    continue
                    except Exception as e:
                        logger.warning(f"[LimitUp] Tushare stk_limit 失败: {e}")

                if limit_up_stocks:
                    payload = {
                        'request_date': request_date,
                        'date': date,
                        'limit_up_stocks': limit_up_stocks,
                        'count': len(limit_up_stocks),
                        'data_quality': zt_res.get('data_quality') if 'zt_res' in locals() else None,
                        'source_chain': zt_res.get('source_chain') if 'zt_res' in locals() else None,
                        'fallback_reason': zt_res.get('fallback_reason') if 'zt_res' in locals() else None,
                        'degraded': bool(zt_res.get('degraded')) if 'zt_res' in locals() else False,
                    }
                    return _ok(payload, source_chain=_dedupe_chain(source_chain), data_timestamp=date)

                return _ok({
                    'request_date': request_date,
                    'date': date,
                    'limit_up_stocks': [],
                    'count': 0,
                    'message': f'{date} 暂无涨停板数据（可能为非交易日）'
                }, source_chain=_dedupe_chain(source_chain), data_timestamp=date)
            
            elif action == 'statistics':
                raw_date = kwargs.get('date') or kwargs.get('trade_date') or kwargs.get('Date') or ''
                date = _normalize_date(raw_date) if raw_date else datetime.now().strftime('%Y%m%d')
                request_date = date
                source_chain = ['limit_up_manager']

                # 委托给 get_limit_up_statistics 工具
                try:
                    from ..market.limit_up import get_limit_up_statistics as _get_stat
                    stat_res = _get_stat(date=date)
                except Exception as e:
                    logger.warning(f"[LimitUp] get_limit_up_statistics 失败: {e}")
                    return _fail(
                        f'涨停统计查询失败: {e}',
                        source_chain=_dedupe_chain(source_chain + ['market.limit_up.get_limit_up_statistics']),
                        data_timestamp=date,
                    )

                if not stat_res.get('success'):
                    message = stat_res.get('error') or stat_res.get('message') or '涨停统计查询失败'
                    return _fail(
                        message,
                        source_chain=_dedupe_chain(source_chain + (stat_res.get('source_chain') or ['market.limit_up.get_limit_up_statistics'])),
                        data_timestamp=date,
                    )

                if stat_res.get('data'):
                    source_chain.extend(stat_res.get('source_chain') or ['market.limit_up.get_limit_up_statistics'])
                    stat_data = stat_res['data']
                    return _ok({
                        'request_date': request_date,
                        'date': date,
                        'total_limit_up': stat_data.get('totalLimitUp', 0),
                        'sealed_limit_up': stat_data.get('totalLimitUp', 0) - (stat_data.get('failedBoard') or 0),
                        'broken_limit_up': stat_data.get('failedBoard') or 0,
                        'seal_rate': f"{stat_data.get('successRate', 0):.2f}%",
                        'data_quality': stat_res.get('data_quality'),
                        'source_chain': stat_res.get('source_chain'),
                        'fallback_reason': stat_res.get('fallback_reason'),
                        'degraded': bool(stat_res.get('degraded')),
                    }, source_chain=_dedupe_chain(source_chain), data_timestamp=date)

                return _ok({
                    'request_date': request_date,
                    'date': date,
                    'total_limit_up': 0,
                    'sealed_limit_up': 0,
                    'broken_limit_up': 0,
                    'message': f'{date} 暂无涨停统计数据（可能为非交易日）'
                }, source_chain=_dedupe_chain(source_chain), data_timestamp=date)
            
            else:
                return _fail(
                    f'Unknown action: {action}. Supported: help, list, statistics',
                    source_chain=['limit_up_manager'],
                )
        except Exception as e:
            logger.error(f"[LimitUp] Error: {e}")
            return fail_with_meta(
                str(e),
                tool_name='limit_up_manager',
                action=action,
                started_at=start_time,
                source_chain=['limit_up_manager'],
            )
