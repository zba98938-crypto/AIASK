"""execution-category tool contracts (split from tool_catalog)."""

from __future__ import annotations

from typing import Any

from ._helpers import STANDARD_ENVELOPE_OUTPUT_SCHEMA, _contract

CONTRACTS: dict[str, dict[str, Any]] = {
    "execution_manager": _contract(
        name="execution_manager",
        title="Execution Manager",
        category="execution",
        description=(
            "TWAP/VWAP execution planning with soft-gate risk warnings, compliance gate, "
            "and cost model transparency. Use dry_run=true to validate a plan without persisting it."
        ),
        required_params=["action"],
        input_schema={
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["help", "twap", "vwap", "list", "summary", "get_config", "set_config", "update"]},
                "params": {"type": "object"},
                "kwargs": {"type": ["object", "string", "null"]},
                "dry_run": {"type": "boolean", "description": "Preview plan without persisting task. Returns status=dry_run_preview."},
            },
            "required": ["action"],
            "additionalProperties": True,
        },
        side_effect_level="stateful",
        freshness="real_time_for_planning_static_for_config",
        examples=[
            {"description": "Dry-run TWAP plan to inspect cost and warnings before submitting", "arguments": {"action": "twap", "dry_run": True, "params": {"code": "600519", "total_quantity": 1000, "duration_minutes": 60}}},
            {"description": "Get current soft-gate config", "arguments": {"action": "get_config"}},
        ],
        tags=["execution", "twap", "vwap", "dry-run", "cost-model"],
    ),
}
