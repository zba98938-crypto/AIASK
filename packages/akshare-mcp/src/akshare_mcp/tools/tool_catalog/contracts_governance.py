"""governance-category tool contracts (split from tool_catalog)."""

from __future__ import annotations

from typing import Any

from ._helpers import STANDARD_ENVELOPE_OUTPUT_SCHEMA, _contract

CONTRACTS: dict[str, dict[str, Any]] = {
    "governance_check_workflow": _contract(
        name="governance_check_workflow",
        title="Governance Check Workflow",
        category="governance",
        description="AI-facing workflow for monitoring governance across factor decay, crowding, model drift, strategy health and online/offline consistency.",
        required_params=[],
        input_schema={
            "type": "object",
            "properties": {
                "target_type": {"type": "string", "enum": ["factor", "model", "strategy", "system"], "description": "Type of target to check"},
                "target_id": {"type": "string", "description": "Identifier of the specific target (factor name, model name, strategy ID)"},
                "ic_history": {"type": "array", "items": {"type": "number"}, "description": "Chronological IC values for factor decay check"},
                "factor_expression": {"type": "string", "description": "Factor expression for crowding analysis"},
                "factor_category": {"type": "string", "description": "Factor category (momentum, value, quality, etc.)"},
                "existing_factor_pool": {"type": "array", "items": {"type": "string"}, "description": "Names of existing factors in the pool"},
                "current_metrics": {"type": "object", "description": "Current model metrics for drift detection"},
                "baseline_metrics": {"type": "object", "description": "Baseline model metrics for drift comparison"},
                "posture_level": {"type": "string", "description": "Strategy posture level (safe, guarded, critical)"},
                "control_mode": {"type": "string", "description": "Strategy control mode (active, halted, manual_stop)"},
                "open_alert_count": {"type": "integer", "description": "Number of open runtime alerts"},
                "include_factor_decay": {"type": "boolean", "default": True},
                "include_crowding": {"type": "boolean", "default": True},
                "include_model_drift": {"type": "boolean", "default": True},
                "include_strategy_health": {"type": "boolean", "default": True},
                "include_consistency": {"type": "boolean", "default": True},
                "as_of": {"type": "string", "description": "PIT cutoff date (ISO string). Omit for current time."},
            },
            "additionalProperties": True,
        },
        side_effect_level="read_only",
        freshness="real_time_governance_snapshot",
        examples=[
            {
                "description": "Check factor governance for a specific factor",
                "arguments": {"target_type": "factor", "target_id": "momentum_20d", "ic_history": [0.05, 0.04, 0.03, 0.02]},
            },
            {
                "description": "Run system-wide governance check",
                "arguments": {"target_type": "system"},
            },
        ],
        tags=["workflow", "governance", "monitoring", "ai-friendly"],
    ),
}
