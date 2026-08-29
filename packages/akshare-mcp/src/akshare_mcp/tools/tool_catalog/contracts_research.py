"""research-category tool contracts (split from tool_catalog)."""

from __future__ import annotations

from typing import Any

from ._helpers import STANDARD_ENVELOPE_OUTPUT_SCHEMA, _contract

CONTRACTS: dict[str, dict[str, Any]] = {
    "analyze_stock_workflow": _contract(
        name="analyze_stock_workflow",
        title="Analyze Stock Workflow",
        category="research",
        description="AI-facing narrow workflow for stock snapshot analysis with profile, kline, financial and decision context.",
        required_params=["code"],
        input_schema={
            "type": "object",
            "properties": {
                "code": {"type": "string", "description": "6-digit stock code"},
                "investment_style": {"type": "string"},
                "include_kline": {"type": "boolean"},
                "include_financials": {"type": "boolean"},
                "include_decision": {"type": "boolean"},
                "kline_limit": {"type": "integer", "minimum": 20, "maximum": 240},
                "as_of": {"type": "string", "description": "PIT cutoff date (ISO string). Omit for current time."},
            },
            "required": ["code"],
            "additionalProperties": True,
        },
        side_effect_level="read_only",
        freshness="intraday_quote_and_recent_fundamental_context",
        examples=[
            {
                "description": "Generate a read-only stock snapshot for one code",
                "arguments": {"code": "600519", "include_financials": True, "include_decision": True},
            }
        ],
        tags=["workflow", "stock-analysis", "ai-friendly"],
    ),
    "analyze_stock_product_workflow": _contract(
        name="analyze_stock_product_workflow",
        title="Analyze Stock Product Workflow",
        category="research",
        description="Unified stock deep-analysis workflow with evidence normalization, integrity gate, agent review, synthesis and report bundle artifacts.",
        required_params=["code"],
        input_schema={
            "type": "object",
            "properties": {
                "code": {"type": "string", "description": "6-digit stock code or stock name"},
                "task": {
                    "type": "string",
                    "enum": ["quick_scan", "deep_analysis", "recover_gaps", "rebuild_report", "trade_plan"],
                },
                "run_id": {"type": "string", "description": "Existing run id for rebuild_report"},
                "investment_style": {"type": "string"},
                "user_id": {"type": "string"},
                "market": {"type": "string", "description": "Market scope; cn by default for phase 1"},
                "as_of": {"type": "string", "description": "PIT cutoff date (ISO string). Omit for current time."},
            },
            "required": ["code"],
            "additionalProperties": True,
        },
        side_effect_level="stateful",
        freshness="intraday_quote_and_recent_fundamental_context",
        examples=[
            {
                "description": "Run full deep analysis for a stock",
                "arguments": {"code": "600519", "task": "deep_analysis", "investment_style": "balanced"},
            },
            {
                "description": "Rebuild an existing HTML report bundle",
                "arguments": {"code": "600519", "task": "rebuild_report", "run_id": "stock-analysis-run-600519-demo"},
            },
        ],
        tags=["workflow", "stock-analysis", "product-surface", "lineage"],
    ),
    "experiment_tracker": _contract(
        name="experiment_tracker",
        title="Experiment Tracker",
        category="research",
        description=(
            "Log and query experiment runs, metrics, and artifacts. "
            "Builtin by default; switches to MLflow when installed. "
            "Actions: backend, log_run, log_metric, log_artifact, get_run, list_runs."
        ),
        required_params=["action"],
        input_schema={
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["backend", "log_run", "log_metric", "log_artifact", "get_run", "list_runs"]},
                "experiment_name": {"type": "string"},
                "run_id": {"type": "string"},
                "metric_key": {"type": "string"},
                "metric_value": {"type": "number"},
                "metric_step": {"type": "integer"},
                "artifact_key": {"type": "string"},
                "artifact_data": {"type": "object"},
                "params": {"type": "object"},
                "tags": {"type": "object"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 200},
            },
            "required": ["action"],
            "additionalProperties": False,
        },
        side_effect_level="stateful",
        freshness="session_scoped_in_memory",
        examples=[
            {"description": "Start an experiment run", "arguments": {"action": "log_run", "experiment_name": "factor_ic_study", "params": {"codes": ["600519"], "horizon": 10}}},
            {"description": "List recent runs", "arguments": {"action": "list_runs", "experiment_name": "factor_ic_study", "limit": 10}},
            {"description": "Check active backend (builtin vs MLflow)", "arguments": {"action": "backend"}},
        ],
        tags=["experiment", "tracking", "mlflow", "lineage", "p2"],
    ),
    "data_validation": _contract(
        name="data_validation",
        title="Data Validation",
        category="research",
        description=(
            "Validate a dataset against expectations: required fields, missing ratios, "
            "type conformance, and minimum quality threshold. "
            "Builtin by default; switches to Great Expectations when installed. "
            "Actions: backend, validate."
        ),
        required_params=["action"],
        input_schema={
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["backend", "validate"]},
                "records": {"type": "array", "items": {"type": "object"}, "description": "List of records to validate"},
                "expectations": {"type": "object", "description": "Expectation config: required_fields, type_map, value_ranges, etc."},
                "dataset_id": {"type": "string"},
                "minimum_quality_threshold": {"type": "number", "minimum": 0.0, "maximum": 1.0},
            },
            "required": ["action"],
            "additionalProperties": False,
        },
        side_effect_level="read_only",
        freshness="stateless_per_call",
        examples=[
            {"description": "Validate a batch of records for required fields", "arguments": {"action": "validate", "records": [{"close": 10.5, "volume": 1000}], "expectations": {"required_fields": ["close", "volume"]}, "minimum_quality_threshold": 0.95}},
            {"description": "Check active backend", "arguments": {"action": "backend"}},
        ],
        tags=["data-quality", "validation", "great-expectations", "p2"],
    ),
}
