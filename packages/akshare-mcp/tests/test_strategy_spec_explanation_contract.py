from __future__ import annotations

from akshare_mcp.services.strategy_spec import StrategySpec


def test_strategy_spec_to_candidate_includes_explanation_contract() -> None:
    spec = StrategySpec(
        strategy_type="momentum",
        params={"lookback": 20, "threshold": 0.02},
        name="momentum explanation smoke",
        description="Momentum strategy for a targeted universe.",
        tags=["external_llm"],
        metadata={
            "generator_type": "external_llm",
            "target_symbols": ["600000"],
            "generation_reason": {
                "provider": "openai",
                "model": "test-model",
                "rationale": "Recent relative strength supports a continuation test.",
            },
            "research_task": {
                "task_id": "task-explain",
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
    )

    candidate = spec.to_candidate(source="external_llm", experiment_id="exp-explain")

    explanation = candidate["strategy_explanation"]
    assert candidate["params"]["strategy_explanation"] == explanation
    assert explanation["version"] == "strategy_explanation.v1"
    assert explanation["summary"] == "Momentum strategy for a targeted universe."
    assert "strategy_explained" in explanation["labels"]
    assert "type:momentum" in explanation["labels"]
    assert "source=external_llm" in explanation["why_generated"]
    assert "provider=openai" in explanation["why_generated"]
    assert "model=test-model" in explanation["why_generated"]
    assert explanation["target_scope"]["symbols"] == ["600000"]
