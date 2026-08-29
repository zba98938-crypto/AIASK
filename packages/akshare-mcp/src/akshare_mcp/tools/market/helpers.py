"""市场数据工具 - 共享辅助函数和缓存

P1 重构: 核心数据获取函数脱离 AkShare 依赖
- get_spot_indexed() → 东财 push2 API (主) → AkShare (降级)
- get_index_spot_indexed() → 东财 push2 API (主) → AkShare (降级)
- get_stock_list_cached() → Tushare stock_basic (主) → AkShare (降级)
"""

import os
import re
import time
import contextlib
import io
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from threading import Lock
from typing import Any, Optional

try:
    import akshare as ak
except ImportError:
    ak = None
import pandas as pd
import requests

from ...utils import (
    enrich_response_meta,
    normalize_code,
    safe_float,
    safe_int,
    pick_value,
    parse_numeric,
    parse_date_input,
    safe_stderr_print,
    ok as _base_ok,
    fail as _base_fail,
)

# 配置常量
_SPOT_TTL_SECONDS = float(os.getenv("AKSHARE_SPOT_TTL_SECONDS", "2"))
_INDEX_SPOT_TTL_SECONDS = float(os.getenv("AKSHARE_INDEX_SPOT_TTL_SECONDS", "5"))
_STOCK_LIST_TTL_SECONDS = float(os.getenv("AKSHARE_STOCK_LIST_TTL_SECONDS", "86400"))
_STOCK_LIST_STALE_SECONDS = float(os.getenv("AKSHARE_STOCK_LIST_STALE_SECONDS", "604800"))
_SPOT_TIMEOUT_SECONDS = float(os.getenv("AKSHARE_SPOT_TIMEOUT_SECONDS", "15"))
_INDEX_TIMEOUT_SECONDS = float(os.getenv("AKSHARE_INDEX_TIMEOUT_SECONDS", "45"))
_SPOT_STALE_SECONDS = float(os.getenv("AKSHARE_SPOT_STALE_SECONDS", "30"))
_INDEX_STALE_SECONDS = float(os.getenv("AKSHARE_INDEX_STALE_SECONDS", "60"))
_RETRY_SLEEP_SECONDS = float(os.getenv("AKSHARE_RETRY_SLEEP_SECONDS", "1.0"))
_MINUTE_BATCH_LIMIT = int(os.getenv("AKSHARE_MINUTE_BATCH_LIMIT", "50"))
_BATCH_FALLBACK_LIMIT = int(os.getenv("AKSHARE_BATCH_FALLBACK_LIMIT", "200"))


def _parse_timeout_list(env_key: str, default: list[float]) -> list[float]:
    raw = os.getenv(env_key, "").strip()
    if not raw:
        return default
    parts = [p for p in re.split(r"[,\s]+", raw) if p]
    timeouts: list[float] = []
    for part in parts:
        try:
            timeouts.append(float(part))
        except ValueError:
            continue
    return timeouts or default


QUOTE_TIMEOUTS = _parse_timeout_list("AKSHARE_QUOTE_TIMEOUTS", [8.0, 15.0])
KLINE_TIMEOUTS = _parse_timeout_list("AKSHARE_KLINE_TIMEOUTS", [15.0, 25.0])


# 缓存
_spot_lock = Lock()
_spot_cache: dict[str, Any] = {"indexed": None, "ts": 0.0}

_index_lock = Lock()
_index_cache: dict[str, Any] = {"indexed": None, "ts": 0.0}

_list_lock = Lock()
_list_cache: dict[str, Any] = {"data": None, "ts": 0.0}

_spot_executor: ThreadPoolExecutor | None = None
_spot_executor_lock = Lock()


def _get_spot_executor() -> ThreadPoolExecutor:
    global _spot_executor
    with _spot_executor_lock:
        if _spot_executor is None:
            _spot_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="market-spot")
        return _spot_executor


def shutdown_spot_executor(*, wait: bool = False) -> None:
    global _spot_executor
    with _spot_executor_lock:
        executor = _spot_executor
        _spot_executor = None
    if executor is not None:
        executor.shutdown(wait=wait, cancel_futures=True)


def _call_silenced(fn):
    """Run noisy upstream APIs without corrupting stdio MCP transport."""
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        return fn()


def run_with_timeout(fn, timeout: float) -> Any:
    """带超时的函数执行"""
    future = _get_spot_executor().submit(_call_silenced, fn)
    try:
        return future.result(timeout=timeout)
    except FuturesTimeoutError:
        future.cancel()
        raise TimeoutError(f"请求超时（>{timeout}s）")
    except Exception as e:
        raise RuntimeError(f"请求失败: {e}")


def run_with_retry(fn, timeouts: list[float]) -> Any:
    """带重试的函数执行"""
    last_error: Optional[Exception] = None
    for timeout in timeouts:
        try:
            return run_with_timeout(fn, timeout)
        except Exception as exc:
            last_error = exc
            safe_stderr_print(f"[helpers] 请求失败 (timeout={timeout}s): {exc}")
            if _RETRY_SLEEP_SECONDS > 0:
                time.sleep(_RETRY_SLEEP_SECONDS)
    if last_error:
        raise last_error
    raise RuntimeError("请求失败")


# ============================================================
# 东财 push2 API — 全市场A股实时行情
# ============================================================

def _eastmoney_get(url: str, params: dict, timeout: float = 15) -> Optional[dict]:
    """东财 HTTP GET，自动处理代理问题（先默认请求，ProxyError 时绕过系统代理直连）"""
    try:
        resp = requests.get(url, params=params, timeout=timeout)
        if resp.status_code == 200:
            return resp.json()
    except (requests.exceptions.ProxyError, requests.exceptions.ConnectionError):
        # 系统代理不通，用 trust_env=False 绕过系统代理直连
        try:
            session = requests.Session()
            session.trust_env = False
            resp = session.get(url, params=params, timeout=timeout)
            if resp.status_code == 200:
                return resp.json()
        except Exception:
            pass
    except Exception:
        pass
    return None


def _parse_eastmoney_spot(payload: Optional[dict]) -> Optional[pd.DataFrame]:
    """解析东财 push2 返回的行情数据为兼容 DataFrame"""
    if not payload:
        return None
    items = payload.get("data", {}).get("diff", [])
    if not items:
        return None
    records = []
    for i in items:
        code = str(i.get("f12", ""))
        if not code:
            continue
        records.append({
            "代码": code,
            "名称": str(i.get("f14", "")),
            "最新价": i.get("f2"),
            "涨跌幅": i.get("f3"),
            "涨跌额": i.get("f4"),
            "成交量": i.get("f5"),
            "成交额": i.get("f6"),
            "振幅": i.get("f7"),
            "最高": i.get("f15"),
            "最低": i.get("f16"),
            "今开": i.get("f17"),
            "昨收": i.get("f18"),
        })
    return pd.DataFrame(records) if records else None


def _fetch_all_a_spot_eastmoney() -> Optional[pd.DataFrame]:
    """东方财富 push2 API 获取全市场A股实时行情
    
    返回与原 ak.stock_zh_a_spot_em() 兼容的 DataFrame 列名:
    代码, 名称, 最新价, 涨跌幅, 涨跌额, 成交量, 成交额, 今开, 最高, 最低, 昨收
    """
    url = "https://push2.eastmoney.com/api/qt/clist/get"
    params = {
        "pn": 1, "pz": 6000, "po": 1, "np": 1, "fltt": 2, "invt": 2,
        # 沪A + 深A + 北交所
        "fs": "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23,m:0+t:81+s:2048",
        "fields": "f12,f14,f2,f3,f4,f5,f6,f7,f15,f16,f17,f18",
    }
    try:
        payload = _eastmoney_get(url, params, timeout=15)
        return _parse_eastmoney_spot(payload)
    except Exception as e:
        safe_stderr_print(f"[helpers] 东财 push2 全市场行情失败: {e}")
        return None


def get_spot_indexed() -> tuple[pd.DataFrame, bool]:
    """获取A股实时行情索引
    
    降级链: 东财 push2 API → AkShare (如可用)
    返回以 '代码' 为索引的 DataFrame。
    """
    now = time.time()
    with _spot_lock:
        indexed = _spot_cache.get("indexed")
        ts = float(_spot_cache.get("ts") or 0.0)
        if indexed is not None and (now - ts) < _SPOT_TTL_SECONDS:
            return indexed, True

        df = None

        # 1. 主路径: 东财 push2 API
        try:
            df = _fetch_all_a_spot_eastmoney()
        except Exception:
            df = None

        # 2. 降级: AkShare (如可用)
        if (df is None or df.empty) and ak is not None:
            try:
                df = run_with_timeout(ak.stock_zh_a_spot_em, _SPOT_TIMEOUT_SECONDS)
            except Exception:
                df = None
        
        if (df is None or df.empty) and ak is not None:
            try:
                df = run_with_timeout(ak.stock_zh_a_spot, _SPOT_TIMEOUT_SECONDS)
            except Exception as e:
                stale_indexed = _spot_cache.get("indexed")
                stale_ts = float(_spot_cache.get("ts") or 0.0)
                if stale_indexed is not None and (now - stale_ts) < _SPOT_STALE_SECONDS:
                    return stale_indexed, True
                raise e
        
        if df is None or df.empty:
            # 尝试返回过期缓存
            stale_indexed = _spot_cache.get("indexed")
            stale_ts = float(_spot_cache.get("ts") or 0.0)
            if stale_indexed is not None and (now - stale_ts) < _SPOT_STALE_SECONDS:
                return stale_indexed, True
            raise RuntimeError("未获取到A股全市场行情")

        if "代码" not in df.columns:
            raise RuntimeError("A股行情缺少代码列")

        df["代码"] = df["代码"].apply(normalize_code)
        indexed = df.set_index("代码", drop=False)

        _spot_cache["indexed"] = indexed
        _spot_cache["ts"] = now
        return indexed, False


# ============================================================
# 东财 push2 API — 指数实时行情
# ============================================================

def _fetch_index_spot_eastmoney() -> Optional[pd.DataFrame]:
    """东方财富 push2 API 获取指数实时行情
    
    返回与原 ak.stock_zh_index_spot_em() 兼容的 DataFrame 列名:
    代码, 名称, 最新价, 涨跌幅, 涨跌额, 成交量, 成交额, 今开, 最高, 最低, 昨收
    """
    url = "https://push2.eastmoney.com/api/qt/clist/get"
    params = {
        "pn": 1, "pz": 500, "po": 1, "np": 1, "fltt": 2, "invt": 2,
        # 上证指数(m:1+s:2) + 深证指数(m:0+t:5)
        "fs": "m:1+s:2,m:0+t:5",
        "fields": "f12,f14,f2,f3,f4,f5,f6,f15,f16,f17,f18",
    }
    try:
        payload = _eastmoney_get(url, params, timeout=15)
        if not payload:
            return None
        items = payload.get("data", {}).get("diff", [])
        if not items:
            return None
        records = []
        for i in items:
            code = str(i.get("f12", ""))
            if not code:
                continue
            records.append({
                "代码": code.zfill(6),
                "名称": str(i.get("f14", "")),
                "最新价": i.get("f2"),
                "涨跌幅": i.get("f3"),
                "涨跌额": i.get("f4"),
                "成交量": i.get("f5"),
                "成交额": i.get("f6"),
                "最高": i.get("f15"),
                "最低": i.get("f16"),
                "今开": i.get("f17"),
                "昨收": i.get("f18"),
            })
        return pd.DataFrame(records) if records else None
    except Exception as e:
        safe_stderr_print(f"[helpers] 东财 push2 指数行情失败: {e}")
        return None


def get_index_spot_indexed() -> tuple[pd.DataFrame, bool]:
    """获取指数实时行情索引
    
    降级链: 东财 push2 API → AkShare (如可用)
    返回以 '代码' 为索引的 DataFrame。
    """
    now = time.time()
    with _index_lock:
        indexed = _index_cache.get("indexed")
        ts = float(_index_cache.get("ts") or 0.0)
        if indexed is not None and (now - ts) < _INDEX_SPOT_TTL_SECONDS:
            return indexed, True

        df = None

        # 1. 主路径: 东财 push2 API
        try:
            df = _fetch_index_spot_eastmoney()
        except Exception:
            df = None

        # 2. 降级: AkShare (如可用)
        if (df is None or df.empty) and ak is not None:
            try:
                df = run_with_timeout(ak.stock_zh_index_spot_em, _INDEX_TIMEOUT_SECONDS)
            except Exception:
                df = None

        if (df is None or df.empty) and ak is not None:
            try:
                df = run_with_timeout(ak.stock_zh_index_spot_sina, min(_INDEX_TIMEOUT_SECONDS, 10.0))
            except Exception as e:
                stale_indexed = _index_cache.get("indexed")
                stale_ts = float(_index_cache.get("ts") or 0.0)
                if stale_indexed is not None and (now - stale_ts) < _INDEX_STALE_SECONDS:
                    return stale_indexed, True
                raise e
        
        if df is None or df.empty:
            stale_indexed = _index_cache.get("indexed")
            stale_ts = float(_index_cache.get("ts") or 0.0)
            if stale_indexed is not None and (now - stale_ts) < _INDEX_STALE_SECONDS:
                return stale_indexed, True
            raise RuntimeError("未获取到指数行情")

        if "代码" not in df.columns:
            raise RuntimeError("指数行情缺少代码列")

        df["代码"] = df["代码"].astype(str).str.zfill(6)
        indexed = df.set_index("代码", drop=False)

        _index_cache["indexed"] = indexed
        _index_cache["ts"] = now
        return indexed, False


# ============================================================
# 股票列表 — Tushare stock_basic (主) → AkShare (降级)
# ============================================================

def _fetch_tushare_basic_list(
    api_name: str,
    *,
    params: Optional[dict[str, Any]] = None,
    fields: str = "ts_code,name",
) -> Optional[list[dict]]:
    """通过 Tushare HTTP 代理获取证券基础列表。"""
    try:
        from ...data_source import data_source
        http_url = data_source.get_tushare_http_url()
        token = getattr(data_source, "tushare_token", "")
        if not http_url or not token:
            return None
        payload = {
            "api_name": api_name,
            "token": token,
            "params": params or {},
            "fields": fields,
        }
        resp = requests.post(http_url, json=payload, timeout=15)
        result = resp.json()
        if result.get("code") != 0:
            return None
        data = result.get("data", {})
        items = data.get("items", [])
        fields = data.get("fields", [])
        if not items or not fields:
            return None
        df = pd.DataFrame(items, columns=fields)
        if df.empty or "ts_code" not in df.columns:
            return None
        records = []
        for _, row in df.iterrows():
            ts_code = str(row.get("ts_code", ""))
            code = ts_code.split(".")[0] if "." in ts_code else ts_code
            name = str(row.get("name", ""))
            if code:
                records.append({"code": code, "name": name})
        return records if records else None
    except Exception as e:
        safe_stderr_print(f"[helpers] Tushare {api_name} 失败: {e}")
        return None


def _fetch_stock_list_tushare() -> Optional[list[dict]]:
    """Tushare HTTP 代理获取全A股列表（一次返回全部代码+名称，无 N+1 问题）"""
    return _fetch_tushare_basic_list(
        "stock_basic",
        params={"list_status": "L"},
        fields="ts_code,name",
    )


def _fetch_fund_list_tushare() -> Optional[list[dict]]:
    """Tushare HTTP 代理获取场内基金/ETF 基础列表。"""
    records = _fetch_tushare_basic_list(
        "fund_basic",
        params={"market": "E"},
        fields="ts_code,name",
    )
    if records:
        return records
    return _fetch_tushare_basic_list(
        "fund_basic",
        params={},
        fields="ts_code,name",
    )


def get_stock_list_cached() -> tuple[list[dict], bool]:
    """获取股票列表（带缓存）
    
    降级链: Tushare stock_basic → AkShare (如可用)
    返回 list[dict]，每个 dict 至少包含 'code' 键，可能包含 'name' 键。
    """
    now = time.time()
    with _list_lock:
        data = _list_cache.get("data")
        ts = float(_list_cache.get("ts") or 0.0)
        if data is not None and (now - ts) < _STOCK_LIST_TTL_SECONDS:
            return data, True

        records = None

        # 1. 主路径: Tushare stock_basic + fund_basic（股票 + 场内基金/ETF）
        stock_records = _fetch_stock_list_tushare() or []
        fund_records = _fetch_fund_list_tushare() or []
        merged: dict[str, dict[str, str]] = {}
        for row in stock_records + fund_records:
            code = normalize_code(row.get("code") or row.get("代码") or "")
            name = str(row.get("name") or row.get("名称") or "").strip()
            if code and name:
                merged[code] = {"code": code, "name": name}
        if merged:
            records = list(merged.values())

        # 2. 降级: AkShare (如可用)
        if not records and ak is not None:
            try:
                df = run_with_timeout(ak.stock_info_a_code_name, min(_SPOT_TIMEOUT_SECONDS, 10.0))
                if df is not None and not df.empty:
                    records = df.to_dict(orient="records")
            except Exception:
                pass
        
        if not records:
            if data is not None and (now - ts) < _STOCK_LIST_STALE_SECONDS:
                return data, True
            raise RuntimeError("未获取到A股股票列表")

        _list_cache["data"] = records
        _list_cache["ts"] = now
        return records, False


def get_name_map() -> dict[str, str]:
    """获取股票代码到名称的映射"""
    try:
        data, _ = get_stock_list_cached()
    except Exception:
        return {}
    
    name_map: dict[str, str] = {}
    for row in data or []:
        code = normalize_code(row.get("code") or row.get("代码") or row.get("股票代码") or "")
        name = row.get("name") or row.get("名称") or row.get("股票简称")
        if code and name:
            name_map[code] = str(name).strip()
    return name_map


# ============================================================
# 通用工具函数
# ============================================================

def calc_change(price: Optional[float], prev_close: Optional[float]) -> tuple[Optional[float], Optional[float]]:
    """计算涨跌额和涨跌幅"""
    if price is None or prev_close is None or prev_close == 0:
        return None, None
    change = price - prev_close
    return change, (change / prev_close) * 100


def _infer_response_source(data) -> str:
    if isinstance(data, dict):
        source = str(data.get("source") or "").strip()
        if source:
            return source
    elif isinstance(data, list):
        sources = [
            str(item.get("source") or "").strip()
            for item in data
            if isinstance(item, dict) and str(item.get("source") or "").strip()
        ]
        unique_sources = list(dict.fromkeys(sources))
        if len(unique_sources) == 1:
            return unique_sources[0]
        if unique_sources:
            return "multiple"
    return "akshare"


def ok(data, cached=False):
    """返回成功结果"""
    result = _base_ok(data, cached=cached)
    source = _infer_response_source(data)
    return enrich_response_meta(result, source=source, source_chain=[source])


def fail(error):
    """返回失败结果"""
    result = _base_fail(error)
    return enrich_response_meta(result, source="none", source_chain=["market.helpers"], degraded=True)
