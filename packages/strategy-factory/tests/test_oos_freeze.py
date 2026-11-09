"""Hermetic prospective registration tests; never use the operator database."""
from __future__ import annotations

import copy
from datetime import datetime, timezone
import importlib.util
import json
from pathlib import Path
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

from strategy_factory.application.candidate_archive import write_candidate_archive
from strategy_factory.application import oos_freeze
from strategy_factory.infrastructure.holdout_seals import json_digest


@pytest.fixture
def fixture_archive(tmp_path, monkeypatch):
    monkeypatch.setenv("AIASK_FACTORY_CANDIDATE_ARCHIVE_DIR", str(tmp_path / "archives"))
    # Other development tasks may edit shared sources while this unit suite is
    # running; manifest hashing itself is exercised separately below.
    monkeypatch.setattr(oos_freeze, "execution_code_identity", lambda: {"sha256": "source-version-A"})
    candidate = {
        "candidate_id": "candidate-1", "strategy_type": "ma_cross",
        "target_symbols": ["002415", "300308", "601888"],
        "params": {"fast_period": 6, "slow_period": 30, "full_research": list(range(3000))},
        "execution_assumptions": {"commission_rate": 0.00025, "slippage_bps": 5, "execution_price_policy": "next_close_legacy"},
    }
    reference = write_candidate_archive("run-1", "evaluated", {"candidates": [candidate]})
    return candidate, reference


def _freeze(database, reference, **kwargs):
    return oos_freeze.freeze_candidates_from_archive(database, reference, selection_data_end="2026-11-03", **kwargs)


def test_archive_to_seal_is_full_future_and_uses_actual_execution(tmp_path, fixture_archive):
    from strategy_factory.application.backtest_filter import BacktestFilter, _ensure_strategy_params
    from strategy_factory.application.candidate_contract import apply_resolved_candidate_envelope, build_factory_backtest_assumptions
    from strategy_factory.application.oos_replay import holdout_identity

    candidate, reference = fixture_archive
    database = tmp_path / "seals.sqlite3"
    before = datetime.now(timezone.utc)
    result = _freeze(database, reference)
    after = datetime.now(timezone.utc)
    assert result["created_count"] == 1 and result["measured_count"] == 0
    status = oos_freeze.pending_holdout_status(database, include_snapshots=True)
    seal = status["seals"][0]
    snapshot = seal["snapshot"]
    assert snapshot["final_candidate"] == candidate
    assert json_digest(snapshot) == seal["snapshot_sha256"]
    frozen = datetime.fromisoformat(seal["frozen_at_utc"].replace("Z", "+00:00"))
    assert before.replace(microsecond=0) <= frozen <= after
    assert seal["planned_start"] > frozen.astimezone(oos_freeze.ZoneInfo("Asia/Shanghai")).date().isoformat()
    assert len(snapshot["planned_sessions"]) == 20
    assert snapshot["calendar"]["mode"] == "maintained_holiday_fallback"
    assert snapshot["selection_information_as_of"] == seal["frozen_at_utc"]
    assert snapshot["selection_input_data_snapshots"] is None
    normalized = apply_resolved_candidate_envelope(candidate)
    expected = {
        **_ensure_strategy_params(normalized["strategy_type"], normalized["params"], research_task=normalized.get("research_task")),
        **build_factory_backtest_assumptions(normalized).to_backtest_kwargs(),
    }
    assert snapshot["effective_execution_params"] == expected
    assert snapshot["target_codes"] == BacktestFilter._resolve_backtest_plan(normalized)[1]
    assert snapshot["target_codes"] == ["002415", "300308", "601888"]
    assert expected["execution_price_policy"] == "next_close_legacy"
    for key, value in holdout_identity(normalized, expected).items():
        assert snapshot[key] == value
    assert seal["total_return"] is None and seal["quality_admission_granted"] is False
    assert status["maturity_evaluator_implemented"] is False


def test_retry_is_idempotent_and_updates_deletes_are_denied(tmp_path, fixture_archive):
    _, reference = fixture_archive
    database = tmp_path / "seals.sqlite3"
    first = _freeze(database, reference)
    retry = _freeze(database, reference)
    assert retry["created_count"] == 0 and retry["existing_count"] == 1
    for key in ("seal_id", "frozen_at_utc", "planned_start", "planned_end", "snapshot_sha256"):
        assert first["seals"][0][key] == retry["seals"][0][key]
    with sqlite3.connect(database) as connection:
        for sql in (
            "UPDATE strategy_holdout_seals SET planned_end='2099-01-01'",
            "DELETE FROM strategy_holdout_seals",
            # REPLACE deletes conflicts without firing DELETE triggers when
            # recursive_triggers is off (SQLite's default).
            "INSERT OR REPLACE INTO strategy_holdout_seals SELECT * FROM strategy_holdout_seals",
        ):
            with pytest.raises(sqlite3.IntegrityError, match="holdout_seal_immutable"):
                connection.execute(sql)
    assert oos_freeze.pending_holdout_status(database)["frozen_count"] == 1


def test_concurrent_registration_only_inserts_once(tmp_path, fixture_archive):
    _, reference = fixture_archive
    database = tmp_path / "seals.sqlite3"
    with ThreadPoolExecutor(max_workers=2) as workers:
        results = list(workers.map(lambda _: _freeze(database, reference), range(2)))
    assert sum(result["created_count"] for result in results) == 1
    assert len({result["seals"][0]["seal_id"] for result in results}) == 1
    assert oos_freeze.pending_holdout_status(database)["frozen_count"] == 1


@pytest.mark.parametrize("change", ["params", "cost", "target", "code", "protocol"])
def test_changed_experiment_never_reuses_or_overwrites_seal(tmp_path, fixture_archive, monkeypatch, change):
    candidate, reference = fixture_archive
    database = tmp_path / "seals.sqlite3"
    first = _freeze(database, reference)
    altered = copy.deepcopy(candidate)
    kwargs = {}
    if change == "params":
        altered["params"]["fast_period"] = 8
    elif change == "cost":
        altered["execution_assumptions"]["commission_rate"] = 0.001
    elif change == "target":
        altered["target_symbols"] = ["600519"]
    elif change == "code":
        monkeypatch.setattr(oos_freeze, "execution_code_identity", lambda: {"sha256": "source-version-B"})
    elif change == "protocol":
        kwargs["min_sessions"] = 21
    new_reference = write_candidate_archive("run-1", "evaluated", {"candidates": [altered]})
    second = _freeze(database, new_reference, **kwargs)
    assert second["created_count"] == 1
    assert first["seals"][0]["seal_id"] != second["seals"][0]["seal_id"]
    saved = oos_freeze.pending_holdout_status(database, include_snapshots=True)["seals"]
    original = next(seal for seal in saved if seal["seal_id"] == first["seals"][0]["seal_id"])
    assert original["snapshot"]["final_candidate"] == candidate


@pytest.mark.parametrize("marker", [{"_storage_audit": {"truncated": True}}, {"storage_mode": "dropped_large_payload"}, {"truncated_item_count": 3}])
def test_truncated_candidate_is_rejected_before_database_creation(tmp_path, fixture_archive, marker):
    candidate, _ = fixture_archive
    candidate["params"]["nested"] = marker
    reference = write_candidate_archive("run-1", "evaluated", {"candidates": [candidate]})
    database = tmp_path / "absent.sqlite3"
    with pytest.raises(ValueError, match="holdout_compact_or_truncated_input"):
        _freeze(database, reference)
    assert not database.exists()


def test_missing_invalid_and_oversize_archive_are_not_accepted(tmp_path, fixture_archive, monkeypatch):
    candidate, reference = fixture_archive
    database = tmp_path / "absent.sqlite3"
    for bad_reference in ({**reference, "complete": False}, {**reference, "phase": "generated"}):
        with pytest.raises(ValueError, match="holdout_complete_evaluated_archive_required"):
            _freeze(database, bad_reference)
    with pytest.raises(ValueError, match="holdout_candidate_id_not_found"):
        _freeze(database, reference, candidate_ids=["missing"])
    with pytest.raises(ValueError, match="candidate_archive_hash_mismatch"):
        _freeze(database, {**reference, "sha256": "bad"})
    monkeypatch.setattr(oos_freeze, "MAX_SNAPSHOT_BYTES", 100)
    with pytest.raises(ValueError, match="holdout_snapshot_size_limit_exceeded"):
        _freeze(database, reference)
    assert not database.exists()


def test_caller_cannot_backdate_freeze_or_set_historical_window(tmp_path, fixture_archive):
    _, reference = fixture_archive
    for field in ("frozen_at_utc", "frozen_at", "planned_start", "planned_end"):
        with pytest.raises(TypeError, match="unexpected keyword"):
            _freeze(tmp_path / "absent.sqlite3", reference, **{field: "2020-01-01"})
    with pytest.raises(ValueError, match="holdout_selection_data_end_in_future"):
        oos_freeze.freeze_candidates_from_archive(tmp_path / "future.sqlite3", reference, selection_data_end="2099-01-01")
    assert oos_freeze.pending_holdout_status(tmp_path / "future.sqlite3")["frozen_count"] == 0


def test_calendar_dates_and_utc_local_midnight_boundary():
    window = oos_freeze.plan_future_window("2026-11-04T07:00:00Z")
    assert window["planned_start"] == "2026-11-05" and window["planned_end"] == "2026-12-02"
    assert oos_freeze.plan_future_window("2026-11-04T16:00:00Z")["planned_start"] == "2026-11-06"
    assert oos_freeze.plan_future_window("2026-09-30T10:00:00Z")["planned_start"] == "2026-10-08"
    with pytest.raises(ValueError, match="holdout_minimum_twenty_sessions"):
        oos_freeze.plan_future_window("2026-11-04T07:00:00Z", 19)
    with pytest.raises(ValueError, match="holdout_calendar_year_unverified"):
        oos_freeze.plan_future_window("2026-12-31T07:00:00Z")


def test_status_never_initializes_missing_database_or_schema(tmp_path):
    absent = tmp_path / "absent.sqlite3"
    status = oos_freeze.pending_holdout_status(absent)
    assert status["reason"] == "holdout_seal_database_missing" and not absent.exists()
    database = tmp_path / "uninitialized.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE sentinel (id INTEGER)")
    before = database.read_bytes()
    assert oos_freeze.pending_holdout_status(database)["reason"] == "holdout_seal_table_missing"
    assert database.read_bytes() == before


def test_status_remains_unmeasured_after_planned_end(tmp_path, fixture_archive, monkeypatch):
    _, reference = fixture_archive
    database = tmp_path / "seals.sqlite3"
    _freeze(database, reference)
    from strategy_factory.infrastructure import holdout_seals
    monkeypatch.setattr(holdout_seals, "database_utc_now", lambda connection: "2099-01-01T10:00:00Z")
    status = oos_freeze.pending_holdout_status(database)
    assert status["measured_count"] == 0 and status["seals"][0]["total_return"] is None
    assert status["seals"][0]["reason"] == "holdout_maturity_evaluator_not_implemented"


def test_real_code_identity_hashes_working_sources():
    manifest = oos_freeze.execution_code_identity()
    assert manifest["source_sha256"]["strategy_factory/application/oos_freeze.py"]
    assert manifest["source_sha256"]["aiask_quant_core/backtest/engine.py"]
    assert manifest["source_sha256"]["aiask_quant_core/strategy_dsl.py"]
    assert manifest["source_sha256"]["aiask_quant_core/slippage.py"]
    assert manifest["sha256"] == json_digest({key: value for key, value in manifest.items() if key != "sha256"})


def test_cli_freeze_and_read_only_status(tmp_path, fixture_archive, capsys):
    _, reference = fixture_archive
    script = Path(__file__).resolve().parents[3] / "scripts/factories/seal_strategy_holdout.py"
    spec = importlib.util.spec_from_file_location("seal_cli", script)
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    reference_path, database = tmp_path / "reference.json", tmp_path / "cli.sqlite3"
    reference_path.write_text(json.dumps(reference), encoding="utf-8")
    assert cli.main(["freeze", "--db", str(database), "--archive-reference", str(reference_path), "--selection-data-end", "2026-11-03", "--all-candidates"]) == 0
    assert json.loads(capsys.readouterr().out)["created_count"] == 1
    assert cli.main(["status", "--db", str(database)]) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["pending_count"] == 1 and status["measured_count"] == 0
