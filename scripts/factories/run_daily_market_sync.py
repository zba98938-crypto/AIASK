#!/usr/bin/env python3
"""Once-daily full-market bar sync sidecar with timeout and completeness stamp."""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
import uuid
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))
RUNTIME_HOME = Path(os.getenv("AIASK_RUNTIME_HOME") or (Path.home() / ".aiask")).expanduser()
STATUS = RUNTIME_HOME / "run" / "daily_market_sync.status.json"
PID_PATH = RUNTIME_HOME / "run" / "daily_market_sync.pid"
FULL_SESSION_MIN_CODES = 1000
DEFAULT_TIMEOUT_SECONDS = 4 * 60 * 60
_LOCK_FD: int | None = None


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


def _record_completeness(*, sync_ok: bool, verified_amounts: dict[str, set[str]] | None = None) -> dict:
    import sqlite3

    configured = os.getenv("AIASK_SQLITE_PATH") or os.getenv("AKSHARE_MCP_SQLITE_PATH")
    db_path = Path(configured).expanduser() if configured else RUNTIME_HOME / "data/db/akshare_mcp.sqlite3"
    con = sqlite3.connect(db_path.resolve().as_uri() + "?mode=ro", uri=True, timeout=30)
    try:
        con.execute("PRAGMA query_only=ON")
        row = con.execute("SELECT MAX(substr(time,1,10)) FROM kline_1d").fetchone()
        as_of = row[0] if row else None
        observed_count = con.execute(
            "SELECT COUNT(DISTINCT code) FROM kline_1d WHERE substr(time,1,10)=?",
            (as_of,),
        ).fetchone()[0] if as_of else 0
        has_stock_universe = con.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='stocks'"
        ).fetchone() is not None
        universe_count = None
        missing_codes: list[str] = []
        if has_stock_universe and as_of:
            universe_codes = [str(item[0]) for item in con.execute(
                "SELECT DISTINCT s.stock_code FROM stocks s "
                "WHERE s.stock_code GLOB '[0-9]*' AND COALESCE(s.list_status, 'L')='L' "
                "ORDER BY s.stock_code"
            )]
            # Exclude symbols already delisted by the snapshot date. Retaining
            # them in today's expected universe would turn historical symbols
            # into permanent false gaps.
            delisted_as_of = {
                str(item[0])
                for item in con.execute(
                    "SELECT DISTINCT s.stock_code FROM stocks s "
                    "JOIN kline_1d k ON k.code=s.stock_code "
                    "WHERE s.stock_code GLOB '[0-9]*' AND COALESCE(s.list_status,'L')='D' "
                    "AND substr(k.time,1,10)<=?",
                    (as_of,),
                )
            }
            universe_codes = [code for code in universe_codes if code not in delisted_as_of]
            universe_count = len(universe_codes)
            present_codes = {str(item[0]) for item in con.execute(
                "SELECT DISTINCT code FROM kline_1d WHERE substr(time,1,10)=?",
                (as_of,),
            )}
            missing_codes = sorted(set(universe_codes) - present_codes)
            observed_universe_codes = len(set(universe_codes) & present_codes)
            count = observed_universe_codes
            coverage_complete = bool(universe_count and not missing_codes)
            coverage_basis = "active_stocks_universe"
        else:
            count = observed_count
            # A row-count threshold cannot prove full-market coverage without
            # an authoritative universe denominator.
            coverage_complete = False
            coverage_basis = "observed_count_only"
        columns = {row[1] for row in con.execute("PRAGMA table_info(kline_1d)")}
        missing_amount = con.execute(
            "SELECT COUNT(DISTINCT code) FROM kline_1d WHERE substr(time,1,10)=? AND amount IS NULL",
            (as_of,),
        ).fetchone()[0] if "amount" in columns and as_of else None
        unverified_amount_codes = sorted(set(universe_codes) - (verified_amounts or {}).get(as_of, set())) if has_stock_universe and as_of else None
        amount_evidence_complete = missing_amount == 0 and unverified_amount_codes == []
        from _daily_kline_ingest import closed_date
        expected_day = closed_date()
        while not _trading_day(expected_day):
            expected_day -= timedelta(days=1)
        fresh = as_of == expected_day.isoformat()
        status = "ok" if sync_ok and coverage_complete and amount_evidence_complete and fresh else "partial"
        detail = json.dumps({
            "source": "free_data_daily_sync",
            "coverage_basis": coverage_basis,
            "coverage_codes": count,
            "observed_codes": observed_count,
            "active_universe_codes": universe_count,
            "delisted_with_history_excluded": len(delisted_as_of) if has_stock_universe and as_of else 0,
            "missing_codes": missing_codes,
            "coverage_unknown_reason": (
                None if has_stock_universe and as_of else
                "stocks_universe_missing" if not has_stock_universe else
                "snapshot_date_missing"
            ),
            "threshold": FULL_SESSION_MIN_CODES,
            "coverage_complete": coverage_complete,
            "amount_missing_codes": missing_amount,
            "amount_evidence_complete": amount_evidence_complete,
            "amount_provenance_unverified_codes": unverified_amount_codes,
            "legacy_amount_provenance_verified": False,
            "expected_closed_session": expected_day.isoformat(),
            "current_session_fresh": fresh,
        }, ensure_ascii=False)
        con.close()
        con = sqlite3.connect(db_path.resolve().as_uri() + "?mode=rw", uri=True, timeout=30)
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
    audit_dir = RUNTIME_HOME / "artifacts/daily_market_sync" / (now.strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:8])
    audit_dir.mkdir(parents=True)
    command = [
        sys.executable, "-u", str(ROOT / "scripts/free_data_daily_sync.py"),
        "--json", "--universe", "full", "--concurrency", "1", "--append-only",
        "--skip-index", "--skip-sector", "--skip-north", "--skip-ic", "--skip-factor-pool",
        "--channels", "eastmoney", "sina", "tencent",
    ]
    if limit:
        command.extend(["--limit", str(limit)])
    started = datetime.now().astimezone().isoformat(timespec="seconds")
    _write_status({"name": "daily_market_sync", "status": "running", "pid": os.getpid(), "started_at": started, "updated_at": started, "audit_dir": str(audit_dir)})
    child_env = dict(os.environ)
    child_env.update(FREE_DATA_DAILY_AUDIT_DIR=str(audit_dir), LIVE_TRADING_ENABLED="0", LIVE_TRADING_ALLOW_WRITE="0", BROKER_ALLOW_WRITE="0", LIVE_TRADING_READ_ONLY="1", BROKER_READ_ONLY="1")
    from _daily_kline_ingest import write_evidence

    write_evidence(audit_dir / "launch", {"command": command, "started_at": started, "pid": os.getpid()})
    proc = None
    failure = None
    interrupted = False
    try:
        with (audit_dir / "stdout.log").open("w") as stdout, (audit_dir / "stderr.log").open("w") as stderr:
            proc = subprocess.Popen(command, cwd=ROOT, env=child_env, stdout=stdout, stderr=stderr,
                                    text=True, start_new_session=True,
                                    pass_fds=(_LOCK_FD,) if _LOCK_FD is not None else ())
            deadline = time.monotonic() + timeout_seconds
            while True:
                _write_status({"name": "daily_market_sync", "status": "syncing", "pid": os.getpid(),
                               "child_pid": proc.pid, "child_command": command, "started_at": started,
                               "updated_at": datetime.now().astimezone().isoformat(), "audit_dir": str(audit_dir),
                               "completed_symbol_results": len(list((audit_dir / "symbol_results").glob("*.json.gz")))})
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise subprocess.TimeoutExpired(command, timeout_seconds)
                try:
                    proc.wait(timeout=min(30, remaining))
                    break
                except subprocess.TimeoutExpired:
                    if time.monotonic() >= deadline:
                        raise
    except subprocess.TimeoutExpired as exc:
        failure = {"reason": "sync_timeout", "timeout_seconds": timeout_seconds, "error": str(exc)}
    except (KeyboardInterrupt, SystemExit):
        interrupted = True
        failure = {"reason": "sync_interrupted"}
    except Exception as exc:
        failure = {"reason": "sync_launch_or_wait_failed", "error": f"{type(exc).__name__}: {exc}"}
    finally:
        if proc is not None and proc.poll() is None:
            os.killpg(proc.pid, signal.SIGTERM)
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.wait(timeout=5)
    from collections import Counter
    import gzip

    receipts = [json.loads(gzip.decompress(path.read_bytes())) for path in (audit_dir / "receipts").glob("*.json.gz")]
    verified_amounts: dict[str, set[str]] = {}
    for receipt in receipts:
        reference = receipt["evidence"]
        raw = gzip.decompress(Path(reference["path"]).read_bytes())
        import hashlib
        if hashlib.sha256(raw).hexdigest() != reference["sha256"]:
            raise ValueError("ingest_plan_hash_mismatch")
        plan = json.loads(raw)["plan"]
        if plan["amount_basis"] == "provider_f57":
            for bar in plan["new_rows"]:
                if bar["amount"] is not None:
                    verified_amounts.setdefault(bar["date"], set()).add(plan["code"])
    summary = {
        "audited_symbol_count": len(receipts),
        "symbol_result_count": len(list((audit_dir / "symbol_results").glob("*.json.gz"))),
        "inserted_bars": sum(item["accepted_count"] for item in receipts),
        "missing_amount_bars": sum(item.get("missing_amount_count", 0) for item in receipts),
        "amount_basis": dict(Counter(item["amount_basis"] for item in receipts)),
        "historical_rows_untouched": True,
        "audit_dir": str(audit_dir),
    }
    successful = failure is None and proc is not None and proc.returncode == 0
    try:
        completeness = _record_completeness(sync_ok=successful, verified_amounts=verified_amounts)
    except Exception as exc:
        completeness = {"status": "unavailable", "error": f"{type(exc).__name__}: {exc}"}
        failure = failure or {"reason": "completeness_write_failed"}
        successful = False
    result = {"status": "completed" if successful else "failed", "returncode": proc.returncode if proc else None,
              **(failure or {}), "completeness": completeness, "ingest_summary": summary,
              "audit_dir": str(audit_dir), "child_pid": proc.pid if proc else None,
              "stdout_log": str(audit_dir / "stdout.log"), "stderr_log": str(audit_dir / "stderr.log")}
    write_evidence(audit_dir / "result", result)
    _write_status({"name": "daily_market_sync", **result, "pid": os.getpid(), "started_at": started,
                   "updated_at": datetime.now().astimezone().isoformat()})
    if interrupted:
        raise KeyboardInterrupt
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
    parser.add_argument("--run-time", default="16:10")
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_SECONDS)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--catch-up", action="store_true", help="Run an immediate append-only catch-up before daily scheduling")
    args = parser.parse_args()
    PID_PATH.parent.mkdir(parents=True, exist_ok=True)
    import fcntl

    lock = PID_PATH.with_suffix(".lock").open("a")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock.close()
        raise SystemExit("daily_market_sync_already_running")
    global _LOCK_FD
    _LOCK_FD = lock.fileno()
    PID_PATH.write_text(f"{os.getpid()}\n", encoding="utf-8")
    previous_term = signal.signal(signal.SIGTERM, lambda signum, frame: (_ for _ in ()).throw(KeyboardInterrupt()))
    try:
        if args.once or not args.daemon:
            result = run_once(timeout_seconds=args.timeout, limit=args.limit)
            print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
            return 0 if result.get("status") in {"completed", "skipped"} else 1
        if args.catch_up:
            run_once(timeout_seconds=args.timeout, limit=args.limit)
        while True:
            delay = _seconds_to(args.run_time)
            while delay > 0:
                _write_status({"name": "daily_market_sync", "status": "waiting", "pid": os.getpid(),
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
        _LOCK_FD = None
        signal.signal(signal.SIGTERM, previous_term)
        lock.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
