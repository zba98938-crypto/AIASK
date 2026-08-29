"""strategy-category tool contracts (split from tool_catalog)."""

from __future__ import annotations

from typing import Any

from ._helpers import STANDARD_ENVELOPE_OUTPUT_SCHEMA, _contract
from ...contracts.strategy_manager_contract import build_strategy_manager_input_schema

CONTRACTS: dict[str, dict[str, Any]] = {
    "strategy_review_workflow": _contract(
        name="strategy_review_workflow",
        title="Strategy Review Workflow",
        category="strategy",
        description="AI-facing workflow for strategy review with lifecycle, runtime, promotion and optional refresh steps.",
        required_params=["strategy_id"],
        input_schema={
            "type": "object",
            "properties": {
                "strategy_id": {"type": "string"},
                "include_factory_status": {"type": "boolean"},
                "include_review_report": {"type": "boolean"},
                "include_runtime_alerts": {"type": "boolean"},
                "run_factory_once": {"type": "boolean"},
                "run_runtime_cycle": {"type": "boolean"},
                "idempotency_key": {"type": "string"},
                "as_of": {"type": "string", "description": "PIT cutoff date (ISO string). Omit for current time."},
            },
            "required": ["strategy_id"],
            "additionalProperties": True,
        },
        side_effect_level="stateful",
        freshness="runtime_and_review_state",
        examples=[
            {
                "description": "Review a strategy in read-only mode",
                "arguments": {"strategy_id": "strat_demo", "include_runtime_alerts": True},
            }
        ],
        tags=["workflow", "strategy", "promotion-review", "runtime"],
    ),
    "strategy_manager": _contract(
        name="strategy_manager",
        title="Strategy Manager",
        category="strategy",
        description="High-capacity manager for strategy marketplace, reviews, factory runs and runtime governance.",
        required_params=["action"],
        input_schema={
            "type": "object",
            "properties": {
                "action": {"type": "string"},
                "params": {"type": "object"},
                "kwargs": {"type": ["object", "string"]},
            },
            "required": ["action"],
            "additionalProperties": True,
        },
        side_effect_level="stateful",
        freshness="depends_on_runtime_state",
        examples=[{"description": "Read strategy review report", "arguments": {"action": "review_report", "params": {"strategy_id": "strat_demo"}}}],
        tags=["manager", "strategy", "heavy-surface"],
    ),
    "strategy_manager": _contract(
        name="strategy_manager",
        title="Strategy Manager",
        category="strategy",
        description=(
            "Strategy marketplace lifecycle manager: create, publish, lifecycle scan, "
            "promotion review, runtime alerts, incubation, vector governance, domain projection, "
            "AI generation, and factory status. Prefer strategy_review_workflow and "
            "resource://strategy/{id}/review for read-only snapshots."
        ),
        required_params=["action"],
        input_schema=build_strategy_manager_input_schema(),
        side_effect_level="stateful",
        freshness="depends_on_strategy_lifecycle_and_factory_state",
        examples=[
            {"description": "List strategies sorted by rank", "arguments": {"action": "list", "params": {"limit": 10, "sort_by": "rank"}}},
            {"description": "Get review report for a specific strategy", "arguments": {"action": "review_report", "params": {"strategy_id": "strat_001"}}},
            {"description": "Verify execution-audit schema, migrations, and linkage coverage", "arguments": {"action": "execution_audit_verification", "params": {"strategy_id": "strat_001"}}},
            {"description": "Inspect vector index health for strategy governance", "arguments": {"action": "vector_health", "params": {"index_name": "strategy_behavior"}}},
            {"description": "Run factory once to advance lifecycle", "arguments": {"action": "factory_run_once"}},
        ],
        tags=["strategy", "lifecycle", "factory", "promotion", "runtime"],
    ),
    # ── P2-1 adapter tools ───────────────────────────────────────────────────
}
