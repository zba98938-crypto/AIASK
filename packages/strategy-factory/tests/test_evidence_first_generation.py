from __future__ import annotations

import math

from strategy_factory.application.trade_prediction_contract import (
    TRADE_PREDICTION_CONTRACT_READY,
    freeze_trade_prediction_contract,
)
from strategy_factory.domain.market_evidence import (
    apply_evidence_first_candidate,
    build_market_evidence_pack,
    summarize_generation_quality,
)


def _assert_all_finite(value) -> None:
    if isinstance(value, dict):
        for nested in value.values():
            _assert_all_finite(nested)
        return
    if isinstance(value, list):
        for nested in value:
            _assert_all_finite(nested)
        return
    if isinstance(value, float):
        assert math.isfinite(value)


def test_positive_factor_ic_resolves_up_with_calibrated_confidence() -> None:
    candidate = {
        "id": "factor-up",
        "strategy_type": "multi_factor",
        "source": "factor_ic",
        "target_symbols": ["600000"],
        "params": {
            "factor_name": "quality_growth",
            "factor_ic": 0.082,
            "factor_ic_trend": "rising",
            "prediction_as_of": "2026-06-05",
        },
    }

    resolved = apply_evidence_first_candidate(candidate, snapshot={"date": "2026-06-05"})

    assert resolved["direction_resolution"]["direction"] == "up"
    assert resolved["confidence"] != 0.55
    assert 0.35 <= resolved["confidence"] <= 0.85
    assert resolved["market_evidence_pack"]["factor_backed"] is True
    assert resolved["non_proxy_evidence_ratio"] == 1.0
    assert resolved["template_dominance_score"] == 0.0


def test_market_evidence_exit_claim_is_not_direction_contradiction() -> None:
    from strategy_factory.application.semantic_contract import audit_candidate_semantic_contract

    candidate = {
        "id": "factor-up",
        "strategy_type": "multi_factor",
        "source": "factor_ic",
        "target_symbols": ["600000"],
        "params": {
            "factor_name": "quality_growth",
            "factor_ic": 0.082,
            "factor_ic_trend": "rising",
            "prediction_as_of": "2026-06-05",
        },
    }

    resolved = apply_evidence_first_candidate(candidate, snapshot={"date": "2026-06-05"})
    audit = audit_candidate_semantic_contract(resolved)

    assert resolved["prediction_contract"]["claims"][1]["claim_id"] == "claim_exit"
    assert resolved["prediction_contract"]["claims"][1]["expected_move"] == "risk_or_time_stop"
    assert audit["contradiction_count"] == 0
    assert "semantic_contract_contradiction_detected" not in audit["hard_fail_reasons"]


def test_negative_factor_ic_resolves_down() -> None:
    candidate = {
        "id": "factor-down",
        "strategy_type": "multi_factor",
        "source": "factor_ic",
        "target_symbols": ["600000"],
        "params": {
            "factor_name": "crowding_risk",
            "factor_ic": -0.074,
            "factor_ic_trend": "falling",
            "prediction_as_of": "2026-06-05",
        },
    }

    resolved = apply_evidence_first_candidate(candidate, snapshot={"date": "2026-06-05"})

    assert resolved["direction_resolution"]["direction"] == "down"
    assert resolved["prediction_contract"]["claims"][0]["expected_move"] == "down"
    assert resolved["prediction_contract"]["target"] == "forward_return_negative"


def test_conflicting_market_evidence_resolves_neutral_and_lowers_confidence() -> None:
    candidate = {
        "id": "factor-conflict",
        "strategy_type": "multi_factor",
        "target_symbols": ["600000"],
        "evidence_chain": {
            "evidences": [
                {
                    "evidence_id": "ev_factor_pos",
                    "source_type": "factor_ic_validated",
                    "direction": "up",
                    "proxy_only": False,
                    "support_metric": {"ic_value": 0.04},
                },
                {
                    "evidence_id": "ev_factor_neg",
                    "source_type": "factor_ic_validated",
                    "direction": "down",
                    "proxy_only": False,
                    "support_metric": {"ic_value": -0.04},
                },
            ]
        },
        "params": {"prediction_as_of": "2026-06-05"},
    }

    resolved = apply_evidence_first_candidate(candidate, snapshot={"date": "2026-06-05"})

    assert resolved["direction_resolution"]["direction"] == "neutral"
    assert resolved["direction_resolution"]["direction_source"] == "conflicting_market_evidence"
    assert resolved["confidence"] <= 0.52
    assert resolved["direction_resolution"]["conflict_count"] >= 1


def test_template_only_candidate_is_diagnostic_neutral_not_fake_alpha() -> None:
    candidate = {
        "id": "template-only",
        "strategy_type": "momentum",
        "target_symbols": ["600000"],
        "trade_plan": {"entry_bias": "trend_follow_long"},
        "params": {"prediction_as_of": "2026-06-05"},
    }

    resolved = apply_evidence_first_candidate(candidate, snapshot={"date": "2026-06-05"})

    assert resolved["direction_resolution"]["direction"] == "neutral"
    assert resolved["direction_resolution"]["direction_source"] == "template_fallback_diagnostic"
    assert resolved["template_dominance_score"] == 1.0
    assert resolved["non_proxy_evidence_ratio"] == 0.0
    assert resolved["diagnostic_only"] is True
    assert resolved["confidence"] <= 0.45


def test_summary_reports_direction_confidence_and_evidence_quality() -> None:
    candidates = [
        apply_evidence_first_candidate(
            {
                "id": "factor-up",
                "strategy_type": "multi_factor",
                "source": "factor_ic",
                "params": {"factor_name": "quality", "factor_ic": 0.08},
            },
            snapshot={"date": "2026-06-05"},
        ),
        apply_evidence_first_candidate(
            {
                "id": "factor-down",
                "strategy_type": "multi_factor",
                "source": "factor_ic",
                "params": {"factor_name": "crowding", "factor_ic": -0.07},
            },
            snapshot={"date": "2026-06-05"},
        ),
        apply_evidence_first_candidate(
            {"id": "fallback", "strategy_type": "momentum"},
            snapshot={"date": "2026-06-05"},
        ),
    ]

    summary = summarize_generation_quality(candidates)

    assert summary["direction_counts"]["up"] == 1
    assert summary["direction_counts"]["down"] == 1
    assert summary["direction_counts"]["neutral"] == 1
    assert summary["confidence_distribution"]["std"] > 0
    assert summary["factor_backed_candidate_count"] == 2
    assert summary["template_dominance_count"] == 1
    assert "generation_direction_collapse" not in summary["generation_quality_flags"]


def test_trade_prediction_contract_uses_evidence_first_direction_and_confidence() -> None:
    candidate = apply_evidence_first_candidate(
        {
            "id": "contract-down",
            "strategy_id": "contract-down",
            "strategy_type": "multi_factor",
            "source": "factor_ic",
            "target_symbols": ["600000"],
            "params": {
                "factor_name": "crowding_risk",
                "factor_ic": -0.09,
                "prediction_as_of": "2026-06-05",
            },
        },
        snapshot={"date": "2026-06-05"},
    )

    frozen = freeze_trade_prediction_contract(candidate)

    assert frozen["status"] == TRADE_PREDICTION_CONTRACT_READY
    assert frozen["contract"]["direction"] == "down"
    assert frozen["contract"]["confidence"] == candidate["confidence"]
    assert frozen["contract"]["direction_source"] == "market_evidence_vote"
    assert frozen["contract"]["template_fallback_used"] is False


def test_evidence_first_refreshes_old_target_injection_trade_contract() -> None:
    from strategy_factory.application.candidate_contract import apply_resolved_candidate_envelope

    candidate = apply_evidence_first_candidate(
        {
            "id": "contract-refresh",
            "strategy_id": "contract-refresh",
            "strategy_type": "momentum",
            "target_symbols": ["600000"],
            "params": {
                "prediction_as_of": "2026-06-05",
                "trade_prediction_contract": {
                    "contract_version": "strategy_factory.trade_prediction_contract.v1",
                    "strategy_id": "contract-refresh",
                    "stock_code": "600000.SH",
                    "prediction_as_of": "2026-06-05T00:00:00+00:00",
                    "target_trading_date": "2026-06-08",
                    "direction": "neutral",
                    "confidence": 0.45,
                    "horizon": "1d",
                    "evidence_refs": ["full_market_topn:old"],
                    "direction_source": "target_injection_diagnostic_fallback",
                    "confidence_source": "target_injection_diagnostic_fallback",
                    "template_fallback_used": True,
                },
                "trade_prediction_contract_status": "ready",
            },
        },
        snapshot={
            "date": "2026-06-05",
            "factor_research": {
                "ranked_factors": [
                    {"factor_name": "momentum", "ic_value": 0.12, "trend": "rising"}
                ]
            },
        },
    )

    resolved = apply_resolved_candidate_envelope(candidate)

    assert resolved["trade_prediction_contract_status"] == TRADE_PREDICTION_CONTRACT_READY
    assert resolved["trade_prediction_contract"]["direction"] == "up"
    assert resolved["trade_prediction_contract"]["confidence"] == candidate["confidence"]
    assert resolved["trade_prediction_contract"]["direction_source"] == "market_evidence_vote"
    assert resolved["trade_prediction_contract"]["template_fallback_used"] is False


def test_price_volume_source_types_are_non_template_market_evidence() -> None:
    pack = build_market_evidence_pack(
        {
            "id": "price-volume",
            "strategy_type": "momentum",
            "evidence_chain": {
                "evidences": [
                    {"evidence_id": "ev_price", "source_type": "price_action", "direction": "up"},
                    {"evidence_id": "ev_volume", "source_type": "volume", "direction": "up"},
                ]
            },
        },
        snapshot={"date": "2026-06-05"},
    )

    assert pack["evidence_source_counts"] == {"price_volume_confirmation": 2}
    assert pack["non_proxy_evidence_ratio"] == 1.0
    assert pack["template_dominance_score"] == 0.0


def test_wide_intake_proxy_without_prediction_direction_is_neutral(monkeypatch) -> None:
    from strategy_factory.application.semantic_contract import build_signal_evidence_records

    monkeypatch.setenv("STRATEGY_FACTORY_WIDE_INTAKE_OBSERVE_ENABLED", "1")

    records = build_signal_evidence_records(
        {
            "id": "observe-proxy",
            "target_symbols": ["600000"],
            "params": {},
        },
        signal_id="signal-1",
        signal_date="2026-06-05",
    )

    assert len(records) == 1
    assert records[0]["source_type"] == "wide_intake_observe_proxy"
    assert records[0]["proxy_only"] is True
    assert records[0]["direction"] == "neutral"


def test_market_evidence_sanitizes_non_finite_factor_metrics() -> None:
    candidate = {
        "id": "non-finite-factor",
        "strategy_type": "multi_factor",
        "source": "factor_ic",
        "target_symbols": ["600000"],
        "params": {
            "factor_name": "quality_growth",
            "factor_ic": "inf",
            "prediction_as_of": "2026-06-05",
        },
        "evidence_chain": {
            "evidences": [
                {
                    "evidence_id": "ev_bad_factor",
                    "source_type": "factor_ic_validated",
                    "support_metric": {"ic_value": float("nan")},
                    "proxy_only": False,
                }
            ]
        },
    }

    resolved = apply_evidence_first_candidate(candidate, snapshot={"date": "2026-06-05"})

    assert resolved["direction_resolution"]["direction"] == "neutral"
    assert resolved["direction_resolution"]["direction_source"] in {
        "neutral_market_evidence",
        "market_evidence_vote",
        "template_fallback_diagnostic",
    }
    assert resolved["market_evidence_pack"]["factor_backed"] is True
    _assert_all_finite(resolved)


def _default_entry_evidence(pack):
    """Build the backfill default evidence chain and return ev_entry_signal."""
    from strategy_factory.application import semantic_contract as sc

    candidate = {"name": "t", "hypothesis": "h"}
    if pack is not None:
        candidate["market_evidence_pack"] = pack
    chain = sc._build_default_evidence_chain(
        candidate,
        strategy_type="momentum",
        target_symbols=["600000"],
        horizon_days=10,
    )
    return chain["evidences"][0]


def test_backfill_evidence_chain_marks_missing_pack_as_proxy() -> None:
    # 无 market_evidence_pack 的纯模板兜底候选必须诚实标 proxy_only=True,
    # 而非伪装成 price_volume_confirmation 真实证据(历史 >=0.999 阈值 bug)。
    entry = _default_entry_evidence(None)
    assert entry["proxy_only"] is True
    assert entry["source_type"] == "template_fallback"


def test_backfill_evidence_chain_zero_score_without_real_evidence_is_proxy() -> None:
    # 有 pack 但 non_proxy_evidence_ratio=0(无真实非模板证据)仍是 proxy。
    entry = _default_entry_evidence(
        {"template_dominance_score": 0.0, "non_proxy_evidence_ratio": 0.0}
    )
    assert entry["proxy_only"] is True
    assert entry["source_type"] == "template_fallback"


def test_backfill_evidence_chain_high_template_dominance_is_proxy() -> None:
    # 高模板主导(0<score<0.999)旧逻辑错标 proxy_only=False,现必须为 True。
    for score in (0.5, 0.95, 0.998):
        entry = _default_entry_evidence(
            {"template_dominance_score": score, "non_proxy_evidence_ratio": 1.0 - score}
        )
        assert entry["proxy_only"] is True, f"score={score} should be proxy"
        assert entry["source_type"] == "template_fallback"


def test_backfill_evidence_chain_real_evidence_is_not_proxy() -> None:
    # 仅当 pack 存在 + 无模板主导 + 有真实非模板证据时才标 price_volume_confirmation。
    entry = _default_entry_evidence(
        {"template_dominance_score": 0.0, "non_proxy_evidence_ratio": 1.0}
    )
    assert entry["proxy_only"] is False
    assert entry["source_type"] == "price_volume_confirmation"
