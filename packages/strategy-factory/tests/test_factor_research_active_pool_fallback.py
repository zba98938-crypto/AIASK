from __future__ import annotations

from datetime import date


def test_active_factor_pool_fallback_uses_eligible_legacy_active_factors() -> None:
    from strategy_factory.application.research._research_build_steps import (
        apply_active_factor_pool_fallback,
    )
    from strategy_factory.application.research.factor_research_builder import (
        FactorResearchBuilder,
    )

    factors = [
        {
            "factor_id": "factor_legacy_active",
            "name": "legacy_active_alpha",
            "family": "momentum",
            "status": "active",
            "expression_dsl": "close / ma(close, 20) - 1",
            "admission_grade": "A",
            "fitness": 1.25,
            "last_evaluated_at": "2026-06-06T22:00:00+00:00",
            "validation_summary": {},
        },
        {
            "factor_id": "factor_quarantine",
            "name": "quarantine_alpha",
            "family": "momentum",
            "status": "quarantine",
            "expression_dsl": "close / ma(close, 10) - 1",
            "admission_grade": "A",
            "fitness": 99.0,
            "validation_summary": {"quality_status": "quarantine"},
        },
    ]
    eligible = [
        item
        for item in factors
        if FactorResearchBuilder._is_eligible_active_pool_factor(item)
    ]
    runtime_context = {
        "governed_top_candidates": [],
        "active_candidate_pool": {},
    }

    candidates = apply_active_factor_pool_fallback(
        FactorResearchBuilder,
        runtime_context,
        eligible,
        snapshot_date=date(2026, 6, 7),
    )

    assert [item["name"] for item in candidates] == ["legacy_active_alpha"]
    assert candidates[0]["factor_pool_factor_id"] == "factor_legacy_active"
    assert candidates[0]["expression_dsl"] == "close / ma(close, 20) - 1"
    assert candidates[0]["factor_pool"]["expression_dsl"] == "close / ma(close, 20) - 1"
    assert runtime_context["governed_candidate_pool_mode"] == "active_factor_pool_fallback"
    assert runtime_context["active_candidate_pool"]["count"] == 1
    assert runtime_context["governed_source_candidate_count"] == 1
    assert runtime_context["active_candidate_pool"]["top_candidates"][0]["pool_entry_mode"] == (
        "active_factor_pool_fallback"
    )


def test_ranked_factor_context_preserves_five_active_pool_factors() -> None:
    from strategy_factory.application.research._research_build_steps import (
        build_ranked_factor_context,
    )
    from strategy_factory.application.research.factor_research_builder import (
        FactorResearchBuilder,
    )

    governed_candidates = [
        {"name": f"gp_factor_{idx}", "family": f"gp_factor_{idx}"}
        for idx in range(1, 6)
    ]

    context = build_ranked_factor_context(
        FactorResearchBuilder,
        factor_ic={},
        factor_trend={},
        names=[],
        history_meta={},
        governed_top_candidates=governed_candidates,
        snapshot={},
    )

    assert context["active_factors"] == [
        "gp_factor_1",
        "gp_factor_2",
        "gp_factor_3",
        "gp_factor_4",
        "gp_factor_5",
    ]
    assert context["top_factor_names"] == context["active_factors"]


def test_active_pool_factors_rank_by_validation_strength_before_spawn() -> None:
    from strategy_factory.application.research.factor_research_builder import (
        FactorResearchBuilder,
    )

    weak = {
        "factor_id": "weak",
        "name": "weak_alpha",
        "status": "active",
        "expression_dsl": "rank(close)",
        "admission_grade": "A",
        "current_ic": 0.08,
        "fitness": 99.0,
        "validation_summary": {
            "rating": {"grade": "A", "total_score": 70.0},
            "metrics": {"rank_ic_ir": 0.4, "rank_ic_mean": 0.02},
        },
    }
    strong = {
        "factor_id": "strong",
        "name": "strong_alpha",
        "status": "active",
        "expression_dsl": "rank(return_20d)",
        "admission_grade": "A",
        "current_ic": 0.05,
        "fitness": 80.0,
        "validation_summary": {
            "rating": {"grade": "A", "total_score": 91.0},
            "metrics": {"rank_ic_ir": 1.2, "rank_ic_mean": 0.12},
        },
    }

    ranked = FactorResearchBuilder._rank_active_pool_factors([weak, strong])

    assert [item["factor_id"] for item in ranked] == ["strong", "weak"]


def test_fresh_governed_pool_downgrades_history_stale_to_warning() -> None:
    from strategy_factory.application.research._research_artifact_payload import (
        build_factor_research_artifact_payload,
    )
    from strategy_factory.application.research.factor_research_builder import (
        FactorResearchBuilder,
    )

    payload = build_factor_research_artifact_payload(
        FactorResearchBuilder,
        snapshot={},
        snapshot_date=date(2026, 6, 9),
        latest_factor_date=date(2026, 6, 1),
        history_meta={},
        factor_ic_source={"status": "success"},
        factor_context={
            "ranked_factors": [],
            "positive_rising_factors": [],
            "active_factors": ["gp_factor_1", "gp_factor_2"],
            "preferred_strategy_types": ["momentum"],
            "top_factor_names": ["gp_factor_1", "gp_factor_2"],
        },
        runtime_context={
            "governed_pool": {
                "available": True,
                "active_pool": {"count": 2},
            },
            "active_candidate_pool": {"count": 2},
            "governed_candidate_pool_mode": "active_factor_pool_fallback",
            "governed_top_candidates": [
                {
                    "name": "gp_factor_1",
                    "family": "gp_factor_1",
                    "latest_validation_at": "2026-06-09T00:00:00+00:00",
                }
            ],
            "governed_source_candidate_count": 2,
            "governed_active_registry_candidate_count": 2,
            "governed_latest_candidate_at": "2026-06-09T00:00:00+00:00",
            "governed_freshness_days": 0,
            "scheduler_status": {},
            "scheduler_quality_flags": [],
            "scheduler_recent_success": False,
        },
        stock_family_allocation={},
        stock_family_allocation_summary={},
        lightweight_mock_fallback=False,
    )

    assert payload["history_stale"] is True
    assert payload["governed_pool_fresh"] is True
    assert payload["stale"] is False
    assert "factor_history_stale" in payload["quality_flags"]
    assert "stale" not in payload["quality_flags"]
    assert payload["summary"]["history_stale"] is True
    assert payload["summary"]["stale"] is False


def test_promoted_active_pool_factor_is_consumed_by_research_context() -> None:
    from strategy_factory.application.research._research_build_steps import (
        apply_active_factor_pool_fallback,
        build_ranked_factor_context,
    )
    from strategy_factory.application.research.factor_research_builder import (
        FactorResearchBuilder,
    )

    factors = [
        {
            "factor_id": "factor_promoted",
            "name": "promoted_alpha",
            "family": "momentum",
            "status": "active",
            "expression_dsl": "close / ma(close, 20) - 1",
            "admission_grade": "B",
            "validation_summary": {"quality_status": "promoted"},
            "fitness": 1.5,
        },
        {
            "factor_id": "factor_quarantine",
            "name": "quarantine_alpha",
            "family": "momentum",
            "status": "active",
            "expression_dsl": "close / ma(close, 10) - 1",
            "admission_grade": "A",
            "validation_summary": {"quality_status": "quarantine"},
            "fitness": 2.5,
        },
    ]
    eligible = [
        item
        for item in factors
        if FactorResearchBuilder._is_eligible_active_pool_factor(item)
    ]
    runtime_context = {
        "governed_top_candidates": [],
        "active_candidate_pool": {},
    }

    candidates = apply_active_factor_pool_fallback(
        FactorResearchBuilder,
        runtime_context,
        eligible,
        snapshot_date=date(2026, 6, 7),
    )
    context = build_ranked_factor_context(
        FactorResearchBuilder,
        factor_ic={"promoted_alpha": 0.08},
        factor_trend={"promoted_alpha": "rising"},
        names=[],
        history_meta={},
        governed_top_candidates=candidates,
        snapshot={},
    )

    assert [item["name"] for item in candidates] == ["promoted_alpha"]
    assert candidates[0]["pool_entry_mode"] == "factor_mining_active_pool"
    assert candidates[0]["factor_pool_factor_id"] == "factor_promoted"
    assert candidates[0]["expression_dsl"] == "close / ma(close, 20) - 1"
    assert context["active_factors"] == ["promoted_alpha"]
    assert context["top_factor_names"] == ["promoted_alpha"]
    assert "momentum" in context["preferred_strategy_types"]
    assert runtime_context["active_candidate_pool"]["count"] == 1
    assert runtime_context["governed_candidate_pool_mode"] == "factor_mining_active_pool"
    assert runtime_context["active_factor_pool_fallback"] is False
    assert runtime_context["factor_mining_active_pool_count"] == 1
    assert runtime_context["governed_source_candidate_count"] == 1


def test_active_pool_fallback_rejects_retire_recommended_promoted_factor() -> None:
    from strategy_factory.application.research.factor_research_builder import (
        FactorResearchBuilder,
    )

    factor = {
        "factor_id": "factor_promoted_but_retire",
        "name": "promoted_but_retire_alpha",
        "family": "momentum",
        "status": "active",
        "expression_dsl": "close / ma(close, 20) - 1",
        "admission_grade": "A",
        "validation_summary": {
            "quality_status": "promoted",
            "qc_shelf_decision": {"decision": "retire", "reasons": ["oos_not_passed"]},
        },
        "fitness": 95.0,
    }

    assert FactorResearchBuilder._is_eligible_active_pool_factor(factor) is False


def test_active_pool_fallback_keeps_promoted_factor_with_stale_zero_qc_advisory() -> None:
    from strategy_factory.application.research.factor_research_builder import (
        FactorResearchBuilder,
    )

    factor = {
        "factor_id": "factor_promoted_stale_qc",
        "name": "promoted_stale_qc_alpha",
        "family": "momentum",
        "status": "active",
        "expression_dsl": "rank(close)",
        "admission_grade": "A",
        "validation_summary": {
            "quality_status": "promoted",
            "qc_shelf_decision": {"decision": "retire", "reasons": ["oos_not_passed"]},
            "qc_labels": {
                "rank_ic_ir": 0.0,
                "bootstrap_ci_lower": 0.0,
                "oos_pass": False,
                "oos_grade": "unknown",
                "monotonicity": 0.0,
                "long_short_return": 0.0,
                "window_stability": 0.0,
                "param_sensitivity": 0.0,
                "dsr": 0.0,
                "pbo": 0.0,
            },
        },
        "fitness": 95.0,
    }

    assert FactorResearchBuilder._is_eligible_active_pool_factor(factor) is True


def test_active_pool_consumption_rejects_non_promoted_retire_and_negative_current_ic() -> None:
    from strategy_factory.application.research.factor_research_builder import (
        FactorResearchBuilder,
    )

    non_promoted_retire = {
        "factor_id": "factor_retire_watch",
        "name": "retire_watch_alpha",
        "family": "momentum",
        "status": "active",
        "expression_dsl": "rank(close)",
        "admission_grade": "A",
        "current_ic": 0.08,
        "validation_summary": {
            "qc_shelf_decision": {"decision": "retire", "reasons": ["oos_not_passed"]},
            "qc_labels": {
                "rank_ic_ir": 0.0,
                "bootstrap_ci_lower": 0.0,
                "oos_pass": False,
                "oos_grade": "unknown",
            },
        },
    }
    negative_current_ic = {
        "factor_id": "factor_negative_ic",
        "name": "negative_ic_alpha",
        "family": "momentum",
        "status": "active",
        "expression_dsl": "rank(open)",
        "admission_grade": "A",
        "current_ic": -0.01,
        "validation_summary": {"quality_status": "promoted"},
    }

    assert FactorResearchBuilder._is_eligible_active_pool_factor(non_promoted_retire) is False
    assert FactorResearchBuilder._is_eligible_active_pool_factor(negative_current_ic) is False


def test_active_pool_fallback_replaces_provisional_only_governed_pool() -> None:
    from strategy_factory.application.research._research_build_steps import (
        apply_active_factor_pool_fallback,
    )
    from strategy_factory.application.research.factor_research_builder import (
        FactorResearchBuilder,
    )

    runtime_context = {
        "governed_top_candidates": [
            {
                "artifact_id": "provisional_watch",
                "name": "provisional_watch",
                "family": "momentum",
            }
        ],
        "governed_candidate_pool_strict_count": 0,
        "governed_candidate_pool_provisional_count": 1,
        "active_candidate_pool": {
            "active_pool_mode": "provisional_validated_watch",
            "count": 1,
            "strict_count": 0,
            "provisional_count": 1,
        },
    }
    factors = [
        {
            "factor_id": "factor_promoted",
            "name": "promoted_alpha",
            "family": "momentum",
            "status": "active",
            "expression_dsl": "rank(close)",
            "admission_grade": "A",
            "validation_summary": {"quality_status": "promoted"},
            "fitness": 1.5,
        }
    ]

    candidates = apply_active_factor_pool_fallback(
        FactorResearchBuilder,
        runtime_context,
        factors,
        snapshot_date=date(2026, 6, 7),
    )

    assert [item["name"] for item in candidates] == ["promoted_alpha"]
    assert runtime_context["governed_candidate_pool_mode"] == "factor_mining_active_pool"
    assert runtime_context["governed_candidate_pool_provisional"] is False
    assert runtime_context["governed_candidate_pool_strict_count"] == 1
    assert runtime_context["active_candidate_pool"]["strict_count"] == 1
