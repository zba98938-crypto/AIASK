"""
AKShare MCP Server Utilities
"""

from __future__ import annotations

import asyncio
import io
import json
import os
import re
import sys
from contextlib import contextmanager, redirect_stdout
from datetime import date, datetime
from typing import Any, Iterator, Optional
from urllib.error import URLError
from urllib.request import Request, urlopen

import pandas as pd

SOURCE_NAME = "akshare"
_STRICT_STOCK_CODE_PATTERNS = (
    re.compile(r"^\d{1,6}$"),
    re.compile(r"^(?:sh|sz|bj)(\d{6})$", re.IGNORECASE),
    re.compile(r"^(\d{6})\.(?:sh|sz|bj)$", re.IGNORECASE),
)


def now_iso() -> str:
    return datetime.now().isoformat()


def _dedupe_text_items(items: list[Any]) -> list[str]:
    deduped: list[str] = []
    for item in items:
        text = str(item or "").strip()
        if text and text not in deduped:
            deduped.append(text)
    return deduped


def enrich_response_meta(
    result: dict[str, Any],
    *,
    source: str | None = None,
    source_chain: list[str] | None = None,
    quality_flags: list[str] | None = None,
    degraded: bool | None = None,
    fallback_used: bool | None = None,
) -> dict[str, Any]:
    if not isinstance(result, dict):
        return result

    resolved_source = str(source or result.get("source") or SOURCE_NAME).strip() or SOURCE_NAME
    chain = _dedupe_text_items(list(source_chain or result.get("source_chain") or [resolved_source]))
    if not chain:
        chain = [resolved_source]

    flags = _dedupe_text_items(list(quality_flags or result.get("quality_flags") or []))
    success = bool(result.get("success"))
    if not success and "failed" not in flags:
        flags.append("failed")

    flag_set = {str(flag).lower() for flag in flags}
    resolved_fallback = bool(fallback_used) if fallback_used is not None else bool(result.get("fallback_used"))
    resolved_fallback = resolved_fallback or len(chain) > 1 or "fallback" in flag_set
    resolved_degraded = bool(degraded) if degraded is not None else bool(result.get("degraded"))
    resolved_degraded = resolved_degraded or "degraded" in flag_set
    resolved_degraded = resolved_degraded or (not success)

    # F-N01-2 fix (诊断报告 §N01/N21/N24/N26/N28/N29): 顶层 envelope 不应掩盖
    # data 内层的 fallback_used/degraded。许多工具把 backend_used/fallback_used 直接
    # 合并进 data（如 skills 的 _skill_payload），导致 data.fallback_used=true 但顶层
    # fallback_used=false。此处把内层一层标志上浮，使顶层与内层一致。
    inner = result.get("data")
    if isinstance(inner, dict):
        inner_fallback = bool(inner.get("fallback_used")) or inner.get("fallback_reason") not in (None, "", [])
        inner_degraded = bool(inner.get("degraded"))
        if inner_fallback:
            resolved_fallback = True
        if inner_degraded:
            resolved_degraded = True
        if inner.get("fallback_reason") not in (None, "", []) and result.get("fallback_reason") in (None, "", []):
            result["fallback_reason"] = inner.get("fallback_reason")

    if resolved_fallback and "fallback" not in flag_set:
        flags.append("fallback")
        flag_set.add("fallback")
    if resolved_degraded and "degraded" not in flag_set:
        flags.append("degraded")
        flag_set.add("degraded")

    result["source"] = resolved_source
    result["source_chain"] = chain
    result["quality_flags"] = flags
    result["fallback_used"] = resolved_fallback
    result["degraded"] = resolved_degraded

    meta = result.get("meta")
    if not isinstance(meta, dict):
        meta = {}
    quality = meta.get("quality")
    if not isinstance(quality, dict):
        quality = {}

    quality.setdefault("status", "available" if success else "failed")
    quality["source_chain"] = chain
    quality["quality_flags"] = list(flags)
    quality["fallback_used"] = resolved_fallback
    if result.get("fallback_reason") not in (None, "", []):
        quality["fallback_reason"] = result.get("fallback_reason")
    quality.setdefault("backend_requested", chain[0] if chain else resolved_source)
    quality["backend_used"] = resolved_source

    meta["quality"] = quality
    meta["source_chain"] = chain
    meta["degraded"] = resolved_degraded
    result["meta"] = meta
    return result


def propagate_data_quality_to_top(result: dict[str, Any]) -> dict[str, Any]:
    """Promote nested data quality/fallback provenance into the standard envelope.

    Several analytical tools build rich ``data.data_quality`` payloads and then
    wrap them with ``ok(...)``.  ``ok`` cannot infer nested degraded/fallback
    state by itself, so callers can use this helper just before returning.
    """
    if not isinstance(result, dict):
        return result

    data = result.get("data")
    if not isinstance(data, dict):
        return enrich_response_meta(result)

    quality = data.get("data_quality")
    if not isinstance(quality, dict):
        quality = {}

    nested_chain = (
        data.get("source_chain")
        or quality.get("source_chain")
        or quality.get("fallback_chain")
        or result.get("source_chain")
    )
    nested_flags = list(result.get("quality_flags") or [])
    for value in list(data.get("quality_flags") or []) + list(quality.get("quality_flags") or []):
        text = str(value or "").strip()
        if text and text not in nested_flags:
            nested_flags.append(text)

    nested_fallback_reason = (
        data.get("fallback_reason")
        if data.get("fallback_reason") not in (None, "", [])
        else quality.get("fallback_reason")
    )
    nested_fallback = (
        bool(data.get("fallback_used"))
        or bool(quality.get("fallback_used"))
        or nested_fallback_reason not in (None, "", [])
    )
    nested_degraded = (
        bool(data.get("degraded"))
        or bool(quality.get("degraded"))
        or "degraded" in {str(flag).lower() for flag in nested_flags}
    )
    if data.get("stale") or data.get("depth_degraded"):
        nested_degraded = True
        for flag in ("stale" if data.get("stale") else "", "depth_degraded" if data.get("depth_degraded") else ""):
            if flag and flag not in nested_flags:
                nested_flags.append(flag)
    if nested_fallback:
        # A fallback result may contain data, but provenance changed enough that
        # callers should treat it as a lower-confidence answer.
        nested_degraded = True
        if "degraded" not in {str(flag).lower() for flag in nested_flags}:
            nested_flags.append("degraded")

    if nested_fallback_reason not in (None, "", []):
        result["fallback_reason"] = nested_fallback_reason
    if nested_fallback:
        result["fallback_used"] = True
    if nested_degraded:
        result["degraded"] = True
    if nested_degraded and "degraded" not in {str(flag).lower() for flag in nested_flags}:
        nested_flags.append("degraded")
    if nested_fallback and "fallback" not in {str(flag).lower() for flag in nested_flags}:
        nested_flags.append("fallback")

    return enrich_response_meta(
        result,
        source_chain=list(nested_chain) if isinstance(nested_chain, list) else None,
        quality_flags=nested_flags,
        degraded=nested_degraded or None,
        fallback_used=nested_fallback or None,
    )


def ok_degraded_empty(
    data: Any,
    *,
    fallback_reason: Any,
    source_chain: list[str] | None = None,
    quality_flags: list[str] | None = None,
    cached: bool = False,
) -> dict:
    """Return a stable successful-but-degraded envelope for empty upstream data."""
    result = ok(data, cached=cached)
    result["fallback_reason"] = fallback_reason
    flags = list(quality_flags or [])
    for flag in ("empty", "degraded"):
        if flag not in flags:
            flags.append(flag)
    return enrich_response_meta(
        result,
        source_chain=source_chain,
        quality_flags=flags,
        degraded=True,
        fallback_used=True,
    )


def ok(data: Any, *, cached: bool = False) -> dict:
    return enrich_response_meta({
        "success": True,
        "data": data,
        "error": None,
        "source": SOURCE_NAME,
        "cached": cached,
        "timestamp": now_iso(),
    })


def fail(
    error: Any,
    *,
    error_code: str | None = None,
    data: Any = None,
    source: str | None = None,
    source_chain: list[str] | None = None,
    quality_flags: list[str] | None = None,
    degraded: bool | None = True,
    fallback_used: bool | None = None,
    fallback_reason: Any = None,
    cached: bool = False,
) -> dict:
    result = {
        "success": False,
        "data": data,
        "error": str(error),
        "source": source or SOURCE_NAME,
        "cached": cached,
        "timestamp": now_iso(),
    }
    if error_code:
        result["error_code"] = error_code
    if fallback_reason not in (None, "", []):
        result["fallback_reason"] = fallback_reason
    return enrich_response_meta(
        result,
        source=source,
        source_chain=source_chain,
        quality_flags=quality_flags,
        degraded=degraded,
        fallback_used=fallback_used,
    )


def safe_stderr_print(*args: Any, sep: str = " ", end: str = "\n") -> None:
    """Best-effort stderr logging that never raises."""
    try:
        msg = sep.join(str(arg) for arg in args) + end
    except Exception:
        msg = " ".join(repr(arg) for arg in args) + end

    try:
        stream = getattr(sys, "stderr", None)
        if stream is None:
            return
        stream.write(msg)
        flush = getattr(stream, "flush", None)
        if callable(flush):
            flush()
    except Exception:
        # Never let diagnostics logging mask the original exception.
        return


@contextmanager
def suppress_stdout(log_prefix: str | None = None) -> Iterator[None]:
    """Protect MCP stdio by swallowing unexpected third-party stdout."""
    buf = io.StringIO()
    with redirect_stdout(buf):
        yield
    leaked = buf.getvalue().strip()
    if leaked and log_prefix:
        preview = leaked if len(leaked) <= 1000 else leaked[:1000] + "...(truncated)"
        safe_stderr_print(f"{log_prefix} suppressed stdout: {preview}")


def safe_float(val: Any) -> Optional[float]:
    """安全转换为浮点数：缺失/异常返回 None（避免用 0 伪装缺失）"""
    try:
        if val is None or pd.isna(val):
            return None
        return float(val)
    except (ValueError, TypeError):
        return None


def safe_int(val: Any) -> Optional[int]:
    """安全转换为整数：缺失/异常返回 None（避免用 0 伪装缺失）"""
    try:
        if val is None or pd.isna(val):
            return None
        return int(float(val))
    except (ValueError, TypeError):
        return None


def parse_numeric(val: Any) -> Optional[float]:
    """解析带单位/百分号的数值字符串，无法解析返回 None。"""
    if val is None or pd.isna(val):
        return None
    if isinstance(val, (int, float)):
        return float(val)
    s = str(val).strip()
    if not s or s.lower() in {"none", "nan", "false", "--"}:
        return None

    multiplier = 1.0
    if s.endswith("%"):
        s = s[:-1].strip()
    if s.endswith("万亿"):
        multiplier = 1e12
        s = s[:-2].strip()
    elif s.endswith("亿"):
        multiplier = 1e8
        s = s[:-1].strip()
    elif s.endswith("万"):
        multiplier = 1e4
        s = s[:-1].strip()
    elif s.endswith("元"):
        s = s[:-1].strip()

    s = s.replace(",", "")
    try:
        return float(s) * multiplier
    except (ValueError, TypeError):
        return None


def normalize_code(code: str) -> str:
    """
    规范化股票/指数代码为 6 位数字（补零）。
    兼容输入: '1', '000001', 'sh600519', 'SZ000001' 等。
    """
    s = str(code or "").strip()
    m = re.search(r"(\d{1,6})", s)
    if not m:
        return s
    return m.group(1).zfill(6)


def normalize_stock_code_strict(code: Any) -> Optional[str]:
    """严格规范化股票代码，仅接受纯数字或带交易所前后缀的别名。"""
    text = str(code or "").strip()
    if not text:
        return None
    for pattern in _STRICT_STOCK_CODE_PATTERNS:
        match = pattern.fullmatch(text)
        if match is None:
            continue
        if match.lastindex:
            return str(match.group(1)).zfill(6)
        return text.zfill(6)
    return None


def resolve_security_code_strict(
    code: Any = None,
    *,
    stock_code: Any = None,
    symbol: Any = None,
    ticker: Any = None,
) -> str:
    """严格解析证券代码别名；非法格式返回空字符串。"""
    for candidate in (code, stock_code, symbol, ticker):
        text = str(candidate or "").strip()
        if not text:
            continue
        normalized = normalize_stock_code_strict(text)
        if normalized:
            return normalized
        return ""
    return ""


def resolve_security_code(
    code: Any = None,
    *,
    stock_code: Any = None,
    symbol: Any = None,
    ticker: Any = None,
    normalize: bool = True,
) -> str:
    """
    统一解析证券代码别名。

    兼容 MCP / 前端常见传参:
    - code
    - stock_code
    - symbol
    - ticker
    """
    for candidate in (code, stock_code, symbol, ticker):
        text = str(candidate or "").strip()
        if not text:
            continue
        return normalize_code(text) if normalize else text
    return ""


def stock_code_missing_error() -> str:
    return "需要提供股票代码（支持 code / stock_code / symbol / ticker）"


def stock_code_format_error(raw_code: Any) -> str:
    return f"股票代码格式无效: {raw_code}。需为 6 位数字或带 sh/sz/bj 前后缀"


def stock_code_not_found_error(code: str) -> str:
    return f"未找到股票 {code} 的信息"


def validate_stock_code_format(
    raw_code: Any,
    *,
    allow_empty: bool = False,
    field_name: str = "股票代码",
) -> tuple[Optional[str], Optional[str]]:
    text = str(raw_code or "").strip()
    if not text:
        if allow_empty:
            return None, None
        return None, f"需要提供{field_name}"
    normalized = normalize_stock_code_strict(text)
    if not normalized:
        return None, stock_code_format_error(text)
    return normalized, None


def _stock_info_payload_is_usable(payload: Any) -> bool:
    if not isinstance(payload, dict):
        return False
    return any(
        str(payload.get(key) or "").strip()
        for key in ("name", "stock_name", "industry", "list_date", "listDate")
    )


def _minimal_confirmed_stock_info(code: str, *, source: str, payload: Any = None) -> dict[str, Any]:
    info = dict(payload or {}) if isinstance(payload, dict) else {}
    return {
        "code": str(info.get("code") or code),
        "name": info.get("name") or info.get("stock_name"),
        "stock_name": info.get("stock_name") or info.get("name"),
        "industry": info.get("industry") or info.get("sector"),
        "list_date": info.get("list_date") or info.get("listDate") or info.get("list_date_ts"),
        "source": source,
        "degraded": True,
        "fallback_used": True,
        "fallback_reason": "stock identity confirmed by fallback source",
    }


async def _lookup_stock_info_fallback_async(code: str) -> Optional[dict[str, Any]]:
    try:
        from .storage import get_db

        db = get_db()
        if hasattr(db, "list_stocks_by_codes"):
            rows = await db.list_stocks_by_codes([code])
            if rows:
                return _minimal_confirmed_stock_info(code, source="db.stocks", payload=rows[0])
        if hasattr(db, "get_latest_quote"):
            quote = await db.get_latest_quote(code)
            if quote:
                return _minimal_confirmed_stock_info(code, source="db.stock_quotes", payload=quote)
        if hasattr(db, "get_klines"):
            klines = await db.get_klines(code, limit=1)
            if klines:
                return _minimal_confirmed_stock_info(code, source="db.kline_1d")
    except Exception:
        pass

    try:
        from .data_source import data_source

        pro = data_source.get_tushare_pro()
        if pro:
            ts_code = f"{code}.SH" if str(code).startswith(("6", "9")) else f"{code}.SZ"
            df = await asyncio.to_thread(
                pro.stock_basic,
                ts_code=ts_code,
                fields="ts_code,symbol,name,industry,list_date",
            )
            if df is not None and not getattr(df, "empty", True):
                row = df.iloc[0].to_dict()
                return _minimal_confirmed_stock_info(
                    code,
                    source="tushare_pro.stock_basic",
                    payload={
                        "code": row.get("symbol") or code,
                        "name": row.get("name"),
                        "industry": row.get("industry"),
                        "list_date": row.get("list_date"),
                    },
                )
    except Exception:
        pass

    try:
        from .data_source import data_source

        rows = await asyncio.to_thread(data_source.get_kline, code, "daily", 1)
        if rows:
            return _minimal_confirmed_stock_info(code, source="data_source.get_kline")
    except Exception:
        pass

    return None


def _lookup_stock_info_fallback_sync(code: str) -> Optional[dict[str, Any]]:
    try:
        from .data_source import data_source

        pro = data_source.get_tushare_pro()
        if pro:
            ts_code = f"{code}.SH" if str(code).startswith(("6", "9")) else f"{code}.SZ"
            df = pro.stock_basic(ts_code=ts_code, fields="ts_code,symbol,name,industry,list_date")
            if df is not None and not getattr(df, "empty", True):
                row = df.iloc[0].to_dict()
                return _minimal_confirmed_stock_info(
                    code,
                    source="tushare_pro.stock_basic",
                    payload={
                        "code": row.get("symbol") or code,
                        "name": row.get("name"),
                        "industry": row.get("industry"),
                        "list_date": row.get("list_date"),
                    },
                )
        rows = data_source.get_kline(code, "daily", 1)
        if rows:
            return _minimal_confirmed_stock_info(code, source="data_source.get_kline")
    except Exception:
        pass
    return None


async def lookup_existing_stock_info_async(code: str) -> Optional[dict[str, Any]]:
    normalized = normalize_stock_code_strict(code)
    if not normalized:
        return None

    try:
        from .storage import get_db

        db = get_db()
        if hasattr(db, "get_stock_info"):
            payload = await db.get_stock_info(normalized)
            if _stock_info_payload_is_usable(payload):
                return dict(payload)
    except Exception:
        pass

    try:
        from .tools.finance import get_stock_info as finance_get_stock_info

        response = await asyncio.to_thread(finance_get_stock_info, normalized)
        payload = response.get("data") if isinstance(response, dict) and response.get("success") else None
        if _stock_info_payload_is_usable(payload):
            return dict(payload)
    except Exception:
        pass

    return await _lookup_stock_info_fallback_async(normalized)


def lookup_existing_stock_info_sync(code: str) -> Optional[dict[str, Any]]:
    normalized = normalize_stock_code_strict(code)
    if not normalized:
        return None

    try:
        from .tools.finance import get_stock_info as finance_get_stock_info

        response = finance_get_stock_info(normalized)
        payload = response.get("data") if isinstance(response, dict) and response.get("success") else None
        if _stock_info_payload_is_usable(payload):
            return dict(payload)
    except Exception:
        pass
    return _lookup_stock_info_fallback_sync(normalized)


async def resolve_existing_security_code_async(
    code: Any = None,
    *,
    stock_code: Any = None,
    symbol: Any = None,
    ticker: Any = None,
) -> tuple[Optional[str], Optional[dict[str, Any]], Optional[str]]:
    raw_code = resolve_security_code(code, stock_code=stock_code, symbol=symbol, ticker=ticker, normalize=False)
    if not raw_code:
        return None, None, stock_code_missing_error()
    normalized, format_error = validate_stock_code_format(raw_code)
    if format_error:
        return None, None, format_error
    stock_info = await lookup_existing_stock_info_async(str(normalized))
    if stock_info is None:
        return None, None, stock_code_not_found_error(str(normalized))
    return str(normalized), stock_info, None


def resolve_existing_security_code_sync(
    code: Any = None,
    *,
    stock_code: Any = None,
    symbol: Any = None,
    ticker: Any = None,
) -> tuple[Optional[str], Optional[dict[str, Any]], Optional[str]]:
    raw_code = resolve_security_code(code, stock_code=stock_code, symbol=symbol, ticker=ticker, normalize=False)
    if not raw_code:
        return None, None, stock_code_missing_error()
    normalized, format_error = validate_stock_code_format(raw_code)
    if format_error:
        return None, None, format_error
    stock_info = lookup_existing_stock_info_sync(str(normalized))
    if stock_info is None:
        return None, None, stock_code_not_found_error(str(normalized))
    return str(normalized), stock_info, None


def validate_int_range(
    value: Any,
    *,
    field_name: str,
    minimum: int | None = None,
    maximum: int | None = None,
) -> tuple[Optional[int], Optional[str]]:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None, f"{field_name} 必须为整数"

    if minimum is not None and parsed < minimum:
        if maximum is None:
            return None, f"{field_name} 必须 >= {minimum}"
        return None, f"{field_name} 必须介于 {minimum} 和 {maximum}"
    if maximum is not None and parsed > maximum:
        if minimum is not None:
            return None, f"{field_name} 必须介于 {minimum} 和 {maximum}"
        return None, f"{field_name} 必须 <= {maximum}"
    return parsed, None


def _tool_arg_present(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, tuple, set, dict)):
        return bool(value)
    return True


def resolve_canonical_arg(
    canonical_name: str,
    canonical_value: Any = None,
    **aliases: Any,
) -> tuple[Any, list[dict[str, Any]], Optional[str]]:
    if _tool_arg_present(canonical_value):
        return canonical_value, [], canonical_name
    for alias_name, alias_value in aliases.items():
        if not _tool_arg_present(alias_value):
            continue
        return (
            alias_value,
            [
                {
                    "canonical": canonical_name,
                    "matched": alias_name,
                    "deprecated": True,
                }
            ],
            alias_name,
        )
    return canonical_value, [], None


def attach_argument_contract_meta(
    result: dict[str, Any],
    *,
    canonical_tool: str,
    canonical_args: dict[str, Any],
    alias_hits: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    if not isinstance(result, dict):
        return result

    meta = result.get("meta")
    if not isinstance(meta, dict):
        meta = {}

    filtered_args = {
        str(key): value
        for key, value in dict(canonical_args or {}).items()
        if _tool_arg_present(value)
    }
    normalized_hits = []
    for hit in alias_hits or []:
        if not isinstance(hit, dict):
            continue
        matched = str(hit.get("matched") or "").strip()
        canonical = str(hit.get("canonical") or "").strip()
        if not matched or not canonical:
            continue
        normalized_hits.append(
            {
                "canonical": canonical,
                "matched": matched,
                "deprecated": bool(hit.get("deprecated", False)),
            }
        )

    meta["argument_contract"] = {
        "canonical_tool": str(canonical_tool or "").strip(),
        "canonical_args": filtered_args,
        "args_matched": sorted(filtered_args.keys()),
        "alias_hits": normalized_hits,
        "deprecated_aliases": [hit["matched"] for hit in normalized_hits if hit.get("deprecated")],
        "contract_version": "aiask.tool_args.v1",
    }
    if normalized_hits:
        meta["deprecation_warnings"] = [
            f"参数别名 {hit['matched']} 已废弃，请改用 {hit['canonical']}"
            for hit in normalized_hits
            if hit.get("deprecated")
        ]
    result["meta"] = meta
    return result


def format_period(value: Any) -> str:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    s = str(value).strip()
    if not s or s.lower() in {"none", "nan", "--"}:
        return ""
    if re.fullmatch(r"\d{8}", s):
        return f"{s[:4]}-{s[4:6]}-{s[6:]}"
    if re.fullmatch(r"\d{6}", s):
        return f"{s[:4]}-{s[4:6]}"
    return s


def format_date(d: date) -> str:
    return d.strftime("%Y-%m-%d")


def format_publish_date(value: Any, fallback: str) -> Optional[str]:
    formatted = format_period(value)
    if formatted:
        return formatted
    return fallback or None


def parse_date_input(value: str) -> Optional[date]:
    s = str(value or "").strip()
    if not s:
        return None
    for fmt in ("%Y-%m-%d", "%Y%m%d", "%Y/%m/%d"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    if re.fullmatch(r"\d{6}", s):
        try:
            return datetime.strptime(s + "01", "%Y%m%d").date()
        except ValueError:
            return None
    return None


def fetch_mofcom_shrzgm_via_curl() -> pd.DataFrame:
    url = "https://data.mofcom.gov.cn/datamofcom/front/gnmy/shrzgmQuery"
    req = Request(
        url,
        method="POST",
        headers={
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/json;charset=UTF-8",
            "User-Agent": "Mozilla/5.0",
        },
        data=b"{}",
    )

    try:
        with urlopen(req, timeout=15) as resp:
            status = getattr(resp, "status", 200)
            payload = resp.read().decode("utf-8", errors="ignore").strip()
    except URLError as exc:
        raise RuntimeError(f"HTTP 请求失败: {exc}") from exc
    except Exception as exc:
        raise RuntimeError(f"请求异常: {exc}") from exc

    if status and int(status) >= 400:
        raise RuntimeError(f"HTTP 状态异常: {status}")

    if not payload:
        raise RuntimeError("HTTP 返回为空")

    data = json.loads(payload)
    if not isinstance(data, list) or not data:
        raise RuntimeError("Mofcom 返回数据为空")

    df = pd.DataFrame(data)
    if df.empty:
        return df

    rename_map = {
        "date": "月份",
        "tiosfs": "社会融资规模增量",
        "rmblaon": "其中-人民币贷款",
        "forcloan": "其中-委托贷款外币贷款",
        "entrustloan": "其中-委托贷款",
        "trustloan": "其中-信托贷款",
        "ndbab": "其中-未贴现银行承兑汇票",
        "bibae": "其中-企业债券",
        "sfinfe": "其中-非金融企业境内股票融资",
    }
    df.rename(columns=rename_map, inplace=True)

    if "月份" in df.columns:
        df["月份"] = df["月份"].astype(str)

    for col in rename_map.values():
        if col == "月份" or col not in df.columns:
            continue
        df[col] = pd.to_numeric(df[col], errors="coerce")

    ordered_cols = [
        "月份",
        "社会融资规模增量",
        "其中-人民币贷款",
        "其中-委托贷款外币贷款",
        "其中-委托贷款",
        "其中-信托贷款",
        "其中-未贴现银行承兑汇票",
        "其中-企业债券",
        "其中-非金融企业境内股票融资",
    ]
    available_cols = [col for col in ordered_cols if col in df.columns]
    if available_cols:
        df = df[available_cols]

    df.sort_values(["月份"], inplace=True)
    df.reset_index(drop=True, inplace=True)
    return df


def pick_value(row: pd.Series, keys: list[str]) -> Any:
    """从 Series 中尝试获取列表中的 key，返回第一个非空值"""
    for k in keys:
        if k in row and pd.notna(row[k]) and str(row[k]).strip() != "":
            return row[k]
    return None
