"""AI-facing governance check workflow tool.

Provides ``governance_check_workflow`` — a narrow MCP tool that runs
selected governance monitoring dimensions and returns a unified report
in the standard envelope format with PIT + lineage metadata.

Usage by AI::

    result = await governance_check_workflow(
        target_type="factor",
        target_id="momentum_20d",
        ic_history=[0.05, 0.04, 0.03, 0.02],
    )
"""

from __future__ import annotations

import time
from typing import Any

from ..services.governance_monitor import GovernanceMonitor
from ..services.governance_persistence import persist_governance_report_snapshot
from ..services.lineage_tracker import LineageContext
from .manager_protocol import fail_with_meta, ok_with_meta
from .pit_middleware import build_pit_meta_simple
from .tool_catalog import build_tool_meta


_monitor = GovernanceMonitor()


async def governance_check_workflow(
    target_type: str = "system",
    target_id: str | None = None,
    # Factor decay
    ic_history: list[float] | None = None,
    factor_expression: str = "",
    factor_category: str | None = None,
    existing_factor_pool: list[str] | None = None,
    # Model drift
    current_metrics: dict[str, Any] | None = None,
    baseline_metrics: dict[str, Any] | None = None,
    # Strategy health
    posture_level: str = "safe",
    control_mode: str = "active",
    open_alert_count: int = 0,
    recovery_eligible: bool = False,
    max_drawdown_pct: float | None = None,
    days_since_last_trade: int | None = None,
    # Consistency
    backtest_assumptions: dict[str, Any] | None = None,
    execution_assumptions: dict[str, Any] | None = None,
    # Feature flags
    include_factor_decay: bool = True,
    include_crowding: bool = True,
    include_model_drift: bool = True,
    include_strategy_health: bool = True,
    include_consistency: bool = True,
    # Standard params
    as_of: str | None = None,
) -> dict[str, Any]:
    """Run governance monitoring checks across selected dimensions.

    Parameters
    ----------
    target_type:
        One of "factor", "model", "strategy", or "system".
    target_id:
        Identifier of the specific target (factor name, model name, strategy ID).
    include_factor_decay / include_crowding / ... :
        Selectively enable/disable each monitoring dimension.
    as_of:
        PIT cutoff date (ISO string).

    Returns
    -------
    Standard unified envelope with ``GovernanceReport`` as ``data``.
    """
    started_at = time.perf_counter()
    resolved_type = str(target_type or "system").strip().lower()
    lineage = LineageContext.create("governance_check_workflow")

    try:
        report = _monitor.run_full_check(
            target_type=resolved_type,
            target_id=target_id,
            ic_history=ic_history,
            factor_expression=factor_expression,
            factor_category=factor_category,
            existing_factor_pool=existing_factor_pool,
            current_metrics=current_metrics,
            baseline_metrics=baseline_metrics,
            posture_level=posture_level,
            control_mode=control_mode,
            open_alert_count=open_alert_count,
            recovery_eligible=recovery_eligible,
            max_drawdown_pct=max_drawdown_pct,
            days_since_last_trade=days_since_last_trade,
            backtest_assumptions=backtest_assumptions,
            execution_assumptions=execution_assumptions,
            include_factor_decay=include_factor_decay,
            include_crowding=include_crowding,
            include_model_drift=include_model_drift,
            include_strategy_health=include_strategy_health,
            include_consistency=include_consistency,
        )
        persisted = await persist_governance_report_snapshot(
            report,
            scope_type=resolved_type,
            scope_id=target_id,
        )

        return ok_with_meta(
            {
                "workflow": "governance_check_workflow",
                "target_type": resolved_type,
                "target_id": target_id,
                "snapshot_id": persisted.get("id"),
                "report": report.to_dict(),
            },
            tool_name="governance_check_workflow",
            action="check",
            started_at=started_at,
            source_chain=["workflow.governance_check", "service.governance_monitor"],
            extra_meta={
                "quality": {
                    "status": report.overall_status,
                    "issue_count": len(report.issues),
                },
                "side_effect": {
                    "level": "read_only",
                    "target": target_id or "system",
                    "confirmation_required": False,
                    "idempotent": True,
                },
                "pit": build_pit_meta_simple(as_of),
                "lineage": lineage.to_meta(),
                "degraded": report.overall_status in ("critical", "warning"),
            },
        )

    except Exception as exc:
        return fail_with_meta(
            str(exc),
            tool_name="governance_check_workflow",
            action="check",
            started_at=started_at,
            error_code="GOVERNANCE_CHECK_FAILED",
            extra_meta={
                "quality": {"status": "failed", "workflow": "governance_check_workflow"},
                "side_effect": {"level": "read_only", "target": target_id or "system"},
                "pit": build_pit_meta_simple(as_of),
                "lineage": lineage.to_meta(),
                "degraded": True,
            },
        )


def register(mcp) -> None:
    mcp.tool(
        title="Governance Check Workflow",
        description="AI-facing workflow for governance monitoring across factor, model, strategy and system dimensions.",
        structured_output=True,
        meta=build_tool_meta("governance_check_workflow"),
    )(governance_check_workflow)
