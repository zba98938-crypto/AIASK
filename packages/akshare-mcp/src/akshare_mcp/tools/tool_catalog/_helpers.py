"""AI-facing MCP tool contract catalog."""

from __future__ import annotations

from typing import Any

STANDARD_ENVELOPE_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "success": {"type": "boolean"},
        "data": {"type": ["object", "array", "string", "number", "boolean", "null"]},
        "error": {"type": ["string", "null"]},
        "error_code": {"type": ["string", "null"]},
        "source": {"type": "string"},
        "cached": {"type": "boolean"},
        "timestamp": {"type": "string"},
        "meta": {
            "type": "object",
            "properties": {
                "trace_id": {"type": "string"},
                "tool_version": {"type": "string"},
                "data_timestamp": {"type": ["string", "null"]},
                "source_chain": {"type": "array", "items": {"type": "string"}},
                "cached": {"type": "boolean"},
                "latency_ms": {"type": "number"},
                "quality": {"type": "object"},
                "provider_contract": {"type": "object"},
                "contract_meta": {"type": "object"},
                "quality_gate": {"type": "object"},
                "reconciliation": {"type": "object"},
                "provider_status": {"type": "object"},
                "side_effect": {"type": "object"},
                "lineage": {"type": "object"},
                "pit": {
                    "type": "object",
                    "description": "Point-In-Time compliance metadata: as_of, pit_passed",
                    "properties": {
                        "as_of": {"type": "string"},
                        "pit_passed": {"type": "boolean"},
                    },
                },
                "idempotency_key": {"type": ["string", "null"]},
                "degraded": {"type": "boolean"},
            },
            "additionalProperties": True,
        },
    },
    "required": ["success", "data", "error", "source", "cached", "timestamp"],
    "additionalProperties": True,
}


def _contract(
    *,
    name: str,
    title: str,
    category: str,
    description: str,
    required_params: list[str],
    input_schema: dict[str, Any],
    side_effect_level: str,
    freshness: str,
    examples: list[dict[str, Any]],
    tags: list[str],
    output_schema: dict[str, Any] | None = None,
    source_policy: dict[str, Any] | None = None,
    contract_source: str = "akshare_mcp.tool_catalog",
) -> dict[str, Any]:
    payload = {
        "name": name,
        "title": title,
        "category": category,
        "description": description,
        "required_params": required_params,
        "input_schema": input_schema,
        "output_schema": output_schema or STANDARD_ENVELOPE_OUTPUT_SCHEMA,
        "side_effect": {
            "level": side_effect_level,
            "confirmation_required": side_effect_level == "trade_risk",
        },
        "freshness": {"expectation": freshness},
        "examples": examples,
        "tags": tags,
        "contract_version": "ai_tool_contract_v1",
        "contract_source": contract_source,
    }
    if source_policy is not None:
        payload["source_policy"] = source_policy
    return payload
