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

def test_tdx_only_company_concept_and_industry_daos(initialized_db: Path) -> None:
    """TDX-only DAOs must read local tables and support filters."""

    adapter = _build_adapter(initialized_db)

    async def scenario() -> tuple[list, list, list, str]:
        await adapter.initialize()
        try:
            await _seed_tdx_only_exposure_fixture(adapter)
            concepts = await adapter.list_company_concept_blocks(
                symbols=["600100"], theme_code="AI", limit=10
            )
            filtered_out = await adapter.list_company_concept_blocks(
                symbols=["600100"], theme_code="not-present", limit=10
            )
            industries = await adapter.list_industry_blocks(symbols=["600100"], limit=10)
            async with adapter.acquire() as conn:
                c002_market_type = await conn.fetchval(
                    "SELECT block_type FROM market_blocks WHERE block_code = $1",
                    "C002",
                )
            return concepts, filtered_out, industries, c002_market_type
        finally:
            await adapter.close()

    concepts, filtered_out, industries, c002_market_type = _run(scenario())
    assert {row["block_code"] for row in concepts} == {"C001", "C002"}
    assert all(row["symbol"] == "600100" for row in concepts)
    by_code = {row["block_code"]: row for row in concepts}
    assert c002_market_type == "tdx"
    assert by_code["C002"]["block_type"] == "concept"
    assert filtered_out == []
    assert {row["industry_source"] for row in industries} >= {"stocks", "tdx_relation"}
    assert any(row["industry_name"] == "Semiconductor" for row in industries)


def test_lineage_and_status_handlers_read_real_tables(initialized_db: Path) -> None:
    """Lineage/exposure/outbox read actions must use persisted DAO state."""

    from akshare_mcp.tools.managers.strategy_mgr_factory_events import (
        handle_factory_event_lineage,
        handle_factory_event_outbox_status,
        handle_factory_theme_exposure_status,
    )

    adapter = _build_adapter(initialized_db)

    async def scenario() -> tuple[dict, dict, dict]:
        await adapter.initialize()
        try:
            await adapter.upsert_event_injection({
                "event_id": "evt_status_read",
                "source": "manual",
                "event_name": "status read event",
                "event_type": "policy_shock",
                "direction": "positive",
                "confidence": 0.8,
                "intensity": 0.7,
                "horizon": "swing_5_20d",
                "scope": "theme",
                "primary_themes": [{"theme_code": "status_theme", "direction": "positive"}],
                "valid_from": "2026-05-24T00:00:00+00:00",
                "valid_until": "2099-01-01T00:00:00+00:00",
                "status": "active",
                "operator_id": "operator_status",
            })
            await adapter.upsert_event_task_lineage({
                "dedupe_key": "evt_status_read:status_theme:600100",
                "event_id": "evt_status_read",
                "task_id": "task_status_read",
                "theme_code": "status_theme",
                "impact_direction": "positive",
                "impact_magnitude": 0.7,
                "target_symbols": ["600100"],
                "target_count": 1,
                "breadth_resolved": "narrow",
            })
            await adapter.update_event_task_lineage_gates(
                event_id="evt_status_read",
                task_id="task_status_read",
                gate_1_passed=True,
                strategies_submitted=2,
            )
            await adapter.bulk_upsert_theme_exposure([
                {
                    "symbol": "600100",
                    "theme_code": "status_theme",
                    "exposure_score": 0.88,
                    "industry_match_level": 2,
                    "evidence": {"source": "test"},
                }
            ])
            await adapter.claim_event_outbox({
                "dedupe_key": "evt_status_read:status_theme:600100",
                "source_event_id": "evt_status_read",
                "theme_code": "status_theme",
                "event_type": "policy_shock",
            })
            await adapter.mark_event_outbox_processed("evt_status_read:status_theme:600100")

            lineage = await handle_factory_event_lineage(
                adapter,
                {"event_id": "evt_status_read", "limit": 10},
            )
            exposure_status = await handle_factory_theme_exposure_status(adapter, {})
            outbox_status = await handle_factory_event_outbox_status(adapter, {"limit": 10})
            return lineage, exposure_status, outbox_status
        finally:
            await adapter.close()

    lineage, exposure_status, outbox_status = _run(scenario())
    assert lineage["success"] is True
    row = lineage["data"]["lineage"][0]
    assert row["event_name"] == "status read event"
    assert row["task_id"] == "task_status_read"
    assert row["target_symbols"] == ["600100"]
    assert row["gate_1_passed"] == 1
    assert row["strategies_submitted"] == 2
    assert exposure_status["data"]["row_count"] >= 1
    assert exposure_status["data"]["theme_count"] >= 1
    assert outbox_status["data"]["counts"]["processed"] >= 1


def test_theme_exposure_refresh_handler_uses_tdx_only_builder(
    initialized_db: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Manual refresh action must bulk-write Phase 6 v1 TDX-only evidence."""

    from akshare_mcp.tools.managers.strategy_mgr_factory_events import (
        handle_factory_theme_exposure_refresh,
    )

    monkeypatch.setenv("STRATEGY_FACTORY_THEME_EXPOSURE_MIN_TURNOVER", "0.1")
    monkeypatch.setenv("STRATEGY_FACTORY_THEME_EXPOSURE_MIN_MARKET_CAP", "10")
    adapter = _build_adapter(initialized_db)

    async def scenario() -> tuple[dict, list]:
        await adapter.initialize()
        try:
            await _seed_tdx_only_exposure_fixture(adapter)
            await adapter.upsert_theme_node({
                "theme_code": "ai_semiconductor",
                "theme_name": "AI Semiconductor",
                "aliases": ["AI"],
                "industry_tags": ["Semiconductor"],
                "breadth": "narrow",
                "is_active": 1,
            })
            response = await handle_factory_theme_exposure_refresh(
                adapter,
                {"stock_limit": 10, "theme_limit": 10, "batch_size": 3},
            )
            rows = await adapter.list_theme_exposure(
                theme_code="ai_semiconductor", min_exposure=0.0, limit=20
            )
            return response, rows
        finally:
            await adapter.close()

    response, rows = _run(scenario())
    assert response["success"] is True
    report = response["data"]
    assert report["status"] == "completed"
    assert report["source"] == "tdx_only_v1"
    assert report["rows_scanned"] >= 2
    assert report["rows_written"] >= 1
    assert report["batch_count"] >= 1
    assert report["industry_coverage"] > 0
    assert report["concept_block_coverage"] > 0
    assert report["skipped_low_liquidity"] >= 1
    target = next(row for row in rows if row["symbol"] == "600100")
    assert target["mainbz_match_score"] == 0
    assert "tdx_only_v1" in str(target["evidence"])


def test_factory_event_bootstrap_seeds_graph_and_refreshes_exposure(initialized_db: Path) -> None:
    """Bootstrap action should seed the default graph and refresh exposure."""

    from akshare_mcp.tools.managers.strategy_mgr_factory_events import (
        handle_factory_event_bootstrap,
    )

    adapter = _build_adapter(initialized_db)

    async def scenario() -> tuple[dict, dict, list, list, dict]:
        await adapter.initialize()
        try:
            await _seed_tdx_only_exposure_fixture(adapter)
            first = await handle_factory_event_bootstrap(
                adapter,
                {"batch_size": 3, "operator_id": "operator_bootstrap"},
            )
            second = await handle_factory_event_bootstrap(
                adapter,
                {"batch_size": 3, "refresh_exposure": False},
            )
            nodes = await adapter.list_theme_nodes(is_active=True, limit=500)
            edges = await adapter.list_theme_edges(is_active=True, limit=500)
            exposure_status = await adapter.get_theme_exposure_status()
            return first, second, nodes, edges, exposure_status
        finally:
            await adapter.close()

    first, second, nodes, edges, exposure_status = _run(scenario())
    assert first["success"] is True
    first_data = first["data"]
    assert first_data["seed"]["status"] == "seeded"
    assert first_data["seed"]["nodes_inserted"] >= 15
    assert first_data["seed"]["edges_inserted"] >= 10
    assert first_data["exposure_refresh"]["status"] == "completed"
    assert first_data["exposure_refresh"]["source"] == "tdx_only_v1"
    assert first_data["exposure_refresh"]["rows_written"] >= 1
    assert first_data["counts"]["theme_nodes"] >= 15
    assert first_data["counts"]["theme_edges"] >= 10
    assert first_data["counts"]["theme_exposure_rows"] >= 1
    assert first_data["availability"]["theme_graph_available"] is True
    assert first_data["availability"]["theme_exposure_available"] is True

    assert second["success"] is True
    assert second["data"]["seed"]["status"] == "skipped"
    assert second["data"]["seed"]["reason"] == "theme_graph_not_empty"
    assert second["data"]["exposure_refresh"] is None
    assert len(nodes) >= 15
    assert len(edges) >= 10
    assert exposure_status["row_count"] >= 1


def test_event_outbox_drain_writes_lineage_once(initialized_db: Path) -> None:
    """Outbox drain must claim, write lineage, mark processed, and dedupe."""

    from akshare_mcp.tools.managers.strategy_mgr_factory_events import (
        handle_factory_event_lineage,
        handle_factory_event_outbox_drain,
        handle_factory_event_outbox_status,
    )

    adapter = _build_adapter(initialized_db)

    async def scenario() -> tuple[dict, dict, dict, dict]:
        await adapter.initialize()
        try:
            await adapter.upsert_theme_node({
                "theme_code": "drain_root",
                "theme_name": "Drain root",
                "breadth": "narrow",
                "default_horizon": "swing_5_20d",
                "is_active": 1,
            })
            await adapter.bulk_upsert_theme_exposure([
                {
                    "symbol": "600100",
                    "theme_code": "drain_root",
                    "exposure_score": 0.85,
                    "industry_match_level": 2,
                    "evidence": {"source": "test"},
                }
            ])
            await adapter.upsert_event_injection({
                "event_id": "evt_drain_once",
                "source": "manual",
                "event_name": "drain event",
                "event_type": "policy_shock",
                "direction": "positive",
                "confidence": 0.9,
                "intensity": 0.9,
                "horizon": "swing_5_20d",
                "scope": "theme",
                "primary_themes": [{"theme_code": "drain_root", "direction": "positive"}],
                "valid_from": "2026-05-24T00:00:00+00:00",
                "valid_until": "2099-01-01T00:00:00+00:00",
                "status": "active",
                "operator_id": "operator_drain",
            })

            first = await handle_factory_event_outbox_drain(adapter, {"limit": 10})
            lineage = await handle_factory_event_lineage(
                adapter,
                {"event_id": "evt_drain_once", "limit": 10},
            )
            second = await handle_factory_event_outbox_drain(adapter, {"limit": 10})
            status = await handle_factory_event_outbox_status(adapter, {"limit": 10})
            return first, lineage, second, status
        finally:
            await adapter.close()

    first, lineage, second, status = _run(scenario())
    assert first["success"] is True
    assert first["data"]["single_worker"] is True
    assert first["data"]["processed"] == 1
    assert first["data"]["failed"] == 0
    assert lineage["data"]["count"] == 1
    row = lineage["data"]["lineage"][0]
    assert row["event_id"] == "evt_drain_once"
    assert row["theme_code"] == "drain_root"
    assert row["target_symbols"] == ["600100"]
    assert second["success"] is True
    assert second["data"]["processed"] == 0
    assert second["data"]["skipped"] >= 1
    assert status["data"]["counts"]["processed"] == 1


def test_theme_regression_run_handler_skips_without_edges(initialized_db: Path) -> None:
    """Manual regression action should be callable and skip cleanly when empty."""

    from akshare_mcp.tools.managers.strategy_mgr_factory_events import (
        handle_factory_theme_regression_run,
    )

    adapter = _build_adapter(initialized_db)

    async def scenario() -> dict:
        await adapter.initialize()
        try:
            return await handle_factory_theme_regression_run(adapter, {})
        finally:
            await adapter.close()

    response = _run(scenario())
    assert response["success"] is True
    assert response["data"] == {"status": "skipped", "reason": "no_active_edges"}


def test_theme_regression_run_handler_parses_apply_updates(monkeypatch) -> None:
    """Manual regression writeback stays opt-in and parses bool-like params."""

    from akshare_mcp.tools.managers.strategy_mgr_factory_events import (
        handle_factory_theme_regression_run,
    )
    from strategy_factory.application.research import theme_response_regression

    calls: list[bool] = []

    class FakeRegression:
        async def run_full_update(self, db, *, apply_updates=False):
            calls.append(apply_updates)
            return {
                "mode": "apply_updates" if apply_updates else "report_only",
                "apply_updates": apply_updates,
            }

    monkeypatch.setattr(theme_response_regression, "ThemeResponseRegression", FakeRegression)

    async def scenario() -> tuple[dict, dict]:
        default = await handle_factory_theme_regression_run(object(), {})
        explicit = await handle_factory_theme_regression_run(
            object(),
            {"apply_updates": "true"},
        )
        return default, explicit

    default, explicit = _run(scenario())
    assert calls == [False, True]
    assert default["data"]["mode"] == "report_only"
    assert explicit["data"]["mode"] == "apply_updates"


# ---------------------------------------------------------------------------
# PR-D (Phase 2, 2026-05-24): preview_tasks must drive real BFS + basket
# ---------------------------------------------------------------------------


def test_preview_tasks_returns_real_propagation_with_candidates(initialized_db: Path) -> None:
    """``factory_event_preview_tasks`` must return impacts + candidate symbols
    via the real ``propagate_event_to_themes`` + ``resolve_target_basket``
    pipeline, not the legacy depth-1 listing."""

    from akshare_mcp.tools.managers.strategy_mgr_factory_events import (
        handle_factory_event_preview_tasks,
    )

    adapter = _build_adapter(initialized_db)

    async def scenario() -> dict:
        await adapter.initialize()
        try:
            # Seed two theme nodes + one edge so BFS has something to walk.
            await adapter.upsert_theme_node({
                "theme_code": "preview_root",
                "theme_name": "Preview root",
                "breadth": "narrow",
                "default_horizon": "swing_5_20d",
            })
            await adapter.upsert_theme_node({
                "theme_code": "preview_child",
                "theme_name": "Preview child",
                "breadth": "narrow",
                "default_horizon": "swing_5_20d",
            })
            await adapter.upsert_theme_edge({
                "source_theme_code": "preview_root",
                "target_theme_code": "preview_child",
                "relation_type": "amplifies",
                "direction_sign": 1,
                "magnitude_factor": 0.8,
                "confidence": 0.7,
                "lag_days": 1,
            })
            # Seed exposure rows so resolve_target_basket has something to pick.
            await adapter.bulk_upsert_theme_exposure([
                {"symbol": "600100", "theme_code": "preview_root", "exposure_score": 0.85,
                 "industry_match_level": 2, "evidence": {"src": "test"}},
                {"symbol": "600200", "theme_code": "preview_root", "exposure_score": 0.7,
                 "industry_match_level": 1, "evidence": {"src": "test"}},
                {"symbol": "600300", "theme_code": "preview_child", "exposure_score": 0.6,
                 "industry_match_level": 1, "evidence": {"src": "test"}},
            ])

            return await handle_factory_event_preview_tasks(
                adapter,
                {
                    "event_id": "preview_event_001",
                    "event_name": "preview test",
                    "event_type": "policy_stimulus",
                    "direction": "positive",
                    "confidence": 0.9,
                    "intensity": 0.9,
                    "horizon": "swing_5_20d",
                    "primary_themes": [
                        {"theme_code": "preview_root", "direction": "positive"},
                    ],
                },
            )
        finally:
            await adapter.close()

    response = _run(scenario())
    assert response["success"] is True
    data = response["data"]
    assert data["preview_mode"] == "real_propagation_v1"
    # BFS reached both root and child.
    theme_codes = {item["theme_code"] for item in data["impacts"]}
    assert "preview_root" in theme_codes
    assert "preview_child" in theme_codes
    # Each impact carries candidate_symbols (because exposure was seeded).
    root_impact = next(it for it in data["impacts"] if it["theme_code"] == "preview_root")
    assert root_impact["candidate_count"] >= 1
    assert root_impact["candidate_symbols"], "root preview should expose seed symbols"
    # Aggregated candidate_symbols deduplicated across themes.
    assert isinstance(data["candidate_symbols"], list)
    assert data["candidate_count"] == len(set(data["candidate_symbols"]))
    # warnings is a list (possibly empty when seed is clean).
    assert isinstance(data["warnings"], list)


def test_preview_tasks_neutral_primary_emits_warning(initialized_db: Path) -> None:
    """Neutral primary 必须只出 lineage 标记,不展开下游."""

    from akshare_mcp.tools.managers.strategy_mgr_factory_events import (
        handle_factory_event_preview_tasks,
    )

    adapter = _build_adapter(initialized_db)

    async def scenario() -> dict:
        await adapter.initialize()
        try:
            # neutral_root has an edge that, if expanded, would surface
            # `neutral_child` — but neutral primaries must not expand.
            await adapter.upsert_theme_node({
                "theme_code": "neutral_root",
                "theme_name": "Neutral root",
                "breadth": "narrow",
            })
            await adapter.upsert_theme_node({
                "theme_code": "neutral_child",
                "theme_name": "Neutral child",
                "breadth": "narrow",
            })
            await adapter.upsert_theme_edge({
                "source_theme_code": "neutral_root",
                "target_theme_code": "neutral_child",
                "relation_type": "amplifies",
                "direction_sign": 1,
                "magnitude_factor": 0.9,
                "confidence": 0.9,
                "lag_days": 0,
            })

            return await handle_factory_event_preview_tasks(
                adapter,
                {
                    "event_id": "preview_neutral",
                    "event_name": "neutral preview",
                    "event_type": "policy_stimulus",
                    "direction": "neutral",
                    "confidence": 0.8,
                    "intensity": 0.8,
                    "primary_themes": [
                        {"theme_code": "neutral_root", "direction": "neutral"},
                    ],
                },
            )
        finally:
            await adapter.close()

    response = _run(scenario())
    assert response["success"] is True
    data = response["data"]
    theme_codes = {it["theme_code"] for it in data["impacts"]}
    assert "neutral_root" in theme_codes
    assert "neutral_child" not in theme_codes
    types = {w["type"] for w in data["warnings"]}
    assert "neutral_primary_skipped" in types
