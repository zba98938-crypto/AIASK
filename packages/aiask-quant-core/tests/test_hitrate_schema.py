"""Behaviour tests for the WP-A/B/C schema additions.

The v3 plan corrected three v2 DDL mistakes; these tests lock the corrected
shape by executing the real schema and asserting on the resulting database,
not on the DDL text.
"""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import date

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


def test_archived_paper_accounts_and_excluded_metrics_are_not_read_as_evidence(tmp_path) -> None:
    db = SQLiteAdapter(path=tmp_path / "paper_evidence_exclusions.sqlite3")

    async def _run() -> tuple[dict | None, list[dict]]:
        try:
            await db.initialize()
            await db.save_strategy(
                {
                    "id": "strategy-evidence-exclusion",
                    "name": "Evidence exclusion",
                    "status": "incubating",
                    "strategy_type": "momentum",
                }
            )
            await db.save_paper_account(
                {
                    "id": "archived-account",
                    "name": "Archived test account",
                    "strategy_id": "strategy-evidence-exclusion",
                    "account_type": "incubation",
                    "status": "archived",
                }
            )
            await db.save_strategy_incubation_account(
                "strategy-evidence-exclusion",
                "archived-account",
                status="archived",
            )
            account = await db.get_paper_account_by_strategy("strategy-evidence-exclusion")
            binding = await db.get_strategy_incubation_account("strategy-evidence-exclusion")
            await db.save_strategy_incubation_metric(
                "strategy-evidence-exclusion",
                "2026-10-27",
                {"account_id": "excluded-account", "nav": 1.01},
            )
            await db.save_strategy_incubation_metric(
                "strategy-evidence-exclusion",
                "2026-10-28",
                {"account_id": "live-account", "nav": 1.02},
            )
            async with db.acquire() as conn:
                await conn.execute(
                    "INSERT INTO paper_evidence_exclusions (account_id, reason_code) VALUES ($1, $2)",
                    "excluded-account",
                    "unverified_test_artifact",
                )
            metrics = await db.list_strategy_incubation_metrics(
                "strategy-evidence-exclusion", limit=10
            )
            return account, binding, metrics
        finally:
            await db.close()

    account, binding, metrics = asyncio.run(_run())
    assert account is None
    assert binding is None
    assert [metric["account_id"] for metric in metrics] == ["live-account"]


def test_future_dated_audit_and_closure_snapshots_are_not_returned(tmp_path) -> None:
    db = SQLiteAdapter(path=tmp_path / "future_evidence_labels.sqlite3")

    async def _run() -> tuple[dict | None, dict | None]:
        try:
            await db.initialize()
            await db.save_strategy(
                {
                    "id": "strategy-future-evidence",
                    "name": "Future evidence",
                    "status": "incubating",
                    "strategy_type": "momentum",
                }
            )
            async with db.acquire() as conn:
                for index in range(1000):
                    await conn.execute(
                        "INSERT INTO kline_1d (time, code, open, high, low, close, volume) "
                        "VALUES ('2026-10-23T15:00:00+08:00', $1, 1, 1, 1, 1, 1)",
                        f"{600000 + index:06d}",
                    )
            await db.upsert_execution_audit_snapshot(
                {
                    "strategy_id": "strategy-future-evidence",
                    "snapshot_id": "audit-ahead-of-bars",
                    "as_of": "2026-10-24",
                    "verdict": {"status": "passed", "reasons": [], "hard_gate_passed": True},
                }
            )
            await db.upsert_strategy_closure_snapshot(
                {
                    "strategy_id": "strategy-future-evidence",
                    "snapshot_type": "incubation_overview",
                    "snapshot_id": "closure-ahead-of-bars",
                    "as_of": "2026-10-29",
                    "snapshot": {"status": "incubating"},
                }
            )
            return (
                await db.get_latest_execution_audit_snapshot("strategy-future-evidence"),
                await db.get_latest_strategy_closure_snapshot("strategy-future-evidence"),
            )
        finally:
            await db.close()

    audit_snapshot, closure_snapshot = asyncio.run(_run())
    assert audit_snapshot is None
    assert closure_snapshot is None


def test_exclusions_invalidate_old_audit_and_closure_snapshots(tmp_path) -> None:
    db = SQLiteAdapter(path=tmp_path / "excluded_snapshot_evidence.sqlite3")

    async def _run() -> tuple[dict | None, dict | None]:
        try:
            await db.initialize()
            await db.save_strategy(
                {
                    "id": "strategy-excluded-snapshot",
                    "name": "Excluded snapshot",
                    "status": "incubating",
                    "strategy_type": "momentum",
                }
            )
            await db.save_paper_account(
                {
                    "id": "excluded-account",
                    "name": "Excluded evidence account",
                    "strategy_id": "strategy-excluded-snapshot",
                    "account_type": "incubation",
                    "status": "archived",
                }
            )
            await db.save_strategy_incubation_account(
                "strategy-excluded-snapshot",
                "excluded-account",
                status="archived",
            )
            async with db.acquire() as conn:
                for index in range(1000):
                    await conn.execute(
                        "INSERT INTO kline_1d (time, code, open, high, low, close, volume) "
                        "VALUES ('2026-10-23T15:00:00+08:00', $1, 1, 1, 1, 1, 1)",
                        f"{600000 + index:06d}",
                    )
            await db.upsert_execution_audit_snapshot(
                {
                    "strategy_id": "strategy-excluded-snapshot",
                    "snapshot_id": "audit-before-exclusion",
                    "as_of": "2026-10-23",
                    "verdict": {"status": "passed", "reasons": [], "hard_gate_passed": True},
                }
            )
            await db.upsert_strategy_closure_snapshot(
                {
                    "strategy_id": "strategy-excluded-snapshot",
                    "snapshot_type": "incubation_overview",
                    "snapshot_id": "closure-before-exclusion",
                    "as_of": "2026-10-23",
                    "snapshot": {"business_stage": "promotion_ready"},
                }
            )
            async with db.acquire() as conn:
                await conn.execute(
                    "INSERT INTO paper_evidence_exclusions (account_id, reason_code) "
                    "VALUES ($1, $2)",
                    "excluded-account",
                    "unverified_test_artifact",
                )

            stale_audit = await db.get_latest_execution_audit_snapshot("strategy-excluded-snapshot")
            stale_closure = await db.get_latest_strategy_closure_snapshot("strategy-excluded-snapshot")
            assert stale_audit is None
            assert stale_closure is None

            await db.upsert_execution_audit_snapshot(
                {
                    "strategy_id": "strategy-excluded-snapshot",
                    "snapshot_id": "audit-after-exclusion",
                    "as_of": "2026-10-23",
                    "verdict": {"status": "needs_attention", "reasons": [], "hard_gate_passed": False},
                }
            )
            await db.upsert_strategy_closure_snapshot(
                {
                    "strategy_id": "strategy-excluded-snapshot",
                    "snapshot_type": "incubation_overview",
                    "snapshot_id": "closure-after-exclusion",
                    "as_of": "2026-10-23",
                    "snapshot": {"business_stage": "incubating"},
                }
            )
            return (
                await db.get_latest_execution_audit_snapshot("strategy-excluded-snapshot"),
                await db.get_latest_strategy_closure_snapshot("strategy-excluded-snapshot"),
            )
        finally:
            await db.close()

    audit_snapshot, closure_snapshot = asyncio.run(_run())
    assert audit_snapshot is not None
    assert audit_snapshot["snapshot_id"] == "audit-after-exclusion"
    assert audit_snapshot["metadata"]["evidence_exclusion_ids"] == ["excluded-account"]
    assert closure_snapshot is not None
    assert closure_snapshot["snapshot_id"] == "closure-after-exclusion"
    assert closure_snapshot["metadata"]["evidence_exclusion_ids"] == ["excluded-account"]


def test_optional_exclusion_table_absence_keeps_legacy_readers_usable(tmp_path) -> None:
    db = SQLiteAdapter(path=tmp_path / "legacy_without_exclusion_table.sqlite3")

    async def _run() -> tuple[dict | None, dict | None, list[dict], dict | None, dict | None]:
        try:
            await db.initialize()
            await db.save_strategy(
                {
                    "id": "strategy-legacy-exclusions",
                    "name": "Legacy exclusion schema",
                    "status": "incubating",
                    "strategy_type": "momentum",
                }
            )
            await db.save_paper_account(
                {
                    "id": "legacy-active-account",
                    "name": "Legacy account",
                    "strategy_id": "strategy-legacy-exclusions",
                    "account_type": "incubation",
                    "status": "active",
                }
            )
            await db.save_strategy_incubation_account(
                "strategy-legacy-exclusions",
                "legacy-active-account",
                status="active",
            )
            await db.save_strategy_incubation_metric(
                "strategy-legacy-exclusions",
                "2026-10-23",
                {"account_id": "legacy-active-account", "nav": 0.95},
            )
            async with db.acquire() as conn:
                await conn.execute("DROP TABLE paper_evidence_exclusions")
            account = await db.get_paper_account_by_strategy("strategy-legacy-exclusions")
            binding = await db.get_strategy_incubation_account("strategy-legacy-exclusions")
            metrics = await db.list_strategy_incubation_metrics("strategy-legacy-exclusions")
            await db.upsert_execution_audit_snapshot(
                {
                    "strategy_id": "strategy-legacy-exclusions",
                    "snapshot_id": "legacy-audit",
                    "as_of": date.today().isoformat(),
                    "verdict": {"status": "passed", "reasons": [], "hard_gate_passed": True},
                }
            )
            await db.upsert_strategy_closure_snapshot(
                {
                    "strategy_id": "strategy-legacy-exclusions",
                    "snapshot_type": "incubation_overview",
                    "snapshot_id": "legacy-closure",
                    "as_of": date.today().isoformat(),
                    "snapshot": {"status": "incubating"},
                }
            )
            audit = await db.get_latest_execution_audit_snapshot("strategy-legacy-exclusions")
            closure = await db.get_latest_strategy_closure_snapshot("strategy-legacy-exclusions")
            return account, binding, metrics, audit, closure
        finally:
            await db.close()

    account, binding, metrics, audit, closure = asyncio.run(_run())
    assert account is not None and account["id"] == "legacy-active-account"
    assert binding is not None and binding["account_id"] == "legacy-active-account"
    assert [row["account_id"] for row in metrics] == ["legacy-active-account"]
    assert audit is not None and audit["snapshot_id"] == "legacy-audit"
    assert audit["metadata"]["evidence_exclusion_ids"] == []
    assert closure is not None and closure["snapshot_id"] == "legacy-closure"
    assert closure["metadata"]["evidence_exclusion_ids"] == []


def test_schema_is_idempotent_on_reinit(tmp_path) -> None:
    path = tmp_path / "hitrate_idempotent.sqlite3"
    _init(path)
    _init(path)
    assert {"prediction_executions", "calibration_models"} <= _tables(path)
    assert "retry_attempts" in _columns(path, "strategy_trade_prediction_outcomes")
