#!/usr/bin/env python3
"""Once-daily full-market bar sync sidecar with timeout and completeness stamp."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
RUNTIME_HOME = Path(os.getenv("AIASK_RUNTIME_HOME") or (Path.home() / ".aiask")).expanduser()
STATUS = RUNTIME_HOME / "run" / "daily_market_sync.status.json"
PID_PATH = RUNTIME_HOME / "run" / "daily_market_sync.pid"
FULL_SESSION_MIN_CODES = 1000
DEFAULT_TIMEOUT_SECONDS = 4 * 60 * 60


def _write_status(payload: dict) -> None:
    STATUS.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATUS.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    tmp.replace(STATUS)


def _trading_day(day: date) -> bool:
    try:
        from strategy_factory.domain.trading_calendar import get_trading_calendar
        return bool(get_trading_calendar().is_trading_day(day))
    except Exception as exc:
        raise RuntimeError(f"cannot verify trading calendar for {day}: {exc}") from exc


def _record_completeness(*, sync_ok: bool) -> dict:
    import sqlite3

    configured = os.getenv("AIASK_SQLITE_PATH") or os.getenv("AKSHARE_MCP_SQLITE_PATH")
    db_path = Path(configured).expanduser() if configured else RUNTIME_HOME / "data/db/akshare_mcp.sqlite3"
    con = sqlite3.connect(db_path, timeout=30)
    try:
        row = con.execute("SELECT MAX(substr(time,1,10)) FROM kline_1d").fetchone()
        as_of = row[0] if row else None
        count = con.execute("SELECT COUNT(DISTINCT code) FROM kline_1d WHERE substr(time,1,10)=?", (as_of,)).fetchone()[0] if as_of else 0
        status = "ok" if sync_ok and count >= FULL_SESSION_MIN_CODES else "partial"
        detail = json.dumps({"source": "free_data_daily_sync", "coverage_codes": count, "threshold": FULL_SESSION_MIN_CODES}, ensure_ascii=False)
        con.execute("""
            INSERT INTO tdx_data_completeness(data_key,status,as_of_date,row_count,detail,updated_at)
            VALUES('kline_1d',?,?,?,?,CURRENT_TIMESTAMP)
            ON CONFLICT(data_key) DO UPDATE SET status=excluded.status, as_of_date=excluded.as_of_date,
                row_count=excluded.row_count, detail=excluded.detail, updated_at=CURRENT_TIMESTAMP
        """, (status, as_of, count, detail))
        con.commit()
        return {"data_key": "kline_1d", "status": status, "as_of_date": as_of, "row_count": count}
    finally:
        con.close()


def run_once(*, timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS, limit: int = 0) -> dict:
    now = datetime.now().astimezone()
    if not _trading_day(now.date()):
        result = {"status": "skipped", "reason": "non_trading_day", "date": now.date().isoformat()}
        _write_status({"name": "daily_market_sync", **result, "pid": os.getpid(), "updated_at": now.isoformat()})
        return result
    command = [sys.executable, str(ROOT / "scripts/free_data_daily_sync.py"), "--json", "--universe", "full", "--concurrency", "1"]
    if limit:
        command.extend(["--limit", str(limit)])
    started = datetime.now().astimezone().isoformat(timespec="seconds")
    _write_status({"name": "daily_market_sync", "status": "running", "pid": os.getpid(), "started_at": started, "updated_at": started})
    try:
        proc = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, timeout=timeout_seconds, check=False)
    except subprocess.TimeoutExpired as exc:
        result = {"status": "failed", "reason": "sync_timeout", "timeout_seconds": timeout_seconds, "error": str(exc)}
        _write_status({"name": "daily_market_sync", **result, "pid": os.getpid(), "started_at": started, "updated_at": datetime.now().astimezone().isoformat()})
        return result
    completeness = _record_completeness(sync_ok=proc.returncode == 0)
    result = {"status": "completed" if proc.returncode == 0 else "failed", "returncode": proc.returncode,
              "completeness": completeness, "stdout_tail": proc.stdout[-4000:], "stderr_tail": proc.stderr[-2000:]}
    _write_status({"name": "daily_market_sync", **result, "pid": os.getpid(), "started_at": started,
                   "updated_at": datetime.now().astimezone().isoformat()})
    return result


def _seconds_to(hhmm: str) -> float:
    hour, minute = (int(x) for x in hhmm.split(":"))
    now = datetime.now().astimezone()
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return (target - now).total_seconds()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--once", action="store_true")
    mode.add_argument("--daemon", action="store_true")
    parser.add_argument("--run-time", default="15:40")
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_SECONDS)
    parser.add_argument("--limit", type=int, default=0)
    args = parser.parse_args()
    if args.once or not args.daemon:
        result = run_once(timeout_seconds=args.timeout, limit=args.limit)
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        return 0 if result.get("status") in {"completed", "skipped"} else 1
    PID_PATH.parent.mkdir(parents=True, exist_ok=True)
    PID_PATH.write_text(f"{os.getpid()}\n", encoding="utf-8")
    try:
        while True:
            delay = _seconds_to(args.run_time)
            while delay > 0:
                _write_status({"name": "daily_market_sync", "status": "running", "pid": os.getpid(),
                               "heartbeat_period_seconds": 60,
                               "updated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                               "run_time": args.run_time})
                chunk = min(60, delay)
                time.sleep(chunk)
                delay -= chunk
            run_once(timeout_seconds=args.timeout, limit=args.limit)
    except KeyboardInterrupt:
        _write_status({"name": "daily_market_sync", "status": "stopped", "pid": os.getpid(), "updated_at": datetime.now().astimezone().isoformat()})
    finally:
        PID_PATH.unlink(missing_ok=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
