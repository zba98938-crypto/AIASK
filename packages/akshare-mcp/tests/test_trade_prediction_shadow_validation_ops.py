from __future__ import annotations

import importlib.util
import sqlite3
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT_PATH = REPO_ROOT / "scripts" / "ops" / "trade_prediction_shadow_validation.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("trade_prediction_shadow_validation_ops", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _create_source_db(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as conn:
        conn.executescript(
            """
            CREATE TABLE kline_intraday (
                code TEXT,
                period TEXT,
                timestamp TEXT,
                adjust TEXT,
                open REAL,
                high REAL,
                low REAL,
                close REAL,
                data_quality_status TEXT
            );
            CREATE TABLE strategy_trade_predictions (
                prediction_id TEXT PRIMARY KEY,
                strategy_id TEXT,
                stock_code TEXT,
                target_trading_date TEXT,
                prediction_status TEXT,
                contract_hash TEXT,
                contract_json TEXT,
                metadata TEXT
            );
            CREATE TABLE strategy_trade_prediction_outcomes (
                outcome_id TEXT PRIMARY KEY,
                prediction_id TEXT,
                strategy_id TEXT,
                stock_code TEXT,
                actual_trading_date TEXT,
                score_version TEXT,
                score_status TEXT,
                trade_prediction_score REAL,
                outcome_json TEXT,
                data_quality_status TEXT,
                metadata TEXT,
                calculated_at TEXT,
                created_at TEXT
            );
            INSERT INTO kline_intraday (
                code, period, timestamp, adjust, open, high, low, close, data_quality_status
            ) VALUES (
                '600000.SH', '5m', '2026-06-08 09:30:00', '', 10, 11, 9, 10.5, 'ok'
            );
            INSERT INTO strategy_trade_predictions (
                prediction_id, strategy_id, stock_code, target_trading_date,
                prediction_status, contract_hash, contract_json, metadata
            ) VALUES (
                'tp_1', 'strategy_1', '600000.SH', '2026-06-08',
                'frozen', 'hash_old',
                '{"family":"momentum","prediction_as_of":"2026-06-07T15:00:00"}',
                '{"regime":"trend_up"}'
            );
            INSERT INTO strategy_trade_prediction_outcomes (
                outcome_id, prediction_id, strategy_id, stock_code, actual_trading_date,
                score_version, score_status, trade_prediction_score, outcome_json,
                data_quality_status, metadata, calculated_at, created_at
            ) VALUES (
                'out_1', 'tp_1', 'strategy_1', '600000.SH', '2026-06-08',
                'trade_prediction_score_v2', 'ok', 0.72,
                '{"direction_hit":true,"target_touch":false,"factor":"momentum"}',
                'ok', '{"event":"earnings"}', '2026-06-08T15:05:00Z', '2026-06-08T15:05:00Z'
            );
            """
        )


def test_shadow_defaults_use_external_runtime_home(monkeypatch, tmp_path: Path) -> None:
    runtime_home = tmp_path / "runtime"
    monkeypatch.setenv("AIASK_RUNTIME_HOME", str(runtime_home))

    module = _load_module()

    assert module.DEFAULT_SHADOW_DB == runtime_home / "data" / "validation" / "trade_prediction_shadow.sqlite3"
    assert module.DEFAULT_REPORT_ROOT == runtime_home / "reports" / "ops" / "trade_prediction_shadow"
    assert module.REPO_ROOT not in module.DEFAULT_SHADOW_DB.parents
    assert module.REPO_ROOT not in module.DEFAULT_REPORT_ROOT.parents


def test_shadow_copy_refuses_to_overwrite_source_db(tmp_path: Path) -> None:
    module = _load_module()
    source_db = tmp_path / "source.sqlite3"
    _create_source_db(source_db)

    with pytest.raises(ValueError):
        module.copy_shadow_database(source_db, source_db, overwrite=True)


def test_shadow_copy_and_schema_check_use_required_tables(tmp_path: Path) -> None:
    module = _load_module()
    source_db = tmp_path / "source.sqlite3"
    shadow_db = tmp_path / "shadow.sqlite3"
    _create_source_db(source_db)

    copied = module.copy_shadow_database(source_db, shadow_db, overwrite=False)
    schema = module.check_shadow_schema(shadow_db)

    assert copied["status"] == "copied"
    assert copied["source_db"] != copied["shadow_db"]
    assert schema["status"] == "ok"
    assert schema["missing_tables"] == []


def test_shadow_env_forces_database_and_live_trade_safety(monkeypatch, tmp_path: Path) -> None:
    module = _load_module()
    shadow_db = tmp_path / "shadow.sqlite3"
    monkeypatch.setenv("LIVE_TRADING_ENABLED", "1")
    monkeypatch.setenv("LIVE_TRADING_ALLOW_WRITE", "1")
    monkeypatch.setenv("BROKER_ALLOW_WRITE", "1")
    monkeypatch.setenv("LIVE_TRADING_READ_ONLY", "0")

    disabled = module.build_shadow_env(shadow_db, toggles_enabled=False)
    enabled = module.build_shadow_env(shadow_db, toggles_enabled=True)

    assert disabled["AKSHARE_MCP_SQLITE_PATH"] == str(shadow_db)
    assert disabled["AIASK_SQLITE_PATH"] == str(shadow_db)
    assert disabled["LIVE_TRADING_ENABLED"] == "0"
    assert disabled["LIVE_TRADING_ALLOW_WRITE"] == "0"
    assert disabled["BROKER_ALLOW_WRITE"] == "0"
    assert disabled["LIVE_TRADING_READ_ONLY"] == "1"
    assert disabled["BROKER_READ_ONLY"] == "1"
    assert all(disabled[key] == "0" for key in module.TOGGLE_KEYS)
    assert all(enabled[key] == "1" for key in module.TOGGLE_KEYS)


def test_toggle_phase_matches_twenty_day_shadow_drill() -> None:
    module = _load_module()

    assert module.toggle_phase_for_day(1, "auto") is False
    assert module.toggle_phase_for_day(10, "auto") is False
    assert module.toggle_phase_for_day(11, "auto") is True
    assert module.toggle_phase_for_day(20, "auto") is True
    assert module.toggle_phase_for_day(1, "enabled") is True
    assert module.toggle_phase_for_day(20, "disabled") is False


def test_local_snapshot_reports_v2_coverage_and_hash_mutation(tmp_path: Path) -> None:
    module = _load_module()
    db_path = tmp_path / "shadow.sqlite3"
    _create_source_db(db_path)

    snapshot = module.collect_local_snapshot(
        db_path,
        previous_contract_hashes={"tp_1": "hash_before"},
    )

    assert snapshot["status"] == "ready"
    assert snapshot["sample_n"] == 1
    assert snapshot["v2_ok_count"] == 1
    assert snapshot["intraday_bar_count"] == 1
    assert snapshot["contract_hash_mutation_count"] == 1
    assert snapshot["contract_hash_mutations"][0]["prediction_id"] == "tp_1"
    assert snapshot["matrix"]["row_count"] >= 1


def test_local_snapshot_detects_duplicate_outcomes(tmp_path: Path) -> None:
    module = _load_module()
    db_path = tmp_path / "shadow.sqlite3"
    _create_source_db(db_path)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO strategy_trade_prediction_outcomes (
                outcome_id, prediction_id, strategy_id, stock_code, actual_trading_date,
                score_version, score_status, trade_prediction_score, outcome_json,
                data_quality_status, metadata, calculated_at, created_at
            ) VALUES (
                'out_2', 'tp_1', 'strategy_1', '600000.SH', '2026-06-08',
                'trade_prediction_score_v2', 'ok', 0.73, '{}',
                'ok', '{}', '2026-06-08T15:06:00Z', '2026-06-08T15:06:00Z'
            )
            """
        )

    snapshot = module.collect_local_snapshot(db_path)
    alerts = module.evaluate_snapshot_alerts({"local": snapshot})

    assert snapshot["duplicate_outcome_count"] == 1
    assert any(item["code"] == "duplicate_outcomes" for item in alerts)


def test_final_summary_keeps_not_ready_until_twenty_daily_snapshots() -> None:
    module = _load_module()
    snapshots = [
        {
            "label": "day_00_baseline",
            "local": {"sample_n": 0, "v2_ok_count": 0},
        },
        {
            "label": "day_01",
            "local": {"sample_n": 1, "v2_ok_count": 0, "duplicate_outcome_count": 0},
        },
    ]

    summary = module.summarize_snapshots(snapshots)

    assert summary["daily_snapshot_count"] == 1
    assert summary["conclusion"] == "not_ready_continue_soak"
    assert "only_1_daily_snapshots_collected" in summary["warnings"]
    assert "no_ok_trade_prediction_score_v2_samples" in summary["warnings"]
