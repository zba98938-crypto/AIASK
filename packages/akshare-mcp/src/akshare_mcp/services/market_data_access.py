"""DB-first market data access helpers for AKShare MCP tools."""

from __future__ import annotations

import asyncio
import os
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from datetime import datetime
from typing import Any

from ..data_source import data_source
from ..storage import get_db, run_with_db_cleanup
from ..utils import normalize_code, safe_float, safe_int, safe_stderr_print

FALLBACK_DB_ONLY = "db_only"
FALLBACK_DB_FIRST_LIVE = "db_first_live_fallback"
FALLBACK_LIVE_ONLY = "live_only_explicit"


def _env_flag(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def quote_max_stale_seconds() -> float:
    try:
        return max(0.0, float(os.getenv("AKSHARE_MCP_QUOTE_MAX_STALE_SECONDS", "30") or "30"))
    except ValueError:
        return 30.0


def _realtime_fallback_enabled() -> bool:
    return _env_flag("AKSHARE_MCP_REALTIME_FALLBACK", True)


def _parse_asof(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value.astimezone() if value.tzinfo else value.astimezone()
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(text).astimezone()
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(text[: len(fmt)], fmt).astimezone()
        except ValueError:
            continue
    return None


def _freshness_seconds(value: Any) -> float | None:
    parsed = _parse_asof(value)
    if parsed is None:
        return None
    return round(max((datetime.now().astimezone() - parsed).total_seconds(), 0.0), 3)


def _normalize_quote(raw: dict[str, Any], *, code: str, source: str) -> dict[str, Any]:
    payload = dict(raw or {})
    normalized_code = normalize_code(payload.get("code") or code)
    change_amt = payload.get("change_amt")
    if change_amt is None:
        change_amt = payload.get("change")
    change_pct = payload.get("changePercent")
    if change_pct is None:
        change_pct = payload.get("change_pct") or payload.get("pct_chg")
    prev_close = (
        payload.get("preClose")
        if payload.get("preClose") is not None
        else payload.get("pre_close")
    )
    if prev_close is None:
        prev_close = payload.get("prev_close")
    asof_value = payload.get("time") or payload.get("trade_time") or payload.get("updated_at")
    return {
        "code": normalized_code,
        "name": payload.get("name") or payload.get("stock_name") or "",
        "price": safe_float(payload.get("price") or payload.get("last") or payload.get("close")),
        "change": safe_float(change_amt),
        "change_amt": safe_float(change_amt),
        "changePercent": safe_float(change_pct),
        "change_pct": safe_float(change_pct),
        "open": safe_float(payload.get("open")),
        "high": safe_float(payload.get("high")),
        "low": safe_float(payload.get("low")),
        "preClose": safe_float(prev_close),
        "prev_close": safe_float(prev_close),
        "volume": safe_int(payload.get("volume")),
        "amount": safe_float(payload.get("amount")),
        "pe": safe_float(payload.get("pe") or payload.get("pe_ttm")),
        "pb": safe_float(payload.get("pb")),
        "mkt_cap": safe_float(payload.get("mkt_cap") or payload.get("market_cap")),
        "time": asof_value,
        "data_timestamp": asof_value,
        "source": source,
    }


async def _save_live_quote(payload: dict[str, Any]) -> None:
    try:
        if not isinstance(payload, dict) or payload.get("price") is None:
            return
        db = get_db()
        await db.save_quote(payload)
    except Exception as exc:
        safe_stderr_print(f"[market_data_access] save live quote skipped: {exc}")


def _access_result(
    *,
    success: bool,
    code: str,
    data: dict[str, Any] | None,
    source_chain: list[str],
    backend_used: str,
    backend_requested: str,
    fallback_used: bool,
    fallback_reason: list[str] | None = None,
    db_snapshot_time: Any = None,
    data_freshness_seconds: float | None = None,
    stale: bool = False,
    error: str | None = None,
) -> dict[str, Any]:
    reasons = [str(item).strip() for item in list(fallback_reason or []) if str(item).strip()]
    result = {
        "success": bool(success),
        "code": code,
        "data": data,
        "source_chain": list(dict.fromkeys(source_chain)),
        "backend_requested": backend_requested,
        "backend_used": backend_used,
        "fallback_used": bool(fallback_used),
        "fallback_reason": reasons or None,
        "db_snapshot_time": db_snapshot_time,
        "data_freshness_seconds": data_freshness_seconds,
        "stale": bool(stale),
        "error": error,
        "attempted_sources": list(dict.fromkeys(source_chain)),
    }
    if isinstance(data, dict):
        for key in (
            "source_chain",
            "backend_requested",
            "backend_used",
            "fallback_used",
            "fallback_reason",
            "db_snapshot_time",
            "data_freshness_seconds",
            "stale",
        ):
            data[key] = result.get(key)
    return result


async def get_quote_snapshot(
    code: str,
    *,
    freshness_ttl: float | None = None,
    fallback_mode: str | None = None,
) -> dict[str, Any]:
    """Read a stock quote snapshot from SQLite before any live source."""
    normalized_code = normalize_code(code)
    ttl = quote_max_stale_seconds() if freshness_ttl is None else max(float(freshness_ttl), 0.0)
    mode = str(fallback_mode or os.getenv("AKSHARE_MCP_DATA_ACCESS_POLICY") or FALLBACK_DB_FIRST_LIVE).strip()
    if mode == "db_first":
        mode = FALLBACK_DB_FIRST_LIVE
    if mode not in {FALLBACK_DB_ONLY, FALLBACK_DB_FIRST_LIVE, FALLBACK_LIVE_ONLY}:
        mode = FALLBACK_DB_FIRST_LIVE

    fallback_reasons: list[str] = []
    db_payload: dict[str, Any] | None = None
    db_snapshot_time: Any = None
    freshness: float | None = None
    stale = False

    if mode != FALLBACK_LIVE_ONLY:
        try:
            db = get_db()
            raw = await db.get_latest_quote(normalized_code)
            if isinstance(raw, dict) and raw:
                db_snapshot_time = raw.get("time") or raw.get("updated_at")
                freshness = _freshness_seconds(db_snapshot_time)
                stale = freshness is not None and freshness > ttl
                db_payload = _normalize_quote(raw, code=normalized_code, source="db.stock_quotes")
                if not stale or mode == FALLBACK_DB_ONLY or not _realtime_fallback_enabled():
                    reasons = ["db_snapshot_stale"] if stale else []
                    return _access_result(
                        success=True,
                        code=normalized_code,
                        data=db_payload,
                        source_chain=["db.stock_quotes"],
                        backend_requested="db.stock_quotes",
                        backend_used="db.stock_quotes",
                        fallback_used=False,
                        fallback_reason=reasons,
                        db_snapshot_time=db_snapshot_time,
                        data_freshness_seconds=freshness,
                        stale=stale,
                    )
                fallback_reasons.append("db_snapshot_stale")
            else:
                fallback_reasons.append("db_snapshot_missing")
        except Exception as exc:
            fallback_reasons.append(f"db_snapshot_failed:{exc}")
            safe_stderr_print(f"[market_data_access] DB quote read failed for {normalized_code}: {exc}")

    if mode == FALLBACK_DB_ONLY or not _realtime_fallback_enabled():
        if db_payload is not None:
            return _access_result(
                success=True,
                code=normalized_code,
                data=db_payload,
                source_chain=["db.stock_quotes"],
                backend_requested="db.stock_quotes",
                backend_used="db.stock_quotes",
                fallback_used=False,
                fallback_reason=fallback_reasons,
                db_snapshot_time=db_snapshot_time,
                data_freshness_seconds=freshness,
                stale=stale,
            )
        return _access_result(
            success=False,
            code=normalized_code,
            data=None,
            source_chain=["db.stock_quotes"],
            backend_requested="db.stock_quotes",
            backend_used="none",
            fallback_used=False,
            fallback_reason=fallback_reasons,
            error="quote snapshot not found in SQLite",
        )

    source_chain = [] if mode == FALLBACK_LIVE_ONLY else ["db.stock_quotes"]
    source_chain.append("data_source.realtime_quote")
    try:
        raw_live = await asyncio.to_thread(data_source.get_realtime_quote, normalized_code)
        live = raw_live.get("data") if isinstance(raw_live, dict) and raw_live.get("success") else raw_live
        if isinstance(live, dict) and live.get("price") is not None:
            source = str(live.get("source") or "data_source.realtime_quote")
            live_payload = _normalize_quote(live, code=normalized_code, source=source)
            await _save_live_quote(live_payload)
            return _access_result(
                success=True,
                code=normalized_code,
                data=live_payload,
                source_chain=source_chain,
                backend_requested=source_chain[0],
                backend_used=source,
                fallback_used=mode != FALLBACK_LIVE_ONLY,
                fallback_reason=fallback_reasons,
                db_snapshot_time=db_snapshot_time,
                data_freshness_seconds=0.0,
                stale=False,
            )
        fallback_reasons.append("live_quote_empty")
    except Exception as exc:
        fallback_reasons.append(f"live_quote_failed:{exc}")
        safe_stderr_print(f"[market_data_access] live quote failed for {normalized_code}: {exc}")

    if db_payload is not None:
        return _access_result(
            success=True,
            code=normalized_code,
            data=db_payload,
            source_chain=["db.stock_quotes", "data_source.realtime_quote"],
            backend_requested="db.stock_quotes",
            backend_used="db.stock_quotes",
            fallback_used=False,
            fallback_reason=fallback_reasons,
            db_snapshot_time=db_snapshot_time,
            data_freshness_seconds=freshness,
            stale=True,
        )

    return _access_result(
        success=False,
        code=normalized_code,
        data=None,
        source_chain=source_chain,
        backend_requested=source_chain[0] if source_chain else "data_source.realtime_quote",
        backend_used="none",
        fallback_used=mode != FALLBACK_LIVE_ONLY,
        fallback_reason=fallback_reasons,
        error="all quote sources returned empty",
    )


def get_quote_snapshot_sync(
    code: str,
    *,
    freshness_ttl: float | None = None,
    fallback_mode: str | None = None,
    timeout: float = 10.0,
) -> dict[str, Any]:
    def _runner() -> dict[str, Any]:
        return run_with_db_cleanup(
            get_quote_snapshot(
                code,
                freshness_ttl=freshness_ttl,
                fallback_mode=fallback_mode,
            )
        )

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
                return _access_result(
                    success=False,
                    code=normalize_code(code),
                    data=None,
                    source_chain=["db.stock_quotes"],
                    backend_requested="db.stock_quotes",
                    backend_used="none",
                    fallback_used=False,
                    error=f"quote snapshot timeout >{timeout:g}s",
                )
    return _runner()


def attach_quote_access_meta(response: dict[str, Any], access: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(response, dict) or not isinstance(access, dict):
        return response
    for key in (
        "source_chain",
        "backend_requested",
        "backend_used",
        "fallback_used",
        "fallback_reason",
        "db_snapshot_time",
        "data_freshness_seconds",
        "stale",
    ):
        response[key] = access.get(key)
    meta = response.get("meta") if isinstance(response.get("meta"), dict) else {}
    quality = meta.get("quality") if isinstance(meta.get("quality"), dict) else {}
    quality.update(
        {
            "source_chain": access.get("source_chain") or [],
            "backend_requested": access.get("backend_requested"),
            "backend_used": access.get("backend_used"),
            "fallback_used": bool(access.get("fallback_used")),
            "fallback_reason": access.get("fallback_reason"),
            "db_snapshot_time": access.get("db_snapshot_time"),
            "data_freshness_seconds": access.get("data_freshness_seconds"),
            "stale": bool(access.get("stale")),
        }
    )
    meta["quality"] = quality
    response["meta"] = meta
    data = response.get("data")
    if isinstance(data, dict):
        for key in (
            "source_chain",
            "backend_requested",
            "backend_used",
            "fallback_used",
            "fallback_reason",
            "db_snapshot_time",
            "data_freshness_seconds",
            "stale",
        ):
            data[key] = access.get(key)
    return response


async def get_quote_snapshot_response(
    code: str,
    *,
    freshness_ttl: float | None = None,
    fallback_mode: str = FALLBACK_DB_ONLY,
) -> dict[str, Any]:
    access = await get_quote_snapshot(code, freshness_ttl=freshness_ttl, fallback_mode=fallback_mode)
    if access.get("success"):
        response = {
            "success": True,
            "data": access.get("data") or {},
            "error": None,
            "source": access.get("backend_used") or "db.stock_quotes",
            "cached": access.get("backend_used") == "db.stock_quotes",
            "timestamp": datetime.now().isoformat(),
        }
    else:
        response = {
            "success": False,
            "data": None,
            "error": access.get("error") or "quote snapshot unavailable",
            "source": "none",
            "cached": False,
            "timestamp": datetime.now().isoformat(),
        }
    return attach_quote_access_meta(response, access)
