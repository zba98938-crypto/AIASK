import importlib.util
import json
import sqlite3
from datetime import date
from pathlib import Path


def _sync_module():
    path = Path(__file__).resolve().parents[3] / "scripts/factories/run_daily_market_sync.py"
    spec = importlib.util.spec_from_file_location("_daily_market_sync_test", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_completeness_without_universe_is_partial_even_above_legacy_threshold(tmp_path, monkeypatch):
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
    partial = sync._record_completeness(sync_ok=True)
    assert partial == {
        "data_key": "kline_1d",
        "status": "partial",
        "as_of_date": "2026-10-28",
        "row_count": 1000,
    }
    con = sqlite3.connect(db_path)
    detail = json.loads(con.execute(
        "SELECT detail FROM tdx_data_completeness WHERE data_key='kline_1d'"
    ).fetchone()[0])
    con.close()
    assert detail["coverage_basis"] == "observed_count_only"
    assert detail["coverage_unknown_reason"] == "stocks_universe_missing"
    assert detail["coverage_complete"] is False

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


def test_completeness_uses_active_universe_and_reports_missing_codes(tmp_path, monkeypatch):
    sync = _sync_module()
    db_path = tmp_path / "akshare_mcp.sqlite3"
    monkeypatch.setenv("AIASK_SQLITE_PATH", str(db_path))
    con = sqlite3.connect(db_path)
    con.execute("CREATE TABLE kline_1d(time TEXT, code TEXT)")
    con.execute("CREATE TABLE stocks(stock_code TEXT, list_status TEXT)")
    con.execute("CREATE TABLE tdx_data_completeness(data_key TEXT PRIMARY KEY,status TEXT,as_of_date TEXT,row_count INTEGER,detail TEXT,updated_at TEXT)")
    con.executemany("INSERT INTO stocks VALUES (?,?)", [
        ("000001", "L"), ("000002", "L"), ("000003", "L"), ("000004", "D"),
    ])
    con.executemany("INSERT INTO kline_1d VALUES (?,?)", [
        ("2026-10-30T15:00:00+08:00", "000001"),
        ("2026-10-30T15:00:00+08:00", "000002"),
        ("2026-10-30T15:00:00+08:00", "sh000001"),
    ])
    con.commit()
    con.close()

    result = sync._record_completeness(sync_ok=True)
    assert result == {
        "data_key": "kline_1d",
        "status": "partial",
        "as_of_date": "2026-10-30",
        "row_count": 2,
    }
    con = sqlite3.connect(db_path)
    detail = json.loads(con.execute(
        "SELECT detail FROM tdx_data_completeness WHERE data_key='kline_1d'"
    ).fetchone()[0])
    con.close()
    assert detail["active_universe_codes"] == 3
    assert detail["observed_codes"] == 3
    assert detail["coverage_codes"] == 2
    assert detail["missing_codes"] == ["000003"]
    assert detail["coverage_complete"] is False


def test_completeness_excludes_delisted_symbols_with_history(tmp_path, monkeypatch):
    sync = _sync_module()
    db_path = tmp_path / "akshare_mcp.sqlite3"
    monkeypatch.setenv("AIASK_SQLITE_PATH", str(db_path))
    con = sqlite3.connect(db_path)
    con.execute("CREATE TABLE kline_1d(time TEXT, code TEXT)")
    con.execute("CREATE TABLE stocks(stock_code TEXT, list_status TEXT)")
    con.execute("CREATE TABLE tdx_data_completeness(data_key TEXT PRIMARY KEY,status TEXT,as_of_date TEXT,row_count INTEGER,detail TEXT,updated_at TEXT)")
    con.executemany("INSERT INTO stocks VALUES (?,?)", [("000001", "L"), ("000004", "D")])
    con.executemany("INSERT INTO kline_1d VALUES (?,?)", [
        ("2026-10-30", "000001"),
        ("2026-10-30", "sh000001"),
        ("2026-10-29", "000004"),
    ])
    con.commit()
    con.close()

    result = sync._record_completeness(sync_ok=True)
    assert result["status"] == "ok"
    assert result["row_count"] == 1
    con = sqlite3.connect(db_path)
    detail = json.loads(con.execute(
        "SELECT detail FROM tdx_data_completeness WHERE data_key='kline_1d'"
    ).fetchone()[0])
    con.close()
    assert detail["delisted_with_history_excluded"] == 1
    assert detail["missing_codes"] == []
