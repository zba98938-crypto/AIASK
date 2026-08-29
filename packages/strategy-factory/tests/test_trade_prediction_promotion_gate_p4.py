from __future__ import annotations

import asyncio

from strategy_factory.application.trade_prediction_promotion_gate import (
    TRADE_PREDICTION_PROMOTION_SCORE_VERSION,
    evaluate_trade_prediction_promotion_gate,
)


def _outcome(index: int, *, score: float = 0.8, status: str = "ok", data_quality: str = "ok") -> dict:
    return {
        "prediction_id": f"tp_{index}",
        "strategy_id": "strategy_1",
        "stock_code": "600000",
        "score_version": TRADE_PREDICTION_PROMOTION_SCORE_VERSION,
        "score_status": status,
        "data_quality_status": data_quality,
        "trade_prediction_score": score,
    }


def test_trade_prediction_gate_default_is_diagnostic_only_even_when_v2_missing() -> None:
    result = asyncio.run(
        evaluate_trade_prediction_promotion_gate(
            outcomes=[],
            enabled=False,
            min_sample_n=3,
        )
    )

    assert result["enabled"] is False
    assert result["diagnostic_only"] is True
    assert result["passed"] is True
    assert result["hard_block"] is False
    assert result["diagnostic_passed"] is False
    assert "promotion_gate_disabled_diagnostic_only" in result["reasons"]
    assert "trade_prediction_score_v2_missing" in result["reasons"]


def test_trade_prediction_gate_enabled_blocks_insufficient_samples() -> None:
    result = asyncio.run(
        evaluate_trade_prediction_promotion_gate(
            outcomes=[_outcome(1)],
            enabled=True,
            min_sample_n=3,
            min_score_lcb_95=0.1,
        )
    )

    assert result["passed"] is False
    assert result["hard_block"] is True
    assert "trade_prediction_insufficient_samples" in result["reasons"]


def test_trade_prediction_gate_enabled_blocks_partial_or_data_gap() -> None:
    outcomes = [_outcome(index) for index in range(30)]
    outcomes.append(_outcome(31, status="partial_intraday_missing", data_quality="intraday_missing"))

    result = asyncio.run(
        evaluate_trade_prediction_promotion_gate(
            outcomes=outcomes,
            enabled=True,
            min_sample_n=30,
            min_score_lcb_95=0.55,
        )
    )

    assert result["passed"] is False
    assert result["hard_block"] is True
    assert "trade_prediction_partial_outcomes" in result["reasons"]
    assert "trade_prediction_data_quality_gap" in result["reasons"]


def test_trade_prediction_gate_enabled_passes_with_v2_lcb_and_sample_threshold() -> None:
    outcomes = [_outcome(index, score=0.8) for index in range(30)]

    result = asyncio.run(
        evaluate_trade_prediction_promotion_gate(
            outcomes=outcomes,
            enabled=True,
            min_sample_n=30,
            min_score_lcb_95=0.55,
        )
    )

    assert result["passed"] is True
    assert result["diagnostic_passed"] is True
    assert result["hard_block"] is False
    assert result["aggregate"]["ok_sample_n"] == 30
    assert result["aggregate"]["score_lcb_95"] >= 0.55
