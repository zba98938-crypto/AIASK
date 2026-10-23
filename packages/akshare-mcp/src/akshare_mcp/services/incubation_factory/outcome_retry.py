"""Terminal or retryable: what to do with an outcome stuck in ``pending_market_data``.

The measured state (2026-10-22): 600 predictions target 2026-10-19/20, the
scarce resource is the daily bar, and the daily scorer simply re-wrote
``pending_market_data`` on every pass while ``pending_for_outcome`` excluded
them from re-scoring. Nothing ever resolved, and the scorecard could only say
"0 scored".

The distinction this module enforces:

* the target session is **later than the newest bar in the store** → the data
  is *late*, not absent. Status stays ``pending_market_data`` but the reason
  becomes ``data_sync_stalled`` with the latest bar date, so a stalled sync is
  visible instead of looking like a scoring bug.
* the target session is **not later than the newest bar yet has no bar** for
  that code → the bar is missing for good. After ``max_attempts`` the row moves
  to ``data_unavailable`` with ``daily_bar_missing_after_retry``.

Nothing here writes a zero return or a fabricated status.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Optional

logger = logging.getLogger(__name__)

DAILY_SCORE_VERSION = "trade_prediction_score_daily_v1"
REASON_SYNC_STALLED = "data_sync_stalled"
REASON_BAR_MISSING_FINAL = "daily_bar_missing_after_retry"
REASON_BAR_PRESENT_RESCORED = "rescored_from_existing_bar"
DEFAULT_MAX_ATTEMPTS = 5


def _string(value: Any) -> str:
    return str(value or "").strip()


def _day(value: Any) -> Optional[date]:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    token = _string(value)
    if not token:
        return None
    for length, fmt in ((10, "%Y-%m-%d"), (8, "%Y%m%d")):
        try:
            return datetime.strptime(token[:length], fmt).date()
        except ValueError:
            continue
    return None


async def _rows(db: Any, sql: str, *args: Any) -> list[dict[str, Any]]:
    fetch = getattr(db, "fetch", None)
    if not callable(fetch):
        return []
    raw = await fetch(sql, *args)
    if isinstance(raw, dict):
        for key in ("data", "rows", "items", "results"):
            if isinstance(raw.get(key), list):
                return [dict(item) for item in raw[key] if isinstance(item, dict)]
        return []
    return [dict(item) for item in (raw or []) if isinstance(item, dict)]


async def latest_bar_date(db: Any, *, code: Optional[str] = None) -> Optional[date]:
    """Newest bar date in the store, optionally for one code.

    The per-code form is the one that matters for triage: the market-wide
    ``MAX(time)`` can be a single index row (measured: 2026-10-20 held 4 rows
    while 2026-10-16 held 5,570), which makes a missing stock bar look like
    "inside the stored range".
    """
    if code:
        rows = await _rows(
            db,
            "SELECT MAX(substr(time, 1, 10)) AS latest FROM kline_1d WHERE substr(code, 1, 6) = substr($1, 1, 6)",
            code,
        )
    else:
        rows = await _rows(db, "SELECT MAX(substr(time, 1, 10)) AS latest FROM kline_1d")
    for row in rows:
        value = _day(row.get("latest"))
        if value is not None:
            return value
    return None


async def last_fully_synced_session(db: Any, threshold: int = 1000, lookback_days: int = 30) -> Optional[date]:
    """Newest session that carries the whole market, not a handful of index rows.

    Measured: 2026-10-16 holds 5,570 codes while 2026-10-19/20 hold 4 each. The
    market-wide ``MAX(time)`` therefore reports a session as "stored" when the
    daily sync has in fact stopped.

    Bounded on purpose: a single ``GROUP BY day`` over the full 1.7M-row table
    is slow, so only the newest ``lookback_days`` distinct sessions are counted
    (each count is an indexed lookup).
    """
    rows = await _rows(
        db,
        "SELECT DISTINCT substr(time, 1, 10) AS day FROM kline_1d ORDER BY day DESC LIMIT $1",
        int(lookback_days),
    )
    for row in rows:
        value = _day(row.get("day"))
        if value is None:
            continue
        if await market_coverage_on(db, value) >= threshold:
            return value
    return None


async def market_coverage_on(db: Any, day: date, threshold: int = 1000) -> int:
    """How many codes have a bar on ``day``; below ``threshold`` the day is not synced."""
    rows = await _rows(
        db,
        "SELECT COUNT(DISTINCT substr(code, 1, 6)) AS codes FROM kline_1d WHERE substr(time, 1, 10) = $1",
        day.isoformat(),
    )
    for row in rows:
        try:
            return int(row.get("codes") or 0)
        except (TypeError, ValueError):
            return 0
    return 0


@dataclass(slots=True)
class StuckOutcome:
    outcome_id: str
    prediction_id: str
    strategy_id: str
    stock_code: str
    target_trading_date: Optional[date]
    attempts: int

    @property
    def key(self) -> str:
        return self.outcome_id or self.prediction_id


async def list_stuck_outcomes(db: Any, *, limit: int = 5000, score_version: str = DAILY_SCORE_VERSION) -> list[StuckOutcome]:
    rows = await _rows(
        db,
        """
        SELECT outcome_id, prediction_id, strategy_id, stock_code,
               actual_trading_date, score_status, metadata
        FROM strategy_trade_prediction_outcomes
        WHERE score_version = $1 AND score_status = 'pending_market_data'
        LIMIT $2
        """,
        score_version,
        int(limit),
    )
    predictions = {}
    prediction_ids = [row.get("prediction_id") for row in rows if row.get("prediction_id")]
    if prediction_ids:
        placeholders = ",".join(f"${index + 1}" for index in range(len(prediction_ids)))
        prediction_rows = await _rows(
            db,
            f"SELECT prediction_id, target_trading_date FROM strategy_trade_predictions WHERE prediction_id IN ({placeholders})",
            *prediction_ids,
        )
        predictions = {row.get("prediction_id"): row.get("target_trading_date") for row in prediction_rows}
    out: list[StuckOutcome] = []
    for row in rows:
        metadata = row.get("metadata")
        if isinstance(metadata, str):
            # Raw fetch returns the TEXT column; the adapter only decodes JSON
            # on the typed list_* helpers.
            try:
                metadata = json.loads(metadata or "{}")
            except ValueError:
                metadata = {}
        attempts = 0
        if isinstance(metadata, dict):
            try:
                attempts = int(metadata.get("retry_attempts") or 0)
            except (TypeError, ValueError):
                attempts = 0
        target = _day(predictions.get(row.get("prediction_id")) or row.get("actual_trading_date"))
        out.append(
            StuckOutcome(
                outcome_id=_string(row.get("outcome_id")),
                prediction_id=_string(row.get("prediction_id")),
                strategy_id=_string(row.get("strategy_id")),
                stock_code=_string(row.get("stock_code")),
                target_trading_date=target,
                attempts=attempts,
            )
        )
    return out


def classify(
    stuck: StuckOutcome,
    *,
    latest_bar: Optional[date],
    code_latest_bar: Optional[date] = None,
    last_synced_session: Optional[date] = None,
    market_codes_on_target: Optional[int] = None,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    market_threshold: int = 1000,
) -> tuple[str, str]:
    """Return ``(action, reason)`` where action is keep | finalize.

    Each step uses a different piece of evidence, and the order matters:

    1. the market-wide max bar exists but that session holds only a handful of
       rows → the daily sync stopped; the max is not a synced session
       (``data_sync_stalled``). Measured: 2026-10-19/20 hold 4 index rows each
       while 2026-10-16 holds 5,570.
    2. this code's own series stops before the target session → a gap for that
       stock (delisted, suspended, never synced), reported as
       ``code_series_stale`` — not as "the day is in the store".
    3. both have the session and the bar still is missing → retry with a budget,
       then ``daily_bar_missing_after_retry``.
    """
    target = stuck.target_trading_date
    if target is None:
        return "finalize", "target_trading_date_missing"
    market_has_session = (
        latest_bar is not None
        and target <= latest_bar
        and not (
            market_codes_on_target is not None
            and target == latest_bar
            and market_codes_on_target < market_threshold
        )
    )
    if not market_has_session:
        return "keep", REASON_SYNC_STALLED
    if last_synced_session is not None and target > last_synced_session:
        return "keep", REASON_SYNC_STALLED
    if code_latest_bar is not None and target > code_latest_bar:
        return "keep", "code_series_stale"
    if stuck.attempts + 1 >= max_attempts:
        return "finalize", REASON_BAR_MISSING_FINAL
    return "keep", "retry_scheduled"


async def apply_classification(
    db: Any,
    *,
    latest_bar: Optional[date],
    last_synced_session: Optional[date] = None,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    limit: int = 5000,
) -> dict[str, Any]:
    if last_synced_session is None:
        last_synced_session = await last_fully_synced_session(db)
    stuck = await list_stuck_outcomes(db, limit=limit)
    counts: dict[str, int] = {}
    code_latest: dict[str, Optional[date]] = {}
    coverage: dict[date, int] = {}
    for item in stuck:
        code = item.stock_code
        if code not in code_latest:
            code_latest[code] = await latest_bar_date(db, code=code)
        target = item.target_trading_date
        if target is not None and target not in coverage:
            coverage[target] = await market_coverage_on(db, target)
        action, reason = classify(
            item,
            latest_bar=latest_bar,
            code_latest_bar=code_latest.get(code),
            last_synced_session=last_synced_session,
            market_codes_on_target=coverage.get(target) if target else None,
            max_attempts=max_attempts,
        )
        counts[reason] = counts.get(reason, 0) + 1
        await _update(db, item, action=action, reason=reason, attempts=item.attempts + 1)
    return {
        "status": "ok",
        "score_version": DAILY_SCORE_VERSION,
        "latest_bar": latest_bar.isoformat() if latest_bar else None,
        "last_synced_session": last_synced_session.isoformat() if last_synced_session else None,
        "examined": len(stuck),
        "reason_counts": counts,
        "finalized": counts.get(REASON_BAR_MISSING_FINAL, 0) + counts.get("target_trading_date_missing", 0),
        "sync_stalled": counts.get(REASON_SYNC_STALLED, 0),
        "code_series_stale": counts.get("code_series_stale", 0),
        "examples": [
            {
                "prediction_id": item.prediction_id,
                "target": item.target_trading_date.isoformat() if item.target_trading_date else None,
                "reason": classify(
                    item,
                    latest_bar=latest_bar,
                    code_latest_bar=code_latest.get(item.stock_code),
                    last_synced_session=last_synced_session,
                    market_codes_on_target=coverage.get(item.target_trading_date) if item.target_trading_date else None,
                    max_attempts=max_attempts,
                )[1],
            }
            for item in stuck[:20]
        ],
    }


async def _update(db: Any, item: StuckOutcome, *, action: str, reason: str, attempts: int) -> None:
    execute = getattr(db, "execute", None)
    if not callable(execute):
        return
    status = "data_unavailable" if action == "finalize" else "pending_market_data"
    try:
        await execute(
            """
            UPDATE strategy_trade_prediction_outcomes
            SET score_status = $1
            WHERE outcome_id = $2
            """,
            status,
            item.outcome_id,
        )
    except Exception as exc:
        logger.warning("stuck outcome update failed for %s: %s", item.outcome_id, exc)
        return
    try:
        await execute(
            """
            UPDATE strategy_trade_prediction_outcomes
            SET metadata = json_set(COALESCE(metadata, '{}'), '$.retry_attempts', $1, '$.stuck_reason', $2)
            WHERE outcome_id = $3
            """,
            int(attempts),
            reason,
            item.outcome_id,
        )
    except Exception as exc:
        logger.warning("stuck outcome metadata update failed for %s: %s", item.outcome_id, exc)


__all__ = [
    "DAILY_SCORE_VERSION",
    "REASON_BAR_MISSING_FINAL",
    "REASON_SYNC_STALLED",
    "StuckOutcome",
    "apply_classification",
    "classify",
    "last_fully_synced_session",
    "latest_bar_date",
    "market_coverage_on",
    "list_stuck_outcomes",
]
