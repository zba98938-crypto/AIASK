"""数据库 K 线新鲜度检测与自动同步。

核心能力:
  1. ensure_fresh_klines — 确保返回的 K 线数据足够新鲜,过期时自动从 API 获取并回写数据库
  2. check_freshness     — 批量检查数据库中多只股票的 K 线新鲜度
  3. sync_stale           — 批量同步过期 K 线数据
"""

from __future__ import annotations

import asyncio
import logging
import math
from datetime import datetime, timedelta
from typing import Any

from ..storage import get_db
from ..utils import fail, ok, validate_int_range, validate_stock_code_format

logger = logging.getLogger(__name__)

_DEFAULT_STALE_DAYS = 5
_SYNC_CONCURRENCY = 3


def _calc_staleness(klines: list[dict]) -> int:
    """计算 K 线最后一根距今天数。"""
    if not klines:
        return 9999
    last_date = klines[-1].get("date", "")
    if not last_date:
        return 9999
    try:
        last_dt = datetime.strptime(str(last_date)[:10], "%Y-%m-%d")
        return (datetime.now() - last_dt).days
    except (ValueError, TypeError):
        return 9999


def _calc_date_staleness(raw_date: Any) -> int | None:
    if not raw_date:
        return None
    try:
        if isinstance(raw_date, datetime):
            parsed = raw_date
        else:
            date_text = str(raw_date).strip()[:10]
            date_format = "%Y%m%d" if len(date_text) == 8 and date_text.isdigit() else "%Y-%m-%d"
            parsed = datetime.strptime(date_text, date_format)
        return max(0, (datetime.now().date() - parsed.date()).days)
    except (TypeError, ValueError):
        return None


def _row_value(row: Any, key: str, default: Any = None) -> Any:
    if row is None:
        return default
    if isinstance(row, dict):
        return row.get(key, default)
    try:
        return row[key]
    except (KeyError, TypeError, IndexError):
        return getattr(row, key, default)


def _safe_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _safe_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _first_safe_float(*values: Any) -> float | None:
    for value in values:
        parsed = _safe_float(value)
        if parsed is not None:
            return parsed
    return None


def _date_text(value: Any) -> str:
    return str(value or "").strip()[:10]


async def _classify_source_confirmed_stale_entries(
    stale_entries: list[dict],
) -> tuple[list[dict], list[dict], list[dict]]:
    """Split stale bars into blocking and locally source-confirmed nonblocking rows."""
    if not stale_entries:
        return [], [], []

    try:
        from ..data_source.tdx_local import get_tdx_local_source

        tdx_local = get_tdx_local_source()
    except Exception:
        return [], list(stale_entries), []

    nonblocking: list[dict] = []
    blocking: list[dict] = []
    invalid_codes: list[dict] = []

    for entry in stale_entries:
        code = str(entry.get("code") or "").strip()
        lower_code = code.lower()

        # Explicit index storage codes need dedicated index refresh, not stock-path exemption.
        if lower_code.startswith(("sh000", "sz399")):
            blocking.append({**entry, "blocking_reason": "stale_index_kline"})
            continue

        normalized, code_error = validate_stock_code_format(code)
        if code_error:
            invalid_codes.append({"code": code, "error": code_error})
            blocking.append({**entry, "blocking_reason": "invalid_tracked_code"})
            continue

        try:
            source_rows = tdx_local.get_kline(str(normalized), period="daily", limit=1)
        except Exception as exc:
            blocking.append({**entry, "blocking_reason": f"tdx_local_check_failed:{type(exc).__name__}"})
            continue

        source_last_date = _date_text((source_rows or [{}])[-1].get("date") if source_rows else "")
        db_last_date = _date_text(entry.get("last_date"))
        if source_last_date and db_last_date and source_last_date <= db_last_date:
            nonblocking.append({
                **entry,
                "source_last_date": source_last_date,
                "nonblocking_reason": "tdx_local_has_no_newer_bar",
            })
            continue

        blocking.append({
            **entry,
            "source_last_date": source_last_date or None,
            "blocking_reason": "tdx_local_has_newer_or_unknown_bar",
        })

    return nonblocking, blocking, invalid_codes


async def ensure_fresh_klines(
    code: str,
    *,
    limit: int = 250,
    max_stale_days: int = _DEFAULT_STALE_DAYS,
) -> tuple[list[dict], dict]:
    """确保返回新鲜的 K 线数据，过期时自动从 API 获取并回写数据库。

    Returns:
        (klines, freshness_info) 二元组。
        freshness_info 包含 staleness_days, source, synced 等元信息。
    """
    db = get_db()
    klines = await db.get_klines(code, limit=limit)

    info: dict[str, Any] = {
        "code": code,
        "source": "sqlite",
        "synced": False,
        "staleness_days": 0,
        "kline_count": 0,
    }

    if klines:
        klines.sort(key=lambda k: k.get("date", ""))
        stale_days = _calc_staleness(klines)
        info["staleness_days"] = stale_days
        info["kline_count"] = len(klines)
        info["last_date"] = klines[-1].get("date", "")

        if stale_days <= max_stale_days:
            info["freshness"] = "fresh"
            return klines, info

        logger.info(
            "K 线过期 (%d 天 > %d)，code=%s，从 API 重新获取并同步",
            stale_days, max_stale_days, code,
        )

    # 从 API 获取最新数据 — 绕过 get_kline 的缓存和 DB 优先逻辑
    from .market.kline import _get_kline_impl
    from datetime import timezone

    started_at = datetime.now(timezone.utc)
    api_result = await _get_kline_impl(code, "daily", limit, started_at)
    if not api_result.get("success") or not api_result.get("data"):
        if klines:
            info["freshness"] = "stale_api_unavailable"
            info["warning"] = f"API 获取失败，使用过期数据（{info.get('staleness_days', '?')}天）"
            return klines, info
        info["freshness"] = "no_data"
        return [], info

    fresh_klines = api_result["data"]
    fresh_klines.sort(key=lambda k: k.get("date", ""))

    # get_kline 内部已通过 _async_save_klines_to_db 回写数据库
    # 但这里做一次显式同步确保落库成功
    sync_ok = await _sync_to_db(code, fresh_klines)

    info["source"] = api_result.get("source", "api")
    info["synced"] = sync_ok
    info["kline_count"] = len(fresh_klines)
    info["staleness_days"] = _calc_staleness(fresh_klines)
    info["last_date"] = fresh_klines[-1].get("date", "") if fresh_klines else ""
    info["freshness"] = "fresh" if info["staleness_days"] <= max_stale_days else "stale"

    return fresh_klines, info


async def _sync_to_db(code: str, klines: list[dict]) -> bool:
    """将 K 线数据同步写入数据库。"""
    try:
        db = get_db()
        result = await db.save_klines(code, klines)
        accepted = result.get("accepted_count", 0) if isinstance(result, dict) else 0
        logger.info(
            "K 线同步完成: code=%s, accepted=%d/%d",
            code, accepted, len(klines),
        )
        return accepted > 0
    except Exception as e:
        logger.warning("K 线同步失败: code=%s, error=%s", code, e)
        return False


async def check_freshness(
    codes: list[str],
    *,
    max_stale_days: int = _DEFAULT_STALE_DAYS,
    db: Any | None = None,
) -> dict[str, Any]:
    """批量检查多只股票的 K 线新鲜度。

    Returns:
        包含 fresh/stale/missing 分类的报告。
    """
    db = db or get_db()
    fresh_list: list[dict] = []
    stale_list: list[dict] = []
    missing_list: list[str] = []

    for code in codes:
        try:
            klines = await db.get_klines(code, limit=1)
            if not klines:
                missing_list.append(code)
                continue

            stale_days = _calc_staleness(klines)
            entry = {
                "code": code,
                "last_date": klines[-1].get("date", ""),
                "staleness_days": stale_days,
            }

            if stale_days <= max_stale_days:
                fresh_list.append(entry)
            else:
                stale_list.append(entry)
        except Exception as e:
            logger.warning("检查 %s 新鲜度失败: %s", code, e)
            missing_list.append(code)

    return {
        "checked_at": datetime.now().isoformat(),
        "max_stale_days": max_stale_days,
        "total": len(codes),
        "fresh_count": len(fresh_list),
        "stale_count": len(stale_list),
        "missing_count": len(missing_list),
        "fresh": fresh_list,
        "stale": sorted(stale_list, key=lambda x: x["staleness_days"], reverse=True),
        "missing": missing_list,
    }


async def sync_stale(
    codes: list[str] | None = None,
    *,
    max_stale_days: int = _DEFAULT_STALE_DAYS,
    limit_per_stock: int = 250,
) -> dict[str, Any]:
    """批量同步过期 K 线数据到数据库。

    如果 codes 为 None，自动扫描数据库中所有股票进行检查。
    """
    db = get_db()

    if codes is None:
        codes = await _get_all_tracked_codes(db)

    report = await check_freshness(codes, max_stale_days=max_stale_days)
    stale_codes = [s["code"] for s in report.get("stale", [])]
    missing_codes = report.get("missing", [])
    to_sync = stale_codes + missing_codes

    if not to_sync:
        return {
            "checked": len(codes),
            "need_sync": 0,
            "synced": 0,
            "failed": 0,
            "detail": [],
            "message": "所有股票数据均为最新，无需同步",
        }

    sem = asyncio.Semaphore(_SYNC_CONCURRENCY)
    results: list[dict] = []

    async def _sync_one(c: str) -> dict:
        async with sem:
            try:
                _, info = await ensure_fresh_klines(
                    c, limit=limit_per_stock, max_stale_days=max_stale_days,
                )
                return {
                    "code": c,
                    "status": "synced" if info.get("synced") else "api_fetched",
                    "staleness_before": next(
                        (s["staleness_days"] for s in report.get("stale", []) if s["code"] == c),
                        None,
                    ),
                    "staleness_after": info.get("staleness_days"),
                    "kline_count": info.get("kline_count", 0),
                }
            except Exception as e:
                return {"code": c, "status": "failed", "error": str(e)}

    tasks = [_sync_one(c) for c in to_sync]
    results = await asyncio.gather(*tasks)

    synced = sum(1 for r in results if r["status"] in ("synced", "api_fetched"))
    failed = sum(1 for r in results if r["status"] == "failed")

    return {
        "checked": len(codes),
        "need_sync": len(to_sync),
        "synced": synced,
        "failed": failed,
        "detail": results,
        "message": f"同步完成: {synced}/{len(to_sync)} 成功, {failed} 失败",
    }


async def _get_all_tracked_codes(db) -> list[str]:
    """获取数据库中已有的所有股票代码。"""
    try:
        async with db.acquire() as conn:
            rows = await conn.fetch(
                "SELECT DISTINCT code FROM kline_1d ORDER BY code",
            )
            return [row["code"] for row in rows]
    except Exception as e:
        logger.warning("获取已跟踪代码列表失败: %s", e)
        return []


# ── MCP 注册 ──────────────────────────────────────────────────────


async def _load_factor_ic_storage_summary(db: Any) -> dict[str, Any]:
    try:
        async with db.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT
                    MAX(ic_date) as max_date,
                    COUNT(*) as row_count,
                    COUNT(DISTINCT factor_name) as factor_count
                FROM factor_ic_history
                """
            )
        max_date = _row_value(row, "max_date")
        row_count = _safe_int(_row_value(row, "row_count"))
        factor_count = _safe_int(_row_value(row, "factor_count"))
        staleness_days = _calc_date_staleness(max_date)
        return {
            "max_date": str(max_date or "") or None,
            "row_count": row_count,
            "factor_count": factor_count,
            "staleness_days": staleness_days,
            "source": "factor_ic_history",
        }
    except Exception as e:
        logger.warning("妫€鏌?factor_ic_history 鏂伴矞搴﹀け璐? %s", e)
        return {
            "max_date": None,
            "row_count": 0,
            "factor_count": 0,
            "staleness_days": None,
            "source": "factor_ic_history",
            "error": str(e),
        }


async def assess_market_temperature_cache_readiness(
    *,
    as_of: str | None = None,
    max_stale_days: int = 1,
    db: Any | None = None,
) -> dict[str, Any]:
    """Read-only readiness check for the market-temperature snapshot cache."""

    resolved_db = db or get_db()
    reader = getattr(resolved_db, "get_market_temperature_snapshot_cache", None)
    source_chain = [
        "tools.db_freshness.assess_market_temperature_cache_readiness",
        "storage.market_temperature_snapshots",
    ]
    if not callable(reader):
        return {
            "ready": False,
            "status": "unavailable",
            "read_only": True,
            "as_of": as_of,
            "max_stale_days": max_stale_days,
            "staleness_days": None,
            "blockers": ["market_temperature_cache_storage_unavailable"],
            "source_chain": source_chain,
        }

    try:
        cached = await reader(as_of)
    except Exception as exc:
        return {
            "ready": False,
            "status": "failed",
            "read_only": True,
            "as_of": as_of,
            "max_stale_days": max_stale_days,
            "staleness_days": None,
            "blockers": ["market_temperature_cache_read_failed"],
            "error": str(exc),
            "source_chain": source_chain,
        }

    if not cached:
        return {
            "ready": False,
            "status": "missing",
            "read_only": True,
            "as_of": as_of,
            "max_stale_days": max_stale_days,
            "staleness_days": None,
            "blockers": ["market_temperature_cache_missing"],
            "source_chain": source_chain,
        }

    snapshot = _row_value(cached, "snapshot", {}) or {}
    if not isinstance(snapshot, dict):
        snapshot = {}
    quality = snapshot.get("quality") if isinstance(snapshot, dict) else {}
    if not isinstance(quality, dict):
        quality = {}
    cache_as_of = _date_text(_row_value(cached, "as_of") or (snapshot or {}).get("as_of"))
    staleness_days = _calc_date_staleness(cache_as_of)
    quality_status = str(_row_value(cached, "quality_status") or quality.get("status") or "unknown").strip().lower()
    stock_count = _safe_int(_row_value(cached, "stock_count") or (snapshot or {}).get("market", {}).get("stock_count"))
    industry_count = _safe_int(_row_value(cached, "industry_count") or quality.get("industry_count"))
    warnings = _row_value(cached, "warnings") or quality.get("warnings") or []
    warnings = list(warnings) if isinstance(warnings, list) else []

    blockers: list[str] = []
    status = "fresh"
    if staleness_days is None:
        blockers.append("market_temperature_cache_date_invalid")
        status = "invalid"
    elif staleness_days > max_stale_days:
        blockers.append("market_temperature_cache_stale")
        status = "stale"
    if stock_count <= 0:
        blockers.append("market_temperature_cache_empty")
    if quality_status in {"empty", "failed", "unknown"}:
        blockers.append(f"market_temperature_quality_{quality_status}")
    if blockers and status == "fresh":
        status = "not_ready"

    degraded = quality_status == "degraded" or bool(warnings)
    ready = not blockers
    return {
        "ready": ready,
        "status": status if ready or status != "fresh" else "fresh_degraded" if degraded else "fresh",
        "read_only": True,
        "as_of": cache_as_of or as_of,
        "requested_as_of": as_of,
        "max_stale_days": max_stale_days,
        "staleness_days": staleness_days,
        "quality_status": quality_status,
        "degraded": degraded,
        "warnings": warnings,
        "market_temperature": _first_safe_float(
            _row_value(cached, "market_temperature"),
            (snapshot or {}).get("market", {}).get("temperature"),
        ),
        "market_state": _row_value(cached, "market_state") or (snapshot or {}).get("market", {}).get("state"),
        "stock_count": stock_count,
        "industry_count": industry_count,
        "cache": {
            "created_at": _row_value(cached, "created_at"),
            "updated_at": _row_value(cached, "updated_at"),
            "source": "market_temperature_snapshots",
        },
        "blockers": blockers,
        "source_chain": source_chain,
    }


async def assess_p0_1_data_readiness(
    codes: list[str] | None = None,
    *,
    max_stale_days: int = _DEFAULT_STALE_DAYS,
    factor_ic_max_stale_days: int = _DEFAULT_STALE_DAYS,
    max_codes: int = 200,
    db: Any | None = None,
) -> dict[str, Any]:
    """Read-only readiness check for the P0-1 data freshness gate."""
    resolved_db = db or get_db()
    explicit_codes = codes is not None
    tracked_codes = list(codes or [])
    if not explicit_codes:
        tracked_codes = await _get_all_tracked_codes(resolved_db)

    total_tracked_codes = len(tracked_codes)
    selected_codes = list(tracked_codes)
    truncated = False
    if not explicit_codes and max_codes > 0 and len(selected_codes) > max_codes:
        selected_codes = selected_codes[:max_codes]
        truncated = True

    if selected_codes:
        freshness = await check_freshness(
            selected_codes,
            max_stale_days=max_stale_days,
            db=resolved_db,
        )
    else:
        freshness = {
            "checked_at": datetime.now().isoformat(),
            "max_stale_days": max_stale_days,
            "total": 0,
            "fresh_count": 0,
            "stale_count": 0,
            "missing_count": 0,
            "fresh": [],
            "stale": [],
            "missing": [],
        }
    factor_ic = await _load_factor_ic_storage_summary(resolved_db)

    nonblocking_stale, blocking_stale, invalid_tracked_codes = await _classify_source_confirmed_stale_entries(
        list(freshness.get("stale") or [])
    )
    kline_ready = (
        bool(selected_codes)
        and len(blocking_stale) == 0
        and freshness.get("missing_count", 0) == 0
        and len(invalid_tracked_codes) == 0
    )
    factor_ic_staleness = factor_ic.get("staleness_days")
    factor_ic_ready = (
        _safe_int(factor_ic.get("row_count")) > 0
        and isinstance(factor_ic_staleness, int)
        and factor_ic_staleness <= factor_ic_max_stale_days
    )

    blockers: list[str] = []
    if total_tracked_codes == 0:
        blockers.append("no_codes")
    if selected_codes and not kline_ready:
        blockers.append("stale_or_missing_klines")
    if invalid_tracked_codes:
        blockers.append("invalid_tracked_codes")
    if _safe_int(factor_ic.get("row_count")) <= 0:
        blockers.append("factor_ic_missing")
    elif not factor_ic_ready:
        blockers.append("factor_ic_stale")
    if truncated:
        blockers.append("sample_only")

    return {
        "ready": kline_ready and factor_ic_ready and not truncated,
        "kline_ready": kline_ready,
        "factor_ic_ready": factor_ic_ready,
        "blockers": blockers,
        "read_only": True,
        "checked_code_count": len(selected_codes),
        "total_tracked_codes": total_tracked_codes,
        "truncated": truncated,
        "max_codes": max_codes,
        "explicit_codes": explicit_codes,
        "max_stale_days": max_stale_days,
        "factor_ic_max_stale_days": factor_ic_max_stale_days,
        "freshness": freshness,
        "nonblocking_stale": nonblocking_stale,
        "nonblocking_stale_count": len(nonblocking_stale),
        "blocking_stale": blocking_stale,
        "blocking_stale_count": len(blocking_stale),
        "invalid_tracked_codes": invalid_tracked_codes,
        "factor_ic": factor_ic,
        "source_chain": [
            "tools.db_freshness.assess_p0_1_data_readiness",
            "tools.db_freshness.check_freshness",
            "storage.kline_1d",
            "storage.factor_ic_history",
        ],
    }


def register(mcp):
    """注册数据库新鲜度检测工具。"""

    @mcp.tool()
    async def check_db_freshness(
        codes: list[str] | None = None,
        max_stale_days: int = 5,
    ):
        """检查数据库中 K 线数据的新鲜度

        扫描数据库中的股票 K 线数据，报告哪些过期、哪些缺失。

        Args:
            codes: 要检查的股票代码列表。为空时检查数据库中所有股票。
            max_stale_days: 最大允许过期天数（默认5天，即超过5天视为过期）
        """
        max_stale_days, stale_error = validate_int_range(max_stale_days, field_name="max_stale_days", minimum=0)
        if stale_error:
            return fail(stale_error)
        invalid_codes = [code for code in list(codes or []) if validate_stock_code_format(code)[1] is not None]
        if invalid_codes:
            return fail(f"存在无效股票代码: {', '.join(invalid_codes)}")
        if not codes:
            db = get_db()
            codes = await _get_all_tracked_codes(db)
            if not codes:
                return ok({
                    "message": "数据库中暂无 K 线数据",
                    "total": 0,
                })

        result = await check_freshness(codes, max_stale_days=max_stale_days)
        return ok(result)

    @mcp.tool()
    async def check_p0_1_data_readiness(
        codes: list[str] | None = None,
        max_stale_days: int = 5,
        factor_ic_max_stale_days: int = 5,
        max_codes: int = 200,
    ):
        """Read-only P0-1 data readiness check for K-lines and factor IC history."""
        max_stale_days, stale_error = validate_int_range(max_stale_days, field_name="max_stale_days", minimum=0)
        if stale_error:
            return fail(stale_error)
        factor_ic_max_stale_days, factor_error = validate_int_range(
            factor_ic_max_stale_days,
            field_name="factor_ic_max_stale_days",
            minimum=0,
        )
        if factor_error:
            return fail(factor_error)
        max_codes, max_codes_error = validate_int_range(max_codes, field_name="max_codes", minimum=0, maximum=10000)
        if max_codes_error:
            return fail(max_codes_error)

        normalized_codes = None
        if codes is not None:
            raw_codes = [codes] if isinstance(codes, str) else list(codes or [])
            normalized_codes = []
            invalid_codes = []
            for raw_code in raw_codes:
                normalized, code_error = validate_stock_code_format(raw_code)
                if code_error:
                    invalid_codes.append(str(raw_code))
                else:
                    normalized_codes.append(str(normalized))
            if invalid_codes:
                return fail(f"Invalid stock code(s): {', '.join(invalid_codes)}")

        result = await assess_p0_1_data_readiness(
            normalized_codes,
            max_stale_days=max_stale_days,
            factor_ic_max_stale_days=factor_ic_max_stale_days,
            max_codes=max_codes,
        )
        return ok(result)

    @mcp.tool()
    async def check_market_temperature_cache_readiness(
        as_of: str | None = None,
        max_stale_days: int = 1,
    ):
        """Read-only freshness check for the durable market-temperature cache."""
        max_stale_days, stale_error = validate_int_range(max_stale_days, field_name="max_stale_days", minimum=0)
        if stale_error:
            return fail(stale_error)
        normalized_as_of = str(as_of or "").strip() or None
        if normalized_as_of and _calc_date_staleness(normalized_as_of) is None:
            return fail(f"Invalid as_of date: {normalized_as_of}")

        result = await assess_market_temperature_cache_readiness(
            as_of=normalized_as_of,
            max_stale_days=max_stale_days,
        )
        return ok(result)

    @mcp.tool()
    async def sync_stale_klines(
        codes: list[str] | None = None,
        max_stale_days: int = 5,
    ):
        """同步过期的 K 线数据到数据库

        检查指定股票的 K 线新鲜度，对过期数据自动从 API 获取最新数据并写入数据库。

        Args:
            codes: 要同步的股票代码列表。为空时自动扫描数据库中所有过期股票。
            max_stale_days: 最大允许过期天数（默认5天）
        """
        max_stale_days, stale_error = validate_int_range(max_stale_days, field_name="max_stale_days", minimum=0)
        if stale_error:
            return fail(stale_error)
        invalid_codes = [code for code in list(codes or []) if validate_stock_code_format(code)[1] is not None]
        if invalid_codes:
            return fail(f"存在无效股票代码: {', '.join(invalid_codes)}")
        result = await sync_stale(
            codes, max_stale_days=max_stale_days,
        )
        return ok(result)
