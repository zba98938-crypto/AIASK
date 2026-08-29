"""search-category tool contracts (split from tool_catalog)."""

from __future__ import annotations

from typing import Any

from ._helpers import STANDARD_ENVELOPE_OUTPUT_SCHEMA, _contract

CONTRACTS: dict[str, dict[str, Any]] = {
    "semantic_stock_search": _contract(
        name="semantic_stock_search",
        title="Semantic Stock Search",
        category="search",
        description="Resolve stock ideas from natural-language cues such as sector, style, theme, code or stock name.",
        required_params=["query"],
        input_schema={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Natural-language stock query in Chinese or ticker/name form."},
                "limit": {"type": "integer", "minimum": 1, "maximum": 100},
            },
            "required": ["query"],
            "additionalProperties": False,
        },
        side_effect_level="read_only",
        freshness="vector_index_and_market_taxonomy_snapshot",
        examples=[
            {"description": "Search high-dividend bank stocks", "arguments": {"query": "高股息银行股", "limit": 20}},
            {"description": "Search by stock code or short name", "arguments": {"query": "600519", "limit": 10}},
        ],
        tags=["search", "semantic", "screening", "vector"],
    ),
    "search_similar_stocks": _contract(
        name="search_similar_stocks",
        title="Search Similar Stocks",
        category="search",
        description="Find similar stocks for a given target code using profile, fundamental and technical similarity signals.",
        required_params=["code"],
        input_schema={
            "type": "object",
            "properties": {
                "code": {"type": "string", "description": "Target stock code."},
                "top_n": {"type": "integer", "minimum": 1, "maximum": 50},
                "similarity_type": {
                    "type": "string",
                    "enum": ["both", "profile", "fundamental", "technical"],
                },
                "search_backend": {"type": "string", "enum": ["db", "memory"]},
                "allow_fallback": {"type": "boolean"},
            },
            "required": ["code"],
            "additionalProperties": True,
        },
        side_effect_level="read_only",
        freshness="vector_index_and_recent_stock_profile_snapshot",
        examples=[
            {
                "description": "Find stocks similar to Kweichow Moutai",
                "arguments": {"code": "600519", "top_n": 10, "similarity_type": "both"},
            }
        ],
        tags=["search", "similarity", "vector", "research"],
    ),
    "available_tools": _contract(
        name="available_tools",
        title="Available Tools",
        category="search",
        description="List available tools with AI-facing contracts, side-effect level and examples when known.",
        required_params=[],
        input_schema={
            "type": "object",
            "properties": {
                "category": {"type": "string"},
                "include_contracts": {"type": "boolean"},
            },
            "additionalProperties": True,
        },
        side_effect_level="read_only",
        freshness="runtime_surface",
        examples=[{"description": "List all AI-callable tools", "arguments": {"include_contracts": True}}],
        tags=["catalog", "discovery"],
    ),
    "get_tool_contract": _contract(
        name="get_tool_contract",
        title="Get Tool Contract",
        category="search",
        description="Fetch a single AI-facing tool contract including required parameters, examples and output schema.",
        required_params=["tool_name"],
        input_schema={
            "type": "object",
            "properties": {"tool_name": {"type": "string"}},
            "required": ["tool_name"],
            "additionalProperties": False,
        },
        side_effect_level="read_only",
        freshness="runtime_surface",
        examples=[{"description": "Inspect one workflow tool", "arguments": {"tool_name": "analyze_stock_workflow"}}],
        tags=["catalog", "contract"],
    ),
}
