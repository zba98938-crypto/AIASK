from __future__ import annotations

import asyncio


class _EventDb:
    def __init__(self, *, clusters: list[dict], signals: dict[str, list[dict]] | None = None):
        self.clusters = clusters
        self.signals = signals or {}

    def list_factory_event_clusters(self, status: str | None = None, limit: int = 200):
        rows = [
            row for row in self.clusters
            if status is None or str(row.get("status") or "") == status
        ]
        return rows[:limit]

    def list_factory_event_signals(self, event_id: str, limit: int = 24):
        return list(self.signals.get(event_id) or [])[:limit]

    def list_factory_theme_definitions(self, active_only: bool = True, limit: int = 256):
        return []


def _legacy_price_cluster() -> dict:
    return {
        "event_id": "local_theme_chip",
        "event_type": "theme_rotation",
        "event_name": "chip relative strength",
        "summary": "local price relative strength proxy",
        "direction": "positive",
        "confidence": 0.7,
        "intensity": 0.8,
        "source_types": ["local_db_rule_v1", "price_relative_strength"],
        "themes": [{"theme_code": "chip", "theme_name": "chip", "direction": "positive"}],
        "evidence": {"engine": "local_db_rule_v1"},
        "status": "active",
    }


def _verified_event_cluster() -> dict:
    validation_summary = {
        "event_signature": "sig-cninfo-001",
        "official_anchor_count": 1,
        "institutional_anchor_count": 0,
        "media_confirm_count": 0,
        "cross_source_count": 1,
        "conflict_count": 0,
        "occurrence_status": "verified_single_anchor",
        "alpha_confirmation_status": "single_anchor_unconfirmed",
        "confidence_cap_reason": "single_official_or_institutional_anchor",
    }
    evidence = {
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
        "confidence_cap_reason": "single_official_or_institutional_anchor",
        "needs_alpha_confirmation": True,
        "conflict_count": 0,
    }
    return {
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
                "target_symbols": ["000001"],
                "validation_summary": validation_summary,
                "occurrence_status": "verified_single_anchor",
                "alpha_confirmation_status": "single_anchor_unconfirmed",
                "confidence_cap_reason": "single_official_or_institutional_anchor",
                "needs_alpha_confirmation": True,
                "conflict_count": 0,
            }
        ],
        "evidence": evidence,
        "status": "active",
    }


def _conflicted_event_cluster() -> dict:
    validation_summary = {
        "event_signature": "sig-conflict-001",
        "official_anchor_count": 1,
        "institutional_anchor_count": 0,
        "media_confirm_count": 1,
        "cross_source_count": 2,
        "conflict_count": 1,
        "occurrence_status": "verified_conflicted",
        "alpha_confirmation_status": "conflicted",
        "confidence_cap_reason": "direction_conflict",
    }
    return {
        "event_id": "mevt_conflict_001",
        "event_type": "order_contract",
        "event_name": "conflicted contract announcement",
        "summary": "official and media directions conflict",
        "direction": "neutral",
        "confidence": 0.55,
        "intensity": 0.55,
        "horizon": "diagnostic",
        "source_types": ["market_events_normalized", "cninfo", "eastmoney"],
        "themes": [],
        "evidence": {
            "source": "market_events_normalized",
            "event_anchor_id": "mevt_conflict_001",
            "source_doc_uids": ["cninfo:000001:2026-06-06:001", "eastmoney:000001:2026-06-06:001"],
            "source_tier": "tier_a",
            "provider_chain": ["cninfo", "eastmoney"],
            "reliability_score": 0.55,
            "normalized_event_status": "verified",
            "evidence_time": "2026-06-06T10:00:00+08:00",
            "verified_event_anchor": True,
            "validation_summary": validation_summary,
            "occurrence_status": "verified_conflicted",
            "alpha_confirmation_status": "conflicted",
            "confidence_cap_reason": "direction_conflict",
            "needs_alpha_confirmation": False,
            "conflict_count": 1,
        },
        "status": "diagnostic",
    }


def test_collector_keeps_price_proxy_events_diagnostic_only():
    from strategy_factory.application.collect import DataCollector

    payload, status, reason, details = asyncio.run(
        DataCollector._collect_event_driven_snapshot(
            _EventDb(
                clusters=[_legacy_price_cluster()],
                signals={
                    "local_theme_chip": [
                        {
                            "symbol": "000001",
                            "theme_code": "chip",
                            "direction": "positive",
                            "final_score": 0.8,
                            "rationale": "price proxy",
                        }
                    ]
                },
            )
        )
    )

    assert status == "success"
    assert reason is None
    assert payload["event_count"] == 0
    assert payload["tasks_ready_count"] == 0
    assert payload["verified_event_count"] == 0
    assert payload["diagnostic_events"][0]["reason"] == "not_normalized_event_source"
    assert details["diagnostic_event_count"] == 1


def test_verified_normalized_event_builds_event_task_with_anchor():
    from strategy_factory.application.collect import DataCollector
    from strategy_factory.application.opportunity import MarketOpportunityScanner

    payload, status, _reason, _details = asyncio.run(
        DataCollector._collect_event_driven_snapshot(
            _EventDb(
                clusters=[_verified_event_cluster()],
                signals={
                    "mevt_cninfo_001": [
                        {
                            "symbol": "000001",
                            "theme_code": "order_contract",
                            "direction": "positive",
                            "final_score": 0.92,
                            "theme_score": 0.92,
                            "rationale": "official disclosure",
                        }
                    ]
                },
            )
        )
    )
    assert status == "success"
    assert payload["event_count"] == 1
    assert payload["verified_event_count"] == 1
    assert payload["single_anchor_event_count"] == 1
    event = payload["events"][0]
    assert event["event_anchor_id"] == "mevt_cninfo_001"
    assert event["source_doc_uids"] == ["cninfo:000001:2026-06-06:001"]
    assert event["source_tier"] == "tier_a"
    assert event["alpha_confirmation_status"] == "single_anchor_unconfirmed"
    assert event["needs_alpha_confirmation"] is True

    tasks = MarketOpportunityScanner._build_event_driven_tasks(
        {"date": "2026-06-06", "event_driven": payload},
        [{"code": "000001", "name": "Ping An Bank", "industry": "bank"}],
    )

    assert len(tasks) == 1
    task = tasks[0]
    assert task["task_source"] == "event_driven"
    assert task["event_source"] == "market_events_normalized"
    assert task["event_anchor_id"] == "mevt_cninfo_001"
    assert task["source_doc_uids"] == ["cninfo:000001:2026-06-06:001"]
    assert task["alpha_confirmation_status"] == "single_anchor_unconfirmed"
    assert task["needs_alpha_confirmation"] is True
    assert task["event_context"]["verified_event_anchor"] is True
    assert task["event_context"]["needs_alpha_confirmation"] is True


def test_conflicted_diagnostic_event_counts_but_does_not_build_task():
    from strategy_factory.application.collect import DataCollector
    from strategy_factory.application.opportunity import MarketOpportunityScanner

    payload, status, _reason, details = asyncio.run(
        DataCollector._collect_event_driven_snapshot(
            _EventDb(clusters=[_conflicted_event_cluster()])
        )
    )

    assert status == "success"
    assert payload["event_count"] == 0
    assert payload["conflict_event_count"] == 1
    assert payload["diagnostic_events"][0]["reason"] == "direction_conflict"
    assert payload["diagnostic_events"][0]["alpha_confirmation_status"] == "conflicted"
    assert details["conflict_event_count"] == 1

    tasks = MarketOpportunityScanner._build_event_driven_tasks(
        {"date": "2026-06-06", "event_driven": payload},
        [{"code": "000001", "name": "Ping An Bank", "industry": "bank"}],
    )

    assert tasks == []


def test_spawner_event_driven_requires_verified_normalized_anchor():
    from strategy_factory.domain.spawner import StrategySpawner

    spawner = StrategySpawner()
    legacy = spawner._from_event_driven({"event_driven": {"events": [_legacy_price_cluster()]}})
    assert legacy == []

    verified_event = _verified_event_cluster()
    verified_event["source"] = "market_events_normalized"
    verified_event["source_tier"] = "tier_a"
    verified_event["source_doc_uids"] = ["cninfo:000001:2026-06-06:001"]
    verified_event["event_anchor_id"] = "mevt_cninfo_001"
    verified_event["verified_event_anchor"] = True
    verified_event["themes"][0].update(
        {
            "source": "market_events_normalized",
            "source_tier": "tier_a",
            "source_doc_uids": ["cninfo:000001:2026-06-06:001"],
            "event_anchor_id": "mevt_cninfo_001",
            "verified_event_anchor": True,
            "preferred_strategy_types": ["event_structure_breakout"],
            "score_summary": {"avg_final_score": 0.92, "max_final_score": 0.92},
            "alpha_confirmation_status": "single_anchor_unconfirmed",
            "needs_alpha_confirmation": True,
        }
    )

    candidates = spawner._from_event_driven({"event_driven": {"events": [verified_event]}})
    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate["strategy_type"] == "event_structure_breakout"
    assert candidate["event_anchor"]["source"] == "market_events_normalized"
    assert candidate["event_anchor"]["source_doc_uids"] == ["cninfo:000001:2026-06-06:001"]
    assert candidate["event_anchor"]["alpha_confirmation_status"] == "single_anchor_unconfirmed"
    assert candidate["event_anchor"]["needs_alpha_confirmation"] is True
    assert candidate["event_prefilter"]["passed"] is True


def test_spawner_rejects_conflicted_normalized_event_anchor():
    from strategy_factory.domain.spawner import StrategySpawner

    conflicted = _conflicted_event_cluster()
    conflicted["source"] = "market_events_normalized"
    conflicted["source_tier"] = "tier_a"
    conflicted["source_doc_uids"] = ["cninfo:000001:2026-06-06:001", "eastmoney:000001:2026-06-06:001"]
    conflicted["event_anchor_id"] = "mevt_conflict_001"
    conflicted["verified_event_anchor"] = True
    conflicted["themes"] = [
        {
            "theme_code": "order_contract",
            "theme_name": "order contract",
            "direction": "neutral",
            "target_symbols": ["000001"],
            "source": "market_events_normalized",
            "source_tier": "tier_a",
            "source_doc_uids": ["cninfo:000001:2026-06-06:001", "eastmoney:000001:2026-06-06:001"],
            "event_anchor_id": "mevt_conflict_001",
            "verified_event_anchor": True,
            "preferred_strategy_types": ["event_structure_breakout"],
            "score_summary": {"avg_final_score": 0.55, "max_final_score": 0.55},
            "validation_summary": conflicted["evidence"]["validation_summary"],
            "occurrence_status": "verified_conflicted",
            "alpha_confirmation_status": "conflicted",
            "confidence_cap_reason": "direction_conflict",
            "conflict_count": 1,
        }
    ]

    candidates = StrategySpawner()._from_event_driven({"event_driven": {"events": [conflicted]}})

    assert candidates == []
