"""Workflow guides (split from tool_catalog)."""

from __future__ import annotations

from typing import Any

WORKFLOW_GUIDES: dict[str, dict[str, Any]] = {
    "stock-analysis": {
        "name": "stock-analysis",
        "title": "Stock Analysis Guide",
        "recommended_tools": ["analyze_stock_workflow", "resource://stock/{code}/profile", "stock-analysis"],
        "steps": [
            "Read stock profile context first.",
            "Fetch a single workflow snapshot instead of chaining many raw tools.",
            "Only fall back to manager tools when the workflow result is insufficient.",
        ],
        "guardrails": [
            "Expose evidence and confidence separately.",
            "Do not treat heuristic decision output as a production-grade forecast.",
        ],
    },
    "stock-deep-analysis": {
        "name": "stock-deep-analysis",
        "title": "Stock Deep Analysis Guide",
        "recommended_tools": [
            "analyze_stock_product_workflow",
            "run_skill(skill_id=akshare-stock-deep-analysis)",
            "resource://stock/{code}/deep-analysis",
            "resource://analysis-run/{run_id}/summary",
            "resource://analysis-run/{run_id}/report",
            "stock-analysis-deep",
        ],
        "steps": [
            "Resolve the stock target first; do not continue when name resolution is ambiguous.",
            "Use a single product workflow run to materialize evidence, gap report, review, synthesis and report artifacts.",
            "Read the persisted run summary or report resource instead of re-chaining raw tools in Web or BFF surfaces.",
        ],
        "guardrails": [
            "Block final report publication when critical fields are missing.",
            "Every qualitative section must cite evidence ids or explicit structured sources.",
            "Keep quick_scan and deep_analysis distinct in output scope and report depth.",
        ],
    },
    "factor-governance": {
        "name": "factor-governance",
        "title": "Factor Governance Guide",
        "recommended_tools": ["factor_candidate_workflow", "quant_manager", "factor-registry-review"],
        "steps": [
            "Generate candidates, then validate them, then inspect registry or research memory.",
            "Check fallback and degraded flags before promoting any candidate.",
            "Persist artifact IDs for replay and later review.",
        ],
        "guardrails": [
            "Do not interpret candidate generation as validation success.",
            "Treat scheduler runs and memory writes as stateful operations.",
        ],
    },
    "strategy-promotion": {
        "name": "strategy-promotion",
        "title": "Strategy Promotion Guide",
        "recommended_tools": ["strategy_review_workflow", "resource://strategy/{id}/review", "strategy-promotion-review"],
        "steps": [
            "Read lifecycle projection and runtime context together.",
            "Inspect latest promotion review before triggering runtime-side actions.",
            "Keep factory runs and runtime cycles explicit and auditable.",
        ],
        "guardrails": [
            "Do not infer deployability from ranking alone.",
            "Surface runtime risk and promotion blockers separately from recommendation text.",
        ],
    },
    "governance-monitoring": {
        "name": "governance-monitoring",
        "title": "Governance Monitoring Guide",
        "recommended_tools": [
            "governance_check_workflow",
            "resource://governance/system/report",
            "resource://factor/{factor_id}/profile",
            "resource://model/{model_id}/profile",
            "resource://strategy/{strategy_id}/governance",
        ],
        "steps": [
            "Start with a system-wide governance check to identify flagged dimensions.",
            "Drill into specific factors, models, or strategies using targeted checks.",
            "Review factor decay and crowding before promoting new candidates.",
            "Compare backtest vs execution assumptions to validate strategy readiness.",
        ],
        "guardrails": [
            "Do not interpret 'healthy' governance as permission to deploy.",
            "Always check online/offline consistency before trusting backtest results.",
            "Surface all governance warnings to the user for final decision.",
        ],
    },
}
