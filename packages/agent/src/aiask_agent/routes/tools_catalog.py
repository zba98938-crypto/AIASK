"""Helpers for the Agent tool catalog HTTP response."""

from __future__ import annotations

from typing import Any

from ..runtime import AgentRuntime

TOOL_CONTRACT_CATALOG_FIELDS = (
    "input_schema",
    "output_schema",
    "outputSchema",
    "annotations",
    "freshness",
    "examples",
    "contract_version",
    "contract_source",
    "source_policy",
    "standard_model",
    "provider_choices",
    "provider_status",
    "quality_gate",
    "reconciliation",
    "form_schema",
)

API_SAFE_CATEGORIES = {"financial_read", "financial_stateful", "mcp_financial"}
APPROVAL_SIDE_EFFECTS = {
    "filesystem_write",
    "process_execution",
    "process_control",
    "browser_state",
    "code_execution",
    "stateful",
    "trade_risk",
}


def _visibility(item: dict[str, Any]) -> str:
    return "api_safe" if str(item.get("category") or "").strip() in API_SAFE_CATEGORIES else "full_mode_only"


def _interaction_mode(item: dict[str, Any]) -> str:
    if item.get("blocked_reason") or str(item.get("status") or "").strip().lower() == "blocked":
        return "blocked"
    side_effect = item.get("side_effect")
    if side_effect == "read_only":
        return "read_only"
    if side_effect == "durable_intent":
        return "intent"
    if isinstance(side_effect, dict):
        if side_effect.get("level") == "read_only" and not side_effect.get("confirmation_required"):
            return "read_only"
        return "approval" if side_effect.get("confirmation_required") else "read_only"
    if isinstance(side_effect, str) and side_effect in APPROVAL_SIDE_EFFECTS:
        return "approval"
    return "read_only" if _visibility(item) == "api_safe" else "approval"


def tool_catalog_item_payload(item: dict[str, Any], parameters: dict[str, Any]) -> dict[str, Any]:
    payload = dict(item)
    payload["parameters"] = parameters
    payload["visibility"] = _visibility(item)
    payload["interaction_mode"] = _interaction_mode(item)
    payload["confirmation_required"] = payload["interaction_mode"] in {"intent", "approval"}
    payload["blocked_reason"] = item.get("blocked_reason")
    for field in TOOL_CONTRACT_CATALOG_FIELDS:
        if field in item and item.get(field) is not None:
            payload[field] = item.get(field)
    return payload


def build_tool_catalog_payload(selected: AgentRuntime, *, implementation: str | None = None) -> dict[str, Any]:
    schemas = {
        item["function"]["name"]: item["function"]
        for item in selected.tool_registry.openai_tools()
        if item.get("type") == "function" and isinstance(item.get("function"), dict)
    }
    payload = {
        "object": "list",
        "data": [
            tool_catalog_item_payload(
                dict(item),
                schemas.get(str(item.get("name")), {}).get("parameters", {}),
            )
            for item in selected.tool_registry.catalog
        ],
    }
    if implementation:
        payload["implementation"] = implementation
    return payload
