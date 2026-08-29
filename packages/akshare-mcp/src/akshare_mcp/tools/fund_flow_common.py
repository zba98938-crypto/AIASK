"""
fund_flow_common.py
Shared constants, config, and the _ProxyBypass context-manager
used by all fund-flow sub-modules.
"""

import json
import logging
import os
import time
import asyncio
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from typing import Any, Optional

import requests

from ..cache import cache
from ..utils import parse_numeric

logger = logging.getLogger(__name__)

# =====================
# Environment-driven config
# =====================

_NORTH_FUND_STALE_DAYS = int(os.getenv("NORTH_FUND_STALE_DAYS", "5"))
_NORTH_FUND_DAILY_QUOTA = float(os.getenv("NORTH_FUND_DAILY_QUOTA", "52000000000"))
_NORTH_FUND_FAST_MODE = os.getenv("NORTH_FUND_FAST_MODE", "0").lower() in {
    "1", "true", "yes", "y",
}
_HKEX_DAILY_STAT_URL = os.getenv(
    "HKEX_DAILY_STAT_URL",
    "https://www.hkex.com.hk/eng/csm/DailyStat/data_tab_daily_{date}e.js",
)
_RETRY_SLEEP_SECONDS = float(os.getenv("AKSHARE_RETRY_SLEEP_SECONDS", "1.0"))
_SECTOR_FLOW_TIMEOUT_SECONDS = float(os.getenv("AKSHARE_SECTOR_FLOW_TIMEOUT_SECONDS", "20"))
_SECTOR_FLOW_INDICATORS = [
    s.strip()
    for s in os.getenv("AKSHARE_SECTOR_FLOW_INDICATORS", "今日,3日,5日").split(",")
    if s.strip()
]
_SECTOR_FLOW_CACHE_MAX_AGE = int(os.getenv("AKSHARE_SECTOR_FLOW_CACHE_MAX_AGE", "3600"))
_SECTOR_FLOW_DISABLE_PROXY_ON_FAIL = os.getenv(
    "AKSHARE_SECTOR_FLOW_DISABLE_PROXY_ON_FAIL", "1"
).lower() in {"1", "true", "yes", "y"}
_SECTOR_FLOW_CACHE_PATH = os.getenv(
    "AKSHARE_SECTOR_FLOW_CACHE_PATH",
    os.path.join(os.getcwd(), ".mcp_cache", "sector_fund_flow.json"),
)
_SECTOR_FLOW_CACHE_KEY = "fund_flow:sector:v2"

# In-memory sector-flow cache (shared mutable state)
_sector_flow_cache: dict[str, Any] = {"data": None, "ts": 0.0}


# =====================
# _ProxyBypass context-manager
# =====================

class _ProxyBypass:
    """Temporarily remove HTTP(S) proxy env-vars so requests go direct."""

    def __enter__(self):
        self._backup = {
            "HTTP_PROXY": os.getenv("HTTP_PROXY"),
            "HTTPS_PROXY": os.getenv("HTTPS_PROXY"),
            "http_proxy": os.getenv("http_proxy"),
            "https_proxy": os.getenv("https_proxy"),
            "NO_PROXY": os.getenv("NO_PROXY"),
            "no_proxy": os.getenv("no_proxy"),
        }
        os.environ["NO_PROXY"] = "*"
        os.environ["no_proxy"] = "*"
        os.environ.pop("HTTP_PROXY", None)
        os.environ.pop("HTTPS_PROXY", None)
        os.environ.pop("http_proxy", None)
        os.environ.pop("https_proxy", None)
        return self

    def __exit__(self, exc_type, exc, tb):
        for key, value in self._backup.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        return False


# =====================
# Shared helpers
# =====================

def _run_with_timeout(fn, timeout: float):
    """Run *fn* in a thread-pool and raise TimeoutError if it exceeds *timeout*."""
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(fn)
        try:
            return future.result(timeout=timeout)
        except FuturesTimeoutError:
            future.cancel()
            raise TimeoutError(f"AKShare sector fund flow timeout >{timeout}s")


def _run_storage_call_sync(coro_factory, timeout: float = 20.0):
    """在同步工具函数中安全执行异步数据库调用。"""
    from ..storage import run_with_db_cleanup

    def _runner():
        return run_with_db_cleanup(coro_factory())

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop and loop.is_running():
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(_runner)
            try:
                return future.result(timeout=timeout)
            except FuturesTimeoutError:
                future.cancel()
                raise TimeoutError(f"storage query timeout >{timeout}s")
    return _runner()


def _get_env_proxy() -> dict[str, str]:
    proxy = {}
    http_proxy = os.getenv("HTTP_PROXY") or os.getenv("http_proxy")
    https_proxy = os.getenv("HTTPS_PROXY") or os.getenv("https_proxy")
    if http_proxy:
        proxy["http"] = http_proxy
    if https_proxy:
        proxy["https"] = https_proxy
    return proxy


def _fetch_eastmoney_datacenter(params: dict[str, Any]) -> list[dict]:
    """Generic Eastmoney datacenter-web API wrapper with proxy fallback."""
    url = "https://datacenter-web.eastmoney.com/api/data/v1/get"
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Referer": "https://data.eastmoney.com/",
    }
    try:
        proxies = _get_env_proxy() or None
        resp = requests.get(url, params=params, headers=headers, timeout=15, proxies=proxies)
        payload = resp.json() if resp.status_code == 200 else {}
        return payload.get("result", {}).get("data", []) or []
    except requests.exceptions.ProxyError:
        try:
            resp = requests.get(url, params=params, headers=headers, timeout=15)
            payload = resp.json() if resp.status_code == 200 else {}
            return payload.get("result", {}).get("data", []) or []
        except Exception:
            return []
    except Exception:
        return []


# =====================
# Sector-flow cache helpers
# =====================

def _load_sector_flow_legacy_cache_file() -> Optional[list[dict]]:
    try:
        if not _SECTOR_FLOW_CACHE_PATH:
            return None
        if not os.path.exists(_SECTOR_FLOW_CACHE_PATH):
            return None
        with open(_SECTOR_FLOW_CACHE_PATH, "r", encoding="utf-8") as f:
            payload = json.load(f)
        if not isinstance(payload, dict):
            return None
        ts = float(payload.get("ts", 0.0))
        data = payload.get("data")
        if not data or not isinstance(data, list):
            return None
        if _SECTOR_FLOW_CACHE_MAX_AGE > 0 and (time.time() - ts) > _SECTOR_FLOW_CACHE_MAX_AGE:
            return None
        return data
    except Exception:
        return None


def _load_sector_flow_cache() -> Optional[list[dict]]:
    try:
        cached_data = cache.get(_SECTOR_FLOW_CACHE_KEY, _SECTOR_FLOW_CACHE_MAX_AGE)
        if isinstance(cached_data, list) and cached_data:
            return cached_data
    except Exception:
        pass

    legacy_cached = _load_sector_flow_legacy_cache_file()
    if legacy_cached:
        try:
            cache.set(_SECTOR_FLOW_CACHE_KEY, legacy_cached)
        except Exception:
            pass
        return legacy_cached
    return None


def _save_sector_flow_cache(data: list[dict]) -> None:
    try:
        if not isinstance(data, list) or not data:
            return
        cache.set(_SECTOR_FLOW_CACHE_KEY, data)
    except Exception:
        return
