"""
市场板块工具 - 获取板块数据
数据源优先级: DB缓存 → 东方财富datacenter HTTP → AKShare
"""

import asyncio
import json
import urllib.request
from typing import Dict, Any, Optional, List
from ..storage.sqlite import get_db
from ..utils import (
    fail,
    ok,
    parse_numeric,
    propagate_data_quality_to_top,
    safe_float,
    safe_stderr_print,
    suppress_stdout,
)
from ..core.normalize import normalize_block_list, normalize_block_stock_list
from ..provider_contracts import attach_tool_provider_contract_meta

try:
    import akshare as ak
except ImportError:
    ak = None

def _is_placeholder_summary(block: dict) -> bool:
    try:
        stock_count = block.get('stock_count')
        avg_change = block.get('avg_change_pct')
        total_amount = block.get('total_amount')
        leader_code = block.get('leader_code')
        leader_name = block.get('leader_name')
        return (
            (stock_count is None or int(stock_count or 0) == 0)
            and (avg_change is None or float(avg_change or 0) == 0.0)
            and (total_amount is None or float(total_amount or 0) == 0.0)
            and not str(leader_code or '').strip()
            and not str(leader_name or '').strip()
        )
    except Exception:
        return False


def _sanitize_placeholder_blocks(blocks: list[dict], block_type: str) -> list[dict]:
    """把历史缓存中的占位 0 值恢复成“未知”，避免误导前端。"""
    sanitized = []
    for item in blocks:
        block = dict(item)
        if block_type in {'concept', 'industry'} and _is_placeholder_summary(block):
            block['stock_count'] = None
            block['avg_change_pct'] = None
            block['total_amount'] = None
            block['leader_code'] = None
            block['leader_name'] = None
            block['degraded'] = True
            block.setdefault('fallback_reason', '板块摘要来自名称列表/历史占位缓存，统计字段暂不可用；可改查 get_block_stocks 获取成分股')
        sanitized.append(block)
    return sanitized


def _fetch_concept_stocks_from_ths(block_code: str, block_name: str | None = None) -> list:
    """通过同花顺概念详情页抓取成分股（支持分页 ajax 回退）。"""
    if ak is None:
        return []

    try:
        import requests
        import py_mini_racer
        from bs4 import BeautifulSoup
        from akshare.stock_feature.stock_board_concept_ths import _get_file_content_ths
        import time
    except Exception as e:
        safe_stderr_print(f"[BlockStocks] THS concept fallback import失败: {e}")
        return []

    try:
        ths_code = str(block_code or "").strip()
        if not (ths_code.isdigit() and len(ths_code) == 6):
            if not block_name:
                return []
            try:
                with suppress_stdout("[BlockStocks] stock_board_concept_name_ths"):
                    df = ak.stock_board_concept_name_ths()
                match = df[df["name"] == str(block_name).strip()]
                if match.empty:
                    return []
                ths_code = str(match.iloc[0]["code"]).strip()
            except Exception as e:
                safe_stderr_print(f"[BlockStocks] THS concept name解析失败: {e}")
                return []

        def _build_session() -> tuple[requests.Session, dict[str, str]]:
            js_code = py_mini_racer.MiniRacer()
            js_code.eval(_get_file_content_ths("ths.js"))
            v_code = js_code.call("v")
            session = requests.Session()
            headers = {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/89.0.4389.90 Safari/537.36"
                ),
                "Cookie": f"v={v_code}",
                "Referer": f"https://q.10jqka.com.cn/gn/detail/code/{ths_code}/",
            }
            return session, headers

        session, headers = _build_session()

        def _parse_page(text: str) -> tuple[list[dict[str, Any]], int]:
            soup = BeautifulSoup(text, features="lxml")
            page_count = 1
            page_info = soup.find(name="span", attrs={"class": "page_info"})
            if page_info:
                raw_page = str(page_info.get_text(strip=True) or "")
                parts = raw_page.split("/")
                if len(parts) == 2 and parts[1].isdigit():
                    page_count = max(1, int(parts[1]))

            table = soup.find(name="table", attrs={"class": "m-table"})
            if table is None:
                return [], page_count

            page_items: list[dict[str, Any]] = []
            rows = table.find_all("tr")
            for row in rows[1:]:
                cells = [cell.get_text(" ", strip=True) for cell in row.find_all("td")]
                if len(cells) < 11:
                    continue
                stock_code = str(cells[1] or "").strip()
                if not stock_code:
                    continue
                page_items.append({
                    "stock_code": stock_code,
                    "stock_name": str(cells[2] or "").strip(),
                    "price": safe_float(cells[3]) or 0.0,
                    "change_pct": safe_float(cells[4]) or 0.0,
                    "volume": 0,
                    "amount": parse_numeric(cells[10]) or 0.0,
                    "_source": "ths_concept_detail",
                })
            return page_items, page_count

        def _request_page(url: str, *, retries: int = 3) -> str:
            nonlocal session, headers
            last_exc = None
            for attempt in range(1, retries + 1):
                try:
                    resp = session.get(url, headers=headers, timeout=20)
                    resp.raise_for_status()
                    if "<table" not in resp.text:
                        raise ValueError("response does not contain table")
                    return resp.text
                except Exception as e:
                    last_exc = e
                    if attempt >= retries:
                        break
                    time.sleep(0.35 * attempt)
                    session, headers = _build_session()
            raise last_exc or RuntimeError("ths page request failed")

        first_url = f"https://q.10jqka.com.cn/gn/detail/code/{ths_code}/"
        first_text = _request_page(first_url)
        first_items, page_count = _parse_page(first_text)
        stocks = list(first_items)

        for page in range(2, page_count + 1):
            try:
                page_url = f"https://q.10jqka.com.cn/gn/detail/code/{ths_code}/page/{page}/ajax/1/"
                page_text = _request_page(page_url)
                page_items, _ = _parse_page(page_text)
                if not page_items:
                    safe_stderr_print(f"[BlockStocks] THS concept第{page}页无数据: code={ths_code}")
                    continue
                stocks.extend(page_items)
            except Exception as e:
                safe_stderr_print(f"[BlockStocks] THS concept第{page}页失败(code={ths_code}): {e}")

        deduped: dict[str, dict[str, Any]] = {}
        for item in stocks:
            code = str(item.get("stock_code") or "").strip()
            if code and code not in deduped:
                deduped[code] = item
        return list(deduped.values())
    except Exception as e:
        safe_stderr_print(f"[BlockStocks] THS concept fallback失败(block_code={block_code}, name={block_name}): {e}")
        return []
