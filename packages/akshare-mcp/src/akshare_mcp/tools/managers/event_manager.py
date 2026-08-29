"""事件管理器 - 财报、分红、重组"""

from typing import Any
from datetime import date, timedelta
import time

from ...storage import get_db
from ..manager_protocol import (
    fail_with_meta,
    normalize_manager_code,
    normalize_manager_kwargs,
    normalize_manager_payload,
    ok_with_meta,
)
from ..news import get_research_reports, get_stock_news, get_stock_notices, get_stock_research


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


def _pick_first(payload: dict, keys: list[str], default=None):
    for key in keys:
        value = payload.get(key)
        if value not in (None, ""):
            return value
    return default


def _normalize_content_event(code: str, row: dict, event_type: str, default_source: str) -> dict | None:
    if not isinstance(row, dict):
        return None
    title = _pick_first(row, ['title', '报告名称', '公告标题', '标题', 'name'])
    event_date = _pick_first(row, ['event_date', 'date', 'time', '公告日期', '发布日期', '日期'])
    source = _pick_first(row, ['source', 'institution', '机构', '媒体名称', '来源'], default_source)
    url = _pick_first(row, ['url', '链接', '网址'], '')
    if not title and not url:
        return None
    return {
        'code': code,
        'event_type': event_type,
        'title': str(title or ''),
        'event_date': str(event_date or ''),
        'source': str(source or default_source),
        'url': str(url or ''),
    }


def _aggregate_events_from_content(code: str, limit: int = 20) -> tuple[list[dict], list[str]]:
    end_date = date.today()
    start_date = end_date - timedelta(days=30)
    events: list[dict] = []
    source_chain: list[str] = []

    def _append_items(items: list[dict], event_type: str, default_source: str, chain_name: str):
        added = 0
        for item in items:
            normalized = _normalize_content_event(code, item, event_type, default_source)
            if normalized:
                events.append(normalized)
                added += 1
        if added > 0:
            source_chain.append(chain_name)

    news_res = get_stock_news(code, limit=min(limit, 10))
    if news_res.get('success') and isinstance(news_res.get('data'), list):
        _append_items(news_res.get('data', []), 'news', 'stock_news', 'news.get_stock_news')

    notice_res = get_stock_notices(
        start_date=start_date.isoformat(),
        end_date=end_date.isoformat(),
        types=['全部'],
        stock_code=code,
    )
    notice_events = (notice_res.get('data') or {}).get('events', []) if notice_res.get('success') else []
    _append_items(notice_events, 'notice', 'stock_notice', 'news.get_stock_notices')

    research_res = get_stock_research(code, limit=min(limit, 10))
    reports = (research_res.get('data') or {}).get('reports', []) if research_res.get('success') else []
    if reports:
        _append_items(reports, 'research', 'stock_research', 'news.get_stock_research')
    else:
        fallback_reports = get_research_reports(symbol=code, limit=min(limit, 10))
        fallback_items = fallback_reports.get('data') if fallback_reports.get('success') else []
        if isinstance(fallback_items, list):
            _append_items(fallback_items, 'research', 'research_reports', 'news.get_research_reports')

    deduped: list[dict] = []
    seen_keys: set[tuple[str, str, str]] = set()
    for item in events:
        dedup_key = (item.get('event_type', ''), item.get('title', ''), item.get('event_date', ''))
        if dedup_key in seen_keys:
            continue
        seen_keys.add(dedup_key)
        deduped.append(item)
    deduped.sort(key=lambda item: item.get('event_date', ''), reverse=True)
    return deduped[:limit], source_chain


def register_event_manager(mcp):
    """注册事件管理器工具"""
    
    @mcp.tool()
    async def event_manager(action: str, params: dict | None = None, kwargs: Any = None):
        """事件管理器（统一 action + kwargs 协议）

        Args:
            action (str, required): 操作类型，可选 help/upcoming_events/get_by_code/calendar/list_events/get_events
            kwargs: JSON 字符串或关键字参数，不同 action 所需参数:
                - help: 无需额外参数
                - upcoming_events: days(int, optional, 未来天数)
                - get_by_code / get_events: code(str, 股票代码)
                - calendar: start_date(str, optional), end_date(str, optional)
                - list_events: type(str, optional, 事件类型)

        Returns:
            dict: {"success": bool, "data": {...}, "error": str|None}

        Examples:
            # 查看帮助
            event_manager(action="help", kwargs="{}")
            # 获取未来7天事件
            event_manager(action="upcoming_events", kwargs='{"days":7}')
            # 按股票查询事件
            event_manager(action="get_by_code", kwargs='{"code":"600519"}')
            # 事件日历
            event_manager(action="calendar", kwargs='{"start_date":"2025-01-01","end_date":"2025-01-31"}')
        """
        start_time = time.perf_counter()
        try:
            kwargs = normalize_manager_payload(params=params, kwargs=kwargs)
            code, kwargs = normalize_manager_code(None, kwargs)

            def _ok(data: dict, source_chain=None):
                return ok_with_meta(
                    data,
                    tool_name="event_manager",
                    action=action,
                    started_at=start_time,
                    source_chain=source_chain,
                )

            def _fail(message: str, source_chain=None):
                return fail_with_meta(
                    message,
                    tool_name="event_manager",
                    action=action,
                    started_at=start_time,
                    source_chain=source_chain,
                )

            SUPPORTED_ACTIONS = {
                'upcoming_events': '获取即将到来的事件',
                'get_by_code': '根据股票代码获取事件',
                'calendar': '获取事件日历（别名）',
                'list_events': '列出事件（别名）',
                'get_events': '获取事件（别名）',
                'help': '显示帮助信息',
            }
            
            if action == 'help':
                return _ok({'supported_actions': SUPPORTED_ACTIONS}, source_chain=['event_manager'])
            
            elif action in ['upcoming_events', 'calendar', 'list_events']:
                days = int(kwargs.get('days') or 7)
                event_type = kwargs.get('type', 'all')
                
                db = get_db()
                async with db.acquire() as conn:
                    rows = await conn.fetch(
                        """SELECT * FROM events 
                           WHERE event_date BETWEEN date('now') AND date('now', '+' || $1 || ' days')
                           ORDER BY event_date""",
                        days
                    )
                    events = [dict(row) for row in rows]

                if event_type != 'all':
                    events = [
                        item for item in events
                        if str(item.get('event_type') or '') == str(event_type)
                    ]
                
                return _ok(
                    {'events': events, 'count': len(events)},
                    source_chain=['event_manager', 'db.events'],
                )
            
            elif action in ['get_by_code', 'get_events']:
                if not code:
                    return _fail('需要提供股票代码', source_chain=['event_manager'])
                limit = int(kwargs.get('limit') or 20)
                
                db = get_db()
                async with db.acquire() as conn:
                    rows = await conn.fetch(
                        "SELECT * FROM events WHERE code = $1 ORDER BY event_date DESC LIMIT $2",
                        code, limit
                    )
                    events = [dict(row) for row in rows]

                source = 'db.events'
                source_chain = ['event_manager', 'db.events']
                fallback_used = False
                if not events:
                    events, aggregated_chain = _aggregate_events_from_content(code, limit=limit)
                    if events:
                        fallback_used = True
                        source = 'aggregated_content'
                        source_chain.extend(aggregated_chain)

                # P3-5.2 fix: 按 (title+date) 二元组去重(诊断报告 §5.2)
                # 历史问题:多源聚合时同一事件被多次写入 events 表,get_by_code 返回重复行
                seen_keys: set[tuple[str, str]] = set()
                deduped: list[dict] = []
                duplicates_removed = 0
                for event in events:
                    title = str((event or {}).get('title') or (event or {}).get('event_title') or '').strip()
                    raw_date = (event or {}).get('event_date') or (event or {}).get('date') or ''
                    date_key = str(raw_date)[:10] if raw_date else ''
                    key = (title.lower(), date_key)
                    if key in seen_keys:
                        duplicates_removed += 1
                        continue
                    seen_keys.add(key)
                    deduped.append(event)

                response_chain = _dedupe_chain(source_chain)
                return _ok(
                    {
                        'code': code,
                        'events': deduped,
                        'count': len(deduped),
                        'source': source,
                        'source_chain': response_chain,
                        'fallback_used': fallback_used,
                        # P3-5.2 fix: 透出去重统计,便于运维核查
                        'raw_count_before_dedup': len(events),
                        'duplicates_removed': duplicates_removed,
                    },
                    source_chain=response_chain,
                )
            
            else:
                return _fail(
                    f'Unknown action: {action}. Supported: {", ".join(SUPPORTED_ACTIONS.keys())}',
                    source_chain=['event_manager'],
                )
        except Exception as e:
            return fail_with_meta(
                str(e),
                tool_name='event_manager',
                action=action,
                started_at=start_time,
                source_chain=['event_manager'],
            )
