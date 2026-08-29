"""Tests for theme graph propagation algorithm (PR-3)."""

from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock

from strategy_factory.application.research.theme_graph import (
    NormalizedEvent,
    ThemeImpact,
    normalize_direction_sign,
    propagate_event_to_themes,
)
from strategy_factory.application.research.target_basket import (
    TargetBasket,
    apply_industry_diversification,
    resolve_target_basket,
    resolve_target_count,
)


# --- normalize_direction_sign ---

def test_direction_sign_positive():
    assert normalize_direction_sign("positive") == 1
    assert normalize_direction_sign("+1") == 1
    assert normalize_direction_sign(1) == 1


def test_direction_sign_negative():
    assert normalize_direction_sign("negative") == -1
    assert normalize_direction_sign("-1") == -1
    assert normalize_direction_sign(-1) == -1


def test_direction_sign_neutral():
    assert normalize_direction_sign("neutral") == 0
    assert normalize_direction_sign(None) == 0
    assert normalize_direction_sign("") == 0


# --- resolve_target_count ---

def test_target_count_minimum():
    result = resolve_target_count(
        confidence=0.0, intensity=0.0,
        theme_breadth="narrow", task_source="auto_event",
        feature_flag_target_max=30,
    )
    assert result >= 3


def test_target_count_maximum():
    result = resolve_target_count(
        confidence=1.0, intensity=1.0,
        theme_breadth="broad", task_source="manual_event",
        feature_flag_target_max=30,
    )
    assert result <= 30


def test_target_count_snapshot_capped_at_12():
    result = resolve_target_count(
        confidence=1.0, intensity=1.0,
        theme_breadth="broad", task_source="snapshot",
        feature_flag_target_max=30,
    )
    assert result <= 12


def test_target_count_flag_max_12_caps():
    result = resolve_target_count(
        confidence=1.0, intensity=1.0,
        theme_breadth="broad", task_source="manual_event",
        feature_flag_target_max=12,
    )
    assert result <= 12


# --- propagate_event_to_themes ---

@pytest.fixture
def mock_graph_db():
    db = MagicMock()
    db.get_theme_node = AsyncMock(return_value={"theme_code": "test", "breadth": "medium"})
    db.list_theme_edges = AsyncMock(return_value=[])
    return db


@pytest.mark.asyncio
async def test_propagation_single_primary_no_edges(mock_graph_db):
    event = NormalizedEvent(
        event_id="test_1",
        primary_themes=[{"theme_code": "upstream_oil_gas", "direction": "positive"}],
        confidence=0.8,
        intensity=0.7,
    )
    impacts = await propagate_event_to_themes(mock_graph_db, event)
    assert len(impacts) == 1
    assert impacts[0].theme_code == "upstream_oil_gas"
    assert impacts[0].direction_sign == 1
    assert impacts[0].depth == 0


@pytest.mark.asyncio
async def test_propagation_with_edges(mock_graph_db):
    mock_graph_db.list_theme_edges = AsyncMock(return_value=[
        {
            "target_theme_code": "airlines",
            "relation_type": "supply_shock",
            "direction_sign": -1,
            "magnitude_factor": 0.7,
            "confidence": 0.65,
            "lag_days": 1,
        },
        {
            "target_theme_code": "shipping_trade",
            "relation_type": "amplifies",
            "direction_sign": 1,
            "magnitude_factor": 0.55,
            "confidence": 0.6,
            "lag_days": 1,
        },
    ])

    event = NormalizedEvent(
        event_id="test_2",
        primary_themes=[{"theme_code": "upstream_oil_gas", "direction": "positive"}],
        confidence=0.8,
        intensity=0.7,
    )
    impacts = await propagate_event_to_themes(mock_graph_db, event)

    codes = {i.theme_code for i in impacts}
    assert "upstream_oil_gas" in codes
    assert "airlines" in codes
    assert "shipping_trade" in codes

    airlines = next(i for i in impacts if i.theme_code == "airlines")
    assert airlines.direction_sign == -1  # positive × -1 = negative
    assert airlines.depth == 1
    assert airlines.lag_days == 1


@pytest.mark.asyncio
async def test_propagation_prunes_weak_signals(mock_graph_db):
    mock_graph_db.list_theme_edges = AsyncMock(return_value=[
        {
            "target_theme_code": "weak_theme",
            "relation_type": "amplifies",
            "direction_sign": 1,
            "magnitude_factor": 0.1,  # Too weak
            "confidence": 0.1,  # Too weak
            "lag_days": 0,
        },
    ])

    event = NormalizedEvent(
        event_id="test_3",
        primary_themes=[{"theme_code": "source", "direction": "positive"}],
        confidence=0.5,
        intensity=0.3,
    )
    impacts = await propagate_event_to_themes(mock_graph_db, event)

    codes = {i.theme_code for i in impacts}
    assert "weak_theme" not in codes


@pytest.mark.asyncio
async def test_propagation_respects_max_depth(mock_graph_db):
    # Depth 1 edge
    async def mock_edges(source=None, **kwargs):
        if source == "a":
            return [{"target_theme_code": "b", "relation_type": "amplifies",
                     "direction_sign": 1, "magnitude_factor": 0.8, "confidence": 0.8, "lag_days": 0}]
        if source == "b":
            return [{"target_theme_code": "c", "relation_type": "amplifies",
                     "direction_sign": 1, "magnitude_factor": 0.8, "confidence": 0.8, "lag_days": 0}]
        if source == "c":
            return [{"target_theme_code": "d", "relation_type": "amplifies",
                     "direction_sign": 1, "magnitude_factor": 0.8, "confidence": 0.8, "lag_days": 0}]
        return []

    mock_graph_db.list_theme_edges = AsyncMock(side_effect=mock_edges)

    event = NormalizedEvent(
        event_id="test_4",
        primary_themes=[{"theme_code": "a", "direction": "positive"}],
        confidence=0.9,
        intensity=0.9,
    )
    impacts = await propagate_event_to_themes(mock_graph_db, event, max_depth=2)

    codes = {i.theme_code for i in impacts}
    assert "a" in codes
    assert "b" in codes
    assert "c" in codes
    assert "d" not in codes  # depth=3, beyond max_depth=2


@pytest.mark.asyncio
async def test_propagation_direction_multiplication():
    """Test -1 × -1 = +1 direction propagation."""
    db = MagicMock()
    db.get_theme_node = AsyncMock(return_value={"breadth": "medium"})

    async def mock_edges(source=None, **kwargs):
        if source == "a":
            return [{"target_theme_code": "b", "relation_type": "dampens",
                     "direction_sign": -1, "magnitude_factor": 0.8, "confidence": 0.8, "lag_days": 0}]
        return []

    db.list_theme_edges = AsyncMock(side_effect=mock_edges)

    event = NormalizedEvent(
        event_id="test_5",
        primary_themes=[{"theme_code": "a", "direction": "negative"}],
        confidence=0.9,
        intensity=0.9,
    )
    impacts = await propagate_event_to_themes(db, event)

    b_impact = next(i for i in impacts if i.theme_code == "b")
    # negative(-1) × dampens(-1) = positive(+1)
    assert b_impact.direction_sign == 1


@pytest.mark.asyncio
async def test_propagation_empty_primary_themes():
    db = MagicMock()
    event = NormalizedEvent(event_id="empty", primary_themes=[])
    impacts = await propagate_event_to_themes(db, event)
    assert impacts == []


# --- PR-D (Phase 2, 2026-05-24) hardening tests ---


@pytest.mark.asyncio
async def test_propagation_neutral_primary_does_not_expand():
    """Neutral primary 不进入 frontier；只写 lineage 标记，不向下游传播."""
    db = MagicMock()
    db.get_theme_node = AsyncMock(return_value={"breadth": "medium"})
    edges_called = AsyncMock(return_value=[
        {
            "target_theme_code": "downstream",
            "relation_type": "amplifies",
            "direction_sign": 1,
            "magnitude_factor": 0.8,
            "confidence": 0.7,
            "lag_days": 0,
        }
    ])
    db.list_theme_edges = edges_called

    event = NormalizedEvent(
        event_id="neutral_test",
        primary_themes=[{"theme_code": "neutral_root", "direction": "neutral"}],
        confidence=0.8,
        intensity=0.7,
    )
    impacts, warnings = await propagate_event_to_themes(db, event, return_warnings=True)

    codes = {i.theme_code for i in impacts}
    assert "neutral_root" in codes
    assert "downstream" not in codes, "neutral primary must not expand"
    neutral_impact = next(i for i in impacts if i.theme_code == "neutral_root")
    assert neutral_impact.direction_sign == 0
    assert neutral_impact.magnitude == 0.0
    assert neutral_impact.source_path == "primary:neutral"
    # warning: neutral_primary_skipped
    types = {w["type"] for w in warnings}
    assert "neutral_primary_skipped" in types
    edges_called.assert_not_called()


@pytest.mark.asyncio
async def test_propagation_visited_blocks_simple_cycle():
    """A -> B -> A 循环必须由 visited (theme_code, depth) 截断."""
    db = MagicMock()
    db.get_theme_node = AsyncMock(return_value={"breadth": "medium"})

    async def cyclic_edges(source=None, **kwargs):
        if source == "a":
            return [{"target_theme_code": "b", "relation_type": "amplifies",
                     "direction_sign": 1, "magnitude_factor": 0.9, "confidence": 0.9, "lag_days": 0}]
        if source == "b":
            return [{"target_theme_code": "a", "relation_type": "amplifies",
                     "direction_sign": 1, "magnitude_factor": 0.9, "confidence": 0.9, "lag_days": 0}]
        return []

    db.list_theme_edges = AsyncMock(side_effect=cyclic_edges)

    event = NormalizedEvent(
        event_id="cycle_test",
        primary_themes=[{"theme_code": "a", "direction": "positive"}],
        confidence=0.95,
        intensity=0.95,
    )
    impacts = await propagate_event_to_themes(db, event, max_depth=4)

    codes = [i.theme_code for i in impacts]
    # Both a and b appear once (no infinite expansion).
    assert codes.count("a") == 1
    assert codes.count("b") == 1


@pytest.mark.asyncio
async def test_propagation_emits_warning_for_missing_primary_theme():
    """Primary 主题不存在时,warning 必须包含 missing_theme_node 标记."""
    db = MagicMock()
    db.get_theme_node = AsyncMock(return_value=None)
    db.list_theme_edges = AsyncMock(return_value=[])

    event = NormalizedEvent(
        event_id="missing_test",
        primary_themes=[{"theme_code": "ghost_theme", "direction": "positive"}],
        confidence=0.7,
        intensity=0.5,
    )
    impacts, warnings = await propagate_event_to_themes(db, event, return_warnings=True)

    types = [w["type"] for w in warnings]
    assert "missing_theme_node" in types
    miss = next(w for w in warnings if w["type"] == "missing_theme_node")
    assert miss["theme_code"] == "ghost_theme"
    assert miss["depth"] == 0


@pytest.mark.asyncio
async def test_propagation_merges_multi_path_records_runner_up():
    """同一 target theme 多路径命中:winner 留 source_path,runner-up 进 warnings."""
    db = MagicMock()
    db.get_theme_node = AsyncMock(return_value={"breadth": "medium"})

    async def multi_path_edges(source=None, **kwargs):
        if source == "root":
            return [
                # Strong edge: magnitude 0.9 wins.
                {"target_theme_code": "target", "relation_type": "amplifies",
                 "direction_sign": 1, "magnitude_factor": 0.9, "confidence": 0.9, "lag_days": 0},
                # Weak edge to a 2nd hop that loops back to target.
                {"target_theme_code": "side", "relation_type": "amplifies",
                 "direction_sign": 1, "magnitude_factor": 0.7, "confidence": 0.7, "lag_days": 0},
            ]
        if source == "side":
            return [
                {"target_theme_code": "target", "relation_type": "amplifies",
                 "direction_sign": 1, "magnitude_factor": 0.7, "confidence": 0.7, "lag_days": 0},
            ]
        return []

    db.list_theme_edges = AsyncMock(side_effect=multi_path_edges)

    event = NormalizedEvent(
        event_id="merge_test",
        primary_themes=[{"theme_code": "root", "direction": "positive"}],
        confidence=0.9,
        intensity=0.9,
    )
    impacts, warnings = await propagate_event_to_themes(db, event, return_warnings=True, max_depth=3)

    target_impact = next(i for i in impacts if i.theme_code == "target")
    # Winner is the depth-1 0.9 edge from root → target.
    assert " || " in target_impact.source_path, "merged source_path must list alternatives"
    assert target_impact.source_path.startswith("primary → amplifies → target")

    types = [w["type"] for w in warnings]
    assert "merged_paths" in types
    merge_warn = next(w for w in warnings if w["type"] == "merged_paths" and w["theme_code"] == "target")
    assert merge_warn["winner"].startswith("primary → amplifies → target")
    # The 2-hop alternative path must be recorded.
    assert any("side" in alt for alt in merge_warn["alternatives"])


@pytest.mark.asyncio
async def test_target_basket_empty_exposure_records_fallback_evidence():
    """Exposure 为空时,evidence 必须显式标 fallback=True,不能静默吞."""
    db = MagicMock()
    db.list_theme_exposure = AsyncMock(return_value=[])

    impact = ThemeImpact(
        theme_code="empty_theme",
        direction_sign=1,
        magnitude=0.7,
        confidence=0.8,
        breadth="narrow",
    )
    basket = await resolve_target_basket(db, impact)
    assert basket.symbols == []
    evidence = basket.evidence
    assert evidence["fallback"] is True
    assert evidence["fallback_reason"] in ("exposure_table_empty",) or evidence["fallback_reason"].startswith(
        "list_theme_exposure_failed"
    )
    assert evidence["target_count_resolved"] >= 3


def test_target_basket_resolve_target_count_uses_canonical_resolver():
    """target_basket.resolve_target_count 必须委托给 domain.target_count_resolver,
    不再保留独立公式（PR-D 删除重复实现）."""
    from strategy_factory.application.research import target_basket as tb_mod
    from strategy_factory.domain import target_count_resolver as canonical

    # Direct alias guarantee.
    assert tb_mod.resolve_target_count.__module__ == tb_mod.__name__
    # Same numeric output for representative inputs.
    for case in [
        dict(confidence=0.5, intensity=0.5, theme_breadth="narrow",
             task_source="event_driven", feature_flag_target_max=12),
        dict(confidence=1.0, intensity=1.0, theme_breadth="broad",
             task_source="manual_event", feature_flag_target_max=30),
        dict(confidence=0.0, intensity=0.0, theme_breadth="medium",
             task_source="snapshot", feature_flag_target_max=12),
    ]:
        assert tb_mod.resolve_target_count(**case) == canonical.resolve_target_count(**case)


# --- apply_industry_diversification ---

def test_diversification_limits_per_industry():
    rows = [
        {"symbol": "001", "industry": "银行", "exposure_score": 0.9},
        {"symbol": "002", "industry": "银行", "exposure_score": 0.85},
        {"symbol": "003", "industry": "银行", "exposure_score": 0.8},
        {"symbol": "004", "industry": "银行", "exposure_score": 0.75},
        {"symbol": "005", "industry": "保险", "exposure_score": 0.7},
    ]
    result = apply_industry_diversification(rows, max_per_industry=2, target_count=10)
    bank_count = sum(1 for r in result if r["industry"] == "银行")
    assert bank_count == 2
    assert len(result) == 3  # 2 banks + 1 insurance
