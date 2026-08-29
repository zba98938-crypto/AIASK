"""Dry-run first repair utility for Strategy Factory incubation warmup debt.

The script audits warmup accounts that have no signals, no orders/trades, and
no effective forward sample. In dry-run mode it only reports candidates. With
``--execute`` it can archive or move those accounts to diagnostic status. It
may also run the existing execution-audit acceptance backfill for strategies
that already have real paper/signal evidence; it never creates synthetic
orders, trades, or returns.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))
sys.path.insert(0, str(ROOT / "packages" / "akshare-mcp" / "src"))
sys.path.insert(0, str(ROOT / "packages" / "aiask-quant-core" / "src"))

from _integration_paths import default_sqlite_path


def _default_db_path() -> Path:
    return default_sqlite_path()


def _connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    return conn


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (name,),
    ).fetchone()
    return row is not None


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    if not _table_exists(conn, table):
        return set()
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _scan_candidates(conn: sqlite3.Connection, *, limit: int) -> list[dict[str, Any]]:
    if not _table_exists(conn, "strategy_incubation_accounts"):
        return []
    account_cols = _columns(conn, "strategy_incubation_accounts")
    created_expr = "a.created_at" if "created_at" in account_cols else "a.bound_at"
    metrics_join = ""
    metrics_cols = "0 AS total_signals, 0 AS total_orders, 0 AS total_trades, 0 AS effective_n_5d"
    if _table_exists(conn, "strategy_incubation_metrics"):
        metrics_join = """
        LEFT JOIN (
            SELECT m.*
            FROM strategy_incubation_metrics m
            JOIN (
                SELECT strategy_id, MAX(metric_date) AS metric_date
                FROM strategy_incubation_metrics
                GROUP BY strategy_id
            ) latest
              ON latest.strategy_id = m.strategy_id
             AND latest.metric_date = m.metric_date
        ) m ON m.strategy_id = a.strategy_id
        """
        metrics_cols = """
            COALESCE(m.total_signals, 0) AS total_signals,
            COALESCE(m.total_orders, 0) AS total_orders,
            COALESCE(m.total_trades, 0) AS total_trades,
            COALESCE(m.effective_n_5d, 0) AS effective_n_5d
        """
    sql = f"""
    SELECT
        a.id AS account_row_id,
        a.strategy_id,
        a.account_id,
        COALESCE(a.stage, '') AS stage,
        COALESCE(a.status, '') AS status,
        {created_expr} AS created_at,
        a.updated_at,
        {metrics_cols},
        COALESCE((SELECT COUNT(*) FROM strategy_signals s WHERE s.strategy_id = a.strategy_id), 0) AS signal_rows,
        COALESCE((
            SELECT COUNT(*)
            FROM strategy_signals s
            JOIN signal_forward_returns fr ON fr.signal_id = s.id
            WHERE s.strategy_id = a.strategy_id
        ), 0) AS forward_return_rows,
        COALESCE((SELECT COUNT(*) FROM paper_orders po WHERE po.strategy_id = a.strategy_id), 0) AS paper_order_rows,
        COALESCE((SELECT COUNT(*) FROM paper_trades pt WHERE pt.strategy_id = a.strategy_id), 0) AS paper_trade_rows,
        COALESCE((
            SELECT COUNT(*)
            FROM strategy_signal_evidence ev
            WHERE ev.strategy_id = a.strategy_id
        ), 0) AS signal_evidence_rows
    FROM strategy_incubation_accounts a
    {metrics_join}
    WHERE COALESCE(a.stage, '') = 'warmup'
      AND COALESCE(a.status, '') = 'active'
    ORDER BY a.updated_at ASC
    LIMIT ?
    """
    rows = [dict(row) for row in conn.execute(sql, (max(1, int(limit)),)).fetchall()]
    candidates: list[dict[str, Any]] = []
    for row in rows:
        no_runtime_evidence = (
            int(row.get("total_signals") or 0) <= 0
            and int(row.get("total_orders") or 0) <= 0
            and int(row.get("total_trades") or 0) <= 0
            and int(row.get("effective_n_5d") or 0) <= 0
            and int(row.get("signal_rows") or 0) <= 0
            and int(row.get("paper_order_rows") or 0) <= 0
            and int(row.get("paper_trade_rows") or 0) <= 0
        )
        if no_runtime_evidence:
            row["recommended_action"] = "archive"
            row["reason"] = "warmup_no_signals_orders_trades_or_effective_sample"
            candidates.append(row)
    return candidates


def _evidence_backfill_targets(conn: sqlite3.Connection, *, limit: int) -> list[str]:
    if not _table_exists(conn, "strategy_incubation_accounts"):
        return []
    rows = conn.execute(
        """
        SELECT DISTINCT a.strategy_id
        FROM strategy_incubation_accounts a
        WHERE COALESCE(a.status, '') = 'active'
          AND COALESCE(a.stage, '') IN ('warmup', 'paper', 'incubating')
          AND (
            EXISTS (SELECT 1 FROM strategy_signals s WHERE s.strategy_id = a.strategy_id)
            OR EXISTS (SELECT 1 FROM paper_orders po WHERE po.strategy_id = a.strategy_id)
            OR EXISTS (SELECT 1 FROM paper_trades pt WHERE pt.strategy_id = a.strategy_id)
          )
        ORDER BY a.updated_at ASC
        LIMIT ?
        """,
        (max(1, int(limit)),),
    ).fetchall()
    return [str(row["strategy_id"]) for row in rows if str(row["strategy_id"] or "").strip()]


def _apply_governance(
    conn: sqlite3.Connection,
    candidates: list[dict[str, Any]],
    *,
    action: str,
) -> int:
    strategy_ids = [str(row.get("strategy_id") or "").strip() for row in candidates]
    strategy_ids = [sid for sid in strategy_ids if sid]
    if not strategy_ids:
        return 0
    account_cols = _columns(conn, "strategy_incubation_accounts")
    if action == "diagnostic":
        assignments = ["stage='diagnostic'", "updated_at=CURRENT_TIMESTAMP"]
        if "archived_reason" in account_cols:
            assignments.insert(1, "archived_reason='warmup_no_evidence_governance_diagnostic'")
        sql = f"""
        UPDATE strategy_incubation_accounts
        SET {", ".join(assignments)}
        WHERE strategy_id=?
          AND COALESCE(stage, '')='warmup'
          AND COALESCE(status, '')='active'
        """
    else:
        assignments = ["status='archived'", "updated_at=CURRENT_TIMESTAMP"]
        if "archived_reason" in account_cols:
            assignments.insert(1, "archived_reason='warmup_no_evidence_governance_archive'")
        sql = f"""
        UPDATE strategy_incubation_accounts
        SET {", ".join(assignments)}
        WHERE strategy_id=?
          AND COALESCE(stage, '')='warmup'
          AND COALESCE(status, '')='active'
        """
    changed = 0
    for sid in strategy_ids:
        changed += conn.execute(sql, (sid,)).rowcount
    conn.commit()
    return changed


async def _run_acceptance_backfill(db_path: Path, strategy_ids: list[str]) -> list[dict[str, Any]]:
    from aiask_quant_core.storage.sqlite import SQLiteAdapter

    db = SQLiteAdapter(path=db_path)
    await db.initialize()
    results: list[dict[str, Any]] = []
    try:
        for sid in strategy_ids:
            result = await db.run_execution_audit_acceptance(strategy_id=sid, backfill=True)
            results.append(
                {
                    "strategy_id": sid,
                    "status": result.get("status"),
                    "execution_audit_gate_status": result.get("execution_audit_gate_status"),
                    "execution_hard_gate_passed": bool(result.get("execution_hard_gate_passed")),
                    "backfill_result": result.get("backfill_result"),
                }
            )
    finally:
        await db.close()
    return results


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Audit and optionally govern warmup incubation debt.")
    parser.add_argument("--db", default=str(_default_db_path()), help="SQLite DB path")
    parser.add_argument("--limit", type=int, default=500, help="maximum warmup rows to scan")
    parser.add_argument("--execute", action="store_true", help="write governance changes")
    parser.add_argument("--action", choices=["archive", "diagnostic"], default="archive")
    parser.add_argument("--backfill-audit", action="store_true", help="run real execution-audit acceptance backfill for evidence-bearing strategies")
    parser.add_argument("--backfill-limit", type=int, default=50)
    parser.add_argument("--output", default=None, help="optional JSON report path")
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    db_path = Path(args.db).resolve()
    conn = _connect(db_path)
    try:
        candidates = _scan_candidates(conn, limit=args.limit)
        backfill_targets = _evidence_backfill_targets(conn, limit=args.backfill_limit)
        changed = 0
        if args.execute:
            changed = _apply_governance(conn, candidates, action=args.action)
    finally:
        conn.close()

    backfill_results: list[dict[str, Any]] = []
    backfill_error: dict[str, Any] | None = None
    if args.backfill_audit:
        try:
            backfill_results = asyncio.run(_run_acceptance_backfill(db_path, backfill_targets))
        except Exception as exc:  # noqa: BLE001 - keep dry-run/governance report usable
            backfill_error = {
                "error_type": type(exc).__name__,
                "error": str(exc),
            }

    report = {
        "db": str(db_path),
        "dry_run": not bool(args.execute),
        "action": args.action,
        "scanned_limit": int(args.limit),
        "candidate_count": len(candidates),
        "changed_count": changed,
        "candidates": candidates[:100],
        "backfill_audit_requested": bool(args.backfill_audit),
        "backfill_target_count": len(backfill_targets),
        "backfill_error": backfill_error,
        "backfill_results": backfill_results[:100],
    }
    text = json.dumps(report, ensure_ascii=False, indent=2)
    print(text)
    if args.output:
        Path(args.output).write_text(text + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
