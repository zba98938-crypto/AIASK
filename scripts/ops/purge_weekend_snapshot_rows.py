#!/usr/bin/env python3
"""One-off: remove weekend-dated paper_nav / strategy_incubation_metrics rows.

ONE-OFF — delete this file after it has been applied to the runtime DB.

Why: before the 2026-10-25 market_session fix, runs on 2026-10-18 (Sun) and
2026-10-24 (Sat) labelled snapshots with the wall-clock date, a session absent
from kline_1d.  Both tables are recomputable daily snapshots with no dependent
rows, so the weekend rows are removed rather than relabelled.

Out of scope on purpose: strategy_signals / strategy_trade_predictions on the
same dates.  They carry filled paper_orders, paper_trades and scored outcomes;
deleting them would orphan real paper executions.

Safety:
* dry run by default; ``--apply`` is required to write
* ``--apply`` first writes a full backup via the SQLite online-backup API and
  verifies the affected row counts in the backup before deleting
* one transaction; the deleted counts must equal the dry-run counts or it
  rolls back
* paper_nav.daily_return of the first row after each removed date is
  recomputed against the row before it (it was computed against the weekend
  row), and every changed row is reported
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

WEEKEND = "strftime('%w', {col}) IN ('0', '6')"
TABLES = (("paper_nav", "nav_date"), ("strategy_incubation_metrics", "metric_date"))


def _default_db() -> Path:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "packages/aiask-quant-core/src"))
    from aiask_quant_core.runtime_paths import resolve_sqlite_path_with_source

    return Path(resolve_sqlite_path_with_source()[0])


def plan(con: sqlite3.Connection) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for table, col in TABLES:
        where = WEEKEND.format(col=col)
        out[table] = dict(con.execute(f"SELECT {col}, COUNT(*) FROM {table} WHERE {where} GROUP BY 1 ORDER BY 1").fetchall())
    return out


def nav_return_fixes(con: sqlite3.Connection) -> list[dict[str, Any]]:
    """Next-row daily_return recomputed as if the weekend row did not exist."""
    fixes = []
    weekend_rows = con.execute(
        f"SELECT account_id, nav_date FROM paper_nav WHERE {WEEKEND.format(col='nav_date')}"
    ).fetchall()
    for account_id, wdate in weekend_rows:
        nxt = con.execute(
            "SELECT id, nav_date, total_value, daily_return FROM paper_nav WHERE account_id=? AND nav_date>? "
            f"AND NOT {WEEKEND.format(col='nav_date')} ORDER BY nav_date LIMIT 1",
            (account_id, wdate),
        ).fetchone()
        if nxt is None:
            continue
        prev = con.execute(
            "SELECT total_value FROM paper_nav WHERE account_id=? AND nav_date<? "
            f"AND NOT {WEEKEND.format(col='nav_date')} ORDER BY nav_date DESC LIMIT 1",
            (account_id, wdate),
        ).fetchone()
        if prev is None or not prev[0]:
            continue
        new_ret = round((float(nxt[2]) - float(prev[0])) / float(prev[0]), 6)
        if nxt[3] is None or abs(float(nxt[3]) - new_ret) > 1e-9:
            fixes.append({"id": nxt[0], "account_id": account_id, "nav_date": nxt[1],
                          "old_daily_return": nxt[3], "new_daily_return": new_ret})
    return fixes


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    db = args.db or _default_db()

    ro = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    expected = plan(ro)
    fixes = nav_return_fixes(ro)
    ro.close()
    report: dict[str, Any] = {"db": str(db), "apply": args.apply, "to_delete": expected,
                              "daily_return_fixes": len(fixes), "daily_return_fix_sample": fixes[:5]}
    if not args.apply:
        print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
        return 0

    backup = db.with_name(f"{db.name}.pre-weekend-purge-{datetime.now():%Y%m%d%H%M%S}")
    src = sqlite3.connect(str(db))
    dst = sqlite3.connect(str(backup))
    src.backup(dst)
    dst.close()
    check = sqlite3.connect(f"file:{backup}?mode=ro", uri=True)
    if plan(check) != expected:
        print("backup verification failed; nothing deleted", file=sys.stderr)
        return 2
    check.close()
    report["backup"] = str(backup)

    try:
        src.execute("BEGIN IMMEDIATE")
        for fix in fixes:
            src.execute("UPDATE paper_nav SET daily_return=? WHERE id=?", (fix["new_daily_return"], fix["id"]))
        deleted = {}
        for table, col in TABLES:
            cur = src.execute(f"DELETE FROM {table} WHERE {WEEKEND.format(col=col)}")
            deleted[table] = cur.rowcount
        want = {t: sum(v.values()) for t, v in expected.items()}
        if deleted != want:
            raise RuntimeError(f"deleted {deleted} != planned {want}")
        src.commit()
    except Exception:
        src.rollback()
        raise
    finally:
        src.close()
    report["deleted"] = deleted
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
