from __future__ import annotations

from akshare_mcp.services.strategy_lifecycle_shared.presentation import build_strategy_presentation
from akshare_mcp.tools.managers.strategy_mgr_crud._support import _build_strategy_market_summary


def test_strategy_presentation_surfaces_explanation_contract() -> None:
    explanation = {
        "version": "strategy_explanation.v1",
        "summary": "Momentum continuation after sector strength.",
        "labels": ["strategy_explained", "type:momentum"],
        "why_generated": "source=external_llm; rationale=relative strength",
    }

    presentation = build_strategy_presentation(
        {
            "id": "s1",
            "name": "explained momentum",
            "strategy_type": "momentum",
            "status": "submitted",
            "params": {"strategy_explanation": explanation},
        }
    )

    assert presentation["strategy_explanation"] == explanation
    assert presentation["strategy_summary"] == "Momentum continuation after sector strength."
    assert presentation["strategy_labels"] == ["strategy_explained", "type:momentum"]
    assert presentation["why_generated"] == "source=external_llm; rationale=relative strength"


def test_strategy_market_summary_includes_explanation_summary() -> None:
    summary = _build_strategy_market_summary(
        {
            "id": "s1",
            "name": "explained momentum",
            "description": "Momentum continuation after sector strength.",
            "strategy_type": "momentum",
            "status": "submitted",
            "target_symbols": ["600000"],
            "params": {
                "generation_reason": {
                    "source": "external_llm",
                    "rationale": "Relative strength and liquidity confirmation.",
                },
                "research_task": {"candidate_family": "momentum"},
            },
        },
        metrics={"sharpe_ratio": 1.1},
    )

    assert summary["strategy_explanation_summary"] == "Momentum continuation after sector strength."
    assert "strategy_explained" in summary["strategy_explanation_labels"]
    assert "type:momentum" in summary["strategy_explanation_labels"]
