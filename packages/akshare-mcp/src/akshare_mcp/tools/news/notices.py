"""新闻/研报工具 — 公告事件"""

import os
import time
from datetime import timedelta
from typing import Any, Optional

import requests

try:
    import akshare as ak
except ImportError:
    ak = None

from ...services.db_first_market_context import load_db_first_document_context
from ...storage import get_db
from ...core.cache_manager import cached
from ...core.rate_limiter import get_limiter
from ...utils import (
    attach_argument_contract_meta,
    fail,
    format_period,
    normalize_code,
    ok,
    parse_date_input,
    resolve_canonical_arg,
)
from ..fund_flow_common import _run_storage_call_sync
from .helpers import _RETRY_SLEEP_SECONDS, _try_tushare_anns


_NOTICE_NODE_MAP = {
    "全部": "0",
    "财务报告": "1",
    "融资公告": "2",
    "风险提示": "3",
    "信息变更": "4",
    "重大事项": "5",
    "资产重组": "6",
    "持股变动": "7",
}
_NOTICE_DETAIL_URL = "https://data.eastmoney.com/notices/detail/{code}/{art_code}.html"
_NOTICE_API_URL = "https://np-anotice-stock.eastmoney.com/api/security/ann"
_NOTICE_HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Referer": "https://data.eastmoney.com/",
}


def _select_notice_code(item: dict[str, Any]) -> str:
    codes = item.get("codes") or []
    if not isinstance(codes, list):
        return ""
    for code_item in codes:
        if not isinstance(code_item, dict):
            continue
        ann_type = str(code_item.get("ann_type") or "")
        if ann_type.startswith("A"):
            return normalize_code(code_item.get("stock_code"))
    if codes and isinstance(codes[0], dict):
        return normalize_code(codes[0].get("stock_code"))
    return ""


def _map_notice_item(item: dict[str, Any], fallback_type: str) -> Optional[dict[str, Any]]:
    code = _select_notice_code(item)
    title = str(item.get("title") or item.get("title_ch") or "")
    art_code = str(item.get("art_code") or "")
    if not code or not title:
        return None
    column_items = item.get("columns") or []
    notice_type = fallback_type
    if column_items and isinstance(column_items[0], dict):
        notice_type = str(column_items[0].get("column_name") or fallback_type)
    return {
        "code": code,
        "name": str(((item.get("codes") or [{}])[0] or {}).get("short_name") or ""),
        "title": title,
        "type": notice_type,
        "date": format_period(item.get("notice_date") or item.get("display_time")),
        "url": _NOTICE_DETAIL_URL.format(code=code, art_code=art_code) if art_code else "",
    }


def _fetch_code_notice_range(
    *,
    start_iso: str,
    end_iso: str,
    code_filter: str,
    notice_type: str,
    max_items: int,
    deadline: Optional[float],
) -> tuple[list[dict[str, Any]], bool]:
    """东财区间公告查询：单股票场景优先使用 stock_list，避免全市场逐日分页。"""
    params = {
        "sr": "-1",
        "page_size": "100",
        "page_index": "1",
        "ann_type": "A",
        "client_source": "web",
        "f_node": _NOTICE_NODE_MAP.get(notice_type, "0"),
        "s_node": "0",
        "begin_time": start_iso,
        "end_time": end_iso,
        "stock_list": code_filter,
    }

    events: list[dict[str, Any]] = []
    seen: set[str] = set()
    page = 1
    partial = False

    while len(events) < max_items:
        if deadline is not None and time.monotonic() > deadline:
            partial = True
            break

        params["page_index"] = str(page)
        resp = requests.get(_NOTICE_API_URL, params=params, headers=_NOTICE_HEADERS, timeout=15)
        payload = resp.json() if resp.status_code == 200 else {}
        data = payload.get("data") or {}
        items = data.get("list") or []
        total_hits = int(data.get("total_hits") or 0)
        total_pages = (total_hits + 99) // 100 if total_hits > 0 else 0

        if not items:
            break

        for item in items:
            mapped = _map_notice_item(item, notice_type)
            if not mapped:
                continue
            key = mapped.get("url") or f"{mapped.get('code')}|{mapped.get('title')}|{mapped.get('date')}"
            if key in seen:
                continue
            seen.add(key)
            events.append(mapped)
            if len(events) >= max_items:
                break

        if total_pages <= 0 or page >= total_pages:
            break
        page += 1

    return events, partial


def fetch_market_notice_head(start_iso: str, end_iso: str, max_items: int = 20) -> list[dict[str, Any]]:
    """快速获取市场公告头部结果，避免全市场逐日分页扫描。"""
    params = {
        "sr": "-1",
        "page_size": str(max(1, min(int(max_items or 20), 50))),
        "page_index": "1",
        "ann_type": "A",
        "client_source": "web",
        "f_node": "0",
        "s_node": "0",
        "begin_time": start_iso,
        "end_time": end_iso,
    }
    try:
        resp = requests.get(_NOTICE_API_URL, params=params, headers=_NOTICE_HEADERS, timeout=10)
        payload = resp.json() if resp.status_code == 200 else {}
        items = (payload.get("data") or {}).get("list") or []
        results: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in items:
            mapped = _map_notice_item(item, "全部")
            if not mapped:
                continue
            key = mapped.get("url") or f"{mapped.get('code')}|{mapped.get('title')}|{mapped.get('date')}"
            if key in seen:
                continue
            seen.add(key)
            results.append(mapped)
            if len(results) >= max_items:
                break
        return results
    except Exception:
        return []


@cached(ttl=1800.0)
def get_stock_notices(
    start_date: str,
    end_date: str,
    types: Optional[list[str]] = None,
    stock_code: str = "",
    *,
    code: str = "",
    symbol: str = "",
    ticker: str = "",
    prefer_db: bool = True,
) -> dict:
    """
    获取公告事件日历（东方财富公告）

    数据源优先级: Tushare Pro(announcements) → AkShare(stock_notice_report)
    时效性: 日频

    Args:
        start_date (str, required): 开始日期，格式 YYYY-MM-DD 或 YYYYMMDD（必填）
        end_date (str, required): 结束日期，格式 YYYY-MM-DD 或 YYYYMMDD（必填，跨度上限由环境变量 AKSHARE_NOTICE_MAX_DAYS 控制，默认31天）
        types (list[str]|None, optional): 公告类型列表
        stock_code (str, optional): 股票代码过滤

    Returns:
        dict: {"success": bool, "data": {...}}
    """
    limiter = get_limiter("news", rate=3.0)
    limiter.acquire()

    try:
        raw_code, alias_hits, _ = resolve_canonical_arg(
            "code",
            code,
            stock_code=stock_code,
            symbol=symbol,
            ticker=ticker,
        )
        start = parse_date_input(start_date)
        end = parse_date_input(end_date)
        canonical_args = {"code": normalize_code(raw_code) if raw_code else "", "start_date": start_date, "end_date": end_date, "types": types}
        if not start or not end:
            return attach_argument_contract_meta(
                fail("日期格式错误，需 YYYY-MM-DD 或 YYYYMMDD"),
                canonical_tool="get_stock_notices",
                canonical_args=canonical_args,
                alias_hits=alias_hits,
            )
        if end < start:
            start, end = end, start

        max_days = int(os.getenv("AKSHARE_NOTICE_MAX_DAYS", "31"))
        if (end - start).days + 1 > max_days:
            end = start + timedelta(days=max_days - 1)

        raw_types = types or []
        type_map = {
            "all": "全部", "全部": "全部",
            "重大事项": "重大事项", "财务报告": "财务报告",
            "融资公告": "融资公告", "风险提示": "风险提示",
            "资产重组": "资产重组", "信息变更": "信息变更",
            "持股变动": "持股变动",
            "major": "重大事项", "financial": "财务报告",
            "financing": "融资公告", "risk": "风险提示",
            "restructuring": "资产重组", "change": "信息变更",
            "holding": "持股变动",
        }
        normalized_types = []
        for t in raw_types:
            key = str(t or "").strip()
            if not key:
                continue
            mapped = type_map.get(key.lower()) or type_map.get(key) or None
            if mapped:
                normalized_types.append(mapped)
        if not normalized_types:
            normalized_types = ["全部"]

        code_filter = normalize_code(raw_code) if raw_code else ""
        canonical_args["code"] = code_filter
        canonical_args["types"] = normalized_types
        events: list[dict[str, Any]] = []
        max_seconds = int(os.getenv("AKSHARE_NOTICE_MAX_SECONDS", "8"))
        max_retry = int(os.getenv("AKSHARE_NOTICE_RETRY", "1"))
        max_items = int(os.getenv("AKSHARE_NOTICE_MAX_ITEMS", "500"))
        start_ts = time.monotonic()
        partial = False

        def _respond(payload: dict) -> dict:
            return attach_argument_contract_meta(
                payload,
                canonical_tool="get_stock_notices",
                canonical_args=canonical_args,
                alias_hits=alias_hits,
            )

        if not code_filter:
            max_items = min(max_items, 200)

        if prefer_db and code_filter and normalized_types == ["全部"]:
            try:
                db_context, _ = _run_storage_call_sync(
                    lambda: load_db_first_document_context(
                        get_db(),
                        code_filter,
                        start_date=start,
                        end_date=end,
                        notice_limit=max_items,
                    ),
                    timeout=min(max(float(max_seconds or 0), 1.0), 8.0),
                )
                db_events = list((db_context or {}).get("notices") or [])
                if db_events:
                    return _respond(ok(
                        {
                            "startDate": start.isoformat(),
                            "endDate": end.isoformat(),
                            "types": normalized_types,
                            "events": db_events[:max_items],
                            "truncated": len(db_events) > max_items,
                            "partial": False,
                        }
                    ))
            except Exception:
                pass

        # 0. Try Tushare Pro announcements first
        try:
            tushare_items = _try_tushare_anns(start.isoformat(), end.isoformat(), code_filter, max_items)
            if tushare_items:
                return _respond(ok(
                    {
                        "startDate": start.isoformat(),
                        "endDate": end.isoformat(),
                        "types": normalized_types,
                        "events": tushare_items,
                        "truncated": len(tushare_items) > max_items,
                        "partial": False,
                    }
                ))
        except Exception:
            pass

        # 1. 单股票优先用东财区间接口，避免全市场逐日扫描导致 partial/超时。
        if code_filter:
            try:
                deadline = (start_ts + max_seconds) if max_seconds > 0 else None
                fast_events: list[dict[str, Any]] = []
                seen: set[str] = set()
                partial = False
                for notice_type in normalized_types:
                    batch, batch_partial = _fetch_code_notice_range(
                        start_iso=start.isoformat(),
                        end_iso=end.isoformat(),
                        code_filter=code_filter,
                        notice_type=notice_type,
                        max_items=max_items,
                        deadline=deadline,
                    )
                    partial = partial or batch_partial
                    for item in batch:
                        key = item.get("url") or f"{item.get('code')}|{item.get('title')}|{item.get('date')}"
                        if key in seen:
                            continue
                        seen.add(key)
                        fast_events.append(item)
                        if len(fast_events) >= max_items:
                            break
                    if len(fast_events) >= max_items or partial:
                        break

                fast_events = sorted(fast_events, key=lambda x: str(x.get("date") or ""), reverse=True)
                return _respond(ok(
                    {
                        "startDate": start.isoformat(),
                        "endDate": end.isoformat(),
                        "types": normalized_types,
                        "events": fast_events[:max_items],
                        "truncated": len(fast_events) > max_items,
                        "partial": partial,
                    }
                ))
            except Exception:
                partial = False

        current = start
        while current <= end:
            if max_seconds > 0 and (time.monotonic() - start_ts) > max_seconds:
                partial = True
                break
            if ak is None:
                break
            date_str = current.strftime("%Y%m%d")
            for notice_type in normalized_types:
                try:
                    df = None
                    last_error: Optional[Exception] = None
                    for _ in range(max_retry):
                        try:
                            df = ak.stock_notice_report(symbol=notice_type, date=date_str) if ak is not None else None
                            if df is not None and not df.empty:
                                break
                        except Exception as exc:
                            last_error = exc
                            if _RETRY_SLEEP_SECONDS > 0:
                                time.sleep(_RETRY_SLEEP_SECONDS)
                except Exception:
                    continue
                if df is None or df.empty or "代码" not in df.columns:
                    continue
                for _, row in df.iterrows():
                    code = normalize_code(row.get("代码", ""))
                    if code_filter and code != code_filter:
                        continue
                    notice_date = row.get("公告日期")
                    events.append(
                        {
                            "code": code,
                            "name": str(row.get("名称", "")),
                            "title": str(row.get("公告标题", "")),
                            "type": str(row.get("公告类型", notice_type)),
                            "date": format_period(notice_date),
                            "url": str(row.get("网址", "")),
                        }
                    )
            current += timedelta(days=1)

        events = sorted(events, key=lambda x: str(x.get("date") or ""), reverse=True)
        truncated = len(events) > max_items
        if truncated:
            events = events[:max_items]

        return _respond(ok(
            {
                "startDate": start.isoformat(),
                "endDate": end.isoformat(),
                "types": normalized_types,
                "events": events,
                "truncated": truncated,
                "partial": partial,
            }
        ))
    except Exception as e:
        return attach_argument_contract_meta(
            fail(e),
            canonical_tool="get_stock_notices",
            canonical_args={"code": normalize_code(code) if code else "", "start_date": start_date, "end_date": end_date, "types": types},
            alias_hits=[],
        )
