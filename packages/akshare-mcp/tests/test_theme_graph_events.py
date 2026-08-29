"""PR-A smoke tests: theme graph schema + seed idempotency.

This test file is part of the event-driven theme-linkage upgrade plan
(see ``docs/event-driven/事件驱动主题联动-结合当前代码升级方案-2026-05-24.md`` Phase 0).

Design principle (Phase 0 verification line 1):
    "新建临时 SQLite 后,真实 adapter 初始化能创建全部事件驱动表"

That is, the test must drive the real ``SQLiteAdapter`` so that any future
DDL change is exercised. Earlier revisions of this file embedded raw
``CREATE TABLE`` strings copied from ``schema_strategy_parts/queries.py``.
That bypassed the very migration code we want to validate, so the
embedded DDL was removed. If you find yourself adding ``CREATE TABLE`` to
this file again, stop — extend the real schema module instead.
"""

from __future__ import annotations

import asyncio
import os
import sqlite3
import sys
from pathlib import Path

import pytest

# Make local ``akshare_mcp`` (compat shell that aliases the canonical
# ``aiask_quant_core.storage.sqlite`` package) and ``aiask_quant_core``
# both importable when this test is run via ``pytest`` from any cwd.
ROOT = Path(__file__).resolve().parents[1]
WORKSPACE_ROOT = Path(__file__).resolve().parents[3]
SRC = ROOT / "src"
QUANT_CORE_SRC = WORKSPACE_ROOT / "packages" / "aiask-quant-core" / "src"
for candidate in (SRC, QUANT_CORE_SRC):
    if candidate.exists() and str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

SCRIPTS_DIR = WORKSPACE_ROOT / "scripts"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

from _theme_graph_helpers import (
    EXPECTED_TABLES,
    FORBIDDEN_TABLES,
    LEGACY_TABLES,
    _build_adapter,
    _run,
    _seed_tdx_only_exposure_fixture,
    _table_columns,
)

def test_event_injection_crud_persists_approver_state(initialized_db: Path) -> None:
    """``upsert_event_injection`` must persist ``approver_id`` / ``approved_at``."""

    adapter = _build_adapter(initialized_db)

    async def scenario() -> tuple[list, list]:
        await adapter.initialize()
        try:
            await adapter.upsert_event_injection({
                "event_id": "test_evt_approver",
                "source": "manual",
                "event_name": "稀土出口管制",
                "event_type": "critical_minerals",
                "direction": "positive",
                "confidence": 0.9,
                "intensity": 0.85,
                "horizon": "swing_5_20d",
                "scope": "theme",
                "primary_themes": [{"theme_code": "rare_earth", "direction": "positive"}],
                "valid_from": "2026-05-24T00:00:00+00:00",
                "valid_until": "2026-06-13T00:00:00+00:00",
                "status": "pending_review",
                "operator_id": "operator_alice",
            })
            # Patch in approval state via a follow-up upsert (simulates handler).
            await adapter.upsert_event_injection({
                "event_id": "test_evt_approver",
                "source": "manual",
                "event_name": "稀土出口管制",
                "event_type": "critical_minerals",
                "primary_themes": [{"theme_code": "rare_earth", "direction": "positive"}],
                "confidence": 0.9,
                "intensity": 0.85,
                "horizon": "swing_5_20d",
                "scope": "theme",
                "valid_from": "2026-05-24T00:00:00+00:00",
                "valid_until": "2026-06-13T00:00:00+00:00",
                "status": "active",
                "operator_id": "operator_alice",
                "approver_id": "approver_bob",
                "approved_at": "2026-05-24T01:23:45+00:00",
            })
            pending = await adapter.list_event_injections(status="pending_review", limit=200)
            active = await adapter.list_event_injections(status="active", limit=200)
            return pending, active
        finally:
            await adapter.close()

    pending, active = _run(scenario())
    pending_ids = [e["event_id"] for e in pending]
    active_ids = [e["event_id"] for e in active]
    assert "test_evt_approver" not in pending_ids, "event must leave pending_review"
    assert "test_evt_approver" in active_ids, "event must end up active"
    target = next(e for e in active if e["event_id"] == "test_evt_approver")
    assert target.get("approver_id") == "approver_bob"
    assert target.get("approved_at") == "2026-05-24T01:23:45+00:00"
    assert target.get("operator_id") == "operator_alice"


def test_event_injection_does_not_lose_approver_on_partial_update(
    initialized_db: Path,
) -> None:
    """A later upsert without approver_id must NOT reset persisted approval."""

    adapter = _build_adapter(initialized_db)

    async def scenario() -> dict:
        await adapter.initialize()
        try:
            await adapter.upsert_event_injection({
                "event_id": "test_evt_approver_keep",
                "source": "manual",
                "event_name": "AI 算力补贴",
                "event_type": "ai_semiconductor",
                "primary_themes": [{"theme_code": "ai_compute", "direction": "positive"}],
                "confidence": 0.7,
                "intensity": 0.6,
                "horizon": "swing_5_20d",
                "valid_from": "2026-05-24T00:00:00+00:00",
                "valid_until": "2026-06-13T00:00:00+00:00",
                "status": "active",
                "operator_id": "operator_alice",
                "approver_id": "approver_bob",
                "approved_at": "2026-05-24T01:23:45+00:00",
            })
            # Operator later edits the rationale only — approver state must
            # survive. The DAO uses COALESCE on approver_id/approved_at, so
            # passing them as None should preserve the existing values.
            await adapter.upsert_event_injection({
                "event_id": "test_evt_approver_keep",
                "source": "manual",
                "event_name": "AI 算力补贴 (rev)",
                "event_type": "ai_semiconductor",
                "primary_themes": [{"theme_code": "ai_compute", "direction": "positive"}],
                "confidence": 0.72,
                "intensity": 0.6,
                "horizon": "swing_5_20d",
                "valid_from": "2026-05-24T00:00:00+00:00",
                "valid_until": "2026-06-13T00:00:00+00:00",
                "status": "active",
                "rationale": "added later",
            })
            rows = await adapter.list_event_injections(status="active", limit=200)
            return next(r for r in rows if r["event_id"] == "test_evt_approver_keep")
        finally:
            await adapter.close()

    row = _run(scenario())
    assert row["approver_id"] == "approver_bob"
    assert row["approved_at"] == "2026-05-24T01:23:45+00:00"
    assert "added later" in (row.get("rationale") or "")


def test_handle_factory_event_approve_writes_db_state(initialized_db: Path) -> None:
    """The manager handler must drive DAO so approval is durable, not just envelope."""

    from aiask_quant_core.storage.sqlite import SQLiteAdapter
    from akshare_mcp.tools.managers.strategy_mgr_factory_events import (
        handle_factory_event_approve,
        handle_factory_event_create,
        handle_factory_event_list,
    )

    adapter = SQLiteAdapter(path=initialized_db)

    async def scenario() -> dict:
        await adapter.initialize()
        try:
            create = await handle_factory_event_create(
                adapter,
                {
                    "event_id": "test_evt_handler",
                    "event_name": "霍尔木兹油运封锁",
                    "event_type": "energy_shipping",
                    "direction": "positive",
                    "confidence": 0.85,
                    "intensity": 0.9,  # >= 0.8 → pending_review
                    "scope": "theme",
                    "primary_themes": [{"theme_code": "shipping_trade", "direction": "positive"}],
                    "operator_id": "operator_alice",
                },
            )
            assert create["success"] is True
            assert create["data"]["status"] == "pending_review"

            # Self-approval must be rejected.
            self_approve = await handle_factory_event_approve(
                adapter,
                {"event_id": "test_evt_handler", "approver_id": "operator_alice"},
            )
            assert self_approve.get("success") is False
            assert "self-approve" in str(self_approve.get("error", ""))

            # Missing approver_id must be rejected.
            missing = await handle_factory_event_approve(
                adapter,
                {"event_id": "test_evt_handler"},
            )
            assert missing.get("success") is False

            ok = await handle_factory_event_approve(
                adapter,
                {"event_id": "test_evt_handler", "approver_id": "approver_bob"},
            )
            assert ok["success"] is True
            assert ok["data"]["status"] == "active"
            assert ok["data"]["approved_by"] == "approver_bob"
            assert ok["data"].get("approved_at")

            listed = await handle_factory_event_list(adapter, {"status": "active"})
            return listed
        finally:
            await adapter.close()

    listed = _run(scenario())
    rows = listed["data"]["events"]
    target = next(r for r in rows if r["event_id"] == "test_evt_handler")
    assert target["approver_id"] == "approver_bob"
    assert target["approved_at"], "approved_at should be persisted, not just returned"


def test_event_update_patch_and_source_alias_preserve_metadata(initialized_db: Path) -> None:
    """Status/approval patches must not blank event metadata; source alias works."""

    from akshare_mcp.tools.managers.strategy_mgr_factory_events import (
        handle_factory_event_approve,
        handle_factory_event_create,
        handle_factory_event_list,
        handle_factory_event_record_outcome,
        handle_factory_event_update,
    )

    adapter = _build_adapter(initialized_db)

    async def scenario() -> tuple[dict, dict, dict, dict, dict]:
        await adapter.initialize()
        try:
            created = await handle_factory_event_create(
                adapter,
                {
                    "event_id": "test_evt_patch_safe",
                    "event_name": "Patch safe event",
                    "event_type": "policy_shock",
                    "event_source": "news_llm",
                    "direction": "positive",
                    "confidence": 0.84,
                    "intensity": 0.86,
                    "primary_themes": [{"theme_code": "ai_compute", "direction": "positive"}],
                    "valid_from": "2026-05-24T00:00:00+00:00",
                    "valid_until": "2099-01-01T00:00:00+00:00",
                    "operator_id": "operator_alice",
                },
            )
            approved = await handle_factory_event_approve(
                adapter,
                {"event_id": "test_evt_patch_safe", "approver_id": "approver_bob"},
            )
            paused = await handle_factory_event_update(
                adapter,
                {"event_id": "test_evt_patch_safe", "action": "pause"},
            )
            outcome = await handle_factory_event_record_outcome(
                adapter,
                {
                    "event_id": "test_evt_patch_safe",
                    "actual_outcome": "mixed",
                    "outcome_notes": "mixed follow-through",
                },
            )
            listed = await handle_factory_event_list(
                adapter,
                {"event_source": "news_llm", "limit": 20},
            )
            return created, approved, paused, outcome, listed
        finally:
            await adapter.close()

    created, approved, paused, outcome, listed = _run(scenario())
    assert created["success"] is True
    assert approved["success"] is True
    assert paused["success"] is True
    assert outcome["success"] is True
    rows = listed["data"]["events"]
    target = next(row for row in rows if row["event_id"] == "test_evt_patch_safe")
    assert target["source"] == "news_llm"
    assert target["status"] == "paused"
    assert target["event_name"] == "Patch safe event"
    assert target["event_type"] == "policy_shock"
    assert target["primary_themes"] == [{"theme_code": "ai_compute", "direction": "positive"}]
    assert target["valid_from"] == "2026-05-24T00:00:00+00:00"
    assert target["valid_until"] == "2099-01-01T00:00:00+00:00"
    assert target["approver_id"] == "approver_bob"
    assert target["actual_outcome"] == "mixed"
    assert target["outcome_notes"] == "mixed follow-through"


def test_record_outcome_rejects_free_text_enum(initialized_db: Path) -> None:
    """Outcome contract must reject arbitrary text values."""

    from akshare_mcp.tools.managers.strategy_mgr_factory_events import (
        handle_factory_event_record_outcome,
    )

    adapter = _build_adapter(initialized_db)

    async def scenario() -> dict:
        await adapter.initialize()
        try:
            return await handle_factory_event_record_outcome(
                adapter,
                {
                    "event_id": "test_evt_outcome_enum",
                    "actual_outcome": "market rallied 4%",
                    "outcome_notes": "free text belongs in notes",
                },
            )
        finally:
            await adapter.close()

    response = _run(scenario())
    assert response["success"] is False
    assert "actual_outcome must be one of" in response["error"]


def test_record_outcome_updates_outcome_columns(initialized_db: Path) -> None:
    """record_outcome must set ``actual_outcome`` and ``outcome_notes`` durably."""

    from akshare_mcp.tools.managers.strategy_mgr_factory_events import (
        handle_factory_event_create,
        handle_factory_event_record_outcome,
    )

    adapter = _build_adapter(initialized_db)

    async def scenario() -> dict:
        await adapter.initialize()
        try:
            await handle_factory_event_create(
                adapter,
                {
                    "event_id": "test_evt_outcome",
                    "event_name": "黄金避险",
                    "event_type": "commodity_gold",
                    "direction": "positive",
                    "confidence": 0.7,
                    "intensity": 0.5,
                    "primary_themes": [{"theme_code": "gold", "direction": "positive"}],
                    "operator_id": "operator_carol",
                },
            )
            await handle_factory_event_record_outcome(
                adapter,
                {
                    "event_id": "test_evt_outcome",
                    "actual_outcome": "positive",
                    "outcome_notes": "黄金 ETF 上涨 4.2%",
                },
            )
            rows = await adapter.list_event_injections(limit=200)
            return next(r for r in rows if r["event_id"] == "test_evt_outcome")
        finally:
            await adapter.close()

    row = _run(scenario())
    assert row.get("actual_outcome") == "positive"
    assert "黄金 ETF" in (row.get("outcome_notes") or "")
    assert row.get("outcome_recorded_at"), "outcome_recorded_at must be set by DAO"


def test_upsert_theme_exposure_round_trip(initialized_db: Path) -> None:
    """Single-row upsert must round-trip and respect UNIQUE(symbol, theme_code)."""

    adapter = _build_adapter(initialized_db)

    async def scenario() -> list:
        await adapter.initialize()
        try:
            await adapter.upsert_theme_exposure({
                "symbol": "601857",
                "theme_code": "upstream_oil_gas",
                "exposure_score": 0.82,
                "industry_match_level": 2,
                "name_match_score": 0.6,
                "mainbz_match_score": 0.7,
                "historical_beta": 0.4,
                "evidence": {"src": "tdx_relation"},
            })
            # Second write with a higher score must update, not duplicate.
            await adapter.upsert_theme_exposure({
                "symbol": "601857",
                "theme_code": "upstream_oil_gas",
                "exposure_score": 0.91,
                "industry_match_level": 2,
                "evidence": {"src": "tdx_relation+kline"},
            })
            return await adapter.list_theme_exposure(
                theme_code="upstream_oil_gas", min_exposure=0.0, limit=10
            )
        finally:
            await adapter.close()

    rows = _run(scenario())
    matching = [r for r in rows if r["symbol"] == "601857"]
    assert len(matching) == 1, "UNIQUE(symbol, theme_code) must be enforced"
    assert abs(float(matching[0]["exposure_score"]) - 0.91) < 1e-6, "second write should overwrite"


def test_bulk_upsert_theme_exposure_idempotent(initialized_db: Path) -> None:
    """Bulk path must batch, accept the same payload twice, and return metrics."""

    adapter = _build_adapter(initialized_db)

    async def scenario() -> tuple[dict, dict, list]:
        await adapter.initialize()
        try:
            payload = [
                {"symbol": f"60{i:04d}", "theme_code": "upstream_oil_gas",
                 "exposure_score": 0.5 + (i % 5) * 0.1,
                 "industry_match_level": 1}
                for i in range(20)
            ]
            payload.append({"symbol": "", "theme_code": "skip_me"})  # invalid row
            r1 = await adapter.bulk_upsert_theme_exposure(payload, batch_size=7)
            r2 = await adapter.bulk_upsert_theme_exposure(payload, batch_size=7)
            rows = await adapter.list_theme_exposure(
                theme_code="upstream_oil_gas", min_exposure=0.0, limit=200
            )
            return r1, r2, rows
        finally:
            await adapter.close()

    r1, r2, rows = _run(scenario())
    assert r1["written"] == 20
    assert r1["skipped"] == 1
    assert r1["batch_count"] >= 3, "must split into multiple batches"
    # Second run is idempotent (overwrites in place, not duplicating).
    assert r2["written"] == 20
    distinct = {(r["symbol"], r["theme_code"]) for r in rows}
    assert len(distinct) == 20


def test_lineage_gate_update_partial(initialized_db: Path) -> None:
    """``update_event_task_lineage_gates`` must support partial patches.

    在 PR-D / PR-E 中 Gate-1 / Gate-2 / Gate-3 由不同处理阶段写回。
    DAO 不能在 Gate-2 上线时把 Gate-1 重置成 NULL。
    """

    adapter = _build_adapter(initialized_db)

    async def scenario() -> dict:
        await adapter.initialize()
        try:
            await adapter.upsert_event_task_lineage({
                "event_id": "test_evt_lineage",
                "task_id": "task_abc",
                "theme_code": "shipping_trade",
                "impact_direction": "positive",
                "impact_magnitude": 0.7,
                "target_symbols": ["601919", "600026"],
                "target_count": 2,
                "breadth_resolved": "narrow",
            })
            # Stage 1: Gate-1 passes.
            await adapter.update_event_task_lineage_gates(
                event_id="test_evt_lineage",
                task_id="task_abc",
                gate_1_passed=True,
            )
            # Stage 2: only Gate-2 fails — must NOT erase the Gate-1 win.
            await adapter.update_event_task_lineage_gates(
                event_id="test_evt_lineage",
                task_id="task_abc",
                gate_2_passed=False,
            )
            await adapter.update_event_task_lineage_gates(
                event_id="test_evt_lineage",
                task_id="task_abc",
                strategies_submitted=3,
            )
            async with adapter.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT gate_1_passed, gate_2_passed, gate_3_passed, "
                    "strategies_submitted FROM strategy_factory_event_task_lineage "
                    "WHERE event_id = $1 AND task_id = $2",
                    "test_evt_lineage",
                    "task_abc",
                )
            return dict(row) if row else {}
        finally:
            await adapter.close()

    row = _run(scenario())
    assert row.get("gate_1_passed") == 1, "Gate-1 must remain truthy after later patches"
    assert row.get("gate_2_passed") == 0
    assert row.get("gate_3_passed") is None, "untouched gate stays NULL"
    assert row.get("strategies_submitted") == 3


def test_outbox_dedupe_idempotent(initialized_db: Path) -> None:
    """First claim must succeed; second claim of an in-flight key bumps attempts.

    Terminal states (processed / abandoned) must reject re-claim with
    ``claimed=False`` so the publisher skips processing.
    """

    adapter = _build_adapter(initialized_db)

    async def scenario() -> tuple[dict, dict, dict, dict, dict]:
        await adapter.initialize()
        try:
            payload = {
                "dedupe_key": "manual:critical_minerals:dummyhash:2026-05-24",
                "source_event_id": "manual_test123",
                "theme_code": "rare_earth",
                "event_type": "critical_minerals",
            }
            first = await adapter.claim_event_outbox(payload)
            second = await adapter.claim_event_outbox(payload)  # same key, in-flight
            await adapter.mark_event_outbox_processed(payload["dedupe_key"])
            third = await adapter.claim_event_outbox(payload)  # processed → no claim
            # And a separate failed branch.
            fail_payload = {
                **payload,
                "dedupe_key": "manual:critical_minerals:other:2026-05-24",
            }
            fourth = await adapter.claim_event_outbox(fail_payload)
            await adapter.mark_event_outbox_failed(
                fail_payload["dedupe_key"], error="downstream timeout"
            )
            fifth = await adapter.claim_event_outbox(fail_payload)
            return first, second, third, fourth, fifth
        finally:
            await adapter.close()

    first, second, third, fourth, fifth = _run(scenario())
    assert first["claimed"] is True and first["attempts"] == 1
    assert second["claimed"] is True and second["attempts"] == 2
    assert third["claimed"] is False
    assert third["status"] == "processed"
    assert fourth["claimed"] is True and fourth["attempts"] == 1
    # Failed (non-terminal) is re-claimable; attempts count must climb.
    assert fifth["claimed"] is True and fifth["attempts"] == 2


def test_outbox_supports_abandon_terminal_state(initialized_db: Path) -> None:
    """``mark_event_outbox_failed(abandon=True)`` must lock the slot."""

    adapter = _build_adapter(initialized_db)

    async def scenario() -> dict:
        await adapter.initialize()
        try:
            payload = {
                "dedupe_key": "manual:abandoned_case:2026-05-24",
                "source_event_id": "manual_abandon",
            }
            await adapter.claim_event_outbox(payload)
            await adapter.mark_event_outbox_failed(
                payload["dedupe_key"], error="permanent", abandon=True
            )
            return await adapter.claim_event_outbox(payload)
        finally:
            await adapter.close()

    result = _run(scenario())
    assert result["claimed"] is False
    assert result["status"] == "abandoned"


# ---------------------------------------------------------------------------
# Phase 6 v1: TDX-only exposure DAO + manager maintenance actions
# ---------------------------------------------------------------------------
