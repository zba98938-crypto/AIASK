"""Compact oversized Strategy Factory JSON fields in a SQLite database.

Default mode is dry-run. Apply mode writes a compacted copy and validates it;
the source database is only replaced when --replace is explicitly provided.
Raw large JSON is summarized in-place with hashes and size metadata.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import shutil
import sqlite3
import sys
from pathlib import Path
from typing import Any


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PACKAGE_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

try:
    from aiask_quant_core.storage.sqlite.strategy_factory_json_budget import (
        bounded_json_text,
        full_market_score_retention_runs,
        high_freq_snapshot_max_bytes,
        stable_json_hash,
    )
except Exception:  # pragma: no cover - standalone fallback for unusual packaging
    def stable_json_hash(value: Any) -> str:
        try:
            encoded = json.dumps(value or {}, ensure_ascii=False, sort_keys=True, default=str)
        except Exception:
            encoded = str(value)
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    def bounded_json_text(field_name: str, value: Any, *, max_bytes: int | None = None) -> str:
        limit = max(4096, int(max_bytes or 64 * 1024))
        original = json.dumps(value or {}, ensure_ascii=False, default=str)
        if len(original.encode("utf-8")) <= limit:
            return original
        fallback = {
            "storage_mode": "dropped_large_payload",
            "field_name": str(field_name or "unknown"),
            "truncated": True,
            "original_size_bytes": len(original.encode("utf-8")),
            "payload_hash": stable_json_hash(value),
            "top_level_keys": sorted(str(key) for key in list((value or {}).keys())[:50])
            if isinstance(value, dict)
            else [],
        }
        return json.dumps(fallback, ensure_ascii=False, default=str)

    def full_market_score_retention_runs() -> int:
        return 1

    def high_freq_snapshot_max_bytes() -> int:
        return 8 * 1024


JSON_LIMIT = 64 * 1024
PARAMS_LIMIT = 32 * 1024
SNAPSHOT_LIMIT = high_freq_snapshot_max_bytes()
STAGES_LIMIT = 128 * 1024

FIELD_LIMITS: dict[tuple[str, str], int] = {
    ("daily_snapshot_history", "factor_research"): JSON_LIMIT,
    ("strategies", "params"): PARAMS_LIMIT,
    ("strategy_status_events", "metadata"): JSON_LIMIT,
    ("strategy_domain_events", "payload"): JSON_LIMIT,
    ("strategy_task_runs", "payload"): JSON_LIMIT,
    ("strategy_task_runs", "result"): JSON_LIMIT,
    ("strategy_generation_experiments", "parameters"): JSON_LIMIT,
    ("strategy_generation_experiments", "strategy_spec"): JSON_LIMIT,
    ("strategy_generation_experiments", "evaluation"): JSON_LIMIT,
    ("strategy_generation_experiments", "result"): JSON_LIMIT,
    ("strategy_quality_reports", "summary"): JSON_LIMIT,
    ("strategy_quality_reports", "quality_gate"): JSON_LIMIT,
    ("strategy_quality_reports", "validation_report"): JSON_LIMIT,
    ("strategy_quality_reports", "risk_report"): JSON_LIMIT,
    ("strategy_quality_reports", "dedup_report"): JSON_LIMIT,
    ("strategy_quality_reports", "backtest_metrics"): JSON_LIMIT,
    ("strategy_quality_reports", "snapshot"): JSON_LIMIT,
    ("strategy_incubation_pipeline_snapshots", "blockers"): SNAPSHOT_LIMIT,
    ("strategy_incubation_pipeline_snapshots", "risk_flags"): SNAPSHOT_LIMIT,
    ("strategy_incubation_pipeline_snapshots", "summary"): SNAPSHOT_LIMIT,
    ("strategy_incubation_pipeline_snapshots", "metadata"): SNAPSHOT_LIMIT,
    ("governance_report_snapshots", "issues"): SNAPSHOT_LIMIT,
    ("governance_report_snapshots", "payload_jsonb"): SNAPSHOT_LIMIT,
    ("strategy_runtime_risk_snapshots", "blockers"): SNAPSHOT_LIMIT,
    ("strategy_runtime_risk_snapshots", "summary"): SNAPSHOT_LIMIT,
    ("strategy_runtime_risk_snapshots", "metadata"): SNAPSHOT_LIMIT,
    ("strategy_projection_snapshots", "projection"): SNAPSHOT_LIMIT,
    ("strategy_projection_snapshots", "metadata"): SNAPSHOT_LIMIT,
    ("strategy_closure_snapshots", "snapshot"): SNAPSHOT_LIMIT,
    ("strategy_closure_snapshots", "metadata"): SNAPSHOT_LIMIT,
    ("strategy_factory_run_artifacts", "payload_json"): JSON_LIMIT,
    ("strategy_factory_runs", "summary"): JSON_LIMIT,
    ("strategy_factory_runs", "stages"): STAGES_LIMIT,
    ("strategy_factory_runs", "snapshot_summary"): JSON_LIMIT,
    ("strategy_factory_topn_snapshots", "selection_rules"): JSON_LIMIT,
    ("strategy_factory_topn_snapshots", "constituents"): JSON_LIMIT,
    ("strategy_factory_topn_snapshots", "metadata"): JSON_LIMIT,
    ("strategy_factory_task_evidence", "evidence_payload"): JSON_LIMIT,
    ("strategy_execution_audit_snapshots", "verification"): JSON_LIMIT,
    ("strategy_execution_audit_snapshots", "acceptance"): JSON_LIMIT,
    ("strategy_execution_audit_snapshots", "audit_summary"): JSON_LIMIT,
    ("strategy_execution_audit_snapshots", "snapshot"): JSON_LIMIT,
    ("strategy_execution_audit_snapshots", "metadata"): JSON_LIMIT,
    ("factory_tasks", "payload_json"): JSON_LIMIT,
    ("factory_tasks", "artifact_refs_json"): JSON_LIMIT,
    ("factory_task_attempts", "result_json"): JSON_LIMIT,
}

FORCE_COMPACT_FIELDS = {
    ("daily_snapshot_history", "factor_research"),
    ("strategies", "params"),
    ("strategy_factory_runs", "summary"),
    ("strategy_factory_runs", "stages"),
    ("strategy_factory_runs", "snapshot_summary"),
    ("strategy_incubation_pipeline_snapshots", "metadata"),
    ("strategy_projection_snapshots", "projection"),
    ("strategy_projection_snapshots", "metadata"),
    ("strategy_closure_snapshots", "snapshot"),
    ("strategy_closure_snapshots", "metadata"),
}

COMPACT_STORAGE_MODES = {
    "compact_json",
    "dropped_large_payload",
    "inline_compact_json",
    "inline_compact_stage",
    "inline_fallback_summary",
}


def json_size_bytes(value: Any) -> int:
    try:
        return len(json.dumps(value or {}, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8"))
    except Exception:
        return 0


def quote_identifier(name: str) -> str:
    return '"' + str(name).replace('"', '""') + '"'


def quote_literal(path: Path) -> str:
    return "'" + str(path).replace("'", "''") + "'"


def table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone()
    return bool(row)


def column_exists(conn: sqlite3.Connection, table: str, column: str) -> bool:
    return any(row[1] == column for row in conn.execute(f"PRAGMA table_info({quote_identifier(table)})"))


def row_counts(conn: sqlite3.Connection) -> dict[str, int]:
    counts: dict[str, int] = {}
    for (table,) in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"):
        counts[str(table)] = int(conn.execute(f"SELECT COUNT(*) FROM {quote_identifier(table)}").fetchone()[0])
    return counts


def parse_json_cell(raw_text: Any) -> tuple[Any, bool]:
    if raw_text in (None, ""):
        return {}, True
    if not isinstance(raw_text, str):
        return raw_text, False
    try:
        return json.loads(raw_text), True
    except Exception:
        return {
            "storage_mode": "invalid_json_text",
            "truncated": True,
            "raw_text_size_bytes": len(raw_text.encode("utf-8")),
            "payload_hash": hashlib.sha256(raw_text.encode("utf-8")).hexdigest(),
        }, False


def already_compacted(value: Any, *, raw_size: int, limit: int) -> bool:
    if raw_size > limit:
        return False
    if isinstance(value, dict):
        mode = str(value.get("storage_mode") or "").strip()
        if mode in COMPACT_STORAGE_MODES:
            return True
        audit = value.get("_storage_audit")
        if isinstance(audit, dict) and str(audit.get("storage_mode") or "") in COMPACT_STORAGE_MODES:
            return True
    return False


def encode_bounded_cell(
    table: str,
    column: str,
    value: Any,
    *,
    raw_size: int,
    parsed: bool,
    limit: int,
) -> str:
    field_name = f"{table}.{column}"
    if parsed:
        return bounded_json_text(field_name, value or {}, max_bytes=limit)
    fallback = {
        "storage_mode": "dropped_large_payload",
        "field_name": field_name,
        "truncated": True,
        "original_size_bytes": int(raw_size),
        "payload_hash": stable_json_hash(value),
        "parse_error": True,
    }
    return json.dumps(fallback, ensure_ascii=False, default=str)


def compact_connection(
    conn: sqlite3.Connection,
    *,
    apply: bool,
    table_filter: str | None = None,
    column_filter: str | None = None,
) -> dict[str, Any]:
    report: dict[str, Any] = {
        "mode": "apply" if apply else "dry_run",
        "fields": [],
        "updated_cells": 0,
        "original_bytes": 0,
        "compacted_bytes": 0,
    }
    if table_filter:
        report["table_filter"] = table_filter
    if column_filter:
        report["column_filter"] = column_filter
    for (table, column), limit in FIELD_LIMITS.items():
        if table_filter and table != table_filter:
            continue
        if column_filter and column != column_filter:
            continue
        if not table_exists(conn, table) or not column_exists(conn, table, column):
            continue
        force_scan = (table, column) in FORCE_COMPACT_FIELDS
        field_report = {
            "table": table,
            "column": column,
            "limit_bytes": limit,
            "scanned_rows": 0,
            "changed_rows": 0,
            "original_bytes": 0,
            "compacted_bytes": 0,
            "top_offenders": [],
        }
        where = (
            "WHERE COALESCE(LENGTH(CAST({column} AS BLOB)), 0) > 0"
            if force_scan
            else "WHERE COALESCE(LENGTH(CAST({column} AS BLOB)), 0) > ?"
        )
        sql = (
            f"SELECT rowid, {quote_identifier(column)} "
            f"FROM {quote_identifier(table)} "
            f"{where.format(column=quote_identifier(column))}"
        )
        params: tuple[Any, ...] = () if force_scan else (limit,)
        offenders: list[dict[str, Any]] = []
        for rowid, raw_text in conn.execute(sql, params):
            field_report["scanned_rows"] += 1
            if raw_text in (None, ""):
                continue
            raw_size = len(raw_text.encode("utf-8")) if isinstance(raw_text, str) else json_size_bytes(raw_text)
            value, parsed = parse_json_cell(raw_text)
            if already_compacted(value, raw_size=raw_size, limit=limit):
                continue
            encoded = encode_bounded_cell(
                table,
                column,
                value,
                raw_size=raw_size,
                parsed=parsed,
                limit=limit,
            )
            compacted_size = len(encoded.encode("utf-8"))
            if encoded == raw_text and compacted_size <= limit:
                continue
            field_report["changed_rows"] += 1
            field_report["original_bytes"] += raw_size
            field_report["compacted_bytes"] += compacted_size
            offenders.append(
                {
                    "rowid": int(rowid),
                    "original_size_bytes": int(raw_size),
                    "compacted_size_bytes": int(compacted_size),
                    "payload_hash": stable_json_hash(value) if parsed else None,
                }
            )
            if apply:
                update_sql = (
                    f"UPDATE {quote_identifier(table)} "
                    f"SET {quote_identifier(column)}=? WHERE rowid=?"
                )
                conn.execute(update_sql, (encoded, rowid))
                if table == "strategy_factory_run_artifacts" and column == "payload_json":
                    try:
                        mode = str(json.loads(encoded or "{}").get("storage_mode") or "inline_compact_json")
                    except Exception:
                        mode = "inline_compact_json"
                    conn.execute(
                        f"UPDATE {quote_identifier(table)} SET storage_mode=? WHERE rowid=?",
                        (mode, rowid),
                    )
        if field_report["changed_rows"]:
            offenders.sort(key=lambda item: int(item["original_size_bytes"]), reverse=True)
            field_report["top_offenders"] = offenders[:5]
            report["fields"].append(field_report)
            report["updated_cells"] += field_report["changed_rows"]
            report["original_bytes"] += field_report["original_bytes"]
            report["compacted_bytes"] += field_report["compacted_bytes"]

    should_apply_score_retention = (
        table_filter is None
        or table_filter == "strategy_factory_full_market_scores"
    ) and column_filter is None
    retention_report = apply_full_market_retention(conn, apply=apply) if should_apply_score_retention else None
    if retention_report:
        report["full_market_retention"] = retention_report

    report["top_offending_fields"] = sorted(
        [
            {
                "table": item["table"],
                "column": item["column"],
                "changed_rows": item["changed_rows"],
                "original_bytes": item["original_bytes"],
                "compacted_bytes": item["compacted_bytes"],
            }
            for item in report["fields"]
        ],
        key=lambda item: int(item["original_bytes"]),
        reverse=True,
    )[:10]
    if apply:
        conn.commit()
    return report


def estimate_connection(
    conn: sqlite3.Connection,
    *,
    table_filter: str | None = None,
    column_filter: str | None = None,
) -> dict[str, Any]:
    report: dict[str, Any] = {
        "mode": "estimate_only",
        "estimated": True,
        "fields": [],
        "updated_cells": 0,
        "original_bytes": 0,
        "compacted_bytes": None,
    }
    if table_filter:
        report["table_filter"] = table_filter
    if column_filter:
        report["column_filter"] = column_filter
    for (table, column), limit in FIELD_LIMITS.items():
        if table_filter and table != table_filter:
            continue
        if column_filter and column != column_filter:
            continue
        if not table_exists(conn, table) or not column_exists(conn, table, column):
            continue
        table_q = quote_identifier(table)
        column_q = quote_identifier(column)
        size_expr = f"COALESCE(LENGTH(CAST({column_q} AS BLOB)), 0)"
        total_rows, non_empty_rows, total_bytes, oversized_rows, oversized_bytes = conn.execute(
            f"""
            SELECT
                COUNT(*),
                SUM(CASE WHEN {size_expr} > 0 THEN 1 ELSE 0 END),
                COALESCE(SUM({size_expr}), 0),
                SUM(CASE WHEN {size_expr} > ? THEN 1 ELSE 0 END),
                COALESCE(SUM(CASE WHEN {size_expr} > ? THEN {size_expr} ELSE 0 END), 0)
            FROM {table_q}
            """,
            (limit, limit),
        ).fetchone()
        oversized_rows = int(oversized_rows or 0)
        total_bytes = int(total_bytes or 0)
        oversized_bytes = int(oversized_bytes or 0)
        if oversized_rows <= 0 and total_bytes <= 0:
            continue
        top_offenders = [
            {"rowid": int(rowid), "original_size_bytes": int(size_bytes or 0)}
            for rowid, size_bytes in conn.execute(
                f"""
                SELECT rowid, {size_expr} AS size_bytes
                FROM {table_q}
                WHERE {size_expr} > ?
                ORDER BY size_bytes DESC
                LIMIT 5
                """,
                (limit,),
            )
        ]
        field_report = {
            "table": table,
            "column": column,
            "limit_bytes": limit,
            "scanned_rows": int(total_rows or 0),
            "non_empty_rows": int(non_empty_rows or 0),
            "changed_rows": oversized_rows,
            "estimated_oversized_rows": oversized_rows,
            "original_bytes": oversized_bytes,
            "estimated_total_bytes": total_bytes,
            "compacted_bytes": None,
            "top_offenders": top_offenders,
        }
        report["fields"].append(field_report)
        report["updated_cells"] += oversized_rows
        report["original_bytes"] += oversized_bytes

    should_apply_score_retention = (
        table_filter is None
        or table_filter == "strategy_factory_full_market_scores"
    ) and column_filter is None
    retention_report = apply_full_market_retention(conn, apply=False) if should_apply_score_retention else None
    if retention_report:
        report["full_market_retention"] = retention_report
    report["top_offending_fields"] = sorted(
        [
            {
                "table": item["table"],
                "column": item["column"],
                "changed_rows": item["changed_rows"],
                "original_bytes": item["original_bytes"],
                "estimated_total_bytes": item["estimated_total_bytes"],
            }
            for item in report["fields"]
        ],
        key=lambda item: int(item["original_bytes"]),
        reverse=True,
    )[:10]
    return report


def apply_full_market_retention(conn: sqlite3.Connection, *, apply: bool) -> dict[str, Any] | None:
    table = "strategy_factory_full_market_scores"
    if not table_exists(conn, table) or not column_exists(conn, table, "run_id"):
        return None
    retention_runs = max(1, int(full_market_score_retention_runs()))
    run_rows = conn.execute(
        f"""
        SELECT run_id, COUNT(*) AS row_count,
               MAX(COALESCE(as_of_date, '')) AS max_as_of_date,
               MAX(COALESCE(created_at, '')) AS max_created_at
        FROM {quote_identifier(table)}
        GROUP BY run_id
        ORDER BY max_as_of_date DESC, max_created_at DESC, run_id DESC
        """
    ).fetchall()
    keep = [str(row[0]) for row in run_rows[:retention_runs]]
    delete = [
        {
            "run_id": str(row[0]),
            "row_count": int(row[1] or 0),
            "max_as_of_date": row[2],
            "max_created_at": row[3],
        }
        for row in run_rows[retention_runs:]
    ]
    deleted_rows = sum(int(item["row_count"]) for item in delete)
    report = {
        "table": table,
        "retention_runs": retention_runs,
        "scanned_runs": len(run_rows),
        "kept_run_ids": keep,
        "deleted_runs": len(delete),
        "deleted_rows": deleted_rows,
        "deleted_run_samples": delete[:10],
    }
    if apply and deleted_rows > 0:
        placeholders = ", ".join("?" for _ in keep) if keep else ""
        if keep:
            conn.execute(
                f"DELETE FROM {quote_identifier(table)} WHERE run_id NOT IN ({placeholders})",
                keep,
            )
        else:
            conn.execute(f"DELETE FROM {quote_identifier(table)}")
    return report


def copy_sqlite_database(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    src = sqlite3.connect(source)
    dst = sqlite3.connect(target)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()


def gzip_sqlite_backup(source: Path, gzip_path: Path, *, force: bool) -> None:
    gzip_path.parent.mkdir(parents=True, exist_ok=True)
    if gzip_path.exists():
        if not force:
            raise FileExistsError(f"backup already exists: {gzip_path}")
        gzip_path.unlink()
    temp_backup = gzip_path.with_suffix("")
    if temp_backup.exists():
        if not force:
            raise FileExistsError(f"temporary backup already exists: {temp_backup}")
        temp_backup.unlink()
    copy_sqlite_database(source, temp_backup)
    with temp_backup.open("rb") as src, gzip.open(gzip_path, "wb") as dst:
        shutil.copyfileobj(src, dst)
    temp_backup.unlink(missing_ok=True)


def integrity_check(path: Path) -> str:
    conn = sqlite3.connect(path)
    try:
        return str(conn.execute("PRAGMA integrity_check").fetchone()[0])
    finally:
        conn.close()


def validate_row_counts(
    before_counts: dict[str, int],
    after_counts: dict[str, int],
    report: dict[str, Any],
) -> None:
    expected = dict(before_counts)
    retention = dict(report.get("full_market_retention") or {})
    deleted_rows = int(retention.get("deleted_rows") or 0)
    table = str(retention.get("table") or "")
    if deleted_rows and table in expected:
        expected[table] = max(0, int(expected.get(table) or 0) - deleted_rows)
    if expected != after_counts:
        diff = {
            table_name: {
                "before": before_counts.get(table_name),
                "expected": expected.get(table_name),
                "after": after_counts.get(table_name),
            }
            for table_name in sorted(set(before_counts) | set(after_counts))
            if expected.get(table_name) != after_counts.get(table_name)
        }
        raise RuntimeError(f"row count validation failed: {json.dumps(diff, ensure_ascii=False, default=str)}")


def compact_database(
    source: Path,
    *,
    apply: bool = False,
    output: Path | None = None,
    force: bool = False,
    replace: bool = False,
    table_filter: str | None = None,
    column_filter: str | None = None,
    estimate_only: bool = False,
) -> dict[str, Any]:
    source = source.resolve()
    if not source.exists():
        raise FileNotFoundError(source)
    if estimate_only and apply:
        raise ValueError("--estimate-only cannot be combined with --apply")

    source_size = source.stat().st_size
    if estimate_only:
        conn = sqlite3.connect(source)
        try:
            report = estimate_connection(
                conn,
                table_filter=table_filter,
                column_filter=column_filter,
            )
        finally:
            conn.close()
        report["source"] = str(source)
        report["output"] = None
        report["source_size_bytes"] = source_size
        return report

    if not apply:
        conn = sqlite3.connect(source)
        try:
            report = compact_connection(
                conn,
                apply=False,
                table_filter=table_filter,
                column_filter=column_filter,
            )
        finally:
            conn.close()
        report["source"] = str(source)
        report["output"] = None
        report["source_size_bytes"] = source_size
        return report

    output = (output or source.with_name(f"{source.stem}.compacted{source.suffix}")).resolve()
    if output == source:
        raise ValueError("output must differ from source unless --replace is used with a separate output")
    if output.exists():
        if not force:
            raise FileExistsError(f"output already exists: {output}")
        output.unlink()

    work = output.with_name(f"{output.stem}.work{output.suffix}")
    if work.exists():
        if not force:
            raise FileExistsError(f"work file already exists: {work}")
        work.unlink()

    copy_sqlite_database(source, work)
    conn = sqlite3.connect(source)
    try:
        before_counts = row_counts(conn)
    finally:
        conn.close()
    conn = sqlite3.connect(work)
    try:
        report = compact_connection(
            conn,
            apply=True,
            table_filter=table_filter,
            column_filter=column_filter,
        )
        after_counts = row_counts(conn)
        validate_row_counts(before_counts, after_counts, report)
        ok = str(conn.execute("PRAGMA integrity_check").fetchone()[0])
        if ok.lower() != "ok":
            raise RuntimeError(f"integrity_check failed for work db: {ok}")
        conn.execute(f"VACUUM INTO {quote_literal(output)}")
    finally:
        conn.close()

    output_integrity = integrity_check(output)
    if output_integrity.lower() != "ok":
        raise RuntimeError(f"integrity_check failed for output db: {output_integrity}")
    try:
        work.unlink(missing_ok=True)
    except PermissionError:
        report["work_cleanup_warning"] = f"could not remove locked work file: {work}"

    report["source"] = str(source)
    report["output"] = str(output)
    report["integrity_check"] = output_integrity
    report["source_size_bytes"] = source_size
    report["output_size_bytes"] = output.stat().st_size
    report["row_counts_before"] = before_counts
    report["row_counts_after"] = after_counts

    if replace:
        backup_gz = source.with_name(f"{source.name}.pre_json_compact.bak.gz")
        gzip_sqlite_backup(source, backup_gz, force=force)
        output.replace(source)
        report["replaced_source"] = True
        report["backup_path"] = str(backup_gz)
        report["final_source_size_bytes"] = source.stat().st_size
    return report


def cleanup_backups(directory: Path, *, keep_one_compressed: bool, force: bool) -> dict[str, Any]:
    directory = directory.resolve()
    candidates = sorted(
        [
            path
            for path in directory.glob("*.sqlite*")
            if path.is_file() and path.stat().st_size >= 1024 * 1024 * 1024
        ],
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    report = {"directory": str(directory), "candidates": [str(path) for path in candidates], "deleted": []}
    keep_path = candidates[0] if keep_one_compressed and candidates else None
    if keep_path is not None:
        gzip_path = keep_path.with_suffix(keep_path.suffix + ".gz")
        if not gzip_path.exists() or force:
            with keep_path.open("rb") as src, gzip.open(gzip_path, "wb") as dst:
                shutil.copyfileobj(src, dst)
        report["kept_compressed"] = str(gzip_path)
    for path in candidates:
        if path == keep_path:
            continue
        path.unlink()
        report["deleted"].append(str(path))
    return report


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("db_path", type=Path, help="SQLite database to inspect or compact")
    parser.add_argument("--table", help="only inspect/compact one allowlisted table")
    parser.add_argument("--column", help="only inspect/compact one allowlisted column")
    parser.add_argument("--estimate-only", action="store_true", help="estimate oversized fields with SQL length aggregates only")
    parser.add_argument("--apply", action="store_true", help="write a compacted copy")
    parser.add_argument("--output", type=Path, help="output compacted SQLite path")
    parser.add_argument("--force", action="store_true", help="overwrite existing output/work files")
    parser.add_argument("--replace", action="store_true", help="replace source after compacted output validates")
    parser.add_argument("--cleanup-backups", type=Path, help="explicit backup directory to clean")
    parser.add_argument("--keep-one-compressed", action="store_true", help="keep newest large backup as .gz")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    report = compact_database(
        args.db_path,
        apply=bool(args.apply),
        output=args.output,
        force=bool(args.force),
        replace=bool(args.replace),
        table_filter=args.table,
        column_filter=args.column,
        estimate_only=bool(args.estimate_only),
    )
    if args.cleanup_backups:
        if not args.apply:
            report["backup_cleanup"] = {
                "skipped": True,
                "reason": "backup cleanup requires --apply",
            }
        else:
            report["backup_cleanup"] = cleanup_backups(
                args.cleanup_backups,
                keep_one_compressed=bool(args.keep_one_compressed),
                force=bool(args.force),
            )
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
