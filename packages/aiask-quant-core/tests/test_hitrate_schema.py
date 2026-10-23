"""Behaviour tests for the WP-A/B/C schema additions.

The v3 plan corrected three v2 DDL mistakes; these tests lock the corrected
shape by executing the real schema and asserting on the resulting database,
not on the DDL text.
"""

from __future__ import annotations

import asyncio
import sqlite3

from aiask_quant_core.storage.sqlite import SQLiteAdapter


def _columns(path, table: str) -> set[str]:
    conn = sqlite3.connect(str(path))
    try:
        return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    finally:
        conn.close()


def _tables(path) -> set[str]:
    conn = sqlite3.connect(str(path))
    try:
        return {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        conn.close()


def _init(path) -> None:
    db = SQLiteAdapter(path=path)

    async def _run() -> None:
        try:
            await db.initialize()
        finally:
            await db.close()

    asyncio.run(_run())


def test_horizon_retry_columns_and_new_tables_exist(tmp_path) -> None:
    path = tmp_path / "hitrate_schema.sqlite3"
    _init(path)
    outcome_columns = _columns(path, "strategy_trade_prediction_outcomes")
    assert {"horizon_score_version", "retry_attempts", "last_retry_at"} <= outcome_columns
    assert {"prediction_executions", "calibration_models"} <= _tables(path)
    # The execution half of an ``ordered`` decision must be storable, not just
    # the decision itself.
    assert {"paper_order_id", "execution_result"} <= _columns(path, "prediction_executions")


def test_prediction_executions_action_is_constrained(tmp_path) -> None:
    path = tmp_path / "hitrate_executions.sqlite3"
    _init(path)
    conn = sqlite3.connect(str(path))
    try:
        conn.execute(
            "INSERT INTO prediction_executions (id, prediction_id, strategy_id, stock_code, action) "
            "VALUES ('e1', 'p1', 's1', '600519', 'abstained')"
        )
        conn.execute(
            "INSERT INTO prediction_executions (id, prediction_id, strategy_id, stock_code, action) "
            "VALUES ('e2', 'p1', 's1', '600519', 'ordered')"
        )
        try:
            conn.execute(
                "INSERT INTO prediction_executions (id, prediction_id, strategy_id, stock_code, action) "
                "VALUES ('e3', 'p1', 's1', '600519', 'maybe')"
            )
        except sqlite3.IntegrityError:
            pass
        else:
            raise AssertionError("an unknown action was accepted")
        conn.commit()
        rows = conn.execute("SELECT action FROM prediction_executions ORDER BY id").fetchall()
        assert [row[0] for row in rows] == ["abstained", "ordered"]
    finally:
        conn.close()


def test_calibration_model_round_trips(tmp_path) -> None:
    path = tmp_path / "hitrate_calibration.sqlite3"
    db = SQLiteAdapter(path=path)

    async def _run() -> dict:
        try:
            await db.initialize()
            return await db.save_calibration_model(
                {
                    "model_hash": "cm_test",
                    "strategy_family": "default",
                    "method": "platt",
                    "feature_version": "confidence_horizon_direction.v1",
                    "fit_start": "2025-01-02",
                    "fit_end": "2025-12-31",
                    "n_train": 300,
                    "n_holdout": 100,
                    "base_rate_train": 0.42,
                    "brier_holdout": 0.231,
                    "brier_baseline": 0.244,
                    "status": "active",
                    "reason_code": "gates_passed",
                    "params_json": '{"a": 1.0, "b": 0.0}',
                }
            )
        finally:
            await db.close()

    saved = asyncio.run(_run())
    assert saved["model_hash"] == "cm_test"
    assert saved["status"] == "active"
    assert saved["reason_code"] == "gates_passed"


def test_schema_is_idempotent_on_reinit(tmp_path) -> None:
    path = tmp_path / "hitrate_idempotent.sqlite3"
    _init(path)
    _init(path)
    assert {"prediction_executions", "calibration_models"} <= _tables(path)
    assert "retry_attempts" in _columns(path, "strategy_trade_prediction_outcomes")
