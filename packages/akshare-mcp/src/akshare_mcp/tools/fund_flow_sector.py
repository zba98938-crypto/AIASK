"""
fund_flow_sector.py
Sector (industry) and concept board fund-flow functions.
Uses Eastmoney direct API and Tushare Pro (no AKShare).
"""

import time
from typing import Any, Optional

import requests

from ..data_source import data_source
from ..provider_contracts import attach_tool_provider_contract_meta
from ..storage import get_db
from ..utils import fail, ok, ok_degraded_empty, parse_numeric, pick_value, safe_float
from ..core.cache_manager import cached
from ..core.rate_limiter import get_limiter

from .fund_flow_common import (
    _ProxyBypass,
    _RETRY_SLEEP_SECONDS,
    _SECTOR_FLOW_TIMEOUT_SECONDS,
    _SECTOR_FLOW_INDICATORS,
    _SECTOR_FLOW_CACHE_MAX_AGE,
    _SECTOR_FLOW_DISABLE_PROXY_ON_FAIL,
    _sector_flow_cache,
    _run_with_timeout,
    _run_storage_call_sync,
    _get_env_proxy,
    _load_sector_flow_cache,
    _save_sector_flow_cache,
)


def _with_provider_contract(result: dict, tool_name: str, **kwargs: Any) -> dict:
    return attach_tool_provider_contract_meta(result, tool_name=tool_name, **kwargs)


# =====================
# Eastmoney direct fallback
# =====================

def _fetch_sector_flow_eastmoney(top_n: int) -> list[dict]:
    # Prefer direct connection; system HTTP(S)_PROXY is often broken for Eastmoney.
    try:
        url = "https://push2.eastmoney.com/api/qt/clist/get"
        params = {
            "pn": 1,
            "pz": top_n,
            "po": 1,
            "np": 1,
            "fltt": 2,
            "invt": 2,
            "fs": "m:90+t:2",
            "fields": "f12,f14,f3,f62,f66,f184",
        }
        session = requests.Session()
        session.trust_env = False
        response = session.get(url, params=params, timeout=10)
        if response.status_code != 200:
            return []
        payload = response.json()
        items = payload.get("data", {}).get("diff", []) if isinstance(payload, dict) else []
        results: list[dict] = []
        for item in items:
            change_raw = parse_numeric(item.get("f3"))
            main_in = parse_numeric(item.get("f62"))
            main_out = parse_numeric(item.get("f66"))
            net_inflow = None
            if main_in is not None and main_out is not None:
                net_inflow = main_in - main_out
            results.append(
                {
                    "name": str(item.get("f14") or ""),
                    "changePercent": change_raw / 100 if change_raw is not None else None,
                    "mainNetInflow": net_inflow if net_inflow is not None else main_in,
                    "mainNetInflowPercent": parse_numeric(item.get("f184")),
                    "superLargeNetInflow": None,
                    "largeNetInflow": None,
                    "mediumNetInflow": None,
                    "smallNetInflow": None,
                }
            )
        return results
    except Exception:
        return []


def _fetch_sector_flow_from_db(top_n: int) -> list[dict]:
    async def _load():
        db = get_db()
        async with db.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT block_name, avg_change_pct, stock_count, total_amount
                FROM market_blocks
                WHERE block_type = 'industry' AND block_name IS NOT NULL AND block_name != ''
                ORDER BY updated_at DESC NULLS LAST, avg_change_pct DESC NULLS LAST
                LIMIT $1
                """,
                max(1, int(top_n)),
            )
        return [dict(row) for row in rows]

    try:
        rows = _run_storage_call_sync(_load, timeout=3.0)
    except Exception:
        return []

    results: list[dict] = []
    for row in list(rows or []):
        name = str(row.get("block_name") or "").strip()
        if not name:
            continue
        change_pct = safe_float(row.get("avg_change_pct"))
        # DB market_blocks 不提供主力净流入，退化为用涨跌幅作为热度代理，
        # 以便上层排序/冷热板块分析保持稳定响应。
        heat_proxy = change_pct if change_pct is not None else 0.0
        results.append(
            {
                "name": name,
                "changePercent": change_pct,
                "mainNetInflow": heat_proxy,
                "mainNetInflowPercent": None,
                "superLargeNetInflow": None,
                "largeNetInflow": None,
                "mediumNetInflow": None,
                "smallNetInflow": None,
                "stockCount": int(row.get("stock_count") or 0),
                "totalAmount": safe_float(row.get("total_amount")),
                "source": "db.market_blocks",
                "degraded": True,
            }
        )
    return results


# =====================
# Public: get_sector_fund_flow
# =====================

@cached(ttl=300.0)
def get_sector_fund_flow(top_n: int = 20) -> dict:
    """
    获取行业板块资金流向

    数据源优先级: DB → Eastmoney direct API → Tushare Pro moneyflow_ind

    Args:
        top_n: 返回前N个板块，默认20
    """
    limiter = get_limiter("fund_flow", max_calls=3, period=1.0)
    limiter.acquire()

    try:
        top_n = int(top_n)

        def _respond(result: dict, *, provider_used: str, source_chain: list[str] | None = None, fallback_reason: str | None = None) -> dict:
            return _with_provider_contract(
                result,
                "get_sector_fund_flow",
                standard_model="SectorFundFlow",
                provider_used=provider_used,
                source_chain=source_chain or [provider_used],
                fallback_reason=fallback_reason,
            )

        # 1. 内存缓存
        cached_data = _sector_flow_cache.get("data")
        cached_ts = _sector_flow_cache.get("ts", 0.0)
        if cached_data and (time.time() - float(cached_ts)) <= _SECTOR_FLOW_CACHE_MAX_AGE:
            return _respond(ok(list(cached_data)[:top_n], cached=True), provider_used="memory_cache")

        # 2. 文件缓存
        file_cached = _load_sector_flow_cache()
        if file_cached:
            _sector_flow_cache["data"] = file_cached
            _sector_flow_cache["ts"] = time.time()
            return _respond(ok(list(file_cached)[:top_n], cached=True), provider_used="cache.sector_fund_flow")

        # 3. DB fallback
        db_fallback = _fetch_sector_flow_from_db(top_n)
        if db_fallback:
            _sector_flow_cache["data"] = db_fallback
            _sector_flow_cache["ts"] = time.time()
            _save_sector_flow_cache(db_fallback)
            return _respond(ok(db_fallback, cached=True), provider_used="db.market_blocks")

        # 4. Eastmoney direct API
        direct_fallback = _fetch_sector_flow_eastmoney(top_n)
        if direct_fallback:
            _sector_flow_cache["data"] = direct_fallback
            _sector_flow_cache["ts"] = time.time()
            _save_sector_flow_cache(direct_fallback)
            return _respond(ok(direct_fallback), provider_used="eastmoney.push2")

        # 5. Tushare Pro moneyflow_ind
        tushare_data = _fetch_sector_flow_tushare(top_n)
        if tushare_data:
            _sector_flow_cache["data"] = tushare_data
            _sector_flow_cache["ts"] = time.time()
            _save_sector_flow_cache(tushare_data)
            return _respond(ok(tushare_data), provider_used="tushare_pro.moneyflow_ind")

        return _respond(
            fail("未获取到行业板块资金流向数据: 所有数据源均失效"),
            provider_used="none",
            source_chain=["memory_cache", "cache.sector_fund_flow", "db.market_blocks", "eastmoney.push2", "tushare_pro.moneyflow_ind"],
            fallback_reason="all sources failed",
        )
    except Exception as e:
        cached_data = _sector_flow_cache.get("data")
        cached_ts = _sector_flow_cache.get("ts", 0.0)
        if cached_data and (time.time() - float(cached_ts)) <= _SECTOR_FLOW_CACHE_MAX_AGE:
            return _with_provider_contract(
                ok(cached_data, cached=True),
                "get_sector_fund_flow",
                standard_model="SectorFundFlow",
                provider_used="memory_cache",
                fallback_reason=str(e),
            )
        file_cached = _load_sector_flow_cache()
        if file_cached:
            return _with_provider_contract(
                ok(file_cached, cached=True),
                "get_sector_fund_flow",
                standard_model="SectorFundFlow",
                provider_used="cache.sector_fund_flow",
                fallback_reason=str(e),
            )
        return _with_provider_contract(
            fail(f"系统错误: {e}"),
            "get_sector_fund_flow",
            standard_model="SectorFundFlow",
            provider_used="none",
            fallback_reason=str(e),
        )


# =====================
# Tushare Pro sector flow
# =====================

def _fetch_sector_flow_tushare(top_n: int) -> list[dict]:
    """从 Tushare Pro moneyflow_ind 获取行业资金流向。"""
    try:
        ts_pro = data_source.get_tushare_pro()
        if not ts_pro:
            return []

        import datetime
        today = datetime.datetime.now().strftime('%Y%m%d')
        # 尝试最近 5 个交易日
        for days_back in range(5):
            check_date = (datetime.datetime.now() - datetime.timedelta(days=days_back)).strftime('%Y%m%d')
            df = ts_pro.moneyflow_ind(trade_date=check_date)
            if df is not None and not df.empty:
                break
        else:
            return []

        if df is None or df.empty:
            return []

        results: list[dict] = []
        for _, row in df.head(top_n).iterrows():
            name = str(row.get("industry") or row.get("ts_code") or "").strip()
            net_amount = safe_float(row.get("net_amount"))
            results.append(
                {
                    "name": name,
                    "changePercent": None,
                    "mainNetInflow": net_amount,
                    "mainNetInflowPercent": None,
                    "superLargeNetInflow": None,
                    "largeNetInflow": None,
                    "mediumNetInflow": None,
                    "smallNetInflow": None,
                    "source": "tushare_pro",
                }
            )
        return results
    except Exception:
        return []


# =====================
# Public: get_concept_fund_flow
# =====================

@cached(ttl=300.0)
def get_concept_fund_flow(top_n: int = 20) -> dict:
    """
    获取概念板块资金流向（Eastmoney direct API）

    Args:
        top_n: 返回前N个板块，默认20
    """
    limiter = get_limiter("fund_flow", max_calls=3, period=1.0)
    limiter.acquire()

    try:
        top_n = int(top_n)

        def _respond(result: dict, *, provider_used: str = "eastmoney.push2.concept", fallback_reason: str | None = None) -> dict:
            return _with_provider_contract(
                result,
                "get_concept_fund_flow",
                standard_model="ConceptFundFlow",
                provider_used=provider_used,
                source_chain=[provider_used],
                fallback_reason=fallback_reason,
            )

        # 使用东方财富概念板块 API
        url = "https://push2.eastmoney.com/api/qt/clist/get"
        params = {
            "pn": 1,
            "pz": top_n,
            "po": 1,
            "np": 1,
            "fltt": 2,
            "invt": 2,
            "fs": "m:90+t:3",  # t:3 = 概念板块
            "fields": "f12,f14,f3,f62,f66,f184",
        }
        proxies = _get_env_proxy() or None
        session = requests.Session()
        session.trust_env = False
        response = session.get(url, params=params, timeout=10)
        if response.status_code != 200:
            return _respond(
                ok_degraded_empty(
                    [],
                    fallback_reason="eastmoney http status",
                    source_chain=["eastmoney.push2.concept"],
                    quality_flags=["upstream_unavailable"],
                ),
                provider_used="none",
                fallback_reason="eastmoney http status",
            )

        payload = response.json()
        items = payload.get("data", {}).get("diff", []) if isinstance(payload, dict) else []
        if not items:
            return _respond(
                ok_degraded_empty(
                    [],
                    fallback_reason="empty items",
                    source_chain=["eastmoney.push2.concept"],
                    quality_flags=["empty_upstream"],
                ),
                fallback_reason="empty items",
            )

        results: list[dict] = []
        for item in items:
            change_raw = parse_numeric(item.get("f3"))
            main_in = parse_numeric(item.get("f62"))
            main_out = parse_numeric(item.get("f66"))
            net_inflow = None
            if main_in is not None and main_out is not None:
                net_inflow = main_in - main_out
            results.append(
                {
                    "name": str(item.get("f14") or ""),
                    "changePercent": change_raw / 100 if change_raw is not None else None,
                    "mainNetInflow": net_inflow if net_inflow is not None else main_in,
                    "mainNetInflowPercent": parse_numeric(item.get("f184")),
                    "inflow": main_in,
                    "outflow": main_out,
                }
            )
        return _respond(ok(results))
    except Exception as e:
        return _with_provider_contract(
            ok_degraded_empty(
                [],
                fallback_reason=str(e),
                source_chain=["eastmoney.push2.concept"],
                quality_flags=["upstream_exception"],
            ),
            "get_concept_fund_flow",
            standard_model="ConceptFundFlow",
            provider_used="none",
            fallback_reason=str(e),
        )
