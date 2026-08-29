"""实时行情模块"""

import asyncio
import requests
from datetime import datetime
from typing import Optional
from ..market.helpers import (
    normalize_code, safe_float, safe_int, pick_value, parse_numeric,
    get_spot_indexed as _get_spot_indexed,
    get_index_spot_indexed as _get_index_spot_indexed,
    get_name_map as _get_name_map,
    run_with_retry as _run_with_retry,
    QUOTE_TIMEOUTS as _QUOTE_TIMEOUTS,
    _SPOT_STALE_SECONDS, _INDEX_STALE_SECONDS,
    ok, fail
)
from ...core.cache_manager import cached
from ...core.rate_limiter import get_limiter
from ...core.validators import validate_quote
from ...data_source import data_source
from ...storage import get_db, run_with_db_cleanup
from ...utils import safe_stderr_print
from ..data_quality import build_quality_meta, infer_missing_fields, normalize_reason_list
from ...services.background_tasks import track_background_task
try:
    import akshare as ak
except ImportError:
    ak = None
import pandas as pd

# 显式声明需要通过 `from ._quote_support import *` 导出的私有 helper。
# Python 的 import * 默认跳过下划线开头的名称，因此必须在 __all__ 中列明。
__all__ = [
    "_current_data_timestamp",
    "_log_quote_source_error",
    "_get_daily_snapshot",
    "_coalesce_price",
    "_calc_change",
    "_normalize_quote_for_storage",
    "_save_quote_best_effort",
    "_save_quote_nonblocking",
    "_save_quotes_nonblocking",
    "_quote_missing_fields",
    "_backfill_prev_close",
    "_ok_quote_response",
    "_fail_quote_response",
    "_get_minute_quote",
    "_get_daily_quote",
    "_get_quote_sina",
    "_get_quote_tencent",
    "_get_realtime_quote_akshare",
]

def _current_data_timestamp() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")

def _log_quote_source_error(stage: str, code: str, err: Exception) -> None:
    safe_stderr_print(f"[quote] {stage} failed for {code}: {err}")

def _get_daily_snapshot(code: str) -> dict[str, Optional[float]]:
    """获取日K快照（用于补充 open/high/low/prev_close）

    降级链: DataSource(Tushare/公开源) → AkShare
    """
    # 1. 优先 DataSource
    try:
        ds_results = data_source.get_kline(code, "daily", 2)
        if ds_results and len(ds_results) >= 1:
            row = ds_results[-1]
            prev_close = safe_float(ds_results[-2].get("close")) if len(ds_results) >= 2 else None
            return {
                "open": safe_float(row.get("open")),
                "high": safe_float(row.get("high")),
                "low": safe_float(row.get("low")),
                "prev_close": prev_close,
            }
    except Exception as e:
        _log_quote_source_error("daily snapshot datasource", code, e)

    # 2. 降级 AkShare
    if ak is None:
        return {}
    try:
        df = _run_with_retry(
            lambda: ak.stock_zh_a_hist(symbol=code, period="daily", adjust=""),
            _QUOTE_TIMEOUTS,
        )
        if df is None or df.empty:
            return {}
        row = df.iloc[-1]
        prev_close = safe_float(df.iloc[-2].get("收盘")) if len(df) >= 2 else None
        return {
            "open": safe_float(row.get("开盘")),
            "high": safe_float(row.get("最高")),
            "low": safe_float(row.get("最低")),
            "prev_close": prev_close,
        }
    except Exception as e:
        _log_quote_source_error("daily snapshot akshare", code, e)
        return {}

def _coalesce_price(value: Optional[float], fallback: Optional[float]) -> Optional[float]:
    if value is None or value == 0:
        return fallback
    return value

def _calc_change(price: Optional[float], prev_close: Optional[float]) -> tuple[Optional[float], Optional[float]]:
    if price is None or prev_close is None or prev_close == 0:
        return None, None
    change = price - prev_close
    return change, (change / prev_close) * 100

def _normalize_quote_for_storage(payload: dict) -> Optional[dict]:
    """将行情对象标准化为 DB save_quote 可接收结构。"""
    if not isinstance(payload, dict):
        return None
    code = normalize_code(str(payload.get("code") or "").strip()) if payload.get("code") else None
    price = safe_float(payload.get("price"))
    if not code or price is None:
        return None

    return {
        "code": code,
        "name": payload.get("name") or "",
        "price": price,
        "change": safe_float(payload.get("change")),
        "change_pct": safe_float(payload.get("changePercent") if payload.get("changePercent") is not None else payload.get("change_pct")),
        "open": safe_float(payload.get("open")),
        "high": safe_float(payload.get("high")),
        "low": safe_float(payload.get("low")),
        "pre_close": safe_float(payload.get("preClose") if payload.get("preClose") is not None else payload.get("pre_close")),
        "volume": safe_int(payload.get("volume")),
        "amount": safe_float(payload.get("amount")),
        "source": payload.get("source") or "unknown",
    }

async def _save_quote_best_effort(payload: dict) -> None:
    """尽力落库：失败不影响主流程返回。"""
    try:
        normalized = _normalize_quote_for_storage(payload)
        if not normalized:
            return
        db = get_db()
        await db.save_quote(normalized)
    except Exception as e:
        safe_stderr_print(f"[quote] save_quote skipped: {e}")

def _save_quote_nonblocking(payload: dict) -> None:
    """在同步工具函数中安全触发异步落库。"""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop and loop.is_running():
        try:
            track_background_task(_save_quote_best_effort(payload), name="quote-save")
        except Exception as e:
            safe_stderr_print(f"[quote] create save task failed: {e}")
        return

    try:
        run_with_db_cleanup(_save_quote_best_effort(payload))
    except Exception as e:
        safe_stderr_print(f"[quote] save_quote run failed: {e}")

def _save_quotes_nonblocking(items: list[dict]) -> None:
    """批量尽力落库（失败不影响主流程）。"""
    if not isinstance(items, list) or not items:
        return

    async def _runner() -> None:
        for item in items:
            await _save_quote_best_effort(item)

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop and loop.is_running():
        try:
            track_background_task(_runner(), name="quote-batch-save")
        except Exception as e:
            safe_stderr_print(f"[quote] create batch save task failed: {e}")
        return

    try:
        run_with_db_cleanup(_runner())
    except Exception as e:
        safe_stderr_print(f"[quote] batch save run failed: {e}")

def _quote_missing_fields(payload: dict) -> list[str]:
    return infer_missing_fields(
        payload,
        ("code", "name", "price", "open", "high", "low", "preClose", "volume", "amount"),
    )

def _backfill_prev_close(payload: dict, code: str) -> dict:
    if not isinstance(payload, dict) or payload.get("preClose"):
        return payload
    try:
        snap = _get_daily_snapshot(code)
        prev_close = snap.get("prev_close")
        if prev_close is None:
            return payload
        payload["preClose"] = prev_close
        if payload.get("change") is None:
            change, change_pct = _calc_change(safe_float(payload.get("price")), prev_close)
            payload["change"] = change
            payload["changePercent"] = change_pct
    except Exception as e:
        _log_quote_source_error("prev_close backfill", code, e)
    return payload

def _ok_quote_response(
    payload: dict,
    *,
    attempted_sources: list[str],
    source_chain: list[str],
    fallback_reason: Optional[str] = None,
) -> dict:
    response = ok(payload, cached=False)
    normalized_reasons = normalize_reason_list(fallback_reason)
    if isinstance(response.get("data"), dict):
        data = response["data"]
        data["attempted_sources"] = attempted_sources
        data["source_chain"] = source_chain
        data["fallback_used"] = len(source_chain) > 1 or (source_chain and source_chain[0] != "data_source")
        data["fallback_reason"] = fallback_reason
        data["data_timestamp"] = payload.get("data_timestamp") or _current_data_timestamp()
        _save_quote_nonblocking(data)
    response.update(
        build_quality_meta(
            source=str(payload.get("source") or "unknown"),
            source_chain=source_chain,
            fallback_reason=normalized_reasons,
            asof_value=payload.get("time") or payload.get("trade_time") or payload.get("data_timestamp"),
            missing_fields=_quote_missing_fields(payload),
            degraded=bool(_quote_missing_fields(payload)),
            success=True,
        )
    )
    return response

def _fail_quote_response(
    message: str,
    *,
    attempted_sources: list[str],
    source_chain: list[str],
    fallback_reason: Optional[str] = None,
) -> dict:
    response = fail(message)
    response["attempted_sources"] = attempted_sources
    response.update(
        build_quality_meta(
            source="none",
            source_chain=source_chain or attempted_sources,
            fallback_reason=normalize_reason_list(fallback_reason or message),
            asof_value=None,
            missing_fields=[],
            degraded=True,
            success=False,
        )
    )
    response["source"] = "none"
    return response

def _get_minute_quote(code: str) -> dict:
    """获取分钟行情（用于实时价格）

    降级链: DataSource(分钟K) → AkShare
    """
    # 1. 优先 DataSource 分钟K线
    try:
        ds_results = data_source.get_kline(code, "1m", 240)
        if ds_results:
            sample_date = str(ds_results[0].get('date', ''))
            if len(sample_date) > 10:  # 确认是分钟数据
                last_row = ds_results[-1]
                first_row = ds_results[0]
                price = safe_float(last_row.get("close"))
                if price is not None:
                    day_high = max((safe_float(r.get("high")) or 0) for r in ds_results)
                    day_low = min((safe_float(r.get("low")) or float('inf')) for r in ds_results if safe_float(r.get("low")))
                    day_volume = sum((safe_int(r.get("volume")) or 0) for r in ds_results)
                    day_amount = sum((safe_float(r.get("amount")) or 0) for r in ds_results)
                    return {
                        "price": price,
                        "open": safe_float(first_row.get("open")),
                        "high": day_high if day_high > 0 else None,
                        "low": day_low if day_low < float('inf') else None,
                        "volume": day_volume,
                        "amount": day_amount,
                        "time": str(last_row.get("date", ""))[:19],
                    }
    except Exception as e:
        _log_quote_source_error("minute quote datasource", code, e)

    # 2. 降级 AkShare
    if ak is None:
        raise RuntimeError(f"无法获取 {code} 分钟行情 (DataSource 不可用, akshare 未安装)")
    df = _run_with_retry(
        lambda: ak.stock_zh_a_hist_min_em(symbol=code, period="1", adjust=""),
        _QUOTE_TIMEOUTS,
    )
    if df is None or df.empty:
        raise RuntimeError(f"未获取到 {code} 分钟行情数据")
    last_row = df.iloc[-1]
    first_row = df.iloc[0]
    price = safe_float(last_row.get("收盘"))
    if price is None:
        raise RuntimeError(f"{code} 分钟行情缺少收盘价")
    day_open = safe_float(first_row.get("开盘"))
    day_high = safe_float(df["最高"].max()) if "最高" in df.columns else safe_float(last_row.get("最高"))
    day_low = safe_float(df["最低"].min()) if "最低" in df.columns else safe_float(last_row.get("最低"))
    if day_open is not None and day_open <= 0:
        day_open = None
    if day_high is not None and day_high <= 0:
        day_high = None
    if day_low is not None and day_low <= 0:
        day_low = None
    day_volume = safe_int(df["成交量"].sum()) if "成交量" in df.columns else safe_int(last_row.get("成交量"))
    day_amount = safe_float(df["成交额"].sum()) if "成交额" in df.columns else safe_float(last_row.get("成交额"))
    return {
        "price": price,
        "open": day_open,
        "high": day_high,
        "low": day_low,
        "volume": day_volume,
        "amount": day_amount,
        "time": str(last_row.get("时间", "")),
    }

def _get_daily_quote(code: str, name: str) -> Optional[dict]:
    """获取日K线行情（降级用）

    降级链: DataSource(Tushare/公开源) → AkShare
    """
    # 1. 优先 DataSource
    try:
        ds_results = data_source.get_kline(code, "daily", 2)
        if ds_results and len(ds_results) >= 1:
            row = ds_results[-1]
            prev_close = safe_float(ds_results[-2].get("close")) if len(ds_results) >= 2 else None
            price = safe_float(row.get("close"))
            if price is not None:
                change, change_pct = _calc_change(price, prev_close)
                return {
                    "code": code,
                    "name": name,
                    "price": price,
                    "change": change,
                    "changePercent": change_pct,
                    "open": safe_float(row.get("open")),
                    "high": safe_float(row.get("high")),
                    "low": safe_float(row.get("low")),
                    "preClose": prev_close,
                    "volume": safe_int(row.get("volume")),
                    "amount": safe_float(row.get("amount")),
                    "fallback": "daily_kline",
                }
    except Exception as e:
        _log_quote_source_error("daily quote datasource", code, e)

    # 2. 降级 AkShare
    if ak is None:
        return None
    try:
        df = _run_with_retry(
            lambda: ak.stock_zh_a_hist(symbol=code, period="daily", adjust="qfq"),
            _QUOTE_TIMEOUTS,
        )
        if df is None or df.empty:
            return None
        row = df.iloc[-1]
        prev_close = safe_float(df.iloc[-2].get("收盘")) if len(df) >= 2 else None
        price = safe_float(row.get("收盘"))
        if price is None:
            return None
        return {
            "code": code,
            "name": name,
            "price": price,
            "change": safe_float(row.get("涨跌额")),
            "changePercent": safe_float(row.get("涨跌幅")),
            "open": safe_float(row.get("开盘")),
            "high": safe_float(row.get("最高")),
            "low": safe_float(row.get("最低")),
            "preClose": prev_close,
            "volume": safe_int(row.get("成交量")),
            "amount": safe_float(row.get("成交额")),
            "fallback": "daily_kline",
        }
    except Exception as e:
        _log_quote_source_error("daily quote akshare", code, e)
        return None

def _get_quote_sina(code: str) -> Optional[dict]:
    """Fallback: Get quote from Sina interface"""
    def _http_get_with_https_preferred(url_https: str, url_http: str, headers: dict, timeout: int = 5):
        try:
            resp = requests.get(url_https, headers=headers, timeout=timeout)
            return resp, "https"
        except Exception as https_err:
            safe_stderr_print(f"[quote] HTTPS fallback source failed, try HTTP: {https_err}")
            resp = requests.get(url_http, headers=headers, timeout=timeout)
            return resp, "http_fallback"

    try:
        symbol = normalize_code(code)
        if symbol.startswith("0") or symbol.startswith("3"):
            sina_code = f"sz{symbol}"
        else:
            sina_code = f"sh{symbol}"

        url_https = f"https://hq.sinajs.cn/list={sina_code}"
        url_http = f"http://hq.sinajs.cn/list={sina_code}"
        headers = {"Referer": "https://finance.sina.com.cn/"}
        resp, transport = _http_get_with_https_preferred(url_https, url_http, headers=headers, timeout=5)
        text = resp.text
        if "=" not in text or '="' not in text:
            return None

        content = text.split('="')[1].strip('";\n')
        if not content:
            return None

        parts = content.split(",")
        if len(parts) < 30:
            return None

        name = parts[0]
        open_ = safe_float(parts[1])
        pre_close = safe_float(parts[2])
        price = safe_float(parts[3])
        high = safe_float(parts[4])
        low = safe_float(parts[5])
        volume = safe_int(parts[8])
        amount = safe_float(parts[9])

        change = None
        change_pct = None
        if price is not None and pre_close is not None and pre_close > 0:
            change = price - pre_close
            change_pct = (change / pre_close) * 100

        return {
            "code": symbol,
            "name": name,
            "price": price,
            "change": change,
            "changePercent": change_pct,
            "open": open_,
            "high": high,
            "low": low,
            "preClose": pre_close,
            "volume": volume,
            "amount": amount,
            "source": f"sina_{transport}",
        }
    except Exception:
        return None

def _get_quote_tencent(code: str) -> Optional[dict]:
    """Fallback: Get quote from Tencent interface"""
    def _http_get_with_https_preferred(url_https: str, url_http: str, timeout: int = 5):
        try:
            resp = requests.get(url_https, timeout=timeout)
            return resp, "https"
        except Exception as https_err:
            safe_stderr_print(f"[quote] HTTPS fallback source failed, try HTTP: {https_err}")
            resp = requests.get(url_http, timeout=timeout)
            return resp, "http_fallback"

    try:
        symbol = normalize_code(code)
        if symbol.startswith("0") or symbol.startswith("3"):
            qt_code = f"sz{symbol}"
        else:
            qt_code = f"sh{symbol}"

        url_https = f"https://qt.gtimg.cn/q={qt_code}"
        url_http = f"http://qt.gtimg.cn/q={qt_code}"
        resp, transport = _http_get_with_https_preferred(url_https, url_http, timeout=5)
        text = resp.text
        if "=" not in text or '="' not in text:
            return None

        content = text.split('="')[1].strip('";\n')
        if not content:
            return None

        parts = content.split("~")
        if len(parts) < 40:
            return None

        name = parts[1]
        price = safe_float(parts[3])
        pre_close = safe_float(parts[4])
        open_ = safe_float(parts[5])
        high = safe_float(parts[33])
        low = safe_float(parts[34])
        volume = safe_int(parts[6])
        amount_raw = safe_float(parts[37])
        amount = amount_raw * 10000 if amount_raw is not None else None

        change = None
        change_pct = None
        if price is not None and pre_close is not None and pre_close > 0:
            change = price - pre_close
            change_pct = (change / pre_close) * 100

        return {
            "code": symbol,
            "name": name,
            "price": price,
            "change": change,
            "changePercent": change_pct,
            "open": open_,
            "high": high,
            "low": low,
            "preClose": pre_close,
            "volume": volume * 100 if volume is not None else None,
            "amount": amount,
            "source": f"tencent_{transport}",
        }
    except Exception:
        return None

def _get_realtime_quote_akshare(code: str) -> Optional[dict]:
    """从 AkShare 获取实时行情（降级用）；仅在需补 name 时调 _get_name_map。"""
    def _lazy_name():
        _m = _get_name_map()
        return _m.get(code, "")

    # 策略1: 优先尝试分钟K线（最快，单只股票）
    try:
        minute = _get_minute_quote(code)
        if minute and minute.get("price"):
            snapshot = _get_daily_snapshot(code)
            prev_close = snapshot.get("prev_close")
            change, change_pct = _calc_change(minute.get("price"), prev_close)
            return {
                "code": code,
                "name": _lazy_name(),
                "price": minute.get("price"),
                "change": change,
                "changePercent": change_pct,
                "open": _coalesce_price(minute.get("open"), snapshot.get("open")),
                "high": _coalesce_price(minute.get("high"), snapshot.get("high")),
                "low": _coalesce_price(minute.get("low"), snapshot.get("low")),
                "preClose": prev_close,
                "volume": minute.get("volume"),
                "amount": minute.get("amount"),
                "time": minute.get("time"),
                "source": "akshare_minute"
            }
    except (TimeoutError, RuntimeError, Exception) as e:
        safe_stderr_print(f"Minute quote failed for {code}: {e}")

    # 策略2: 尝试日K线（单只股票）
    try:
        daily = _get_daily_quote(code, _lazy_name())
        if daily and daily.get("price"):
            daily["source"] = "akshare_daily"
            return daily
    except (TimeoutError, RuntimeError, Exception) as e:
        safe_stderr_print(f"Daily quote failed for {code}: {e}")

    # 策略3: 降级到全市场数据（较慢，但数据完整）
    try:
        df, cached = _get_spot_indexed()
        if code in df.index:
            r = df.loc[code]
            spot_name = pick_value(r, ["名称", "股票简称"]) or _lazy_name()
            price = safe_float(pick_value(r, ["最新价", "最新", "现价"]))
            if price is not None:
                return {
                    "code": code,
                    "name": str(spot_name or ""),
                    "price": price,
                    "change": safe_float(pick_value(r, ["涨跌额", "涨跌"])),
                    "changePercent": safe_float(pick_value(r, ["涨跌幅", "涨幅"])),
                    "open": safe_float(pick_value(r, ["今开", "开盘"])),
                    "high": safe_float(pick_value(r, ["最高", "最高价"])),
                    "low": safe_float(pick_value(r, ["最低", "最低价"])),
                    "preClose": safe_float(pick_value(r, ["昨收", "昨收价"])),
                    "volume": safe_int(pick_value(r, ["成交量"])),
                    "amount": safe_float(pick_value(r, ["成交额"])),
                    "turnoverRate": safe_float(pick_value(r, ["换手率"])),
                    "source": "akshare_spot"
                }
    except (TimeoutError, RuntimeError, Exception) as e:
        safe_stderr_print(f"Spot market failed for {code}: {e}")

    return None
