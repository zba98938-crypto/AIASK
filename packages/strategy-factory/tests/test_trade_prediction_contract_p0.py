from __future__ import annotations

from pathlib import Path

import pytest

from strategy_factory.application.candidate_contract import apply_resolved_candidate_envelope
from strategy_factory.application.trade_prediction_contract import (
    DERIVED_FROM_LEGACY_CONTRACT,
    TRADE_PREDICTION_CONTRACT_READY,
    TRADE_PREDICTION_CONTRACT_REJECTED,
    TRADE_PREDICTION_CONTRACT_VERSION,
    freeze_trade_prediction_contract,
)


def _explicit_contract() -> dict:
    return {
        "strategy_id": "strategy-1",
        "stock_code": "600000",
        "prediction_as_of": "2026-06-05T09:30:00+08:00",
        "target_trading_date": "2026-06-08",
        "direction": "up",
        "confidence": 0.71,
        "horizon": "next_day",
        "evidence_refs": ["ev-1", {"id": "ev-2", "source": "headline"}],
    }


def test_freeze_trade_prediction_contract_has_stable_hash() -> None:
    first = freeze_trade_prediction_contract(_explicit_contract())
    second = freeze_trade_prediction_contract({**_explicit_contract(), "contract_version": TRADE_PREDICTION_CONTRACT_VERSION})

    assert first["status"] == TRADE_PREDICTION_CONTRACT_READY
    assert first["contract_hash"]
    assert first["contract_hash"] == second["contract_hash"]
    assert first["contract"]["contract_version"] == TRADE_PREDICTION_CONTRACT_VERSION
    assert first["contract"]["stock_code"] == "600000.SH"


@pytest.mark.parametrize(
    ("field_name", "reason"),
    [
        ("stock_code", "missing:stock_code"),
        ("target_trading_date", "missing:target_trading_date"),
        ("evidence_refs", "missing:evidence_refs"),
    ],
)
def test_trade_prediction_contract_rejects_missing_required_fields(field_name: str, reason: str) -> None:
    payload = _explicit_contract()
    payload.pop(field_name)

    frozen = freeze_trade_prediction_contract(payload)

    assert frozen["status"] == TRADE_PREDICTION_CONTRACT_REJECTED
    assert frozen["contract_hash"] is None
    assert reason in frozen["reject_reasons"]


def test_trade_prediction_contract_rejects_invalid_direction_and_confidence() -> None:
    frozen = freeze_trade_prediction_contract(
        {
            **_explicit_contract(),
            "direction": "moon",
            "confidence": 1.5,
        }
    )

    assert frozen["status"] == TRADE_PREDICTION_CONTRACT_REJECTED
    assert "invalid:direction" in frozen["reject_reasons"]
    assert "invalid:confidence" in frozen["reject_reasons"]


def test_trade_prediction_contract_can_be_derived_from_legacy_candidate() -> None:
    candidate = {
        "id": "legacy-strategy",
        "strategy_id": "legacy-strategy",
        "params": {
            "target_symbols": ["000001"],
            "as_of_date": "2026-06-05",
            "prediction_contract": {
                "claims": [
                    {
                        "id": "claim-1",
                        "direction": "bullish",
                        "confidence": 0.64,
                        "target_trading_date": "2026-06-08",
                        "horizon": "next_day",
                        "evidence_ids": ["ev-1"],
                    }
                ]
            },
            "evidence_chain": {"evidences": [{"id": "ev-1", "source": "test"}]},
        },
    }

    frozen = freeze_trade_prediction_contract(candidate)

    assert frozen["status"] == TRADE_PREDICTION_CONTRACT_READY
    assert frozen["contract"]["contract_source"] == DERIVED_FROM_LEGACY_CONTRACT
    assert frozen["contract"]["stock_code"] == "000001.SZ"
    assert frozen["contract"]["direction"] == "up"


def test_resolved_candidate_envelope_derives_ready_trade_prediction_from_legacy_fields() -> None:
    candidate = {
        "id": "legacy-resolved",
        "name": "legacy resolved",
        "strategy_type": "momentum",
        "target_symbols": ["600000"],
        "params": {
            "as_of_date": "2026-06-05",
            "holding_horizon": {"horizon": "next_day"},
            "trade_plan": {"entry_bias": "long", "holding_horizon": "next_day"},
            "prediction_contract": {
                "claims": [
                    {
                        "claim_id": "claim-1",
                        "expected_move": {"return_pct": 0.025},
                        "confidence": 0.66,
                        "evidence_ids": ["ev-1"],
                    }
                ]
            },
            "evidence_chain": {"evidences": [{"evidence_id": "ev-1", "source": "test"}]},
        },
    }

    resolved = apply_resolved_candidate_envelope(candidate)

    contract = resolved["trade_prediction_contract"]
    assert resolved["trade_prediction_contract_status"] == TRADE_PREDICTION_CONTRACT_READY
    assert resolved["trade_prediction_contract_hash"]
    assert contract["stock_code"] == "600000.SH"
    assert contract["direction"] == "up"
    assert contract["target_trading_date"] == "2026-06-08"
    assert contract["contract_source"] == DERIVED_FROM_LEGACY_CONTRACT


def test_resolved_candidate_envelope_keeps_trade_prediction_reject_reasons() -> None:
    resolved = apply_resolved_candidate_envelope(
        {
            "id": "missing-trade-prediction-fields",
            "name": "missing fields",
            "strategy_type": "momentum",
            "params": {
                "prediction_contract": {"claims": [{"claim_id": "claim-1"}]},
            },
        }
    )

    assert resolved["trade_prediction_contract_status"] == TRADE_PREDICTION_CONTRACT_REJECTED
    assert "stock_code" in resolved["trade_prediction_contract_missing_fields"]
    assert "missing:stock_code" in resolved["trade_prediction_contract_reject_reasons"]
    assert resolved["params"]["trade_prediction_contract_missing_fields"] == resolved["trade_prediction_contract_missing_fields"]
    assert resolved["params"]["trade_prediction_contract_reject_reasons"] == resolved["trade_prediction_contract_reject_reasons"]


def test_topn_target_injection_makes_local_candidate_trade_prediction_ready() -> None:
    from strategy_factory.application.research.runner import ResearchPlaneRunner
    from strategy_factory.application.semantic_contract import ensure_candidate_semantic_contract
    from strategy_factory.domain.strategy_profile import apply_candidate_strategy_profile

    candidates = [
        {
            "id": "local-targetless",
            "name": "local targetless",
            "strategy_type": "momentum",
            "holding_horizon": {"min_days": 3, "max_days": 12},
            "trade_plan": {
                "entry_bias": "trend_follow_long",
                "exit_bias": "time_stop",
            },
            "params": {
                "holding_horizon": {"min_days": 3, "max_days": 12},
                "trade_plan": {
                    "entry_bias": "trend_follow_long",
                    "exit_bias": "time_stop",
                },
            },
        }
    ]

    injected, report = ResearchPlaneRunner._inject_topn_targets_into_local_candidates(
        candidates,
        {
            "snapshot_id": "fmt-test",
            "as_of_date": "2026-06-05",
            "constituents": [
                {"code": "002241", "rank": 1},
                {"code": "000063", "rank": 2},
                {"code": "603501", "rank": 3},
            ],
        },
        {"date": "2026-06-05"},
    )
    resolved = apply_resolved_candidate_envelope(
        apply_candidate_strategy_profile(
            ensure_candidate_semantic_contract(injected[0]),
            snapshot={"date": "2026-06-05"},
        )
    )

    assert report["injected_candidate_count"] == 1
    assert set(resolved["target_symbols"]) == {"002241", "000063", "603501"}
    assert resolved["trade_prediction_contract_status"] == TRADE_PREDICTION_CONTRACT_READY
    assert resolved["trade_prediction_contract_hash"]
    assert resolved["trade_prediction_contract"]["stock_code"] in {
        "002241.SZ",
        "000063.SZ",
        "603501.SH",
    }
    assert resolved["trade_prediction_contract"]["direction"] == "neutral"
    assert resolved["trade_prediction_contract"]["direction_source"] == "target_injection_diagnostic_fallback"
    assert resolved["trade_prediction_contract"]["template_fallback_used"] is True


def test_topn_target_injection_keeps_factor_pool_broad_validation() -> None:
    from strategy_factory.application.research.runner import ResearchPlaneRunner

    candidates = [
        {
            "id": "factor-pool-targetless",
            "strategy_type": "multi_factor",
            "generator_mode": "factor_pool",
            "factor_pool_factor_id": "factor-1",
            "candidate_provenance": {
                "source_candidate_artifact_id": "factor-1",
                "generator_mode": "factor_pool",
            },
            "params": {
                "factor_pool_factor_id": "factor-1",
                "candidate_provenance": {
                    "source_candidate_artifact_id": "factor-1",
                    "generator_mode": "factor_pool",
                },
            },
        }
    ]
    constituents = [{"code": f"6005{i:02d}", "rank": i} for i in range(1, 15)]

    injected, report = ResearchPlaneRunner._inject_topn_targets_into_local_candidates(
        candidates,
        {
            "snapshot_id": "fmt-factor-pool",
            "as_of_date": "2026-06-05",
            "constituents": constituents,
        },
        {"date": "2026-06-05"},
    )

    candidate = injected[0]
    params = candidate["params"]
    assert report["injected_candidate_count"] == 1
    assert len(candidate["target_symbols"]) == 12
    assert candidate["validation_profile"]["profile"] == "factor_rank_validation"
    assert candidate["validation_profile"]["validation_focus"] == "broad_generalization"
    assert candidate["validation_profile"]["primary_validation_layer"] == "combined"
    assert params["validation_profile"] == candidate["validation_profile"]
    assert params["research_task"]["validation_focus"] == "broad_generalization"
    assert params["research_task"]["universe_expansion_policy"] == "allow_market_fallback"
    assert candidate["diagnostic_only"] is True


def test_apply_resolved_candidate_envelope_attaches_trade_prediction_fields() -> None:
    from strategy_factory.application.candidate_contract import build_logic_signature

    candidate = {
        "id": "candidate-1",
        "name": "candidate",
        "strategy_type": "momentum",
        "params": {
            "trade_prediction_contract": _explicit_contract(),
            "evidence_chain": {"evidences": [{"id": "ev-1"}]},
            "prediction_contract": {"claims": [{"id": "claim-1", "evidence_ids": ["ev-1"]}]},
            "confidence_contract": {"probability": 0.71},
        },
    }
    before_signature = build_logic_signature(candidate)

    resolved = apply_resolved_candidate_envelope(candidate)

    assert resolved["trade_prediction_contract_status"] == TRADE_PREDICTION_CONTRACT_READY
    assert resolved["trade_prediction_contract_hash"]
    assert resolved["params"]["trade_prediction_contract_hash"] == resolved["trade_prediction_contract_hash"]
    assert build_logic_signature(resolved) == before_signature


def test_submission_runtime_context_hard_fails_missing_trade_prediction_contract() -> None:
    from strategy_factory.application.submission_gate import runner

    context = runner._resolve_semantic_runtime_context(
        {
            "id": "candidate-without-trade-prediction",
            "strategy_type": "custom_rule",
            "params": {
                "evidence_chain": {"evidences": [{"evidence_id": "ev-1"}]},
                "prediction_contract": {"claims": [{"claim_id": "claim-1", "evidence_ids": ["ev-1"]}]},
                "confidence_contract": {"probability": 0.61},
            },
        }
    )

    assert context["trade_prediction_contract_status"] == "missing"
    assert "trade_prediction_contract_not_ready" in context["hard_fail_reasons"]


@pytest.mark.asyncio
async def test_submission_quality_gate_hard_rejects_missing_trade_prediction_contract(monkeypatch) -> None:
    from strategy_factory.application.submission_gate import runner

    async def _passing_statistical_gate(*_args, **_kwargs) -> dict:
        return {
            "passed": True,
            "passed_strict": True,
            "profile": "factor_rank_validation",
            "validation_focus": "target_plus_representative",
            "reasons": [],
        }

    monkeypatch.setattr(runner, "get_strategy_registry", lambda: {"custom_rule": object})
    monkeypatch.setattr(runner, "_run_statistical_gate", _passing_statistical_gate)
    monkeypatch.setattr(
        runner,
        "_resolve_validation_profile",
        lambda _strategy: {"profile": "factor_rank_validation", "validation_focus": "target_plus_representative"},
    )
    monkeypatch.setattr(runner, "_build_multiple_testing_registry", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(runner, "_inject_run_correction_metrics", lambda *_args, **_kwargs: {})

    result = await runner.run_submission_quality_gate(
        None,
        {
            "id": "candidate-without-ready-trade-prediction",
            "strategy_type": "custom_rule",
            "params": {
                "evidence_chain": {"evidences": [{"evidence_id": "ev-1"}]},
                "prediction_contract": {"claims": [{"claim_id": "claim-1", "evidence_ids": ["ev-1"]}]},
                "confidence_contract": {"probability": 0.61},
            },
        },
    )

    assert result["passed"] is False
    assert result["passed_strict"] is False
    assert result["provisional_pass"] is False
    assert "trade_prediction_contract_not_ready" in result["reasons"]
    assert "trade_prediction_contract_not_ready" in result["hard_fail_reasons"]


def test_submitter_build_strategy_data_refreezes_contract_with_final_strategy_id() -> None:
    from strategy_factory.application.submitter import StrategySubmitter

    candidate = {
        "id": "candidate-temp-id",
        "name": "trade prediction candidate",
        "strategy_type": "momentum",
        "target_symbols": ["600000"],
        "params": {
            "trade_prediction_contract": _explicit_contract(),
            "evidence_chain": {"evidences": [{"evidence_id": "ev-1"}]},
            "prediction_contract": {"claims": [{"claim_id": "claim-1", "evidence_ids": ["ev-1"]}]},
            "confidence_contract": {"probability": 0.71},
        },
    }

    data = StrategySubmitter._build_strategy_data(
        "strategy-final-id",
        "trade prediction candidate",
        candidate,
        metrics={},
    )

    contract = data["params"]["trade_prediction_contract"]
    assert data["params"]["trade_prediction_contract_status"] == TRADE_PREDICTION_CONTRACT_READY
    assert data["params"]["trade_prediction_contract_hash"]
    assert contract["strategy_id"] == "strategy-final-id"
    assert contract["contract_hash"] == data["params"]["trade_prediction_contract_hash"]


def test_llm_prompt_requires_trade_prediction_contract() -> None:
    repo_root = Path(__file__).resolve().parents[3]
    prompt_source = (
        repo_root
        / "packages"
        / "akshare-mcp"
        / "src"
        / "akshare_mcp"
        / "services"
        / "_strategy_llm_provider_prompt.py"
    ).read_text(encoding="utf-8")

    assert "trade_prediction_contract" in prompt_source
    assert prompt_source.count("trade_prediction_contract_required_fields") >= 2
    for field_name in (
        "stock_code",
        "prediction_as_of",
        "target_trading_date",
        "direction",
        "confidence",
        "horizon",
        "evidence_refs",
    ):
        assert field_name in prompt_source
    assert "direction must be up|down|neutral" in prompt_source
