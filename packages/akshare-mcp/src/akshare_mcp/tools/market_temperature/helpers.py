"""Market temperature pure helpers (split from market_temperature tool)."""

from __future__ import annotations

import asyncio
import math
from datetime import datetime, timedelta
from typing import Any

from ...services.market_temperature import build_market_temperature_snapshot

def _safe_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _safe_int(value: Any, default: int = 0) -> int:
    if value in (None, ""):
        return default
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(parsed):
        return default
    return int(parsed)


def _first_safe_float(*values: Any) -> float | None:
    for value in values:
        parsed = _safe_float(value)
        if parsed is not None:
            return parsed
    return None


def _first_safe_int(default: int, *values: Any) -> int:
    for value in values:
        parsed = _safe_float(value)
        if parsed is not None:
            return int(parsed)
    return default


def _first_present(row: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = row.get(key)
        if value is not None and value != "":
            return value
    return None


def _pct_change(latest: dict[str, Any], previous: dict[str, Any] | None) -> float | None:
    explicit = _safe_float(_first_present(latest, "change_pct", "pct_change"))
    if explicit is not None:
        return explicit
    close = _safe_float(latest.get("close"))
    prev_close = _safe_float((previous or {}).get("close"))
    if close is None or prev_close is None or prev_close <= 0:
        return None
    return (close - prev_close) / prev_close * 100.0


async def _build_stock_row(db: Any, stock: dict[str, Any], *, as_of: str | None, min_bars: int) -> dict[str, Any] | None:
    code = str(stock.get("code") or "").strip()
    if not code:
        return None
    try:
        klines = await db.get_klines(code, end_date=as_of, limit=max(21, int(min_bars or 20) + 1))
    except TypeError:
        klines = await db.get_klines(code, limit=max(21, int(min_bars or 20) + 1))
    except Exception:
        return None
    if not klines:
        return None
    latest = dict(klines[-1])
    previous = dict(klines[-2]) if len(klines) >= 2 else None
    closes = [_safe_float(row.get("close")) for row in klines[-20:]]
    close_values = [value for value in closes if value is not None]
    ma20 = sum(close_values) / len(close_values) if len(close_values) >= 20 else None
    return {
        "code": code,
        "name": stock.get("name") or stock.get("stock_name") or code,
        "industry": stock.get("industry") or stock.get("sector") or "",
        "date": latest.get("date") or latest.get("time"),
        "close": latest.get("close"),
        "pct_change": _pct_change(latest, previous),
        "ma20": ma20,
        "amount": latest.get("amount"),
        "turnover": latest.get("turnover"),
        "market_cap": stock.get("market_cap") or latest.get("market_cap") or latest.get("mkt_cap"),
    }


async def _load_stock_rows_from_db(db: Any, *, limit: int, as_of: str | None, min_bars: int) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    universe_limit = max(1, min(int(limit or 300), 1000))
    universe = await db.list_stock_universe(limit=universe_limit)
    semaphore = asyncio.Semaphore(16)

    async def guarded(stock: dict[str, Any]) -> dict[str, Any] | None:
        async with semaphore:
            return await _build_stock_row(db, stock, as_of=as_of, min_bars=min_bars)

    rows = await asyncio.gather(*(guarded(dict(stock or {})) for stock in universe))
    stock_rows = [row for row in rows if row is not None]
    return stock_rows, {
        "universe_limit": universe_limit,
        "universe_count": len(universe),
        "loaded_stock_rows": len(stock_rows),
        "missing_kline_rows": max(0, len(universe) - len(stock_rows)),
    }


def _bounded_top_n(value: int) -> int:
    return max(0, min(int(value or 0), 50))


def _sort_industries_by_temperature(industries: list[dict[str, Any]], *, reverse: bool) -> list[dict[str, Any]]:
    if reverse:
        return sorted(
            industries,
            key=lambda item: (
                _safe_float(item.get("temperature")) is not None,
                _safe_float(item.get("temperature")) if _safe_float(item.get("temperature")) is not None else -1.0,
                _safe_float(item.get("market_cap")) or 0.0,
            ),
            reverse=True,
        )
    return sorted(
        industries,
        key=lambda item: (
            _safe_float(item.get("temperature")) is None,
            _safe_float(item.get("temperature")) if _safe_float(item.get("temperature")) is not None else 101.0,
            -(_safe_float(item.get("market_cap")) or 0.0),
        ),
    )


def _snapshot_with_top_n(snapshot: dict[str, Any], top_n: int) -> dict[str, Any]:
    payload = dict(snapshot or {})
    industries = [dict(item or {}) for item in list(payload.get("industries") or [])]
    safe_top_n = _bounded_top_n(top_n)
    payload["industries"] = industries
    payload["hot_industries"] = _sort_industries_by_temperature(industries, reverse=True)[:safe_top_n]
    payload["cold_industries"] = _sort_industries_by_temperature(industries, reverse=False)[:safe_top_n]
    return payload


def _compact_cache_row(row: dict[str, Any], *, include_snapshot: bool = False) -> dict[str, Any]:
    snapshot = row.get("snapshot") if isinstance(row.get("snapshot"), dict) else {}
    market = snapshot.get("market") if isinstance(snapshot.get("market"), dict) else {}
    quality = snapshot.get("quality") if isinstance(snapshot.get("quality"), dict) else {}
    warnings = row.get("warnings") or quality.get("warnings") or []
    if not isinstance(warnings, list):
        warnings = []
    payload = {
        "as_of": row.get("as_of") or snapshot.get("as_of"),
        "contract_version": row.get("contract_version") or snapshot.get("contract_version"),
        "market_temperature": _first_safe_float(row.get("market_temperature"), market.get("temperature")),
        "market_state": row.get("market_state") or market.get("state"),
        "stock_count": _first_safe_int(0, row.get("stock_count"), market.get("stock_count")),
        "industry_count": _first_safe_int(0, row.get("industry_count"), quality.get("industry_count")),
        "quality_status": row.get("quality_status") or quality.get("status"),
        "warnings": warnings,
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
    }
    if include_snapshot:
        payload["snapshot"] = snapshot
    return payload


def _safe_text(value: Any) -> str:
    return str(value or "").strip()


def _normalize_match_token(value: Any) -> str:
    return _safe_text(value).lower()


def _industry_matches(industry_row: dict[str, Any], query: str, *, match_mode: str) -> bool:
    needle = _normalize_match_token(query)
    if not needle:
        return True
    tokens = [
        _normalize_match_token(industry_row.get("code")),
        _normalize_match_token(industry_row.get("name")),
        _normalize_match_token(industry_row.get("industry")),
        _normalize_match_token(industry_row.get("industry_name")),
    ]
    if match_mode == "contains":
        return any(needle in token for token in tokens if token)
    return any(needle == token for token in tokens if token)


def _compact_industry_history_row(
    cache_row: dict[str, Any],
    industry_row: dict[str, Any],
    *,
    include_source_chain: bool = False,
) -> dict[str, Any]:
    snapshot = cache_row.get("snapshot") if isinstance(cache_row.get("snapshot"), dict) else {}
    market = snapshot.get("market") if isinstance(snapshot.get("market"), dict) else {}
    quality = snapshot.get("quality") if isinstance(snapshot.get("quality"), dict) else {}
    warnings = cache_row.get("warnings") or quality.get("warnings") or []
    if not isinstance(warnings, list):
        warnings = []
    payload = {
        "as_of": cache_row.get("as_of") or snapshot.get("as_of") or industry_row.get("date"),
        "code": industry_row.get("code") or industry_row.get("industry_code") or "",
        "name": industry_row.get("name") or industry_row.get("industry") or "",
        "temperature": _safe_float(industry_row.get("temperature")),
        "state": industry_row.get("state"),
        "ma20_breadth": _safe_float(industry_row.get("ma20_breadth")),
        "advance_count": _safe_int(industry_row.get("advance_count")),
        "decline_count": _safe_int(industry_row.get("decline_count")),
        "flat_count": _safe_int(industry_row.get("flat_count")),
        "stock_count": _safe_int(industry_row.get("stock_count")),
        "market_cap_weight": _safe_float(industry_row.get("market_cap_weight")),
        "market_temperature": _first_safe_float(cache_row.get("market_temperature"), market.get("temperature")),
        "market_state": cache_row.get("market_state") or market.get("state"),
        "quality_status": cache_row.get("quality_status") or quality.get("status"),
        "warnings": warnings,
        "updated_at": cache_row.get("updated_at"),
    }
    if include_source_chain:
        payload["source_chain"] = [
            _safe_text(item)
            for item in list(snapshot.get("source_chain") or [])
            if _safe_text(item)
        ]
    return payload


def _stock_industry_matches(stock_row: dict[str, Any], query: str, *, match_mode: str) -> bool:
    needle = _normalize_match_token(query)
    if not needle:
        return False
    tokens = [
        _normalize_match_token(stock_row.get("industry_code")),
        _normalize_match_token(stock_row.get("sw_industry_code")),
        _normalize_match_token(stock_row.get("industry")),
        _normalize_match_token(stock_row.get("sector")),
        _normalize_match_token(stock_row.get("industry_name")),
        _normalize_match_token(stock_row.get("sw_industry")),
    ]
    if match_mode == "exact":
        return any(needle == token for token in tokens if token)
    return any(needle in token for token in tokens if token)


def _compact_industry_constituent_row(
    stock_row: dict[str, Any],
    *,
    include_source_chain: bool = False,
) -> dict[str, Any]:
    payload = {
        "code": _safe_text(stock_row.get("code")),
        "name": stock_row.get("name") or stock_row.get("stock_name") or _safe_text(stock_row.get("code")),
        "industry_code": stock_row.get("industry_code") or stock_row.get("sw_industry_code"),
        "industry": stock_row.get("industry") or stock_row.get("sector") or "",
        "sector": stock_row.get("sector") or stock_row.get("industry") or "",
        "market": stock_row.get("market"),
        "market_cap": _safe_float(stock_row.get("market_cap")),
        "pe_ratio": _safe_float(stock_row.get("pe_ratio")),
        "pb_ratio": _safe_float(stock_row.get("pb_ratio")),
        "list_date": stock_row.get("list_date"),
    }
    if include_source_chain:
        payload["source_chain"] = ["db.stocks"]
    return payload


def _parse_forward_horizons(value: Any) -> list[int]:
    raw_items = value
    if raw_items in (None, "", []):
        raw_items = [1, 3, 5]
    if isinstance(raw_items, str):
        raw_items = raw_items.replace(";", ",").split(",")
    if not isinstance(raw_items, (list, tuple, set)):
        raw_items = [raw_items]
    horizons: list[int] = []
    seen: set[int] = set()
    for item in raw_items:
        try:
            parsed = float(item)
        except (TypeError, ValueError):
            continue
        if not math.isfinite(parsed):
            continue
        horizon = int(parsed)
        horizon = max(1, min(horizon, 20))
        if horizon in seen:
            continue
        seen.add(horizon)
        horizons.append(horizon)
    return horizons or [1, 3, 5]


def _market_snapshot_from_cache_row(row: dict[str, Any]) -> dict[str, Any]:
    snapshot = row.get("snapshot") if isinstance(row.get("snapshot"), dict) else {}
    market = snapshot.get("market") if isinstance(snapshot.get("market"), dict) else {}
    quality = snapshot.get("quality") if isinstance(snapshot.get("quality"), dict) else {}
    return {
        "as_of": row.get("as_of") or snapshot.get("as_of"),
        "temperature": _first_safe_float(row.get("market_temperature"), market.get("temperature")),
        "state": row.get("market_state") or market.get("state") or "unknown",
        "avg_pct_change": _safe_float(market.get("avg_pct_change")),
        "weighted_pct_change": _safe_float(market.get("weighted_pct_change")),
        "quality_status": row.get("quality_status") or quality.get("status"),
        "warnings": row.get("warnings") or quality.get("warnings") or [],
    }


def _forward_target_value(row: dict[str, Any], target_field: str) -> float | None:
    if target_field == "avg_pct_change":
        return _safe_float(row.get("avg_pct_change"))
    if target_field == "temperature_delta":
        return _safe_float(row.get("temperature"))
    return _safe_float(row.get("weighted_pct_change"))


def _forward_window_return(
    rows: list[dict[str, Any]],
    start_index: int,
    horizon: int,
    *,
    target_field: str,
) -> float | None:
    if target_field == "temperature_delta":
        current = _safe_float(rows[start_index].get("temperature"))
        future_index = start_index + horizon
        if current is None or future_index >= len(rows):
            return None
        future = _safe_float(rows[future_index].get("temperature"))
        if future is None:
            return None
        return future - current

    values: list[float] = []
    for row in rows[start_index + 1:start_index + horizon + 1]:
        value = _forward_target_value(row, target_field)
        if value is not None:
            values.append(value)
    if len(values) < horizon:
        return None
    return sum(values)


def _parse_trade_date(value: Any) -> datetime | None:
    text = _safe_text(value)[:10]
    if not text:
        return None
    try:
        return datetime.strptime(text, "%Y-%m-%d")
    except ValueError:
        return None


def _date_plus_days(value: Any, days: int) -> str | None:
    parsed = _parse_trade_date(value)
    if parsed is None:
        return None
    return (parsed + timedelta(days=max(1, int(days or 1)))).strftime("%Y-%m-%d")


def _compact_benchmark_bar(row: dict[str, Any]) -> dict[str, Any] | None:
    date_text = _safe_text(row.get("date") or row.get("time"))[:10]
    close = _safe_float(row.get("close"))
    if not date_text or close is None or close <= 0:
        return None
    return {"date": date_text, "close": close}


async def _load_benchmark_bars(
    db: Any,
    *,
    benchmark_code: str,
    start_date: str | None,
    end_date: str | None,
) -> list[dict[str, Any]]:
    code = _safe_text(benchmark_code) or "000300"
    readers = [getattr(db, "get_index_klines", None), getattr(db, "get_klines", None)]
    for reader in readers:
        if not callable(reader):
            continue
        try:
            rows = await reader(code, start_date=start_date, end_date=end_date)
        except TypeError:
            try:
                rows = await reader(code, limit=500)
            except TypeError:
                rows = await reader(code)
        except Exception:
            continue
        bars = []
        for raw_row in list(rows or []):
            try:
                row = dict(raw_row or {})
            except (TypeError, ValueError):
                continue
            bar = _compact_benchmark_bar(row)
            if bar is not None:
                bars.append(bar)
        if bars:
            bars.sort(key=lambda item: str(item.get("date") or ""))
            return bars
    return []


def _benchmark_forward_return(
    bars: list[dict[str, Any]],
    as_of: Any,
    horizon: int,
) -> tuple[float | None, dict[str, Any]]:
    date_text = _safe_text(as_of)[:10]
    if not date_text or not bars:
        return None, {}
    start_index = next(
        (index for index, bar in enumerate(bars) if str(bar.get("date") or "") >= date_text),
        None,
    )
    if start_index is None:
        return None, {}
    future_index = start_index + max(1, int(horizon or 1))
    if future_index >= len(bars):
        return None, {}
    current = _safe_float(bars[start_index].get("close"))
    future = _safe_float(bars[future_index].get("close"))
    if current is None or future is None or current <= 0:
        return None, {}
    return (future / current - 1.0) * 100.0, {
        "benchmark_as_of": bars[start_index].get("date"),
        "benchmark_forward_date": bars[future_index].get("date"),
        "benchmark_close": current,
        "benchmark_forward_close": future,
    }


def _state_direction_hit(state: str, forward_value: float, *, neutral_band_pct: float) -> bool:
    normalized = str(state or "unknown").strip().lower()
    if normalized in {"hot", "warm"}:
        return forward_value > 0
    if normalized in {"cold", "cool"}:
        return forward_value < 0
    if normalized == "neutral":
        return abs(forward_value) <= neutral_band_pct
    return False


def _summarize_forward_values(values: list[float], hits: list[bool], *, min_samples: int) -> dict[str, Any]:
    safe_values = [value for value in values if math.isfinite(value)]
    safe_hits = hits[: len(safe_values)]
    sample_n = len(safe_values)
    payload: dict[str, Any] = {
        "sample_n": sample_n,
        "direction_hits": sum(1 for item in safe_hits if item),
        "reliable": sample_n >= min_samples,
    }
    if sample_n:
        payload["avg_forward_return"] = sum(safe_values) / sample_n
        payload["hit_rate"] = payload["direction_hits"] / sample_n
        payload["min_forward_return"] = min(safe_values)
        payload["max_forward_return"] = max(safe_values)
    else:
        payload["avg_forward_return"] = None
        payload["hit_rate"] = None
        payload["min_forward_return"] = None
        payload["max_forward_return"] = None
    return payload


async def _cached_snapshot(db: Any, *, as_of: str | None, top_n: int) -> dict[str, Any] | None:
    reader = getattr(db, "get_market_temperature_snapshot_cache", None)
    if not callable(reader):
        return None
    cached = await reader(as_of)
    if not cached:
        return None
    snapshot = cached.get("snapshot") if isinstance(cached, dict) else None
    if not isinstance(snapshot, dict) or not snapshot:
        return None
    payload = _snapshot_with_top_n(snapshot, top_n)
    source_chain = [
        "market_temperature_snapshots",
        *[str(item) for item in list(payload.get("source_chain") or []) if str(item).strip()],
    ]
    payload["source_chain"] = source_chain
    quality = dict(payload.get("quality") or {})
    quality["cache_status"] = "hit"
    quality["cache_updated_at"] = cached.get("updated_at")
    payload["quality"] = quality
    payload["cache"] = {
        "status": "hit",
        "as_of": cached.get("as_of") or payload.get("as_of"),
        "updated_at": cached.get("updated_at"),
        "created_at": cached.get("created_at"),
        "source": "market_temperature_snapshots",
    }
    return payload


async def _compute_snapshot_from_db(
    db: Any,
    *,
    limit: int,
    top_n: int,
    as_of: str | None,
    min_bars: int,
    source_chain: list[str],
) -> tuple[dict[str, Any], bool]:
    stock_rows, load_quality = await _load_stock_rows_from_db(
        db,
        limit=limit,
        as_of=as_of,
        min_bars=min_bars,
    )
    snapshot = build_market_temperature_snapshot(
        stock_rows,
        as_of=as_of,
        top_n=top_n,
    )
    quality = dict(snapshot.get("quality") or {})
    quality.update(load_quality)
    degraded = quality.get("status") != "healthy" or bool(load_quality.get("missing_kline_rows"))
    if load_quality.get("universe_count") and load_quality.get("loaded_stock_rows") < load_quality.get("universe_count"):
        quality.setdefault("warnings", [])
        if "partial_kline_coverage" not in quality["warnings"]:
            quality["warnings"].append("partial_kline_coverage")
        quality["status"] = "degraded"
        degraded = True
    snapshot["quality"] = quality
    snapshot["source_chain"] = source_chain
    return snapshot, degraded
