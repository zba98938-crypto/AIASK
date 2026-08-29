from __future__ import annotations

from typing import Any


AIASK_TOOL_CONTRACT_VERSION = "aiask_tool_contract_v2"

SOURCE_CHAIN_SCHEMA: dict[str, Any] = {"type": "array", "items": {"type": "string"}}
RECORD_ARRAY_SCHEMA: dict[str, Any] = {"type": "array", "items": {"type": "object", "additionalProperties": True}}


def _aiask_envelope_output_schema(data_schema: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "success": {"type": "boolean"},
            "data": data_schema or {},
            "error": {"type": ["string", "null"]},
            "error_code": {"type": "string"},
            "meta": {
                "type": "object",
                "properties": {
                    "trace_id": {"type": "string"},
                    "source_chain": SOURCE_CHAIN_SCHEMA,
                    "side_effect": {"type": "object"},
                    "toolset": {"type": "string"},
                },
                "required": ["trace_id", "source_chain", "side_effect"],
                "additionalProperties": True,
            },
        },
        "required": ["success", "data", "error", "meta"],
        "additionalProperties": True,
    }


AIASK_ENVELOPE_OUTPUT_SCHEMA: dict[str, Any] = _aiask_envelope_output_schema()

QUOTE_DATA_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "code": {"type": "string"},
        "symbol": {"type": "string"},
        "name": {"type": "string"},
        "price": {"type": ["number", "string", "null"]},
        "change": {"type": ["number", "string", "null"]},
        "change_pct": {"type": ["number", "string", "null"]},
        "provider": {"type": "string"},
        "data_timestamp": {"type": "string"},
        "source_chain": SOURCE_CHAIN_SCHEMA,
        "quote": {"type": "object", "additionalProperties": True},
    },
    "additionalProperties": True,
}

NEWS_DIGEST_DATA_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "code": {"type": "string"},
        "query": {"type": "string"},
        "items": RECORD_ARRAY_SCHEMA,
        "news": RECORD_ARRAY_SCHEMA,
        "sources": RECORD_ARRAY_SCHEMA,
        "source_chain": SOURCE_CHAIN_SCHEMA,
    },
    "additionalProperties": True,
}

STOCK_ANALYSIS_DATA_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "workflow": {"type": "string"},
        "code": {"type": "string"},
        "summary": {"type": "object", "additionalProperties": True},
        "steps": RECORD_ARRAY_SCHEMA,
        "artifacts": {"type": "object", "additionalProperties": True},
        "freshness": {"type": "object", "additionalProperties": True},
        "data_quality": {"type": "object", "additionalProperties": True},
        "quality_gate": {"type": "object", "additionalProperties": True},
        "decision_policy": {"type": "object", "additionalProperties": True},
        "limitations": {"type": "array", "items": {"type": "string"}},
        "disclaimer": {"type": "string"},
        "source_chain": SOURCE_CHAIN_SCHEMA,
    },
    "additionalProperties": True,
}

MARKET_TEMPERATURE_DATA_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "status": {"type": "string"},
        "as_of": {"type": "string"},
        "market": {"type": "object", "additionalProperties": True},
        "industries": RECORD_ARRAY_SCHEMA,
        "hot_industries": RECORD_ARRAY_SCHEMA,
        "cold_industries": RECORD_ARRAY_SCHEMA,
        "quality": {"type": "object", "additionalProperties": True},
        "warnings": {"type": "array", "items": {"type": "string"}},
        "source_chain": SOURCE_CHAIN_SCHEMA,
    },
    "additionalProperties": True,
}

PORTFOLIO_RISK_DATA_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "status": {"type": "string"},
        "portfolio_risk": {"type": "object", "additionalProperties": True},
        "risk_metrics": {"type": "object", "additionalProperties": True},
        "stress": {"type": "object", "additionalProperties": True},
        "weights": RECORD_ARRAY_SCHEMA,
        "warnings": {"type": "array", "items": {"type": "string"}},
        "limitations": {"type": "array", "items": {"type": "string"}},
        "source_chain": SOURCE_CHAIN_SCHEMA,
    },
    "additionalProperties": True,
}

QUANT_RESEARCH_DATA_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "status": {"type": "string"},
        "research_id": {"type": "string"},
        "research": {"type": "object", "additionalProperties": True},
        "report": {"type": "object", "additionalProperties": True},
        "stages": RECORD_ARRAY_SCHEMA,
        "artifacts": RECORD_ARRAY_SCHEMA,
        "source_chain": SOURCE_CHAIN_SCHEMA,
    },
    "additionalProperties": True,
}

STRATEGY_REVIEW_DATA_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "status": {"type": "string"},
        "items": RECORD_ARRAY_SCHEMA,
        "reviews": RECORD_ARRAY_SCHEMA,
        "snapshots": RECORD_ARRAY_SCHEMA,
        "count": {"type": "integer"},
        "quality": {"type": "object", "additionalProperties": True},
        "source_chain": SOURCE_CHAIN_SCHEMA,
    },
    "additionalProperties": True,
}

TRADE_PREDICTION_DATA_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "status": {"type": "string"},
        "items": RECORD_ARRAY_SCHEMA,
        "matrix": {"type": "object", "additionalProperties": True},
        "summary": {"type": "object", "additionalProperties": True},
        "score_distribution": {"type": "object", "additionalProperties": True},
        "quality": {"type": "object", "additionalProperties": True},
        "source_chain": SOURCE_CHAIN_SCHEMA,
    },
    "additionalProperties": True,
}

STOCK_RADAR_DATA_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "status": {"type": "string"},
        "candidates": RECORD_ARRAY_SCHEMA,
        "digest": {"type": "object", "additionalProperties": True},
        "degraded": {"type": "boolean"},
        "warnings": {"type": "array", "items": {"type": "string"}},
        "source_chain": SOURCE_CHAIN_SCHEMA,
    },
    "additionalProperties": True,
}

ACTION_INTENT_DATA_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "intent": {"type": "object", "additionalProperties": True},
        "intent_id": {"type": "string"},
        "status": {"type": "string"},
        "side_effect": {"type": "object", "additionalProperties": True},
    },
    "additionalProperties": True,
}

TOOL_CATALOG_DATA_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "tools": RECORD_ARRAY_SCHEMA,
        "count": {"type": "integer"},
        "toolset": {"type": "string"},
    },
    "additionalProperties": True,
}

EVIDENCE_RECORDS_DATA_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "sources": RECORD_ARRAY_SCHEMA,
        "artifacts": RECORD_ARRAY_SCHEMA,
        "items": RECORD_ARRAY_SCHEMA,
        "source_chain": SOURCE_CHAIN_SCHEMA,
    },
    "additionalProperties": True,
}

DOMAIN_OUTPUT_SCHEMA_BY_NAME: dict[str, dict[str, Any]] = {
    "agent_tool_catalog": _aiask_envelope_output_schema(TOOL_CATALOG_DATA_SCHEMA),
    "agent_analyze_stock": _aiask_envelope_output_schema(STOCK_ANALYSIS_DATA_SCHEMA),
    "agent_stock_live_quote": _aiask_envelope_output_schema(QUOTE_DATA_SCHEMA),
    "agent_stock_news_digest": _aiask_envelope_output_schema(NEWS_DIGEST_DATA_SCHEMA),
    "agent_portfolio_risk": _aiask_envelope_output_schema(PORTFOLIO_RISK_DATA_SCHEMA),
    "agent_quant_research_run": _aiask_envelope_output_schema(QUANT_RESEARCH_DATA_SCHEMA),
    "agent_strategy_review_snapshot": _aiask_envelope_output_schema(STRATEGY_REVIEW_DATA_SCHEMA),
    "agent_trade_prediction_status": _aiask_envelope_output_schema(TRADE_PREDICTION_DATA_SCHEMA),
    "agent_trade_prediction_outcomes": _aiask_envelope_output_schema(TRADE_PREDICTION_DATA_SCHEMA),
    "agent_trade_prediction_matrix": _aiask_envelope_output_schema(TRADE_PREDICTION_DATA_SCHEMA),
    "agent_stock_radar_status": _aiask_envelope_output_schema(STOCK_RADAR_DATA_SCHEMA),
    "agent_stock_radar_candidates": _aiask_envelope_output_schema(STOCK_RADAR_DATA_SCHEMA),
    "agent_stock_radar_digest": _aiask_envelope_output_schema(STOCK_RADAR_DATA_SCHEMA),
    "agent_action_intent_create": _aiask_envelope_output_schema(ACTION_INTENT_DATA_SCHEMA),
    "agent_action_intent_get": _aiask_envelope_output_schema(ACTION_INTENT_DATA_SCHEMA),
}

APPROVAL_LEVELS = {
    "browser_state",
    "code_execution",
    "durable_intent",
    "filesystem_write",
    "physical_state_change",
    "platform_admin",
    "process_control",
    "process_execution",
    "stateful",
    "trade_risk",
}
DESTRUCTIVE_LEVELS = {
    "browser_state",
    "filesystem_write",
    "physical_state_change",
    "platform_admin",
    "process_control",
    "stateful",
    "trade_risk",
}
OPEN_WORLD_LEVELS = {
    "browser_state",
    "external_generation",
    "external_message",
    "physical_state_change",
    "platform_admin",
    "trade_risk",
}
OPEN_WORLD_CATEGORIES = {
    "browser",
    "financial_read",
    "homeassistant",
    "image_gen",
    "mcp_admin",
    "mcp_financial",
    "messaging",
    "moa",
    "platform_admin",
    "platform_gateway",
    "stt",
    "tts",
    "vision",
    "web",
    "webhook_admin",
}


def _side_effect_level(side_effect: Any) -> str:
    if isinstance(side_effect, dict):
        return str(side_effect.get("level") or side_effect.get("side_effect") or "read_only").strip() or "read_only"
    return str(side_effect or "read_only").strip() or "read_only"


def _side_effect_requires_confirmation(side_effect: Any) -> bool:
    return isinstance(side_effect, dict) and bool(side_effect.get("confirmation_required"))


def _side_effect_idempotent(side_effect: Any, *, level: str) -> bool:
    if isinstance(side_effect, dict) and "idempotent" in side_effect:
        return bool(side_effect.get("idempotent"))
    return level == "read_only"


def tool_contract_annotations(item: dict[str, Any]) -> dict[str, bool]:
    side_effect = item.get("side_effect")
    level = _side_effect_level(side_effect)
    category = str(item.get("category") or "").strip()
    text = " ".join(
        str(item.get(key) or "").lower()
        for key in ("name", "capability", "category", "side_effect")
    )
    read_only = level == "read_only" and not _side_effect_requires_confirmation(side_effect)
    requires_approval = _side_effect_requires_confirmation(side_effect) or level in APPROVAL_LEVELS
    trade_risk = level == "trade_risk" or any(token in text for token in ("trade_risk", "live_trading", "order", "broker_write"))
    return {
        "readOnlyHint": read_only,
        "destructiveHint": level in DESTRUCTIVE_LEVELS or trade_risk,
        "idempotentHint": _side_effect_idempotent(side_effect, level=level),
        "openWorldHint": level in OPEN_WORLD_LEVELS or category in OPEN_WORLD_CATEGORIES,
        "requiresApproval": requires_approval,
        "tradeRisk": trade_risk,
    }


def domain_output_schema(item: dict[str, Any]) -> dict[str, Any] | None:
    name = str(item.get("name") or "").strip()
    capability = str(item.get("capability") or "").strip()
    if name in DOMAIN_OUTPUT_SCHEMA_BY_NAME:
        return DOMAIN_OUTPUT_SCHEMA_BY_NAME[name]
    if capability.startswith("market_temperature_") or name.startswith("agent_market_temperature_"):
        return _aiask_envelope_output_schema(MARKET_TEMPERATURE_DATA_SCHEMA)
    if capability.startswith("factory_") or capability.startswith("strategy_"):
        return _aiask_envelope_output_schema(STRATEGY_REVIEW_DATA_SCHEMA)
    if capability.startswith("trade_prediction_"):
        return _aiask_envelope_output_schema(TRADE_PREDICTION_DATA_SCHEMA)
    if "artifact" in name or "source" in name or "artifact" in capability or "source" in capability:
        return _aiask_envelope_output_schema(EVIDENCE_RECORDS_DATA_SCHEMA)
    return None


def enrich_tool_contract(item: dict[str, Any]) -> dict[str, Any]:
    payload = dict(item or {})
    annotations = {
        **tool_contract_annotations(payload),
        **dict(payload.get("annotations") or {}),
    }
    output_schema = payload.get("outputSchema") or payload.get("output_schema") or domain_output_schema(payload) or AIASK_ENVELOPE_OUTPUT_SCHEMA
    payload["annotations"] = annotations
    payload.setdefault("output_schema", output_schema)
    payload.setdefault("outputSchema", output_schema)
    payload.setdefault("contract_version", AIASK_TOOL_CONTRACT_VERSION)
    payload.setdefault("contract_source", "aiask_agent.tools.contracts")
    return payload


def enrich_tool_contracts(items: tuple[dict[str, Any], ...] | list[dict[str, Any]]) -> tuple[dict[str, Any], ...]:
    return tuple(enrich_tool_contract(dict(item)) for item in items)
