"""P1-5: tests for the operational-table retention tool.

These lock in the *safety* properties of scripts/ops/db_retention.py:
  - dry-run never deletes,
  - --apply honours the retention window and min-keep floor,
  - market-data / reference tables are refused (allowlist only),
  - a gzip backup is written before deletion.

The tests build a tiny throwaway SQLite DB so they never touch real data.
"""

from __future__ import annotations

import gzip
import importlib.util
import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
TOOL = REPO_ROOT / "scripts" / "ops" / "db_retention.py"


def _load_tool():
    spec = importlib.util.spec_from_file_location("db_retention", TOOL)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _make_db(path: Path, *, fresh: int, old: int) -> None:
    conn = sqlite3.connect(str(path))
    conn.execute(
        "CREATE TABLE strategy_task_runs (id INTEGER PRIMARY KEY, strategy_id TEXT, "
        "task_name TEXT, status TEXT, payload TEXT, result TEXT, started_at TEXT, completed_at TEXT)"
    )
    now = datetime.now(UTC).replace(tzinfo=None)
    rows = []
    for i in range(old):
        ts = (now - timedelta(days=400)).strftime("%Y-%m-%d %H:%M:%S")
        rows.append((f"s{i}", "t", "done", "{}", "{}", ts, ts))
    for i in range(fresh):
        ts = now.strftime("%Y-%m-%d %H:%M:%S")
        rows.append((f"s{i}", "t", "done", "{}", "{}", ts, ts))
    conn.executemany(
        "INSERT INTO strategy_task_runs (strategy_id, task_name, status, payload, result, started_at, completed_at) "
        "VALUES (?,?,?,?,?,?,?)",
        rows,
    )
    conn.commit()
    conn.close()


@pytest.fixture()
def tmp_db(tmp_path: Path) -> Path:
    db = tmp_path / "t.sqlite3"
    _make_db(db, fresh=10, old=100)
    return db


def test_dry_run_deletes_nothing(tmp_db: Path) -> None:
    tool = _load_tool()
    rc = tool.main(["--db", str(tmp_db), "--table", "strategy_task_runs", "--days", "30", "--min-keep", "5"])
    assert rc == 0
    conn = sqlite3.connect(str(tmp_db))
    remaining = conn.execute("SELECT COUNT(*) FROM strategy_task_runs").fetchone()[0]
    conn.close()
    assert remaining == 110, "dry-run must not delete any rows"


def test_apply_honours_window_and_min_keep(tmp_db: Path, tmp_path: Path) -> None:
    tool = _load_tool()
    backup_dir = tmp_path / "bk"
    rc = tool.main([
        "--db", str(tmp_db), "--table", "strategy_task_runs",
        "--days", "30", "--min-keep", "5", "--partition-keep", "0",
        "--apply", "--backup-dir", str(backup_dir),
    ])
    assert rc == 0
    conn = sqlite3.connect(str(tmp_db))
    remaining = conn.execute("SELECT COUNT(*) FROM strategy_task_runs").fetchone()[0]
    conn.close()
    # 10 fresh always kept; min_keep=5 < 10 so window dominates. Old rows (400d)
    # are deletable down to the min_keep floor. Result: only fresh rows remain.
    assert remaining == 10, f"expected only fresh rows to remain, got {remaining}"
    # backup written
    backups = list(backup_dir.glob("strategy_task_runs_retention_*.json.gz"))
    assert backups, "a gzip backup must be written before delete"
    with gzip.open(backups[0], "rt", encoding="utf-8") as fh:
        payload = json.load(fh)
    assert payload["row_count"] == 100


def test_min_keep_prevents_emptying(tmp_path: Path) -> None:
    tool = _load_tool()
    db = tmp_path / "t2.sqlite3"
    _make_db(db, fresh=0, old=50)  # all old
    rc = tool.main([
        "--db", str(db), "--table", "strategy_task_runs",
        "--days", "30", "--min-keep", "20", "--partition-keep", "0",
        "--apply", "--no-backup",
    ])
    assert rc == 0
    conn = sqlite3.connect(str(db))
    remaining = conn.execute("SELECT COUNT(*) FROM strategy_task_runs").fetchone()[0]
    conn.close()
    assert remaining == 20, f"min_keep floor must be honoured even when all rows are old, got {remaining}"


def test_partition_keep_preserves_latest_per_strategy(tmp_path: Path) -> None:
    tool = _load_tool()
    db = tmp_path / "partition.sqlite3"
    conn = sqlite3.connect(str(db))
    conn.execute(
        "CREATE TABLE strategy_task_runs (id INTEGER PRIMARY KEY, strategy_id TEXT, "
        "task_name TEXT, status TEXT, payload TEXT, result TEXT, started_at TEXT, completed_at TEXT)"
    )
    now = datetime.now(UTC).replace(tzinfo=None)
    old_ts = (now - timedelta(days=400)).strftime("%Y-%m-%d %H:%M:%S")
    rows = [
        ("same", "t", "done", "{}", "{}", old_ts, old_ts),
        ("same", "t", "done", "{}", "{}", old_ts, old_ts),
        ("same", "t", "done", "{}", "{}", old_ts, old_ts),
        ("single", "t", "done", "{}", "{}", old_ts, old_ts),
    ]
    conn.executemany(
        "INSERT INTO strategy_task_runs (strategy_id, task_name, status, payload, result, started_at, completed_at) "
        "VALUES (?,?,?,?,?,?,?)",
        rows,
    )
    conn.commit()
    conn.close()

    rc = tool.main([
        "--db", str(db), "--table", "strategy_task_runs",
        "--days", "30", "--min-keep", "0", "--partition-keep", "1",
        "--apply", "--no-backup",
    ])
    assert rc == 0
    conn = sqlite3.connect(str(db))
    remaining = conn.execute(
        "SELECT strategy_id, COUNT(*) FROM strategy_task_runs GROUP BY strategy_id ORDER BY strategy_id"
    ).fetchall()
    conn.close()
    assert remaining == [("same", 1), ("single", 1)]


def test_protected_table_is_refused(tmp_db: Path) -> None:
    tool = _load_tool()
    rc = tool.main(["--db", str(tmp_db), "--table", "kline_1d"])
    assert rc == 1, "market-data tables must not be eligible for retention"
