"""Read-only market temperature MCP tool."""

from __future__ import annotations

import asyncio
import math
import time
from datetime import datetime, timedelta
from typing import Any

from ...services.market_temperature import build_market_temperature_snapshot
from ...storage import get_db
from ..manager_protocol import ERR_INTERNAL, ERR_PARAM, fail_with_meta, ok_with_meta

TOOL_NAME = "get_market_temperature_snapshot"
REFRESH_TOOL_NAME = "refresh_market_temperature_snapshot_cache"
LIST_CACHE_TOOL_NAME = "list_market_temperature_snapshot_cache"
INDUSTRY_HISTORY_TOOL_NAME = "list_market_temperature_industry_history"
INDUSTRY_CONSTITUENTS_TOOL_NAME = "list_market_temperature_industry_constituents"
FORWARD_VALIDATION_TOOL_NAME = "get_market_temperature_forward_validation"

from .helpers import (
    _benchmark_forward_return,
    _bounded_top_n,
    _build_stock_row,
    _cached_snapshot,
    _compact_benchmark_bar,
    _compact_cache_row,
    _compact_industry_constituent_row,
    _compact_industry_history_row,
    _compute_snapshot_from_db,
    _date_plus_days,
    _first_present,
    _first_safe_float,
    _first_safe_int,
    _forward_target_value,
    _forward_window_return,
    _industry_matches,
    _load_benchmark_bars,
    _load_stock_rows_from_db,
    _market_snapshot_from_cache_row,
    _normalize_match_token,
    _parse_forward_horizons,
    _parse_trade_date,
    _pct_change,
    _safe_float,
    _safe_int,
    _safe_text,
    _snapshot_with_top_n,
    _sort_industries_by_temperature,
    _state_direction_hit,
    _stock_industry_matches,
    _summarize_forward_values,
)

async def get_market_temperature_snapshot(
    limit: int = 300,
    top_n: int = 8,
    as_of: str | None = None,
    min_bars: int = 20,
    use_cache: bool = True,
) -> dict[str, Any]:
    """Build a read-only market temperature snapshot from local SQLite data.

    The tool deliberately caps the default universe so it is safe for ad-hoc MCP
    calls. A full-market production path should persist daily snapshots during
    data sync and serve them directly.
    """

    started_at = time.perf_counter()
    source_chain = ["db.stocks", "db.kline_1d", "market_temperature.service"]
    try:
        db = get_db()
        if use_cache:
            cached_snapshot = await _cached_snapshot(db, as_of=as_of, top_n=top_n)
            if cached_snapshot is not None:
                quality = dict(cached_snapshot.get("quality") or {})
                return ok_with_meta(
                    cached_snapshot,
                    tool_name=TOOL_NAME,
                    action="snapshot",
                    started_at=started_at,
                    source_chain=cached_snapshot.get("source_chain") or ["market_temperature_snapshots"],
                    data_timestamp=cached_snapshot.get("as_of"),
                    cached=True,
                    extra_meta={
                        "degraded": quality.get("status") != "healthy",
                        "quality": quality,
                        "side_effect": {
                            "level": "read_only",
                            "target": "market_temperature_snapshot",
                            "confirmation_required": False,
                            "idempotent": True,
                        },
                    },
                )

        snapshot, degraded = await _compute_snapshot_from_db(
            db,
            limit=limit,
            top_n=top_n,
            as_of=as_of,
            min_bars=min_bars,
            source_chain=source_chain,
        )
        quality = dict(snapshot.get("quality") or {})
        return ok_with_meta(
            snapshot,
            tool_name=TOOL_NAME,
            action="snapshot",
            started_at=started_at,
            source_chain=source_chain,
            data_timestamp=snapshot.get("as_of"),
            extra_meta={
                "degraded": degraded,
                "quality": quality,
                "side_effect": {
                    "level": "read_only",
                    "target": "market_temperature_snapshot",
                    "confirmation_required": False,
                    "idempotent": True,
                },
            },
        )
    except Exception as exc:
        return fail_with_meta(
            str(exc),
            tool_name=TOOL_NAME,
            action="snapshot",
            started_at=started_at,
            source_chain=source_chain,
            error_code=ERR_INTERNAL,
            extra_meta={
                "degraded": True,
                "quality": {
                    "status": "failed",
                    "quality_flags": ["market_temperature_snapshot_failed"],
                },
            },
        )


async def refresh_market_temperature_snapshot_cache(
    limit: int = 1000,
    top_n: int = 20,
    as_of: str | None = None,
    min_bars: int = 20,
) -> dict[str, Any]:
    """Refresh the durable local market-temperature cache.

    This tool is explicit local-state mutation for data-sync jobs. The Agent
    finance-safe facade intentionally exposes only the read-only snapshot tool.
    """

    started_at = time.perf_counter()
    source_chain = ["db.stocks", "db.kline_1d", "market_temperature.service", "market_temperature_snapshots"]
    try:
        db = get_db()
        snapshot, degraded = await _compute_snapshot_from_db(
            db,
            limit=limit,
            top_n=top_n,
            as_of=as_of,
            min_bars=min_bars,
            source_chain=source_chain[:-1],
        )
        writer = getattr(db, "save_market_temperature_snapshot", None)
        if not callable(writer):
            raise RuntimeError("market temperature snapshot storage is unavailable")
        cached = await writer(
            snapshot,
            request={"limit": limit, "top_n": top_n, "as_of": as_of, "min_bars": min_bars},
            source_chain=source_chain,
        )
        saved_snapshot = dict(cached.get("snapshot") or snapshot)
        saved_snapshot["cache"] = {
            "status": "written",
            "as_of": cached.get("as_of") or saved_snapshot.get("as_of"),
            "updated_at": cached.get("updated_at"),
            "created_at": cached.get("created_at"),
            "source": "market_temperature_snapshots",
        }
        saved_snapshot["source_chain"] = source_chain
        quality = dict(saved_snapshot.get("quality") or {})
        quality["cache_status"] = "written"
        quality["cache_updated_at"] = cached.get("updated_at")
        saved_snapshot["quality"] = quality
        return ok_with_meta(
            saved_snapshot,
            tool_name=REFRESH_TOOL_NAME,
            action="refresh_cache",
            started_at=started_at,
            source_chain=source_chain,
            data_timestamp=saved_snapshot.get("as_of"),
            extra_meta={
                "degraded": degraded,
                "quality": quality,
                "side_effect": {
                    "level": "local_state",
                    "target": "market_temperature_snapshots",
                    "confirmation_required": False,
                    "idempotent": True,
                },
            },
        )
    except Exception as exc:
        return fail_with_meta(
            str(exc),
            tool_name=REFRESH_TOOL_NAME,
            action="refresh_cache",
            started_at=started_at,
            source_chain=source_chain,
            error_code=ERR_INTERNAL,
            extra_meta={
                "degraded": True,
                "quality": {
                    "status": "failed",
                    "quality_flags": ["market_temperature_snapshot_cache_refresh_failed"],
                },
                "side_effect": {
                    "level": "local_state",
                    "target": "market_temperature_snapshots",
                    "confirmation_required": False,
                    "idempotent": True,
                },
            },
        )


async def list_market_temperature_snapshot_cache(
    limit: int = 30,
    include_snapshot: bool = False,
) -> dict[str, Any]:
    """List compact metadata for durable market-temperature cache entries."""

    started_at = time.perf_counter()
    source_chain = ["market_temperature_snapshots"]
    try:
        db = get_db()
        reader = getattr(db, "list_market_temperature_snapshot_cache", None)
        if not callable(reader):
            raise RuntimeError("market temperature snapshot storage is unavailable")
        safe_limit = max(1, min(int(limit or 30), 365))
        rows = await reader(safe_limit)
        items = [
            _compact_cache_row(dict(row or {}), include_snapshot=bool(include_snapshot))
            for row in list(rows or [])
        ]
        payload = {
            "items": items,
            "count": len(items),
            "limit": safe_limit,
            "include_snapshot": bool(include_snapshot),
            "source_chain": source_chain,
        }
        return ok_with_meta(
            payload,
            tool_name=LIST_CACHE_TOOL_NAME,
            action="list_cache",
            started_at=started_at,
            source_chain=source_chain,
            data_timestamp=items[0].get("as_of") if items else None,
            cached=True,
            extra_meta={
                "degraded": False,
                "quality": {
                    "status": "available" if items else "empty",
                    "row_count": len(items),
                    "cache_status": "listed",
                },
                "side_effect": {
                    "level": "read_only",
                    "target": "market_temperature_snapshots",
                    "confirmation_required": False,
                    "idempotent": True,
                },
            },
        )
    except Exception as exc:
        return fail_with_meta(
            str(exc),
            tool_name=LIST_CACHE_TOOL_NAME,
            action="list_cache",
            started_at=started_at,
            source_chain=source_chain,
            error_code=ERR_INTERNAL,
            extra_meta={
                "degraded": True,
                "quality": {
                    "status": "failed",
                    "quality_flags": ["market_temperature_snapshot_cache_list_failed"],
                },
                "side_effect": {
                    "level": "read_only",
                    "target": "market_temperature_snapshots",
                    "confirmation_required": False,
                    "idempotent": True,
                },
            },
        )


async def list_market_temperature_industry_history(
    industry: str | None = None,
    limit: int = 120,
    top_n: int = 10,
    match_mode: str = "exact",
    include_source_chain: bool = False,
) -> dict[str, Any]:
    """List point-in-time industry temperature history from durable cache entries."""

    started_at = time.perf_counter()
    source_chain = ["market_temperature_snapshots", "market_temperature.industry_history"]
    try:
        db = get_db()
        reader = getattr(db, "list_market_temperature_snapshot_cache", None)
        if not callable(reader):
            raise RuntimeError("market temperature snapshot storage is unavailable")
        safe_limit = max(1, min(int(limit or 120), 365))
        safe_top_n = max(1, min(int(top_n or 10), 50))
        normalized_match_mode = str(match_mode or "exact").strip().lower()
        if normalized_match_mode not in {"exact", "contains"}:
            normalized_match_mode = "exact"
        query = _safe_text(industry)
        rows = await reader(safe_limit)
        items: list[dict[str, Any]] = []
        for raw_row in list(rows or []):
            row = dict(raw_row or {})
            snapshot = row.get("snapshot") if isinstance(row.get("snapshot"), dict) else {}
            industries = [
                dict(item or {})
                for item in list(snapshot.get("industries") or [])
                if isinstance(item, dict)
            ]
            matches = [
                item
                for item in industries
                if _industry_matches(item, query, match_mode=normalized_match_mode)
            ]
            if not query:
                matches = _sort_industries_by_temperature(matches, reverse=True)[:safe_top_n]
            for industry_row in matches:
                items.append(
                    _compact_industry_history_row(
                        row,
                        industry_row,
                        include_source_chain=bool(include_source_chain),
                    )
                )
        items.sort(key=lambda item: (str(item.get("as_of") or ""), str(item.get("name") or "")))
        payload = {
            "items": items,
            "count": len(items),
            "limit": safe_limit,
            "top_n": safe_top_n,
            "industry": query or None,
            "match_mode": normalized_match_mode,
            "include_source_chain": bool(include_source_chain),
            "source_chain": source_chain,
        }
        return ok_with_meta(
            payload,
            tool_name=INDUSTRY_HISTORY_TOOL_NAME,
            action="industry_history",
            started_at=started_at,
            source_chain=source_chain,
            data_timestamp=items[-1].get("as_of") if items else None,
            cached=True,
            extra_meta={
                "degraded": False,
                "quality": {
                    "status": "available" if items else "empty",
                    "row_count": len(items),
                    "cache_status": "listed",
                    "industry": query or None,
                },
                "side_effect": {
                    "level": "read_only",
                    "target": "market_temperature_snapshots",
                    "confirmation_required": False,
                    "idempotent": True,
                },
            },
        )
    except Exception as exc:
        return fail_with_meta(
            str(exc),
            tool_name=INDUSTRY_HISTORY_TOOL_NAME,
            action="industry_history",
            started_at=started_at,
            source_chain=source_chain,
            error_code=ERR_INTERNAL,
            extra_meta={
                "degraded": True,
                "quality": {
                    "status": "failed",
                    "quality_flags": ["market_temperature_industry_history_failed"],
                },
                "side_effect": {
                    "level": "read_only",
                    "target": "market_temperature_snapshots",
                    "confirmation_required": False,
                    "idempotent": True,
                },
            },
        )


async def list_market_temperature_industry_constituents(
    industry: str,
    limit: int = 200,
    offset: int = 0,
    match_mode: str = "contains",
    include_source_chain: bool = False,
) -> dict[str, Any]:
    """List local stock-universe constituents for one market-temperature industry."""

    started_at = time.perf_counter()
    source_chain = ["db.stocks", "market_temperature.industry_constituents"]
    query = _safe_text(industry)
    safe_limit = max(1, min(int(limit or 200), 1000))
    safe_offset = max(0, min(int(offset or 0), 10000))
    normalized_match_mode = str(match_mode or "contains").strip().lower()
    if normalized_match_mode not in {"exact", "contains"}:
        normalized_match_mode = "contains"
    side_effect = {
        "level": "read_only",
        "target": "stocks",
        "confirmation_required": False,
        "idempotent": True,
    }

    if not query:
        return fail_with_meta(
            "industry is required",
            tool_name=INDUSTRY_CONSTITUENTS_TOOL_NAME,
            action="industry_constituents",
            started_at=started_at,
            source_chain=source_chain,
            error_code=ERR_PARAM,
            extra_meta={
                "degraded": True,
                "quality": {
                    "status": "failed",
                    "quality_flags": ["market_temperature_industry_required"],
                },
                "side_effect": side_effect,
            },
        )

    try:
        db = get_db()
        reader = getattr(db, "list_stock_universe", None)
        if not callable(reader):
            raise RuntimeError("stock universe storage is unavailable")

        fetch_limit = min(10000, max(safe_offset + safe_limit * 5, safe_limit))
        try:
            rows = await reader(limit=fetch_limit, offset=0, industry=query)
        except TypeError:
            try:
                rows = await reader(limit=fetch_limit, offset=0)
            except TypeError:
                rows = await reader(limit=fetch_limit)

        candidates: list[dict[str, Any]] = []
        for raw_row in list(rows or []):
            try:
                candidates.append(dict(raw_row or {}))
            except (TypeError, ValueError):
                continue
        matches = [
            row
            for row in candidates
            if _stock_industry_matches(row, query, match_mode=normalized_match_mode)
        ]
        page = matches[safe_offset:safe_offset + safe_limit]
        items = [
            _compact_industry_constituent_row(
                row,
                include_source_chain=bool(include_source_chain),
            )
            for row in page
        ]
        payload = {
            "items": items,
            "count": len(items),
            "total_matches": len(matches),
            "limit": safe_limit,
            "offset": safe_offset,
            "industry": query,
            "match_mode": normalized_match_mode,
            "include_source_chain": bool(include_source_chain),
            "source_chain": source_chain,
        }
        return ok_with_meta(
            payload,
            tool_name=INDUSTRY_CONSTITUENTS_TOOL_NAME,
            action="industry_constituents",
            started_at=started_at,
            source_chain=source_chain,
            cached=True,
            extra_meta={
                "degraded": False,
                "quality": {
                    "status": "available" if items else "empty",
                    "row_count": len(items),
                    "total_matches": len(matches),
                    "industry": query,
                },
                "side_effect": side_effect,
            },
        )
    except Exception as exc:
        return fail_with_meta(
            str(exc),
            tool_name=INDUSTRY_CONSTITUENTS_TOOL_NAME,
            action="industry_constituents",
            started_at=started_at,
            source_chain=source_chain,
            error_code=ERR_INTERNAL,
            extra_meta={
                "degraded": True,
                "quality": {
                    "status": "failed",
                    "quality_flags": ["market_temperature_industry_constituents_failed"],
                },
                "side_effect": side_effect,
            },
        )


async def get_market_temperature_forward_validation(
    limit: int = 180,
    horizons: list[int] | str | int | None = None,
    target_field: str = "weighted_pct_change",
    benchmark_code: str | None = None,
    min_samples: int = 3,
    neutral_band_pct: float = 0.2,
    include_samples: bool = False,
) -> dict[str, Any]:
    """Build a read-only PIT forward-validation matrix from cached market-temperature snapshots."""

    started_at = time.perf_counter()
    source_chain = ["market_temperature_snapshots", "market_temperature.forward_validation"]
    side_effect = {
        "level": "read_only",
        "target": "market_temperature_snapshots",
        "confirmation_required": False,
        "idempotent": True,
    }
    try:
        db = get_db()
        reader = getattr(db, "list_market_temperature_snapshot_cache", None)
        if not callable(reader):
            raise RuntimeError("market temperature snapshot storage is unavailable")
        safe_limit = max(2, min(int(limit or 180), 365))
        safe_horizons = _parse_forward_horizons(horizons)
        requested_target = str(target_field or "weighted_pct_change").strip().lower() or "weighted_pct_change"
        normalized_target = requested_target
        quality_warnings: list[str] = []
        if normalized_target not in {"weighted_pct_change", "avg_pct_change", "temperature_delta", "benchmark_return"}:
            normalized_target = "weighted_pct_change"
            quality_warnings.append("unsupported_target_field_fallback_to_weighted_pct_change")
        safe_benchmark_code = _safe_text(benchmark_code) or ("000300" if normalized_target == "benchmark_return" else "")
        safe_min_samples = max(1, min(int(min_samples or 3), 100))
        safe_neutral_band = max(0.0, min(_safe_float(neutral_band_pct) or 0.0, 5.0))
        rows = await reader(min(365, safe_limit + max(safe_horizons)))
        market_rows = [
            _market_snapshot_from_cache_row(dict(row or {}))
            for row in list(rows or [])
        ]
        market_rows = [
            row
            for row in market_rows
            if _safe_text(row.get("as_of")) and _safe_float(row.get("temperature")) is not None
        ]
        market_rows.sort(key=lambda item: str(item.get("as_of") or ""))
        source_chain_for_response = list(source_chain)
        benchmark_bars: list[dict[str, Any]] = []
        benchmark_status = "not_requested"
        if normalized_target == "benchmark_return":
            start_date = str(market_rows[0].get("as_of") or "") if market_rows else None
            end_date = _date_plus_days(
                market_rows[-1].get("as_of") if market_rows else None,
                max(safe_horizons) * 7 + 14,
            )
            benchmark_bars = await _load_benchmark_bars(
                db,
                benchmark_code=safe_benchmark_code,
                start_date=start_date,
                end_date=end_date,
            )
            source_chain_for_response.append("db.kline_1d")
            if benchmark_bars:
                benchmark_status = "available"
            else:
                benchmark_status = "unavailable_fallback_to_weighted_pct_change"
                quality_warnings.append("benchmark_kline_unavailable")
                normalized_target = "weighted_pct_change"

        buckets: dict[str, dict[int, dict[str, list[Any]]]] = {}
        samples: list[dict[str, Any]] = []
        for index, row in enumerate(market_rows):
            state = str(row.get("state") or "unknown").strip().lower() or "unknown"
            for horizon in safe_horizons:
                sample_extra: dict[str, Any] = {}
                if normalized_target == "benchmark_return":
                    forward_value, sample_extra = _benchmark_forward_return(
                        benchmark_bars,
                        row.get("as_of"),
                        horizon,
                    )
                else:
                    forward_value = _forward_window_return(
                        market_rows,
                        index,
                        horizon,
                        target_field=normalized_target,
                    )
                if forward_value is None:
                    continue
                hit = _state_direction_hit(state, forward_value, neutral_band_pct=safe_neutral_band)
                bucket = buckets.setdefault(state, {}).setdefault(horizon, {"values": [], "hits": []})
                bucket["values"].append(forward_value)
                bucket["hits"].append(hit)
                if include_samples:
                    samples.append(
                        {
                            "as_of": row.get("as_of"),
                            "state": state,
                            "temperature": row.get("temperature"),
                            "horizon": horizon,
                            "forward_return": forward_value,
                            "direction_hit": hit,
                            "target_field": normalized_target,
                            **sample_extra,
                        }
                    )

        matrix: dict[str, dict[str, Any]] = {}
        for state, horizon_map in sorted(buckets.items()):
            matrix[state] = {}
            for horizon in safe_horizons:
                cell = horizon_map.get(horizon, {"values": [], "hits": []})
                matrix[state][f"{horizon}d"] = _summarize_forward_values(
                    [
                        value
                        for item in list(cell.get("values") or [])
                        if (value := _safe_float(item)) is not None
                    ],
                    [bool(item) for item in list(cell.get("hits") or [])],
                    min_samples=safe_min_samples,
                )

        sample_count = sum(
            int(cell.get("sample_n") or 0)
            for state_cells in matrix.values()
            for cell in state_cells.values()
        )
        payload = {
            "matrix": matrix,
            "states": sorted(matrix.keys()),
            "horizons": safe_horizons,
            "count": sample_count,
            "snapshot_count": len(market_rows),
            "limit": safe_limit,
            "target_field": normalized_target,
            "requested_target_field": requested_target,
            "benchmark_code": safe_benchmark_code or None,
            "benchmark_status": benchmark_status,
            "benchmark_bar_count": len(benchmark_bars),
            "min_samples": safe_min_samples,
            "neutral_band_pct": safe_neutral_band,
            "include_samples": bool(include_samples),
            "samples": samples[:500] if include_samples else [],
            "source_chain": source_chain_for_response,
        }
        return ok_with_meta(
            payload,
            tool_name=FORWARD_VALIDATION_TOOL_NAME,
            action="forward_validation",
            started_at=started_at,
            source_chain=source_chain_for_response,
            data_timestamp=market_rows[-1].get("as_of") if market_rows else None,
            cached=True,
            extra_meta={
                "degraded": len(market_rows) < max(safe_horizons) + safe_min_samples or bool(quality_warnings),
                "quality": {
                    "status": "available" if sample_count else "empty",
                    "row_count": sample_count,
                    "snapshot_count": len(market_rows),
                    "target_field": normalized_target,
                    "requested_target_field": requested_target,
                    "benchmark_status": benchmark_status,
                    "warnings": quality_warnings,
                },
                "side_effect": side_effect,
            },
        )
    except Exception as exc:
        return fail_with_meta(
            str(exc),
            tool_name=FORWARD_VALIDATION_TOOL_NAME,
            action="forward_validation",
            started_at=started_at,
            source_chain=source_chain,
            error_code=ERR_INTERNAL,
            extra_meta={
                "degraded": True,
                "quality": {
                    "status": "failed",
                    "quality_flags": ["market_temperature_forward_validation_failed"],
                },
                "side_effect": side_effect,
            },
        )


def register(mcp) -> None:
    mcp.tool()(get_market_temperature_snapshot)
    mcp.tool()(refresh_market_temperature_snapshot_cache)
    mcp.tool()(list_market_temperature_snapshot_cache)
    mcp.tool()(list_market_temperature_industry_history)
    mcp.tool()(list_market_temperature_industry_constituents)
    mcp.tool()(get_market_temperature_forward_validation)
