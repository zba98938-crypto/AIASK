#!/usr/bin/env python
"""P1-5 remediation: operational-table retention / compaction.

Profiling the live ``akshare_mcp.sqlite3`` (2026-05-29) showed the DB is
~3.27 GB, but the bulk is legitimate market data (``kline_1d`` ~8.7M rows).
The *compactable* growth is unbounded operational/experiment JSON logs:

    strategy_incubation_pipeline_snapshots.metadata  ~14 GB
    strategy_task_runs.result                         ~4.8 GB
    strategy_quality_reports                          ~1.2 GB
    strategy_domain_events.payload                    ~0.8 GB

freelist is tiny, so a full VACUUM without first pruning/compacting reclaims
almost nothing. The fix is *retention* (age-based pruning of old operational
rows, while keeping newest rows per strategy/scope) plus vacuum/compaction.
Market-data tables are NEVER touched.

Design / safety:
  * DRY-RUN BY DEFAULT. Nothing is deleted unless ``--apply`` is passed.
  * Each retention rule keeps rows newer than N days (per table ``created_at``
    / ``started_at``), always keeps the most recent ``--min-keep`` rows, and
    can also keep the newest N rows per strategy/scope partition.
  * Before any delete with ``--apply`` it writes a gzip JSON backup of the
    rows to be removed under ``data/backups/`` (override with ``--backup-dir``;
    ``--no-backup`` to skip).
  * Reclaims space with ``PRAGMA incremental_vacuum`` (auto_vacuum is already
    INCREMENTAL). Use ``--full-vacuum`` only in a maintenance window.
  * Market-data / reference tables are hard-excluded; the tool refuses to
    operate on any table not in its allowlist.

Usage:
    python scripts/ops/db_retention.py                 # dry-run report
    python scripts/ops/db_retention.py --apply          # prune + incremental vacuum
    python scripts/ops/db_retention.py --table strategy_task_runs --days 30 --apply
    python scripts/ops/db_retention.py --table strategy_task_runs --partition-keep 0
"""

from __future__ import annotations

import argparse
import gzip
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = REPO_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from _integration_paths import default_sqlite_path, runtime_home

# Per-table retention policy. ts_col is the column used for age comparison.
# Only operational / experiment / event-log tables are eligible. Market-data
# and reference tables are intentionally absent and therefore protected.
RETENTION_RULES: dict[str, dict[str, Any]] = {
    "strategy_incubation_pipeline_snapshots": {
        "ts_col": "created_at",
        "days": 14,
        "min_keep": 5000,
        "partition_cols": ("strategy_id",),
        "partition_keep": 2,
    },
    "governance_report_snapshots": {
        "ts_col": "created_at",
        "days": 14,
        "min_keep": 5000,
        "partition_cols": ("scope_type", "scope_id"),
        "partition_keep": 2,
    },
    "strategy_runtime_risk_snapshots": {
        "ts_col": "created_at",
        "days": 14,
        "min_keep": 5000,
        "partition_cols": ("strategy_id",),
        "partition_keep": 2,
    },
    "strategy_projection_snapshots": {
        "ts_col": "created_at",
        "days": 14,
        "min_keep": 5000,
        "partition_cols": ("strategy_id", "projection_type"),
        "partition_keep": 2,
    },
    "strategy_task_runs": {
        "ts_col": "started_at",
        "days": 14,
        "min_keep": 5000,
        "partition_cols": ("strategy_id",),
        "partition_keep": 1,
    },
    "strategy_quality_reports": {
        "ts_col": "created_at",
        "days": 30,
        "min_keep": 5000,
        "partition_cols": ("strategy_id",),
        "partition_keep": 2,
    },
    "strategy_generation_experiments": {"ts_col": "created_at", "days": 60, "min_keep": 2000},
    "strategy_domain_events": {"ts_col": "created_at", "days": 60, "min_keep": 5000},
}

# Hard guard: tables that must never be pruned by this tool.
PROTECTED_TABLES = {
    "kline_1d", "factor_values", "stock_quotes", "financials", "margin_detail",
    "block_stocks", "tdx_relation", "tdx_gpjy_daily", "tdx_bkjy_daily",
    "tdx_consensus", "tdx_stock_extra", "strategies", "strategy_artifacts",
}


def _quote_identifier(name: str) -> str:
    return '"' + str(name).replace('"', '""') + '"'


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _resolve_db_path(explicit: str | None) -> Path:
    if explicit:
        return Path(explicit).expanduser().resolve()
    env = os.getenv("AIASK_SQLITE_PATH") or os.getenv("AKSHARE_MCP_SQLITE_PATH")
    if env:
        return Path(env).expanduser().resolve()
    return default_sqlite_path()


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    return row is not None


def _candidate_predicate(
    *,
    table: str,
    ts_col: str,
    days: int,
    keep_floor: int,
    partition_cols: tuple[str, ...] = (),
    partition_keep: int = 0,
) -> tuple[str, tuple[Any, ...]]:
    table_q = _quote_identifier(table)
    ts_q = _quote_identifier(ts_col)
    clauses = [
        f"datetime({ts_q}) < datetime('now', '-{int(days)} days')",
        "rowid <= ?",
    ]
    params: list[Any] = [keep_floor]
    if partition_cols and partition_keep > 0:
        partition_expr = ", ".join(_quote_identifier(col) for col in partition_cols)
        clauses.append(
            "rowid IN ("
            "SELECT rowid FROM ("
            f"SELECT rowid, ROW_NUMBER() OVER (PARTITION BY {partition_expr} "
            f"ORDER BY datetime({ts_q}) DESC, rowid DESC) AS _retention_rank "
            f"FROM {table_q}"
            ") WHERE _retention_rank > ?"
            ")"
        )
        params.append(int(partition_keep))
    return " AND ".join(clauses), tuple(params)


def _cutoff_id(conn: sqlite3.Connection, table: str, rule: dict[str, Any]) -> tuple[int, int]:
    """Return (delete_count, total_count) for rows older than `days` while
    always keeping the newest `min_keep` rows.

    Strategy: find the rowid boundary so that rows with rowid <= boundary are
    candidates for deletion only if (a) older than cutoff AND (b) not within
    the newest min_keep rows.
    """
    ts_col = str(rule["ts_col"])
    days = int(rule["days"])
    min_keep = int(rule["min_keep"])
    total = conn.execute(f"SELECT COUNT(*) FROM {_quote_identifier(table)}").fetchone()[0]
    if total == 0:
        return 0, 0
    keep_floor_row = conn.execute(
        f"SELECT rowid FROM {_quote_identifier(table)} ORDER BY rowid DESC LIMIT 1 OFFSET ?",
        (min_keep,),
    ).fetchone()
    if keep_floor_row is None:
        return 0, total
    where_sql, params = _candidate_predicate(
        table=table,
        ts_col=ts_col,
        days=days,
        keep_floor=int(keep_floor_row[0]),
        partition_cols=tuple(rule.get("partition_cols") or ()),
        partition_keep=int(rule.get("partition_keep") or 0),
    )
    deletable = conn.execute(
        f"SELECT COUNT(*) FROM {_quote_identifier(table)} WHERE {where_sql}",
        params,
    ).fetchone()[0]
    return deletable, total


def _delete_old(
    conn: sqlite3.Connection,
    table: str,
    rule: dict[str, Any],
    *,
    backup_path: Path | None,
) -> int:
    """Delete old rows. Returns number deleted. Writes backup first if asked."""
    ts_col = str(rule["ts_col"])
    days = int(rule["days"])
    min_keep = int(rule["min_keep"])
    keep_floor_row = conn.execute(
        f"SELECT rowid FROM {_quote_identifier(table)} ORDER BY rowid DESC LIMIT 1 OFFSET ?",
        (min_keep,),
    ).fetchone()
    if keep_floor_row is None:
        return 0  # fewer than min_keep rows: keep all
    where_sql, params = _candidate_predicate(
        table=table,
        ts_col=ts_col,
        days=days,
        keep_floor=int(keep_floor_row[0]),
        partition_cols=tuple(rule.get("partition_cols") or ()),
        partition_keep=int(rule.get("partition_keep") or 0),
    )
    count = conn.execute(
        f"SELECT COUNT(*) FROM {_quote_identifier(table)} WHERE {where_sql}",
        params,
    ).fetchone()[0]
    if count <= 0:
        return 0

    if backup_path is not None:
        cols = [d[0] for d in conn.execute(f"SELECT * FROM {_quote_identifier(table)} LIMIT 0").description]
        select_sql = f"SELECT rowid, * FROM {_quote_identifier(table)} WHERE {where_sql}"
        backup_path.parent.mkdir(parents=True, exist_ok=True)
        with gzip.open(backup_path, "wt", encoding="utf-8") as fh:
            fh.write("{")
            json.dump("table", fh)
            fh.write(":")
            json.dump(table, fh, ensure_ascii=False)
            fh.write(",")
            json.dump("exported_at", fh)
            fh.write(":")
            json.dump(_now(), fh)
            fh.write(",")
            json.dump("row_count", fh)
            fh.write(":")
            json.dump(int(count), fh)
            fh.write(",")
            json.dump("columns", fh)
            fh.write(":")
            json.dump(["rowid", *cols], fh, ensure_ascii=False)
            fh.write(",")
            json.dump("rows", fh)
            fh.write(":[")
            first = True
            for row in conn.execute(select_sql, params):
                if not first:
                    fh.write(",")
                json.dump(list(row), fh, ensure_ascii=False, default=str)
                first = False
            fh.write("]}")

    conn.execute(
        f"DELETE FROM {_quote_identifier(table)} WHERE {where_sql}",
        params,
    )
    return int(count)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="AIASK operational-table retention (P1-5)")
    parser.add_argument("--db", default=None, help="SQLite path (default: env or AIASK_RUNTIME_HOME)")
    parser.add_argument("--table", default=None, help="Limit to one table (must be in the retention allowlist)")
    parser.add_argument("--days", type=int, default=None, help="Override retention window for --table")
    parser.add_argument("--min-keep", type=int, default=None, help="Override min rows kept for --table")
    parser.add_argument(
        "--partition-keep",
        type=int,
        default=None,
        help="Override newest rows kept per strategy/scope partition; 0 disables partition retention",
    )
    parser.add_argument("--apply", action="store_true", help="Actually delete (default: dry-run)")
    parser.add_argument("--no-backup", action="store_true", help="Skip gzip backup before delete")
    parser.add_argument("--backup-dir", default=None, help="Backup directory (default: data/backups)")
    parser.add_argument("--full-vacuum", action="store_true", help="Run full VACUUM after apply (locks DB; maintenance window only)")
    args = parser.parse_args(argv)

    db_path = _resolve_db_path(args.db)
    if not db_path.exists():
        print(f"ERROR: DB not found: {db_path}")
        return 1
    if args.partition_keep is not None and args.partition_keep < 0:
        print("ERROR: --partition-keep must be >= 0")
        return 1

    rules = dict(RETENTION_RULES)
    if args.table:
        if args.table not in RETENTION_RULES:
            print(f"ERROR: {args.table!r} is not in the retention allowlist {sorted(RETENTION_RULES)}")
            return 1
        rule = dict(RETENTION_RULES[args.table])
        if args.days is not None:
            rule["days"] = args.days
        if args.min_keep is not None:
            rule["min_keep"] = args.min_keep
        if args.partition_keep is not None:
            rule["partition_keep"] = args.partition_keep
        rules = {args.table: rule}
    elif args.partition_keep is not None:
        rules = {
            table: {**rule, "partition_keep": args.partition_keep}
            for table, rule in RETENTION_RULES.items()
        }

    backup_dir = Path(args.backup_dir).resolve() if args.backup_dir else (runtime_home() / "data" / "backups")
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    mode = "APPLY" if args.apply else "DRY-RUN"
    print(f"[{_now()}] db_retention {mode}  db={db_path}")

    conn = sqlite3.connect(str(db_path), timeout=60)
    try:
        conn.execute("PRAGMA foreign_keys=ON")
        total_deleted = 0
        for table, rule in rules.items():
            if table in PROTECTED_TABLES:
                print(f"  SKIP protected table {table}")
                continue
            if not _table_exists(conn, table):
                print(f"  SKIP missing table {table}")
                continue
            deletable, total = _cutoff_id(conn, table, rule)
            partition = ""
            if rule.get("partition_cols") and int(rule.get("partition_keep") or 0) > 0:
                partition = (
                    f", keep newest {int(rule.get('partition_keep') or 0):,} per "
                    f"{'/'.join(str(col) for col in rule.get('partition_cols') or ())}"
                )
            print(
                f"  {table}: total={total:,} deletable={deletable:,} "
                f"(keep newest {rule['min_keep']:,}{partition} & < {rule['days']}d via {rule['ts_col']})"
            )
            if args.apply and deletable > 0:
                backup_path = None if args.no_backup else backup_dir / f"{table}_retention_{stamp}.json.gz"
                deleted = _delete_old(
                    conn, table, rule,
                    backup_path=backup_path,
                )
                conn.commit()
                total_deleted += deleted
                bk = f" backup={backup_path}" if backup_path else " (no backup)"
                print(f"    deleted {deleted:,} rows.{bk}")

        if args.apply and total_deleted > 0:
            if args.full_vacuum:
                print("  running full VACUUM (this locks the DB)...")
                conn.execute("VACUUM")
            else:
                print("  reclaiming pages via PRAGMA incremental_vacuum...")
                conn.execute("PRAGMA incremental_vacuum")
            conn.commit()

        page_count = conn.execute("PRAGMA page_count").fetchone()[0]
        page_size = conn.execute("PRAGMA page_size").fetchone()[0]
        print(f"  db main-file size now ~{page_count*page_size/1024/1024:.1f} MB")
    finally:
        conn.close()

    if not args.apply:
        print("DRY-RUN complete. Re-run with --apply to delete (a gzip backup is written first).")
    else:
        print(f"APPLY complete. total_deleted={total_deleted:,}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
