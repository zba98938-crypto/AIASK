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
from ...provider_contracts import attach_provider_contract_meta
from ...services.market_data_access import (
    FALLBACK_DB_FIRST_LIVE,
    attach_quote_access_meta,
    get_quote_snapshot_sync,
)
from ...storage import get_db, run_with_db_cleanup
from ...utils import (
    attach_argument_contract_meta,
    enrich_response_meta,
    resolve_canonical_arg,
    resolve_existing_security_code_sync,
    safe_stderr_print,
)
from ..data_quality import build_quality_meta, infer_missing_fields, normalize_reason_list
try:
    import akshare as ak
except ImportError:
    ak = None
import pandas as pd

from ._quote_support import *


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
    persist: bool = True,
    access_meta: Optional[dict] = None,
) -> dict:
    response = ok(payload, cached=False)
    normalized_reasons = normalize_reason_list(fallback_reason)
    if isinstance(response.get("data"), dict):
        data = response["data"]
        data["attempted_sources"] = attempted_sources
        data["source_chain"] = source_chain
        data["fallback_used"] = (
            bool((access_meta or {}).get("fallback_used"))
            if isinstance(access_meta, dict) and "fallback_used" in access_meta
            else len(source_chain) > 1 or (source_chain and source_chain[0] != "data_source")
        )
        data["fallback_reason"] = fallback_reason
        data["data_timestamp"] = payload.get("data_timestamp") or _current_data_timestamp()
        if isinstance(access_meta, dict):
            for key in ("backend_requested", "backend_used", "db_snapshot_time", "data_freshness_seconds", "stale"):
                if key in access_meta:
                    data[key] = access_meta.get(key)
        if persist:
            _save_quote_nonblocking(data)
    response.update(
        build_quality_meta(
            source=str((access_meta or {}).get("backend_used") or payload.get("source") or "unknown"),
            source_chain=source_chain,
            fallback_reason=normalized_reasons,
            asof_value=payload.get("time") or payload.get("trade_time") or payload.get("data_timestamp"),
            missing_fields=_quote_missing_fields(payload),
            degraded=bool(_quote_missing_fields(payload)) or bool((access_meta or {}).get("stale")),
            success=True,
        )
    )
    if isinstance(access_meta, dict):
        response = attach_quote_access_meta(response, access_meta)
    response = attach_provider_contract_meta(
        response,
        tool_name="get_realtime_quote",
        standard_model="EquityQuote",
        provider_requested=source_chain[0] if source_chain else "data_source",
        provider_used=str((access_meta or {}).get("backend_used") or payload.get("source") or response.get("source") or "unknown"),
        source_chain=source_chain,
        fallback_reason=normalized_reasons,
        data_timestamp=payload.get("data_timestamp") or payload.get("time") or payload.get("trade_time"),
        freshness={
            "expectation": "intraday_or_latest_quote_snapshot",
            "data_timestamp_field": "data_timestamp",
        },
    )
    # P2-4.5.1 fix(诊断报告 §4.5.1):清洗 name 字段乱码
    try:
        from ...services.name_sanitize import wrap_response_with_clean_names
        response = wrap_response_with_clean_names(response, fallback="")
    except Exception:
        pass
    return response


def _eastmoney_index_secid(code: str) -> str:
    """Return Eastmoney secid for common mainland index codes."""
    normalized = normalize_code(code)
    market = "0" if normalized.startswith(("39", "98")) else "1"
    return f"{market}.{normalized}"


def _eastmoney_scaled_number(value, *, decimals: int = 2, force_scaled: bool = False) -> Optional[float]:
    parsed = safe_float(value)
    if parsed is None:
        return None
    if isinstance(value, str) and "." in value:
        return parsed
    decimals = max(0, min(int(decimals or 0), 6))
    divisor = 10 ** decimals
    if divisor > 1 and (force_scaled or abs(parsed) >= 10000):
        return round(parsed / divisor, decimals)
    return parsed


def _fetch_single_index_quote_eastmoney(code: str) -> Optional[dict]:
    """Fetch one index quote through Eastmoney's lightweight single-security API."""
    url = "https://push2.eastmoney.com/api/qt/stock/get"
    params = {
        "secid": _eastmoney_index_secid(code),
        "ut": "fa5fd1943c7b386f172d6893dbfba10b",
        "fltt": 2,
        "invt": 2,
        "fields": "f43,f44,f45,f46,f47,f48,f57,f58,f59,f60,f169,f170",
    }

    payload = None
    try:
        resp = requests.get(url, params=params, timeout=6)
        if resp.status_code == 200:
            payload = resp.json()
    except (requests.exceptions.ProxyError, requests.exceptions.ConnectionError):
        try:
            session = requests.Session()
            session.trust_env = False
            resp = session.get(url, params=params, timeout=6)
            if resp.status_code == 200:
                payload = resp.json()
        except Exception:
            payload = None
    except Exception:
        payload = None

    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        return None

    decimals = safe_int(data.get("f59")) or 2
    price = _eastmoney_scaled_number(data.get("f43"), decimals=decimals)
    if price is None:
        return None

    change = _eastmoney_scaled_number(data.get("f169"), decimals=decimals, force_scaled=True)
    change_pct = _eastmoney_scaled_number(data.get("f170"), decimals=2, force_scaled=True)
    return {
        "code": normalize_code(str(data.get("f57") or code)),
        "name": _safe_index_name(data.get("f58"), normalize_code(str(data.get("f57") or code))),
        "price": price,
        "change": change,
        "changePercent": change_pct,
        "open": _eastmoney_scaled_number(data.get("f46"), decimals=decimals),
        "high": _eastmoney_scaled_number(data.get("f44"), decimals=decimals),
        "low": _eastmoney_scaled_number(data.get("f45"), decimals=decimals),
        "preClose": _eastmoney_scaled_number(data.get("f60"), decimals=decimals),
        "volume": safe_int(data.get("f47")),
        "amount": safe_float(data.get("f48")),
        "source": "eastmoney_index_single",
    }


_COMMON_INDEX_NAMES = {
    "000001": "上证指数",
    "399001": "深证成指",
    "399006": "创业板指",
    "000300": "沪深300",
    "000905": "中证500",
    "000852": "中证1000",
    "000016": "上证50",
    "000688": "科创50",
    "399005": "中小100",
    "399330": "深证100",
}


def _looks_like_gbk_garbled(value: object) -> bool:
    """Detect strings that look like GBK→UTF-8 mojibake (e.g. '????').

    When upstream providers return chunked response without proper encoding header,
    pandas / requests fall back to latin1 and we may end up with '?' placeholders.
    Detect these so caller can substitute static index name dictionary.
    """
    if not isinstance(value, str) or not value:
        return False
    # Heuristic 1: 字符串中 ? 占比超过 50% 视为乱码
    qmark_ratio = value.count("?") / len(value)
    if qmark_ratio >= 0.5:
        return True
    # Heuristic 2: 全 \ufffd (replacement char)
    if all(ch == "\ufffd" for ch in value):
        return True
    return False


def _safe_index_name(raw: object, code: str) -> str:
    """Pick a safe index display name; fall back to static table if upstream returns mojibake."""
    candidate = str(raw or "").strip()
    if not candidate or _looks_like_gbk_garbled(candidate):
        return _COMMON_INDEX_NAMES.get(code, "")
    return candidate


def _degraded_empty_index_quote(
    code: str,
    message: str,
    *,
    attempted_sources: list[str],
    source_chain: list[str],
    fallback_reason: Optional[str | list[str]] = None,
    strict_failure: bool = False,
) -> dict:
    """Return a stable empty index quote when all upstream providers are unavailable.

    P3-5.8 fix: 当 strict_failure=True 时返回 success=false(诊断报告 §5.8)
    历史问题:全降级链空时仍 success=true price=None,AI 误以为指数无行情
    现在调用方可以传 strict_failure=True 显式标记上游不可用
    """
    reasons = normalize_reason_list(fallback_reason or message)
    payload = {
        "code": code,
        "name": _COMMON_INDEX_NAMES.get(code, ""),
        "price": None,
        "change": None,
        "changePercent": None,
        "open": None,
        "high": None,
        "low": None,
        "preClose": None,
        "volume": None,
        "amount": None,
        "source": "none",
        "attempted_sources": list(dict.fromkeys(attempted_sources or source_chain or [])),
        "source_chain": list(dict.fromkeys(source_chain or attempted_sources or [])),
        "fallback_used": True,
        "fallback_reason": reasons,
        "data_timestamp": _current_data_timestamp(),
        "degraded": True,
    }
    if strict_failure:
        # P3-5.8: 严格模式下返回 fail
        response = fail(message)
        response["data"] = payload
        response["source"] = "none"
        response["fallback_reason"] = reasons
        response.update(
            build_quality_meta(
                source="none",
                source_chain=payload["source_chain"],
                fallback_reason=reasons,
                asof_value=None,
                missing_fields=_quote_missing_fields(payload),
                degraded=True,
                success=False,
            )
        )
        return enrich_response_meta(
            response,
            source="none",
            source_chain=payload["source_chain"],
            quality_flags=["degraded", "empty_upstream", "fallback", "all_sources_failed"],
            degraded=True,
            fallback_used=True,
        )
    response = ok(payload, cached=False)
    response["fallback_reason"] = reasons
    response.update(
        build_quality_meta(
            source="none",
            source_chain=payload["source_chain"],
            fallback_reason=reasons,
            asof_value=None,
            missing_fields=_quote_missing_fields(payload),
            degraded=True,
            success=True,
        )
    )
    return enrich_response_meta(
        response,
        source="none",
        source_chain=payload["source_chain"],
        quality_flags=["degraded", "empty_upstream", "fallback"],
        degraded=True,
        fallback_used=True,
    )

def get_realtime_quote(
    code: str = "",
    *,
    stock_code: str = "",
    symbol: str = "",
    ticker: str = "",
) -> dict:
    """获取单只股票实时行情（优化版）

    数据源优先级: DataSource(Tushare/公开源) → AkShare(分钟K/日K/全市场) → Sina → Tencent
    时效性: 交易时段内接近实时（秒级），非交易时段返回最近收盘数据

    Args:
        stock_code (str, required): 股票代码，6位数字，如 "600519"、"000001"

    Returns:
        dict: {"success": bool, "data": {...}}
        data 字段:
        - code (str): 标准化股票代码
        - name (str): 股票名称
        - price (float|None): 最新价
        - change (float|None): 涨跌额
        - changePercent (float|None): 涨跌幅(%)
        - open (float|None): 今开
        - high (float|None): 最高
        - low (float|None): 最低
        - preClose (float|None): 昨收
        - volume (int|None): 成交量(股)
        - amount (float|None): 成交额(元)
        - source (str): 数据来源标识，如 "data_source"/"akshare_minute"/"sina"/"tencent"

    Errors:
        - 所有数据源均不可用时返回 success=false

    Examples:
        get_realtime_quote("600519")
        get_realtime_quote("000001")
    """
    limiter = get_limiter("quote", max_calls=10, period=1.0)
    limiter.acquire()

    def _as_plain_quote(v):
        # validate_quote 可能返回 pydantic 模型；统一转为 dict 以注入结构化 trace 字段
        if hasattr(v, "model_dump"):
            try:
                return v.model_dump()
            except Exception:
                return dict(v)
        return v

    try:
        raw_code, alias_hits, _ = resolve_canonical_arg(
            "code",
            code,
            stock_code=stock_code,
            symbol=symbol,
            ticker=ticker,
        )
        code, _, code_error = resolve_existing_security_code_sync(code=raw_code)
        canonical_args = {"code": code or raw_code}
        def _respond(payload: dict) -> dict:
            return attach_argument_contract_meta(
                payload,
                canonical_tool="get_realtime_quote",
                canonical_args=canonical_args,
                alias_hits=alias_hits,
            )
        if code_error:
            return _respond(
                _fail_quote_response(
                    code_error,
                    attempted_sources=[],
                    source_chain=["validate.stock_code"],
                    fallback_reason=code_error,
                )
            )
        attempted_sources: list[str] = []
        fallback_reason_parts: list[str] = []
        access_chain = ["db.stock_quotes"]

        # 1. DataSource 优先：Tushare / 公开源 → akshare
        attempted_sources.append("db.stock_quotes")
        try:
            access = get_quote_snapshot_sync(code, fallback_mode=FALLBACK_DB_FIRST_LIVE)
            access_chain = list(access.get("source_chain") or ["db.stock_quotes"])
            attempted_sources.extend(access_chain)
            if access.get("fallback_reason"):
                fallback_reason_parts.extend(str(item) for item in access.get("fallback_reason") or [])
            if access.get("error"):
                fallback_reason_parts.append(str(access.get("error")))
            res = access.get("data") if access.get("success") else None
            if res:
                validated = _as_plain_quote(validate_quote(res))
                if isinstance(validated, dict):
                    validated = _backfill_prev_close(validated, code)
                return _respond(
                    _ok_quote_response(
                        validated,
                        attempted_sources=list(dict.fromkeys(attempted_sources)),
                        source_chain=access_chain,
                        fallback_reason=access.get("fallback_reason"),
                        persist=False,
                        access_meta=access,
                    )
                )
        except Exception as e:
            fallback_reason_parts.append(f"data_source失败: {e}")
            safe_stderr_print(f"DataSource quote failed for {code}: {e}")

        # 2. Try AkShare
        attempted_sources.append("akshare")
        try:
            res = _get_realtime_quote_akshare(code)
        except (TimeoutError, RuntimeError, Exception) as e:
            fallback_reason_parts.append(f"akshare失败: {e}")
            safe_stderr_print(f"AkShare quote failed for {code}: {e}")
            res = None
        if res:
            validated = _as_plain_quote(validate_quote(res))
            if isinstance(validated, dict):
                validated = _backfill_prev_close(validated, code)
            return _respond(
                _ok_quote_response(
                    validated,
                    attempted_sources=list(dict.fromkeys(attempted_sources)),
                    source_chain=list(dict.fromkeys([*access_chain, "akshare"])),
                    fallback_reason="; ".join(fallback_reason_parts) if fallback_reason_parts else "DataSource不可用，已降级至AkShare",
                )
            )

        # 3. Try Sina
        attempted_sources.append("sina")
        safe_stderr_print(f"Trying Sina for {code}...")
        res = _get_quote_sina(code)
        if res:
            validated = _as_plain_quote(validate_quote(res))
            if isinstance(validated, dict):
                validated = _backfill_prev_close(validated, code)
            return _respond(
                _ok_quote_response(
                    validated,
                    attempted_sources=list(dict.fromkeys(attempted_sources)),
                    source_chain=list(dict.fromkeys([*access_chain, "akshare", "sina"])),
                    fallback_reason="; ".join(fallback_reason_parts) if fallback_reason_parts else "上游源不可用，已降级至Sina",
                )
            )

        # 4. Try Tencent
        attempted_sources.append("tencent")
        safe_stderr_print(f"Sina failed for {code}, trying Tencent...")
        res = _get_quote_tencent(code)
        if res:
            validated = _as_plain_quote(validate_quote(res))
            if isinstance(validated, dict):
                validated = _backfill_prev_close(validated, code)
            return _respond(
                _ok_quote_response(
                    validated,
                    attempted_sources=list(dict.fromkeys(attempted_sources)),
                    source_chain=list(dict.fromkeys([*access_chain, "akshare", "sina", "tencent"])),
                    fallback_reason="; ".join(fallback_reason_parts) if fallback_reason_parts else "上游源不可用，已降级至Tencent",
                )
            )

        attempted = " -> ".join(attempted_sources)
        reason = "; ".join(fallback_reason_parts) if fallback_reason_parts else "所有上游源均返回空数据"
        return _respond(
            _fail_quote_response(
                f"所有数据源均无法获取 {code} 的实时行情（attempted={attempted}, reason={reason}）",
                attempted_sources=attempted_sources,
                source_chain=list(dict.fromkeys([*access_chain, "akshare", "sina", "tencent"])),
                fallback_reason=reason,
            )
        )
    except Exception as e:
        return attach_argument_contract_meta(
            _fail_quote_response(
                str(e),
                attempted_sources=[],
                source_chain=["get_realtime_quote"],
                fallback_reason=str(e),
            ),
            canonical_tool="get_realtime_quote",
            canonical_args={"code": code or ""},
            alias_hits=[],
        )




def _build_batch_quote_response(
    *,
    codes: list[str],
    quotes: list[dict],
    missing: list[str],
    cached: bool,
) -> dict:
    result = ok(quotes, cached=cached)
    result["requested"] = codes
    result["found"] = len(quotes)
    result["missing"] = missing
    result["quotes"] = quotes

    unique_sources = list(dict.fromkeys(str(item.get("source") or "").strip() for item in quotes if str(item.get("source") or "").strip()))
    source_chain: list[str] = []
    for item in quotes:
        item_chain = item.get("source_chain") if isinstance(item, dict) else None
        if isinstance(item_chain, list):
            source_chain.extend(str(source).strip() for source in item_chain if str(source).strip())
    if unique_sources:
        source_chain.extend(unique_sources)
    source_chain = list(dict.fromkeys(source_chain or ["db.stock_quotes"]))
    fallback_reasons: list[str] = []
    if any(bool(item.get("fallback_used")) for item in quotes if isinstance(item, dict)):
        fallback_reasons.append("batch_quote_used_fallback_source")
    if missing:
        fallback_reasons.append(f"missing_codes:{','.join(missing)}")

    result.update(
        build_quality_meta(
            source=unique_sources[0] if len(unique_sources) == 1 else "multiple_adapters",
            source_chain=source_chain,
            fallback_reason=fallback_reasons or None,
            asof_value=datetime.now().astimezone().isoformat(timespec="seconds"),
            missing_fields=[],
            degraded=bool(fallback_reasons),
            success=True,
            accepted_count=len(quotes),
            rejected_count=len(missing),
        )
    )
    result["backend_requested"] = "db.stock_quotes"
    backend_used = list(dict.fromkeys(str(item.get("backend_used") or item.get("source") or "").strip() for item in quotes if str(item.get("backend_used") or item.get("source") or "").strip()))
    result["backend_used"] = backend_used[0] if len(backend_used) == 1 else ("multiple_adapters" if backend_used else "none")
    result["fallback_used"] = bool(fallback_reasons)
    result["fallback_reason"] = fallback_reasons or None
    return result



def get_batch_quotes(stock_codes: list[str]) -> dict:
    """批量获取股票实时行情

    数据源优先级（逐只）: DataSource(Tushare/公开源) → AkShare(分钟K/全市场) → Sina → Tencent → 日K
    时效性: 交易时段内接近实时；批量 ≤5 只走分钟K线，>5 只走全市场快照

    Args:
        stock_codes (list[str], required): 股票代码列表，如 ["600519", "000001"]

    Returns:
        dict: {"success": bool, "data": {...}}
        data 字段:
        - requested (list[str]): 请求的代码列表
        - found (int): 成功获取数量
        - missing (list[str]): 未获取到的代码
        - quotes (list[dict]): 行情列表，每项含 code/name/price/change/changePercent/volume/amount/source

    Errors:
        - stock_codes 为空时返回 success=false

    Examples:
        get_batch_quotes(["600519", "000001", "000858"])
    """
    from ..market.helpers import _MINUTE_BATCH_LIMIT, _BATCH_FALLBACK_LIMIT

    try:
        codes = [normalize_code(c) for c in stock_codes or []]
        if not codes:
            return fail("stock_codes 不能为空")

        name_map: Optional[dict] = None  # 延后加载，仅在使用 akshare 或补 name 时再调 _get_name_map
        quotes: list[dict] = []
        missing: list[str] = []

        spot_df: Optional[pd.DataFrame] = None
        spot_cached = False
        spot_unavailable = False
        fallback_enabled = len(codes) <= _BATCH_FALLBACK_LIMIT

        use_minute = len(codes) <= _MINUTE_BATCH_LIMIT
        for code in codes:
            # FIX-9: 存在性校验，非法码（如 999999）直接进 missing，
            # 避免回退到 db.stock_quotes 快照时坐标化到上证指数（F-N03-2）。
            # 与单查 get_realtime_quote 的 resolve_existing_security_code_sync 行为一致。
            try:
                _norm, _info, _code_err = resolve_existing_security_code_sync(code=code)
                if _code_err:
                    missing.append(code)
                    continue
            except Exception as _e:
                _log_quote_source_error("batch code existence check", code, _e)
            # 1. 优先 DataSource：Tushare / 公开源 → akshare
            try:
                access = get_quote_snapshot_sync(code, fallback_mode=FALLBACK_DB_FIRST_LIVE)
                fallback = dict(access.get("data") or {}) if access.get("success") else {}
                if fallback and fallback.get("price") is not None:
                    if not fallback.get("name"):
                        if name_map is None:
                            name_map = _get_name_map()
                        fallback["name"] = name_map.get(code, "")
                    for meta_key in (
                        "source_chain",
                        "backend_requested",
                        "backend_used",
                        "fallback_used",
                        "fallback_reason",
                        "db_snapshot_time",
                        "data_freshness_seconds",
                        "stale",
                    ):
                        fallback[meta_key] = access.get(meta_key)
                    quotes.append(fallback)
                    continue
                if access.get("error"):
                    raise RuntimeError(str(access.get("error")))
            except Exception as e:
                _log_quote_source_error("batch quote db_first", code, e)

            # 以下为 akshare/其他降级路径，需要 name 时再拉取 name_map
            if name_map is None:
                name_map = _get_name_map()
            name = name_map.get(code, "")

            if use_minute:
                try:
                    minute = _get_minute_quote(code)
                    snapshot = _get_daily_snapshot(code)
                    prev_close = snapshot.get("prev_close")
                    change, change_pct = _calc_change(minute.get("price"), prev_close)
                    quotes.append(
                        {
                            "code": code,
                            "name": name,
                            "price": minute.get("price"),
                            "change": change,
                            "changePercent": change_pct,
                            "volume": minute.get("volume"),
                            "amount": minute.get("amount"),
                            "preClose": prev_close,
                            "time": minute.get("time"),
                            "source": "akshare_minute",
                        }
                    )
                    continue
                except Exception as e:
                    _log_quote_source_error("batch minute quote", code, e)

            if spot_df is None:
                try:
                    spot_df, spot_cached = _get_spot_indexed()
                except Exception as e:
                    _log_quote_source_error("batch spot snapshot", code, e)
                    spot_df = None
                    spot_unavailable = True

            if spot_df is not None and code in spot_df.index:
                row = spot_df.loc[code]
                spot_name = pick_value(row, ["名称", "股票简称"]) or name
                price = safe_float(pick_value(row, ["最新价", "最新", "现价"]))
                if price is not None:
                    quotes.append(
                        {
                            "code": code,
                            "name": str(spot_name or ""),
                            "price": price,
                            "change": safe_float(pick_value(row, ["涨跌额", "涨跌"])),
                            "changePercent": safe_float(pick_value(row, ["涨跌幅", "涨幅"])),
                            "volume": safe_int(pick_value(row, ["成交量"])),
                            "amount": safe_float(pick_value(row, ["成交额"])),
                            "source": "akshare_spot",
                        }
                    )
                    continue

            if fallback_enabled:
                fallback = _get_quote_sina(code)
                if fallback:
                    if not fallback.get("name") and name:
                        fallback["name"] = name
                    quotes.append(fallback)
                    continue

                fallback = _get_quote_tencent(code)
                if fallback:
                    if not fallback.get("name") and name:
                        fallback["name"] = name
                    quotes.append(fallback)
                    continue

            daily = None
            if not spot_unavailable:
                try:
                    daily = _get_daily_quote(code, name)
                except Exception as e:
                    _log_quote_source_error("batch daily quote", code, e)
                    daily = None
            if daily is not None:
                quotes.append(daily)
                continue

            missing.append(code)

        return _build_batch_quote_response(
            codes=codes,
            quotes=quotes,
            missing=missing,
            cached=spot_cached or (bool(quotes) and all(item.get("backend_used") == "db.stock_quotes" for item in quotes)),
        )
    except Exception as e:
        return fail(e)

def get_batch_quotes_compat(codes: list[str]) -> dict:
    """批量获取股票实时行情（兼容Node.js版本）

    内部调用 get_batch_quotes，返回格式简化为 data=quotes 列表（无 requested/found/missing 包装）。

    Args:
        codes (list[str], required): 股票代码列表，如 ["600519", "000001"]

    Returns:
        dict: {"success": bool, "data": list[dict], "source": str, "cached": bool}
        data 为行情列表，每项含 code/name/price/change/changePercent/volume/amount/source

    Examples:
        get_batch_quotes_compat(["600519", "000001"])
    """
    result = get_batch_quotes(codes)

    if not result.get('success'):
        return result

    # P0-2 修复说明：
    # 旧实现假设 result['data'] 为 {'quotes': [...] }，但当前主接口 data 多数为 list，导致类型错误。
    # 新实现按多种历史结构兼容提取，保证兼容层返回稳定 list。
    data = result.get('data')
    if isinstance(data, list):
        quotes = data
    elif isinstance(data, dict) and isinstance(data.get('quotes'), list):
        quotes = data.get('quotes')
    elif isinstance(result.get('quotes'), list):
        quotes = result.get('quotes')
    else:
        quotes = []

    response = ok(quotes, cached=result.get('cached', False))
    response['source'] = result.get('source', 'multiple_adapters')
    for key in ("source_chain", "quality_flags", "fallback_used", "fallback_reason", "degraded"):
        if key in result:
            response[key] = result.get(key)
    return response

def _sanitize_index_response(response: dict) -> dict:
    """P2-4.5.1 fix:清洗 index quote 响应中的 name 字段乱码(诊断报告 §4.5.1)。"""
    try:
        from ...services.name_sanitize import wrap_response_with_clean_names
        return wrap_response_with_clean_names(response, fallback="")
    except Exception:
        return response


def get_index_quote(index_code: str) -> dict:
    """获取指数实时行情

    降级链: 东财 push2 (get_index_spot_indexed) → AkShare Sina 接口
    时效性: 交易时段内接近实时

    Args:
        index_code (str, required): 指数代码，如 "000001"(上证指数)、"399001"(深证成指)、"399006"(创业板指)

    Returns:
        dict: {"success": bool, "data": {...}}
        data 字段:
        - code (str): 标准化指数代码
        - name (str): 指数名称
        - price (float): 最新点位
        - change (float|None): 涨跌额
        - changePercent (float|None): 涨跌幅(%)
        - open (float|None): 今开
        - high (float|None): 最高
        - low (float|None): 最低
        - preClose (float|None): 昨收
        - volume (int|None): 成交量
        - amount (float|None): 成交额

    Errors:
        - 指数代码不存在时返回 success=false
        - 数据源返回异常时返回 success=true 但 price=None 并附 message

    Examples:
        get_index_quote("000001")
        get_index_quote("399006")
    """
    code = normalize_code(index_code)
    attempted_sources = ["eastmoney_index"]
    fallback_reason_parts: list[str] = []

    def _tushare_index_daily_response(reason: str) -> dict | None:
        extended_attempts = list(dict.fromkeys([*attempted_sources, "tushare_index_daily"]))
        try:
            ts_pro = data_source.get_tushare_pro()
            if ts_pro is None:
                raise RuntimeError("tushare_pro unavailable")
            ts_code = f"{code}.SZ" if code.startswith("39") else f"{code}.SH"
            from datetime import datetime as _dt, timedelta as _td

            end_d = _dt.now().strftime("%Y%m%d")
            start_d = (_dt.now() - _td(days=10)).strftime("%Y%m%d")
            df_ts = ts_pro.index_daily(ts_code=ts_code, start_date=start_d, end_date=end_d)
            if df_ts is None or df_ts.empty:
                raise RuntimeError("index_daily returned empty")

            row_ts = df_ts.iloc[0]
            ts_price = safe_float(row_ts.get("close"))
            if ts_price is None:
                raise RuntimeError("index_daily missing close")

            ts_pre = safe_float(row_ts.get("pre_close"))
            ts_change, ts_pct = _calc_change(ts_price, ts_pre)
            return _ok_quote_response(
                {
                    "code": code,
                    "name": _COMMON_INDEX_NAMES.get(code, ""),
                    "price": ts_price,
                    "change": ts_change,
                    "changePercent": ts_pct,
                    "open": safe_float(row_ts.get("open")),
                    "high": safe_float(row_ts.get("high")),
                    "low": safe_float(row_ts.get("low")),
                    "preClose": ts_pre,
                    "volume": safe_float(row_ts.get("vol")),
                    "amount": safe_float(row_ts.get("amount")),
                    "source": "tushare_index_daily_fallback",
                    "data_timestamp": str(row_ts.get("trade_date") or ""),
                },
                attempted_sources=extended_attempts,
                source_chain=["eastmoney_index", "sina_index", "tushare_index_daily"],
                fallback_reason=reason,
            )
        except Exception as exc:
            fallback_reason_parts.append(f"tushare_index_daily失败: {exc}")
            safe_stderr_print(f"[quote] tushare index daily fallback failed for {index_code}: {exc}")
            return None

    try:
        cached = False
        used_source = "eastmoney_index"
        try:
            single_payload = _fetch_single_index_quote_eastmoney(code)
            if single_payload:
                return _ok_quote_response(
                    single_payload,
                    attempted_sources=["eastmoney_index_single"],
                    source_chain=["eastmoney_index_single"],
                    fallback_reason=None,
                )
            fallback_reason_parts.append("eastmoney_index_single returned empty")
        except Exception as exc:
            fallback_reason_parts.append(f"eastmoney_index_single failed: {exc}")
            safe_stderr_print(f"[quote] eastmoney single index failed for {code}: {exc}")

        try:
            df, cached = _get_index_spot_indexed()
        except Exception as exc:
            df = pd.DataFrame()
            fallback_reason_parts.append(f"eastmoney_index失败: {exc}")
            safe_stderr_print(f"[quote] eastmoney index failed for {code}: {exc}")

        if code not in df.index:
            attempted_sources.append("sina_index")
            try:
                if ak is not None:
                    df_sina = _run_with_retry(ak.stock_zh_index_spot_sina, _QUOTE_TIMEOUTS)
                    if df_sina is not None and not df_sina.empty:
                        df_sina["代码"] = df_sina["代码"].apply(normalize_code)
                        df_sina = df_sina.set_index("代码", drop=False)
                        if code in df_sina.index:
                            df = df_sina
                            cached = False
                            used_source = "sina_index"
            except Exception as exc:
                fallback_reason_parts.append(f"sina_index失败: {exc}")
                safe_stderr_print(f"[quote] index sina fallback failed for {code}: {exc}")

        if code not in df.index:
            fallback_response = _tushare_index_daily_response(f"未找到指数 {code}")
            if fallback_response is not None:
                return fallback_response
            return _degraded_empty_index_quote(
                code,
                f"未找到指数 {code}",
                attempted_sources=attempted_sources,
                source_chain=["eastmoney_index", "sina_index", "tushare_index_daily"],
                fallback_reason=fallback_reason_parts or f"未找到指数 {code}",
            )

        row = df.loc[code]
        price = safe_float(pick_value(row, ["最新价", "最新", "现价"]))
        if price is None:
            fallback_response = _tushare_index_daily_response(f"指数 {code} 缺少价格数据")
            if fallback_response is not None:
                return fallback_response
            return _degraded_empty_index_quote(
                code,
                f"指数 {code} 缺少价格数据",
                attempted_sources=attempted_sources,
                source_chain=["eastmoney_index", "sina_index", "tushare_index_daily"],
                fallback_reason=fallback_reason_parts or f"指数 {code} 缺少价格数据",
            )

        payload = {
            "code": code,
            "name": _safe_index_name(pick_value(row, ["名称", "指数名称"]), code),
            "price": price,
            "change": safe_float(pick_value(row, ["涨跌额", "涨跌"])),
            "changePercent": safe_float(pick_value(row, ["涨跌幅", "涨幅"])),
            "open": safe_float(pick_value(row, ["今开", "开盘"])),
            "high": safe_float(pick_value(row, ["最高", "最高价"])),
            "low": safe_float(pick_value(row, ["最低", "最低价"])),
            "preClose": safe_float(pick_value(row, ["昨收", "昨收价"])),
            "volume": safe_int(pick_value(row, ["成交量"])),
            "amount": safe_float(pick_value(row, ["成交额"])),
            "source": used_source,
        }
        active_chain = ["eastmoney_index"] if used_source == "eastmoney_index" else ["eastmoney_index", "sina_index"]
        active_attempts = ["eastmoney_index"] if used_source == "eastmoney_index" else attempted_sources
        return _ok_quote_response(
            payload,
            attempted_sources=active_attempts,
            source_chain=active_chain,
            fallback_reason=fallback_reason_parts or None,
        )
    except Exception as exc:
        err = str(exc)
        fallback_response = None
        if "decode" in err.lower() or "starting with" in err or "'<'" in err:
            fallback_response = _tushare_index_daily_response(err[:200])
        if fallback_response is not None:
            return fallback_response
        # P3-5.8 fix: 全降级链空时返回 success=false(诊断报告 §5.8)
        return _degraded_empty_index_quote(
            code,
            err,
            attempted_sources=attempted_sources,
            source_chain=["eastmoney_index", "sina_index", "tushare_index_daily"],
            fallback_reason=fallback_reason_parts or err,
            strict_failure=True,
        )
