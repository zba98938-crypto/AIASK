"""新闻/研报工具 — 私有辅助函数"""

import os
import time
from typing import Any

try:
    import akshare as ak
except ImportError:
    ak = None

from ...core.rate_limiter import get_limiter
from ...data_source import data_source
from ...utils import format_period, normalize_code, pick_value

_RETRY_SLEEP_SECONDS = float(os.getenv("AKSHARE_RETRY_SLEEP_SECONDS", "0.5"))


def _dedup_reports(reports: list) -> list:
    """按 (标题+机构+日期) 去重"""
    seen: set[tuple[str, str, str]] = set()
    result: list[dict] = []
    for r in reports:
        key = (
            str(r.get("title", "")).strip().lower(),
            str(r.get("institution", "")).strip().lower(),
            str(r.get("date", "")).strip(),
        )
        if key not in seen:
            seen.add(key)
            result.append(r)
    return result


def _to_ymd(value: Any) -> str:
    text = format_period(value)
    return text.replace("-", "") if text else ""


def _map_tushare_ann_rows(rows: list[dict]) -> list[dict]:
    results: list[dict] = []
    for row in rows:
        title = row.get("ann_title") or row.get("title") or row.get("公告标题")
        time_val = row.get("ann_date") or row.get("f_ann_date") or row.get("date")
        url = row.get("ann_url") or row.get("url")
        if not title and not url:
            continue
        results.append(
            {
                "title": str(title or ""),
                "time": format_period(time_val),
                "source": "tushare_announcement",
                "url": str(url or ""),
                "date": format_period(time_val),
            }
        )
    return results


def _try_tushare_anns(start_date: str, end_date: str, stock_code: str, limit: int) -> list[dict]:
    pro = data_source.get_tushare_pro()
    if not pro:
        return []
    try:
        ts_code = ""
        if stock_code:
            code = normalize_code(stock_code)
            ts_code = f"{code}.SH" if code.startswith("6") else f"{code}.SZ"
        df = pro.anns(
            ts_code=ts_code,
            start_date=_to_ymd(start_date),
            end_date=_to_ymd(end_date),
        )
        if df is None or df.empty:
            return []
        rows = df.head(limit).fillna("").to_dict(orient="records")
        return _map_tushare_ann_rows(rows)
    except Exception:
        return []


def _try_tushare_news(start_date: str, end_date: str, limit: int) -> list[dict]:
    pro = data_source.get_tushare_pro()
    if not pro:
        return []
    try:
        df = pro.news(
            start_date=_to_ymd(start_date),
            end_date=_to_ymd(end_date),
            src="sina",
        )
        if df is None or df.empty:
            return []
        rows = df.head(limit).fillna("").to_dict(orient="records")
        results: list[dict] = []
        for row in rows:
            title = row.get("title") or row.get("新闻标题")
            time_val = row.get("datetime") or row.get("date") or row.get("time")
            url = row.get("url")
            if not title and not url:
                continue
            results.append(
                {
                    "title": str(title or ""),
                    "time": format_period(time_val),
                    "source": str(row.get("src") or row.get("source") or "tushare"),
                    "url": str(url or ""),
                    "date": format_period(time_val),
                }
            )
        return results
    except Exception:
        return []


def _map_news_rows(rows: list[dict]) -> list[dict]:
    results: list[dict] = []
    for row in rows:
        title = pick_value(row, ["title", "标题", "新闻标题", "公告标题"])
        time_val = pick_value(row, ["time", "发布时间", "公告日期", "日期", "date", "发布时间"])
        source = pick_value(row, ["source", "来源", "来源网站", "媒体名称"])
        url = pick_value(row, ["url", "链接", "网址", "新闻链接"])
        if not title and not url:
            continue
        results.append(
            {
                "title": str(title or ""),
                "time": format_period(time_val),
                "source": str(source or "eastmoney"),
                "url": str(url or ""),
                "date": format_period(time_val),
            }
        )
    return results


def _map_research_rows(rows: list[dict]) -> list[dict]:
    results: list[dict] = []
    for row in rows:
        title = pick_value(row, ["title", "报告名称", "标题", "研报标题"])
        institution = pick_value(row, ["institution", "机构名称", "机构", "研究机构", "发布机构"])
        author = pick_value(row, ["author", "作者", "研究员", "分析师"])
        time_val = pick_value(row, ["date", "发布日期", "日期", "发布时间"])
        url = pick_value(row, ["url", "链接", "网址", "报告链接"])
        if not title and not institution:
            continue
        results.append(
            {
                "title": str(title or ""),
                "time": format_period(time_val),
                "source": str(institution or author or ""),
                "url": str(url or ""),
                "date": format_period(time_val),
            }
        )
    return results


def _fetch_eastmoney_research(code: str, limit: int) -> list[dict]:
    """东财 datacenter 获取个股研报数据"""
    import requests
    try:
        url = "https://datacenter-web.eastmoney.com/api/data/v1/get"
        params = {
            "sortColumns": "REPORT_DATE",
            "sortTypes": -1,
            "pageSize": min(limit, 50),
            "pageNumber": 1,
            "reportName": "RPT_RATINGCHANGE_DET",
            "columns": "REPORT_DATE,ORG_NAME,RESEARCHER,TITLE,RATING_NAME,PREDICT_NEXT_TWO_EPS",
            "filter": f'(SECURITY_CODE="{code}")',
        }
        headers = {"User-Agent": "Mozilla/5.0", "Referer": "https://data.eastmoney.com/"}
        resp = requests.get(url, params=params, headers=headers, timeout=15)
        if resp.status_code != 200:
            return []
        payload = resp.json()
        items = payload.get("result", {}).get("data", []) or []
        results = []
        for item in items:
            results.append({
                "title": str(item.get("TITLE") or ""),
                "institution": str(item.get("ORG_NAME") or ""),
                "author": str(item.get("RESEARCHER") or ""),
                "rating": str(item.get("RATING_NAME") or ""),
                "targetPrice": None,
                "date": str(item.get("REPORT_DATE", "")).split(" ")[0],
            })
        return results
    except Exception:
        return []


def _try_akshare_news_functions(code: str, limit: int) -> list[dict]:
    if ak is None:
        return []
    candidates = [
        ("stock_news_em", {"symbol": code}),
        ("stock_news_em", {"code": code}),
        ("stock_news", {"symbol": code}),
    ]
    for func_name, kwargs in candidates:
        func = getattr(ak, func_name, None)
        if not func:
            continue
        try:
            df = func(**kwargs)
        except Exception:
            continue
        if df is None or df.empty:
            continue
        rows = df.head(limit).fillna("").to_dict(orient="records")
        mapped = _map_news_rows(rows)
        if mapped:
            return mapped
    return []
