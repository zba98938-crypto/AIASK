from __future__ import annotations

import math

from akshare_mcp.services.strategy_hypothesis_generator import LLMHypothesisGenerator


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


def test_hypothesis_generator_rejects_non_finite_numeric_semantics() -> None:
    result = LLMHypothesisGenerator.build(
        {
            "hypothesis": "Momentum persistence should survive a narrow validation window.",
            "failure_mode": "false_breakout",
            "strategy_type": "momentum",
            "holding_horizon": {
                "rationale": "short trend follow-through",
                "alpha_half_life": "inf",
                "max_days": float("nan"),
            },
            "execution_assumptions": {
                "commission_rate": "inf",
                "slippage_bps": float("nan"),
                "tradability_filter": True,
                "slippage_model": "fixed",
            },
            "portfolio_spec": {
                "position_assumption": "single_name",
                "max_position_pct": "nan",
                "target_weight_scheme": "single_name",
            },
            "stock_pool": {"symbols": ["600519"]},
            "validation_profile": {"validation_focus": "candidate_target_only"},
        },
        research_task={"task_source": "snapshot", "target_symbols": ["600519"]},
    )

    artifact = result.to_artifact()

    assert artifact["alpha_half_life"] is None
    assert artifact["cost_sensitivity_grid"] == {}
    assert "hypothesis_missing:alpha_half_life" in artifact["reject_reasons"]
    assert "hypothesis_missing:cost_sensitivity_grid" in artifact["reject_reasons"]
    _assert_all_finite(artifact)
