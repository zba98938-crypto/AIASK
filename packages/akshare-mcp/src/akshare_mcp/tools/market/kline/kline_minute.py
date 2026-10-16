"""K线数据模块"""

import asyncio
import os
import re
import json
import requests
from datetime import datetime, timedelta
from typing import Optional
from ...market.helpers import (
    normalize_code, safe_float, safe_int, parse_date_input,
    run_with_retry as _run_with_retry,
    _parse_timeout_list as _parse_timeout_list,
    KLINE_TIMEOUTS as _KLINE_TIMEOUTS,
    ok, fail
)
from ....core.cache_manager import cached
from ....core.rate_limiter import get_limiter
from ....core.validators import validate_kline_list
from ....data_source import data_source
from ....provider_contracts import attach_provider_contract_meta
from ....storage import get_db
from ....utils import (
    attach_argument_contract_meta,
    resolve_canonical_arg,
    safe_stderr_print,
    validate_stock_code_format,
)
from ...data_quality import build_quality_meta, infer_missing_fields
try:
    from ...baostock_api import baostock_client
except (ImportError, Exception):
    baostock_client = None
import pandas as pd

_KLINE_TOTAL_TIMEOUT = float(os.getenv("KLINE_TOTAL_TIMEOUT", "45"))
_MINUTE_KLINE_TIMEOUTS = _parse_timeout_list("AKSHARE_MINUTE_KLINE_TIMEOUTS", [4.0, 8.0])
_MINUTE_SINA_TIMEOUT = float(os.getenv("AKSHARE_MINUTE_SINA_TIMEOUT", "6"))


_SOFT_KLINE_FIELDS = frozenset({"turnover", "change_pct"})


_DB_STALE_DAYS = int(os.getenv("KLINE_DB_STALE_DAYS", "5"))

from .kline_rows import (
    _append_chain_step,
    _fail_kline_response,
    _has_validated_kline_rows,
    _is_db_data_fresh,
    _is_fund_like_code,
    _kline_missing_core_fields,
    _kline_missing_fields,
    _kline_row_date,
    _kline_rows_usable,
    _latest_kline_row,
    _ok_kline_response,
    _process_kline_akshare,
    _validated_kline_rows,
)
import logging

logger = logging.getLogger(__name__)

def _parse_minute_period(period: str) -> Optional[int]:
    raw = str(period or "").strip().lower()
    if raw.endswith("m"):
        raw = raw[:-1]
    try:
        minutes = int(raw)
    except ValueError as _swallowed:
        logger.warning("packages/akshare-mcp/src/akshare_mcp/tools/market/kline/kline_minute.py:73: swallowed %s and continued: %s", type(_swallowed).__name__, _swallowed)
        return None
    if minutes in (1, 5, 15, 30, 60):
        return minutes
    return None


def _get_minute_kline_from_sina(code: str, minutes: int, limit: int) -> list[dict]:
    try:
        if code.startswith("6") or code.startswith("68"):
            symbol = f"sh{code}"
        elif code.startswith("8") or code.startswith("4"):
            symbol = f"bj{code}"
        else:
            symbol = f"sz{code}"

        url = (
            "https://quotes.sina.cn/cn/api/jsonp_v2.php/"
            f"data=/CN_MarketDataService.getKLineData?symbol={symbol}&scale={minutes}&ma=no&datalen={limit}"
        )
        resp = requests.get(
            url,
            headers={
                "Referer": "https://finance.sina.com.cn",
                "User-Agent": "Mozilla/5.0",
            },
            timeout=_MINUTE_SINA_TIMEOUT,
        )
    except (requests.exceptions.ProxyError, requests.exceptions.ConnectionError):
        try:
            session = requests.Session()
            session.trust_env = False
            resp = session.get(
                url,
                headers={
                    "Referer": "https://finance.sina.com.cn",
                    "User-Agent": "Mozilla/5.0",
                },
                timeout=_MINUTE_SINA_TIMEOUT,
            )
        except Exception:
            return []
    except Exception:
        return []
    try:
        payload = resp.text or ""
        match = re.search(r"\(\[([\s\S]*?)\]\)", payload)
        if not match:
            return []
        klines = json.loads(f"[{match.group(1)}]")
        results = []
        for item in klines:
            results.append(
                {
                    "date": str(item.get("day") or "")[:19],
                    "open": safe_float(item.get("open")),
                    "close": safe_float(item.get("close")),
                    "high": safe_float(item.get("high")),
                    "low": safe_float(item.get("low")),
                    "volume": safe_int(item.get("volume")),
                    "amount": safe_float(item.get("amount")),
                    "source": "sina",
                }
            )
        return results
    except Exception:
        return []


# P3-5.4 fix: 加 tencent 第三源(诊断报告 §5.4)
# 历史问题:Sina 偶尔被反爬墙或限流,akshare→sina 双源都失败时 AI 拿不到分钟K
# tencent: http://web.ifzq.gtimg.cn/appstock/app/kline/mkline?param=sh600519,m5,,300


def _get_minute_kline_from_eastmoney(code: str, minutes: int, limit: int) -> list[dict]:
    """East money minute bars — the third channel, and a different vendor.

    Sina and Tencent are the two that were always intended here; this one was added
    after a probe found that Tencent's endpoint had been failing on every call, so
    the chain had silently held a single working member. It reports 不复权 prices
    (``fqt=0``) like Sina, and its volume is 手, converted below.
    """

    try:
        secid = ("1." if code.startswith(("6", "5")) else "0.") + str(code)
        session = requests.Session()
        session.trust_env = False
        resp = session.get(
            "https://push2his.eastmoney.com/api/qt/stock/kline/get",
            params={
                "secid": secid,
                "fields1": "f1,f2,f3",
                "fields2": "f51,f52,f53,f54,f55,f56",
                "klt": int(minutes),
                "fqt": 0,
                "end": "20500101",
                "lmt": int(limit),
            },
            headers={"Referer": "https://quote.eastmoney.com/", "User-Agent": "Mozilla/5.0"},
            timeout=_MINUTE_SINA_TIMEOUT,
        )
        resp.raise_for_status()
        klines = (resp.json().get("data") or {}).get("klines") or []
    except Exception:
        return []

    results: list[dict] = []
    for item in klines:
        parts = str(item).split(",")
        # f51 date, f52 open, f53 close, f54 high, f55 low, f56 volume(手)
        if len(parts) < 6:
            continue
        try:
            results.append(
                {
                    "date": parts[0],
                    "open": safe_float(parts[1]),
                    "close": safe_float(parts[2]),
                    "high": safe_float(parts[3]),
                    "low": safe_float(parts[4]),
                    "volume": safe_int(parts[5]) * 100,
                    "amount": 0.0,
                    "source": "eastmoney",
                }
            )
        except Exception:
            continue
    return results
def _get_minute_kline_from_tencent(code: str, minutes: int, limit: int) -> list[dict]:
    try:
        if code.startswith("6") or code.startswith("68"):
            tx_code = f"sh{code}"
        elif code.startswith("8") or code.startswith("4"):
            tx_code = f"bj{code}"
        else:
            tx_code = f"sz{code}"
        period_param = f"m{int(minutes)}"
        # `web.ifzq.gtimg.cn` closes the TLS connection early on this path — over
        # https and plain http alike — while the daily endpoint answers on both, so
        # this looked like a TLS fault rather than a hostname one. The unsuffixed
        # host serves the same payload. Every minute call from this project was
        # failing here, which is why the tool's source chain never left Sina.
        url = (
            "https://ifzq.gtimg.cn/appstock/app/kline/mkline"
            f"?param={tx_code},{period_param},,{int(limit)}"
        )
        try:
            resp = requests.get(
                url,
                headers={
                    "Referer": "https://gu.qq.com",
                    "User-Agent": "Mozilla/5.0",
                },
                timeout=_MINUTE_SINA_TIMEOUT,
            )
        except (requests.exceptions.ProxyError, requests.exceptions.ConnectionError):
            session = requests.Session()
            session.trust_env = False
            resp = session.get(
                url,
                headers={
                    "Referer": "https://gu.qq.com",
                    "User-Agent": "Mozilla/5.0",
                },
                timeout=_MINUTE_SINA_TIMEOUT,
            )

        try:
            payload = resp.json()
        except Exception:
            return []
        # tencent payload: {"code":0, "data":{"sh600519":{"m5":[ [date,open,close,high,low,vol], ...]}}}
        if not isinstance(payload, dict) or payload.get("code") != 0:
            return []
        data = payload.get("data") or {}
        stock_block = data.get(tx_code) or {}
        rows = stock_block.get(period_param) or stock_block.get("data") or []
        if not isinstance(rows, list):
            return []
        results: list[dict] = []
        for row in rows[-int(limit):]:
            if not isinstance(row, list) or len(row) < 6:
                continue
            # row: [time_str, open, close, high, low, volume, *amount]
            try:
                date_str = str(row[0])
                # tencent 时间格式: "202602031430" 或 "20260203143000"
                if len(date_str) >= 12:
                    formatted = (
                        f"{date_str[0:4]}-{date_str[4:6]}-{date_str[6:8]} "
                        f"{date_str[8:10]}:{date_str[10:12]}"
                    )
                    if len(date_str) >= 14:
                        formatted += f":{date_str[12:14]}"
                    else:
                        formatted += ":00"
                else:
                    formatted = date_str
                results.append({
                    "date": formatted,
                    "open": safe_float(row[1]),
                    "close": safe_float(row[2]),
                    "high": safe_float(row[3]),
                    "low": safe_float(row[4]),
                    # Tencent reports 手 and Sina reports 股. Writing them through
                    # unconverted mixes the two in one column, so a fallback bar
                    # would read 100x smaller than its neighbour from the primary.
                    "volume": safe_int(row[5]) * 100,
                    # Tencent puts a dict at index 6, so `safe_float` swallowed a
                    # TypeError once per row — 300 disclosed-and-ignored errors per
                    # call, which is enough noise to hide a real one.
                    "amount": safe_float(row[6]) if len(row) > 6 and isinstance(row[6], (int, float, str)) else 0.0,
                    "source": "tencent",
                })
            except Exception as _swallowed:
                logger.warning("packages/akshare-mcp/src/akshare_mcp/tools/market/kline/kline_minute.py:250: swallowed %s and continued: %s", type(_swallowed).__name__, _swallowed)
                continue
        return results
    except Exception:
        return []
