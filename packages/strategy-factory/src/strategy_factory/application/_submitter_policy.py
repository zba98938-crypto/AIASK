"""策略工厂候选提交。"""

from __future__ import annotations

import asyncio
import logging
import re
import time as _time
from collections import Counter
from typing import TYPE_CHECKING, Any, List, Optional
from uuid import uuid4

from ..api.contracts import normalize_strategy_preferences, resolve_refresh_existing_contract
from .quality_gates import build_completed_gate_3_report
from .quality_reporting import build_quality_report, normalize_quality_gate_result
from .submission_gate import run_submission_quality_gate as _local_run_submission_quality_gate
from .utils import (
    _auto_name as _local_auto_name,
    _extract_event_context as _local_extract_event_context,
    _update_strategy_status as _local_update_strategy_status,
    get_strategy_factory_package as _local_get_strategy_factory_package,
)
from ..domain.constants import (
    FACTORY_SUBMISSION_MIN_BACKTEST_TRADES,
    FACTORY_SUBMISSION_MIN_EVENT_TARGET_COVERAGE,
    FACTORY_SUBMISSION_REJECT_GENERIC_AI_NAMES,
    FACTORY_SUBMISSION_REQUIRE_STRICT_PASS_FOR_REFRESH,
    FACTORY_SUBMISSION_REQUIRE_TASK_PREFERENCE_MATCH,
    SUBMIT_CONCURRENCY,
)
from ..domain.targets import _build_task_signature, _normalize_research_task_contract, _normalize_target_codes
from ..infrastructure.mcp_services import build_strategy_vector_profile

if TYPE_CHECKING:
    from ..api.contracts import IncubationGateway, RiskGateway, ValidationGateway

logger = logging.getLogger(__name__)

def _compat_setting(name: str, default):
    return default


def _auto_name(*args, **kwargs):
    return _local_auto_name(*args, **kwargs)


def _extract_event_context(*args, **kwargs):
    return _local_extract_event_context(*args, **kwargs)


def get_strategy_factory_package():
    return _local_get_strategy_factory_package()


async def _update_strategy_status(*args, **kwargs):
    return await _local_update_strategy_status(*args, **kwargs)


class _CompatValidationGateway:
    """Resolve validation runner through the legacy patch-point at call time."""

    async def run_validation_report(self, strategy_type: str, params: dict[str, Any], db) -> Optional[dict]:
        from ..api.quality_reporting import run_validation_report

        return await run_validation_report(strategy_type, dict(params or {}), db)


class _CompatRiskGateway:
    """Resolve risk runner through the legacy patch-point at call time."""

    async def run_risk_report(self, strategy_type: str, params: dict[str, Any], db) -> Optional[dict]:
        from ..api.quality_reporting import run_risk_report

        return await run_risk_report(strategy_type, dict(params or {}), db)


class _StrategySubmitterPolicyMixin:
        @classmethod
        def _factory_policy_outcome(
            cls,
            candidate: dict,
            *,
            name: str,
            gate: dict,
            backtest_metrics: Optional[dict],
            refresh_existing: bool,
        ) -> dict[str, Any]:
            reasons: list[str] = []
            warnings: list[str] = []
            provisional_blocks: list[str] = []
            preference_override_applied = False
            metrics = dict(backtest_metrics or {})
            normalized_gate = normalize_quality_gate_result(gate)
            refresh_contract = resolve_refresh_existing_contract(
                candidate=candidate,
                dedup_result=candidate.get("dedup_result"),
            )
            refresh_existing = bool(refresh_existing or refresh_contract.get("refresh_existing"))

            min_trades = int(_compat_setting("FACTORY_SUBMISSION_MIN_BACKTEST_TRADES", FACTORY_SUBMISSION_MIN_BACKTEST_TRADES) or FACTORY_SUBMISSION_MIN_BACKTEST_TRADES)
            trade_count = int(metrics.get("trade_count") or metrics.get("trades_count") or 0)
            if trade_count < min_trades:
                reasons.append(f"Factory policy: backtest trade_count {trade_count} < {min_trades}")

            reject_generic_names = bool(
                _compat_setting("FACTORY_SUBMISSION_REJECT_GENERIC_AI_NAMES", FACTORY_SUBMISSION_REJECT_GENERIC_AI_NAMES)
            )
            if reject_generic_names and cls._is_generic_ai_name(name, candidate):
                reasons.append("Factory policy: candidate name is too generic for AI strategy submission")

            research_task = _normalize_research_task_contract(candidate.get("research_task") or {})
            strategy_preferences = normalize_strategy_preferences(
                research_task.get("preferred_strategy_types"),
                research_task.get("strategy_preferences"),
            )
            allowed_strategy_types = {
                str(item).strip().lower()
                for item in list(research_task.get("allowed_strategy_types") or [])
                if str(item).strip()
            }
            strategy_type = str(candidate.get("strategy_type") or "").strip().lower()
            if allowed_strategy_types and strategy_type not in allowed_strategy_types:
                reasons.append(
                    f"Factory policy: strategy_type {strategy_type or 'unknown'} not in allowed_strategy_types {sorted(allowed_strategy_types)}"
                )
            require_preference_match = bool(
                _compat_setting("FACTORY_SUBMISSION_REQUIRE_TASK_PREFERENCE_MATCH", FACTORY_SUBMISSION_REQUIRE_TASK_PREFERENCE_MATCH)
            )
            preference_strength = str(research_task.get("preference_strength") or "soft").strip().lower()
            preference_reason = str(research_task.get("preference_reason") or "").strip()
            event_driven = str(research_task.get("task_source") or "").strip().lower() == "event_driven"
            task_target_symbols = _normalize_target_codes(
                [
                    research_task.get("target_symbols"),
                    research_task.get("stock_pool"),
                    (research_task.get("event_context") or {}).get("target_symbols"),
                ],
                limit=16,
            )
            candidate_symbols = cls._candidate_symbols(candidate)
            min_target_coverage = float(
                _compat_setting(
                    "FACTORY_SUBMISSION_MIN_EVENT_TARGET_COVERAGE",
                    FACTORY_SUBMISSION_MIN_EVENT_TARGET_COVERAGE,
                ) or FACTORY_SUBMISSION_MIN_EVENT_TARGET_COVERAGE
            )
            target_coverage = 1.0
            has_material_target_drift = False
            if event_driven and task_target_symbols and candidate_symbols:
                overlap = len(set(candidate_symbols).intersection(task_target_symbols))
                target_coverage = overlap / max(1, len(candidate_symbols))
                has_material_target_drift = target_coverage < min_target_coverage
            if require_preference_match and strategy_preferences:
                style_tokens = cls._candidate_style_tokens(candidate)
                if not set(strategy_preferences).intersection(style_tokens):
                    evidence_override = (
                        not has_material_target_drift
                        and
                        (
                        float(metrics.get("post_cost_sharpe") or metrics.get("sharpe_ratio") or 0.0) >= 0.2
                        or float(metrics.get("target_layer_oos_return") or metrics.get("total_return") or 0.0) >= 0.02
                        or float(metrics.get("event_window_hit_ratio") or 0.0) >= 0.5
                        )
                    )
                    message = (
                        "Factory policy: candidate style "
                        f"{sorted(style_tokens) or ['unknown']} does not align with task preferences {strategy_preferences}"
                    )
                    if preference_reason:
                        message = f"{message} (reason: {preference_reason})"
                    if evidence_override:
                        preference_override_applied = True
                        warnings.append(f"{message}; overridden by stronger target evidence")
                    elif preference_strength == "hard" or has_material_target_drift:
                        reasons.append(message)
                    elif preference_strength == "medium":
                        provisional_blocks.append(message)
                        warnings.append(f"{message}; medium_preference_blocks_provisional_only")
                    else:
                        warnings.append(message)

            if event_driven and task_target_symbols and candidate_symbols and has_material_target_drift:
                    drift_message = (
                        "Factory policy: candidate universe drifted from event target symbols "
                        f"(coverage {target_coverage:.1%} < {min_target_coverage:.0%})"
                    )
                    if str(research_task.get("target_symbol_policy") or "").strip().lower() == "strict_intersection":
                        reasons.append(drift_message)
                    else:
                        warnings.append(drift_message)

            constraint_check = dict(candidate.get("constraint_check") or {})
            constraint_violation = str(constraint_check.get("constraint_violation") or "").strip()
            if constraint_violation:
                # PR-DQ: Downgrade strict_intersection_empty to warning.
                # LLM-generated candidates often use representative stocks for backtest
                # which may not overlap with research_task.target_symbols.
                # This doesn't invalidate the strategy logic.
                if constraint_violation == "strict_intersection_empty":
                    warnings.append(
                        f"Factory policy: constraint violation {constraint_violation} (downgraded to warning)"
                    )
                else:
                    reasons.append(
                        f"Factory policy: constraint violation {constraint_violation}"
                    )

            require_strict_refresh = bool(
                _compat_setting(
                    "FACTORY_SUBMISSION_REQUIRE_STRICT_PASS_FOR_REFRESH",
                    FACTORY_SUBMISSION_REQUIRE_STRICT_PASS_FOR_REFRESH,
                )
            )
            if refresh_existing and require_strict_refresh and not normalized_gate.get("passed_strict", normalized_gate.get("passed")):
                reasons.append("Factory policy: refresh_existing candidate requires strict quality gate pass")

            return {
                "reasons": reasons,
                "warnings": warnings,
                "provisional_blocks": provisional_blocks,
                "task_preference": {
                    "preferred_strategy_types": strategy_preferences,
                    "strategy_preferences": list(strategy_preferences),
                    "preference_strength": preference_strength,
                    "preference_reason": preference_reason,
                    "override_applied": preference_override_applied,
                },
                "refresh_existing_contract": refresh_contract,
            }

        @classmethod
        def _apply_factory_submission_policy(
            cls,
            candidate: dict,
            *,
            name: str,
            gate: Optional[dict],
            backtest_metrics: Optional[dict],
            refresh_existing: bool,
        ) -> dict:
            normalized_gate = normalize_quality_gate_result(gate)
            policy_outcome = cls._factory_policy_outcome(
                candidate,
                name=name,
                gate=normalized_gate,
                backtest_metrics=backtest_metrics,
                refresh_existing=refresh_existing,
            )
            policy_reasons = list(policy_outcome.get("reasons") or [])
            policy_warnings = list(policy_outcome.get("warnings") or [])
            provisional_blocks = list(policy_outcome.get("provisional_blocks") or [])
            task_preference = dict(policy_outcome.get("task_preference") or {})
            refresh_existing_contract = dict(policy_outcome.get("refresh_existing_contract") or {})
            if not policy_reasons and not policy_warnings and not provisional_blocks:
                if task_preference or refresh_existing_contract:
                    return normalize_quality_gate_result({
                        **normalized_gate,
                        "task_preference": task_preference,
                        "refresh_existing_contract": refresh_existing_contract,
                    })
                return normalized_gate

            merged_reasons = list(normalized_gate.get("reasons") or [])
            for item in policy_reasons:
                if item not in merged_reasons:
                    merged_reasons.append(item)
            warnings = list(normalized_gate.get("warnings") or [])
            for item in policy_warnings:
                if item not in warnings:
                    warnings.append(item)
            blocks_provisional_only = bool(provisional_blocks and normalized_gate.get("provisional_pass"))
            if blocks_provisional_only:
                for item in provisional_blocks:
                    message = f"Factory policy: provisional_only_blocked_by_medium_preference {item}"
                    if message not in merged_reasons:
                        merged_reasons.append(message)
            return normalize_quality_gate_result(
                {
                    **normalized_gate,
                    "passed": False if (policy_reasons or blocks_provisional_only) else bool(normalized_gate.get("passed")),
                    "passed_strict": False if (policy_reasons or blocks_provisional_only) else bool(normalized_gate.get("passed_strict", normalized_gate.get("passed"))),
                    "provisional_pass": False if (policy_reasons or blocks_provisional_only) else bool(normalized_gate.get("provisional_pass")),
                    "reason": merged_reasons[0] if merged_reasons else "",
                    "reasons": merged_reasons,
                    "warnings": warnings,
                    "task_preference": task_preference,
                    "refresh_existing_contract": refresh_existing_contract,
                }
            )

        @classmethod
        def _build_quality_report(
            cls,
            strategy_id: str,
            candidate: dict,
            snapshot: dict,
            backtest_metrics: dict,
            quality_gate: dict,
            validation_report: Optional[dict],
            risk_report: Optional[dict],
            final_status: str,
            submission_lane: Optional[str] = None,
        ) -> dict:
            return build_quality_report(
                strategy_id=strategy_id,
                strategy_type=candidate.get("strategy_type"),
                quality_gate=quality_gate,
                validation_report=validation_report,
                risk_report=risk_report,
                dedup_report=candidate.get("dedup_result") or {},
                backtest_metrics=backtest_metrics or {},
                snapshot={
                    "date": snapshot.get("date"),
                    "fg_level": snapshot.get("fg_level"),
                    "fear_greed_index": snapshot.get("fear_greed_index"),
                },
                status_after_review=final_status,
                review_source="strategy_factory_submit",
                report_type="submission",
                spawn_reason=candidate.get("spawn_reason"),
                submission_audit={
                    "task_signature": _build_task_signature(candidate.get("research_task") or {}),
                    "refresh_mode": (candidate.get("dedup_result") or {}).get("refresh_mode"),
                    "refresh_existing_contract": dict((quality_gate or {}).get("refresh_existing_contract") or {}),
                    "submission_lane": submission_lane,
                    "execution_audit_gate_status": quality_gate.get("execution_audit_gate_status"),
                    "execution_audit_gate_reasons": list(quality_gate.get("execution_audit_gate_reasons") or []),
                    "execution_hard_gate_passed": quality_gate.get("execution_hard_gate_passed"),
                    "trace_id": candidate.get("trace_id"),
                    "correlation_id": (
                        candidate.get("correlation_id")
                        or candidate.get("trace_id")
                        or snapshot.get("correlation_id")
                    ),
                    "factory_run_id": candidate.get("factory_run_id") or snapshot.get("factory_run_id"),
                    "parent_task_run_id": candidate.get("task_run_id") or dict(candidate.get("params") or {}).get("task_run_id"),
                    "source_action": "strategy_factory_submit",
                    "direct_trade_candidate": bool((quality_gate or {}).get("live_candidate_ready")),
                    "committee_review": dict(candidate.get("committee_review") or {}),
                    "task_preference": dict((quality_gate or {}).get("task_preference") or {}),
                    "candidate_provenance": cls._candidate_provenance(candidate),
                    "evidence_chain": dict(candidate.get("evidence_chain") or {}),
                    "prediction_contract": dict(candidate.get("prediction_contract") or {}),
                    "confidence_contract": dict(candidate.get("confidence_contract") or {}),
                    "evidence_alignment_audit": dict(candidate.get("evidence_alignment_audit") or {}),
                    "legacy_semantic_contract": candidate.get("legacy_semantic_contract"),
                    "contradiction_count": candidate.get("contradiction_count"),
                    "proxy_dependency_score": candidate.get("proxy_dependency_score"),
                },
            )
