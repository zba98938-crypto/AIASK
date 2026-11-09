import importlib.util
import json
import sqlite3
import subprocess
import sys
from datetime import date
from pathlib import Path

import pytest


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
    # This minimal fixture has no amount column, so amount quality is unknown.
    assert result["status"] == "partial"
    assert result["row_count"] == 1
    con = sqlite3.connect(db_path)
    detail = json.loads(con.execute(
        "SELECT detail FROM tdx_data_completeness WHERE data_key='kline_1d'"
    ).fetchone()[0])
    con.close()
    assert detail["delisted_with_history_excluded"] == 1
    assert detail["missing_codes"] == []


@pytest.mark.parametrize("exit_code", [0, 7])
def test_real_child_logs_heartbeat_and_terminal_evidence(tmp_path, monkeypatch, exit_code):
    sync = _sync_module()
    monkeypatch.setattr(sync, "RUNTIME_HOME", tmp_path)
    monkeypatch.setattr(sync, "STATUS", tmp_path / "status.json")
    monkeypatch.setattr(sync, "_trading_day", lambda day: True)
    monkeypatch.setattr(sync, "_record_completeness", lambda **kwargs: {"status": "partial", "sync_ok": kwargs["sync_ok"]})
    real_popen = subprocess.Popen
    def child(command, **kwargs):
        assert kwargs["env"]["BROKER_ALLOW_WRITE"] == "0"
        assert "--append-only" in command
        assert "--skip-ic" in command
        return real_popen([sys.executable, "-c", f"import sys; print('x'*10000); print('diagnostic',file=sys.stderr);sys.exit({exit_code})"], **kwargs)
    monkeypatch.setattr(sync.subprocess, "Popen", child)
    statuses = []
    original_status = sync._write_status
    def record(payload):
        statuses.append(payload)
        original_status(payload)
    monkeypatch.setattr(sync, "_write_status", record)
    result = sync.run_once(timeout_seconds=10)
    assert result["status"] == ("completed" if exit_code == 0 else "failed")
    assert result["returncode"] == exit_code
    assert len(Path(result["stdout_log"]).read_text().strip()) == 10000
    assert Path(result["stderr_log"]).read_text().strip() == "diagnostic"
    assert any(item.get("child_pid") == result["child_pid"] and item["status"] == "syncing" for item in statuses)
    assert list((Path(result["audit_dir"]) / "result").glob("*.json.gz"))
    assert result["completeness"]["sync_ok"] is (exit_code == 0)


def test_timeout_reaps_real_child_and_preserves_failure_evidence(tmp_path, monkeypatch):
    import os
    sync = _sync_module()
    monkeypatch.setattr(sync, "RUNTIME_HOME", tmp_path)
    monkeypatch.setattr(sync, "STATUS", tmp_path / "status.json")
    monkeypatch.setattr(sync, "_trading_day", lambda day: True)
    monkeypatch.setattr(sync, "_record_completeness", lambda **kwargs: {"status": "partial"})
    real_popen = subprocess.Popen
    monkeypatch.setattr(sync.subprocess, "Popen", lambda command, **kwargs: real_popen([sys.executable, "-c", "import time;time.sleep(30)"], **kwargs))
    result = sync.run_once(timeout_seconds=0.1)
    assert result["status"] == "failed"
    assert result["reason"] == "sync_timeout"
    with pytest.raises(ProcessLookupError):
        os.kill(result["child_pid"], 0)
    assert list((Path(result["audit_dir"]) / "result").glob("*.json.gz"))


def test_once_refuses_existing_daemon_lock(tmp_path, monkeypatch):
    import fcntl
    sync = _sync_module()
    monkeypatch.setattr(sync, "PID_PATH", tmp_path / "sync.pid")
    monkeypatch.setattr(sys, "argv", ["run_daily_market_sync.py", "--once"])
    with (tmp_path / "sync.lock").open("a") as owner:
        fcntl.flock(owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(SystemExit, match="already_running"):
            sync.main()


def test_nonnull_legacy_amount_requires_provenance(tmp_path, monkeypatch):
    sync = _sync_module()
    database = tmp_path / "market.sqlite3"
    monkeypatch.setenv("AIASK_SQLITE_PATH", str(database))
    import _daily_kline_ingest
    monkeypatch.setattr(_daily_kline_ingest, "closed_date", lambda: date(2026,11,5))
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE kline_1d(time TEXT,code TEXT,amount REAL)")
        connection.execute("CREATE TABLE stocks(stock_code TEXT,list_status TEXT)")
        connection.execute("CREATE TABLE tdx_data_completeness(data_key TEXT PRIMARY KEY,status TEXT,as_of_date TEXT,row_count INTEGER,detail TEXT,updated_at TEXT)")
        connection.execute("INSERT INTO stocks VALUES('000001','L')")
        connection.execute("INSERT INTO kline_1d VALUES('2026-11-05','000001',12345)")
    assert sync._record_completeness(sync_ok=True)["status"] == "partial"
    assert sync._record_completeness(sync_ok=True, verified_amounts={"2026-11-05": {"000001"}})["status"] == "ok"
    monkeypatch.setattr(_daily_kline_ingest, "closed_date", lambda: date(2026,11,6))
    assert sync._record_completeness(sync_ok=True, verified_amounts={"2026-11-05": {"000001"}})["status"] == "partial"
