"""data_sync-category tool contracts (split from tool_catalog)."""

from __future__ import annotations

from typing import Any

from ._helpers import STANDARD_ENVELOPE_OUTPUT_SCHEMA, _contract

CONTRACTS: dict[str, dict[str, Any]] = {
    "data_quality_workflow": _contract(
        name="data_quality_workflow",
        title="Data Quality Workflow",
        category="data_sync",
        description="Assess dataset completeness, missing fields, minimum quality gate and remediation hints.",
        required_params=[],
        input_schema={
            "type": "object",
            "properties": {
                "dataset_id": {"type": "string"},
                "records": {"type": "array", "items": {"type": "object"}},
                "required_fields": {"type": "array", "items": {"type": "string"}},
                "as_of_field": {"type": "string"},
                "as_of_value": {"type": "string"},
                "source": {"type": "string"},
                "source_chain": {"type": "array", "items": {"type": "string"}},
                "minimum_quality_threshold": {"type": "number"},
                "persist_artifact": {"type": "boolean"},
                "output_artifact_id": {"type": "string"},
                "as_of": {"type": "string", "description": "PIT cutoff date (ISO string). Omit for current time."},
            },
            "additionalProperties": True,
        },
        side_effect_level="stateful",
        freshness="depends_on_input_dataset_snapshot",
        examples=[
            {
                "description": "Validate a small record batch against required fields",
                "arguments": {
                    "dataset_id": "dataset_demo",
                    "required_fields": ["code", "date", "close"],
                    "records": [{"code": "600519", "date": "2026-04-01", "close": 1820.5}],
                },
            }
        ],
        tags=["workflow", "data-quality", "dataset", "validation"],
    ),
    "sync_market_temperature_snapshot_cache": _contract(
        name="sync_market_temperature_snapshot_cache",
        title="Sync Market Temperature Snapshot Cache",
        category="data_sync",
        description=(
            "Data-sync entrypoint for refreshing the persisted market thermometer cache "
            "from local SQLite stock and K-line data."
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
                "description": "Refresh the market thermometer cache from the data-sync surface",
                "arguments": {"limit": 1000, "top_n": 20},
            }
        ],
        tags=["data-sync", "market", "breadth", "temperature", "cache", "stateful"],
    ),
}
