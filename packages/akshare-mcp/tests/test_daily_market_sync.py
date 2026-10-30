import sqlite3
import importlib.util
from datetime import date
from pathlib import Path


def _sync_module():
    path = Path(__file__).resolve().parents[3] / "scripts/factories/run_daily_market_sync.py"
    spec = importlib.util.spec_from_file_location("_daily_market_sync_test", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_completeness_row_is_ok_or_partial_from_actual_coverage(tmp_path, monkeypatch):
    sync = _sync_module()

    runtime = tmp_path / "runtime"
    db_path = runtime / "data/db/akshare_mcp.sqlite3"
    db_path.parent.mkdir(parents=True)
    monkeypatch.setenv("AIASK_SQLITE_PATH", str(db_path))
    con = sqlite3.connect(db_path)
    con.execute("CREATE TABLE kline_1d(time TEXT, code TEXT)")
    con.execute("CREATE TABLE tdx_data_completeness(data_key TEXT PRIMARY KEY,status TEXT,as_of_date TEXT,row_count INTEGER,detail TEXT,updated_at TEXT)")
    con.executemany("INSERT INTO kline_1d VALUES (?,?)", [("2026-10-28T15:00:00+08:00", f"{i:06d}") for i in range(1000)])
    con.commit()
    con.close()
    monkeypatch.setattr(sync, "RUNTIME_HOME", runtime)
    ok = sync._record_completeness(sync_ok=True)
    assert ok == {"data_key": "kline_1d", "status": "ok", "as_of_date": "2026-10-28", "row_count": 1000}

    con = sqlite3.connect(db_path)
    con.execute("DELETE FROM kline_1d WHERE code >= '000500'")
    con.commit()
    con.close()
    partial = sync._record_completeness(sync_ok=True)
    assert partial["status"] == "partial"
    assert partial["row_count"] == 500


def test_failed_sync_cannot_stamp_completeness_ok(tmp_path, monkeypatch):
    sync = _sync_module()

    runtime = tmp_path / "runtime"
    db_path = runtime / "data/db/akshare_mcp.sqlite3"
    db_path.parent.mkdir(parents=True)
    monkeypatch.setenv("AIASK_SQLITE_PATH", str(db_path))
    con = sqlite3.connect(db_path)
    con.execute("CREATE TABLE kline_1d(time TEXT, code TEXT)")
    con.execute("CREATE TABLE tdx_data_completeness(data_key TEXT PRIMARY KEY,status TEXT,as_of_date TEXT,row_count INTEGER,detail TEXT,updated_at TEXT)")
    con.executemany("INSERT INTO kline_1d VALUES (?,?)", [("2026-10-28", f"{i:06d}") for i in range(1100)])
    con.commit(); con.close()
    monkeypatch.setattr(sync, "RUNTIME_HOME", runtime)
    assert sync._record_completeness(sync_ok=False)["status"] == "partial"
