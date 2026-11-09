"""Persisted seal-to-engine checks against hermetic SQLite fixtures only."""
from __future__ import annotations

from datetime import date, timedelta
import hashlib
import json
from pathlib import Path
import sqlite3

import pytest

from strategy_factory.application import oos_freeze, oos_maturity
from strategy_factory.application.candidate_archive import write_candidate_archive
from strategy_factory.infrastructure.holdout_seals import json_digest


@pytest.fixture
def experiment(tmp_path, monkeypatch):
    monkeypatch.setenv("AIASK_FACTORY_CANDIDATE_ARCHIVE_DIR", str(tmp_path / "archive"))
    monkeypatch.setattr(oos_freeze, "execution_code_identity", lambda: {"sha256": "fixture-source-A"})
    monkeypatch.setattr(oos_maturity, "execution_code_identity", lambda: {"sha256": "fixture-source-A"})
    # UTC Jan 4 is local Jan 5: replay must preserve the instant while comparing
    # selection and trade dates in the market's calendar.
    monkeypatch.setattr(oos_freeze, "database_utc_now", lambda connection: "2026-01-04T16:30:00Z")
    monkeypatch.setattr(oos_maturity, "database_utc_now", lambda connection: "2026-02-03T10:00:00Z")
    reference = write_candidate_archive("test-run", "evaluated", {"candidates": [{
        "candidate_id": "fixed-candidate", "strategy_type": "buy_and_hold", "params": {},
        "target_symbols": ["000001", "000002"],
        "execution_assumptions": {"commission_rate": .001, "slippage_bps": 5, "execution_price_policy": "next_open"},
    }]})
    database = tmp_path / "experiment.sqlite3"
    frozen = oos_freeze.freeze_candidates_from_archive(database, reference, selection_data_end="2026-01-05")
    seal_id = frozen["seals"][0]["seal_id"]
    view = oos_freeze.pending_holdout_status(database, include_snapshots=True)["seals"][0]
    snapshot = view["snapshot"]
    warmup, cursor = [], date(2026, 1, 5)
    while len(warmup) < 80:
        if cursor.weekday() < 5:
            warmup.append(cursor.isoformat())
        cursor -= timedelta(days=1)
    days = sorted(warmup) + snapshot["planned_sessions"]
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE kline_1d (time TEXT,code TEXT,open REAL,high REAL,low REAL,close REAL,volume INTEGER)")
        connection.executemany("INSERT INTO kline_1d VALUES (?,?,?,?,?,?,?)", [
            (day, code, 10 + i * .01, 10.01 + i * .01, 9.99 + i * .01, 10 + i * .01, 10000000)
            for code in snapshot["target_codes"] for i, day in enumerate(days)
        ])
    return database, seal_id, tmp_path / "evidence", snapshot


async def evaluate(experiment, **kwargs):
    database, seal_id, directory, _ = experiment
    return await oos_maturity.evaluate_sealed_holdout(database, seal_id, directory, **kwargs)


@pytest.mark.asyncio
async def test_pending_uses_database_time_even_with_complete_future_fixture(experiment, monkeypatch):
    monkeypatch.setattr(oos_maturity, "database_utc_now", lambda connection: "2026-02-02T15:00:00Z")
    result = await evaluate(experiment, replay=True)
    assert result["status"] == "sealed_pending"
    assert result["reason"] == "holdout_window_not_mature"
    assert result["total_return"] is None
    assert result["window_elapsed"] is False
    assert not (Path(result["evidence_directory"]) / "engine-results.json").exists()


@pytest.mark.asyncio
async def test_ready_requires_explicit_replay_and_preserves_inputs(experiment):
    result = await evaluate(experiment)
    assert result["status"] == "ready_unmeasured" and result["total_return"] is None
    assert result["quality_admission_granted"] is False
    assert len(result["coverage"]) == 2
    assert all(row["sessions"] == 20 for row in result["coverage"].values())
    directory = Path(result["evidence_directory"])
    raw = json.loads((directory / "raw-inputs.json").read_text())
    assert json_digest(raw) == result["raw_input_snapshot_sha256"]
    assert len(raw["000001"]) == 100
    assert not (directory / "engine-results.json").exists()


@pytest.mark.asyncio
async def test_replay_binds_real_seal_preserves_full_fills_and_does_not_write_db(experiment, monkeypatch):
    database, _, _, snapshot = experiment
    before = hashlib.sha256(database.read_bytes()).hexdigest()
    original_connect = sqlite3.connect
    observed = []

    def connect(*args, **kwargs):
        assert args[0].endswith("?mode=ro") and kwargs["uri"] is True
        connection = original_connect(*args, **kwargs)
        connection.set_trace_callback(observed.append)
        return connection

    monkeypatch.setattr(oos_maturity.sqlite3, "connect", connect)
    result = await evaluate(experiment, replay=True)
    assert result["status"] == "measured", result
    assert 0 < result["total_return"] < .02
    assert result["quality_admission_granted"] is False
    assert result["database_writes"] is False
    assert any(sql == "PRAGMA query_only=ON" for sql in observed)
    assert hashlib.sha256(database.read_bytes()).hexdigest() == before
    full = json.loads((Path(result["evidence_directory"]) / "engine-results.json").read_text())
    assert json_digest(full) == result["engine_results_sha256"]
    assert set(full) == set(snapshot["target_codes"])
    assert result["total_return"] == pytest.approx(sum(row["data"]["total_return"] for row in full.values()) / 2)
    for row in full.values():
        fills = row["data"]["fills"]
        assert fills[0]["time"][:10] == snapshot["planned_start"]
        assert fills[-1]["time"][:10] == snapshot["planned_end"]
        assert len(fills) == 2


@pytest.mark.asyncio
async def test_source_version_change_never_replays_old_seal(experiment, monkeypatch):
    monkeypatch.setattr(oos_maturity, "execution_code_identity", lambda: {"sha256": "fixture-source-B"})
    result = await evaluate(experiment, replay=True)
    assert result["reason"] == "holdout_execution_code_mismatch"
    assert result["total_return"] is None and result["execution_code_matches"] is False


@pytest.mark.asyncio
async def test_source_changes_during_replay_preserve_result_but_withhold_metric(experiment, monkeypatch):
    manifests = iter([{"sha256": "fixture-source-A"}, {"sha256": "fixture-source-B"}])
    monkeypatch.setattr(oos_maturity, "execution_code_identity", lambda: next(manifests))
    result = await evaluate(experiment, replay=True)
    assert result["reason"] == "holdout_execution_code_changed_during_replay"
    assert result["total_return"] is None and result["status"] == "unavailable"
    assert (Path(result["evidence_directory"]) / "engine-results.json").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation,reason", [
    ("DELETE FROM kline_1d WHERE code='000002' AND time='2026-01-12'", "holdout_calendar_incomplete_or_mismatched:000002"),
    ("UPDATE kline_1d SET time='2026-01-10' WHERE code='000002' AND time='2026-01-12'", "holdout_calendar_incomplete_or_mismatched:000002"),
    ("INSERT INTO kline_1d SELECT time || 'T15:00:00+08:00',code,open,high,low,close,volume FROM kline_1d WHERE code='000002' AND time='2026-01-12'", "holdout_duplicate_trade_date:000002:2026-01-12"),
    ("UPDATE kline_1d SET close=NULL WHERE code='000002' AND time='2026-01-12'", "holdout_invalid_bar:000002:2026-01-12:close"),
    ("UPDATE kline_1d SET high=1 WHERE code='000002' AND time='2026-01-12'", "holdout_invalid_bar:000002:2026-01-12:ohlc_order"),
    ("DELETE FROM kline_1d WHERE code='000002' AND time<'2025-12-01'", "holdout_insufficient_warmup:000002"),
])
async def test_missing_or_conflicting_target_never_drops_target_or_moves_window(experiment, mutation, reason):
    with sqlite3.connect(experiment[0]) as connection:
        connection.execute(mutation)
    result = await evaluate(experiment, replay=True)
    assert result["status"] == "unavailable" and result["total_return"] is None
    assert result["reason"] == reason
    assert (Path(result["evidence_directory"]) / "raw-inputs.json").exists()


@pytest.mark.asyncio
async def test_missing_database_not_created_and_missing_seal_is_audited(tmp_path, experiment):
    missing = tmp_path / "absent.sqlite3"
    result = await oos_maturity.evaluate_sealed_holdout(missing, "none", tmp_path / "reports")
    assert result["status"] == "unavailable" and not missing.exists()
    result = await oos_maturity.evaluate_sealed_holdout(experiment[0], "none", experiment[2])
    assert result["reason"] == "holdout_seal_not_found"
    assert Path(result["report_file"]).is_file()


def test_cli_is_read_only_unless_replay_explicitly_selected(experiment, capsys):
    import importlib.util

    script = Path(__file__).resolve().parents[3] / "scripts/factories/check_strategy_holdout.py"
    spec = importlib.util.spec_from_file_location("holdout_check_cli", script)
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    database, seal_id, directory, _ = experiment
    args = ["--db", str(database), "--seal-id", seal_id, "--evidence-dir", str(directory)]
    assert cli.main(args) == 0
    checked = json.loads(capsys.readouterr().out)
    assert checked["status"] == "ready_unmeasured"
    assert cli.main([*args, "--replay"]) == 0
    measured = json.loads(capsys.readouterr().out)
    assert measured["status"] == "measured"
    assert measured["quality_admission_granted"] is False
