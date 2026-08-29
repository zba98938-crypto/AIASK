#!/usr/bin/env python3
"""SQLite data quality audit for the AKShare MCP database."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path


def _default_db_path() -> Path:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
    from akshare_mcp.storage.sqlite.schema_base import default_sqlite_path

    return default_sqlite_path()


def _tables(conn: sqlite3.Connection) -> list[str]:
    return [
        row["name"]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name")
    ]


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sqlite-path", default=str(_default_db_path()))
    parser.add_argument("--output", default="data_quality_report.json")
    args = parser.parse_args()

    conn = sqlite3.connect(str(Path(args.sqlite_path).expanduser()))
    conn.row_factory = sqlite3.Row
    report = {"timestamp": datetime.now().isoformat(timespec="seconds"), "database": str(args.sqlite_path), "tables": {}, "issues": []}

    for table in _tables(conn):
        count = conn.execute(f"SELECT COUNT(*) AS count FROM {table}").fetchone()["count"]
        columns = _columns(conn, table)
        stats = {"count": int(count), "columns": len(columns)}
        for candidate in ("updated_at", "time", "trade_date", "created_at"):
            if candidate in columns:
                row = conn.execute(f"SELECT MIN({candidate}) AS min_value, MAX({candidate}) AS max_value FROM {table}").fetchone()
                stats[f"{candidate}_min"] = row["min_value"]
                stats[f"{candidate}_max"] = row["max_value"]
                break
        if count == 0:
            report["issues"].append({"level": "warning", "message": f"{table} is empty"})
        report["tables"][table] = stats

    Path(args.output).write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"Audited {len(report['tables'])} tables from {args.sqlite_path}")
    print(f"Report written to {args.output}")


if __name__ == "__main__":
    main()
