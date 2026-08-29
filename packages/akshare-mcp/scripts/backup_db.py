"""Backup SQLite tables to JSONL files."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path


def _default_db_path() -> Path:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
    from akshare_mcp.storage.sqlite.schema_base import default_sqlite_path

    return default_sqlite_path()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sqlite-path", default=str(_default_db_path()))
    parser.add_argument("--output-dir", default="backup")
    parser.add_argument("--tables", default="all", help="all or comma-separated table names")
    args = parser.parse_args()

    db_path = Path(args.sqlite_path).expanduser()
    output_dir = Path(args.output_dir).expanduser()
    output_dir.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    if args.tables == "all":
        tables = [
            row["name"]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name")
        ]
    else:
        tables = [item.strip() for item in args.tables.split(",") if item.strip()]

    for table in tables:
        target = output_dir / f"{table}.jsonl"
        with target.open("w", encoding="utf-8") as handle:
            for row in conn.execute(f"SELECT * FROM {table}"):
                handle.write(json.dumps(dict(row), ensure_ascii=False, default=str) + "\n")
        print(f"Backed up {table} -> {target}")


if __name__ == "__main__":
    main()
