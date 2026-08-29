"""quant-category tool contracts (split from tool_catalog)."""

from __future__ import annotations

from typing import Any

from ._helpers import STANDARD_ENVELOPE_OUTPUT_SCHEMA, _contract

CONTRACTS: dict[str, dict[str, Any]] = {
    "factor_candidate_workflow": _contract(
        name="factor_candidate_workflow",
        title="Factor Candidate Workflow",
        category="quant",
        description="AI-facing workflow for factor candidate generation, validation, registry review and scheduler checks.",
        required_params=[],
        input_schema={
            "type": "object",
            "properties": {
                "task": {
                    "type": "string",
                    "enum": ["pipeline", "generate", "validate", "registry_review", "scheduler_check"],
                },
                "code": {"type": "string"},
                "codes": {"type": "array", "items": {"type": "string"}},
                "artifact_id": {"type": "string"},
                "candidate_index": {"type": "integer", "minimum": 0},
                "candidate_count": {"type": "integer", "minimum": 1, "maximum": 24},
                "persist_artifact": {"type": "boolean"},
                "write_memory": {"type": "boolean"},
                "run_scheduler_now": {"type": "boolean"},
                "idempotency_key": {"type": "string"},
                "as_of": {"type": "string", "description": "PIT cutoff date (ISO string). Omit for current time."},
            },
            "additionalProperties": True,
        },
        side_effect_level="stateful",
        freshness="depends_on_market_context_and_artifact_state",
        examples=[
            {
                "description": "Generate and validate candidate factors for several stocks",
                "arguments": {"task": "pipeline", "codes": ["600519", "000001"], "candidate_count": 6},
            }
        ],
        tags=["workflow", "factor-mining", "registry", "lineage"],
    ),
    "prediction_diagnosis_workflow": _contract(
        name="prediction_diagnosis_workflow",
        title="Prediction Diagnosis Workflow",
        category="quant",
        description="Diagnose model probabilities with calibration, uncertainty and lineage-friendly output.",
        required_params=["probabilities", "labels"],
        input_schema={
            "type": "object",
            "properties": {
                "probabilities": {"type": "array", "items": {"type": "number"}},
                "labels": {"type": "array", "items": {"type": "number"}},
                "raw_scores": {"type": "array", "items": {"type": "number"}},
                "method": {"type": "string", "enum": ["raw", "platt", "isotonic"]},
                "platt_a": {"type": "number"},
                "platt_b": {"type": "number"},
                "coverage_target": {"type": "number"},
                "dataset_id": {"type": "string"},
                "run_id": {"type": "string"},
                "persist_artifact": {"type": "boolean"},
                "output_artifact_id": {"type": "string"},
                "as_of": {"type": "string", "description": "PIT cutoff date (ISO string). Omit for current time."},
            },
            "required": ["probabilities", "labels"],
            "additionalProperties": True,
        },
        side_effect_level="stateful",
        freshness="depends_on_input_predictions_and_labels",
        examples=[
            {
                "description": "Build a calibration report from model probabilities",
                "arguments": {"probabilities": [0.2, 0.8], "labels": [0, 1], "method": "raw"},
            }
        ],
        tags=["workflow", "prediction", "calibration", "uncertainty"],
    ),
    "search_by_kline": _contract(
        name="search_by_kline",
        title="Search By Kline Pattern",
        category="quant",
        description="Find stocks with K-line shapes similar to a target stock over a recent window.",
        required_params=["code"],
        input_schema={
            "type": "object",
            "properties": {
                "code": {"type": "string", "description": "Target stock code."},
                "days": {"type": "integer", "minimum": 5, "maximum": 240},
                "top_n": {"type": "integer", "minimum": 1, "maximum": 50},
                "search_backend": {"type": "string", "enum": ["db", "memory"]},
                "allow_fallback": {"type": "boolean"},
            },
            "required": ["code"],
            "additionalProperties": True,
        },
        side_effect_level="read_only",
        freshness="recent_kline_vector_snapshot",
        examples=[
            {
                "description": "Match recent 20-day K-line patterns",
                "arguments": {"code": "600519", "days": 20, "top_n": 10},
            }
        ],
        tags=["quant", "kline", "vector", "pattern-search"],
    ),
    "quant_manager": _contract(
        name="quant_manager",
        title="Quant Manager",
        category="quant",
        description="High-capacity manager for quant research, factor mining, model registry and experiment replay.",
        required_params=["action"],
        input_schema={
            "type": "object",
            "properties": {
                "action": {"type": "string"},
                "params": {"type": "object"},
                "kwargs": {"type": ["object", "string"]},
                "code": {"type": "string"},
            },
            "required": ["action"],
            "additionalProperties": True,
        },
        side_effect_level="stateful",
        freshness="depends_on_action",
        examples=[{"description": "Call factor candidate generation", "arguments": {"action": "llm_factor_mining", "params": {"codes": ["600519"]}}}],
        tags=["manager", "quant", "heavy-surface"],
    ),
    "quant_manager": _contract(
        name="quant_manager",
        title="Quant Manager",
        category="quant",
        description=(
            "Quant research hub: factor mining, candidate validation, registry, research memory, "
            "autoML, IC analysis, and scheduler control. Prefer factor_candidate_workflow for "
            "end-to-end pipelines; use quant_manager directly for individual steps."
        ),
        required_params=["action"],
        input_schema={
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": [
                        "help", "llm_factor_mining", "validate_factor_candidate",
                        "factor_candidate_registry", "factor_research_memory",
                        "factor_ic", "batch_compute_factors", "calculate_factors",
                        "automl_discovery", "champion_challenger", "model_registry",
                        "replay_experiment", "scheduler_status", "scheduler_run_now",
                    ],
                },
                "params": {"type": "object"},
                "kwargs": {"type": ["object", "string", "null"]},
                "code": {"type": "string", "description": "Single 6-digit stock code"},
                "dry_run": {"type": "boolean", "description": "If true, validate only; no artifact is persisted."},
                "as_of": {"type": "string", "description": "PIT cutoff date (ISO). Applies to validation and IC steps."},
            },
            "required": ["action"],
            "additionalProperties": True,
        },
        side_effect_level="stateful",
        freshness="depends_on_market_data_and_artifact_state",
        examples=[
            {"description": "Mine LLM factor candidates for two stocks", "arguments": {"action": "llm_factor_mining", "params": {"codes": ["600519", "000001"], "candidate_count": 4, "persist_artifact": True}}},
            {"description": "Validate a factor candidate from artifact (dry_run)", "arguments": {"action": "validate_factor_candidate", "params": {"artifact_id": "art_xxx", "dry_run": True}}},
            {"description": "Check active pool in factor registry", "arguments": {"action": "factor_candidate_registry", "params": {"op": "active_pool", "limit": 20}}},
        ],
        tags=["quant", "factor-mining", "registry", "automl"],
    ),
}
