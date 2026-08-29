"""Tests for event task generator (PR-6 + PR-C / Phase 1)."""

from __future__ import annotations

import os
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from strategy_factory.application.research.event_task_generator import (
    generate_tasks_from_active_events,
    _build_event_task,
    _compute_task_priority,
    _normalize_event_source,
)
from strategy_factory.application.research.theme_graph import NormalizedEvent, ThemeImpact


def test_build_event_task_unifies_task_source_to_event_driven():
    """PR-C: every event task must report task_source="event_driven" + event_source="manual"."""
    event = NormalizedEvent(
        event_id="test_001",
        event_name="Oil shock",
        event_type="commodity",
        source="manual",
        confidence=0.8,
        intensity=0.7,
        horizon="swing_5_20d",
        valid_from="2026-05-24T00:00:00+00:00",
        valid_until="2026-06-13T00:00:00+00:00",
    )
    impact = ThemeImpact(
        theme_code="upstream_oil_gas",
        direction_sign=1,
        magnitude=0.7,
        confidence=0.8,
        lag_days=1,
        depth=0,
        breadth="narrow",
    )
    task = _build_event_task(
        event,
        impact,
        ["600028", "601857"],
        event_source="manual",
        dedupe_key="test_001:upstream_oil_gas:600028-601857",
    )

    # PR-C: top-level + research_task task_source unified
    assert task["task_source"] == "event_driven"
    assert task["research_task"]["task_source"] == "event_driven"
    # event_source recorded in three places: top-level, research_task, event_context
    assert task["event_source"] == "manual"
    assert task["research_task"]["event_source"] == "manual"
    assert task["event_context"]["event_source"] == "manual"
    # legacy fields still in place
    assert task["candidate_family"] == "upstream_oil_gas"
    assert task["target_symbols"] == ["600028", "601857"]
    assert task["event_context"]["event_id"] == "test_001"
    assert task["event_context"]["direction"] == "bullish"
    # PR-C event_context expansions
    assert task["event_context"]["valid_from"] == "2026-05-24T00:00:00+00:00"
    assert task["event_context"]["valid_until"] == "2026-06-13T00:00:00+00:00"
    assert task["event_context"]["dedupe_key"] == "test_001:upstream_oil_gas:600028-601857"
    assert "task_id" in task


def test_build_event_task_preserves_event_source_for_news_origin():
    """非 manual 来源也必须正确进入 event_source 槽位."""
    event = NormalizedEvent(
        event_id="news_evt_42",
        event_name="Tariff escalation",
        event_type="trade_tariff",
        source="news_llm",
        confidence=0.6,
        intensity=0.55,
    )
    impact = ThemeImpact(
        theme_code="trade_tariff_impacts",
        direction_sign=-1,
        magnitude=0.55,
        confidence=0.6,
    )
    task = _build_event_task(
        event,
        impact,
        ["000001", "000002", "000003"],
        event_source="news_llm",
    )
    assert task["task_source"] == "event_driven"
    assert task["event_source"] == "news_llm"
    assert task["event_context"]["event_source"] == "news_llm"


def test_normalize_event_source_handles_aliases():
    assert _normalize_event_source("manual") == "manual"
    assert _normalize_event_source("news_llm") == "news_llm"
    assert _normalize_event_source("macro_shock") == "macro_shock"
    assert _normalize_event_source("market_anomaly") == "market_anomaly"
    assert _normalize_event_source("price_inference") == "price_inference"
    # legacy aliases
    assert _normalize_event_source("auto") == "price_inference"
    assert _normalize_event_source("news") == "news_llm"
    assert _normalize_event_source("macro") == "macro_shock"
    assert _normalize_event_source("anomaly") == "market_anomaly"
    # unknown / empty → default manual
    assert _normalize_event_source("") == "manual"
    assert _normalize_event_source(None) == "manual"
    assert _normalize_event_source("garbage_value") == "manual"


def test_task_priority_manual_higher():
    event_manual = NormalizedEvent(event_id="m1", source="manual", confidence=0.8, intensity=0.7)
    event_auto = NormalizedEvent(event_id="a1", source="price_inference", confidence=0.8, intensity=0.7)
    impact = ThemeImpact(theme_code="t", direction_sign=1, magnitude=0.7, confidence=0.8)

    priority_manual = _compute_task_priority(event_manual, impact)
    priority_auto = _compute_task_priority(event_auto, impact)
    assert priority_manual < priority_auto  # Lower number = higher priority


@pytest.mark.asyncio
async def test_generate_disabled_when_flag_off():
    with patch.dict(os.environ, {"STRATEGY_FACTORY_MANUAL_EVENT_ENABLED": "0"}):
        # Need to reimport to pick up env change
        import importlib
        import strategy_factory.application.research.event_task_generator as mod
        importlib.reload(mod)

        db = MagicMock()
        result = await mod.generate_tasks_from_active_events(db)
        assert result["enabled"] is False

        # Restore
        importlib.reload(mod)


def _mock_active_event_db(*, claim_result=None, lineage_side_effect=None):
    db = MagicMock()
    db.list_event_injections = AsyncMock(return_value=[
        {
            "event_id": "claim_evt_001",
            "source": "manual",
            "event_name": "Claim event",
            "event_type": "policy_shock",
            "direction": "positive",
            "confidence": 0.8,
            "intensity": 0.7,
            "horizon": "swing_5_20d",
            "primary_themes": [{"theme_code": "claim_theme", "direction": "positive"}],
            "valid_from": "2026-05-24T00:00:00",
            "valid_until": "2099-12-31T00:00:00",
            "status": "active",
        }
    ])
    db.get_theme_node = AsyncMock(return_value={"theme_code": "claim_theme", "breadth": "narrow"})
    db.list_theme_edges = AsyncMock(return_value=[])
    db.list_theme_exposure = AsyncMock(return_value=[
        {"symbol": "600100", "exposure_score": 0.9, "industry": "technology"},
    ])
    db.claim_event_outbox = AsyncMock(return_value=claim_result or {
        "claimed": True,
        "status": "processing",
        "attempts": 1,
        "dedupe_key": "claim_evt_001:claim_theme:600100",
    })
    db.upsert_event_task_lineage = AsyncMock(side_effect=lineage_side_effect)
    db.mark_event_outbox_processed = AsyncMock(return_value={"status": "processed"})
    db.mark_event_outbox_failed = AsyncMock(return_value={"status": "failed"})
    return db


class _VerifiedSignalFallbackDb:
    def __init__(self) -> None:
        self.list_event_injections = AsyncMock(return_value=[])
        self.claim_event_outbox = AsyncMock(return_value={
            "claimed": True,
            "status": "processing",
            "attempts": 1,
            "dedupe_key": "mevt_cninfo_001:order_contract:000001",
        })
        self.upsert_event_task_lineage = AsyncMock(return_value=None)
        self.mark_event_outbox_processed = AsyncMock(return_value={"status": "processed"})
        self.mark_event_outbox_failed = AsyncMock(return_value={"status": "failed"})

    def list_factory_event_clusters(self, status: str | None = None, limit: int = 200):
        if status == "diagnostic":
            return []
        if status not in (None, "active"):
            return []
        validation_summary = {
            "event_signature": "sig-cninfo-001",
            "official_anchor_count": 1,
            "cross_source_count": 1,
            "conflict_count": 0,
            "occurrence_status": "verified_single_anchor",
            "alpha_confirmation_status": "single_anchor_unconfirmed",
        }
        return [
            {
                "event_id": "mevt_cninfo_001",
                "event_type": "order_contract",
                "event_name": "official contract announcement",
                "summary": "official disclosure anchored event",
                "direction": "positive",
                "confidence": 0.65,
                "intensity": 0.65,
                "horizon": "swing_1_5d",
                "source_types": ["market_events_normalized", "cninfo"],
                "themes": [
                    {
                        "theme_code": "order_contract",
                        "theme_name": "order contract",
                        "direction": "positive",
                    }
                ],
                "evidence": {
                    "source": "market_events_normalized",
                    "event_anchor_id": "mevt_cninfo_001",
                    "source_doc_uids": ["cninfo:000001:2026-06-06:001"],
                    "source_tier": "tier_a",
                    "provider_chain": ["cninfo"],
                    "reliability_score": 0.65,
                    "normalized_event_status": "verified",
                    "evidence_time": "2026-06-06T10:00:00+08:00",
                    "verified_event_anchor": True,
                    "validation_summary": validation_summary,
                    "occurrence_status": "verified_single_anchor",
                    "alpha_confirmation_status": "single_anchor_unconfirmed",
                    "needs_alpha_confirmation": True,
                    "conflict_count": 0,
                },
                "status": "active",
            }
        ][:limit]

    def list_factory_event_signals(self, event_id: str | None = None, limit: int = 24):
        if event_id != "mevt_cninfo_001":
            return []
        return [
            {
                "event_id": "mevt_cninfo_001",
                "symbol": "000001",
                "theme_code": "order_contract",
                "direction": "positive",
                "final_score": 0.92,
                "theme_score": 0.92,
                "exposure_score": 0.88,
                "rationale": "official disclosure",
            }
        ][:limit]

    def list_factory_theme_definitions(self, active_only: bool = True, limit: int = 256):
        return []


@pytest.mark.asyncio
async def test_verified_event_cluster_fallback_claims_outbox_without_injections():
    import importlib
    import strategy_factory.application.research.event_task_generator as mod

    with patch.dict(os.environ, {
        "STRATEGY_FACTORY_MANUAL_EVENT_ENABLED": "1",
        "STRATEGY_FACTORY_THEME_GRAPH_ENABLED": "1",
    }):
        importlib.reload(mod)
        db = _VerifiedSignalFallbackDb()

        result = await mod.generate_tasks_from_active_events(
            db,
            {"date": "2026-06-06"},
            claim_outbox=True,
        )

        assert result["event_source_mode"] == "verified_event_clusters"
        assert result["active_injection_event_count"] == 0
        assert result["event_count"] == 1
        assert result["task_count"] == 1
        assert result["lineage_count"] == 1
        assert result["outbox_claimed"] == 1
        assert result["outbox_processed"] == 1
        assert result["outbox_failed"] == 0
        task = result["tasks"][0]
        assert task["event_source"] == "market_events_normalized"
        assert task["event_context"]["dedupe_key"] == "mevt_cninfo_001:order_contract:000001"
        assert result["lineage_records"][0]["dedupe_key"] == "mevt_cninfo_001:order_contract:000001"
        db.claim_event_outbox.assert_awaited_once()
        claim_payload = db.claim_event_outbox.call_args.args[0]
        assert claim_payload["source_event_id"] == "mevt_cninfo_001"
        assert claim_payload["theme_code"] == "order_contract"
        db.upsert_event_task_lineage.assert_awaited_once()
        db.mark_event_outbox_processed.assert_awaited_once_with(
            "mevt_cninfo_001:order_contract:000001"
        )
        db.mark_event_outbox_failed.assert_not_awaited()
        importlib.reload(mod)


@pytest.mark.asyncio
async def test_claim_outbox_success_is_required_before_emit():
    import importlib
    import strategy_factory.application.research.event_task_generator as mod

    with patch.dict(os.environ, {
        "STRATEGY_FACTORY_MANUAL_EVENT_ENABLED": "1",
        "STRATEGY_FACTORY_THEME_GRAPH_ENABLED": "1",
        "STRATEGY_FACTORY_DYNAMIC_TARGET_COUNT_ENABLED": "0",
    }):
        importlib.reload(mod)
        db = _mock_active_event_db()

        result = await mod.generate_tasks_from_active_events(db, claim_outbox=True)

        assert result["task_count"] == 1
        assert result["outbox_claimed"] == 1
        assert result["outbox_processed"] == 1
        assert result["outbox_skipped"] == 0
        assert result["outbox_failed"] == 0
        db.claim_event_outbox.assert_awaited_once()
        db.upsert_event_task_lineage.assert_awaited_once()
        db.mark_event_outbox_processed.assert_awaited_once()
        db.mark_event_outbox_failed.assert_not_awaited()
        importlib.reload(mod)


@pytest.mark.asyncio
async def test_claim_outbox_processed_dedupe_skips_emit():
    import importlib
    import strategy_factory.application.research.event_task_generator as mod

    with patch.dict(os.environ, {
        "STRATEGY_FACTORY_MANUAL_EVENT_ENABLED": "1",
        "STRATEGY_FACTORY_THEME_GRAPH_ENABLED": "1",
    }):
        importlib.reload(mod)
        db = _mock_active_event_db(claim_result={
            "claimed": False,
            "status": "processed",
            "attempts": 1,
            "dedupe_key": "claim_evt_001:claim_theme:600100",
        })

        result = await mod.generate_tasks_from_active_events(db, claim_outbox=True)

        assert result["task_count"] == 0
        assert result["lineage_count"] == 0
        assert result["outbox_claimed"] == 0
        assert result["outbox_skipped"] == 1
        assert result["outbox_failed"] == 0
        db.upsert_event_task_lineage.assert_not_awaited()
        db.mark_event_outbox_processed.assert_not_awaited()
        importlib.reload(mod)


@pytest.mark.asyncio
async def test_claim_outbox_lineage_failure_marks_failed_and_skips_emit():
    import importlib
    import strategy_factory.application.research.event_task_generator as mod

    with patch.dict(os.environ, {
        "STRATEGY_FACTORY_MANUAL_EVENT_ENABLED": "1",
        "STRATEGY_FACTORY_THEME_GRAPH_ENABLED": "1",
    }):
        importlib.reload(mod)
        db = _mock_active_event_db(lineage_side_effect=RuntimeError("lineage write failed"))

        result = await mod.generate_tasks_from_active_events(db, claim_outbox=True)

        assert result["task_count"] == 0
        assert result["lineage_count"] == 0
        assert result["outbox_claimed"] == 1
        assert result["outbox_failed"] == 1
        db.mark_event_outbox_processed.assert_not_awaited()
        db.mark_event_outbox_failed.assert_awaited_once()
        importlib.reload(mod)


@pytest.mark.asyncio
async def test_generate_with_active_events_unifies_task_source():
    """Full flow with mocked DB and enabled flags — PR-C semantics check."""
    import importlib
    import strategy_factory.application.research.event_task_generator as mod

    with patch.dict(os.environ, {
        "STRATEGY_FACTORY_MANUAL_EVENT_ENABLED": "1",
        "STRATEGY_FACTORY_THEME_GRAPH_ENABLED": "1",
        "STRATEGY_FACTORY_DYNAMIC_TARGET_COUNT_ENABLED": "0",
    }):
        importlib.reload(mod)

        db = MagicMock()
        db.list_event_injections = AsyncMock(return_value=[
            {
                "event_id": "manual_test123",
                "source": "manual",
                "event_name": "Test event",
                "event_type": "commodity",
                "direction": "positive",
                "confidence": 0.8,
                "intensity": 0.7,
                "horizon": "swing_5_20d",
                "primary_themes": [{"theme_code": "upstream_oil_gas", "direction": "positive"}],
                "valid_from": "2026-05-24T00:00:00",
                "valid_until": "2099-12-31T00:00:00",
                "status": "active",
            }
        ])
        db.get_theme_node = AsyncMock(return_value={"theme_code": "upstream_oil_gas", "breadth": "narrow"})
        db.list_theme_edges = AsyncMock(return_value=[
            {
                "target_theme_code": "airlines",
                "relation_type": "supply_shock",
                "direction_sign": -1,
                "magnitude_factor": 0.7,
                "confidence": 0.65,
                "lag_days": 1,
            }
        ])
        db.list_theme_exposure = AsyncMock(return_value=[
            {"symbol": "600028", "exposure_score": 0.8, "industry": "石油化工"},
            {"symbol": "601857", "exposure_score": 0.9, "industry": "石油开采"},
        ])
        db.upsert_event_task_lineage = AsyncMock(return_value=None)

        result = await mod.generate_tasks_from_active_events(db)

        assert result["enabled"] is True
        assert result["event_count"] == 1
        assert result["task_count"] >= 1
        assert len(result["tasks"]) >= 1

        # PR-C: every produced task is event_driven + carries event_source
        for task in result["tasks"]:
            assert task["task_source"] == "event_driven", (
                f"task_source={task.get('task_source')!r} must be 'event_driven' "
                "after PR-C unification"
            )
            assert task["event_source"] in (
                "manual", "news_llm", "macro_shock",
                "market_anomaly", "price_inference",
            ), f"invalid event_source: {task.get('event_source')!r}"
            assert "dedupe_key" in task["event_context"]
            # event from "source": "manual" → event_source=="manual"
            assert task["event_source"] == "manual"

        # Restore module
        importlib.reload(mod)


@pytest.mark.asyncio
async def test_generate_dedupe_key_is_stable_across_runs():
    """同一事件 + 同一影响 + 同一目标股池必须产生相同的 dedupe_key（不算 task_id 的随机后缀）."""
    import importlib
    import strategy_factory.application.research.event_task_generator as mod

    with patch.dict(os.environ, {
        "STRATEGY_FACTORY_MANUAL_EVENT_ENABLED": "1",
        "STRATEGY_FACTORY_THEME_GRAPH_ENABLED": "1",
        "STRATEGY_FACTORY_DYNAMIC_TARGET_COUNT_ENABLED": "0",
    }):
        importlib.reload(mod)

        async def _run_once():
            db = MagicMock()
            db.list_event_injections = AsyncMock(return_value=[
                {
                    "event_id": "stable_dedupe_evt",
                    "source": "macro_shock",
                    "event_name": "rate hike",
                    "event_type": "policy_stimulus",
                    "direction": "negative",
                    "confidence": 0.7,
                    "intensity": 0.5,
                    "horizon": "swing_5_20d",
                    "primary_themes": [{"theme_code": "real_estate_dev", "direction": "negative"}],
                    "valid_from": "2026-05-24T00:00:00",
                    "valid_until": "2099-12-31T00:00:00",
                    "status": "active",
                }
            ])
            db.get_theme_node = AsyncMock(return_value={"theme_code": "real_estate_dev", "breadth": "medium"})
            db.list_theme_edges = AsyncMock(return_value=[])
            db.list_theme_exposure = AsyncMock(return_value=[
                {"symbol": "000002", "exposure_score": 0.8, "industry": "房地产"},
                {"symbol": "001979", "exposure_score": 0.7, "industry": "房地产"},
            ])
            db.upsert_event_task_lineage = AsyncMock(return_value=None)
            return await mod.generate_tasks_from_active_events(db)

        first = await _run_once()
        second = await _run_once()
        first_keys = {t["event_context"]["dedupe_key"] for t in first["tasks"]}
        second_keys = {t["event_context"]["dedupe_key"] for t in second["tasks"]}
        assert first_keys == second_keys
        assert all(
            t["event_source"] == "macro_shock"
            for t in (first["tasks"] + second["tasks"])
        )

        importlib.reload(mod)
