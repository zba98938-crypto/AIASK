#!/usr/bin/env python
"""P1-5 gate harness: SQLite soak / growth monitor.

The readiness review (risk P1-5) requires a staged soak run that records DB
file size growth, oversized single rows, and whether incremental vacuum keeps
the file bounded, BEFORE unconstrained production. This script is that
harness. It does NOT itself run for 6 hours in CI -- you point it at the live
SQLite DB while a factory/incubation workload runs and it samples size + large
rows on an interval, then asserts the configured thresholds at the end.

It is intentionally read-only against the DB (PRAGMA + SELECT only): it never
writes, vacuums, or prunes. That keeps it safe to run against a real soak DB.

Usage (run alongside a workload, e.g. run_strategy_factory.py --once loops):
    python scripts/ops/db_soak.py \
        --db %AIASK_SQLITE_PATH% \
        --duration-min 360 \
        --interval-sec 300 \
        --max-db-mb 100 \
        --max-row-kb 256 \
        --out reports/ops/db_soak_$(date +%Y%m%d).json

Exit code 0 = all gates held; 1 = a threshold was breached (or DB missing).
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _db_size_mb(path: Path) -> float:
    total = 0
    for suffix in ("", "-wal", "-shm"):
        p = Path(str(path) + suffix)
        if p.exists():
            total += p.stat().st_size
    return total / 1024 / 1024


def _largest_rows(conn: sqlite3.Connection, top: int = 10) -> list[dict[str, Any]]:
    """Find the largest single rows across user tables using length() of the row.

    Read-only. Uses a cheap heuristic: sum of length(quote(col)) per row on the
    columns SQLite can measure. Skips tables it cannot introspect.
    """
    rows: list[dict[str, Any]] = []
    tables = [
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
    ]
    for table in tables:
        try:
            cols = [c[1] for c in conn.execute(f'PRAGMA table_info("{table}")').fetchall()]
            if not cols:
                continue
            length_expr = " + ".join(f'COALESCE(LENGTH(CAST("{c}" AS BLOB)),0)' for c in cols)
            row = conn.execute(
                f'SELECT MAX({length_expr}) FROM "{table}"'
            ).fetchone()
            max_bytes = int(row[0] or 0)
            rows.append({"table": table, "max_row_bytes": max_bytes})
        except sqlite3.DatabaseError:
            continue
    rows.sort(key=lambda r: r["max_row_bytes"], reverse=True)
    return rows[:top]


def sample(conn: sqlite3.Connection, db_path: Path) -> dict[str, Any]:
    page_count = conn.execute("PRAGMA page_count").fetchone()[0]
    page_size = conn.execute("PRAGMA page_size").fetchone()[0]
    freelist = conn.execute("PRAGMA freelist_count").fetchone()[0]
    auto_vacuum = conn.execute("PRAGMA auto_vacuum").fetchone()[0]
    return {
        "ts": _now(),
        "db_size_mb": round(_db_size_mb(db_path), 3),
        "page_count": page_count,
        "page_size": page_size,
        "freelist_count": freelist,
        "auto_vacuum_mode": auto_vacuum,  # 0=none 1=full 2=incremental
        "largest_rows": _largest_rows(conn),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="AIASK SQLite soak monitor (P1-5)")
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--duration-min", type=float, default=360.0)
    parser.add_argument("--interval-sec", type=float, default=300.0)
    parser.add_argument("--max-db-mb", type=float, default=100.0)
    parser.add_argument("--max-row-kb", type=float, default=256.0)
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args(argv)

    if not args.db.exists():
        print(f"ERROR: DB not found: {args.db}")
        return 1

    samples: list[dict[str, Any]] = []
    deadline = time.monotonic() + args.duration_min * 60.0
    max_row_bytes_allowed = args.max_row_kb * 1024

    print(f"[{_now()}] soak start: db={args.db} duration={args.duration_min}min interval={args.interval_sec}s")
    breaches: list[str] = []
    while True:
        # Open a fresh read-only connection per sample so we never hold a write lock.
        uri = f"file:{args.db}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=30)
        try:
            s = sample(conn, args.db)
        finally:
            conn.close()
        samples.append(s)

        if s["db_size_mb"] > args.max_db_mb:
            breaches.append(f"db_size {s['db_size_mb']}MB > {args.max_db_mb}MB at {s['ts']}")
        for r in s["largest_rows"]:
            if r["max_row_bytes"] > max_row_bytes_allowed:
                breaches.append(
                    f"row in {r['table']} = {r['max_row_bytes']}B > {int(max_row_bytes_allowed)}B at {s['ts']}"
                )

        print(
            f"[{s['ts']}] size={s['db_size_mb']}MB freelist={s['freelist_count']} "
            f"largest_row={s['largest_rows'][0] if s['largest_rows'] else 'n/a'}"
        )
        if time.monotonic() >= deadline:
            break
        time.sleep(args.interval_sec)

    # Dedup breaches (a persistent oversize row repeats every interval).
    unique_breaches = sorted(set(breaches))
    result = {
        "db": str(args.db),
        "started": samples[0]["ts"] if samples else None,
        "ended": samples[-1]["ts"] if samples else None,
        "sample_count": len(samples),
        "max_db_mb_observed": max((s["db_size_mb"] for s in samples), default=0.0),
        "thresholds": {"max_db_mb": args.max_db_mb, "max_row_kb": args.max_row_kb},
        "passed": not unique_breaches,
        "breaches": unique_breaches,
        "samples": samples,
    }

    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"wrote {args.out}")

    if unique_breaches:
        print("SOAK GATE FAILED:")
        for b in unique_breaches:
            print(f"  - {b}")
        return 1
    print(f"SOAK GATE PASSED: max size {result['max_db_mb_observed']}MB over {len(samples)} samples")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
