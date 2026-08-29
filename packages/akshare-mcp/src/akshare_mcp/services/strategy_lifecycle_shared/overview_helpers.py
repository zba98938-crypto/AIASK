"""Lifecycle overview builders and service-level composition."""

from __future__ import annotations

import logging
from datetime import date
from typing import Any

from .common import (
    DEPRECATION_THRESHOLDS,
    PROMOTION_THRESHOLDS,
    _EARLY_SIGNAL_STAGES,
    _EARLY_STAGE_PROMOTION_MDD_TOLERANCE,
    _EXECUTION_AUDIT_PROMOTION_BLOCKING_STAGES,
    _TREND_EXECUTABLE_DSL_TYPES,
    _confidence_diagnostics_enabled,
    _promotion_cross_regime_enabled,
    evaluate_cross_regime_skill,
    _quality_report_bool,
    _quality_report_field,
    _safe_float,
    _safe_int,
    _string,
    metric_bucket_value,
)
from .execution_quality import (
    EXPECTED_FORWARD_DAYS,
    _build_confidence_diagnostics,
    _build_execution_quality_snapshot,
    _build_position_cycle_evidence,
    _build_signal_quality_snapshot,
    _normalize_execution_quality_for_contract,
    _resolve_high_precision_overview_context,
    build_execution_quality,
    derive_signal_quality,
)
from .incubation import (
    _coerce_date,
    get_latest_quality_report,
    resolve_incubation_action_plan,
    resolve_incubation_pipeline_stage,
)
from .execution_audit_snapshot import (
    snapshot_verdict_payload,
    with_execution_audit_snapshot_metadata,
)
from .prediction_trace import (
    _build_prediction_trace_ledger_view,
    _build_execution_lineage,
    _extract_runtime_playbook_provenance,
    _extract_semantic_lineage,
    _load_prediction_trace_entity_chain,
)

logger = logging.getLogger(__name__)

def _quality_report_timestamp(payload: dict[str, Any] | None) -> str | None:
    report = dict(payload or {})
    return _string(report.get("updated_at") or report.get("created_at")) or None


# PR-S2: closure_snapshots.snapshot 入库前裁剪。
# 当前 _assemble_overview_result 返回的 result 里嵌入了：
#   - execution_audit_snapshot（已改为 _ref，但 cached_payload 路径仍可能保留旧字段）
#   - quality_report（最大 ~200 KB）
#   - backtest_report（最大 ~3 MB，单独嵌进 result）
# closure_snapshots 是生命周期视图、不需要这些原文，全部 pop 掉。
_CLOSURE_SNAPSHOT_DROP_FIELDS = (
    "execution_audit_snapshot",
    "quality_report",
    "backtest_report",
    "validation_report",
    "stages",
)


def _trim_closure_snapshot(result: dict[str, Any] | None) -> dict[str, Any]:
    """裁剪 closure_snapshots.snapshot 写入前的 result：删掉嵌进来的大对象。"""
    snap = dict(result or {})
    for big_field in _CLOSURE_SNAPSHOT_DROP_FIELDS:
        snap.pop(big_field, None)
    return snap




def _resolve_risk_hard_gate(
    strategy: dict,
    *,
    max_drawdown: float,
) -> dict[str, Any]:
    params = dict(strategy.get("params") or {})
    drawdown_contract = dict(
        strategy.get("drawdown_invalidation_contract")
        or params.get("drawdown_invalidation_contract")
        or {}
    )
    parameter_coherence_audit = dict(
        strategy.get("parameter_coherence_audit")
        or params.get("parameter_coherence_audit")
        or {}
    )
    reasons: list[str] = []
    status = "passed"
    apply_as_hard_gate = bool(drawdown_contract.get("apply_as_hard_gate"))
    review_drawdown_pct = _safe_float(drawdown_contract.get("review_drawdown_pct"))
    kill_drawdown_pct = _safe_float(drawdown_contract.get("kill_drawdown_pct"))
    coherence_blockers = [
        _string(item)
        for item in list(parameter_coherence_audit.get("blockers") or [])
        if _string(item)
    ]
    if coherence_blockers:
        status = "failed_parameters"
        reasons.extend(f"parameter_coherence:{item}" for item in coherence_blockers)
    if apply_as_hard_gate and kill_drawdown_pct is not None and kill_drawdown_pct > 0 and max_drawdown >= kill_drawdown_pct:
        status = "kill_switch"
        reasons.append(f"max_drawdown>={kill_drawdown_pct:.0%}")
    elif apply_as_hard_gate and review_drawdown_pct is not None and review_drawdown_pct > 0 and max_drawdown >= review_drawdown_pct and status == "passed":
        status = "forced_review"
        reasons.append(f"max_drawdown>={review_drawdown_pct:.0%}")
    result = {
        "status": status,
        "reasons": list(dict.fromkeys(reasons)),
        "drawdown_invalidation_contract": drawdown_contract,
        "parameter_coherence_audit": parameter_coherence_audit,
    }
    return result


async def _persist_and_finalize_overview(
    db,
    *,
    strategy: dict,
    strategy_id: str,
    result: dict,
    execution_audit_snapshot,
    quality_report_updated_at,
    signal_stats_signature=None,
    execution_state_signature=None,
) -> dict:
    upsert_strategy_closure_snapshot = getattr(db, "upsert_strategy_closure_snapshot", None)
    if callable(upsert_strategy_closure_snapshot):
        try:
            closure_snapshot = await upsert_strategy_closure_snapshot(
                {
                    "strategy_id": strategy_id,
                    "snapshot_type": "incubation_overview",
                    "snapshot_id": f"cls_{strategy_id}_incubation_overview",
                    "as_of": result.get("as_of"),
                    "source_run_id": (execution_audit_snapshot or {}).get("source_run_id"),
                    "factory_run_id": (execution_audit_snapshot or {}).get("factory_run_id"),
                    "correlation_id": (execution_audit_snapshot or {}).get("correlation_id"),
                    "trace_id": (execution_audit_snapshot or {}).get("trace_id"),
                    "submission_lane": (execution_audit_snapshot or {}).get("submission_lane"),
                    "parent_task_run_id": (execution_audit_snapshot or {}).get("parent_task_run_id"),
                    "source_action": (execution_audit_snapshot or {}).get("source_action") or "incubation_overview",
                    # PR-S2: closure_snapshots.snapshot 不再 inline 大对象。
                    # _trim_closure_snapshot 删掉嵌进 result 的 audit/quality_report/backtest 副本。
                    "snapshot": _trim_closure_snapshot(result),
                    "metadata": {
                        "strategy_status": strategy.get("status"),
                        "quality_report_updated_at": quality_report_updated_at,
                        "signal_stats_signature": signal_stats_signature or {},
                        "execution_state_signature": execution_state_signature or {},
                        "execution_audit_snapshot_id": (execution_audit_snapshot or {}).get("snapshot_id"),
                        "pipeline_stage": result.get("pipeline_stage"),
                        "promotion_gate_status": result.get("promotion_gate_status"),
                        "latest_signal_snapshot_as_of": dict(result.get("latest_signal_snapshot") or {}).get("as_of_date"),
                        "snapshot_source": "incubation_overview",
                    },
                }
            )
            if closure_snapshot:
                result["closure_snapshot_id"] = closure_snapshot.get("snapshot_id")
                result["snapshot_source"] = "strategy_closure_snapshots"
        except Exception as exc:
            logger.warning(
                "failed to persist incubation overview closure snapshot for %s: %s",
                strategy_id,
                exc,
            )
    return with_execution_audit_snapshot_metadata(
        result,
        snapshot=execution_audit_snapshot,
    )
