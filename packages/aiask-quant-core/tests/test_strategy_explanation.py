from __future__ import annotations

from aiask_quant_core.strategy_explanation import (
    EXPLANATION_VERSION,
    build_strategy_explanation,
    render_strategy_description,
)


def test_build_strategy_explanation_summarizes_generated_strategy() -> None:
    candidate = {
        "name": "event breakout candidate",
        "description": "Breakout after theme event confirmation",
        "strategy_type": "event_structure_breakout",
        "target_symbols": ["600000", "000001"],
        "tags": ["pipeline_staged"],
        "generation_reason": {
            "source": "external_llm",
            "provider": "openai",
            "model": "test-model",
            "category": "event",
            "rationale": "Theme strength and volume contraction support a breakout setup.",
        },
        "research_task": {
            "task_id": "task-1",
            "theme": "AI infrastructure",
            "opportunity_type": "sector_breakout",
            "candidate_family": "event_breakout",
        },
        "trade_plan": {
            "entry_bias": "breakout_with_volume_confirmation",
            "exit_bias": "false_breakout_or_time_stop",
        },
        "risk_rules": {"stop_loss_pct": 0.08, "max_holding_days": 12},
        "holding_horizon": {"min_days": 3, "max_days": 12},
    }

    explanation = build_strategy_explanation(
        candidate,
        metrics={"sharpe_ratio": 1.23, "total_return": 0.18, "max_drawdown": -0.06},
        source="unit_test",
    )

    assert explanation["version"] == EXPLANATION_VERSION
    assert explanation["summary"] == "Breakout after theme event confirmation"
    assert "strategy_explained" in explanation["labels"]
    assert "type:event_structure_breakout" in explanation["labels"]
    assert "family:event_breakout" in explanation["labels"]
    assert "source=external_llm" in explanation["why_generated"]
    assert "provider=openai" in explanation["why_generated"]
    assert explanation["target_scope"]["symbols"] == ["600000", "000001"]
    assert explanation["signal_logic"]["entry"] == "breakout_with_volume_confirmation"
    assert explanation["risk_notes"]["risk_rules"]["stop_loss_pct"] == 0.08
    assert explanation["evidence"]["metrics"]["sharpe_ratio"] == 1.23

    rendered = render_strategy_description("event breakout candidate", explanation)
    assert "Why:" in rendered
    assert "Targets: 600000, 000001" in rendered
    assert "Entry: breakout_with_volume_confirmation" in rendered
