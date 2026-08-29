"""研究管理器 - 研报、评级"""

from typing import Any, Optional
import time

from ...storage import get_db
from ...utils import normalize_code
from ..manager_protocol import (
    normalize_manager_payload,
    fail_with_meta,
    normalize_manager_code,
    normalize_manager_kwargs,
    ok_with_meta,
)


def _normalize_limit(value, default: int = 10, minimum: int = 1, maximum: int = 50) -> int:
    try:
        limit = int(value)
    except Exception:
        limit = default
    return max(minimum, min(limit, maximum))


def register_research_manager(mcp):
    """注册研究管理器工具"""
    
    @mcp.tool()
    async def research_manager(action: str, params: dict | None = None, kwargs: Any = None, code: str | None = None, limit: int | None = None):
        """研究管理器（统一 action + kwargs 协议）

        Args:
            action (str, required): 操作类型，可选 help/get_reports/get_ratings
            code (str, optional): 股票代码
            kwargs: JSON 字符串或关键字参数，不同 action 所需参数:
                - help: 无需额外参数
                - get_reports: code(str, optional), limit(int, optional)
                - get_ratings: code(str)

        Returns:
            dict: {"success": bool, "data": {...}, "error": str|None}

        Examples:
            # 查看帮助
            research_manager(action="help", kwargs="{}")
            # 获取研报列表
            research_manager(action="get_reports", code="600519", kwargs='{"limit":5}')
            # 获取一致评级
            research_manager(action="get_ratings", code="600519", kwargs="{}")
        """
        start_time = time.perf_counter()
        try:
            db = get_db()
            kwargs = normalize_manager_payload(
                params=params,
                kwargs=kwargs,
                extra={
                    "code": code,
                    "limit": limit,
                },
            )
            kwargs = normalize_manager_kwargs(kwargs)
            code, kwargs = normalize_manager_code(code, kwargs)

            def _ok(data: dict, source_chain=None):
                return ok_with_meta(
                    data,
                    tool_name='research_manager',
                    action=action,
                    started_at=start_time,
                    source_chain=source_chain,
                )

            def _fail(message: str, source_chain=None):
                return fail_with_meta(
                    message,
                    tool_name='research_manager',
                    action=action,
                    started_at=start_time,
                    source_chain=source_chain,
                )
            
            if action == 'help':
                return _ok({
                    'supported_actions': {
                        'get_reports': '获取研报列表（需要 code）',
                        'get_ratings': '获取一致评级（需要 code）',
                        'help': '显示帮助信息',
                    }
                }, source_chain=['research_manager'])
            
            elif action == 'get_reports':
                if not code:
                    return _fail('需要提供股票代码')
                
                code = normalize_code(code)
                limit = _normalize_limit(kwargs.get('limit', 10))
                
                # 调用真实数据源获取研报
                try:
                    from ..news import get_stock_research
                    result = get_stock_research(code, limit=limit)
                    if result.get('success') and result.get('data', {}).get('reports'):
                        reports_data = result['data']
                        return _ok({
                            'code': code,
                            'reports': reports_data.get('reports', []),
                            'count': reports_data.get('total', 0),
                            'source': 'real_data'
                        }, source_chain=['research_manager', 'news.get_stock_research'])
                except Exception:
                    pass
                
                # 降级：尝试 Tushare forecast
                try:
                    from ...data_source import data_source
                    ts_pro = data_source.get_tushare_pro()
                    if ts_pro:
                        ts_code = f"{code}.SH" if code.startswith('6') else f"{code}.SZ"
                        df = ts_pro.report_rc(ts_code=ts_code, fields='report_title,org_name,author_name,report_date,rating')
                        if df is not None and not df.empty:
                            reports = []
                            for _, row in df.head(limit).iterrows():
                                reports.append({
                                    'title': str(row.get('report_title', '') or '').strip(),
                                    'institution': str(row.get('org_name', '') or '').strip(),
                                    'author': str(row.get('author_name', '') or '').strip(),
                                    'rating': str(row.get('rating', '') or '').strip(),
                                    'target_price': None,
                                    'date': str(row.get('report_date', '') or ''),
                                })
                            if reports:
                                return _ok({
                                    'code': code,
                                    'reports': reports,
                                    'count': len(reports),
                                    'source': 'tushare'
                                }, source_chain=['research_manager', 'tushare.report_rc'])
                except Exception:
                    pass

                # P1-3.4 fix: db.research_reports 回退(诊断报告 §3.4 manager 与 db 脱节)
                # 历史问题:research_manager.get_reports tushare/akshare 全跪,但 db 实际有 reports
                # 修复:fallback 到 db.research_reports,与 get_research_reports/search_research_db 同源
                try:
                    async with db.acquire() as conn:
                        rows = await conn.fetch(
                            """SELECT title, institution, author, rating,
                                      target_price, publish_date, summary
                               FROM research_reports
                               WHERE stock_code = $1
                               ORDER BY publish_date DESC
                               LIMIT $2""",
                            code, int(limit),
                        )
                    if rows:
                        reports = [
                            {
                                "title": str(r.get("title") or "").strip(),
                                "institution": str(r.get("institution") or "").strip(),
                                "author": str(r.get("author") or "").strip(),
                                "rating": str(r.get("rating") or "").strip(),
                                "target_price": r.get("target_price"),
                                "date": str(r.get("publish_date") or ""),
                                "summary": str(r.get("summary") or "")[:500],
                            }
                            for r in rows
                        ]
                        return _ok({
                            'code': code,
                            'reports': reports,
                            'count': len(reports),
                            'source': 'db.research_reports',
                            'fallback_used': True,
                            'fallback_reason': 'tushare_and_news_failed',
                        }, source_chain=['research_manager', 'tushare.report_rc', 'db.research_reports'])
                except Exception:
                    pass

                return _ok({
                    'code': code,
                    'reports': [],
                    'count': 0,
                    'message': f'暂无 {code} 的研报数据'
                }, source_chain=['research_manager'])
            
            elif action == 'get_ratings':
                if not code:
                    return _fail('需要提供股票代码')
                
                code = normalize_code(code)
                
                # 尝试从 Tushare 获取一致评级
                try:
                    from ...data_source import data_source
                    ts_pro = data_source.get_tushare_pro()
                    if ts_pro:
                        ts_code = f"{code}.SH" if code.startswith('6') else f"{code}.SZ"
                        df = ts_pro.report_rc(ts_code=ts_code, fields='rating')
                        if df is not None and not df.empty:
                            ratings = {'buy': 0, 'hold': 0, 'sell': 0}
                            for _, row in df.iterrows():
                                r = str(row.get('rating', '') or '').lower()
                                if '买入' in r or 'buy' in r or '增持' in r:
                                    ratings['buy'] += 1
                                elif '卖出' in r or 'sell' in r or '减持' in r:
                                    ratings['sell'] += 1
                                else:
                                    ratings['hold'] += 1
                            total = sum(ratings.values())
                            if total > 0:
                                consensus = max(ratings, key=ratings.get)
                                return _ok({
                                    'code': code,
                                    'consensus_rating': consensus,
                                    'ratings': ratings,
                                    'total': total,
                                    'source': 'tushare'
                                }, source_chain=['research_manager', 'tushare.report_rc'])
                except Exception:
                    pass

                # P1-3.4 fix: db.research_reports 回退聚合 ratings(同 get_reports 修复)
                try:
                    async with db.acquire() as conn:
                        rows = await conn.fetch(
                            """SELECT rating FROM research_reports
                               WHERE stock_code = $1
                               ORDER BY publish_date DESC
                               LIMIT 200""",
                            code,
                        )
                    if rows:
                        ratings = {'buy': 0, 'hold': 0, 'sell': 0}
                        for row in rows:
                            r = str(row.get("rating") or "").lower()
                            if not r:
                                continue
                            # 中英文映射统一(诊断报告 §4.6 推荐)
                            if any(k in r for k in ('买入', '增持', 'buy', 'overweight', 'add')):
                                ratings['buy'] += 1
                            elif any(k in r for k in ('卖出', '减持', 'sell', 'underweight', 'reduce')):
                                ratings['sell'] += 1
                            elif any(k in r for k in ('持有', '中性', 'hold', 'neutral')):
                                ratings['hold'] += 1
                            # 其他 rating 值不计入
                        total = sum(ratings.values())
                        if total > 0:
                            consensus = max(ratings, key=ratings.get)
                            return _ok({
                                'code': code,
                                'consensus_rating': consensus,
                                'ratings': ratings,
                                'total': total,
                                'source': 'db.research_reports',
                                'fallback_used': True,
                                'fallback_reason': 'tushare_failed',
                            }, source_chain=['research_manager', 'tushare.report_rc', 'db.research_reports'])
                except Exception:
                    pass

                return _ok({
                    'code': code,
                    'consensus_rating': 'unknown',
                    'ratings': {'buy': 0, 'hold': 0, 'sell': 0},
                    'total': 0,
                    'message': f'暂无 {code} 的评级数据'
                }, source_chain=['research_manager'])
            
            else:
                return _fail(f'Unknown action: {action}. Supported: help, get_reports, get_ratings')
        except Exception as e:
            return fail_with_meta(
                str(e),
                tool_name='research_manager',
                action=action,
                started_at=start_time,
                source_chain=['research_manager'],
            )
