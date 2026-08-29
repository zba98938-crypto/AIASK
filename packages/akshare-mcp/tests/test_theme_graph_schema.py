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

def test_real_adapter_creates_event_driven_tables(initialized_db: Path) -> None:
    """``SQLiteAdapter.initialize()`` must create the five event-driven tables.

    This replaces the previous test which embedded copy-pasted DDL — that
    version could never detect schema drift inside
    ``schema_strategy_parts/queries.py``.
    """

    conn = sqlite3.connect(str(initialized_db))
    try:
        names = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
    finally:
        conn.close()

    missing = [t for t in EXPECTED_TABLES if t not in names]
    assert not missing, f"adapter init left these tables uncreated: {missing}"


def test_real_adapter_keeps_legacy_event_tables(initialized_db: Path) -> None:
    """Legacy cluster/signal tables must coexist with the new event_injections."""

    conn = sqlite3.connect(str(initialized_db))
    try:
        names = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
    finally:
        conn.close()

    missing = [t for t in LEGACY_TABLES if t not in names]
    assert not missing, (
        "legacy cluster/signal tables disappeared; cutover must be planned, "
        f"not implicit: {missing}"
    )


def test_no_forbidden_tushare_shaped_cache(initialized_db: Path) -> None:
    """Phase 0.5 禁令: 不允许出现 strategy_factory_company_mainbz."""

    conn = sqlite3.connect(str(initialized_db))
    try:
        names = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
    finally:
        conn.close()

    leaked = [t for t in FORBIDDEN_TABLES if t in names]
    assert not leaked, (
        "Phase 0.5 forbids Tushare-shaped local caches in the event-driven plan; "
        f"these tables are not allowed: {leaked}"
    )


def test_event_injections_has_approver_columns(initialized_db: Path) -> None:
    """方案 §6 Phase 0 验收: approver_id / approved_at 列必须存在.

    DAO 写入它们是 PR-B1 的工作,但列必须先就位。
    """

    conn = sqlite3.connect(str(initialized_db))
    try:
        cols = _table_columns(conn, "strategy_factory_event_injections")
    finally:
        conn.close()

    for required in ("approver_id", "approved_at"):
        assert required in cols, (
            f"{required} column missing on strategy_factory_event_injections; "
            "PR-B1 cannot persist approval state without it."
        )


def test_event_task_lineage_has_gate_columns(initialized_db: Path) -> None:
    """方案 §6 Phase 0 验收: lineage 表必须支持 Gate 状态写回."""

    conn = sqlite3.connect(str(initialized_db))
    try:
        cols = _table_columns(conn, "strategy_factory_event_task_lineage")
    finally:
        conn.close()

    for required in (
        "gate_1_passed",
        "gate_2_passed",
        "gate_3_passed",
        "strategies_submitted",
    ):
        assert required in cols, (
            f"{required} column missing on strategy_factory_event_task_lineage; "
            "Phase 0 lineage update method cannot land without it."
        )


def test_seed_idempotent_against_real_schema(initialized_db: Path) -> None:
    """Seed must run twice without dup, on top of a real adapter-built schema."""

    if str(SCRIPTS_DIR) not in sys.path:
        sys.path.insert(0, str(SCRIPTS_DIR))
    from seed_strategy_factory_theme_graph import (  # type: ignore[import-not-found]
        seed,
        THEME_NODES,
        THEME_EDGES,
    )

    asyncio.run(seed(str(initialized_db)))
    asyncio.run(seed(str(initialized_db)))  # second run should be a no-op

    conn = sqlite3.connect(str(initialized_db))
    try:
        node_count = conn.execute(
            "SELECT COUNT(*) FROM strategy_factory_theme_nodes"
        ).fetchone()[0]
        edge_count = conn.execute(
            "SELECT COUNT(*) FROM strategy_factory_theme_edges"
        ).fetchone()[0]
    finally:
        conn.close()

    assert node_count == len(THEME_NODES) == 15, (
        f"expected 15 seed nodes, got {node_count}"
    )
    assert edge_count == len(THEME_EDGES) == 10, (
        f"expected 10 seed edges, got {edge_count}"
    )


def test_seed_node_fields_round_trip(initialized_db: Path) -> None:
    """Seed must preserve breadth / horizon / industry_tags exactly as declared."""

    if str(SCRIPTS_DIR) not in sys.path:
        sys.path.insert(0, str(SCRIPTS_DIR))
    from seed_strategy_factory_theme_graph import seed  # type: ignore[import-not-found]

    asyncio.run(seed(str(initialized_db)))

    conn = sqlite3.connect(str(initialized_db))
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT theme_code, theme_name, breadth, default_horizon, industry_tags "
            "FROM strategy_factory_theme_nodes WHERE theme_code = 'upstream_oil_gas'"
        ).fetchone()
    finally:
        conn.close()

    assert row is not None, "seed did not produce upstream_oil_gas node"
    assert row["breadth"] == "narrow"
    assert row["default_horizon"] == "swing_1_5d"
    assert "石油开采" in row["industry_tags"]


def test_edge_unique_constraint_against_real_schema(initialized_db: Path) -> None:
    """Verify edge UNIQUE(source, target, relation_type) enforced by real DDL."""

    conn = sqlite3.connect(str(initialized_db))
    try:
        conn.execute(
            "INSERT INTO strategy_factory_theme_edges "
            "(source_theme_code, target_theme_code, relation_type, direction_sign) "
            "VALUES ('a', 'b', 'amplifies', 1)"
        )
        conn.commit()

        # Duplicate triple should be ignored, even if direction_sign differs.
        conn.execute(
            "INSERT OR IGNORE INTO strategy_factory_theme_edges "
            "(source_theme_code, target_theme_code, relation_type, direction_sign) "
            "VALUES ('a', 'b', 'amplifies', -1)"
        )
        conn.commit()

        count = conn.execute(
            "SELECT COUNT(*) FROM strategy_factory_theme_edges "
            "WHERE source_theme_code='a' AND target_theme_code='b' "
            "AND relation_type='amplifies'"
        ).fetchone()[0]
    finally:
        conn.close()

    assert count == 1, "UNIQUE constraint did not collapse duplicate triple"


def test_test_file_does_not_embed_ddl() -> None:
    """Self-check: this very file must never reintroduce ``CREATE TABLE`` DDL.

    Embedded DDL was the bug PR-A is fixing; future maintainers should
    feel friction immediately if they paste a CREATE TABLE in here.

    The check looks for ``conn.execute(`` immediately followed (within a
    short window) by a SQL string containing ``CREATE TABLE``. That avoids
    false positives from prose / docstrings / assert messages, which are
    fine and historically necessary to explain *why* the DDL was removed.
    """

    import re

    text = Path(__file__).read_text(encoding="utf-8")
    pattern = re.compile(
        r"conn\s*\.\s*execute\s*\(\s*[fr]?[\"']{1,3}[^\"']*?CREATE\s+TABLE",
        re.IGNORECASE | re.DOTALL,
    )
    matches = pattern.findall(text)
    assert not matches, (
        "test_theme_graph_schema.py must not embed CREATE TABLE statements; "
        "drive SQLiteAdapter().initialize() instead. "
        f"Found {len(matches)} embedded DDL call(s)."
    )


# ---------------------------------------------------------------------------
# PR-B1 (2026-05-24): DAO contract tests
#
# Phase 0 验收余项 — 全部依赖 ``initialized_db`` fixture (= real adapter):
#   - factory_event_create / list / record_outcome 在真实 DAO 上 CRUD
#   - factory_event_approve 持久化 approver_id / approved_at + 自审被拒绝
#   - outbox / 消费状态可以按 dedupe_key 幂等处理同一事件
#   - upsert_theme_exposure / bulk_upsert_theme_exposure 行为一致
#   - update_event_task_lineage_gates 部分更新不重置已通过 Gate
# ---------------------------------------------------------------------------
