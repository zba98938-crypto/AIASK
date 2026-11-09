"""Check real subprocess supervision and read-only diagnostic connections."""

import asyncio
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import sys
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[3]


def load_script(name):
    spec = importlib.util.spec_from_file_location(f"audit_{name}", ROOT / "scripts/factories" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.mark.asyncio
async def test_long_utf8_line_keeps_supervision_and_restart_alive(tmp_path, monkeypatch):
    module = load_script("run_three_factories")
    stop = asyncio.Event()
    state = module.FactoryState(name="synthetic")
    body = "量" * 70000
    spec = module.FactorySpec(
        name="synthetic", command=(sys.executable, "-c", f"print({body!r})"), log_name="synthetic.log",
    )

    async def finish_after_restart(event, _seconds):
        assert state.restart_count == 1
        event.set()

    monkeypatch.setattr(module, "_sleep_or_stop", finish_after_restart)
    await asyncio.wait_for(
        module._run_factory(spec, env=os.environ.copy(), log_dir=tmp_path, restart_delay_sec=1,
                            restart=True, quiet=True, stop_event=stop, state=state),
        timeout=10,
    )
    assert state.last_exit_code == 0 and state.restart_count == 1
    assert body in (tmp_path / "synthetic.log").read_text()
    records = [json.loads(line) for line in (tmp_path / "execution_records.jsonl").read_text().splitlines()]
    assert any(row["event_type"] == "factory_restart_scheduled" for row in records)


@pytest.mark.asyncio
async def test_supervisor_task_failure_stops_with_nonzero_verdict(tmp_path, monkeypatch):
    module = load_script("run_three_factories")
    args = module.parse_args([
        "--python", sys.executable, "--log-dir", str(tmp_path), "--no-incubation",
        "--no-factor", "--no-event-ingest", "--heartbeat-interval", "0",
    ])

    async def fail_manager(*args, **kwargs):
        raise OSError("synthetic reader failure")

    monkeypatch.setattr(module, "_run_factory", fail_manager)
    monkeypatch.setattr(module.signal, "signal", lambda *args: None)
    assert await asyncio.wait_for(module._run(args), timeout=5) == 1
    records = [json.loads(line) for line in (tmp_path / "execution_records.jsonl").read_text().splitlines()]
    failures = [r for r in records if r["event_type"] == "supervisor_task_failed"]
    assert failures and "synthetic reader failure" in failures[0]["error"]
    assert records[-1]["event_type"] == "supervisor_stopped"


def test_diagnostics_selects_explicit_db_and_cannot_write(tmp_path, monkeypatch):
    module = load_script("diagnose_factory_health")
    selected = tmp_path / "selected.sqlite3"
    other = tmp_path / "configured.sqlite3"
    for path, value in [(selected, "selected"), (other, "wrong")]:
        with sqlite3.connect(path) as conn:
            conn.execute("CREATE TABLE marker (value TEXT)")
            conn.execute("INSERT INTO marker VALUES (?)", (value,))
    before = selected.read_bytes()
    monkeypatch.setattr(module, "get_settings", lambda: SimpleNamespace(sqlite_path=str(other)))
    diagnostic = module.FactoryHealthDiagnostics(db_path=str(selected))
    assert diagnostic._connect_db()
    try:
        assert diagnostic._query_one("SELECT value FROM marker") == {"value": "selected"}
        with pytest.raises(sqlite3.DatabaseError, match="readonly"):
            diagnostic.conn.execute("DELETE FROM marker")
        assert diagnostic.conn.execute("PRAGMA query_only").fetchone()[0] == 1
    finally:
        diagnostic.conn.close()
    assert selected.read_bytes() == before


def test_diagnostics_does_not_create_a_missing_database(tmp_path):
    module = load_script("diagnose_factory_health")
    missing = tmp_path / "missing.sqlite3"
    diagnostic = module.FactoryHealthDiagnostics(db_path=str(missing))
    assert diagnostic._connect_db() is False
    assert not missing.exists()
    assert diagnostic.results["summary"]["blocked"] == 1
