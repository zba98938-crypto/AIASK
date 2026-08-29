"""market-category tool contracts (split from tool_catalog)."""

from __future__ import annotations

from typing import Any

from ._helpers import STANDARD_ENVELOPE_OUTPUT_SCHEMA, _contract

CONTRACTS: dict[str, dict[str, Any]] = {
    "get_market_temperature_snapshot": _contract(
        name="get_market_temperature_snapshot",
        title="Market Temperature Snapshot",
        category="market",
        description=(
            "Build a read-only A-share market thermometer from local SQLite stock universe "
            "and daily K-line data. Returns market temperature, MA20 breadth, hot/cold "
            "industry rankings, and explicit data-quality metadata."
        ),
        required_params=[],
        input_schema={
            "type": "object",
            "properties": {
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 1000,
                    "description": "Maximum stock-universe sample size loaded from SQLite.",
                },
                "top_n": {
                    "type": "integer",
                    "minimum": 0,
                    "maximum": 50,
                    "description": "Number of hot/cold industry rows to return.",
                },
                "as_of": {
                    "type": ["string", "null"],
                    "description": "Optional point-in-time cutoff date, YYYY-MM-DD.",
                },
                "min_bars": {
                    "type": "integer",
                    "minimum": 2,
                    "maximum": 120,
                    "description": "Minimum daily bars requested per stock for MA20 calculation.",
                },
                "use_cache": {
                    "type": "boolean",
                    "default": True,
                    "description": "When true, serve the latest matching local snapshot cache before recomputing.",
                },
            },
            "additionalProperties": False,
        },
        side_effect_level="read_only",
        freshness="local_sqlite_daily_kline_snapshot",
        examples=[
            {
                "description": "Build a default market thermometer snapshot",
                "arguments": {"limit": 300, "top_n": 8},
            },
            {
                "description": "Build a point-in-time snapshot for a specific trade date",
                "arguments": {"as_of": "2026-06-08", "limit": 500, "top_n": 10},
            },
        ],
        tags=["market", "breadth", "industry-rotation", "temperature", "read-only"],
    ),
    "refresh_market_temperature_snapshot_cache": _contract(
        name="refresh_market_temperature_snapshot_cache",
        title="Refresh Market Temperature Snapshot Cache",
        category="market",
        description=(
            "Compute the A-share market thermometer from local SQLite stock and K-line data, "
            "then persist the snapshot into the durable local cache for fast read-only access."
        ),
        required_params=[],
        input_schema={
            "type": "object",
            "properties": {
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 1000,
                    "description": "Maximum stock-universe sample size loaded from SQLite.",
                },
                "top_n": {
                    "type": "integer",
                    "minimum": 0,
                    "maximum": 50,
                    "description": "Number of hot/cold industry rows stored in the returned preview.",
                },
                "as_of": {
                    "type": ["string", "null"],
                    "description": "Optional point-in-time cutoff date, YYYY-MM-DD.",
                },
                "min_bars": {
                    "type": "integer",
                    "minimum": 2,
                    "maximum": 120,
                    "description": "Minimum daily bars requested per stock for MA20 calculation.",
                },
            },
            "additionalProperties": False,
        },
        side_effect_level="stateful",
        freshness="local_sqlite_daily_snapshot_cache_refresh",
        examples=[
            {
                "description": "Refresh the full local market-temperature cache",
                "arguments": {"limit": 1000, "top_n": 20},
            },
            {
                "description": "Refresh a point-in-time cache entry",
                "arguments": {"as_of": "2026-06-08", "limit": 1000, "top_n": 20},
            },
        ],
        tags=["market", "breadth", "industry-rotation", "temperature", "cache", "stateful"],
    ),
    "list_market_temperature_snapshot_cache": _contract(
        name="list_market_temperature_snapshot_cache",
        title="List Market Temperature Snapshot Cache",
        category="market",
        description=(
            "List compact metadata for persisted market thermometer cache entries, "
            "including trade date, temperature, state, sample counts, quality status, and warnings."
        ),
        required_params=[],
        input_schema={
            "type": "object",
            "properties": {
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 365,
                    "description": "Maximum number of cached trade-date snapshots to list.",
                },
                "include_snapshot": {
                    "type": "boolean",
                    "default": False,
                    "description": "When true, include each raw snapshot JSON; keep false for compact UI/history queries.",
                },
            },
            "additionalProperties": False,
        },
        side_effect_level="read_only",
        freshness="local_sqlite_market_temperature_cache_history",
        examples=[
            {
                "description": "List recent market thermometer cache entries",
                "arguments": {"limit": 30},
            }
        ],
        tags=["market", "breadth", "industry-rotation", "temperature", "cache", "history", "read-only"],
    ),
    "list_market_temperature_industry_history": _contract(
        name="list_market_temperature_industry_history",
        title="List Market Temperature Industry History",
        category="market",
        description=(
            "Read point-in-time industry temperature history from persisted market thermometer "
            "cache snapshots. Pass an industry name/code for one time series, or omit it to "
            "return the top industries per cached trade date."
        ),
        required_params=[],
        input_schema={
            "type": "object",
            "properties": {
                "industry": {
                    "type": ["string", "null"],
                    "description": "Optional industry name or code to filter, for example bank or 801780.",
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 365,
                    "description": "Maximum cached trade-date snapshots to inspect.",
                },
                "top_n": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 50,
                    "description": "When industry is omitted, return at most this many industries per date.",
                },
                "match_mode": {
                    "type": "string",
                    "enum": ["exact", "contains"],
                    "default": "exact",
                    "description": "Industry match mode for the name/code filter.",
                },
                "include_source_chain": {
                    "type": "boolean",
                    "default": False,
                    "description": "Include snapshot source_chain on each row for audits.",
                },
            },
            "additionalProperties": False,
        },
        side_effect_level="read_only",
        freshness="local_sqlite_market_temperature_industry_history",
        examples=[
            {
                "description": "Read one industry's cached temperature time series",
                "arguments": {"industry": "bank", "limit": 60},
            },
            {
                "description": "Read top industry rotation rows for recent cached dates",
                "arguments": {"limit": 20, "top_n": 8},
            },
        ],
        tags=["market", "breadth", "industry-rotation", "temperature", "cache", "history", "read-only"],
    ),
    "list_market_temperature_industry_constituents": _contract(
        name="list_market_temperature_industry_constituents",
        title="List Market Temperature Industry Constituents",
        category="market",
        description=(
            "Read local stock-universe constituents for one market-temperature industry. "
            "This is the drill-down companion to industry heat/history rows and stays "
            "strictly read-only against the local SQLite stocks table."
        ),
        required_params=["industry"],
        input_schema={
            "type": "object",
            "properties": {
                "industry": {
                    "type": "string",
                    "minLength": 1,
                    "description": "Industry name to match against stock universe industry/sector fields.",
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 1000,
                    "description": "Maximum constituent rows to return.",
                },
                "offset": {
                    "type": "integer",
                    "minimum": 0,
                    "maximum": 10000,
                    "description": "Offset within the matched constituent list.",
                },
                "match_mode": {
                    "type": "string",
                    "enum": ["exact", "contains"],
                    "default": "contains",
                    "description": "Industry match mode for stock universe industry/sector fields.",
                },
                "include_source_chain": {
                    "type": "boolean",
                    "default": False,
                    "description": "Include source_chain on each constituent row for audits.",
                },
            },
            "required": ["industry"],
            "additionalProperties": False,
        },
        side_effect_level="read_only",
        freshness="local_sqlite_stock_universe",
        examples=[
            {
                "description": "List the largest local constituents for one industry",
                "arguments": {"industry": "bank", "limit": 50, "match_mode": "contains"},
            }
        ],
        tags=["market", "industry-rotation", "temperature", "constituents", "stocks", "read-only"],
    ),
    "get_market_temperature_forward_validation": _contract(
        name="get_market_temperature_forward_validation",
        title="Get Market Temperature Forward Validation",
        category="market",
        description=(
            "Build a read-only point-in-time validation matrix from persisted market thermometer "
            "snapshots. Buckets by market temperature state and summarizes future cached market "
            "breadth returns over configurable horizons."
        ),
        required_params=[],
        input_schema={
            "type": "object",
            "properties": {
                "limit": {
                    "type": "integer",
                    "minimum": 2,
                    "maximum": 365,
                    "description": "Maximum cached snapshots to use for validation.",
                },
                "horizons": {
                    "type": ["array", "string", "integer", "null"],
                    "description": "Forward horizons in cached trade-date steps, for example [1, 3, 5] or '1,3,5'.",
                },
                "target_field": {
                    "type": "string",
                    "enum": ["weighted_pct_change", "avg_pct_change", "temperature_delta", "benchmark_return"],
                    "default": "weighted_pct_change",
                    "description": "Forward target. benchmark_return uses local benchmark/index K-line closes when available.",
                },
                "benchmark_code": {
                    "type": ["string", "null"],
                    "default": "000300",
                    "description": "Optional benchmark/index code used when target_field is benchmark_return.",
                },
                "min_samples": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 100,
                    "description": "Minimum samples for a matrix cell to be marked reliable.",
                },
                "neutral_band_pct": {
                    "type": "number",
                    "minimum": 0.0,
                    "maximum": 5.0,
                    "description": "Neutral-state hit threshold in percentage points.",
                },
                "include_samples": {
                    "type": "boolean",
                    "default": False,
                    "description": "Include compact per-date validation samples for audit/debug views.",
                },
            },
            "additionalProperties": False,
        },
        side_effect_level="read_only",
        freshness="local_sqlite_market_temperature_forward_validation",
        examples=[
            {
                "description": "Build the default market-temperature validation matrix",
                "arguments": {"limit": 180, "horizons": [1, 3, 5]},
            }
        ],
        tags=["market", "breadth", "temperature", "forward-validation", "matrix", "read-only"],
    ),
}
