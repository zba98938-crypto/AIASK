"""risk-category tool contracts (split from tool_catalog)."""

from __future__ import annotations

from typing import Any

from ._helpers import STANDARD_ENVELOPE_OUTPUT_SCHEMA, _contract

CONTRACTS: dict[str, dict[str, Any]] = {
    "risk_manager": _contract(
        name="risk_manager",
        title="Risk Manager",
        category="risk",
        description="Portfolio risk analysis: VaR, stress-test, and exposure breakdown. Pass action + params/kwargs.",
        required_params=["action"],
        input_schema={
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["help", "calculate_var", "stress_test", "risk_exposure", "list"]},
                "params": {"type": "object"},
                "kwargs": {"type": ["object", "string", "null"]},
                "codes": {"type": "array", "items": {"type": "string"}},
                "weights": {"type": "array", "items": {"type": "number"}},
                "scenario": {"type": "string"},
                "confidence": {"type": "number", "minimum": 0.8, "maximum": 0.9999},
                "method": {"type": "string", "enum": ["historical", "parametric", "monte_carlo"]},
                "lookback_days": {"type": "integer"},
                "portfolio_value": {"type": "number"},
            },
            "required": ["action"],
            "additionalProperties": True,
        },
        side_effect_level="read_only",
        freshness="depends_on_kline_and_portfolio_data",
        examples=[
            {"description": "Calculate 95% VaR for a 2-stock portfolio", "arguments": {"action": "calculate_var", "codes": ["600519", "000001"], "weights": [0.6, 0.4], "confidence": 0.95}},
            {"description": "Run stress test with market_crash scenario", "arguments": {"action": "stress_test", "codes": ["600519"], "scenario": "market_crash"}},
        ],
        tags=["risk", "var", "stress-test", "exposure"],
    ),
}
