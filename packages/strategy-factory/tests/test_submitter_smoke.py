"""Smoke tests for StrategySubmitter import and basic structure."""

from __future__ import annotations

import asyncio


def test_submitter_import():
    from strategy_factory.application.submitter import StrategySubmitter

    submitter = StrategySubmitter()
    assert submitter is not None
    assert hasattr(submitter, "submit")


def test_submitter_mro_chain():
    """Verify the mixin chain is correctly assembled."""
    from strategy_factory.application.submitter import StrategySubmitter

    mro_names = [c.__name__ for c in StrategySubmitter.__mro__]
    assert "_StrategySubmitterHelpersMixin" in mro_names
    assert "_StrategySubmitterPolicyMixin" in mro_names
    assert "_StrategySubmitterActionsMixin" in mro_names


def test_submitter_candidate_provenance_reads_params_payload():
    from strategy_factory.application.submitter import StrategySubmitter

    provenance = StrategySubmitter._candidate_provenance(
        {
            "strategy_type": "multi_factor",
            "params": {
                "candidate_provenance": {
                    "source_candidate_artifact_id": "factor-1",
                    "source_validation_artifact_id": "factor-1",
                    "candidate_family": "liquidity",
                    "generator_mode": "factor_pool",
                    "alpha_source": "factor_mining_active_pool",
                }
            },
        }
    )

    assert provenance["source_candidate_artifact_id"] == "factor-1"
    assert provenance["source_validation_artifact_id"] == "factor-1"
    assert provenance["candidate_family"] == "liquidity"
    assert provenance["generator_mode"] == "factor_pool"
    assert provenance["alpha_source"] == "factor_mining_active_pool"


def test_submitter_candidate_provenance_reassembles_params_fields():
    from strategy_factory.application.submitter import StrategySubmitter

    provenance = StrategySubmitter._candidate_provenance(
        {
            "strategy_type": "macro_timing",
            "params": {
                "source_candidate_artifact_id": "factor-2",
                "source_validation_artifact_id": "factor-2",
                "candidate_family": "liquidity",
                "generator_mode": "factor_pool",
                "alpha_source": "factor_mining_active_pool",
                "candidate_registry_stage": "active_factor_pool",
                "candidate_validation_score": 77.0,
            },
        }
    )

    assert provenance["source_candidate_artifact_id"] == "factor-2"
    assert provenance["source_validation_artifact_id"] == "factor-2"
    assert provenance["candidate_family"] == "liquidity"
    assert provenance["generator_mode"] == "factor_pool"
    assert provenance["alpha_source"] == "factor_mining_active_pool"
    assert provenance["candidate_registry_stage"] == "active_factor_pool"
    assert provenance["validation_score"] == 77.0


def test_submitter_build_strategy_data_persists_explanation_contract():
    from strategy_factory.application.submitter import StrategySubmitter

    data = StrategySubmitter._build_strategy_data(
        "strategy-explained",
        "explained momentum",
        {
            "id": "candidate-explained",
            "name": "explained momentum",
            "description": "Momentum continuation after sector strength.",
            "strategy_type": "momentum",
            "target_symbols": ["600000"],
            "tags": ["external_llm"],
            "params": {
                "lookback": 20,
                "threshold": 0.02,
            },
            "generation_reason": {
                "source": "external_llm",
                "provider": "openai",
                "model": "test-model",
                "rationale": "Relative strength and liquidity support a continuation setup.",
            },
            "research_task": {
                "task_id": "task-submit-explain",
                "theme": "relative strength",
                "opportunity_type": "sector_breakout",
                "candidate_family": "momentum",
            },
            "trade_plan": {
                "entry_bias": "momentum_confirmation",
                "exit_bias": "momentum_decay",
            },
            "risk_rules": {"stop_loss_pct": 0.08},
        },
        metrics={"sharpe_ratio": 1.4, "total_return": 0.12, "max_drawdown": -0.05},
    )

    explanation = data["params"]["strategy_explanation"]
    assert explanation["version"] == "strategy_explanation.v1"
    assert explanation["summary"] == "Momentum continuation after sector strength."
    assert "source=external_llm" in explanation["why_generated"]
    assert explanation["target_scope"]["symbols"] == ["600000"]
    assert "Why:" in data["description"]
    assert "Targets: 600000" in data["description"]
    assert "Backtest:" in data["description"]
    assert "strategy_explained" in data["tags"]
    assert "type:momentum" in data["tags"]


def test_submitter_birth_regime_includes_compact_market_temperature_context():
    from strategy_factory.application.submitter import StrategySubmitter

    birth_regime = StrategySubmitter._extract_birth_regime(
        {
            "date": "2026-06-08",
            "fg_level": "neutral",
            "market_internals": {
                "market_temperature": {
                    "as_of": "2026-06-08",
                    "market_temperature": 52.4,
                    "market_state": "neutral",
                    "quality_status": "healthy",
                    "readiness_status": "ready",
                    "staleness_days": 0,
                    "stock_count": 988,
                    "industry_count": 31,
                    "source_chain": ["market_temperature_snapshots", "market_temperature.service"],
                }
            },
        }
    )

    context = birth_regime["market_temperature_context"]
    assert context["as_of"] == "2026-06-08"
    assert context["temperature"] == 52.4
    assert context["state"] == "neutral"
    assert context["quality_status"] == "healthy"
    assert context["readiness_status"] == "ready"
    assert context["stock_count"] == 988
    assert context["industry_count"] == 31
    assert "market" not in context


def test_submission_quality_gate_importable():
    from strategy_factory.application.submission_gate import run_submission_quality_gate

    assert callable(run_submission_quality_gate)


def test_candidate_semantic_contract_backfill_is_complete():
    from strategy_factory.application.semantic_contract import (
        audit_candidate_semantic_contract,
        ensure_candidate_semantic_contract,
    )

    candidate = ensure_candidate_semantic_contract(
        {
            "strategy_type": "value_factor",
            "name": "value rotation",
            "params": {"lookback": 20},
            "target_symbols": ["600000"],
            "holding_horizon": {"max_days": 20},
            "risk_rules": {"max_holding_days": 20},
        }
    )

    assert candidate["evidence_chain"]["evidences"]
    assert candidate["prediction_contract"]["claims"]
    assert candidate["confidence_contract"]["prediction_quality"]
    assert candidate["claim_to_trade_plan_map"]["mapped_claim_count"] == 2
    audit = audit_candidate_semantic_contract(candidate)
    assert audit["using_new_contract"] is True
    assert audit["hard_fail_reasons"] == []


def test_candidate_semantic_contract_backfills_trade_plan_claim_ids_from_real_claims():
    from strategy_factory.application.semantic_contract import (
        audit_candidate_semantic_contract,
        ensure_candidate_semantic_contract,
    )

    candidate = ensure_candidate_semantic_contract(
        {
            "strategy_type": "momentum",
            "name": "real claim candidate",
            "target_symbols": ["600000"],
            "holding_horizon": {"max_days": 10},
            "trade_plan": {
                "entry": {"node_id": "entry_real", "summary": "enter on signal"},
                "exit": {"node_id": "exit_real", "summary": "exit on invalidation"},
            },
            "evidence_chain": {
                "evidences": [
                    {"evidence_id": "ev_up", "source_type": "technical", "direction": "up"},
                    {"evidence_id": "ev_exit", "source_type": "risk_contract", "direction": "down"},
                ]
            },
            "prediction_contract": {
                "claims": [
                    {
                        "claim_id": "momentum_claim_entry",
                        "claim_type": "entry",
                        "expected_move": "up",
                        "evidence_ids": ["ev_up"],
                    },
                    {
                        "claim_id": "momentum_claim_exit",
                        "claim_type": "exit",
                        "expected_move": "down",
                        "evidence_ids": ["ev_exit"],
                    },
                ]
            },
        }
    )

    assert candidate["trade_plan"]["entry"]["claim_ids"] == ["momentum_claim_entry"]
    assert candidate["trade_plan"]["entry"]["evidence_ids"] == ["ev_up"]
    assert candidate["trade_plan"]["exit"]["claim_ids"] == ["momentum_claim_exit"]
    assert candidate["trade_plan"]["exit"]["evidence_ids"] == ["ev_exit"]
    assert candidate["claim_to_trade_plan_map"]["trade_step_to_claim_ids"]["entry_real"] == [
        "momentum_claim_entry"
    ]
    assert candidate["claim_to_trade_plan_map"]["trade_step_to_claim_ids"]["exit_real"] == [
        "momentum_claim_exit"
    ]

    audit = audit_candidate_semantic_contract(candidate)
    assert "trade_plan_node_missing_claim_ids" not in audit["hard_fail_reasons"]
    assert audit["trade_plan_missing_claim_ids"] == 0


def test_candidate_semantic_contract_preserves_invalid_explicit_claim_ids_as_hard_fail():
    from strategy_factory.application.semantic_contract import (
        audit_candidate_semantic_contract,
        ensure_candidate_semantic_contract,
    )

    candidate = ensure_candidate_semantic_contract(
        {
            "strategy_type": "momentum",
            "name": "bad explicit claim candidate",
            "target_symbols": ["600000"],
            "trade_plan": {
                "entry": {
                    "node_id": "entry_bad",
                    "claim_ids": ["not_a_real_claim"],
                }
            },
            "evidence_chain": {
                "evidences": [
                    {"evidence_id": "ev_up", "source_type": "technical", "direction": "up"},
                ]
            },
            "prediction_contract": {
                "claims": [
                    {
                        "claim_id": "momentum_claim_entry",
                        "claim_type": "entry",
                        "expected_move": "up",
                        "evidence_ids": ["ev_up"],
                    }
                ]
            },
        }
    )

    assert candidate["trade_plan"]["entry"]["claim_ids"] == ["not_a_real_claim"]
    audit = audit_candidate_semantic_contract(candidate)
    assert "trade_plan_node_missing_claim_ids" in audit["hard_fail_reasons"]


def test_submitter_reports_factor_pool_performance():
    from strategy_factory.application.submitter import StrategySubmitter

    class FactorPoolGateway:
        def __init__(self):
            self.calls = []

        async def report_factor_performance(self, factor_id, strategy_id, metrics):
            self.calls.append((factor_id, strategy_id, dict(metrics)))

    gateway = FactorPoolGateway()
    submitter = StrategySubmitter(factor_pool_gateway=gateway)

    result = asyncio.run(
        submitter._report_factor_performance_after_submit(
            candidate={"params": {"factor_pool_factor_id": "factor-1"}},
            strategy_id="strategy-1",
            metrics={"ic_mean": 0.07, "turnover_rate": 0.2},
            validation_report=None,
            gate={"passed": True},
            read_only=False,
        )
    )

    assert result["reported"] is True
    assert gateway.calls == [
        (
            "factor-1",
            "strategy-1",
            {
                "realized_ic": 0.07,
                "realized_turnover": 0.2,
                "realized_cost": 0.0,
                "period": "submission",
            },
        )
    ]
