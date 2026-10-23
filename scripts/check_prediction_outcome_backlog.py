#!/usr/bin/env python3
"""Triage outcomes stuck in ``pending_market_data`` (WP-0.9).

Diagnostic, not a test: named ``check_*.py`` so pytest does not collect it.

A stuck outcome is one of two things, and conflating them is how "0 scored"
survived for weeks:

* **late data** — the target session is newer than the newest bar in the store.
  The sync, not the scorer, is the blocker. Stays ``pending_market_data``, the
  reason is recorded as ``data_sync_stalled``.
* **missing bar** — the target session is inside the stored range and the bar
  still is not there. After ``--max-attempts`` it becomes ``data_unavailable``.

Usage::

    python scripts/check_prediction_outcome_backlog.py            # dry run
    python scripts/check_prediction_outcome_backlog.py --apply    # write

The read-only database path is opened ``mode=ro``; ``--apply`` needs the normal
path and refuses to run against a file that does not exist.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any

DEFAULT_DB = Path(os.path.expanduser("~/.aiask/data/db/akshare_mcp.sqlite3"))
DAILY_SCORE_VERSION = "trade_prediction_score_daily_v1"
MAX_ATTEMPTS = 5


def _connect(path: Path, *, readonly: bool) -> sqlite3.Connection:
    if not path.exists():
        raise SystemExit(f"database not found: {path}")
    mode = "mode=ro" if readonly else "mode=rw"
    conn = sqlite3.connect(f"file:{path}?{mode}", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _row(conn: sqlite3.Connection, sql: str, args: tuple[Any, ...] = ()) -> dict[str, Any]:
    row = conn.execute(sql, args).fetchone()
    return dict(row) if row else {}


def _rows(conn: sqlite3.Connection, sql: str, args: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    return [dict(row) for row in conn.execute(sql, args).fetchall()]


def _day(value: Any) -> date | None:
    token = str(value or "").strip()
    if not token:
        return None
    for length, fmt in ((10, "%Y-%m-%d"), (8, "%Y%m%d")):
        try:
            return datetime.strptime(token[:length], fmt).date()
        except ValueError:
            continue
    return None


MARKET_THRESHOLD = 1000


def _market_coverage(conn: sqlite3.Connection, day: date | None) -> int:
    if day is None:
        return 0
    row = _row(
        conn,
        "SELECT COUNT(DISTINCT substr(code, 1, 6)) AS codes FROM kline_1d WHERE substr(time, 1, 10) = ?",
        (day.isoformat(),),
    )
    return int(row.get("codes") or 0)


def _last_fully_synced(conn: sqlite3.Connection) -> date | None:
    """Newest session carrying the whole market (>= MARKET_THRESHOLD codes).

    Bounded to the newest 30 distinct sessions: a full-table GROUP BY on 1.7M
    rows is slow, and every count below is an indexed lookup.
    """
    rows = _rows(conn, "SELECT DISTINCT substr(time, 1, 10) AS day FROM kline_1d ORDER BY day DESC LIMIT 30")
    for row in rows:
        day = _day(row.get("day"))
        if day is not None and _market_coverage(conn, day) >= MARKET_THRESHOLD:
            return day
    return None


def collect(conn: sqlite3.Connection) -> dict[str, Any]:
    latest = _row(conn, "SELECT MAX(substr(time, 1, 10)) AS latest FROM kline_1d").get("latest")
    latest_day = _day(latest)
    stuck = _rows(
        conn,
        """
        SELECT o.outcome_id, o.prediction_id, o.stock_code,
               substr(p.target_trading_date, 1, 10) AS target,
               o.metadata
        FROM strategy_trade_prediction_outcomes o
        LEFT JOIN strategy_trade_predictions p ON p.prediction_id = o.prediction_id
        WHERE o.score_version = ? AND o.score_status = 'pending_market_data'
        """,
        (DAILY_SCORE_VERSION,),
    )
    # Compute the expensive lookups once, not per row: the naive version ran a
    # full-table GROUP BY per stuck outcome (600x) and never finished.
    synced = _last_fully_synced(conn)
    code_latest_map: dict[str, date | None] = {}
    for code in {str(row.get("stock_code") or "") for row in stuck}:
        code_latest_map[code] = _day(
            _row(
                conn,
                "SELECT MAX(substr(time, 1, 10)) AS latest FROM kline_1d WHERE substr(code, 1, 6) = substr(?, 1, 6)",
                (code,),
            ).get("latest")
        )
    coverage_map: dict[str, int] = {}
    buckets: dict[str, list[dict[str, Any]]] = {"sync_stalled": [], "code_stale": [], "finalize": [], "retry": []}
    for row in stuck:
        target_day = _day(row.get("target"))
        try:
            metadata = json.loads(row.get("metadata") or "{}")
        except ValueError:
            metadata = {}
        attempts = int(metadata.get("retry_attempts") or 0)
        code = str(row.get("stock_code") or "")
        code_latest = code_latest_map.get(code)
        key = row.get("target") or ""
        if key not in coverage_map:
            coverage_map[key] = _market_coverage(conn, target_day)
        coverage = coverage_map[key]
        if target_day is None:
            buckets["finalize"].append({**row, "reason": "target_trading_date_missing"})
        elif latest_day is None or target_day > latest_day or (synced is not None and target_day > synced):
            buckets["sync_stalled"].append({**row, "reason": "data_sync_stalled"})
        elif code_latest is None or target_day > code_latest:
            buckets["code_stale"].append({**row, "reason": "code_series_stale", "code_latest": code_latest.isoformat() if code_latest else None})
        elif target_day == latest_day and coverage < MARKET_THRESHOLD:
            buckets["sync_stalled"].append({**row, "reason": "data_sync_stalled", "market_codes": coverage})
        elif attempts + 1 >= MAX_ATTEMPTS:
            buckets["finalize"].append({**row, "reason": "daily_bar_missing_after_retry"})
        else:
            buckets["retry"].append({**row, "reason": "retry_scheduled"})
    return {
        "generated_at": datetime.now().astimezone().isoformat(),
        "latest_bar": latest,
        "last_fully_synced_session": synced.isoformat() if synced else None,
        "score_version": DAILY_SCORE_VERSION,
        "stuck_total": len(stuck),
        "sync_stalled": len(buckets["sync_stalled"]),
        "code_stale": len(buckets["code_stale"]),
        "retry": len(buckets["retry"]),
        "finalize": len(buckets["finalize"]),
        "targets": sorted({row["target"] for row in stuck if row.get("target")}),
        "examples": {
            key: [{k: v for k, v in row.items() if k != "metadata"} for row in rows[:5]]
            for key, rows in buckets.items()
        },
        "status": (
            "degraded"
            if buckets["sync_stalled"]
            else ("code_gap" if buckets["code_stale"] else ("actionable" if stuck else "clear"))
        ),
        "detail": _detail(latest, len(stuck), buckets),
    }


def _detail(latest: Any, total: int, buckets: dict[str, list[dict[str, Any]]]) -> str:
    if not total:
        return "no outcomes are stuck in pending_market_data"
    if buckets["sync_stalled"]:
        return (
            f"{total} stuck outcomes; {len(buckets['sync_stalled'])} cannot be scored because the target "
            f"session is not really synced (market max bar {latest}) — the daily sync is the blocker, "
            f"not the scorer"
        )
    if buckets["code_stale"]:
        return (
            f"{total} stuck outcomes; {len(buckets['code_stale'])} belong to codes whose own series stops "
            f"before the target session (delisted/suspended/never synced) — re-scoring cannot fix those"
        )
    return f"{total} stuck outcomes, all inside their code's bar range: {len(buckets['retry'])} retryable, {len(buckets['finalize'])} final"


def apply(conn: sqlite3.Connection, report: dict[str, Any]) -> dict[str, Any]:
    now = datetime.now().astimezone().isoformat()
    finalized = 0
    retried = 0
    rows = _rows(
        conn,
        """
        SELECT o.outcome_id, o.prediction_id, substr(p.target_trading_date, 1, 10) AS target, o.metadata
        FROM strategy_trade_prediction_outcomes o
        LEFT JOIN strategy_trade_predictions p ON p.prediction_id = o.prediction_id
        WHERE o.score_version = ? AND o.score_status = 'pending_market_data'
        """,
        (DAILY_SCORE_VERSION,),
    )
    latest_day = _day(report.get("latest_bar"))
    for row in rows:
        target_day = _day(row.get("target"))
        try:
            metadata = json.loads(row.get("metadata") or "{}")
        except ValueError:
            metadata = {}
        attempts = int(metadata.get("retry_attempts") or 0)
        code_latest = _day(
            _row(
                conn,
                "SELECT MAX(substr(time, 1, 10)) AS latest FROM kline_1d WHERE substr(code, 1, 6) = substr(?, 1, 6)",
                (str(row.get("stock_code") or ""),),
            ).get("latest")
        )
        received = (
            target_day is not None
            and latest_day is not None
            and target_day <= latest_day
            and code_latest is not None
            and target_day <= code_latest
            and not (target_day == latest_day and _market_coverage(conn, target_day) < MARKET_THRESHOLD)
        )
        if target_day is None or (received and attempts + 1 >= MAX_ATTEMPTS):
            status, reason = "data_unavailable", "daily_bar_missing_after_retry"
            finalized += 1
        elif not received:
            status, reason = "pending_market_data", "data_sync_stalled"
        else:
            status, reason = "pending_market_data", "retry_scheduled"
            retried += 1
        metadata["retry_attempts"] = attempts + 1
        metadata["stuck_reason"] = reason
        metadata["stuck_checked_at"] = now
        conn.execute(
            "UPDATE strategy_trade_prediction_outcomes SET score_status = ?, metadata = ? WHERE outcome_id = ?",
            (status, json.dumps(metadata, ensure_ascii=False), row["outcome_id"]),
        )
    conn.commit()
    return {"applied": True, "finalized": finalized, "retried": retried, "rows_touched": len(rows)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--apply", action="store_true", help="write the classifications (default: dry run)")
    parser.add_argument("--json", action="store_true", help="print JSON instead of a table")
    args = parser.parse_args(argv)

    conn = _connect(args.db, readonly=not args.apply)
    try:
        report = collect(conn)
        if args.apply:
            report["write"] = apply(conn, report)
    finally:
        conn.close()

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(f"latest bar      : {report['latest_bar']}")
        print(f"stuck outcomes  : {report['stuck_total']}  targets={report['targets']}")
        print(f"  sync stalled  : {report['sync_stalled']}")
        print(f"  retryable     : {report['retry']}")
        print(f"  finalize      : {report['finalize']}")
        print(f"status          : {report['status']}")
        print(f"detail          : {report['detail']}")
        if args.apply:
            print(f"written         : {report['write']}")
    # Exit code distinguishes the verdicts: 0 clear/retryable, 1 sync stalled.
    return 0 if report["status"] != "degraded" else 1


if __name__ == "__main__":
    sys.exit(main())
