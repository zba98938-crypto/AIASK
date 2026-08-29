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
try:
    import akshare as ak
except ImportError:
    ak = None
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

def _parse_minute_period(period: str) -> Optional[int]:
    raw = str(period or "").strip().lower()
    if raw.endswith("m"):
        raw = raw[:-1]
    try:
        minutes = int(raw)
    except ValueError:
        return None
    if minutes in (1, 5, 15, 30, 60):
        return minutes
    return None


def _get_minute_kline_from_akshare(code: str, minutes: int, limit: int) -> list[dict]:
    try:
        df = _run_with_retry(
            lambda: ak.stock_zh_a_hist_min_em(symbol=code, period=str(minutes), adjust=""),
            _MINUTE_KLINE_TIMEOUTS,
        )
    except Exception:
        return []
    if df is None or df.empty:
        return []
    df = df.tail(int(limit))
    results = []
    for _, row in df.iterrows():
        ts = row.get("时间") or row.get("日期") or row.get("time") or row.get("date")
        date_str = str(ts)[:19]
        results.append(
            {
                "date": date_str,
                "open": safe_float(row.get("开盘") or row.get("open")),
                "close": safe_float(row.get("收盘") or row.get("close")),
                "high": safe_float(row.get("最高") or row.get("high")),
                "low": safe_float(row.get("最低") or row.get("low")),
                "volume": safe_int(row.get("成交量") or row.get("volume")),
                "amount": safe_float(row.get("成交额") or row.get("amount")),
                "source": "akshare_minute",
            }
        )
    return results


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
def _get_minute_kline_from_tencent(code: str, minutes: int, limit: int) -> list[dict]:
    try:
        if code.startswith("6") or code.startswith("68"):
            tx_code = f"sh{code}"
        elif code.startswith("8") or code.startswith("4"):
            tx_code = f"bj{code}"
        else:
            tx_code = f"sz{code}"
        period_param = f"m{int(minutes)}"
        url = (
            "https://web.ifzq.gtimg.cn/appstock/app/kline/mkline"
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
                    "volume": safe_int(row[5]),
                    "amount": safe_float(row[6]) if len(row) > 6 else 0.0,
                    "source": "tencent",
                })
            except Exception:
                continue
        return results
    except Exception:
        return []
