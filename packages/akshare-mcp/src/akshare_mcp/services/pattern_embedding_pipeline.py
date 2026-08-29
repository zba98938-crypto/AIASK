"""K-line pattern embedding backfill pipeline for unified vector storage."""

from __future__ import annotations

import hashlib
import math
from datetime import date, datetime
from typing import Any, Iterable, Optional

from ..vector_collection_scope import kline_collection_spec
from .vector_search import vector_search_engine


def _normalize_codes(raw: Any) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, (list, tuple, set)):
        items = raw
    else:
        items = str(raw).replace(";", ",").split(",")
    normalized: list[str] = []
    seen: set[str] = set()
    for item in items:
        token = str(item or "").strip()
        if not token or token in seen:
            continue
        seen.add(token)
        normalized.append(token)
    return normalized


def _normalize_positive_int(value: Any, default: int, *, minimum: int = 1, maximum: int = 5000) -> int:
    try:
        resolved = int(default if value is None or value == "" else value)
    except (TypeError, ValueError):
        resolved = int(default)
    return max(minimum, min(resolved, maximum))


def _normalize_vector(values: Iterable[float]) -> list[float]:
    vector = [float(item) for item in list(values or [])]
    if not vector:
        return []
    norm = math.sqrt(sum(item * item for item in vector))
    if norm <= 0:
        return vector
    return [round(item / norm, 10) for item in vector]


def _coerce_date(value: Any) -> Optional[date]:
    if value is None:
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text[:10])
    except Exception:
        return None


def _coerce_date_str(value: Any) -> str:
    parsed = _coerce_date(value)
    return parsed.isoformat() if parsed else ""


def _window_uid(
    *,
    stock_code: str,
    period: str,
    adjust: str,
    window_size: int,
    vector_method: str,
    end_date: str,
    version: str,
) -> str:
    basis = "|".join(
        [
            str(stock_code or "").strip(),
            str(period or "daily").strip().lower(),
            str(adjust or "").strip().lower(),
            str(int(window_size or 0)),
            str(vector_method or "returns").strip().lower(),
            str(end_date or "").strip(),
            str(version or "v1").strip(),
        ]
    )
    return f"kwin_{hashlib.sha1(basis.encode('utf-8')).hexdigest()[:24]}"


def _profile_type(
    *,
    db,
    window_size: int,
    vector_method: str,
    period: str,
    adjust: str,
) -> str:
    builder = getattr(db, "_kline_pattern_profile_type", None)
    if callable(builder):
        return str(
            builder(
                window_size=window_size,
                vector_method=vector_method,
                period=period,
                adjust=adjust,
            )
        )
    return "|".join(
        [
            str(vector_method or "returns").strip().lower(),
            str(period or "daily").strip().lower(),
            str(adjust or "").strip().lower(),
            str(int(window_size or 0)),
        ]
    )


async def _load_candidate_rows(
    db,
    *,
    stock_codes: list[str],
    code_limit: int,
) -> list[dict[str, Any]]:
    if stock_codes:
        rows: list[dict[str, Any]] = []
        for code in stock_codes:
            try:
                info = await db.get_stock_info(code)
            except Exception:
                info = None
            payload = dict(info or {})
            rows.append(
                {
                    "code": code,
                    "name": payload.get("name") or payload.get("stock_name") or "",
                    "industry": payload.get("industry") or "",
                }
            )
        return rows
    if hasattr(db, "list_stock_universe"):
        rows = await db.list_stock_universe(limit=code_limit)
        return [
            {
                "code": str(dict(row).get("code") or "").strip(),
                "name": dict(row).get("name") or dict(row).get("stock_name") or "",
                "industry": dict(row).get("industry") or "",
            }
            for row in rows
            if str(dict(row).get("code") or "").strip()
        ]
    return []


async def _load_recent_klines(db, code: str, *, limit: int) -> list[dict[str, Any]]:
    resolved_limit = max(1, int(limit or 1))
    if hasattr(db, "acquire"):
        try:
            async with db.acquire() as conn:
                rows = await conn.fetch(
                    """
                    SELECT code, time, open, high, low, close, volume, amount, turnover, change_pct
                    FROM (
                        SELECT
                            code, time, open, high, low, close, volume, amount, turnover, change_pct,
                            ROW_NUMBER() OVER (PARTITION BY code ORDER BY time DESC) AS rn
                        FROM kline_1d
                        WHERE code = $1
                    ) ranked
                    WHERE rn <= $2
                    ORDER BY time ASC
                    """,
                    str(code or "").strip(),
                    resolved_limit,
                )
            if rows:
                return [
                    {
                        "date": row["time"].strftime("%Y-%m-%d") if hasattr(row.get("time"), "strftime") else str(row.get("time")),
                        "code": row.get("code"),
                        "open": float(row["open"]) if row.get("open") is not None else None,
                        "high": float(row["high"]) if row.get("high") is not None else None,
                        "low": float(row["low"]) if row.get("low") is not None else None,
                        "close": float(row["close"]) if row.get("close") is not None else None,
                        "volume": int(row["volume"]) if row.get("volume") is not None else 0,
                        "amount": float(row["amount"]) if row.get("amount") is not None else None,
                        "turnover": float(row["turnover"]) if row.get("turnover") is not None else None,
                        "change_pct": float(row["change_pct"]) if row.get("change_pct") is not None else None,
                    }
                    for row in rows
                ]
        except Exception:
            pass
    rows = await db.get_klines(code, limit=resolved_limit)
    return list(rows or [])[-resolved_limit:]


def _forward_return(klines: list[dict[str, Any]], end_idx: int, horizon: int) -> Optional[float]:
    if end_idx < 0 or end_idx >= len(klines):
        return None
    target_idx = end_idx + int(horizon or 0)
    if target_idx >= len(klines):
        return None
    start_close = float(klines[end_idx].get("close") or 0.0)
    end_close = float(klines[target_idx].get("close") or 0.0)
    if start_close <= 0 or end_close <= 0:
        return None
    return round((end_close - start_close) / start_close, 6)


async def backfill_kline_pattern_vectors(
    db,
    *,
    stock_codes: Any = None,
    code_limit: Any = 200,
    window_size: Any = 20,
    lookback_days: Any = 180,
    max_windows_per_code: Any = 1,
    step_days: Any = 5,
    vector_method: str = "returns",
    period: str = "daily",
    adjust: str = "",
    version: str = "v1",
    rebuild_existing: Any = False,
    dry_run: Any = False,
) -> dict[str, Any]:
    resolved_codes = _normalize_codes(stock_codes)
    resolved_code_limit = _normalize_positive_int(code_limit, 200, minimum=1, maximum=5000)
    resolved_window_size = _normalize_positive_int(window_size, 20, minimum=5, maximum=240)
    resolved_lookback_days = _normalize_positive_int(lookback_days, 180, minimum=resolved_window_size, maximum=2000)
    resolved_max_windows_per_code = _normalize_positive_int(max_windows_per_code, 1, minimum=1, maximum=200)
    resolved_step_days = _normalize_positive_int(step_days, 5, minimum=1, maximum=120)
    resolved_rebuild_existing = bool(rebuild_existing)
    resolved_dry_run = bool(dry_run)

    candidate_rows = await _load_candidate_rows(
        db,
        stock_codes=resolved_codes,
        code_limit=resolved_code_limit,
    )
    normalized_vector_method = str(vector_method or "returns").strip().lower()
    collection_spec = kline_collection_spec(normalized_vector_method, resolved_window_size)
    collection_name = str(collection_spec.get("collection_name") or "kline_pattern_embeddings")
    model_id = str(collection_spec.get("model_id") or f"derived:{normalized_vector_method}")
    results = {
        "stock_codes": [row.get("code") for row in candidate_rows if row.get("code")],
        "code_count": len(candidate_rows),
        "window_size": resolved_window_size,
        "lookback_days": resolved_lookback_days,
        "max_windows_per_code": resolved_max_windows_per_code,
        "step_days": resolved_step_days,
        "vector_method": normalized_vector_method,
        "period": str(period or "daily").strip().lower(),
        "adjust": str(adjust or "").strip().lower(),
        "version": str(version or "v1").strip(),
        "rebuild_existing": resolved_rebuild_existing,
        "dry_run": resolved_dry_run,
        "processed_codes": 0,
        "skipped_codes": 0,
        "candidate_windows": 0,
        "saved_windows": 0,
        "saved_profiles": 0,
        "indexed_profiles": 0,
        "collection_name": collection_name,
        "model_id": model_id,
    }

    profile_type = _profile_type(
        db=db,
        window_size=resolved_window_size,
        vector_method=results["vector_method"],
        period=results["period"],
        adjust=results["adjust"],
    )
    results["profile_type"] = profile_type
    index_ensured = False
    collection_saved = False

    for row in candidate_rows:
        code = str(row.get("code") or "").strip()
        if not code:
            continue
        history_limit = resolved_lookback_days + max(20, resolved_window_size + (resolved_max_windows_per_code - 1) * resolved_step_days)
        klines = await _load_recent_klines(db, code, limit=history_limit)
        if not klines or len(klines) < resolved_window_size:
            results["skipped_codes"] += 1
            continue

        results["processed_codes"] += 1
        end_indices = []
        cursor = len(klines) - 1
        while cursor >= resolved_window_size - 1 and len(end_indices) < resolved_max_windows_per_code:
            end_indices.append(cursor)
            cursor -= resolved_step_days

        for end_idx in end_indices:
            start_idx = end_idx - resolved_window_size + 1
            window_klines = list(klines[start_idx : end_idx + 1])
            vector = vector_search_engine.kline_to_vector(window_klines, method=results["vector_method"])
            vector_values = _normalize_vector(vector.tolist() if hasattr(vector, "tolist") else vector)
            if not vector_values:
                continue
            results["candidate_windows"] += 1
            start_date = _coerce_date_str(window_klines[0].get("date"))
            end_date = _coerce_date_str(window_klines[-1].get("date"))
            uid = _window_uid(
                stock_code=code,
                period=results["period"],
                adjust=results["adjust"],
                window_size=resolved_window_size,
                vector_method=results["vector_method"],
                end_date=end_date,
                version=results["version"],
            )
            payload = {
                "stock_name": row.get("name") or "",
                "industry": row.get("industry") or "",
                "dates": [_coerce_date_str(item.get("date")) for item in window_klines],
                "close_series": [float(item.get("close") or 0.0) for item in window_klines],
                "volume_series": [int(item.get("volume") or 0) for item in window_klines],
            }
            metadata = {
                "stock_name": row.get("name") or "",
                "industry": row.get("industry") or "",
                "source": "kline_1d",
                "window_end_index": end_idx,
            }
            if resolved_dry_run:
                results["saved_windows"] += 1
                results["saved_profiles"] += 1
                continue
            saved_window = await db.save_kline_pattern_window(
                {
                    "window_uid": uid,
                    "stock_code": code,
                    "end_date": end_date,
                    "start_date": start_date,
                    "period": results["period"],
                    "adjust": results["adjust"],
                    "window_size": resolved_window_size,
                    "vector_method": results["vector_method"],
                    "metric": "cosine",
                    "vector_dim": len(vector_values),
                    "forward_return_5d": _forward_return(klines, end_idx, 5),
                    "forward_return_10d": _forward_return(klines, end_idx, 10),
                    "forward_return_20d": _forward_return(klines, end_idx, 20),
                    "payload": payload,
                    "metadata": metadata,
                }
            )
            results["saved_windows"] += 1
            if not collection_saved:
                await db.save_vector_collection(
                    {
                        "collection_name": collection_name,
                        "entity_family": "kline_pattern",
                        "backend": db.get_vector_backend(),
                        "metric": "cosine",
                        "model_id": model_id,
                        "vector_dim": len(vector_values),
                        "normalization": "unit",
                        "status": "active",
                        "metadata": {
                            "domain": "quant",
                            "window_size": resolved_window_size,
                            "vector_method": results["vector_method"],
                            "period": results["period"],
                            "adjust": results["adjust"],
                        },
                    }
                )
                collection_saved = True
            await db.save_vector_profile(
                {
                    "collection_name": collection_name,
                    "entity_type": "kline_pattern_window",
                    "entity_id": uid,
                    "stock_code": code,
                    "profile_type": profile_type,
                    "model_id": model_id,
                    "vector_dim": len(vector_values),
                    "metric": "cosine",
                    "version": results["version"],
                    "signature": hashlib.sha1(
                        f"{uid}|{results['vector_method']}|{results['version']}|{payload['close_series']}".encode("utf-8")
                    ).hexdigest(),
                    "embedding": vector_values,
                    "metadata": {
                        **metadata,
                        "window_uid": saved_window.get("window_uid"),
                        "start_date": start_date,
                        "end_date": end_date,
                        "window_size": resolved_window_size,
                        "period": results["period"],
                        "adjust": results["adjust"],
                        "rebuild_existing": resolved_rebuild_existing,
                    },
                }
            )
            results["saved_profiles"] += 1
            if not index_ensured and hasattr(db, "ensure_vector_profile_sqlite_python_index"):
                await db.ensure_vector_profile_sqlite_python_index(
                    collection_name=collection_name,
                    version=results["version"],
                    vector_dim=len(vector_values),
                    profile_type=profile_type,
                    metric="cosine",
                )
                index_ensured = True
                results["indexed_profiles"] += 1

    return results
