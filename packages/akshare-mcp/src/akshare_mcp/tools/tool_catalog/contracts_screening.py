"""screening-category tool contracts (split from tool_catalog)."""

from __future__ import annotations

from typing import Any

from ._helpers import STANDARD_ENVELOPE_OUTPUT_SCHEMA, _contract

CONTRACTS: dict[str, dict[str, Any]] = {
    "screener_manager": _contract(
        name="screener_manager",
        title="Screener Manager",
        category="screening",
        description="Manager for fundamental, technical and combined stock screening. Supports action + params/kwargs payloads.",
        required_params=["action"],
        input_schema={
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": [
                        "help",
                        "screen",
                        "list",
                        "list_strategies",
                        "save_strategy",
                        "run_strategy",
                        "technical_screen",
                        "list_conditions",
                        "combined_screen",
                    ],
                },
                "params": {"type": "object"},
                "kwargs": {"type": ["object", "string", "null"]},
                "criteria": {"type": "object", "description": "Fundamental criteria dict for screen action."},
                "fundamental_criteria": {"type": "object", "description": "Fundamental criteria dict for combined_screen."},
                "fundamental_conditions": {
                    "type": "array",
                    "description": "Optional parsed fundamental condition list from parse_selection_query.",
                    "items": {"type": "object"},
                },
                "conditions": {
                    "type": "array",
                    "description": "Technical condition ids or condition dicts with params.",
                    "items": {"type": ["string", "object"]},
                },
                "technical_conditions": {
                    "type": "array",
                    "items": {"type": ["string", "object"]},
                },
                "tech_conditions": {
                    "type": "array",
                    "items": {"type": ["string", "object"]},
                },
                "logic": {"type": "string", "enum": ["AND", "OR"]},
                "stock_pool": {"type": ["array", "string"]},
                "limit": {"type": "integer", "minimum": 1, "maximum": 500},
            },
            "required": ["action"],
            "additionalProperties": True,
        },
        side_effect_level="stateful",
        freshness="depends_on_screen_action_and_market_snapshot",
        examples=[
            {
                "description": "Run a combined screen using parsed semantic conditions",
                "arguments": {
                    "action": "combined_screen",
                    "fundamental_conditions": [{"field": "pe_ratio", "operator": "<", "value": 20}],
                    "technical_conditions": [{"id": "price_above_ma", "params": {"n": 20}}],
                    "logic": "AND",
                    "limit": 20,
                },
            }
        ],
        tags=["manager", "screening", "fundamental", "technical"],
    ),
}
