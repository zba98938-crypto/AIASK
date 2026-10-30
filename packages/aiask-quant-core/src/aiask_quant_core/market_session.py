"""Label output with the newest synced market session, not the wall clock.

Measured (2026-10-24, a Saturday): incubation, SignalTracker and the factory
submit path fell back to ``date.today()`` and wrote 1,921
``strategy_signals``, 574 ``strategy_incubation_metrics``, 598 ``paper_nav``
and 657 ``strategy_trade_predictions`` rows dated 2026-10-24 — a session that
does not exist in ``kline_1d``.  Downstream joins on trading date then return
nothing for those rows.

The session is derived from the data: the newest day whose ``kline_1d``
coverage reaches ``threshold`` rows.  A day with a handful of rows (the live
store held 53 codes/day for 10-19..10-23 while 10-16 held 5,570) is not a
synced session.  Index bars are not used: ``sh000001`` stopped at 10-20 while
stock bars reached 10-23.  Re-running before the next sync labels the previous
session again, so the same keys are upserted instead of a new impossible date.

Falling back to the wall clock is honest only when the store has no bars at
all (or cannot be read); every result says which source it used.
"""

from __future__ import annotations

import logging
import time as _time
import weakref
from datetime import date, datetime, timedelta
from typing import Any, Optional

logger = logging.getLogger(__name__)

FULL_SESSION_MIN_CODES = 1000
LOOKBACK_CALENDAR_DAYS = 21
_CACHE_TTL_SEC = 300.0

SOURCE_SYNCED = "kline_1d_synced_session"
SOURCE_PARTIAL = "kline_1d_partial_latest"
SOURCE_WALL_CLOCK = "wall_clock_no_bars"
SOURCE_UNREADABLE = "wall_clock_store_unreadable"

_cache: dict[int, tuple[weakref.ReferenceType[Any], float, dict[str, Any]]] = {}


def _day(value: Any) -> Optional[date]:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    token = str(value or "").strip()
    for length, fmt in ((10, "%Y-%m-%d"), (8, "%Y%m%d")):
        try:
            return datetime.strptime(token[:length], fmt).date()
        except ValueError:
            continue
    return None


async def _rows(db: Any, sql: str, *args: Any) -> list[dict[str, Any]]:
    fetch = getattr(db, "fetch", None)
    if callable(fetch):
        raw = await fetch(sql, *args)
    elif hasattr(db, "acquire"):
        async with db.acquire() as conn:
            raw = await conn.fetch(sql, *args)
    else:
        raise TypeError(f"{type(db).__name__} exposes neither fetch nor acquire")
    return [dict(item) for item in (raw or [])]


async def _coverage(db: Any, day: date) -> int:
    # Range predicate on ``time`` (~10 ms) instead of substr() over the table.
    rows = await _rows(
        db,
        "SELECT COUNT(*) AS n FROM kline_1d WHERE time >= $1 AND time < $2",
        day.isoformat(),
        (day + timedelta(days=1)).isoformat(),
    )
    # COUNT(*) always yields one integer row; anything else is a store error and
    # propagates to resolve_session_date's "unreadable" branch.
    return int(rows[0]["n"]) if rows else 0


async def resolve_session_date(
    db: Any,
    *,
    threshold: int = FULL_SESSION_MIN_CODES,
    use_cache: bool = True,
) -> dict[str, Any]:
    """Return ``{"date", "source", "latest_bar_date", "coverage"}``."""
    key = id(db)
    if use_cache:
        hit = _cache.get(key)
        if hit is not None and hit[0]() is db and _time.monotonic() - hit[1] < _CACHE_TTL_SEC:
            return dict(hit[2])

    try:
        latest: Optional[date] = None
        for row in await _rows(db, "SELECT MAX(time) AS latest FROM kline_1d"):
            latest = _day(row.get("latest"))
        if latest is None:
            result = {"date": date.today(), "source": SOURCE_WALL_CLOCK, "latest_bar_date": None, "coverage": 0}
        else:
            result = {"date": latest, "source": SOURCE_PARTIAL, "latest_bar_date": latest, "coverage": None}
            for offset in range(LOOKBACK_CALENDAR_DAYS):
                day = latest - timedelta(days=offset)
                coverage = await _coverage(db, day)
                if coverage >= threshold:
                    result = {"date": day, "source": SOURCE_SYNCED, "latest_bar_date": latest, "coverage": coverage}
                    break
    except Exception as exc:
        logger.warning("market session resolution failed; using wall clock: %s: %s", type(exc).__name__, exc)
        # Not cached: the next call retries the store.
        return {"date": date.today(), "source": SOURCE_UNREADABLE, "latest_bar_date": None,
                "coverage": None, "error": f"{type(exc).__name__}: {exc}"}

    if use_cache:
        try:
            db_ref = weakref.ref(db, lambda _ref, cache_key=key: _cache.pop(cache_key, None))
        except TypeError:
            # Some adapter/fake objects do not support weak references; avoiding
            # cache is safer than keeping an id-only entry that can leak to a
            # later object after Python reuses the id.
            return result
        _cache[key] = (db_ref, _time.monotonic(), dict(result))
    return result


async def session_date_or_today(db: Any) -> date:
    return (await resolve_session_date(db))["date"]


def clear_cache() -> None:
    _cache.clear()
