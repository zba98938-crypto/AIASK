"""Research object MCP resources.

Provides 6 read-only MCP resource templates for AI consumption:

1. resource://factor/{factor_id}/profile
2. resource://model/{model_id}/profile
3. resource://dataset/{dataset_id}/profile
4. resource://strategy/{strategy_id}/governance
5. resource://experiment/{experiment_id}/summary
6. resource://governance/system/report

These resources expose factor, model, dataset, strategy, experiment and
governance objects as structured, subscribable MCP surfaces — transforming
scattered API return values into first-class research objects.
"""

from __future__ import annotations

from typing import Any

from ..services.artifact_registry import get_artifact_async
from ..services.governance_monitor import GovernanceMonitor
from ..services.governance_persistence import (
    get_latest_governance_report_snapshot,
    persist_governance_report_snapshot,
)
from ..tools.manager_protocol import LINEAGE_REFERENCE_KEYS


# ── Helpers ───────────────────────────────────────────────────────────────────

def _extract_lineage(payload: dict[str, Any] | None) -> dict[str, Any]:
    source = dict(payload or {})
    lineage: dict[str, Any] = {}
    for key in LINEAGE_REFERENCE_KEYS:
        value = source.get(key)
        if value not in (None, "", []):
            lineage[key] = value
    explicit = source.get("lineage")
    if isinstance(explicit, dict):
        for key, value in explicit.items():
            if value not in (None, "", []):
                lineage.setdefault(str(key), value)
    return lineage


_monitor = GovernanceMonitor()


# ── 1. Factor Profile ────────────────────────────────────────────────────────

async def build_factor_profile_payload(factor_id: str) -> dict[str, Any]:
    """Build a comprehensive factor profile from registry and artifacts."""
    resolved_id = str(factor_id or "").strip()

    # Try to get factor artifact
    artifact = await get_artifact_async(resolved_id)
    if not artifact:
        return {
            "uri": f"resource://factor/{resolved_id}/profile",
            "factor_id": resolved_id,
            "found": False,
            "error": f"Factor not found: {resolved_id}",
            "hint": "Use factor_candidate_workflow to generate and register factors.",
        }

    payload = dict(artifact)
    nested = dict(payload.get("payload") or {}) if isinstance(payload.get("payload"), dict) else {}

    # Extract enrichment if available
    enrichment = nested.get("factor_enrichment") or payload.get("factor_enrichment") or {}
    validation = nested.get("validation") or payload.get("validation") or {}

    return {
        "uri": f"resource://factor/{resolved_id}/profile",
        "factor_id": resolved_id,
        "found": True,
        "expression": nested.get("expression") or payload.get("expression"),
        "hypothesis": nested.get("hypothesis") or payload.get("hypothesis"),
        "category": nested.get("category") or payload.get("category"),
        "enrichment": enrichment,
        "validation": validation,
        "quality": nested.get("quality") or payload.get("quality") or {},
        "lineage": _extract_lineage({**payload, **nested, "factor_candidate_id": resolved_id}),
        "registered_at": payload.get("registered_at"),
        "updated_at": payload.get("updated_at"),
    }


# ── 2. Model Profile ─────────────────────────────────────────────────────────

async def build_model_profile_payload(model_id: str) -> dict[str, Any]:
    """Build a model profile from registry and rolling model data."""
    resolved_id = str(model_id or "").strip()

    # Try artifact
    artifact = await get_artifact_async(resolved_id)

    # Try rolling model registry
    try:
        from ..services.rolling_model_registry import default_rolling_registry
        stability = default_rolling_registry.compute_rolling_stability(resolved_id)
        degradation = default_rolling_registry.detect_degradation(resolved_id)
        current_stage = default_rolling_registry.get_current_stage(resolved_id)
    except Exception:
        stability = {}
        degradation = {}
        current_stage = "unknown"

    if not artifact and not stability.get("available"):
        return {
            "uri": f"resource://model/{resolved_id}/profile",
            "model_id": resolved_id,
            "found": False,
            "error": f"Model not found: {resolved_id}",
            "hint": "Record model evaluations via RollingModelRegistry or persist artifacts.",
        }

    payload = dict(artifact or {})
    nested = dict(payload.get("payload") or {}) if isinstance(payload.get("payload"), dict) else {}

    return {
        "uri": f"resource://model/{resolved_id}/profile",
        "model_id": resolved_id,
        "found": True,
        "current_stage": current_stage,
        "rolling_stability": stability if stability.get("available") else None,
        "degradation_check": degradation if degradation.get("model_name") else None,
        "quality": nested.get("quality") or payload.get("quality") or {},
        "lineage": _extract_lineage({**payload, **nested, "model_id": resolved_id}),
        "artifact": {
            "artifact_type": payload.get("artifact_type"),
            "registered_at": payload.get("registered_at"),
            "updated_at": payload.get("updated_at"),
        } if artifact else None,
    }


# ── 3. Dataset Profile ───────────────────────────────────────────────────────

async def build_dataset_profile_payload(dataset_id: str) -> dict[str, Any]:
    """Build a dataset profile from artifact registry."""
    resolved_id = str(dataset_id or "").strip()

    artifact = await get_artifact_async(resolved_id)
    if not artifact:
        return {
            "uri": f"resource://dataset/{resolved_id}/profile",
            "dataset_id": resolved_id,
            "found": False,
            "error": f"Dataset not found: {resolved_id}",
            "hint": "Run data_quality_workflow with persist_artifact=true to materialize a dataset profile.",
        }

    payload = dict(artifact)
    nested = dict(payload.get("payload") or {}) if isinstance(payload.get("payload"), dict) else {}
    quality = nested.get("quality") or payload.get("quality") or nested.get("quality_meta") or {}

    return {
        "uri": f"resource://dataset/{resolved_id}/profile",
        "dataset_id": resolved_id,
        "found": True,
        "quality": quality,
        "record_count": nested.get("accepted_count", 0) + nested.get("rejected_count", 0),
        "accepted_count": nested.get("accepted_count"),
        "rejected_count": nested.get("rejected_count"),
        "minimum_quality_passed": nested.get("minimum_quality_passed"),
        "lineage": _extract_lineage({**payload, **nested, "dataset_id": resolved_id}),
        "artifact": {
            "artifact_type": payload.get("artifact_type"),
            "registered_at": payload.get("registered_at"),
            "updated_at": payload.get("updated_at"),
        },
    }


# ── 4. Strategy Governance ────────────────────────────────────────────────────

async def build_strategy_governance_payload(strategy_id: str) -> dict[str, Any]:
    """Build a strategy governance report."""
    resolved_id = str(strategy_id or "").strip()
    persisted = await get_latest_governance_report_snapshot(scope_type="strategy", scope_id=resolved_id)
    if persisted:
        report_payload = dict(persisted.get("payload_jsonb") or {})
    else:
        report = _monitor.run_full_check(
            target_type="strategy",
            target_id=resolved_id,
            include_factor_decay=False,
            include_crowding=False,
            include_model_drift=False,
        )
        persisted = await persist_governance_report_snapshot(
            report,
            scope_type="strategy",
            scope_id=resolved_id,
        )
        report_payload = report.to_dict()

    return {
        "uri": f"resource://strategy/{resolved_id}/governance",
        "strategy_id": resolved_id,
        "found": True,
        "snapshot_id": persisted.get("id") if persisted else None,
        "governance_report": report_payload,
        "lineage": {"strategy_id": resolved_id},
    }


# ── 5. Experiment Summary ────────────────────────────────────────────────────

async def build_experiment_summary_payload(experiment_id: str) -> dict[str, Any]:
    """Build an experiment summary from artifact registry."""
    resolved_id = str(experiment_id or "").strip()

    artifact = await get_artifact_async(resolved_id)
    if not artifact:
        return {
            "uri": f"resource://experiment/{experiment_id}/summary",
            "experiment_id": resolved_id,
            "found": False,
            "error": f"Experiment not found: {resolved_id}",
            "hint": "Record experiment results via artifact_registry or experiment tracker adapter.",
        }

    payload = dict(artifact)
    nested = dict(payload.get("payload") or {}) if isinstance(payload.get("payload"), dict) else {}

    return {
        "uri": f"resource://experiment/{resolved_id}/summary",
        "experiment_id": resolved_id,
        "found": True,
        "artifact_type": payload.get("artifact_type"),
        "strategy": payload.get("strategy"),
        "parameters": nested.get("parameters") or nested.get("params") or {},
        "results": nested.get("results") or nested.get("metrics") or {},
        "quality": nested.get("quality") or payload.get("quality") or {},
        "lineage": _extract_lineage({**payload, **nested, "experiment_id": resolved_id}),
        "registered_at": payload.get("registered_at"),
        "updated_at": payload.get("updated_at"),
    }


# ── 6. System Governance Report ───────────────────────────────────────────────

async def build_system_governance_payload() -> dict[str, Any]:
    """Build a system-wide governance report."""
    persisted = await get_latest_governance_report_snapshot(scope_type="system", scope_id=None)
    if persisted:
        report_payload = dict(persisted.get("payload_jsonb") or {})
    else:
        report = _monitor.run_full_check(target_type="system")
        persisted = await persist_governance_report_snapshot(report, scope_type="system", scope_id=None)
        report_payload = report.to_dict()
    return {
        "uri": "resource://governance/system/report",
        "found": True,
        "snapshot_id": persisted.get("id") if persisted else None,
        "governance_report": report_payload,
    }


# ── Registration ──────────────────────────────────────────────────────────────

def register(mcp) -> None:
    """Register research object MCP resources."""

    @mcp.resource(
        "resource://factor/{factor_id}/profile",
        name="factor_profile",
        title="Factor Profile",
        description="Comprehensive factor profile with enrichment, validation, decay and crowding data",
        mime_type="application/json",
    )
    async def factor_profile(factor_id: str) -> dict[str, Any]:
        return await build_factor_profile_payload(factor_id)

    @mcp.resource(
        "resource://model/{model_id}/profile",
        name="model_profile",
        title="Model Profile",
        description="Model lifecycle profile with rolling stability, degradation, and champion/challenger state",
        mime_type="application/json",
    )
    async def model_profile(model_id: str) -> dict[str, Any]:
        return await build_model_profile_payload(model_id)

    @mcp.resource(
        "resource://dataset/{dataset_id}/profile",
        name="dataset_profile",
        title="Dataset Profile",
        description="Dataset quality profile with validation summary, PIT status, and lineage",
        mime_type="application/json",
    )
    async def dataset_profile(dataset_id: str) -> dict[str, Any]:
        return await build_dataset_profile_payload(dataset_id)

    @mcp.resource(
        "resource://strategy/{strategy_id}/governance",
        name="strategy_governance",
        title="Strategy Governance Report",
        description="Strategy governance report with health, alerts, and consistency analysis",
        mime_type="application/json",
    )
    async def strategy_governance(strategy_id: str) -> dict[str, Any]:
        return await build_strategy_governance_payload(strategy_id)

    @mcp.resource(
        "resource://experiment/{experiment_id}/summary",
        name="experiment_summary",
        title="Experiment Summary",
        description="Experiment artifact summary with parameters, results, quality, and lineage",
        mime_type="application/json",
    )
    async def experiment_summary(experiment_id: str) -> dict[str, Any]:
        return await build_experiment_summary_payload(experiment_id)

    @mcp.resource(
        "resource://governance/system/report",
        name="system_governance_report",
        title="System Governance Report",
        description="System-wide governance report across all monitoring dimensions",
        mime_type="application/json",
    )
    async def system_governance_report() -> dict[str, Any]:
        return await build_system_governance_payload()
