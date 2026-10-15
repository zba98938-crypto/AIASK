"""策略工厂候选提交。"""


from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import logging
import re
import time as _time
from collections import Counter
from typing import TYPE_CHECKING, Any, List, Optional
from uuid import uuid4

from aiask_quant_core.strategy_explanation import build_strategy_explanation, render_strategy_description

from ..candidate_contract import (
    apply_resolved_candidate_envelope,
    build_candidate_contract_hash,
    build_candidate_identity_signature,
    build_execution_contract_hash,
    build_portfolio_candidate_contract,
    build_resolved_candidate_envelope,
    build_tested_object_hash,
    candidate_contract_value,
)
from ..incubation_budgeter import IncubationBudgeter
from ..compact_contracts import compact_event_window_metrics, compact_json, compact_scalar_metrics
from ..quality_gates import _VALID_STRATEGY_TYPES, build_completed_gate_3_report
from ..quality_reporting import build_quality_report, normalize_quality_gate_result
from ..semantic_contract import (
    audit_candidate_semantic_contract,
    build_candidate_evidence_records,
    ensure_candidate_semantic_contract,
    normalize_semantic_contract_fields,
    synthesize_confidence_contract,
)
from ..submission_gate import run_submission_quality_gate as _local_run_submission_quality_gate
from ..trade_prediction_promotion_gate import (
    evaluate_trade_prediction_promotion_gate,
)
from ..utils import (
    _auto_name as _local_auto_name,
    _extract_event_context as _local_extract_event_context,
    _update_strategy_status as _local_update_strategy_status,
    get_strategy_factory_package as _local_get_strategy_factory_package,
)
from ..research_protocol_contract import (
    adapt_research_validation_contract_for_submission,
    normalize_prediction_trace_id,
)
from ..services.submission_coordinator import SubmissionExecutionOptions
from ..services.admission_authority import SubmissionAdmissionAuthority
from ..services.readiness_service import resolve_market_temperature_context
from .._runtime_toggles import (
    diagnostic_observation_batch_limit as _diagnostic_observation_batch_limit,
    diagnostic_observation_dedupe_enabled as _diagnostic_observation_dedupe_enabled,
    diagnostic_observation_enabled as _diagnostic_observation_enabled,
    diagnostic_observation_final_status as _diagnostic_observation_final_status,
    diagnostic_observation_health_guard_enabled as _diagnostic_observation_health_guard_enabled,
    diagnostic_observation_health_max_age_hours as _diagnostic_observation_health_max_age_hours,
    diagnostic_observation_min_trade_count as _diagnostic_observation_min_trade_count,
    diagnostic_observation_min_win_rate as _diagnostic_observation_min_win_rate,
    diagnostic_observation_ttl_days as _diagnostic_observation_ttl_days,
    observe_first_enabled as _observe_first_enabled,
    observe_d_grade_enabled as _observe_d_grade_enabled,
    strategy_factory_gate3_record_only_enabled as _gate3_record_only_enabled,
    strategy_factory_min_validation_grade as _strategy_factory_min_validation_grade,
    validation_grade_at_least as _validation_grade_at_least,
    wide_intake_observe_enabled as _wide_intake_observe_enabled,
)
from ...domain.constants import (
    FACTORY_SUBMISSION_MIN_BACKTEST_TRADES,
    FACTORY_SUBMISSION_MIN_EVENT_TARGET_COVERAGE,
    FACTORY_SUBMISSION_REJECT_GENERIC_AI_NAMES,
    FACTORY_SUBMISSION_REQUIRE_STRICT_PASS_FOR_REFRESH,
    FACTORY_SUBMISSION_REQUIRE_TASK_PREFERENCE_MATCH,
    STRATEGY_FACTORY_SPEC_COMPLETENESS_MODE,
    STRATEGY_FACTORY_EVIDENCE_CONTRACT_ENABLED,
    SUBMIT_CONCURRENCY,
)
from ...domain.targets import _build_task_signature, _normalize_research_task_contract, _normalize_target_codes
from ...infrastructure.mcp_services import (
    build_strategy_vector_profile,
    get_strategy_promotion_pipeline_service,
    get_strategy_runtime_control_service,
)

if TYPE_CHECKING:
    from ...api.contracts import IncubationGateway, RiskGateway, ValidationGateway

logger = logging.getLogger(__name__)

from ._runner_helpers import (
    _build_gate3_quality_record_contract,
    _build_gate_a_spec_override,
    _candidate_artifact_ids,
    _candidate_family_outcome_summary,
    _candidate_hard_failures,
    _candidate_retrieval_context_ids,
    _candidate_trace_ids,
    _compact_unique,
    _compat_setting,
    _enrich_quality_report_v2,
    _gate_a_payload,
    _gate_a_revision_actions,
    _gate_b_payload,
    _gate_c_payload,
    _normalized_spec_completeness_mode,
    _quality_summary_validation_grade,
    _spec_completeness_mode,
)


def _auto_name(*args, **kwargs):
    return _local_auto_name(*args, **kwargs)


def _extract_event_context(*args, **kwargs):
    return _local_extract_event_context(*args, **kwargs)


def get_strategy_factory_package():
    return _local_get_strategy_factory_package()


async def _update_strategy_status(*args, **kwargs):
    return await _local_update_strategy_status(*args, **kwargs)


_SEMANTIC_CONTRACT_FIELDS = (
    "market_evidence_pack",
    "alpha_thesis",
    "template_dominance_score",
    "non_proxy_evidence_ratio",
    "direction_resolution",
    "confidence_calibration",
    "evidence_chain",
    "prediction_contract",
    "confidence_contract",
    "evidence_alignment_audit",
    "dsl_support_audit",
    "legacy_semantic_contract",
    "contradiction_count",
    "proxy_dependency_score",
)

_RUNTIME_BOOTSTRAP_REQUIRED_FIELDS = (
    "holding_horizon",
    "trade_plan",
    "risk_rules",
    "execution_assumptions",
)
_EMPTY_CONTRACT_VALUES = (None, "", [], {})


def _safe_float(value: Any, default: Optional[float] = None) -> Optional[float]:
    try:
        if value in (None, "", [], {}):
            return default
        return float(value)
    except Exception:
        return default


def _safe_int(value: Any, default: Optional[int] = None) -> Optional[int]:
    try:
        if value in (None, "", [], {}):
            return default
        return int(float(value))
    except Exception:
        return default


def _diagnostic_reason_codes(gate: Optional[dict[str, Any]]) -> list[str]:
    payload = dict(gate or {})
    values: list[Any] = []
    for key in (
        "reason_codes",
        "reasons",
        "admission_block_reasons",
        "warning_codes",
        "warnings",
    ):
        raw = payload.get(key)
        if isinstance(raw, (list, tuple, set)):
            values.extend(raw)
        elif raw not in (None, "", [], {}):
            values.append(raw)
    return _compact_unique([str(item or "").strip().lower() for item in values], limit=32)


def _diagnostic_metric(
    candidate: dict[str, Any],
    metrics: Optional[dict[str, Any]],
    gate: Optional[dict[str, Any]],
    *keys: str,
) -> Any:
    sources = [
        dict(metrics or {}),
        dict(candidate.get("backtest_metrics") or {}),
        dict((candidate.get("backtest_result") or {}).get("metrics") or {}),
        dict(gate or {}),
    ]
    for source in sources:
        for key in keys:
            if source.get(key) not in (None, "", [], {}):
                return source.get(key)
    return None


def _parse_win_rate_from_reason(reason: str) -> Optional[float]:
    token = str(reason or "").strip().lower()
    if not token.startswith("win_rate_"):
        return None
    nums = re.findall(r"\d+", token)
    if len(nums) < 2:
        return None
    try:
        return float(f"{int(nums[0])}.{nums[1]}")
    except Exception as _swallowed:
        logger.warning("packages/strategy-factory/src/strategy_factory/application/_submitter_actions/runner.py:224: swallowed %s and continued: %s", type(_swallowed).__name__, _swallowed)
        return None


def _diagnostic_observation_target_symbols(candidate: dict[str, Any]) -> list[str]:
    payload = dict(candidate or {})
    params = dict(payload.get("params") or {})
    return _normalize_target_codes(
        [
            payload.get("target_symbols"),
            payload.get("stock_pool"),
            params.get("target_symbols"),
            params.get("stock_pool"),
            (params.get("dsl") or {}).get("metadata") if isinstance(params.get("dsl"), dict) else None,
        ],
        limit=16,
    )


_DIAGNOSTIC_FINGERPRINT_VOLATILE_KEYS = {
    "id",
    "strategy_id",
    "trace_id",
    "prediction_trace_id",
    "correlation_id",
    "task_run_id",
    "factory_run_id",
    "experiment_id",
    "source_run_id",
    "created_at",
    "updated_at",
    "generated_at",
    "timestamp",
    "source_candidate_artifact_id",
    "source_generation_artifact_id",
    "source_validation_artifact_id",
    "candidate_memory_record_id",
    "multiple_testing_registry_record_id",
}


def _diagnostic_observation_stable_value(value: Any) -> Any:
    if isinstance(value, dict):
        stable: dict[str, Any] = {}
        for raw_key, raw_value in value.items():
            key = str(raw_key or "").strip()
            if not key or key in _DIAGNOSTIC_FINGERPRINT_VOLATILE_KEYS:
                continue
            cleaned = _diagnostic_observation_stable_value(raw_value)
            if cleaned in (None, "", [], {}):
                continue
            stable[key] = cleaned
        return stable
    if isinstance(value, (list, tuple)):
        cleaned_items = [
            _diagnostic_observation_stable_value(item)
            for item in list(value)
        ]
        return [item for item in cleaned_items if item not in (None, "", [], {})]
    return value


def _diagnostic_observation_core_params(candidate: dict[str, Any]) -> dict[str, Any]:
    payload = dict(candidate or {})
    params = dict(payload.get("params") or {})
    core_keys = (
        "fast_window",
        "slow_window",
        "short_window",
        "long_window",
        "lookback",
        "lookback_days",
        "period",
        "period_days",
        "rebalance_days",
        "holding_days",
        "holding_period",
        "threshold",
        "entry_threshold",
        "exit_threshold",
        "factor_name",
        "factor_names",
        "factor_weights",
        "signal_rule",
        "dsl",
    )
    compact: dict[str, Any] = {}
    for key in core_keys:
        value = params.get(key, payload.get(key))
        if value in (None, "", [], {}):
            continue
        stable_value = _diagnostic_observation_stable_value(value)
        if stable_value not in (None, "", [], {}):
            compact[key] = stable_value
    for key in ("logic_signature", "dsl_signature", "factor_signature", "entry_exit_signature"):
        value = payload.get(key) or params.get(key)
        if value not in (None, "", [], {}):
            compact[key] = value
    return compact


def _diagnostic_observation_fingerprint(candidate: dict[str, Any], reason: str) -> str:
    payload = dict(candidate or {})
    params = dict(payload.get("params") or {})
    provenance = dict(payload.get("candidate_provenance") or params.get("candidate_provenance") or {})
    fingerprint_payload = {
        "strategy_type": str(payload.get("strategy_type") or "").strip().lower(),
        "candidate_family": str(
            provenance.get("candidate_family")
            or payload.get("candidate_family")
            or params.get("candidate_family")
            or ""
        ).strip().lower(),
        "target_symbols": _diagnostic_observation_target_symbols(payload),
        "core_params": _diagnostic_observation_core_params(payload),
        "reason": str(reason or "diagnostic_observation").strip().lower(),
    }
    raw = json.dumps(fingerprint_payload, sort_keys=True, ensure_ascii=False, default=str)
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]
    return f"diag_{digest}"


def _has_gate_2_full_backtest_evidence(candidate: dict[str, Any]) -> bool:
    payload = dict(candidate or {})
    for key in (
        "gate_2_passed",
        "gate2_passed",
        "full_backtest_passed",
        "passed_full_backtest",
        "backtest_passed",
    ):
        if payload.get(key) is True:
            return True

    for key in ("backtest_outcome", "backtest_result", "raw_backtest_result"):
        outcome = dict(payload.get(key) or {})
        if outcome.get("passed") is True:
            return True
        if str(outcome.get("reason_code") or "").strip().lower() == "passed":
            return True

    metrics = dict(payload.get("backtest_metrics") or {})
    contract = dict(
        payload.get("backtest_metrics_contract")
        or metrics.get("backtest_metrics_contract")
        or {}
    )
    for source in (metrics, contract):
        for key in (
            "gate_2_passed",
            "gate2_passed",
            "full_backtest_passed",
            "passed_full_backtest",
            "backtest_passed",
        ):
            if source.get(key) is True:
                return True
    return False


def _is_diagnostic_observation_candidate(
    gate: Optional[dict[str, Any]],
    candidate: Optional[dict[str, Any]],
    metrics: Optional[dict[str, Any]],
    *,
    refresh_existing: bool,
    read_only: bool,
) -> tuple[bool, Optional[str]]:
    gate_payload = dict(gate or {})
    candidate_payload = dict(candidate or {})
    if bool(gate_payload.get("passed")):
        return False, None
    if refresh_existing or read_only:
        return False, None
    dedup_result = dict(candidate_payload.get("dedup_result") or {})
    if not dedup_result:
        return False, None
    if bool(dedup_result.get("duplicate")) or bool(dedup_result.get("refresh_existing")):
        return False, None
    if not _has_gate_2_full_backtest_evidence(candidate_payload):
        return False, None
    gate_a_decision = str(
        gate_payload.get("gate_a_decision") or candidate_payload.get("gate_a_decision") or ""
    ).strip().lower()
    if gate_a_decision in {"reject", "revise"}:
        return False, None

    reason_codes = _diagnostic_reason_codes(gate_payload)
    hard_tokens = {
        "not_executable",
        "non_executable",
        "runtime_contract_missing",
        "missing_runtime_contract",
        "missing_runtime",
        "semantic_hard_fail",
        "data_missing",
        "missing_data",
        "insufficient_data",
        "max_drawdown_hard",
        "max_drawdown_exceeded",
        "multiple_testing_high_risk",
        "overfitting_high_risk",
        "overfit_high_risk",
        "precompile_reject",
        "generator_hard_reject",
    }
    if any(any(token in reason for token in hard_tokens) for reason in reason_codes):
        return False, None

    for raw in list(candidate_payload.get("hard_failures") or []):
        payload = dict(raw or {})
        decision = str(payload.get("decision") or payload.get("severity") or "").strip().lower()
        if decision in {"reject", "hard_fail", "error"}:
            return False, None
    for raw in list(candidate_payload.get("completion_issues") or []):
        payload = dict(raw or {})
        decision = str(payload.get("decision") or payload.get("severity") or "").strip().lower()
        if decision == "reject":
            return False, None

    trade_count = _safe_int(
        _diagnostic_metric(candidate_payload, metrics, gate_payload, "trade_count", "trades_count", "total_trades"),
        default=0,
    ) or 0
    min_trade_count = _diagnostic_observation_min_trade_count()
    if trade_count < min_trade_count:
        return False, None
    max_drawdown = abs(
        _safe_float(
            _diagnostic_metric(candidate_payload, metrics, gate_payload, "max_drawdown", "drawdown"),
            default=0.0,
        )
        or 0.0
    )
    if max_drawdown > 0.35:
        return False, None

    win_rate = _safe_float(
        _diagnostic_metric(candidate_payload, metrics, gate_payload, "win_rate", "avg_win_rate"),
        default=None,
    )
    parsed_win_rates = [_parse_win_rate_from_reason(reason) for reason in reason_codes]
    parsed_win_rates = [value for value in parsed_win_rates if value is not None]
    if win_rate is None and parsed_win_rates:
        win_rate = parsed_win_rates[0]
    min_win_rate = _diagnostic_observation_min_win_rate()
    if win_rate is not None and min_win_rate <= float(win_rate) < 0.40:
        return True, next((r for r in reason_codes if r.startswith("win_rate_")), "win_rate_near_threshold")

    allowed_fragments = (
        "weak_wf_ic_ir",
        "weak_pkf_ic",
        "weak_bootstrap_ci_lower",
    )
    for reason in reason_codes:
        if any(fragment in reason for fragment in allowed_fragments):
            return True, reason
        if reason.startswith("period_robustness_"):
            return True, reason
        if (reason.startswith("trade_count_") or "trade_count" in reason) and trade_count >= min_trade_count:
            return True, reason
    return False, None


def _diagnostic_observation_submission_action(
    base_action: Optional[dict[str, Any]],
    *,
    reason: str,
) -> dict[str, Any]:
    ttl_days = _diagnostic_observation_ttl_days()
    final_status = _diagnostic_observation_final_status()
    trigger_reason = "observe_first_intake" if _observe_first_enabled() else "diagnostic_observation_gate3_failed"
    action = dict(base_action or {})
    nested = dict(action.get("submission_action") or {})
    nested.update(
        {
            "type": "diagnostic",
            "trigger_reason": trigger_reason,
            "next_step": "diagnostic_observation",
            "submission_lane": "diagnostic_observation",
            "final_status": final_status,
            "completed": False,
            "diagnostic_observation": True,
            "diagnostic_reason": reason,
            "diagnostic_ttl_days": ttl_days,
            "admission_layer": "diagnostic",
        }
    )
    action.update(
        {
            "submission_action": nested,
            "submission_action_type": "diagnostic",
            "submission_action_trigger": trigger_reason,
            "submission_action_next_step": "diagnostic_observation",
            "submission_action_completed": False,
            "submission_lane": "diagnostic_observation",
            "final_status": final_status,
            "admission_decision": "diagnostic",
            "admission_layer": "diagnostic",
            "diagnostic_observation": True,
            "diagnostic_reason": reason,
            "diagnostic_reason_code": reason,
            "diagnostic_ttl_days": ttl_days,
        }
    )
    return action


def _semantic_contract_feature_enabled() -> bool:
    return bool(STRATEGY_FACTORY_EVIDENCE_CONTRACT_ENABLED)


def _assign_optional_payload(target: dict[str, Any], key: str, value: Any) -> None:
    if value not in (None, "", [], {}):
        target[key] = value


def _apply_candidate_semantic_contract(candidate: dict[str, Any], semantic_audit: Optional[dict[str, Any]]) -> dict[str, Any]:
    payload = dict(candidate or {})
    semantic_fields = normalize_semantic_contract_fields(payload)
    if semantic_audit is not None:
        semantic_fields["evidence_alignment_audit"] = dict(semantic_audit or {})
        semantic_fields["legacy_semantic_contract"] = bool(semantic_audit.get("legacy_semantic_contract"))
        semantic_fields["contradiction_count"] = int(semantic_audit.get("contradiction_count") or 0)
        semantic_fields["proxy_dependency_score"] = semantic_audit.get("proxy_dependency_score")
    params = dict(payload.get("params") or {})
    for field_name in _SEMANTIC_CONTRACT_FIELDS:
        _assign_optional_payload(params, field_name, semantic_fields.get(field_name))
    payload["params"] = params
    for field_name in _SEMANTIC_CONTRACT_FIELDS:
        _assign_optional_payload(payload, field_name, semantic_fields.get(field_name))
    return payload


def _apply_semantic_contract_gate(gate: Optional[dict], semantic_audit: Optional[dict[str, Any]]) -> dict[str, Any]:
    normalized_gate = normalize_quality_gate_result(gate)
    audit = dict(semantic_audit or {})
    if not audit:
        return normalized_gate
    for field_name in (
        "evidence_alignment_audit",
        "legacy_semantic_contract",
        "contradiction_count",
        "proxy_dependency_score",
    ):
        value = (
            audit
            if field_name == "evidence_alignment_audit"
            else audit.get(field_name)
        )
        _assign_optional_payload(normalized_gate, field_name, value)
    hard_fail_reasons = [
        str(reason or "").strip()
        for reason in list(audit.get("hard_fail_reasons") or [])
        if str(reason or "").strip()
    ]
    if hard_fail_reasons and bool(audit.get("using_new_contract")):
        existing_reasons = [
            str(reason or "").strip()
            for reason in list(normalized_gate.get("reasons") or [])
            if str(reason or "").strip()
        ]
        normalized_gate["passed"] = False
        normalized_gate["reasons"] = list(dict.fromkeys([*existing_reasons, *hard_fail_reasons]))
    return normalize_quality_gate_result(normalized_gate)


class _CompatValidationGateway:
    """Resolve validation runner through the legacy patch-point at call time."""

    async def run_validation_report(self, strategy_type: str, params: dict[str, Any], db) -> Optional[dict]:
        from ...api.quality_reporting import run_validation_report

        return await run_validation_report(strategy_type, dict(params or {}), db)


class _CompatRiskGateway:
    """Resolve risk runner through the legacy patch-point at call time."""

    async def run_risk_report(self, strategy_type: str, params: dict[str, Any], db) -> Optional[dict]:
        from ...api.quality_reporting import run_risk_report

        return await run_risk_report(strategy_type, dict(params or {}), db)


class _StrategySubmitterActionsMixin:
# --- runner_parts/gate_payloads.py ---
        async def submit(
            self,
            candidates: List[dict],
            snapshot: dict,
            db,
            *,
            read_only: bool = False,
        ) -> dict:
            """批量提交候选策略，每个策略独立处理，单个失败不影响其他。"""
            created = 0
            created_total = 0
            created_strategy_pool = 0
            created_audit_only = 0
            refreshed = 0
            submitted = 0
            gate_3_input = 0
            passed = 0
            gate_3_passed = 0
            gate_3_failed = 0
            gate_3_provisional_passed = 0
            gate_3_recorded = 0
            gate3_quality_recorded = 0
            gate3_record_quality_qualified_count = 0
            gate3_record_diagnostic_only_count = 0
            gate_3_failure_codes: Counter[str] = Counter()
            submission_lane_counts: Counter[str] = Counter()
            submission_action_type_counts: Counter[str] = Counter()
            admission_decision_counts: Counter[str] = Counter()
            strict_incubation_ready_count = 0
            factor_performance_reported_count = 0
            pre_observe_hard_reject_count = 0
            submitted_items: List[dict] = []
            self._diagnostic_observation_lock = asyncio.Lock()
            self._diagnostic_observation_claimed = 0
            self._diagnostic_observation_limit = _diagnostic_observation_batch_limit()
            self._diagnostic_observation_fingerprints = set()
            incubation_budget_plan = IncubationBudgeter.plan(candidates, snapshot)
            incubation_budget_summary = dict(incubation_budget_plan.get("summary") or {})
            for candidate in candidates:
                marker = int(id(candidate))
                existing_budget = dict(candidate.get("incubation_budget") or {})
                planned_budget = dict((incubation_budget_plan.get("plans") or {}).get(marker) or {})
                merged_budget = {**planned_budget, **existing_budget}
                planned_track = str(planned_budget.get("track") or "").strip().lower()
                if planned_track in {
                    "formal_incubation",
                    "observe_incubation",
                    "deferred_budget_queue",
                    "deferred_submission",
                }:
                    merged_budget["track"] = planned_track
                observe_first_requested = bool(candidate.get("observe_first_intake")) or bool(
                    dict(candidate.get("params") or {}).get("observe_first_intake")
                )
                if observe_first_requested:
                    # Observe-first candidates still carry a micro observe placeholder from the
                    # candidate pipeline, but the budget planner may explicitly reserve a formal
                    # slot for a strict-ready sample. Preserve that formal plan here so the
                    # downstream admission authority can actually request formal incubation. The
                    # planner can also defer a feedback-controlled candidate; do not let the
                    # observe-first placeholder re-open that lane.
                    if planned_track == "formal_incubation":
                        merged_budget["track"] = "formal_incubation"
                        if planned_budget.get("budget_tier") not in (None, "", [], {}):
                            merged_budget["budget_tier"] = planned_budget.get("budget_tier")
                    elif planned_track in {"deferred_budget_queue", "deferred_submission"}:
                        merged_budget["track"] = planned_track
                    else:
                        merged_budget["track"] = "observe_incubation"
                        merged_budget.setdefault("budget_tier", "micro")
                    merged_budget["observe_first_intake"] = True
                candidate["incubation_budget"] = merged_budget
            submit_concurrency = int(_compat_setting("SUBMIT_CONCURRENCY", SUBMIT_CONCURRENCY) or SUBMIT_CONCURRENCY)
            sem = asyncio.Semaphore(submit_concurrency)

            async def _submit_guarded(candidate: dict) -> Optional[dict]:
                async with sem:
                    try:
                        return await self._submit_one(
                            candidate,
                            snapshot,
                            db,
                            read_only=read_only,
                        )
                    except Exception as exc:
                        logger.warning("StrategySubmitter: failed for %s: %s", candidate.get("strategy_type"), exc)
                        return None

            results = await asyncio.gather(
                *[_submit_guarded(c) for c in candidates],
                return_exceptions=True,
            )
            for result in results:
                if result is None or isinstance(result, BaseException):
                    continue
                gate_3_input += 1
                if result.get("created_total", result.get("created", False)):
                    created_total += 1
                if result.get("created_strategy_pool", result.get("created", False)):
                    created += 1
                    created_strategy_pool += 1
                if result.get("created_audit_only"):
                    created_audit_only += 1
                if result.get("refreshed_existing"):
                    refreshed += 1
                if result.get("submitted"):
                    submitted += 1
                if result.get("gate_3_recorded"):
                    gate_3_recorded += 1
                if result.get("gate3_quality_recorded") or result.get("gate3_record_quality_qualified"):
                    gate3_quality_recorded += 1
                    gate3_record_quality_qualified_count += 1
                if result.get("gate3_record_diagnostic_only"):
                    gate3_record_diagnostic_only_count += 1
                if result.get("passed"):
                    passed += 1
                gate_3 = dict(result.get("gate_3") or {})
                gate_a_decision = str(gate_3.get("gate_a_decision") or "").strip().lower()
                gate_a_short_circuit = gate_a_decision in {"revise", "reject"} and str(
                    gate_3.get("admission_stage") or ""
                ).strip().lower() == "gate_a"
                if gate_3.get("passed"):
                    gate_3_passed += 1
                    if gate_3.get("provisional_pass"):
                        gate_3_provisional_passed += 1
                elif not gate_a_short_circuit:
                    gate_3_failed += 1
                    for code in gate_3.get("reason_codes") or []:
                        normalized = str(code or "").strip()
                        if normalized:
                            gate_3_failure_codes[normalized] += 1
                summary = dict(result["summary"] or {})
                submission_lane = str(summary.get("submission_lane") or "").strip().lower()
                if submission_lane:
                    submission_lane_counts[submission_lane] += 1
                action_type = str(summary.get("submission_action_type") or "").strip().lower()
                if action_type:
                    submission_action_type_counts[action_type] += 1
                admission_decision = str(summary.get("admission_decision") or "").strip().lower()
                if admission_decision:
                    admission_decision_counts[admission_decision] += 1
                if bool(summary.get("strict_incubation_ready")):
                    strict_incubation_ready_count += 1
                if bool(summary.get("factor_performance_reported")):
                    factor_performance_reported_count += 1
                if list(summary.get("pre_observe_hard_reject_reasons") or []):
                    pre_observe_hard_reject_count += 1
                submitted_items.append(summary)

            planned_track_counts = dict(incubation_budget_summary.get("track_counts") or {})
            effective_track_counts = {
                "formal_incubation": int(submission_lane_counts.get("formal_incubation") or 0),
                "observe_incubation": int(submission_lane_counts.get("observe_incubation") or 0)
                + int(submission_lane_counts.get("diagnostic_observation") or 0),
                "deferred_budget_queue": int(submission_lane_counts.get("deferred_submission") or 0),
            }
            concrete_final_track_total = sum(int(value or 0) for value in effective_track_counts.values())
            final_lane_counts = dict(submission_lane_counts)
            if final_lane_counts:
                reconciled_budget_summary = {
                    **incubation_budget_summary,
                    "planned_track_counts": planned_track_counts,
                    "final_lane_counts": final_lane_counts,
                    "effective_track_counts": effective_track_counts,
                    "track_counts_reconciled": bool(concrete_final_track_total > 0),
                    "auto_promoted_formal_count": max(
                        0,
                        int(effective_track_counts.get("formal_incubation") or 0)
                        - int(planned_track_counts.get("formal_incubation") or 0),
                    ),
                    "auto_promoted_observe_count": max(
                        0,
                        int(effective_track_counts.get("observe_incubation") or 0)
                        - int(planned_track_counts.get("observe_incubation") or 0),
                    ),
                }
                if concrete_final_track_total > 0:
                    reconciled_budget_summary["track_counts"] = effective_track_counts
                incubation_budget_summary = reconciled_budget_summary

            observe_admitted_count = int(submission_lane_counts.get("observe_incubation") or 0) + int(
                submission_lane_counts.get("diagnostic_observation") or 0
            )
            gate3_audit_failed_count = gate_3_failed
            gate_report = build_completed_gate_3_report(
                {
                    "gate_3_input": gate_3_input,
                    "submitted": submitted,
                    "gate_3_passed": gate_3_passed,
                    "gate_3_failed": gate_3_failed,
                    "gate_3_recorded": gate_3_recorded,
                    "gate3_quality_recorded": gate3_quality_recorded,
                    "gate3_record_quality_qualified_count": gate3_record_quality_qualified_count,
                    "gate3_record_diagnostic_only_count": gate3_record_diagnostic_only_count,
                    "gate3_audit_failed_count": gate3_audit_failed_count,
                    "gate_3_provisional_passed": gate_3_provisional_passed,
                    "gate_3_failure_reason_topn": [
                        {"reason_code": reason_code, "count": count}
                        for reason_code, count in gate_3_failure_codes.most_common(5)
                    ],
                    "observe_admitted_count": observe_admitted_count,
                    "pre_observe_hard_reject_count": pre_observe_hard_reject_count,
                    "formal_incubation_count": int(submission_lane_counts.get("formal_incubation") or 0),
                    "observe_incubation_count": int(submission_lane_counts.get("observe_incubation") or 0),
                    "diagnostic_observation_count": int(submission_lane_counts.get("diagnostic_observation") or 0),
                    "live_ready_review_count": int(submission_lane_counts.get("live_ready_review") or 0),
                    "deferred_submission_count": int(submission_lane_counts.get("deferred_submission") or 0),
                    "research_only_count": int(submission_action_type_counts.get("research_only") or 0),
                    "strict_incubation_ready_count": strict_incubation_ready_count,
                }
            )

            # PR-S3: 排空持久化 DLQ，附在 submit_result 中供 cycle runner 摘要暴露
            persistence_dlq = []
            drain = getattr(self, "drain_persistence_dlq", None)
            if callable(drain):
                persistence_dlq = drain()
            return {
                "created": created,
                "created_total": created_total,
                "created_strategy_pool": created_strategy_pool,
                "created_audit_only": created_audit_only,
                "refreshed": refreshed,
                "gate_3_input": gate_3_input,
                "submitted": submitted,
                "passed_quality_gate": passed,
                "gate_3_passed": gate_3_passed,
                "gate_3_failed": gate_3_failed,
                "gate_3_recorded": gate_3_recorded,
                "gate3_quality_recorded": gate3_quality_recorded,
                "gate3_record_quality_qualified_count": gate3_record_quality_qualified_count,
                "gate3_record_diagnostic_only_count": gate3_record_diagnostic_only_count,
                "gate3_audit_failed_count": gate3_audit_failed_count,
                "gate_3_provisional_passed": gate_3_provisional_passed,
                "gate_3_failure_reason_topn": gate_report["gate_3"]["failure_reason_topn"],
                "observe_admitted_count": observe_admitted_count,
                "pre_observe_hard_reject_count": pre_observe_hard_reject_count,
                "formal_incubation_count": int(submission_lane_counts.get("formal_incubation") or 0),
                "observe_incubation_count": int(submission_lane_counts.get("observe_incubation") or 0),
                "diagnostic_observation_count": int(submission_lane_counts.get("diagnostic_observation") or 0),
                "live_ready_review_count": int(submission_lane_counts.get("live_ready_review") or 0),
                "deferred_submission_count": int(submission_lane_counts.get("deferred_submission") or 0),
                "research_only_count": int(submission_action_type_counts.get("research_only") or 0),
                "strict_incubation_ready_count": strict_incubation_ready_count,
                "factor_performance_reported_count": factor_performance_reported_count,
                "quality_gate": gate_report,
                "gate_report": gate_report,
                "incubation_budget_summary": incubation_budget_summary,
                "admission_decision_counts": dict(admission_decision_counts),
                "strategies": submitted_items,
                "persistence_dlq_count": len(persistence_dlq),
                "persistence_dlq": persistence_dlq,
            }

        async def _claim_diagnostic_observation_slot(self, *, fingerprint: str | None = None) -> bool:
            if not _diagnostic_observation_enabled():
                return False
            lock = getattr(self, "_diagnostic_observation_lock", None)
            if lock is None:
                lock = asyncio.Lock()
                self._diagnostic_observation_lock = lock
            async with lock:
                limit = int(
                    getattr(
                        self,
                        "_diagnostic_observation_limit",
                        _diagnostic_observation_batch_limit(),
                    )
                    or 0
                )
                token = str(fingerprint or "").strip()
                claimed_fingerprints = getattr(self, "_diagnostic_observation_fingerprints", None)
                if claimed_fingerprints is None:
                    claimed_fingerprints = set()
                    self._diagnostic_observation_fingerprints = claimed_fingerprints
                if token and token in claimed_fingerprints:
                    return False
                claimed = int(getattr(self, "_diagnostic_observation_claimed", 0) or 0)
                if claimed >= max(1, limit):
                    return False
                if token:
                    claimed_fingerprints.add(token)
                self._diagnostic_observation_claimed = claimed + 1
                return True

        async def _diagnostic_observation_admission_guard(
            self,
            db,
            *,
            candidate: dict,
            reason: str,
            fingerprint: str,
        ) -> dict[str, Any]:
            guard = {
                "allowed": True,
                "reason": "accepted",
                "diagnostic_reason": str(reason or "diagnostic_observation"),
                "diagnostic_fingerprint": str(fingerprint or ""),
                "health_guard_enabled": _diagnostic_observation_health_guard_enabled(),
                "dedupe_enabled": _diagnostic_observation_dedupe_enabled(),
                "ttl_days": _diagnostic_observation_ttl_days(),
            }

            if _diagnostic_observation_health_guard_enabled():
                max_age_hours = _diagnostic_observation_health_max_age_hours()
                guard["health_max_age_hours"] = max_age_hours
                health = await self._call_optional_db_method(
                    db,
                    "get_incubation_factory_health",
                    max_age_hours=max_age_hours,
                )
                if isinstance(health, dict):
                    guard["incubation_factory_health"] = dict(health)
                    if not bool(health.get("healthy")):
                        guard["allowed"] = False
                        guard["reason"] = "incubation_factory_stale"
                        return guard
                else:
                    guard["incubation_factory_health"] = {
                        "supported": False,
                        "healthy": True,
                    }

            if _diagnostic_observation_dedupe_enabled() and fingerprint:
                existing = await self._call_optional_db_method(
                    db,
                    "find_active_diagnostic_observation_by_fingerprint",
                    str(fingerprint),
                    ttl_days=_diagnostic_observation_ttl_days(),
                )
                if existing:
                    payload = dict(existing or {})
                    guard["allowed"] = False
                    guard["reason"] = "diagnostic_fingerprint_duplicate"
                    guard["existing_strategy_id"] = payload.get("id") or payload.get("strategy_id")
                    guard["existing_account_id"] = payload.get("diagnostic_account_id") or payload.get("account_id")
                    return guard

            return guard
# --- runner_parts/semantic_contract.py ---

        async def _submit_one(
            self,
            candidate: dict,
            snapshot: dict,
            db,
            *,
            read_only: bool = False,
        ) -> dict:
            """处理单个候选策略的完整提交流程。"""
            gate3_record_only = bool(_gate3_record_only_enabled())
            execution_options = SubmissionExecutionOptions(read_only=bool(read_only or gate3_record_only))
            quality_report_options = SubmissionExecutionOptions(
                read_only=read_only,
                record_only=gate3_record_only,
            )
            candidate = apply_resolved_candidate_envelope(candidate)
            candidate = self._ensure_runtime_playbook(candidate)
            run_submission_quality_gate = _local_run_submission_quality_gate

            existing_strategy = await self._resolve_existing_strategy(candidate, db)
            refresh_existing = existing_strategy is not None
            existing_status = str((existing_strategy or {}).get("status") or "draft")
            strategy_id = str((existing_strategy or {}).get("id") or f"factory_{int(_time.time())}_{uuid4().hex[:8]}")
            name = self._candidate_name(candidate, existing_strategy)
            metrics = dict(candidate.get("backtest_metrics") or {})
            backtest_metrics_contract = dict(
                candidate.get("backtest_metrics_contract")
                or metrics.get("backtest_metrics_contract")
                or {}
            )
            if backtest_metrics_contract and not metrics.get("backtest_metrics_contract"):
                metrics["backtest_metrics_contract"] = backtest_metrics_contract
            semantic_audit: dict[str, Any] = {}
            if _semantic_contract_feature_enabled():
                candidate = ensure_candidate_semantic_contract(candidate)
                candidate["confidence_contract"] = synthesize_confidence_contract(candidate)
                semantic_audit = audit_candidate_semantic_contract(candidate)
                candidate = _apply_candidate_semantic_contract(candidate, semantic_audit)
            gate_a_override = _build_gate_a_spec_override(candidate)
            data = self._build_strategy_data(strategy_id, name, candidate, metrics, existing=existing_strategy)
            candidate = apply_resolved_candidate_envelope(
                {
                    **dict(candidate or {}),
                    "id": strategy_id,
                    "name": name,
                    "params": dict(data.get("params") or {}),
                    "target_symbols": list((data.get("params") or {}).get("target_symbols") or candidate.get("target_symbols") or []),
                    "stock_pool": dict((data.get("params") or {}).get("stock_pool") or candidate.get("stock_pool") or {}),
                    "research_task": dict((data.get("params") or {}).get("research_task") or candidate.get("research_task") or {}),
                    "validation_profile": dict(
                        (data.get("params") or {}).get("validation_profile")
                        or candidate.get("validation_profile")
                        or {}
                    ),
                    "targeting_policy": dict(
                        (data.get("params") or {}).get("targeting_policy")
                        or candidate.get("targeting_policy")
                        or {}
                    ),
                    "constraint_check": dict(
                        (data.get("params") or {}).get("constraint_check")
                        or candidate.get("constraint_check")
                        or {}
                    ),
                    "candidate_contract_snapshot": dict((data.get("params") or {}).get("candidate_contract_snapshot") or {}),
                    "candidate_contract_hash": str((data.get("params") or {}).get("candidate_contract_hash") or ""),
                    "execution_contract_hash": str((data.get("params") or {}).get("execution_contract_hash") or ""),
                    "tested_object_hash": str((data.get("params") or {}).get("tested_object_hash") or ""),
                    "candidate_identity_signature": str((data.get("params") or {}).get("candidate_identity_signature") or ""),
                    "candidate_lineage_contract": dict((data.get("params") or {}).get("candidate_lineage_contract") or {}),
                    "logic_signature": str((data.get("params") or {}).get("logic_signature") or ""),
                    "dsl_signature": str((data.get("params") or {}).get("dsl_signature") or ""),
                    "factor_signature": str((data.get("params") or {}).get("factor_signature") or ""),
                    "entry_exit_signature": str((data.get("params") or {}).get("entry_exit_signature") or ""),
                    "prediction_trace_id": str(
                        (data.get("params") or {}).get("prediction_trace_id")
                        or candidate.get("prediction_trace_id")
                        or ""
                    ),
                    "trace_id": str(
                        (data.get("params") or {}).get("trace_id")
                        or candidate.get("trace_id")
                        or ""
                    ),
                    "research_validation_contract": dict(
                        (data.get("params") or {}).get("research_validation_contract")
                        or candidate.get("research_validation_contract")
                        or {}
                    ),
                    "research_validation_contract_submission_adapter": dict(
                        (data.get("params") or {}).get("research_validation_contract_submission_adapter")
                        or candidate.get("research_validation_contract_submission_adapter")
                        or {}
                    ),
                    "research_protocol_version": str(
                        (data.get("params") or {}).get("research_protocol_version")
                        or candidate.get("research_protocol_version")
                        or ""
                    ),
                    "candidate_contract_version": str(
                        (data.get("params") or {}).get("candidate_contract_version")
                        or candidate.get("candidate_contract_version")
                        or ""
                    ),
                    "spec_completeness": str(
                        (data.get("params") or {}).get("spec_completeness")
                        or candidate.get("spec_completeness")
                        or ""
                    ),
                    "field_provenance": dict(
                        (data.get("params") or {}).get("field_provenance")
                        or candidate.get("field_provenance")
                        or {}
                    ),
                    "field_provenance_summary": dict(
                        (data.get("params") or {}).get("field_provenance_summary")
                        or candidate.get("field_provenance_summary")
                        or {}
                    ),
                    "completion_issues": list(
                        (data.get("params") or {}).get("completion_issues")
                        or candidate.get("completion_issues")
                        or []
                    ),
                    "hard_failures": list(
                        (data.get("params") or {}).get("hard_failures")
                        or candidate.get("hard_failures")
                        or []
                    ),
                    "backtest_metrics": dict(metrics or {}),
                    "backtest_metrics_contract": dict(backtest_metrics_contract or {}),
                }
            )
            if _semantic_contract_feature_enabled():
                candidate = _apply_candidate_semantic_contract(candidate, semantic_audit)
            validation_report = None
            risk_report = None
            if gate_a_override is None:
                validation_report, risk_report = await self._evaluate_reports(candidate, db)
                gate = await run_submission_quality_gate(
                    db,
                    {**data, "status": existing_status if refresh_existing else "submitted"},
                    validation_report=validation_report,
                    risk_report=risk_report,
                    backtest_metrics={
                        **dict(metrics or {}),
                        "trade_count": metrics.get("trade_count"),
                        "trades_count": metrics.get("trades_count"),
                    },
                    incubation_budget_track=str(candidate.get("incubation_budget", {}).get("track") or "formal_incubation"),
                )
            else:
                gate = dict(gate_a_override)
            gate = self._apply_factory_submission_policy(
                candidate,
                name=name,
                gate=gate,
                backtest_metrics=metrics,
                refresh_existing=refresh_existing,
            )
            if _semantic_contract_feature_enabled():
                gate = _apply_semantic_contract_gate(gate, semantic_audit)
            candidate = self._apply_gate_runtime_context(candidate, gate)
            incubation_budget = dict(candidate.get("incubation_budget") or {})
            incubation_budget_track = str(incubation_budget.get("track") or "").strip().lower()
            if self._should_bootstrap_observe_candidate(
                gate,
                incubation_budget_track=incubation_budget_track,
            ):
                incubation_budget = {
                    **incubation_budget,
                    "track": "formal_incubation",
                    "auto_promoted_from_track": incubation_budget_track or "deferred_budget_queue",
                    "auto_promote_reason": "strict_incubation_ready_deferred_formal",
                }
                incubation_budget.setdefault("budget_tier", "standard")
                candidate_params = dict(candidate.get("params") or {})
                candidate_params["incubation_budget"] = dict(incubation_budget)
                candidate = {
                    **dict(candidate or {}),
                    "incubation_budget": incubation_budget,
                    "params": candidate_params,
                }
            candidate_provenance = self._candidate_provenance(candidate, existing_strategy)
            strategy_profile = dict(candidate_provenance.get("strategy_profile") or {})
            incubation_budget = dict(candidate.get("incubation_budget") or {})
            incubation_budget_track = str(incubation_budget.get("track") or "formal_incubation").strip().lower()

            submission_action = self._resolve_submission_action_plan(
                gate,
                candidate=candidate,
                refresh_existing=refresh_existing,
                existing_status=existing_status,
                incubation_budget_track=incubation_budget_track,
                read_only=read_only,
            )
            submission_lane = str(submission_action.get("submission_lane") or "deferred_submission")
            final_status = str(submission_action.get("final_status") or "submitted")
            diagnostic_ok, diagnostic_reason = (False, None)
            if not bool(submission_action.get("wide_intake_admitted")):
                diagnostic_ok, diagnostic_reason = _is_diagnostic_observation_candidate(
                    gate,
                    candidate,
                    metrics,
                    refresh_existing=refresh_existing,
                    read_only=read_only,
                )
            diagnostic_guard: dict[str, Any] = {}
            diagnostic_fingerprint = ""
            if diagnostic_ok:
                diagnostic_reason = diagnostic_reason or "diagnostic_observation"
                diagnostic_fingerprint = _diagnostic_observation_fingerprint(candidate, diagnostic_reason)
                diagnostic_guard = await self._diagnostic_observation_admission_guard(
                    db,
                    candidate=candidate,
                    reason=diagnostic_reason,
                    fingerprint=diagnostic_fingerprint,
                )
            if (
                diagnostic_ok
                and bool(diagnostic_guard.get("allowed"))
                and await self._claim_diagnostic_observation_slot(fingerprint=diagnostic_fingerprint)
            ):
                submission_action = _diagnostic_observation_submission_action(
                    submission_action,
                    reason=diagnostic_reason,
                )
                submission_lane = str(submission_action.get("submission_lane") or "diagnostic_observation")
                final_status = str(submission_action.get("final_status") or "submitted")
                diagnostic_params = {
                    "admission_layer": "diagnostic",
                    "diagnostic_observation": True,
                    "diagnostic_fingerprint": diagnostic_fingerprint,
                    "diagnostic_guard": dict(diagnostic_guard or {}),
                    "diagnostic_reason": diagnostic_reason,
                    "diagnostic_reason_code": diagnostic_reason,
                    "diagnostic_ttl_days": _diagnostic_observation_ttl_days(),
                    "source_lane": "diagnostic_observation",
                }
                nested_action = dict(submission_action.get("submission_action") or {})
                nested_action.update(diagnostic_params)
                submission_action = {
                    **dict(submission_action or {}),
                    **diagnostic_params,
                    "submission_action": nested_action,
                }
                candidate = {
                    **dict(candidate or {}),
                    **diagnostic_params,
                    "params": {
                        **dict(candidate.get("params") or {}),
                        **diagnostic_params,
                    },
                }
            candidate = self._apply_runtime_bootstrap_contract(
                candidate,
                submission_lane=submission_lane,
                runtime_bootstrap_eligible=bool(submission_action.get("runtime_bootstrap_eligible")),
                runtime_bootstrap_budget_tier=str(submission_action.get("runtime_bootstrap_budget_tier") or "") or None,
            )
            planned_submission_lane = str(
                submission_action.get("planned_submission_lane") or submission_lane
            )
            planned_final_status = str(
                submission_action.get("planned_final_status") or final_status
            )
            candidate_params = dict(candidate.get("params") or {})
            if str(submission_lane or "").strip():
                candidate_params["submission_lane"] = submission_lane
            if planned_submission_lane:
                candidate_params["planned_submission_lane"] = planned_submission_lane
            if str(final_status or "").strip():
                candidate_params["final_status"] = final_status
            if planned_final_status:
                candidate_params["planned_final_status"] = planned_final_status
            candidate_params["formal_track_requested"] = bool(
                submission_action.get("formal_track_requested")
            )
            candidate_params["formal_track_auto_corrected"] = bool(
                submission_action.get("formal_track_auto_corrected")
            )
            candidate_params["formal_track_eligible"] = bool(
                submission_action.get("formal_track_eligible")
            )
            candidate_params["observe_first_intake_requested"] = bool(
                submission_action.get("observe_first_intake_requested")
            )
            for field_name in (
                "runtime_bootstrap_eligible",
                "runtime_bootstrap_reason",
                "runtime_bootstrap_budget_tier",
                "runtime_playbook_present",
                "runtime_contract_missing_fields",
                "wide_intake_admitted",
                "observe_intake_requested",
                "pre_observe_hard_reject_reasons",
                "strategy_type_registered",
                "formal_track_blockers",
                "execution_semantic_mode",
                "execution_semantic_gap",
                "execution_semantic_gap_reasons",
                "dsl_required",
                "dsl_compiled",
                "semantic_runtime_match",
                "runtime_family_data_source",
                "proxy_runtime_used",
                "diagnostic_only",
                "execution_readiness_tier",
                "semantic_contract_missing_fields",
                "trade_prediction_contract_status",
                "trade_prediction_contract_hash",
                "trade_prediction_contract_missing_fields",
                "trade_prediction_contract_reject_reasons",
                "trade_prediction_contract_observation_gap",
            ):
                value = submission_action.get(field_name)
                if value not in (None, "", [], {}):
                    candidate_params[field_name] = value
            predicted_incubation_track = str(
                submission_action.get("incubation_budget_track")
                or incubation_budget_track
                or dict(candidate.get("incubation_budget") or {}).get("track")
                or dict(candidate_params.get("incubation_budget") or {}).get("track")
                or ""
            ).strip().lower()
            if predicted_incubation_track:
                candidate_params["incubation_budget_track"] = predicted_incubation_track
            candidate = {
                **dict(candidate or {}),
                "params": candidate_params,
            }
            data = self._build_strategy_data(strategy_id, name, candidate, metrics, existing=existing_strategy)
            candidate = apply_resolved_candidate_envelope(
                {
                    **dict(candidate or {}),
                    "id": strategy_id,
                    "name": name,
                    "params": dict(data.get("params") or {}),
                    "target_symbols": list((data.get("params") or {}).get("target_symbols") or candidate.get("target_symbols") or []),
                    "stock_pool": dict((data.get("params") or {}).get("stock_pool") or candidate.get("stock_pool") or {}),
                    "research_task": dict((data.get("params") or {}).get("research_task") or candidate.get("research_task") or {}),
                    "validation_profile": dict(
                        (data.get("params") or {}).get("validation_profile")
                        or candidate.get("validation_profile")
                        or {}
                    ),
                    "targeting_policy": dict(
                        (data.get("params") or {}).get("targeting_policy")
                        or candidate.get("targeting_policy")
                        or {}
                    ),
                    "constraint_check": dict(
                        (data.get("params") or {}).get("constraint_check")
                        or candidate.get("constraint_check")
                        or {}
                    ),
                    "candidate_contract_snapshot": dict((data.get("params") or {}).get("candidate_contract_snapshot") or {}),
                    "candidate_contract_hash": str((data.get("params") or {}).get("candidate_contract_hash") or ""),
                    "execution_contract_hash": str((data.get("params") or {}).get("execution_contract_hash") or ""),
                    "tested_object_hash": str((data.get("params") or {}).get("tested_object_hash") or ""),
                    "candidate_identity_signature": str((data.get("params") or {}).get("candidate_identity_signature") or ""),
                    "candidate_lineage_contract": dict((data.get("params") or {}).get("candidate_lineage_contract") or {}),
                    "logic_signature": str((data.get("params") or {}).get("logic_signature") or ""),
                    "dsl_signature": str((data.get("params") or {}).get("dsl_signature") or ""),
                    "factor_signature": str((data.get("params") or {}).get("factor_signature") or ""),
                    "entry_exit_signature": str((data.get("params") or {}).get("entry_exit_signature") or ""),
                    "prediction_trace_id": str(
                        (data.get("params") or {}).get("prediction_trace_id")
                        or candidate.get("prediction_trace_id")
                        or ""
                    ),
                    "trace_id": str(
                        (data.get("params") or {}).get("trace_id")
                        or candidate.get("trace_id")
                        or ""
                    ),
                    "research_validation_contract": dict(
                        (data.get("params") or {}).get("research_validation_contract")
                        or candidate.get("research_validation_contract")
                        or {}
                    ),
                    "research_validation_contract_submission_adapter": dict(
                        (data.get("params") or {}).get("research_validation_contract_submission_adapter")
                        or candidate.get("research_validation_contract_submission_adapter")
                        or {}
                    ),
                    "research_protocol_version": str(
                        (data.get("params") or {}).get("research_protocol_version")
                        or candidate.get("research_protocol_version")
                        or ""
                    ),
                    "candidate_contract_version": str(
                        (data.get("params") or {}).get("candidate_contract_version")
                        or candidate.get("candidate_contract_version")
                        or ""
                    ),
                    "spec_completeness": str(
                        (data.get("params") or {}).get("spec_completeness")
                        or candidate.get("spec_completeness")
                        or ""
                    ),
                    "field_provenance": dict(
                        (data.get("params") or {}).get("field_provenance")
                        or candidate.get("field_provenance")
                        or {}
                    ),
                    "field_provenance_summary": dict(
                        (data.get("params") or {}).get("field_provenance_summary")
                        or candidate.get("field_provenance_summary")
                        or {}
                    ),
                    "completion_issues": list(
                        (data.get("params") or {}).get("completion_issues")
                        or candidate.get("completion_issues")
                        or []
                    ),
                    "hard_failures": list(
                        (data.get("params") or {}).get("hard_failures")
                        or candidate.get("hard_failures")
                        or []
                    ),
                    "backtest_metrics": dict(metrics or {}),
                    "backtest_metrics_contract": dict(backtest_metrics_contract or {}),
                }
            )
            quality_report = self._build_quality_report(
                strategy_id=strategy_id,
                candidate=candidate,
                snapshot=snapshot,
                backtest_metrics=metrics,
                quality_gate=gate,
                validation_report=validation_report,
                risk_report=risk_report,
                final_status=final_status,
                submission_lane=submission_lane,
            )
            quality_report = _enrich_quality_report_v2(
                quality_report,
                candidate=candidate,
                gate=gate,
                final_status=final_status,
            )
            quality_summary = dict(quality_report.get("summary") or {})
            quality_summary["candidate_contract_hash"] = candidate.get("candidate_contract_hash")
            quality_summary["execution_contract_hash"] = candidate.get("execution_contract_hash")
            quality_summary["tested_object_hash"] = candidate.get("tested_object_hash")
            quality_summary["candidate_identity_signature"] = candidate.get("candidate_identity_signature")
            quality_summary["target_pool_id"] = (
                dict((candidate.get("candidate_contract_snapshot") or {}).get("targeting") or {}).get("target_pool_id")
            )
            quality_summary["lineage_id"] = (
                dict((candidate.get("candidate_contract_snapshot") or {}).get("lineage") or {}).get("lineage_id")
            )
            quality_summary["multiple_testing_registry"] = dict(gate.get("multiple_testing_registry") or {})
            quality_report["summary"] = quality_summary
            quality_report["candidate_contract_hash"] = candidate.get("candidate_contract_hash")
            quality_report["execution_contract_hash"] = candidate.get("execution_contract_hash")
            quality_report["tested_object_hash"] = candidate.get("tested_object_hash")
            quality_report["candidate_identity_signature"] = candidate.get("candidate_identity_signature")
            quality_report["candidate_contract_snapshot"] = dict(candidate.get("candidate_contract_snapshot") or {})
            quality_report["candidate_lineage_contract"] = dict(candidate.get("candidate_lineage_contract") or {})
            quality_report["logic_signature"] = candidate.get("logic_signature")
            quality_report["dsl_signature"] = candidate.get("dsl_signature")
            quality_report["factor_signature"] = candidate.get("factor_signature")
            quality_report["entry_exit_signature"] = candidate.get("entry_exit_signature")

            multiple_testing_registry = dict(gate.get("multiple_testing_registry") or {})
            multiple_testing_registry_record_id = None
            if gate3_record_only:
                gate3_quality_record = _build_gate3_quality_record_contract(
                    gate=gate,
                    quality_summary=quality_summary,
                    read_only=read_only,
                    gate3_record_only=gate3_record_only,
                )
                planned_submission_lane = submission_lane
                planned_final_status = final_status
                record_status = "gate3_record_passed" if bool(gate.get("passed")) else "gate3_record_failed"
                record_action = dict(submission_action.get("submission_action") or {})
                record_action.update(
                    {
                        "type": "gate3_record_only",
                        "trigger_reason": "gate3_record_only",
                        "next_step": "await_manual_record_review",
                        "completed": False,
                        "planned_submission_lane": planned_submission_lane,
                        "planned_final_status": planned_final_status,
                        "record_status": record_status,
                        "strategy_created": False,
                        "lifecycle_action_executed": False,
                    }
                )
                submission_lane = "gate3_record_only"
                final_status = "gate3_recorded"
                post_gate = {
                    "submission_lane": submission_lane,
                    "final_status": final_status,
                    "planned_submission_lane": planned_submission_lane,
                    "planned_final_status": planned_final_status,
                    "gate3_record_only": True,
                    "record_only": True,
                    "gate_3_recorded": bool(not read_only),
                    "gate3_record_status": record_status,
                    **gate3_quality_record,
                    "quality_report_record_only": True,
                    "quality_report_persisted": False,
                    "gate3_audit_evidence_persisted": False,
                    "record_only_scope": "gate3_audit_evidence",
                    "automatic_downstream_action": False,
                    "strategy_created": False,
                    "lifecycle_action_executed": False,
                    "submission_action": record_action,
                    "submission_action_type": "gate3_record_only",
                    "submission_action_trigger": "gate3_record_only",
                    "submission_action_gaps": list(submission_action.get("submission_action_gaps") or []),
                    "submission_action_fallback_conditions": list(
                        submission_action.get("submission_action_fallback_conditions") or []
                    ),
                    "submission_action_next_step": "await_manual_record_review",
                    "submission_action_completed": False,
                    "admission_decision": submission_action.get("admission_decision"),
                    "formal_track_requested": bool(submission_action.get("formal_track_requested")),
                    "formal_track_eligible": bool(submission_action.get("formal_track_eligible")),
                    "formal_track_blockers": list(submission_action.get("formal_track_blockers") or []),
                    "runtime_bootstrap_reason": submission_action.get("runtime_bootstrap_reason"),
                    "wide_intake_admitted": bool(submission_action.get("wide_intake_admitted")),
                }
                self._apply_submission_action_audit(
                    quality_report,
                    final_status=final_status,
                    submission_lane=submission_lane,
                    submission_audit=post_gate,
                )
                if not read_only:
                    await self._record_experiment(
                        db,
                        candidate,
                        strategy_id,
                        name,
                        snapshot,
                        gate,
                        record_status,
                        validation_report,
                        risk_report,
                        quality_report,
                        metrics,
                        None,
                    )
                    evidence_record = await self._record_gate3_audit_evidence(
                        db,
                        candidate,
                        strategy_id,
                        snapshot,
                        gate,
                        quality_report,
                        metrics,
                        status=record_status,
                        planned_submission_lane=planned_submission_lane,
                        planned_final_status=planned_final_status,
                    )
                    if evidence_record:
                        post_gate["gate3_audit_evidence_id"] = evidence_record.get("id")
                        post_gate["gate3_audit_evidence_persisted"] = True
            else:
                await self._submission_coordinator.persist_candidate(
                    strategy_id=strategy_id,
                    candidate=candidate,
                    data=data,
                    metrics=metrics,
                    validation_report=validation_report,
                    risk_report=risk_report,
                    gate=gate,
                    db=db,
                    refresh_existing=refresh_existing,
                    options=execution_options,
                )
                if refresh_existing:
                    post_gate = await self._submission_coordinator.handle_existing_refresh(
                        strategy_id=strategy_id,
                        name=name,
                        candidate=candidate,
                        gate=gate,
                        quality_report=quality_report,
                        backtest_metrics=metrics,
                        snapshot=snapshot,
                        validation_report=validation_report,
                        risk_report=risk_report,
                        db=db,
                        existing_status=existing_status,
                        submission_lane=submission_lane,
                        submission_action=submission_action,
                        options=execution_options,
                    )
                else:
                    post_gate = await self._submission_coordinator.handle_new_candidate(
                        strategy_id=strategy_id,
                        name=name,
                        candidate=candidate,
                        data=data,
                        gate=gate,
                        quality_report=quality_report,
                        backtest_metrics=metrics,
                        snapshot=snapshot,
                        validation_report=validation_report,
                        risk_report=risk_report,
                        db=db,
                        submission_lane=submission_lane,
                        submission_action=submission_action,
                        options=execution_options,
                    )
                    if not read_only:
                        try:
                            parent_strategy_id = (
                                str((candidate.get("dedup_result") or {}).get("parent_strategy_id") or "").strip()
                                or str(candidate.get("parent_strategy_id") or "").strip()
                                or None
                            )
                            await self._save_strategy_lineage_record(
                                db,
                                strategy_id=strategy_id,
                                parent_strategy_id=parent_strategy_id,
                                reason=str(candidate.get("spawn_reason") or ""),
                                snapshot=snapshot,
                                candidate={**dict(candidate or {}), "multiple_testing_registry": multiple_testing_registry},
                            )
                        except Exception as exc:
                            logger.warning("StrategySubmitter: save lineage failed for %s: %s", strategy_id, exc)
                        if multiple_testing_registry and callable(getattr(db, "save_factory_task_evidence", None)):
                            research_task = _normalize_research_task_contract(candidate.get("research_task") or {})
                            evidence_payload = {
                                "task_key": str(
                                    multiple_testing_registry.get("task_key")
                                    or multiple_testing_registry.get("registry_key")
                                    or multiple_testing_registry.get("task_signature")
                                    or strategy_id
                                ).strip(),
                                "event_id": research_task.get("event_id"),
                                "theme_code": str(research_task.get("theme_code") or "").strip(),
                                "symbol": next(iter(_normalize_target_codes(candidate.get("target_symbols") or [])), None),
                                "evidence_type": "multiple_testing_registry",
                                "weight": float((multiple_testing_registry.get("selection_ratio") or 0.0) or 0.0),
                                "evidence_payload": {
                                    **multiple_testing_registry,
                                    "strategy_id": strategy_id,
                                    "status": str(post_gate.get("final_status") or final_status),
                                },
                            }
                            try:
                                persisted_registry = db.save_factory_task_evidence(evidence_payload)
                                if inspect.isawaitable(persisted_registry):
                                    persisted_registry = await persisted_registry
                                if isinstance(persisted_registry, dict):
                                    multiple_testing_registry_record_id = persisted_registry.get("id")
                                    multiple_testing_registry = {
                                        **multiple_testing_registry,
                                        "evidence_record_id": multiple_testing_registry_record_id,
                                    }
                            except Exception as exc:
                                logger.warning(
                                    "StrategySubmitter: save multiple-testing registry failed for %s: %s",
                                    strategy_id,
                                    exc,
                                )
            if _semantic_contract_feature_enabled() and not read_only and not gate3_record_only:
                candidate_evidence_rows = build_candidate_evidence_records(
                    candidate,
                    strategy_id=strategy_id,
                )
                if callable(getattr(db, "save_strategy_candidate_evidence", None)):
                    for evidence_payload in candidate_evidence_rows:
                        try:
                            persisted_candidate_evidence = db.save_strategy_candidate_evidence(
                                {
                                    "id": evidence_payload.get("id"),
                                    "candidate_id": evidence_payload.get("candidate_id"),
                                    "strategy_id": strategy_id,
                                    "candidate_artifact_id": evidence_payload.get("candidate_artifact_id"),
                                    "experiment_id": evidence_payload.get("experiment_id"),
                                    "evidence_id": evidence_payload.get("evidence_id"),
                                    "evidence_type": evidence_payload.get("evidence_type"),
                                    "source_type": evidence_payload.get("source_type"),
                                    "event_type": evidence_payload.get("event_type"),
                                    "target_symbols": evidence_payload.get("target_symbols") or [],
                                    "direction": evidence_payload.get("direction"),
                                    "horizon_days": evidence_payload.get("horizon_days"),
                                    "raw_confidence": evidence_payload.get("raw_confidence"),
                                    "calibrated_confidence": evidence_payload.get("calibrated_confidence"),
                                    "freshness_ts": evidence_payload.get("freshness_ts"),
                                    "proxy_only": evidence_payload.get("proxy_only"),
                                    "support_metric": evidence_payload.get("support_metric") or {},
                                    "doc_uid": evidence_payload.get("doc_uid"),
                                    "headline_label_id": evidence_payload.get("headline_label_id"),
                                    "source_task_key": evidence_payload.get("source_task_key") or evidence_payload.get("task_key"),
                                    "payload": evidence_payload.get("evidence_payload") or evidence_payload,
                                }
                            )
                            if inspect.isawaitable(persisted_candidate_evidence):
                                await persisted_candidate_evidence
                        except Exception as exc:
                            logger.warning(
                                "StrategySubmitter: save candidate evidence failed for %s: %s",
                                strategy_id,
                                exc,
                            )
            final_status = str(post_gate.get("final_status") or final_status)
            submission_lane = str(post_gate.get("submission_lane") or submission_lane)
            if gate3_record_only:
                factor_performance_report = {
                    "reported": False,
                    "skipped": True,
                    "reason": "gate3_record_only",
                }
            else:
                factor_performance_report = await self._report_factor_performance_after_submit(
                    candidate=candidate,
                    strategy_id=strategy_id,
                    metrics=metrics,
                    validation_report=validation_report,
                    gate=gate,
                    read_only=read_only,
                )
            quality_summary = dict(quality_report.get("summary") or {})
            candidate_params = dict(candidate.get("params") or {})
            candidate_budget = dict(candidate.get("incubation_budget") or {})
            if not candidate_budget:
                candidate_budget = dict(candidate_params.get("incubation_budget") or {})

            def _pick_value(*values: Any) -> Any:
                for value in values:
                    if value not in (None, "", [], {}):
                        return value
                return None

            resolved_submission_action = dict(post_gate.get("submission_action") or submission_action.get("submission_action") or {})
            resolved_submission_action_type = post_gate.get("submission_action_type", submission_action.get("submission_action_type"))
            resolved_submission_action_trigger = post_gate.get("submission_action_trigger", submission_action.get("submission_action_trigger"))
            resolved_submission_action_gaps = list(
                post_gate.get("submission_action_gaps", submission_action.get("submission_action_gaps") or [])
                or []
            )
            resolved_submission_action_fallback_conditions = list(
                post_gate.get(
                    "submission_action_fallback_conditions",
                    submission_action.get("submission_action_fallback_conditions") or [],
                )
                or []
            )
            resolved_submission_action_next_step = post_gate.get(
                "submission_action_next_step",
                submission_action.get("submission_action_next_step"),
            )
            for field_name in (
                "runtime_bootstrap_eligible",
                "runtime_bootstrap_reason",
                "runtime_bootstrap_budget_tier",
                "runtime_playbook_present",
                "runtime_contract_missing_fields",
                "wide_intake_admitted",
                "observe_intake_requested",
                "pre_observe_hard_reject_reasons",
                "strategy_type_registered",
                "formal_track_requested",
                "formal_track_auto_corrected",
                "formal_track_eligible",
                "formal_track_blockers",
                "observe_first_intake_requested",
                "planned_submission_lane",
                "planned_final_status",
                "execution_semantic_mode",
                "execution_semantic_gap",
                "execution_semantic_gap_reasons",
                "dsl_required",
                "dsl_compiled",
                "semantic_runtime_match",
                "runtime_family_data_source",
                "proxy_runtime_used",
                "diagnostic_only",
                "execution_readiness_tier",
                "semantic_contract_missing_fields",
                "trade_prediction_contract_status",
                "trade_prediction_contract_hash",
                "trade_prediction_contract_missing_fields",
                "trade_prediction_contract_reject_reasons",
                "trade_prediction_contract_observation_gap",
            ):
                value = _pick_value(
                    resolved_submission_action.get(field_name),
                    post_gate.get(field_name),
                    quality_summary.get(field_name),
                    gate.get(field_name),
                    candidate.get(field_name),
                    candidate_params.get(field_name),
                )
                if value not in (None, "", [], {}):
                    resolved_submission_action[field_name] = value
            dedup_result = dict(candidate.get("dedup_result") or {})
            constraint_check = dict(candidate.get("constraint_check") or {})
            validation_profile = dict(candidate.get("validation_profile") or {})
            precompile_validation = dict(candidate.get("_generator_precompile_validation") or {})
            precompile_constraint_check = dict(precompile_validation.get("constraint_check") or {})
            feedback_metrics = dict(incubation_budget.get("feedback_metrics") or {})
            target_alignment_violation = (
                str(
                    constraint_check.get("alignment_contract_violation")
                    or precompile_constraint_check.get("alignment_contract_violation")
                    or ""
                ).strip().lower()
                or None
            )
            generator_precompile_reject_reason = (
                str(precompile_validation.get("generator_precompile_reject_reason") or "").strip() or None
            )
            contract_reject_reasons = [
                str(reason).strip()
                for reason in list(precompile_validation.get("contract_reject_reasons") or [])
                if str(reason).strip()
            ]
            feedback_control_mode = str(
                feedback_metrics.get("control_mode")
                or incubation_budget.get("feedback_control_mode")
                or "normal"
            ).strip().lower() or "normal"
            feedback_target_pool_control_mode = str(
                feedback_metrics.get("target_pool_control_mode") or "normal"
            ).strip().lower() or "normal"
            feedback_generator_mode_control_mode = str(
                feedback_metrics.get("generator_mode_control_mode") or "normal"
            ).strip().lower() or "normal"
            prediction_trace_id = normalize_prediction_trace_id(
                candidate.get("prediction_trace_id"),
                candidate.get("trace_id"),
                fallback=quality_summary.get("prediction_trace_id"),
            )
            gate_a_summary = _gate_a_payload(candidate)
            gate_b_summary = _gate_b_payload(candidate, gate)
            gate_c_summary = _gate_c_payload({**candidate, **post_gate}, gate, final_status)

            summary = dict(quality_summary)
            summary.update({
                "strategy_id": strategy_id,
                "prediction_trace_id": prediction_trace_id or None,
                "trace_id": prediction_trace_id or None,
                "experiment_id": candidate.get("experiment_id"),
                "generator_type": candidate.get("generator_type"),
                "name": name,
                "status": final_status,
                "passed": bool(gate.get("passed")),
                "passed_strict": bool(gate.get("passed_strict", gate.get("passed"))),
                "provisional_pass": bool(gate.get("provisional_pass")),
                "admission_stage": gate.get("admission_stage"),
                "incubation_pass_mode": gate.get("incubation_pass_mode"),
                "research_candidate_ready": bool(gate.get("research_candidate_ready")),
                "incubation_candidate_ready": bool(gate.get("incubation_candidate_ready")),
                "live_candidate_ready": bool(gate.get("live_candidate_ready")),
                "gate_b_review_decision": gate.get("gate_b_review_decision"),
                "business_admission_decision": dict(gate.get("business_admission_decision") or {}),
                "benchmark_comparison": dict(gate.get("benchmark_comparison") or {}),
                "cost_sensitivity_summary": dict(gate.get("cost_sensitivity_summary") or {}),
                "cash_sleeve_audit": dict(gate.get("cash_sleeve_audit") or {}),
                "family_holding_bucket": dict(gate.get("family_holding_bucket") or {}),
                "submission_lane": submission_lane,
                "formal_track_requested": bool(
                    post_gate.get("formal_track_requested", submission_action.get("formal_track_requested"))
                ),
                "formal_track_auto_corrected": bool(
                    post_gate.get(
                        "formal_track_auto_corrected",
                        submission_action.get("formal_track_auto_corrected"),
                    )
                ),
                "formal_track_eligible": bool(
                    post_gate.get("formal_track_eligible", submission_action.get("formal_track_eligible"))
                ),
                "formal_track_blockers": list(
                    post_gate.get("formal_track_blockers", submission_action.get("formal_track_blockers") or [])
                ),
                "observe_first_intake_requested": bool(
                    post_gate.get(
                        "observe_first_intake_requested",
                        submission_action.get("observe_first_intake_requested"),
                    )
                ),
                "planned_submission_lane": post_gate.get(
                    "planned_submission_lane",
                    _pick_value(
                        submission_action.get("planned_submission_lane"),
                        resolved_submission_action.get("planned_submission_lane"),
                        submission_lane,
                    ),
                ),
                "planned_final_status": post_gate.get(
                    "planned_final_status",
                    _pick_value(
                        submission_action.get("planned_final_status"),
                        resolved_submission_action.get("planned_final_status"),
                        final_status,
                    ),
                ),
                "runtime_bootstrap_reason": post_gate.get(
                    "runtime_bootstrap_reason",
                    _pick_value(
                        submission_action.get("runtime_bootstrap_reason"),
                        resolved_submission_action.get("runtime_bootstrap_reason"),
                        quality_summary.get("runtime_bootstrap_reason"),
                        gate.get("runtime_bootstrap_reason"),
                        candidate.get("runtime_bootstrap_reason"),
                        candidate_params.get("runtime_bootstrap_reason"),
                    ),
                ),
                "runtime_bootstrap_eligible": bool(
                    post_gate.get(
                        "runtime_bootstrap_eligible",
                        _pick_value(
                            submission_action.get("runtime_bootstrap_eligible"),
                            resolved_submission_action.get("runtime_bootstrap_eligible"),
                            quality_summary.get("runtime_bootstrap_eligible"),
                            gate.get("runtime_bootstrap_eligible"),
                            candidate.get("runtime_bootstrap_eligible"),
                            candidate_params.get("runtime_bootstrap_eligible"),
                        ),
                    )
                ),
                "runtime_bootstrap_budget_tier": post_gate.get(
                    "runtime_bootstrap_budget_tier",
                    _pick_value(
                        submission_action.get("runtime_bootstrap_budget_tier"),
                        resolved_submission_action.get("runtime_bootstrap_budget_tier"),
                        quality_summary.get("runtime_bootstrap_budget_tier"),
                        gate.get("runtime_bootstrap_budget_tier"),
                        candidate.get("runtime_bootstrap_budget_tier"),
                        candidate_params.get("runtime_bootstrap_budget_tier"),
                    ),
                ),
                "runtime_playbook_present": bool(
                    post_gate.get(
                        "runtime_playbook_present",
                        _pick_value(
                            submission_action.get("runtime_playbook_present"),
                            resolved_submission_action.get("runtime_playbook_present"),
                            quality_summary.get("runtime_playbook_present"),
                            gate.get("runtime_playbook_present"),
                            candidate.get("runtime_playbook_present"),
                            candidate_params.get("runtime_playbook_present"),
                        ),
                    )
                ),
                "runtime_contract_missing_fields": list(
                    post_gate.get(
                        "runtime_contract_missing_fields",
                        _pick_value(
                            submission_action.get("runtime_contract_missing_fields"),
                            resolved_submission_action.get("runtime_contract_missing_fields"),
                            quality_summary.get("runtime_contract_missing_fields"),
                            gate.get("runtime_contract_missing_fields"),
                            candidate.get("runtime_contract_missing_fields"),
                            candidate_params.get("runtime_contract_missing_fields"),
                            [],
                        ) or [],
                    )
                ),
                "wide_intake_admitted": bool(
                    post_gate.get(
                        "wide_intake_admitted",
                        _pick_value(
                            submission_action.get("wide_intake_admitted"),
                            resolved_submission_action.get("wide_intake_admitted"),
                            quality_summary.get("wide_intake_admitted"),
                            gate.get("wide_intake_admitted"),
                            candidate.get("wide_intake_admitted"),
                            candidate_params.get("wide_intake_admitted"),
                        ),
                    )
                ),
                "observe_intake_requested": bool(
                    post_gate.get(
                        "observe_intake_requested",
                        _pick_value(
                            submission_action.get("observe_intake_requested"),
                            resolved_submission_action.get("observe_intake_requested"),
                            quality_summary.get("observe_intake_requested"),
                            gate.get("observe_intake_requested"),
                            candidate.get("observe_intake_requested"),
                            candidate_params.get("observe_intake_requested"),
                        ),
                    )
                ),
                "pre_observe_hard_reject_reasons": list(
                    post_gate.get(
                        "pre_observe_hard_reject_reasons",
                        submission_action.get("pre_observe_hard_reject_reasons") or [],
                    )
                ),
                "strategy_type_registered": bool(
                    post_gate.get(
                        "strategy_type_registered",
                        submission_action.get("strategy_type_registered"),
                    )
                ),
                "execution_semantic_mode": post_gate.get(
                    "execution_semantic_mode",
                    _pick_value(
                        submission_action.get("execution_semantic_mode"),
                        resolved_submission_action.get("execution_semantic_mode"),
                        quality_summary.get("execution_semantic_mode"),
                        gate.get("execution_semantic_mode"),
                        candidate.get("execution_semantic_mode"),
                        candidate_params.get("execution_semantic_mode"),
                    ),
                ),
                "execution_semantic_gap": bool(
                    post_gate.get(
                        "execution_semantic_gap",
                        _pick_value(
                            submission_action.get("execution_semantic_gap"),
                            resolved_submission_action.get("execution_semantic_gap"),
                            quality_summary.get("execution_semantic_gap"),
                            gate.get("execution_semantic_gap"),
                            candidate.get("execution_semantic_gap"),
                            candidate_params.get("execution_semantic_gap"),
                        ),
                    )
                ),
                "execution_semantic_gap_reasons": list(
                    post_gate.get(
                        "execution_semantic_gap_reasons",
                        submission_action.get("execution_semantic_gap_reasons") or [],
                    )
                ),
                "dsl_required": bool(
                    post_gate.get(
                        "dsl_required",
                        submission_action.get("dsl_required"),
                    )
                ),
                "dsl_compiled": bool(
                    post_gate.get(
                        "dsl_compiled",
                        submission_action.get("dsl_compiled"),
                    )
                ),
                "semantic_runtime_match": bool(
                    post_gate.get(
                        "semantic_runtime_match",
                        _pick_value(
                            submission_action.get("semantic_runtime_match"),
                            resolved_submission_action.get("semantic_runtime_match"),
                            quality_summary.get("semantic_runtime_match"),
                            gate.get("semantic_runtime_match"),
                            candidate.get("semantic_runtime_match"),
                            candidate_params.get("semantic_runtime_match"),
                        ),
                    )
                ),
                "runtime_family_data_source": post_gate.get(
                    "runtime_family_data_source",
                    _pick_value(
                        submission_action.get("runtime_family_data_source"),
                        resolved_submission_action.get("runtime_family_data_source"),
                        quality_summary.get("runtime_family_data_source"),
                        gate.get("runtime_family_data_source"),
                        candidate.get("runtime_family_data_source"),
                        candidate_params.get("runtime_family_data_source"),
                    ),
                ),
                "proxy_runtime_used": bool(
                    post_gate.get(
                        "proxy_runtime_used",
                        _pick_value(
                            submission_action.get("proxy_runtime_used"),
                            resolved_submission_action.get("proxy_runtime_used"),
                            quality_summary.get("proxy_runtime_used"),
                            gate.get("proxy_runtime_used"),
                            candidate.get("proxy_runtime_used"),
                            candidate_params.get("proxy_runtime_used"),
                        ),
                    )
                ),
                "execution_readiness_tier": post_gate.get(
                    "execution_readiness_tier",
                    _pick_value(
                        submission_action.get("execution_readiness_tier"),
                        resolved_submission_action.get("execution_readiness_tier"),
                        quality_summary.get("execution_readiness_tier"),
                        gate.get("execution_readiness_tier"),
                        candidate.get("execution_readiness_tier"),
                        candidate_params.get("execution_readiness_tier"),
                    ),
                ),
                "semantic_contract_missing_fields": list(
                    post_gate.get(
                        "semantic_contract_missing_fields",
                        submission_action.get("semantic_contract_missing_fields") or [],
                    )
                ),
                "trade_prediction_contract_status": post_gate.get(
                    "trade_prediction_contract_status",
                    _pick_value(
                        submission_action.get("trade_prediction_contract_status"),
                        resolved_submission_action.get("trade_prediction_contract_status"),
                        quality_summary.get("trade_prediction_contract_status"),
                        gate.get("trade_prediction_contract_status"),
                        candidate.get("trade_prediction_contract_status"),
                        candidate_params.get("trade_prediction_contract_status"),
                    ),
                ),
                "trade_prediction_contract_hash": post_gate.get(
                    "trade_prediction_contract_hash",
                    _pick_value(
                        submission_action.get("trade_prediction_contract_hash"),
                        resolved_submission_action.get("trade_prediction_contract_hash"),
                        quality_summary.get("trade_prediction_contract_hash"),
                        gate.get("trade_prediction_contract_hash"),
                        candidate.get("trade_prediction_contract_hash"),
                        candidate_params.get("trade_prediction_contract_hash"),
                    ),
                ),
                "trade_prediction_contract_missing_fields": list(
                    post_gate.get(
                        "trade_prediction_contract_missing_fields",
                        submission_action.get("trade_prediction_contract_missing_fields") or [],
                    )
                ),
                "trade_prediction_contract_reject_reasons": list(
                    post_gate.get(
                        "trade_prediction_contract_reject_reasons",
                        submission_action.get("trade_prediction_contract_reject_reasons") or [],
                    )
                ),
                "trade_prediction_contract_observation_gap": bool(
                    post_gate.get(
                        "trade_prediction_contract_observation_gap",
                        _pick_value(
                            submission_action.get("trade_prediction_contract_observation_gap"),
                            resolved_submission_action.get("trade_prediction_contract_observation_gap"),
                            quality_summary.get("trade_prediction_contract_observation_gap"),
                            gate.get("trade_prediction_contract_observation_gap"),
                            candidate.get("trade_prediction_contract_observation_gap"),
                            candidate_params.get("trade_prediction_contract_observation_gap"),
                        ),
                    )
                ),
                "submission_action_type": resolved_submission_action_type,
                "submission_action_trigger": resolved_submission_action_trigger,
                "submission_action_gaps": resolved_submission_action_gaps,
                "submission_action_fallback_conditions": resolved_submission_action_fallback_conditions,
                "submission_action_next_step": resolved_submission_action_next_step,
                "submission_action_completed": bool(
                    post_gate.get("submission_action_completed", submission_action.get("submission_action_completed"))
                ),
                "submission_action": resolved_submission_action,
                "admission_decision_contract_version": submission_action.get(
                    "admission_decision_contract_version"
                ),
                "admission_decision": submission_action.get("admission_decision"),
                "direct_trade_candidate": bool(gate.get("live_candidate_ready")),
                "pool_admission_applied": bool(post_gate.get("pool_admission_applied")),
                "promotion_applied_transition": dict(post_gate.get("promotion_applied_transition") or {}),
                "admission_block_reasons": list(gate.get("admission_block_reasons") or []),
                "admission_evaluations": dict(gate.get("admission_evaluations") or {}),
                "reasons": gate.get("reasons") or [],
                "reason_codes": gate.get("reason_codes") or [],
                "warning_codes": gate.get("warning_codes") or [],
                "gate_3": dict(gate or {}),
                "dedup_result": dedup_result,
                "refresh_mode": dedup_result.get("refresh_mode"),
                "refresh_decision_basis": dedup_result.get("refresh_decision_basis"),
                "revision_trigger_reason": dedup_result.get("revision_trigger_reason"),
                "tested_object_hash_changed": dedup_result.get(
                    "tested_object_hash_changed",
                    dedup_result.get("tested_object_changed"),
                ),
                "existing_identity_available": bool(dedup_result.get("existing_identity_available")),
                "existing_tested_object_available": bool(dedup_result.get("existing_tested_object_available")),
                "constraint_check": constraint_check,
                "validation_profile": validation_profile,
                "target_alignment_violation": target_alignment_violation,
                "generator_precompile_reject_reason": generator_precompile_reject_reason,
                "contract_reject_reasons": contract_reject_reasons,
                "feedback_control_mode": feedback_control_mode,
                "feedback_target_pool_control_mode": feedback_target_pool_control_mode,
                "feedback_generator_mode_control_mode": feedback_generator_mode_control_mode,
                "primary_validation_layer": gate.get("primary_validation_layer"),
                "quality_summary": quality_summary,
                "validation_grade": quality_summary.get("validation_grade"),
                "raw_validation_grade": quality_summary.get("raw_validation_grade"),
                "effective_validation_grade": quality_summary.get("effective_validation_grade"),
                "validation_grade_adjustment_reason": quality_summary.get(
                    "validation_grade_adjustment_reason"
                ),
                "validation_total_score": quality_summary.get("validation_total_score"),
                "raw_validation_total_score": quality_summary.get("raw_validation_total_score"),
                "strict_incubation_ready": bool(
                    quality_summary.get("strict_incubation_ready", gate.get("strict_incubation_ready"))
                ),
                "strict_incubation_blocked": bool(
                    quality_summary.get("strict_incubation_blocked", gate.get("strict_incubation_blocked"))
                ),
                "event_window_config": dict(metrics.get("event_window_config") or {}),
                "event_window_metrics": dict(metrics.get("event_window_metrics") or {}),
                "position_assumption": metrics.get("position_assumption"),
                "cost_assumptions": dict(metrics.get("cost_assumptions") or {}),
                "explicit_cost_breakdown": dict(metrics.get("explicit_cost_breakdown") or {}),
                "implicit_cost_breakdown": dict(metrics.get("implicit_cost_breakdown") or {}),
                "backtest_assumptions": dict(metrics.get("backtest_assumptions") or {}),
                "execution_reality": dict(quality_report.get("execution_reality") or {}),
                "attempt_adjustment": dict(gate.get("attempt_adjustment") or {}),
                "committee_review": dict(candidate.get("committee_review") or {}),
                "run_correction": {
                    "mode": gate.get("run_correction_mode"),
                    "deflated_sharpe_proxy": gate.get("deflated_sharpe_proxy"),
                    "pbo_proxy": gate.get("pbo_proxy"),
                    "reality_check_pvalue_proxy": gate.get("reality_check_pvalue_proxy"),
                    "spa_pvalue_proxy": gate.get("spa_pvalue_proxy"),
                    "multiple_testing_mode": gate.get("multiple_testing_mode"),
                    "deflated_sharpe_ratio": gate.get("deflated_sharpe_ratio"),
                    "deflated_sharpe_reference_sharpe": gate.get("deflated_sharpe_reference_sharpe"),
                    "deflated_sharpe_effective_trials": gate.get("deflated_sharpe_effective_trials"),
                    "pbo": gate.get("pbo"),
                    "white_reality_check_pvalue": gate.get("white_reality_check_pvalue"),
                    "hansen_spa_pvalue": gate.get("hansen_spa_pvalue"),
                    "multiple_testing": dict(gate.get("multiple_testing") or {}),
                },
                "multiple_testing_registry": multiple_testing_registry,
                "multiple_testing_registry_record_id": multiple_testing_registry_record_id,
                "factor_performance_reported": bool(factor_performance_report.get("reported")),
                "factor_performance_report": factor_performance_report,
                "task_preference": dict(gate.get("task_preference") or {}),
                "task_signature": _build_task_signature(candidate.get("research_task") or {}),
                "candidate_contract_hash": candidate.get("candidate_contract_hash"),
                "execution_contract_hash": candidate.get("execution_contract_hash"),
                "tested_object_hash": candidate.get("tested_object_hash"),
                "candidate_identity_signature": candidate.get("candidate_identity_signature"),
                "research_protocol_version": candidate.get("research_protocol_version"),
                "candidate_contract_version": candidate.get("candidate_contract_version"),
                "spec_completeness": candidate.get("spec_completeness"),
                "field_provenance_summary": dict(candidate.get("field_provenance_summary") or {}),
                "completion_issues": list(candidate.get("completion_issues") or []),
                "hard_failures": list(candidate.get("hard_failures") or []),
                "gate_a": gate_a_summary,
                "gate_b": gate_b_summary,
                "gate_c": gate_c_summary,
                "candidate_contract_snapshot": dict(candidate.get("candidate_contract_snapshot") or {}),
                "candidate_lineage_contract": dict(candidate.get("candidate_lineage_contract") or {}),
                "logic_signature": candidate.get("logic_signature"),
                "dsl_signature": candidate.get("dsl_signature"),
                "factor_signature": candidate.get("factor_signature"),
                "entry_exit_signature": candidate.get("entry_exit_signature"),
                "target_pool_id": (
                    dict((candidate.get("candidate_contract_snapshot") or {}).get("targeting") or {}).get("target_pool_id")
                ),
                "candidate_provenance": candidate_provenance,
                "strategy_profile": strategy_profile,
                "source_candidate_artifact_id": candidate_provenance.get("source_candidate_artifact_id"),
                "source_generation_artifact_id": candidate_provenance.get("source_generation_artifact_id"),
                "source_validation_artifact_id": candidate_provenance.get("source_validation_artifact_id"),
                "candidate_memory_record_id": candidate_provenance.get("memory_record_id"),
                "candidate_family": candidate_provenance.get("candidate_family"),
                "candidate_family_id": candidate_provenance.get("candidate_family_id"),
                "holding_period_bucket": candidate_provenance.get("holding_period_bucket"),
                "alpha_source": candidate_provenance.get("alpha_source"),
                "risk_level": candidate_provenance.get("risk_level"),
                "regime_fit": candidate_provenance.get("regime_fit"),
                "generator_mode": candidate_provenance.get("generator_mode"),
                "direction_bias": candidate_provenance.get("direction_bias"),
                "validation_profile_name": candidate_provenance.get("validation_profile"),
                "target_symbol_count": candidate_provenance.get("target_symbol_count"),
                "candidate_registry_stage": candidate_provenance.get("candidate_registry_stage"),
                "candidate_validation_score": candidate_provenance.get("validation_score"),
                "expected_regime": list(candidate_provenance.get("expected_regime") or []),
                "expected_holding_period": candidate_provenance.get("expected_holding_period"),
                "candidate_latest_validation_at": candidate_provenance.get("latest_validation_at"),
                "candidate_latest_validation_age_days": candidate_provenance.get("latest_validation_age_days"),
                "incubation_budget": incubation_budget,
                "incubation_budget_track": _pick_value(
                    post_gate.get("incubation_budget_track"),
                    incubation_budget_track,
                    candidate_budget.get("track"),
                    dict(candidate_params.get("incubation_budget") or {}).get("track"),
                    quality_summary.get("incubation_budget_track"),
                ),
                "incubation_budget_rank": _pick_value(
                    post_gate.get("incubation_budget_rank"),
                    incubation_budget.get("rank"),
                    candidate_budget.get("rank"),
                    quality_summary.get("incubation_budget_rank"),
                ),
                "incubation_budget_priority_score": _pick_value(
                    post_gate.get("incubation_budget_priority_score"),
                    incubation_budget.get("priority_score"),
                    candidate_budget.get("priority_score"),
                    quality_summary.get("incubation_budget_priority_score"),
                ),
                "incubation_budget_exploration_candidate": bool(incubation_budget.get("exploration_candidate")),
                **post_gate,
            })
            created_total = bool(not refresh_existing and not read_only)
            created_strategy_pool = bool(
                created_total
                and final_status in {"submitted", "incubating"}
                and submission_lane != "diagnostic_observation"
            )
            created_audit_only = bool(not read_only and created_total and not created_strategy_pool)
            gate_3_recorded = bool(not read_only and gate3_record_only)
            gate3_quality_record = _build_gate3_quality_record_contract(
                gate=gate,
                quality_summary=quality_summary,
                read_only=read_only,
                gate3_record_only=gate3_record_only,
            )
            submitted = bool(
                final_status == "submitted"
                and not read_only
                and not gate3_record_only
            )
            resolved_diagnostic_only = post_gate.get("diagnostic_only")
            if resolved_diagnostic_only is None:
                resolved_diagnostic_only = quality_summary.get("diagnostic_only")
            if resolved_diagnostic_only is None:
                resolved_diagnostic_only = gate.get("diagnostic_only")
            if resolved_diagnostic_only is None:
                resolved_diagnostic_only = candidate.get("diagnostic_only")
            summary.update(
                {
                    "created_total": created_total,
                    "created_strategy_pool": created_strategy_pool,
                    "created_audit_only": created_audit_only,
                    "gate_3_recorded": gate_3_recorded,
                    "record_only": bool(gate3_record_only),
                    "gate3_record_only": bool(gate3_record_only),
                    "diagnostic_only": bool(
                        read_only if resolved_diagnostic_only is None else resolved_diagnostic_only
                    ),
                    "read_only": bool(read_only),
                    **gate3_quality_record,
                }
            )
            for field_name in _SEMANTIC_CONTRACT_FIELDS:
                _assign_optional_payload(summary, field_name, candidate.get(field_name))
            quality_report["summary"] = summary
            await self._submission_coordinator.save_quality_report(
                db,
                strategy_id,
                quality_report,
                options=quality_report_options,
            )
            return {
                "created": created_strategy_pool,
                "created_total": created_total,
                "created_strategy_pool": created_strategy_pool,
                "created_audit_only": created_audit_only,
                "gate_3_recorded": gate_3_recorded,
                **gate3_quality_record,
                "record_only": bool(gate3_record_only),
                "refreshed_existing": refresh_existing,
                "submitted": submitted,
                "passed": bool(gate.get("passed")),
                "gate_3": dict(gate or {}),
                "admission_decision_contract_version": submission_action.get(
                    "admission_decision_contract_version"
                ),
                "admission_decision": submission_action.get("admission_decision"),
                "factor_performance_reported": bool(factor_performance_report.get("reported")),
                "summary": summary,
            }

        @staticmethod
        async def _resolve_existing_strategy(candidate: dict, db) -> Optional[dict]:
            dedup_result = dict(candidate.get("dedup_result") or {})
            if not dedup_result.get("refresh_existing"):
                return None
            if str(dedup_result.get("refresh_mode") or "").strip().lower() == "spawn_revision_from_existing":
                return None
            strategy_id = str(dedup_result.get("matched_strategy_id") or "").strip()
            if not strategy_id or not hasattr(db, "get_strategy"):
                return None
            try:
                existing = await db.get_strategy(strategy_id)
            except Exception as exc:
                logger.warning("StrategySubmitter: load existing strategy failed for %s: %s", strategy_id, exc)
                return None
            return dict(existing or {}) if existing else None
# --- runner_parts/submission_flow.py ---

        @classmethod
        async def _save_strategy_lineage_record(
            cls,
            db,
            *,
            strategy_id: str,
            parent_strategy_id: Optional[str],
            reason: str,
            snapshot: dict,
            candidate: Optional[dict] = None,
        ) -> None:
            save_lineage = cls._get_optional_db_method(db, "save_strategy_lineage")
            if save_lineage is None:
                return
            contract_snapshot = dict((candidate or {}).get("candidate_contract_snapshot") or {})
            targeting = dict(contract_snapshot.get("targeting") or {})
            lineage_metadata = {
                "candidate_contract_hash": (candidate or {}).get("candidate_contract_hash"),
                "execution_contract_hash": (candidate or {}).get("execution_contract_hash"),
                "tested_object_hash": (candidate or {}).get("tested_object_hash"),
                "candidate_identity_signature": (candidate or {}).get("candidate_identity_signature"),
                "candidate_contract_snapshot": contract_snapshot,
                "candidate_lineage_contract": dict((candidate or {}).get("candidate_lineage_contract") or contract_snapshot.get("lineage") or {}),
                "logic_signature": (candidate or {}).get("logic_signature"),
                "dsl_signature": (candidate or {}).get("dsl_signature"),
                "factor_signature": (candidate or {}).get("factor_signature"),
                "entry_exit_signature": (candidate or {}).get("entry_exit_signature"),
                "target_pool_id": targeting.get("target_pool_id"),
                "task_signature": dict(contract_snapshot.get("lineage") or {}).get("task_signature"),
                "validation_profile": dict(contract_snapshot.get("validation_profile") or {}),
                "lineage_id": dict(contract_snapshot.get("lineage") or {}).get("lineage_id"),
                "multiple_testing_registry": dict((candidate or {}).get("multiple_testing_registry") or {}),
            }
            accepts_metadata = False
            try:
                signature = inspect.signature(save_lineage)
                params = list(signature.parameters.values())
                accepts_metadata = (
                    "metadata" in signature.parameters
                    or any(param.kind == inspect.Parameter.VAR_KEYWORD for param in params)
                )
            except (TypeError, ValueError):
                accepts_metadata = False
            # PR-S0: 不再把 cycle 全量 snapshot 当 birth_regime 传入，
            # 改为只保留出生时的市场状态（fear_greed / regime / sector / factor 偏好）。
            # 这是 strategy_lineage 表的原始语义。cycle 全量字段（stages /
            # quality_gate / backtest_report / candidate_contract_snapshot 等）
            # 已经在 strategy_factory_runs / strategy_quality_reports / strategies.params
            # 中各有归宿，不需要在 lineage 表里再存一份 130 MB 的副本。
            birth_regime = cls._extract_birth_regime(snapshot)
            result = (
                save_lineage(strategy_id, parent_strategy_id, reason, birth_regime, metadata=lineage_metadata)
                if accepts_metadata
                else save_lineage(strategy_id, parent_strategy_id, reason, birth_regime)
            )
            if inspect.isawaitable(result):
                await result

        @classmethod
        def _extract_birth_regime(cls, snapshot: Optional[dict]) -> dict:
            """PR-S0: 从工厂 cycle snapshot 抽取轻量的"出生市场状态"字典。

            原 strategy_lineage.birth_regime 字段语义是"策略出生时的市场环境快照"，
            预期 ≤ 50 KB。此函数只摘取该语义需要的字段，丢弃 cycle 中的大对象。
            """
            snap = dict(snapshot or {})
            factor_research = dict(snap.get("factor_research") or {})
            factor_summary = dict(factor_research.get("summary") or {})
            market_temperature_context = resolve_market_temperature_context(snap)
            birth_regime = {
                "fg_level": snap.get("fg_level"),
                "fear_greed_index": snap.get("fear_greed_index"),
                "hot_sectors": list(snap.get("hot_sectors") or [])[:8],
                "cold_sectors": list(snap.get("cold_sectors") or [])[:8],
                "active_factors": list(factor_research.get("active_factors") or [])[:6],
                "preferred_strategy_types": list(factor_research.get("preferred_strategy_types") or [])[:6],
                "factor_research_summary": {
                    "top_factor_names": list(factor_summary.get("top_factor_names") or [])[:6],
                    "degraded": bool(factor_research.get("degraded")),
                },
                "regime_summary": dict(snap.get("regime_summary") or {}),
                "as_of": snap.get("as_of") or snap.get("date"),
                "snapshot_id": snap.get("snapshot_id") or snap.get("trace_id"),
                "factory_run_id": snap.get("factory_run_id"),
            }
            if bool(market_temperature_context.get("available")):
                birth_regime["market_temperature_context"] = {
                    "as_of": market_temperature_context.get("as_of"),
                    "temperature": market_temperature_context.get("temperature"),
                    "state": market_temperature_context.get("state"),
                    "quality_status": market_temperature_context.get("quality_status"),
                    "readiness_status": market_temperature_context.get("readiness_status"),
                    "staleness_days": market_temperature_context.get("staleness_days"),
                    "degraded": bool(market_temperature_context.get("degraded")),
                    "warnings": list(market_temperature_context.get("warnings") or [])[:8],
                    "source_chain": list(market_temperature_context.get("source_chain") or [])[:8],
                    "stock_count": market_temperature_context.get("stock_count"),
                    "industry_count": market_temperature_context.get("industry_count"),
                }
            return birth_regime

        @classmethod
        def _build_strategy_data(
            cls,
            strategy_id: str,
            name: str,
            candidate: dict,
            metrics: dict,
            existing: Optional[dict] = None,
        ) -> dict:
            """构建策略记录数据。"""
            candidate = apply_resolved_candidate_envelope(candidate)
            existing = dict(existing or {})
            description = f"{name}\n生成原因: {candidate.get('spawn_reason', '')}"
            if metrics:
                description += f"\n回测: Sharpe {metrics.get('sharpe_ratio', 0):.2f} | "
                description += f"收益 {metrics.get('total_return', 0):.1%} | "
                description += f"回撤 {metrics.get('max_drawdown', 0):.1%}"

            description = ""
            existing_params = dict(existing.get("params") or {})
            normalized_task = _normalize_research_task_contract(candidate.get("research_task") or existing_params.get("research_task") or {})
            candidate_provenance = cls._candidate_provenance(candidate, existing)
            research_validation_contract = dict(
                candidate.get("research_validation_contract")
                or existing_params.get("research_validation_contract")
                or {}
            )
            research_validation_contract_submission_adapter = adapt_research_validation_contract_for_submission(
                research_validation_contract
            )
            prediction_trace_id = normalize_prediction_trace_id(
                candidate.get("prediction_trace_id"),
                candidate.get("trace_id"),
                fallback=existing_params.get("prediction_trace_id") or existing_params.get("trace_id"),
            )

            def _assign_if_present(target: dict, key: str, value) -> None:
                if value not in (None, [], {}, ""):
                    target[key] = value

            stored_params = {
                **existing_params,
                **dict(candidate["params"] or {}),
                "target_symbols": list(candidate.get("target_symbols") or existing_params.get("target_symbols") or []),
                "stock_pool": dict(candidate.get("stock_pool") or existing_params.get("stock_pool") or {}),
                "research_task": normalized_task,
                "hypothesis": candidate.get("hypothesis") or existing_params.get("hypothesis"),
                "holding_horizon": dict(candidate.get("holding_horizon") or existing_params.get("holding_horizon") or {}),
                "trade_plan": dict(candidate.get("trade_plan") or existing_params.get("trade_plan") or {}),
                "risk_rules": dict(candidate.get("risk_rules") or existing_params.get("risk_rules") or {}),
                "position_sizing": dict(candidate.get("position_sizing") or existing_params.get("position_sizing") or {}),
                "execution_notes": candidate.get("execution_notes") or existing_params.get("execution_notes"),
                "rebalance_rule": dict(candidate.get("rebalance_rule") or existing_params.get("rebalance_rule") or {}),
                "portfolio_spec": dict(candidate.get("portfolio_spec") or existing_params.get("portfolio_spec") or {}),
                "execution_assumptions": dict(candidate.get("execution_assumptions") or existing_params.get("execution_assumptions") or {}),
                "runtime_playbook": dict(candidate.get("runtime_playbook") or existing_params.get("runtime_playbook") or {}),
                "validation_profile": dict(candidate.get("validation_profile") or existing_params.get("validation_profile") or {}),
                "targeting_policy": dict(candidate.get("targeting_policy") or existing_params.get("targeting_policy") or {}),
                "constraint_check": dict(candidate.get("constraint_check") or existing_params.get("constraint_check") or {}),
                "task_signature": _build_task_signature(normalized_task),
            }
            if research_validation_contract_submission_adapter:
                stored_params["validation_profile"] = {
                    **dict(research_validation_contract_submission_adapter.get("validation_profile") or {}),
                    **dict(stored_params.get("validation_profile") or {}),
                }
            _assign_if_present(stored_params, "research_validation_contract", research_validation_contract)
            _assign_if_present(
                stored_params,
                "research_validation_contract_submission_adapter",
                research_validation_contract_submission_adapter,
            )
            _assign_if_present(
                stored_params,
                "research_protocol_version",
                candidate.get("research_protocol_version")
                or existing_params.get("research_protocol_version")
                or research_validation_contract_submission_adapter.get("research_protocol_version"),
            )
            _assign_if_present(
                stored_params,
                "candidate_contract_version",
                candidate.get("candidate_contract_version") or existing_params.get("candidate_contract_version"),
            )
            _assign_if_present(
                stored_params,
                "spec_completeness",
                candidate.get("spec_completeness") or existing_params.get("spec_completeness"),
            )
            _assign_if_present(
                stored_params,
                "field_provenance",
                dict(candidate.get("field_provenance") or existing_params.get("field_provenance") or {}),
            )
            _assign_if_present(
                stored_params,
                "field_provenance_summary",
                dict(
                    candidate.get("field_provenance_summary")
                    or existing_params.get("field_provenance_summary")
                    or research_validation_contract_submission_adapter.get("field_provenance_summary")
                    or {}
                ),
            )
            _assign_if_present(
                stored_params,
                "completion_issues",
                list(
                    candidate.get("completion_issues")
                    or existing_params.get("completion_issues")
                    or research_validation_contract_submission_adapter.get("completion_issues")
                    or []
                ),
            )
            _assign_if_present(
                stored_params,
                "hard_failures",
                list(candidate.get("hard_failures") or existing_params.get("hard_failures") or []),
            )
            _assign_if_present(stored_params, "prediction_trace_id", prediction_trace_id)
            _assign_if_present(stored_params, "trace_id", prediction_trace_id)
            _assign_if_present(
                stored_params,
                "evidence_chain",
                dict(candidate.get("evidence_chain") or existing_params.get("evidence_chain") or {}),
            )
            _assign_if_present(
                stored_params,
                "prediction_contract",
                dict(candidate.get("prediction_contract") or existing_params.get("prediction_contract") or {}),
            )
            _assign_if_present(
                stored_params,
                "confidence_contract",
                dict(candidate.get("confidence_contract") or existing_params.get("confidence_contract") or {}),
            )
            _assign_if_present(
                stored_params,
                "evidence_alignment_audit",
                dict(candidate.get("evidence_alignment_audit") or existing_params.get("evidence_alignment_audit") or {}),
            )
            _assign_if_present(
                stored_params,
                "dsl_support_audit",
                dict(candidate.get("dsl_support_audit") or existing_params.get("dsl_support_audit") or {}),
            )
            _assign_if_present(
                stored_params,
                "claim_to_trade_plan_map",
                dict(candidate.get("claim_to_trade_plan_map") or existing_params.get("claim_to_trade_plan_map") or {}),
            )
            _assign_if_present(
                stored_params,
                "semantic_contract_backfilled",
                candidate.get("semantic_contract_backfilled")
                if candidate.get("semantic_contract_backfilled") is not None
                else existing_params.get("semantic_contract_backfilled"),
            )
            if candidate.get("legacy_semantic_contract") is not None:
                stored_params["legacy_semantic_contract"] = bool(candidate.get("legacy_semantic_contract"))
            if candidate.get("contradiction_count") is not None:
                stored_params["contradiction_count"] = int(candidate.get("contradiction_count") or 0)
            if candidate.get("proxy_dependency_score") is not None:
                stored_params["proxy_dependency_score"] = candidate.get("proxy_dependency_score")
            for field_name in (
                "semantic_runtime_match",
                "runtime_family_data_source",
                "proxy_runtime_used",
                "diagnostic_only",
                "execution_readiness_tier",
                "event_prefilter",
                "semantic_contract_missing_fields",
                "execution_semantic_gap_reasons",
                "execution_semantic_mode",
                "dsl_compiled",
                "execution_semantic_gap",
            ):
                value = candidate.get(field_name)
                if value in (None, "", [], {}):
                    value = existing_params.get(field_name)
                _assign_if_present(stored_params, field_name, value)
            for runtime_struct_field in (
                "instrument_profile",
                "trade_plan_to_dsl_map",
                "execution_semantic_contract",
                "fundamental_runtime_contract",
            ):
                _assign_if_present(
                    stored_params,
                    runtime_struct_field,
                    dict(
                        candidate.get(runtime_struct_field)
                        or existing_params.get(runtime_struct_field)
                        or {}
                    ),
                )
            if candidate_provenance:
                stored_params["candidate_provenance"] = candidate_provenance
                if candidate_provenance.get("source_candidate_artifact_id"):
                    stored_params["source_candidate_artifact_id"] = candidate_provenance.get("source_candidate_artifact_id")
                if candidate_provenance.get("source_generation_artifact_id"):
                    stored_params["source_generation_artifact_id"] = candidate_provenance.get("source_generation_artifact_id")
                if candidate_provenance.get("source_validation_artifact_id"):
                    stored_params["source_validation_artifact_id"] = candidate_provenance.get("source_validation_artifact_id")
                if candidate_provenance.get("memory_record_id"):
                    stored_params["candidate_memory_record_id"] = candidate_provenance.get("memory_record_id")
                _assign_if_present(
                    stored_params,
                    "strategy_profile",
                    dict(candidate_provenance.get("strategy_profile") or stored_params.get("strategy_profile") or {}),
                )
                if candidate_provenance.get("candidate_family"):
                    stored_params["candidate_family"] = candidate_provenance.get("candidate_family")
                _assign_if_present(stored_params, "candidate_family_id", candidate_provenance.get("candidate_family_id"))
                _assign_if_present(stored_params, "holding_period_bucket", candidate_provenance.get("holding_period_bucket"))
                _assign_if_present(stored_params, "alpha_source", candidate_provenance.get("alpha_source"))
                _assign_if_present(stored_params, "risk_level", candidate_provenance.get("risk_level"))
                _assign_if_present(stored_params, "regime_fit", candidate_provenance.get("regime_fit"))
                _assign_if_present(stored_params, "generator_mode", candidate_provenance.get("generator_mode"))
                _assign_if_present(stored_params, "direction_bias", candidate_provenance.get("direction_bias"))
                _assign_if_present(stored_params, "validation_profile_name", candidate_provenance.get("validation_profile"))
                _assign_if_present(stored_params, "target_symbol_count", candidate_provenance.get("target_symbol_count"))
                if candidate_provenance.get("candidate_registry_stage"):
                    stored_params["candidate_registry_stage"] = candidate_provenance.get("candidate_registry_stage")
                if candidate_provenance.get("validation_score") is not None:
                    stored_params["candidate_validation_score"] = candidate_provenance.get("validation_score")
                if candidate_provenance.get("expected_regime"):
                    stored_params["expected_regime"] = list(candidate_provenance.get("expected_regime") or [])
                if candidate_provenance.get("expected_holding_period") is not None:
                    stored_params["expected_holding_period"] = candidate_provenance.get("expected_holding_period")
                if candidate_provenance.get("latest_validation_at"):
                    stored_params["candidate_latest_validation_at"] = candidate_provenance.get("latest_validation_at")
                if candidate_provenance.get("latest_validation_age_days") is not None:
                    stored_params["candidate_latest_validation_age_days"] = candidate_provenance.get("latest_validation_age_days")
                if candidate_provenance.get("admission_block_reasons"):
                    stored_params["candidate_admission_block_reasons"] = list(
                        candidate_provenance.get("admission_block_reasons") or []
                    )
                if candidate_provenance.get("candidate_evidence_status"):
                    stored_params["candidate_evidence_status"] = dict(candidate_provenance.get("candidate_evidence_status") or {})
            if candidate.get("selection_logic") or existing_params.get("selection_logic"):
                stored_params["selection_logic"] = list(candidate.get("selection_logic") or existing_params.get("selection_logic") or [])
            if candidate.get("incubation_budget"):
                stored_params["incubation_budget"] = dict(candidate.get("incubation_budget") or {})
            strategy_explanation = build_strategy_explanation(
                {
                    **existing,
                    **dict(candidate or {}),
                    "id": strategy_id,
                    "strategy_id": strategy_id,
                    "name": name,
                    "strategy_type": candidate["strategy_type"],
                    "params": dict(stored_params),
                    "tags": list(
                        dict.fromkeys(
                            [
                                *(existing.get("tags") or []),
                                "auto_generated",
                                "factory",
                                candidate["strategy_type"],
                                *(candidate.get("tags") or []),
                            ]
                        )
                    ),
                },
                metrics=metrics,
                existing=existing,
                source="strategy_factory_submit",
            )
            if strategy_explanation:
                stored_params["strategy_explanation"] = strategy_explanation
                description = render_strategy_description(
                    name,
                    strategy_explanation,
                    metrics=metrics,
                )
            if not description:
                description = f"{name}\nGenerated reason: {candidate.get('spawn_reason', '')}"
            contract_source = {
                **existing,
                **dict(candidate or {}),
                "id": strategy_id,
                "strategy_id": strategy_id,
                "name": name,
                "strategy_type": candidate["strategy_type"],
                "params": dict(stored_params),
                "target_symbols": list(stored_params.get("target_symbols") or []),
                "stock_pool": dict(stored_params.get("stock_pool") or {}),
                "research_task": dict(stored_params.get("research_task") or {}),
            }
            trade_prediction_seed_params = dict(stored_params)
            explicit_trade_prediction_contract = dict(
                candidate.get("trade_prediction_contract")
                or stored_params.get("trade_prediction_contract")
                or {}
            )
            if explicit_trade_prediction_contract:
                explicit_trade_prediction_contract["strategy_id"] = strategy_id
                contract_source["trade_prediction_contract"] = explicit_trade_prediction_contract
                trade_prediction_seed_params["trade_prediction_contract"] = explicit_trade_prediction_contract
            from strategy_factory.application.trade_prediction_contract import freeze_trade_prediction_contract

            frozen_trade_prediction = freeze_trade_prediction_contract(
                {
                    **contract_source,
                    "id": strategy_id,
                    "strategy_id": strategy_id,
                    "trade_prediction_contract": explicit_trade_prediction_contract,
                    "params": trade_prediction_seed_params,
                }
            )
            stored_params["trade_prediction_contract"] = dict(frozen_trade_prediction.get("contract") or {})
            stored_params["trade_prediction_contract_status"] = frozen_trade_prediction.get("status")
            stored_params["trade_prediction_contract_hash"] = frozen_trade_prediction.get("contract_hash")
            stored_params["trade_prediction_contract_missing_fields"] = list(
                frozen_trade_prediction.get("missing_fields") or []
            )
            stored_params["trade_prediction_contract_reject_reasons"] = list(
                frozen_trade_prediction.get("reject_reasons") or []
            )
            contract_source["params"] = dict(stored_params)
            contract_snapshot = build_portfolio_candidate_contract(contract_source)
            resolved_candidate_envelope = build_resolved_candidate_envelope(contract_source)
            stored_params["candidate_contract_snapshot"] = contract_snapshot
            stored_params["candidate_contract_hash"] = build_candidate_contract_hash(contract=contract_snapshot)
            stored_params["execution_contract_hash"] = (
                str(resolved_candidate_envelope.get("execution_contract_hash") or "").strip()
                or build_execution_contract_hash(contract=contract_snapshot)
            )
            stored_params["tested_object_hash"] = (
                str(resolved_candidate_envelope.get("tested_object_hash") or "").strip()
                or build_tested_object_hash(contract_source)
            )
            stored_params["candidate_identity_signature"] = build_candidate_identity_signature(contract_source)
            stored_params["candidate_lineage_contract"] = dict(contract_snapshot.get("lineage") or {})
            stored_params["logic_signature"] = str(resolved_candidate_envelope.get("logic_signature") or "")
            stored_params["dsl_signature"] = str(resolved_candidate_envelope.get("dsl_signature") or "")
            stored_params["factor_signature"] = str(resolved_candidate_envelope.get("factor_signature") or "")
            stored_params["entry_exit_signature"] = str(resolved_candidate_envelope.get("entry_exit_signature") or "")
            stored_params["resolved_candidate_envelope"] = resolved_candidate_envelope
            return {
                "id": strategy_id,
                "name": name,
                "description": description,
                "author_id": existing.get("author_id") or "strategy_factory",
                "strategy_type": candidate["strategy_type"],
                "params": stored_params,
                "factor_weights": dict((candidate["params"] or {}).get("factor_weights", existing.get("factor_weights") or {})),
                "status": existing.get("status") or "draft",
                "prediction_trace_id": prediction_trace_id or None,
                "trace_id": prediction_trace_id or None,
                "research_protocol_version": stored_params.get("research_protocol_version"),
                "candidate_contract_version": stored_params.get("candidate_contract_version"),
                "spec_completeness": stored_params.get("spec_completeness"),
                "field_provenance_summary": dict(stored_params.get("field_provenance_summary") or {}),
                "completion_issues": list(stored_params.get("completion_issues") or []),
                "hard_failures": list(stored_params.get("hard_failures") or []),
                "tags": list(
                    dict.fromkeys(
                        [
                            *(existing.get("tags") or []),
                            "auto_generated",
                            "factory",
                            candidate["strategy_type"],
                            *(candidate.get("tags") or []),
                            *(strategy_explanation.get("labels") or []),
                        ]
                    )
                ),
            }

        @classmethod
        def _factor_pool_summary_float(cls, *values: Any) -> Optional[float]:
            for value in values:
                if value in (None, "", [], {}):
                    continue
                try:
                    numeric = float(value)
                except (TypeError, ValueError) as _swallowed:
                    logger.warning("packages/strategy-factory/src/strategy_factory/application/_submitter_actions/runner.py:2814: swallowed %s and continued: %s", type(_swallowed).__name__, _swallowed)
                    continue
                if numeric == numeric and numeric not in (float("inf"), float("-inf")):
                    return float(numeric)
            return None

        @classmethod
        def _factor_pool_summary_blocks_submission(cls, summary: dict[str, Any]) -> bool:
            shelf_decision = dict(summary.get("qc_shelf_decision") or {})
            decision = str(shelf_decision.get("decision") or "").strip().lower()
            if decision not in {"retire", "quarantine", "reject"}:
                return False
            if bool(summary.get("qc_autoshelf_applied")):
                return True
            labels = summary.get("qc_labels")
            if not isinstance(labels, dict):
                return True
            availability_keys = {
                "oos_available",
                "layered_available",
                "robustness_available",
                "multiple_testing_available",
            }
            if any(key in labels for key in availability_keys):
                return any(bool(labels.get(key)) for key in availability_keys)
            numeric_keys = (
                "rank_ic_ir",
                "bootstrap_ci_lower",
                "monotonicity",
                "long_short_return",
                "window_stability",
                "param_sensitivity",
                "dsr",
                "pbo",
            )
            all_zero = True
            for key in numeric_keys:
                value = cls._factor_pool_summary_float(labels.get(key))
                if value is not None and abs(value) > 1e-12:
                    all_zero = False
                    break
            unknown_oos = (
                not bool(labels.get("oos_pass"))
                and str(labels.get("oos_grade") or "").strip().lower() in {"", "unknown"}
            )
            return not (all_zero and unknown_oos)

        @classmethod
        def _factor_pool_validation_report_from_params(
            cls,
            params: dict[str, Any],
            candidate: dict[str, Any],
            base_report: Optional[dict[str, Any]],
        ) -> Optional[dict[str, Any]]:
            summary = dict((params or {}).get("factor_pool_validation_summary") or {})
            if not summary or cls._factor_pool_summary_blocks_submission(summary):
                return base_report

            metrics = dict(summary.get("metrics") or {})
            evidence = dict(summary.get("evidence_summary") or {})
            quick = dict(summary.get("quick_evidence") or {})
            quick_evidence = dict(quick.get("evidence_summary") or {})
            rating = dict(summary.get("rating") or {})
            quality_evidence = dict(rating.get("quality_evidence") or {})
            quality_summary = dict(quality_evidence.get("summary") or {})
            governance = dict(rating.get("governance") or {})
            governance_raw = dict(governance.get("raw_metrics") or {})
            persisted = dict(summary.get("persisted_outputs") or {})
            avg_stability_ratio = cls._factor_pool_summary_float(governance_raw.get("avg_stability_ratio"))
            avg_degradation = cls._factor_pool_summary_float(governance_raw.get("avg_degradation"))

            quality_status = str(summary.get("quality_status") or "").strip().lower()
            recommendation = str(rating.get("recommendation") or "").strip().lower()
            if quality_status not in {"promoted", "active"} and recommendation != "promote":
                return base_report

            rank_ic_mean = cls._factor_pool_summary_float(
                metrics.get("rank_ic_mean"),
                evidence.get("rank_ic_mean"),
                quality_summary.get("rank_ic_mean"),
                quick.get("rank_ic_mean"),
                quick_evidence.get("rank_ic_mean"),
            )
            rank_ic_ir = cls._factor_pool_summary_float(
                metrics.get("rank_ic_ir"),
                evidence.get("rank_ic_ir"),
                quality_summary.get("rank_ic_ir"),
                quick.get("rank_ic_ir"),
                quick_evidence.get("rank_ic_ir"),
            )
            rank_ic_std = cls._factor_pool_summary_float(metrics.get("rank_ic_std"))
            sample_dates = cls._factor_pool_summary_float(
                metrics.get("sample_dates"),
                evidence.get("sample_dates"),
                quality_summary.get("sample_dates"),
                quick.get("sample_dates"),
                quick_evidence.get("sample_dates"),
            )
            bootstrap_ci_lower = cls._factor_pool_summary_float(
                metrics.get("bootstrap_ci_lower"),
                metrics.get("ci_lower"),
                evidence.get("bootstrap_ci_lower"),
                quality_summary.get("bootstrap_ci_lower"),
            )
            if (
                bootstrap_ci_lower is None
                and rank_ic_mean is not None
                and rank_ic_std is not None
                and sample_dates is not None
                and sample_dates > 1
            ):
                bootstrap_ci_lower = rank_ic_mean - 1.96 * (rank_ic_std / (sample_dates ** 0.5))

            if (
                rank_ic_ir is None
                or rank_ic_mean is None
                or bootstrap_ci_lower is None
                or rank_ic_ir < 0.3
                or rank_ic_mean < 0.02
                or bootstrap_ci_lower < 0.0
            ):
                return base_report

            factor_meta = dict(candidate.get("factor_pool_metadata") or {})
            factor_name = str(
                (params or {}).get("factor_name")
                or factor_meta.get("factor_name")
                or (params or {}).get("factor_pool_factor_id")
                or factor_meta.get("factor_id")
                or candidate.get("strategy_type")
                or "active_factor_pool"
            ).strip()
            report = dict(base_report or {})
            report["strategy_validation_report"] = dict(base_report or {})
            report["factor_name"] = factor_name
            report["n_periods"] = int(sample_dates or 0)
            report["n_stocks"] = int(
                cls._factor_pool_summary_float(
                    evidence.get("avg_cross_section_n"),
                    quality_summary.get("avg_cross_section_n"),
                    quick.get("avg_cross_section_n"),
                    quick_evidence.get("avg_cross_section_n"),
                )
                or 0
            )
            report["walk_forward"] = {
                **dict(report.get("walk_forward") or {}),
                "method": "active_factor_pool_governed_oos",
                "oos_rank_ic_mean": round(float(rank_ic_mean), 6),
                "oos_rank_ic_ir": round(float(rank_ic_ir), 6),
            }
            report["purged_kfold"] = {
                **dict(report.get("purged_kfold") or {}),
                "method": "active_factor_pool_governed_cross_section",
                "oos_rank_ic_mean": round(float(rank_ic_mean), 6),
                "oos_rank_ic_ir": round(float(rank_ic_ir), 6),
            }
            report["bootstrap_ci"] = {
                **dict(report.get("bootstrap_ci") or {}),
                "ci_lower": round(float(bootstrap_ci_lower), 6),
                "ic_mean": round(float(rank_ic_mean), 6),
                "sample_size": int(sample_dates or 0),
                "source": "active_factor_pool_validation_summary",
            }
            report["multiple_testing"] = {
                **dict(report.get("multiple_testing") or {}),
                "deflated_sharpe": {
                    "available": governance_raw.get("deflated_sharpe") is not None,
                    "dsr": cls._factor_pool_summary_float(governance_raw.get("deflated_sharpe")),
                },
                "pbo": {
                    "available": governance_raw.get("pbo") is not None,
                    "pbo": cls._factor_pool_summary_float(governance_raw.get("pbo")),
                },
                "white_reality_check": {
                    "available": governance_raw.get("white_reality_check_p_value") is not None,
                    "p_value": cls._factor_pool_summary_float(governance_raw.get("white_reality_check_p_value")),
                },
                "hansen_spa": {
                    "available": governance_raw.get("hansen_spa_p_value") is not None,
                    "p_value": cls._factor_pool_summary_float(governance_raw.get("hansen_spa_p_value")),
                },
            }
            report["rating"] = rating or dict(report.get("rating") or {})
            statistical_metrics = {
                **dict(report.get("statistical_metrics") or {}),
                "wf_ic_ir": {"value": round(float(rank_ic_ir), 6), "source": "active_factor_pool_validation_summary"},
                "pkf_ic": {"value": round(float(rank_ic_mean), 6), "source": "active_factor_pool_validation_summary"},
                "bootstrap_ci_lower": {
                    "value": round(float(bootstrap_ci_lower), 6),
                    "source": "active_factor_pool_validation_summary",
                    "derived": bool(rank_ic_std is not None),
                },
            }
            if avg_stability_ratio is not None:
                statistical_metrics["param_sensitivity"] = {
                    "value": round(max(0.0, min(1.0, 1.0 - float(avg_stability_ratio))), 6),
                    "source": "active_factor_pool_governance.avg_stability_ratio",
                    "derived": True,
                }
            if avg_degradation is not None:
                statistical_metrics["period_robustness"] = {
                    "value": {
                        "first_half_ic": round(float(rank_ic_mean), 6),
                        "second_half_ic": round(float(rank_ic_mean) - abs(float(avg_degradation)), 6),
                    },
                    "source": "active_factor_pool_governance.avg_degradation",
                    "derived": True,
                }
            report["statistical_metrics"] = statistical_metrics
            report["active_factor_pool_validation"] = {
                "source": "factor_pool_validation_summary",
                "quality_status": quality_status,
                "quality_score": summary.get("quality_score"),
                "sample_dates": sample_dates,
                "ic_history_rows": persisted.get("ic_history_rows_total") or persisted.get("ic_history_rows"),
                "source_candidate_artifact_id": (params or {}).get("source_candidate_artifact_id"),
                "source_validation_artifact_id": (params or {}).get("source_validation_artifact_id"),
            }
            report["validation_report_source"] = "active_factor_pool_validation_summary"
            return report

        async def _evaluate_reports(self, candidate: dict, db) -> tuple[Optional[dict], Optional[dict]]:
            """先计算验证/风险报告，避免在 Gate-3 前产生持久化副作用。优化：并发执行。"""
            import asyncio as _asyncio
            report_params = self._candidate_report_params(candidate)

            async def _run_validation():
                try:
                    return await self._get_validation_gateway().run_validation_report(
                        candidate["strategy_type"],
                        report_params,
                        db,
                    )
                except Exception as exc:
                    logger.warning("StrategySubmitter: validation report failed for %s: %s", candidate.get("strategy_type"), exc)
                    return None

            async def _run_risk():
                try:
                    return await self._get_risk_gateway().run_risk_report(
                        candidate["strategy_type"],
                        report_params,
                        db,
                    )
                except Exception as exc:
                    logger.warning("StrategySubmitter: risk report failed for %s: %s", candidate.get("strategy_type"), exc)
                    return None

            validation_report, risk_report = await _asyncio.gather(_run_validation(), _run_risk())
            validation_report = self._factor_pool_validation_report_from_params(
                report_params,
                candidate,
                validation_report,
            )
            try:
                from strategy_factory.application.research.statistical_robustness import (
                    enrich_validation_report_with_robustness_derivations,
                )

                candidate_index = int(
                    candidate.get("candidate_index")
                    or candidate.get("rank")
                    or candidate.get("priority_rank")
                    or 0
                )
                validation_report = enrich_validation_report_with_robustness_derivations(
                    validation_report,
                    backtest_metrics=dict(candidate.get("backtest_metrics") or {}),
                    candidate_index=candidate_index,
                )
            except Exception as exc:
                logger.warning(
                    "StrategySubmitter: validation robustness enrichment failed for %s: %s",
                    candidate.get("strategy_type"),
                    exc,
                )
            return validation_report, risk_report

        async def _save_metric_with_retry(
            self,
            db,
            strategy_id: str,
            period: str,
            payload: dict,
            *,
            attempts: int = 3,
            initial_delay: float = 0.2,
        ) -> bool:
            """PR-S3: 持久化指标带 retry + DLQ。

            最多 ``attempts`` 次（指数退避 0.2s → 0.5s → 1.0s），全部失败后
            将 (strategy_id, period, payload, last_error) 记入实例 DLQ 列表
            ``_persistence_dlq``，供 cycle runner / 监控读取。
            """
            delays = [0.0]
            delay = initial_delay
            for _ in range(max(attempts - 1, 0)):
                delays.append(delay)
                delay = min(delay * 2.5, 1.5)
            import asyncio as _asyncio_local
            last_exc: Optional[Exception] = None
            for delay in delays:
                if delay > 0:
                    await _asyncio_local.sleep(delay)
                try:
                    await db.save_strategy_metrics(strategy_id, period, payload)
                    return True
                except Exception as exc:
                    last_exc = exc
                    logger.debug(
                        "StrategySubmitter: save %s metrics attempt failed for %s (delay=%.1fs): %s",
                        period, strategy_id, delay, exc,
                    )
            logger.warning(
                "StrategySubmitter: save %s metrics failed for %s after %d attempts: %s",
                period, strategy_id, len(delays), last_exc,
            )
            dlq = getattr(self, "_persistence_dlq", None)
            if dlq is None:
                dlq = []
                setattr(self, "_persistence_dlq", dlq)
            dlq.append(
                {
                    "strategy_id": strategy_id,
                    "period": period,
                    "payload": payload,
                    "error": str(last_exc) if last_exc else None,
                    "error_type": type(last_exc).__name__ if last_exc else None,
                    "attempts": len(delays),
                }
            )
            return False

        async def _persist_metrics(
            self,
            strategy_id: str,
            metrics: dict,
            validation_report: Optional[dict],
            risk_report: Optional[dict],
            db,
        ) -> None:
            """Gate-3 决策后再落库指标，避免未通过候选提前写入。"""
            if metrics:
                await self._save_metric_with_retry(
                    db,
                    strategy_id,
                    "backtest",
                    {
                        "sharpe_ratio": metrics.get("sharpe_ratio"),
                        "total_return": metrics.get("total_return"),
                        "max_drawdown": metrics.get("max_drawdown"),
                        "win_rate": metrics.get("win_rate"),
                        "trade_count": int(metrics.get("trades_count", 0)),
                    },
                )

            if validation_report:
                rating = validation_report.get("rating", {})
                await self._save_metric_with_retry(
                    db,
                    strategy_id,
                    "validation",
                    {
                        "grade": rating.get("grade"),
                        "total_score": rating.get("total_score"),
                        "oos_rank_ic": validation_report.get("walk_forward", {}).get("oos_rank_ic_mean"),
                        "recommendation": rating.get("recommendation"),
                    },
                )

            if risk_report:
                await self._save_metric_with_retry(db, strategy_id, "risk", risk_report)

        @staticmethod
        def _factor_feedback_float(value, default: float = 0.0) -> float:
            try:
                if value is None:
                    return default
                return float(value)
            except (TypeError, ValueError):
                return default

        @classmethod
        def _factor_pool_factor_id_from_candidate(cls, candidate: dict) -> str:
            payload = dict(candidate or {})
            params = dict(payload.get("params") or {})
            metadata = dict(payload.get("metadata") or {})
            factor_meta = dict(payload.get("factor_pool_metadata") or {})
            return str(
                metadata.get("factor_pool_factor_id")
                or payload.get("factor_pool_factor_id")
                or params.get("factor_pool_factor_id")
                or factor_meta.get("factor_id")
                or ""
            ).strip()

        @classmethod
        def _factor_feedback_realized_ic(
            cls,
            metrics: dict,
            validation_report: Optional[dict],
        ) -> float:
            metric_payload = dict(metrics or {})
            for key in ("ic_mean", "rank_ic_mean", "mean_ic", "oos_rank_ic_mean"):
                if metric_payload.get(key) is not None:
                    return cls._factor_feedback_float(metric_payload.get(key))
            for bucket_name in ("factor_metrics", "statistical_validation", "validation"):
                bucket = dict(metric_payload.get(bucket_name) or {})
                for key in ("ic_mean", "rank_ic_mean", "oos_rank_ic_mean"):
                    if bucket.get(key) is not None:
                        return cls._factor_feedback_float(bucket.get(key))

            report = dict(validation_report or {})
            for bucket_name in ("walk_forward", "oos_validation", "metrics"):
                bucket = dict(report.get(bucket_name) or {})
                for key in ("oos_rank_ic_mean", "rank_ic_mean", "ic_mean"):
                    if bucket.get(key) is not None:
                        return cls._factor_feedback_float(bucket.get(key))
            return 0.0

        async def _report_factor_performance_after_submit(
            self,
            *,
            candidate: dict,
            strategy_id: str,
            metrics: dict,
            validation_report: Optional[dict],
            gate: dict,
            read_only: bool,
        ) -> dict[str, Any]:
            factor_id = self._factor_pool_factor_id_from_candidate(candidate)
            if read_only:
                return {"reported": False, "reason": "read_only"}
            if not factor_id:
                return {"reported": False, "reason": "missing_factor_pool_factor_id"}
            if not bool((gate or {}).get("passed")):
                return {"reported": False, "reason": "gate_not_passed", "factor_id": factor_id}

            payload = {
                "realized_ic": self._factor_feedback_realized_ic(metrics, validation_report),
                "realized_turnover": self._factor_feedback_float(
                    (metrics or {}).get("turnover_rate")
                    or (metrics or {}).get("turnover")
                ),
                "realized_cost": self._factor_feedback_float(
                    (metrics or {}).get("estimated_cost_rate")
                    or dict((metrics or {}).get("cost_assumptions") or {}).get("commission_rate")
                ),
                "period": "submission",
            }
            try:
                await self._get_factor_pool_gateway().report_factor_performance(
                    factor_id,
                    strategy_id,
                    payload,
                )
                return {"reported": True, "factor_id": factor_id, "metrics": payload}
            except Exception as exc:
                logger.debug(
                    "StrategySubmitter: factor performance report failed for %s/%s: %s",
                    factor_id,
                    strategy_id,
                    exc,
                )
                return {
                    "reported": False,
                    "factor_id": factor_id,
                    "reason": "report_failed",
                    "error": str(exc),
                }

        @staticmethod
        def _normalized_validation_grade(gate: Optional[dict[str, Any]]) -> str:
            normalized_gate = dict(gate or {})
            return str(
                normalized_gate.get("effective_validation_grade")
                or normalized_gate.get("validation_grade")
                or normalized_gate.get("raw_validation_grade")
                or ""
            ).strip().upper()

        @staticmethod
        def _strategy_type_registered(candidate: Optional[dict[str, Any]]) -> bool:
            strategy_type = str(
                candidate_contract_value(candidate or {}, "strategy_type")
                or dict(candidate or {}).get("strategy_type")
                or ""
            ).strip().lower()
            return bool(strategy_type) and strategy_type in _VALID_STRATEGY_TYPES

        @staticmethod
        def _default_runtime_holding_horizon(strategy_type: str) -> dict[str, Any]:
            normalized = str(strategy_type or "").strip().lower()
            if normalized in {"quality_factor", "value_factor"}:
                return {"min_days": 30, "max_days": 84, "cooldown_window_days": 7}
            if normalized in {"momentum", "ma_cross", "volatility_breakout"}:
                return {"min_days": 14, "max_days": 48, "cooldown_window_days": 5}
            return {"min_days": 5, "max_days": 20, "cooldown_window_days": 5}

        @staticmethod
        def _default_runtime_trade_plan(strategy_type: str) -> dict[str, Any]:
            normalized = str(strategy_type or "").strip().lower()
            if normalized == "momentum":
                return {
                    "entry_bias": "trend_persistence_confirmation",
                    "exit_bias": "false_breakout_or_momentum_decay",
                }
            if normalized == "ma_cross":
                return {
                    "entry_bias": "adaptive_cross_with_volume_confirmation",
                    "exit_bias": "range_reentry_or_cross_failure",
                }
            if normalized in {"quality_factor", "value_factor"}:
                return {
                    "entry_bias": "cross_sectional_rank",
                    "exit_bias": "rank_decay_or_periodic_rebalance",
                }
            return {
                "entry_bias": "signal_confirmed",
                "exit_bias": "signal_or_time_stop",
            }

        @classmethod
        def _default_runtime_risk_rules(cls, strategy_type: str, holding_horizon: dict[str, Any]) -> dict[str, Any]:
            normalized = str(strategy_type or "").strip().lower()
            max_holding_days = int(holding_horizon.get("max_days") or 20)
            if normalized in {"quality_factor", "value_factor"}:
                return {
                    "stop_loss_pct": 0.08,
                    "take_profit_pct": 0.18,
                    "max_holding_days": max(max_holding_days, 42),
                    "cooldown_days": max(5, int(holding_horizon.get("cooldown_window_days") or 7)),
                }
            return {
                "stop_loss_pct": 0.10,
                "take_profit_pct": 0.20,
                "max_holding_days": max_holding_days,
                "cooldown_days": max(3, int(holding_horizon.get("cooldown_window_days") or 5)),
            }

        @staticmethod
        def _default_runtime_incubation_policy(holding_horizon: dict[str, Any]) -> dict[str, Any]:
            max_days = max(8, int(dict(holding_horizon or {}).get("max_days") or 20))
            expected_trade_count = max(4.0, min(12.0, 252.0 / float(max_days)))
            warmup_target_signals = max(4, min(8, int(round(expected_trade_count / 2.5)) or 4))
            warmup_soft_timeout_days = max(5, min(18, int(round(max(5, warmup_target_signals * 2.0)))))
            warmup_hard_timeout_days = max(20, min(45, int(round(max(20, warmup_target_signals * 5.0)))))
            warmup_max_days = max(30, min(60, int(round(max(warmup_hard_timeout_days + 10, warmup_soft_timeout_days + 15)))))
            return {
                "warmup_target_signals": warmup_target_signals,
                "warmup_soft_timeout_days": warmup_soft_timeout_days,
                "warmup_hard_timeout_days": warmup_hard_timeout_days,
                "warmup_max_days": warmup_max_days,
            }

        @staticmethod
        def _default_runtime_execution_assumptions() -> dict[str, Any]:
            return {
                "commission_rate": 0.00025,
                "slippage_bps": 5,
                "tradability_filter": True,
                "slippage_model": "fixed",
            }

        @classmethod
        def _runtime_playbook_from_contract(cls, candidate: Optional[dict[str, Any]]) -> dict[str, Any]:
            payload = dict(candidate or {})
            playbook = dict(candidate_contract_value(payload, "runtime_playbook", {}) or {})
            if playbook:
                return playbook
            holding_horizon = dict(candidate_contract_value(payload, "holding_horizon", {}) or {})
            trade_plan = dict(candidate_contract_value(payload, "trade_plan", {}) or {})
            risk_rules = dict(candidate_contract_value(payload, "risk_rules", {}) or {})
            execution_assumptions = dict(candidate_contract_value(payload, "execution_assumptions", {}) or {})
            portfolio_spec = dict(candidate_contract_value(payload, "portfolio_spec", {}) or {})
            if not holding_horizon and not trade_plan and not risk_rules and not execution_assumptions:
                return {}

            strategy_type = str(payload.get("strategy_type") or "").strip().lower()
            stop_loss_pct = abs(float(risk_rules.get("stop_loss_pct") or risk_rules.get("stop_loss") or 0.08) or 0.08)
            take_profit_pct = abs(
                float(risk_rules.get("take_profit_pct") or risk_rules.get("take_profit") or max(stop_loss_pct * 2.0, 0.12))
                or max(stop_loss_pct * 2.0, 0.12)
            )
            time_stop_days = max(1, int(risk_rules.get("max_holding_days") or holding_horizon.get("max_days") or 20))
            cooldown_days = max(
                1,
                int(
                    risk_rules.get("cooldown_days")
                    or risk_rules.get("cooldown_window_days")
                    or trade_plan.get("cooldown_window_days")
                    or holding_horizon.get("cooldown_window_days")
                    or 5
                ),
            )
            max_position_pct = min(
                0.35,
                max(
                    0.02,
                    float(
                        portfolio_spec.get("max_position_pct")
                        or risk_rules.get("max_position_pct")
                        or 0.18
                    )
                    or 0.18,
                ),
            )
            family = "default"
            if strategy_type in {"momentum", "ma_cross", "volatility_breakout", "event_structure_breakout"}:
                family = "trend"
            elif strategy_type in {"quality_factor", "value_factor"}:
                family = "slow_factor"
                time_stop_days = max(time_stop_days, 42)
            failure_exit_rule = (
                "opposite_signal_or_breakout_failure"
                if family == "trend"
                else "quality_drift_or_rank_decay"
                if family == "slow_factor"
                else "signal_or_time_stop"
            )
            playbook = {
                "entry_policy": {
                    "order_style": "marketable_limit",
                    "signal_validity_days": max(1, min(5, max(1, time_stop_days // 5))),
                    "max_slippage_bps": float(
                        execution_assumptions.get("max_slippage_bps")
                        or execution_assumptions.get("slippage_bps")
                        or 5.0
                    ),
                    "tradability_guard": bool(
                        execution_assumptions.get("tradability_filter")
                        if execution_assumptions.get("tradability_filter") is not None
                        else True
                    ),
                },
                "exit_policy": {
                    "initial_stop_loss_pct": round(max(0.02, stop_loss_pct), 4),
                    "take_profit_pct": round(max(take_profit_pct, stop_loss_pct), 4),
                    "trailing_stop_pct": round(max(0.03, min(stop_loss_pct * (0.8 if family == "trend" else 1.0), 0.12)), 4),
                    "trailing_activation_profit_pct": round(max(stop_loss_pct, 0.05), 4),
                    "time_stop_days": int(time_stop_days),
                    "failure_exit_rule": failure_exit_rule,
                },
                "adverse_move_policy": {
                    "loss_bands": [
                        {
                            "threshold_pct": round(max(0.01, stop_loss_pct * 0.5), 4),
                            "action": "hold",
                            "label": "soft_drawdown_watch",
                        },
                        {
                            "threshold_pct": round(max(0.02, stop_loss_pct), 4),
                            "action": "reduce" if family == "slow_factor" else "exit",
                            "label": "primary_stop_band",
                        },
                        {
                            "threshold_pct": round(max(0.03, stop_loss_pct * 1.2), 4),
                            "action": "freeze_reentry",
                            "label": "hard_stop_band",
                        },
                    ],
                    "average_down": "forbid",
                    "freeze_after_stop": True,
                    "reduce_on_drawdown": family == "slow_factor",
                },
                "reentry_policy": {
                    "cooldown_days": int(cooldown_days),
                    "reclaim_condition": (
                        "reclaim_fast_ma_and_break_recent_high"
                        if family == "trend"
                        else "recover_rank_and_trend_alignment"
                        if family == "slow_factor"
                        else "signal_reconfirm_after_cooldown"
                    ),
                    "max_retries_per_20d": 1 if family == "slow_factor" else 2,
                },
                "position_policy": {
                    "budget_mode": "fixed_fraction",
                    "base_budget_pct": 0.05 if family == "slow_factor" else 0.04,
                    "max_position_pct": round(max_position_pct, 4),
                    "max_concurrent_positions": 2 if family in {"trend", "slow_factor"} else 1,
                    "scale_in": {"enabled": False, "mode": "forbid"},
                    "scale_out": {
                        "enabled": family == "slow_factor",
                        "mode": "reduce_then_exit" if family == "slow_factor" else "take_profit_or_trailing",
                    },
                },
                "incubation_policy": cls._default_runtime_incubation_policy(holding_horizon),
            }
            return playbook

        @classmethod
        def _ensure_runtime_playbook(cls, candidate: Optional[dict[str, Any]]) -> dict[str, Any]:
            payload = dict(candidate or {})
            params = dict(payload.get("params") or {})
            strategy_type = str(payload.get("strategy_type") or "").strip().lower()
            holding_horizon = dict(candidate_contract_value(payload, "holding_horizon", {}) or {})
            if not holding_horizon:
                holding_horizon = cls._default_runtime_holding_horizon(strategy_type)
            trade_plan = dict(candidate_contract_value(payload, "trade_plan", {}) or {})
            if not trade_plan:
                trade_plan = cls._default_runtime_trade_plan(strategy_type)
            risk_rules = dict(candidate_contract_value(payload, "risk_rules", {}) or {})
            if not risk_rules:
                risk_rules = cls._default_runtime_risk_rules(strategy_type, holding_horizon)
            execution_assumptions = dict(candidate_contract_value(payload, "execution_assumptions", {}) or {})
            if not execution_assumptions:
                execution_assumptions = cls._default_runtime_execution_assumptions()
            payload["holding_horizon"] = holding_horizon
            payload["trade_plan"] = trade_plan
            payload["risk_rules"] = risk_rules
            payload["execution_assumptions"] = execution_assumptions
            params.update(
                {
                    "holding_horizon": dict(holding_horizon),
                    "trade_plan": dict(trade_plan),
                    "risk_rules": dict(risk_rules),
                    "execution_assumptions": dict(execution_assumptions),
                }
            )
            playbook = cls._runtime_playbook_from_contract(payload)
            if not playbook:
                payload["params"] = params
                return payload
            params["runtime_playbook"] = dict(playbook)
            payload["params"] = params
            payload["runtime_playbook"] = dict(playbook)
            return payload
# --- runner_parts/persistence.py ---

        @classmethod
        def _apply_gate_runtime_context(
            cls,
            candidate: Optional[dict[str, Any]],
            gate: Optional[dict[str, Any]],
        ) -> dict[str, Any]:
            payload = cls._ensure_runtime_playbook(candidate)
            normalized_gate = dict(gate or {})
            params = dict(payload.get("params") or {})
            runtime_field_names = (
                "execution_semantic_mode",
                "execution_semantic_gap",
                "execution_semantic_gap_reasons",
                "dsl_required",
                "dsl_compiled",
                "semantic_runtime_match",
                "runtime_family_data_source",
                "proxy_runtime_used",
                "diagnostic_only",
                "execution_readiness_tier",
                "semantic_contract_missing_fields",
                "trade_prediction_contract_status",
                "trade_prediction_contract_hash",
                "trade_prediction_contract_missing_fields",
                "trade_prediction_contract_reject_reasons",
                "trade_prediction_contract_observation_gap",
            )

            def _copy_runtime_value(value: Any) -> Any:
                if isinstance(value, dict):
                    return dict(value)
                if isinstance(value, list):
                    return list(value)
                return value

            for field_name in runtime_field_names:
                if field_name not in normalized_gate:
                    continue
                value = normalized_gate.get(field_name)
                if value in _EMPTY_CONTRACT_VALUES:
                    continue
                copied = _copy_runtime_value(value)
                payload[field_name] = copied
                params[field_name] = _copy_runtime_value(value)
            payload["params"] = params
            return payload

        @classmethod
        def _runtime_bootstrap_context(
            cls,
            gate: Optional[dict[str, Any]],
            *,
            candidate: Optional[dict[str, Any]],
        ) -> dict[str, Any]:
            normalized_gate = dict(gate or {})
            payload = cls._apply_gate_runtime_context(candidate, normalized_gate)
            validation_grade = cls._normalized_validation_grade(normalized_gate)
            strategy_type_registered = cls._strategy_type_registered(payload)
            missing_runtime_fields = [
                field_name
                for field_name in _RUNTIME_BOOTSTRAP_REQUIRED_FIELDS
                if candidate_contract_value(payload, field_name) in _EMPTY_CONTRACT_VALUES
            ]
            runtime_playbook_present = bool(candidate_contract_value(payload, "runtime_playbook", {}))
            execution_semantic_mode = str(
                candidate_contract_value(payload, "execution_semantic_mode") or ""
            ).strip().lower() or None
            execution_semantic_gap = bool(candidate_contract_value(payload, "execution_semantic_gap"))
            execution_semantic_gap_reasons = [
                str(item or "").strip()
                for item in list(candidate_contract_value(payload, "execution_semantic_gap_reasons", []) or [])
                if str(item or "").strip()
            ]
            dsl_required = bool(candidate_contract_value(payload, "dsl_required"))
            dsl_compiled = bool(candidate_contract_value(payload, "dsl_compiled"))
            semantic_runtime_match = bool(
                candidate_contract_value(payload, "semantic_runtime_match", True)
            )
            runtime_family_data_source = str(
                candidate_contract_value(payload, "runtime_family_data_source") or ""
            ).strip().lower() or None
            proxy_runtime_used = bool(candidate_contract_value(payload, "proxy_runtime_used"))
            diagnostic_only = bool(candidate_contract_value(payload, "diagnostic_only"))
            execution_readiness_tier = str(
                candidate_contract_value(payload, "execution_readiness_tier") or ""
            ).strip().lower() or None
            semantic_contract_missing_fields = [
                str(item or "").strip()
                for item in list(candidate_contract_value(payload, "semantic_contract_missing_fields", []) or [])
                if str(item or "").strip()
            ]
            trade_prediction_contract_status = str(
                candidate_contract_value(payload, "trade_prediction_contract_status") or ""
            ).strip().lower()
            trade_prediction_contract_hash = str(
                candidate_contract_value(payload, "trade_prediction_contract_hash") or ""
            ).strip()
            trade_prediction_contract = dict(
                candidate_contract_value(payload, "trade_prediction_contract", {}) or {}
            )
            trade_prediction_contract_reject_reasons = [
                str(item or "").strip().lower()
                for item in list(
                    candidate_contract_value(payload, "trade_prediction_contract_reject_reasons", []) or []
                )
                if str(item or "").strip()
            ]
            trade_prediction_contract_observation_gap = False
            if trade_prediction_contract_status != "ready":
                source = str(
                    trade_prediction_contract.get("contract_source") or ""
                ).strip().lower()
                invalid_reasons = [
                    reason
                    for reason in trade_prediction_contract_reject_reasons
                    if reason.startswith("invalid:")
                ]
                derived_gap_reasons = {"invalid:direction", "invalid:confidence"}
                missing_gap_reasons = {"missing:direction", "missing:confidence"}
                missing_reasons = [
                    reason
                    for reason in trade_prediction_contract_reject_reasons
                    if reason.startswith("missing:")
                ]
                trade_prediction_contract_observation_gap = bool(
                    source != "explicit"
                    and trade_prediction_contract_reject_reasons
                    and not any(reason not in derived_gap_reasons for reason in invalid_reasons)
                    and not any(reason not in missing_gap_reasons for reason in missing_reasons)
                    and (
                        any(
                            reason in missing_gap_reasons
                            for reason in trade_prediction_contract_reject_reasons
                        )
                        or any(reason in derived_gap_reasons for reason in trade_prediction_contract_reject_reasons)
                    )
                )
            if trade_prediction_contract_observation_gap:
                diagnostic_only = True
                if not execution_readiness_tier or execution_readiness_tier == "formal_runtime_ready":
                    execution_readiness_tier = "observe_diagnostic_only"
            trade_prediction_ready = (
                trade_prediction_contract_status == "ready"
                and bool(trade_prediction_contract_hash)
                and bool(trade_prediction_contract)
            )
            evidence_hard_fail_reasons = [
                str(item or "").strip().lower()
                for item in list(
                    dict(payload.get("evidence_alignment_audit") or {}).get("hard_fail_reasons") or []
                )
                if str(item or "").strip()
            ]
            if trade_prediction_ready:
                evidence_hard_fail_reasons = [
                    reason
                    for reason in evidence_hard_fail_reasons
                    if "prediction_contract_conflict_resolution_rule_missing" not in reason
                ]
            semantic_hard_fail = bool(evidence_hard_fail_reasons)
            quality_passed = bool(normalized_gate.get("passed"))
            # === DEV-V1 P0: D 级硬否决 toggle 化 ===
            # 当 STRATEGY_FACTORY_OBSERVE_D_GRADE_ENABLED=1 时,D 级 + Gate passed 候选
            # 可以走 observe lane (paper micro budget tier 自动生效);
            # formal_track_blockers 中已有的 strict_incubation_pass 仍挡住 D 级升 formal,
            # formal 严格性零变化。
            allow_d_grade_observe = _observe_d_grade_enabled()
            # === INVERT-DESIGN P1 改动A: Layer 1 宽进准入 ===
            # 当 STRATEGY_FACTORY_WIDE_INTAKE_OBSERVE_ENABLED=1 时,放开"必须 Gate-3 passed"
            # 这一前置。结构合法(类型已注册 + runtime 字段齐 + 无 semantic hard fail)的候选,
            # 即使 quality_passed=False,也判 observe-eligible,走零资本 paper 观察。
            # 此开关只放开 observe,不改 formal(下方 formal_track_blockers 仍要求 strict pass)。
            incubation_budget = dict(payload.get("incubation_budget") or {})
            params = dict(payload.get("params") or {})
            if not incubation_budget:
                incubation_budget = dict(params.get("incubation_budget") or {})
            incubation_budget_track = str(incubation_budget.get("track") or "").strip().lower()
            observe_intake_requested = bool(
                candidate_contract_value(payload, "observe_first_intake")
                or params.get("observe_first_intake")
                or incubation_budget.get("observe_first_intake")
                or incubation_budget_track == "observe_incubation"
            )
            wide_intake_observe = _wide_intake_observe_enabled() or observe_intake_requested
            gate_reason_values: list[str] = []
            for key in (
                "hard_fail_reasons",
                "admission_block_reasons",
                "reason_codes",
                "reasons",
            ):
                raw_values = normalized_gate.get(key)
                if isinstance(raw_values, (list, tuple, set)):
                    gate_reason_values.extend(
                        str(item or "").strip().lower()
                        for item in raw_values
                        if str(item or "").strip()
                    )
                elif raw_values not in _EMPTY_CONTRACT_VALUES:
                    gate_reason_values.append(str(raw_values or "").strip().lower())
            gate_reason_values = list(dict.fromkeys(gate_reason_values))
            audit_only_fragments = (
                "insufficient_statistical_evidence",
                "missing_statistical_metrics",
                "missing_wf_ic_ir",
                "missing_pkf_ic",
                "missing_bootstrap_ci_lower",
                "missing_param_sensitivity",
                "weak_wf_ic_ir",
                "weak_pkf_ic",
                "weak_bootstrap_ci_lower",
                "weak_param_sensitivity",
                "walk_forward_ic_ir",
                "purged_kfold_ic",
                "bootstrap_ci_lower",
                "param_sensitivity",
                "period_robustness",
                "trade_count",
                "factory_policy_backtest_trade_count",
                "win_rate",
                "profit_factor",
                "expectancy",
                "out_of_sample_profit_factor",
            )
            hard_fail_fragments = (
                "trade_prediction_contract_not_ready",
                "missing_runtime_contract",
                "runtime_contract_missing",
                "semantic_hard_fail",
                "final_strategy_missing_semantic_contract",
                "prediction_contract_claim_missing_evidence_ids",
                "prediction_contract_conflict_resolution_rule_missing",
                "trade_plan_node_missing_claim_ids",
                "dsl_entry_not_mapped_to_trade_plan",
                "dsl_exit_not_mapped_to_trade_plan",
                "semantic_contract_contradiction_detected",
                "proxy_only_event_evidence_not_allowed",
                "dsl_contains_unsupported_rules",
                "lagging_entry_without_lead_evidence",
                "temporal_coherence_audit_failed",
                "ambiguous_regime_condition_not_allowed",
                "lookahead",
                "leakage",
                "live_trading",
                "broker_write",
                "precompile_reject",
                "generator_hard_reject",
                "not_executable",
                "non_executable",
            )
            pre_observe_hard_reasons = [
                reason
                for reason in gate_reason_values
                if any(fragment in reason for fragment in hard_fail_fragments)
                and not any(fragment in reason for fragment in audit_only_fragments)
            ]
            if trade_prediction_ready:
                pre_observe_hard_reasons = [
                    reason
                    for reason in pre_observe_hard_reasons
                    if "prediction_contract_conflict_resolution_rule_missing" not in reason
                ]
            if not trade_prediction_ready and not trade_prediction_contract_observation_gap:
                pre_observe_hard_reasons.append("trade_prediction_contract_not_ready")
            pre_observe_hard_reasons = list(dict.fromkeys(pre_observe_hard_reasons))
            semantic_hard_fail = bool(semantic_hard_fail or pre_observe_hard_reasons)
            structurally_valid = (
                strategy_type_registered
                and not missing_runtime_fields
                and not semantic_hard_fail
            )
            runtime_bootstrap_eligible = (
                quality_passed
                and (allow_d_grade_observe or validation_grade != "D")
                and strategy_type_registered
                and not missing_runtime_fields
                and not semantic_hard_fail
            )
            # 宽进:Gate-3 未过但结构合法 → 仍可进 observe。
            wide_intake_admitted = bool(
                wide_intake_observe
                and not runtime_bootstrap_eligible
                and structurally_valid
            )
            if wide_intake_admitted:
                runtime_bootstrap_eligible = True
            if runtime_bootstrap_eligible:
                # === DEV-V1 P0: D 级走 observe 时使用专用 reason ===
                if wide_intake_admitted:
                    runtime_bootstrap_reason = "wide_intake_observe_gate3_not_required"
                elif validation_grade == "D":
                    runtime_bootstrap_reason = "d_grade_observe_only_micro_budget"
                elif execution_semantic_gap:
                    runtime_bootstrap_reason = "execution_semantic_gap_observe_only"
                elif proxy_runtime_used:
                    runtime_bootstrap_reason = "proxy_runtime_observe_only"
                elif diagnostic_only:
                    runtime_bootstrap_reason = "diagnostic_only_observe"
                else:
                    runtime_bootstrap_reason = "quality_passed_non_d_candidate_with_complete_runtime_contract"
            elif not strategy_type_registered:
                runtime_bootstrap_reason = "strategy_type_not_registered"
            elif missing_runtime_fields:
                runtime_bootstrap_reason = f"missing_runtime_contract:{','.join(missing_runtime_fields)}"
            elif semantic_hard_fail:
                runtime_bootstrap_reason = str((pre_observe_hard_reasons or ["semantic_hard_fail"])[0])
            elif not quality_passed:
                runtime_bootstrap_reason = "quality_gate_failed"
            elif validation_grade == "D":
                # 仅在 toggle OFF 且 D 级 + Gate passed 时走到此分支
                runtime_bootstrap_reason = "validation_grade_d_not_allowed_for_runtime"
            else:
                runtime_bootstrap_reason = "runtime_bootstrap_blocked"
            budget_tier = None
            if runtime_bootstrap_eligible:
                budget_tier = (
                    "micro"
                    if wide_intake_admitted or execution_semantic_gap or proxy_runtime_used or diagnostic_only
                    else "standard" if _validation_grade_at_least(validation_grade, "A") else "micro"
                )
            return {
                "runtime_bootstrap_eligible": runtime_bootstrap_eligible,
                "runtime_bootstrap_reason": runtime_bootstrap_reason,
                "runtime_bootstrap_budget_tier": budget_tier,
                "wide_intake_admitted": wide_intake_admitted,
                "runtime_playbook_present": runtime_playbook_present,
                "runtime_contract_missing_fields": missing_runtime_fields,
                "observe_intake_requested": observe_intake_requested,
                "pre_observe_hard_reject_reasons": pre_observe_hard_reasons,
                "trade_prediction_contract_status": trade_prediction_contract_status or "missing",
                "trade_prediction_contract_hash": trade_prediction_contract_hash or None,
                "trade_prediction_contract_observation_gap": trade_prediction_contract_observation_gap,
                "strategy_type_registered": strategy_type_registered,
                "execution_semantic_mode": execution_semantic_mode,
                "execution_semantic_gap": execution_semantic_gap,
                "execution_semantic_gap_reasons": execution_semantic_gap_reasons,
                "dsl_required": dsl_required,
                "dsl_compiled": dsl_compiled,
                "semantic_runtime_match": semantic_runtime_match,
                "runtime_family_data_source": runtime_family_data_source,
                "proxy_runtime_used": proxy_runtime_used,
                "diagnostic_only": diagnostic_only,
                "execution_readiness_tier": execution_readiness_tier,
                "semantic_contract_missing_fields": semantic_contract_missing_fields,
            }

        @classmethod
        def _apply_runtime_bootstrap_contract(
            cls,
            candidate: Optional[dict[str, Any]],
            *,
            submission_lane: str,
            runtime_bootstrap_eligible: bool,
            runtime_bootstrap_budget_tier: Optional[str],
        ) -> dict[str, Any]:
            payload = cls._ensure_runtime_playbook(candidate)
            if (
                str(submission_lane or "").strip().lower() != "observe_incubation"
                or not runtime_bootstrap_eligible
                or runtime_bootstrap_budget_tier not in {"standard", "micro"}
            ):
                return payload
            playbook = dict(payload.get("runtime_playbook") or {})
            entry_policy = dict(playbook.get("entry_policy") or {})
            position_policy = dict(playbook.get("position_policy") or {})
            entry_policy["order_style"] = "marketable_limit"
            if runtime_bootstrap_budget_tier == "standard":
                position_policy["base_budget_pct"] = 0.06
                position_policy["max_concurrent_positions"] = 2
            else:
                position_policy["base_budget_pct"] = 0.03
                position_policy["max_concurrent_positions"] = 1
            playbook["entry_policy"] = entry_policy
            playbook["position_policy"] = position_policy
            params = dict(payload.get("params") or {})
            params["runtime_playbook"] = dict(playbook)
            payload["params"] = params
            payload["runtime_playbook"] = dict(playbook)
            return payload

        @classmethod
        def _should_bootstrap_observe_candidate(
            cls,
            gate: Optional[dict[str, Any]],
            *,
            incubation_budget_track: str,
        ) -> bool:
            normalized_gate = dict(gate or {})
            track = str(incubation_budget_track or "").strip().lower()
            if track not in {"", "deferred_budget_queue", "deferred_submission"}:
                return False
            if not bool(normalized_gate.get("passed")):
                return False
            if bool(normalized_gate.get("live_candidate_ready")):
                return False
            if bool(normalized_gate.get("research_only_due_to_trade_audit_gap")):
                return False
            if not bool(normalized_gate.get("research_candidate_ready")):
                return False
            if not bool(normalized_gate.get("strict_incubation_ready")):
                return False
            if str(normalized_gate.get("incubation_pass_mode") or "").strip().lower() != "strict":
                return False
            if bool(normalized_gate.get("provisional_pass")):
                return False
            validation_grade = cls._normalized_validation_grade(normalized_gate)
            return _validation_grade_at_least(validation_grade, "A")

        @classmethod
        def _allow_observe_trade_audit_bootstrap(
            cls,
            gate: Optional[dict[str, Any]],
            *,
            incubation_budget_track: str,
        ) -> bool:
            normalized_gate = dict(gate or {})
            if str(incubation_budget_track or "").strip().lower() != "observe_incubation":
                return False
            if not bool(normalized_gate.get("passed")):
                return False
            if not bool(normalized_gate.get("research_candidate_ready")):
                return False
            if bool(normalized_gate.get("live_candidate_ready")):
                return False
            validation_grade = cls._normalized_validation_grade(normalized_gate)
            return validation_grade != "D"

        @classmethod
        def _resolve_submission_action_plan(
            cls,
            gate: dict,
            *,
            candidate: Optional[dict[str, Any]] = None,
            refresh_existing: bool,
            existing_status: str,
            incubation_budget_track: str,
            read_only: bool = False,
        ) -> dict[str, Any]:
            return SubmissionAdmissionAuthority.resolve(
                gate,
                candidate=candidate,
                refresh_existing=refresh_existing,
                existing_status=existing_status,
                incubation_budget_track=incubation_budget_track,
                runtime_bootstrap_resolver=cls._runtime_bootstrap_context,
                read_only=read_only,
            )

        @classmethod
        def _resolve_submission_lane(
            cls,
            gate: dict,
            *,
            candidate: Optional[dict[str, Any]] = None,
            refresh_existing: bool,
            existing_status: str,
            incubation_budget_track: str,
        ) -> tuple[str, str]:
            plan = cls._resolve_submission_action_plan(
                gate,
                candidate=candidate,
                refresh_existing=refresh_existing,
                existing_status=existing_status,
                incubation_budget_track=incubation_budget_track,
            )
            return str(plan.get("submission_lane") or "deferred_submission"), str(plan.get("final_status") or "submitted")

        @staticmethod
        def _apply_submission_action_audit(
            quality_report: Optional[dict],
            *,
            final_status: str,
            submission_lane: str,
            submission_audit: Optional[dict] = None,
        ) -> dict:
            report = quality_report if isinstance(quality_report, dict) else {}
            summary = dict(report.get("summary") or {})
            audit = dict(submission_audit or {})
            summary["status_after_review"] = final_status
            summary["final_status"] = final_status
            summary["submission_lane"] = submission_lane
            report["final_status"] = final_status
            report["submission_lane"] = submission_lane
            field_names = (
                "live_review_ready",
                "paper_lane_ready",
                "paper_account_id",
                "paper_account_status",
                "incubation_factory_required",
                "paper_observation_backlog_status",
                "paper_observation_handoff_warning",
                "diagnostic_observation",
                "diagnostic_lane_ready",
                "diagnostic_account_id",
                "diagnostic_account_status",
                "diagnostic_fingerprint",
                "diagnostic_guard",
                "diagnostic_reason",
                "diagnostic_reason_code",
                "diagnostic_ttl_days",
                "admission_layer",
                "live_review_account_id",
                "runtime_control_mode",
                "runtime_control_status",
                "promotion_review_id",
                "promotion_review_status",
                "promotion_review_recommendation",
                "promotion_review_score",
                "pool_admission_applied",
                "promotion_applied_transition",
                "trade_prediction_promotion_gate",
                "trade_prediction_promotion_gate_enabled",
                "trade_prediction_promotion_gate_hard_block",
                "trade_prediction_promotion_gate_reasons",
                "runtime_bootstrap_eligible",
                "runtime_bootstrap_reason",
                "runtime_bootstrap_budget_tier",
                "runtime_playbook_present",
                "runtime_contract_missing_fields",
                "wide_intake_admitted",
                "observe_intake_requested",
                "pre_observe_hard_reject_reasons",
                "strategy_type_registered",
                "formal_track_requested",
                "formal_track_auto_corrected",
                "formal_track_eligible",
                "formal_track_blockers",
                "observe_first_intake_requested",
                "planned_submission_lane",
                "planned_final_status",
                "incubation_budget_track",
                "incubation_budget_rank",
                "incubation_budget_priority_score",
                "incubation_budget_exploration_candidate",
                "execution_semantic_mode",
                "execution_semantic_gap",
                "execution_semantic_gap_reasons",
                "dsl_required",
                "dsl_compiled",
                "semantic_runtime_match",
                "runtime_family_data_source",
                "proxy_runtime_used",
                "diagnostic_only",
                "execution_readiness_tier",
                "semantic_contract_missing_fields",
                "trade_prediction_contract_status",
                "trade_prediction_contract_hash",
                "trade_prediction_contract_missing_fields",
                "trade_prediction_contract_reject_reasons",
                "trade_prediction_contract_observation_gap",
                "submission_action",
                "submission_action_type",
                "submission_action_trigger",
                "submission_action_gaps",
                "submission_action_fallback_conditions",
                "submission_action_next_step",
                "submission_action_completed",
            )
            clearable_fields = {"submission_action_next_step"}
            for field_name in field_names:
                if field_name not in audit:
                    continue
                value = audit.get(field_name)
                if value in (None, [], {}, "") and field_name not in clearable_fields:
                    continue
                summary[field_name] = value
                report[field_name] = value
            report["summary"] = summary
            return report

        async def _enqueue_paper_observation(
            self,
            db,
            strategy: dict,
            snapshot: dict,
        ) -> dict:
            paper_account_id = None
            paper_account_status = None
            handoff_warning = None
            incubation_gateway = self._get_incubation_gateway()

            try:
                binding = await incubation_gateway.ensure_account(
                    db,
                    strategy,
                    source_run_id=snapshot.get("date"),
                    stage="paper",
                )
                paper_account_id = (
                    (binding or {}).get("account_id")
                    or (binding or {}).get("id")
                    or ((binding or {}).get("account") or {}).get("id")
                    or ((binding or {}).get("account") or {}).get("account_id")
                    or ((binding or {}).get("binding") or {}).get("account_id")
                    or ((binding or {}).get("binding") or {}).get("id")
                )
                if paper_account_id:
                    updated = await self._call_optional_db_method(
                        db,
                        "update_paper_account_status",
                        paper_account_id,
                        "active",
                        stage="paper",
                        promotion_candidate=False,
                    )
                    paper_account_status = (updated or {}).get("status") if isinstance(updated, dict) else "active"
            except Exception as exc:
                handoff_warning = "paper_observation_account_unavailable"
                logger.warning("StrategyFactory: ensure paper observation account failed for %s: %s", strategy.get("id"), exc)

            paper_lane_ready = bool(paper_account_id)
            if not paper_lane_ready and not handoff_warning:
                handoff_warning = "paper_observation_account_unavailable"
            return {
                "paper_lane_ready": paper_lane_ready,
                "paper_account_id": paper_account_id,
                "paper_account_status": paper_account_status or ("active" if paper_account_id else None),
                "incubation_factory_required": True,
                "paper_observation_backlog_status": "queued" if paper_lane_ready else "handoff_failed",
                "paper_observation_handoff_warning": handoff_warning,
            }

        async def _enqueue_diagnostic_observation(
            self,
            db,
            strategy: dict,
            snapshot: dict,
        ) -> dict:
            diagnostic_account_id = None
            diagnostic_account_status = None
            incubation_gateway = self._get_incubation_gateway()
            trace = dict(strategy.get("_closure_trace") or {})
            trace.update(
                {
                    "admission_layer": "diagnostic",
                    "diagnostic_observation": True,
                    "diagnostic_reason": strategy.get("diagnostic_reason")
                    or trace.get("diagnostic_reason"),
                    "diagnostic_reason_code": strategy.get("diagnostic_reason_code")
                    or trace.get("diagnostic_reason_code")
                    or strategy.get("diagnostic_reason")
                    or trace.get("diagnostic_reason"),
                    "diagnostic_fingerprint": strategy.get("diagnostic_fingerprint")
                    or trace.get("diagnostic_fingerprint")
                    or dict(strategy.get("params") or {}).get("diagnostic_fingerprint"),
                    "diagnostic_guard": strategy.get("diagnostic_guard")
                    or trace.get("diagnostic_guard")
                    or dict(strategy.get("params") or {}).get("diagnostic_guard"),
                    "diagnostic_ttl_days": strategy.get("diagnostic_ttl_days")
                    or trace.get("diagnostic_ttl_days")
                    or _diagnostic_observation_ttl_days(),
                    "source_lane": "diagnostic_observation",
                }
            )
            strategy = {
                **dict(strategy or {}),
                "_closure_trace": trace,
            }

            try:
                binding = await incubation_gateway.ensure_account(
                    db,
                    strategy,
                    source_run_id=snapshot.get("date"),
                    stage="diagnostic",
                )
                diagnostic_account_id = (
                    ((binding or {}).get("account") or {}).get("id")
                    or ((binding or {}).get("binding") or {}).get("account_id")
                )
                if diagnostic_account_id:
                    updated = await self._call_optional_db_method(
                        db,
                        "update_paper_account_status",
                        diagnostic_account_id,
                        "active",
                        stage="diagnostic",
                        promotion_candidate=False,
                    )
                    diagnostic_account_status = (updated or {}).get("status") if isinstance(updated, dict) else "active"
            except Exception as exc:
                logger.warning("StrategyFactory: ensure diagnostic observation account failed for %s: %s", strategy.get("id"), exc)

            return {
                "diagnostic_observation": True,
                "diagnostic_lane_ready": bool(diagnostic_account_id),
                "diagnostic_account_id": diagnostic_account_id,
                "diagnostic_account_status": diagnostic_account_status or ("active" if diagnostic_account_id else None),
                "diagnostic_fingerprint": trace.get("diagnostic_fingerprint"),
                "diagnostic_guard": trace.get("diagnostic_guard"),
                "diagnostic_reason": trace.get("diagnostic_reason"),
                "diagnostic_reason_code": trace.get("diagnostic_reason_code"),
                "diagnostic_ttl_days": trace.get("diagnostic_ttl_days"),
                "admission_layer": "diagnostic",
            }

        async def _enqueue_live_ready_review(
            self,
            db,
            strategy: dict,
            snapshot: dict,
            gate: dict,
            trace_context: Optional[dict[str, Any]] = None,
        ) -> dict:
            review_account_id = None
            runtime_control = None
            promotion_review = None
            trade_prediction_gate: dict[str, Any] = {}
            incubation_gateway = self._get_incubation_gateway()

            try:
                binding = await incubation_gateway.ensure_account(
                    db,
                    strategy,
                    source_run_id=snapshot.get("date"),
                    stage="candidate",
                )
                review_account_id = (
                    ((binding or {}).get("account") or {}).get("id")
                    or ((binding or {}).get("binding") or {}).get("account_id")
                )
                if review_account_id:
                    await self._call_optional_db_method(
                        db,
                        "update_paper_account_status",
                        review_account_id,
                        "active",
                        stage="candidate",
                        promotion_candidate=True,
                    )
            except Exception as exc:
                logger.warning("StrategyFactory: ensure live-ready paper account failed for %s: %s", strategy.get("id"), exc)

            try:
                runtime_control = await get_strategy_runtime_control_service().set_control(
                    db,
                    strategy,
                    control_mode="monitor",
                    source="strategy_factory_live_ready_review",
                    reason="live_candidate_ready_submission",
                    trigger_event_type="factory_live_ready_submission",
                    action_summary={
                        "submission_lane": "live_ready_review",
                        "direct_trade_candidate": bool(gate.get("live_candidate_ready")),
                    },
                    metadata={
                        "submission_lane": "live_ready_review",
                        "snapshot_date": snapshot.get("date"),
                        "admission_stage": gate.get("admission_stage"),
                        "incubation_pass_mode": gate.get("incubation_pass_mode"),
                    },
                    apply_runtime_changes=True,
                )
            except Exception as exc:
                logger.warning("StrategyFactory: set live-ready runtime control failed for %s: %s", strategy.get("id"), exc)

            try:
                params = dict((strategy or {}).get("params") or {})
                contract = dict(params.get("trade_prediction_contract") or {})
                stock_code = (
                    contract.get("stock_code")
                    or params.get("stock_code")
                    or (strategy or {}).get("stock_code")
                )
                trade_prediction_gate = await evaluate_trade_prediction_promotion_gate(
                    db,
                    strategy_id=str((strategy or {}).get("id") or "").strip() or None,
                    stock_code=str(stock_code or "").strip() or None,
                )
            except Exception as exc:
                logger.warning("StrategyFactory: trade prediction promotion gate failed for %s: %s", strategy.get("id"), exc)
                trade_prediction_gate = {
                    "object": "strategy_factory.trade_prediction_promotion_gate",
                    "enabled": False,
                    "diagnostic_only": True,
                    "passed": True,
                    "diagnostic_passed": False,
                    "hard_block": False,
                    "degraded": True,
                    "error": str(exc),
                    "reasons": ["trade_prediction_gate_error"],
                }

            promotion_auto_apply = not bool(trade_prediction_gate.get("hard_block"))
            try:
                promotion_review = await get_strategy_promotion_pipeline_service().review(
                    db,
                    strategy,
                    source="strategy_factory_live_ready_review",
                    auto_apply=promotion_auto_apply,
                )
            except Exception as exc:
                logger.warning("StrategyFactory: trigger live-ready promotion review failed for %s: %s", strategy.get("id"), exc)

            review_payload = dict((promotion_review or {}).get("review") or {})
            applied_transition = dict((promotion_review or {}).get("applied_transition") or {})
            applied_status = str(applied_transition.get("to") or "").strip().lower()
            if bool(trade_prediction_gate.get("hard_block")) and applied_status:
                applied_transition = {}
                applied_status = ""
            action_audit: dict[str, Any] = {}
            if applied_status:
                action_audit["final_status"] = applied_status
                action_audit["pool_admission_applied"] = applied_status == "listed"
                action_audit["promotion_applied_transition"] = applied_transition
            if applied_status == "listed":
                action_audit.update(
                    {
                        "submission_action": {
                            "type": "pool_admission",
                            "trigger_reason": "live_candidate_ready_pool_admission",
                            "gaps": [],
                            "fallback_conditions": ["return_to_runtime_review_if_post_admission_controls_fail"],
                            "next_step": None,
                            "submission_lane": "live_ready_review",
                            "final_status": applied_status,
                            "completed": True,
                        },
                        "submission_action_type": "pool_admission",
                        "submission_action_trigger": "live_candidate_ready_pool_admission",
                        "submission_action_gaps": [],
                        "submission_action_fallback_conditions": [
                            "return_to_runtime_review_if_post_admission_controls_fail"
                        ],
                        "submission_action_next_step": None,
                        "submission_action_completed": True,
                    }
                )
            return {
                "live_review_ready": bool(review_account_id or runtime_control or review_payload),
                "paper_account_id": review_account_id,
                "live_review_account_id": review_account_id,
                "runtime_control_mode": (runtime_control or {}).get("control_mode"),
                "runtime_control_status": (runtime_control or {}).get("status"),
                "promotion_review_id": review_payload.get("id"),
                "promotion_review_status": review_payload.get("status"),
                "promotion_review_recommendation": review_payload.get("recommendation"),
                "promotion_review_score": review_payload.get("score"),
                "trade_prediction_promotion_gate": trade_prediction_gate,
                "trade_prediction_promotion_gate_enabled": bool(trade_prediction_gate.get("enabled")),
                "trade_prediction_promotion_gate_hard_block": bool(trade_prediction_gate.get("hard_block")),
                "trade_prediction_promotion_gate_reasons": list(trade_prediction_gate.get("reasons") or []),
                **action_audit,
            }

        async def _handle_existing_refresh(
            self,
            strategy_id: str,
            name: str,
            candidate: dict,
            gate: dict,
            quality_report: dict,
            backtest_metrics: Optional[dict],
            snapshot: dict,
            validation_report: Optional[dict],
            risk_report: Optional[dict],
            db,
            *,
            existing_status: str,
            submission_lane: str,
            submission_action: Optional[dict[str, Any]] = None,
        ) -> dict:
            """复用已有策略时，仅刷新质量报告与实验留痕。"""
            self._apply_submission_action_audit(
                quality_report,
                final_status=existing_status,
                submission_lane=submission_lane,
                submission_audit=dict(submission_action or {}),
            )
            await self._record_experiment(
                db,
                candidate,
                strategy_id,
                name,
                snapshot,
                gate,
                "accepted" if gate.get("passed") else "rejected",
                validation_report,
                risk_report,
                quality_report,
                backtest_metrics,
                None,
            )
            return {
                "refreshed_existing": True,
                "reused_existing_strategy_id": strategy_id,
                "existing_status": existing_status,
                "submission_lane": submission_lane,
                "final_status": existing_status,
                **dict(submission_action or {}),
            }
# --- runner_parts/post_gate.py ---
        async def _handle_post_gate(
            self,
            strategy_id: str,
            name: str,
            candidate: dict,
            data: dict,
            gate: dict,
            quality_report: dict,
            backtest_metrics: Optional[dict],
            snapshot: dict,
            validation_report: Optional[dict],
            risk_report: Optional[dict],
            db,
            submission_lane: str,
            submission_action: Optional[dict[str, Any]] = None,
        ) -> dict:
            """质检通过后的生命周期动作统一交给 coordinator 编排。"""
            from ..services.lifecycle_coordinator import LifecycleTransitionRequest

            incubation_budget = dict(candidate.get("incubation_budget") or {})
            incubation_budget_track = str(incubation_budget.get("track") or "formal_incubation").strip().lower()
            lifecycle_result = await self._lifecycle_coordinator.execute(
                db,
                LifecycleTransitionRequest(
                    strategy_id=strategy_id,
                    name=name,
                    candidate=candidate,
                    data=data,
                    gate=gate,
                    quality_report=quality_report,
                    snapshot=snapshot,
                    submission_lane=submission_lane,
                    submission_action=dict(submission_action or {}),
                    backtest_metrics=backtest_metrics,
                    validation_report=validation_report,
                    risk_report=risk_report,
                    factory_run_id=candidate.get("factory_run_id") or snapshot.get("factory_run_id"),
                    trace_id=candidate.get("trace_id"),
                    correlation_id=(
                        candidate.get("correlation_id")
                        or candidate.get("trace_id")
                        or snapshot.get("correlation_id")
                    ),
                    parent_task_run_id=candidate.get("task_run_id") or dict(candidate.get("params") or {}).get("task_run_id"),
                    source_action="strategy_factory_submit",
                    snapshot_date=snapshot.get("date"),
                    quality_gate_summary=dict(quality_report.get("summary") or {}),
                ),
            )
            lifecycle_payload = lifecycle_result.to_dict()
            incubation_binding = lifecycle_payload.get("incubation_binding")
            incubation_pipeline = lifecycle_payload.get("incubation_pipeline")
            vector_profile = lifecycle_payload.get("vector_profile")
            vector_audit = dict(lifecycle_payload.get("vector_audit") or {})
            live_review_action = dict(lifecycle_payload.get("live_review_action") or {})
            paper_action = dict(lifecycle_payload.get("paper_action") or {})
            diagnostic_action = dict(lifecycle_payload.get("diagnostic_action") or {})
            action_audit = {
                **dict(submission_action or {}),
                **dict(lifecycle_payload.get("action_audit") or {}),
            }
            final_status = str(lifecycle_payload.get("final_status") or "rejected")

            self._apply_submission_action_audit(
                quality_report,
                final_status=final_status,
                submission_lane=submission_lane,
                submission_audit=action_audit,
            )

            if gate.get("passed"):
                await self._record_experiment(
                    db,
                    candidate,
                    strategy_id,
                    name,
                    snapshot,
                    gate,
                    "accepted",
                    validation_report,
                    risk_report,
                    quality_report,
                    backtest_metrics,
                    incubation_pipeline,
                )
            else:
                await self._record_experiment(
                    db,
                    candidate,
                    strategy_id,
                    name,
                    snapshot,
                    gate,
                    "rejected",
                    validation_report,
                    risk_report,
                    quality_report,
                    backtest_metrics,
                    None,
                )

            return {
                "incubation_account_id": ((incubation_binding or {}).get("account") or {}).get("id"),
                "incubation_pipeline_stage": ((incubation_pipeline or {}).get("snapshot") or {}).get("pipeline_stage"),
                "incubation_pipeline_status": ((incubation_pipeline or {}).get("snapshot") or {}).get("pipeline_status"),
                "incubation_readiness_score": ((incubation_pipeline or {}).get("snapshot") or {}).get("readiness_score"),
                "incubation_task_run_id": (incubation_pipeline or {}).get("task_run_id"),
                "incubation_budget_track": incubation_budget_track,
                "incubation_budget_rank": incubation_budget.get("rank"),
                "incubation_budget_priority_score": incubation_budget.get("priority_score"),
                "submission_lane": submission_lane,
                "vector_profile_id": (vector_profile or {}).get("id"),
                "vector_backend": (vector_profile or {}).get("backend"),
                "vector_backend_requested": (vector_audit or {}).get("backend_requested"),
                "vector_backend_used": (vector_audit or {}).get("backend_used"),
                "vector_fallback_used": (vector_audit or {}).get("fallback_used"),
                "vector_fallback_reason": (vector_audit or {}).get("fallback_reason"),
                "vector_latency_ms": (vector_audit or {}).get("latency_ms"),
                "execution_audit_snapshot_id": lifecycle_payload.get("execution_audit_snapshot_id"),
                "correlation_id": lifecycle_payload.get("correlation_id"),
                "trace_id": lifecycle_payload.get("trace_id"),
                "factory_run_id": lifecycle_payload.get("factory_run_id"),
                "parent_task_run_id": lifecycle_payload.get("parent_task_run_id"),
                "lifecycle_task_run_id": ((lifecycle_payload.get("lifecycle_task_run") or {}).get("id")),
                "lifecycle_transition_steps": lifecycle_payload.get("steps") or [],
                **paper_action,
                **diagnostic_action,
                **live_review_action,
                **action_audit,
                "final_status": final_status,
            }

        @classmethod
        def _build_replay_candidate(
            cls,
            strategy: dict,
            *,
            latest_report: Optional[dict],
            backtest_metrics: Optional[dict],
        ) -> dict[str, Any]:
            payload = dict(strategy or {})
            params = dict(payload.get("params") or {})
            report = dict(latest_report or {})
            summary = dict(report.get("summary") or {})
            quality_gate = dict(report.get("quality_gate") or {})
            candidate = apply_resolved_candidate_envelope(
                {
                    **payload,
                    "id": payload.get("id"),
                    "name": payload.get("name"),
                    "strategy_type": payload.get("strategy_type"),
                    "params": params,
                    "spawn_reason": (
                        summary.get("spawn_reason")
                        or payload.get("spawn_reason")
                        or params.get("spawn_reason")
                    ),
                    "dedup_result": dict(
                        report.get("dedup_report")
                        or payload.get("dedup_result")
                        or params.get("dedup_result")
                        or {}
                    ),
                    "committee_review": dict(
                        report.get("committee_review")
                        or summary.get("committee_review")
                        or payload.get("committee_review")
                        or params.get("committee_review")
                        or {}
                    ),
                    "backtest_metrics": dict(backtest_metrics or report.get("backtest_metrics") or {}),
                }
            )
            incubation_budget = dict(
                candidate.get("incubation_budget")
                or params.get("incubation_budget")
                or summary.get("incubation_budget")
                or {}
            )
            summary_track = str(summary.get("incubation_budget_track") or "").strip()
            if summary_track and not incubation_budget.get("track"):
                incubation_budget["track"] = summary_track
            if summary.get("incubation_budget_rank") is not None and incubation_budget.get("rank") is None:
                incubation_budget["rank"] = summary.get("incubation_budget_rank")
            if (
                summary.get("incubation_budget_priority_score") is not None
                and incubation_budget.get("priority_score") is None
            ):
                incubation_budget["priority_score"] = summary.get("incubation_budget_priority_score")
            if (
                summary.get("incubation_budget_exploration_candidate") is not None
                and incubation_budget.get("exploration_candidate") is None
            ):
                incubation_budget["exploration_candidate"] = bool(
                    summary.get("incubation_budget_exploration_candidate")
                )
            if incubation_budget:
                candidate["incubation_budget"] = incubation_budget
            for field_name in _SEMANTIC_CONTRACT_FIELDS:
                value = candidate.get(field_name)
                if value in (None, "", [], {}):
                    value = params.get(field_name)
                if value in (None, "", [], {}):
                    value = summary.get(field_name)
                if value in (None, "", [], {}):
                    value = quality_gate.get(field_name)
                if value not in (None, "", [], {}):
                    candidate[field_name] = value
            return cls._ensure_runtime_playbook(candidate)
# --- runner_parts/orchestrator.py ---

        async def replay_existing_submission(
            self,
            strategy: dict,
            snapshot: dict,
            db,
            *,
            validation_report: Optional[dict] = None,
            risk_report: Optional[dict] = None,
            backtest_metrics: Optional[dict] = None,
            latest_report: Optional[dict] = None,
            read_only: bool = False,
        ) -> dict[str, Any]:
            strategy_payload = dict(strategy or {})
            strategy_id = str(strategy_payload.get("id") or "").strip()
            if not strategy_id:
                raise ValueError("strategy_id is required for replay_existing_submission")
            gate3_record_only = bool(_gate3_record_only_enabled())
            execution_options = SubmissionExecutionOptions(read_only=bool(read_only or gate3_record_only))
            quality_report_options = SubmissionExecutionOptions(
                read_only=read_only,
                record_only=gate3_record_only,
            )
            name = str(strategy_payload.get("name") or strategy_id).strip()
            report = dict(latest_report or {})
            metrics = dict(backtest_metrics or report.get("backtest_metrics") or {})
            candidate = self._build_replay_candidate(
                strategy_payload,
                latest_report=report,
                backtest_metrics=metrics,
            )
            semantic_audit: dict[str, Any] = {}
            if _semantic_contract_feature_enabled():
                candidate = ensure_candidate_semantic_contract(candidate)
                candidate["confidence_contract"] = synthesize_confidence_contract(candidate)
                semantic_audit = audit_candidate_semantic_contract(candidate)
                candidate = _apply_candidate_semantic_contract(candidate, semantic_audit)
            incubation_budget = dict(candidate.get("incubation_budget") or {})
            incubation_budget_track = str(
                incubation_budget.get("track")
                or dict(report.get("summary") or {}).get("incubation_budget_track")
                or "formal_incubation"
            ).strip().lower() or "formal_incubation"
            run_submission_quality_gate = _local_run_submission_quality_gate
            gate = await run_submission_quality_gate(
                db,
                {**strategy_payload, "status": strategy_payload.get("status")},
                validation_report=validation_report,
                risk_report=risk_report,
                backtest_metrics={
                    **dict(metrics or {}),
                    "trade_count": metrics.get("trade_count"),
                    "trades_count": metrics.get("trades_count"),
                },
                incubation_budget_track=incubation_budget_track,
            )
            gate = self._apply_factory_submission_policy(
                candidate,
                name=name,
                gate=gate,
                backtest_metrics=metrics,
                refresh_existing=False,
            )
            if _semantic_contract_feature_enabled():
                gate = _apply_semantic_contract_gate(gate, semantic_audit)
            submission_action = self._resolve_submission_action_plan(
                gate,
                candidate=candidate,
                refresh_existing=False,
                existing_status=str(strategy_payload.get("status") or "submitted"),
                incubation_budget_track=incubation_budget_track,
                read_only=read_only,
            )
            submission_lane = str(submission_action.get("submission_lane") or "deferred_submission")
            final_status = str(submission_action.get("final_status") or strategy_payload.get("status") or "submitted")
            candidate = self._apply_runtime_bootstrap_contract(
                candidate,
                submission_lane=submission_lane,
                runtime_bootstrap_eligible=bool(submission_action.get("runtime_bootstrap_eligible")),
                runtime_bootstrap_budget_tier=str(submission_action.get("runtime_bootstrap_budget_tier") or "") or None,
            )
            strategy_payload = {
                **strategy_payload,
                "params": {
                    **dict(strategy_payload.get("params") or {}),
                    "runtime_playbook": dict(candidate.get("runtime_playbook") or {}),
                },
            }
            quality_report = self._build_quality_report(
                strategy_id=strategy_id,
                candidate=candidate,
                snapshot=snapshot,
                backtest_metrics=metrics,
                quality_gate=gate,
                validation_report=validation_report,
                risk_report=risk_report,
                final_status=final_status,
                submission_lane=submission_lane,
            )
            quality_report = _enrich_quality_report_v2(
                quality_report,
                candidate=candidate,
                gate=gate,
                final_status=final_status,
            )
            quality_summary = dict(quality_report.get("summary") or {})
            if gate3_record_only:
                gate3_quality_record = _build_gate3_quality_record_contract(
                    gate=gate,
                    quality_summary=quality_summary,
                    read_only=read_only,
                    gate3_record_only=gate3_record_only,
                )
                planned_submission_lane = submission_lane
                planned_final_status = final_status
                record_status = "gate3_record_passed" if bool(gate.get("passed")) else "gate3_record_failed"
                record_action = dict(submission_action.get("submission_action") or {})
                record_action.update(
                    {
                        "type": "gate3_record_only",
                        "trigger_reason": "gate3_record_only",
                        "next_step": "await_manual_record_review",
                        "completed": False,
                        "planned_submission_lane": planned_submission_lane,
                        "planned_final_status": planned_final_status,
                        "record_status": record_status,
                        "strategy_created": False,
                        "lifecycle_action_executed": False,
                    }
                )
                submission_lane = "gate3_record_only"
                final_status = "gate3_recorded"
                post_gate = {
                    "submission_lane": submission_lane,
                    "final_status": final_status,
                    "planned_submission_lane": planned_submission_lane,
                    "planned_final_status": planned_final_status,
                    "gate3_record_only": True,
                    "record_only": True,
                    "gate_3_recorded": bool(not read_only),
                    "gate3_record_status": record_status,
                    **gate3_quality_record,
                    "quality_report_record_only": True,
                    "quality_report_persisted": False,
                    "gate3_audit_evidence_persisted": False,
                    "record_only_scope": "gate3_audit_evidence",
                    "automatic_downstream_action": False,
                    "strategy_created": False,
                    "lifecycle_action_executed": False,
                    "submission_action": record_action,
                    "submission_action_type": "gate3_record_only",
                    "submission_action_trigger": "gate3_record_only",
                    "submission_action_gaps": list(submission_action.get("submission_action_gaps") or []),
                    "submission_action_fallback_conditions": list(
                        submission_action.get("submission_action_fallback_conditions") or []
                    ),
                    "submission_action_next_step": "await_manual_record_review",
                    "submission_action_completed": False,
                    "admission_decision": submission_action.get("admission_decision"),
                }
                self._apply_submission_action_audit(
                    quality_report,
                    final_status=final_status,
                    submission_lane=submission_lane,
                    submission_audit=post_gate,
                )
                if not read_only:
                    await self._record_experiment(
                        db,
                        candidate,
                        strategy_id,
                        name,
                        snapshot,
                        gate,
                        record_status,
                        validation_report,
                        risk_report,
                        quality_report,
                        metrics,
                        None,
                    )
                    evidence_record = await self._record_gate3_audit_evidence(
                        db,
                        candidate,
                        strategy_id,
                        snapshot,
                        gate,
                        quality_report,
                        metrics,
                        status=record_status,
                        planned_submission_lane=planned_submission_lane,
                        planned_final_status=planned_final_status,
                    )
                    if evidence_record:
                        post_gate["gate3_audit_evidence_id"] = evidence_record.get("id")
                        post_gate["gate3_audit_evidence_persisted"] = True
            else:
                post_gate = await self._submission_coordinator.handle_new_candidate(
                    strategy_id=strategy_id,
                    name=name,
                    candidate=candidate,
                    data=strategy_payload,
                    gate=gate,
                    quality_report=quality_report,
                    backtest_metrics=metrics,
                    snapshot=snapshot,
                    validation_report=validation_report,
                    risk_report=risk_report,
                    db=db,
                    submission_lane=submission_lane,
                    submission_action=submission_action,
                    options=execution_options,
                )
            final_status = str(post_gate.get("final_status") or final_status)
            submission_lane = str(post_gate.get("submission_lane") or submission_lane)
            await self._submission_coordinator.save_quality_report(
                db,
                strategy_id,
                quality_report,
                options=quality_report_options,
            )
            return {
                "strategy_id": strategy_id,
                "name": name,
                "status": final_status,
                "submission_lane": submission_lane,
                "incubation_budget_track": incubation_budget_track,
                "admission_decision_contract_version": submission_action.get(
                    "admission_decision_contract_version"
                ),
                "admission_decision": submission_action.get("admission_decision"),
                "gate": dict(gate or {}),
                "quality_report": quality_report,
                "diagnostic_only": bool(read_only),
                **dict(post_gate or {}),
            }

        @classmethod
        async def _record_experiment(
            cls,
            db,
            candidate: dict,
            strategy_id: str,
            name: str,
            snapshot: dict,
            gate: dict,
            status: str,
            validation_report: Optional[dict],
            risk_report: Optional[dict],
            quality_report: Optional[dict],
            backtest_metrics: Optional[dict],
            incubation_pipeline: Optional[dict],
        ) -> None:
            """记录策略生成实验（通过/未通过共用）。"""
            experiment_id = candidate.get("experiment_id")
            if not experiment_id or not hasattr(db, "save_strategy_generation_experiment"):
                return

            def _assign_if_present(target: dict, key: str, value) -> None:
                if value not in (None, [], {}, ""):
                    target[key] = value

            try:
                existing = await db.get_strategy_generation_experiment(experiment_id) if hasattr(db, "get_strategy_generation_experiment") else None
                existing = dict(existing or {})
                existing_spec = dict(existing.get("strategy_spec") or {})
                existing_evaluation = dict(existing.get("evaluation") or {})
                existing_result = dict(existing.get("result") or {})
                candidate_provenance = cls._candidate_provenance(candidate)
                quality_payload = compact_json(quality_report or {})
                quality_payload = dict(quality_payload or {}) if isinstance(quality_payload, dict) else {}
                backtest_payload = compact_scalar_metrics(backtest_metrics or {})
                compact_gate = compact_json(gate or {})
                compact_gate = dict(compact_gate or {}) if isinstance(compact_gate, dict) else {}
                event_window_config = dict(
                    quality_payload.get("event_window_config")
                    or backtest_payload.get("event_window_config")
                    or {}
                )
                event_window_metrics = compact_event_window_metrics(
                    quality_payload.get("event_window_metrics")
                    or backtest_payload.get("event_window_metrics")
                    or {}
                )
                cost_assumptions = dict(
                    quality_payload.get("cost_assumptions")
                    or backtest_payload.get("cost_assumptions")
                    or {}
                )
                backtest_assumptions = dict(
                    quality_payload.get("backtest_assumptions")
                    or backtest_payload.get("backtest_assumptions")
                    or {}
                )
                execution_reality = dict(quality_payload.get("execution_reality") or {})
                quality_summary = dict(quality_payload.get("summary") or {})

                research_task = _normalize_research_task_contract(
                    dict(candidate.get("research_task") or {})
                    or dict(existing_spec.get("research_task") or {})
                    or dict(existing_evaluation.get("research_task") or {})
                )
                contract_snapshot = dict(candidate.get("candidate_contract_snapshot") or {})
                try:
                    contract_snapshot = build_portfolio_candidate_contract(
                        {
                            **dict(candidate or {}),
                            "research_task": research_task,
                        }
                    )
                except Exception as exc:
                    logger.warning(
                        "StrategySubmitter: rebuild candidate contract snapshot failed for %s: %s",
                        strategy_id,
                        exc,
                    )
                    if research_task:
                        contract_snapshot = {
                            **contract_snapshot,
                            "research_task": research_task,
                        }
                candidate_lineage_contract = dict(
                    candidate.get("candidate_lineage_contract")
                    or contract_snapshot.get("lineage")
                    or {}
                )
                event_context = (
                    dict(candidate.get("event_context") or {})
                    or _extract_event_context(research_task)
                    or dict(existing_spec.get("event_context") or {})
                    or dict(existing_evaluation.get("event_context") or {})
                )
                parameters = {
                    **dict(existing.get("parameters") or {}),
                    **dict(candidate.get("params") or {}),
                }
                _assign_if_present(parameters, "target_symbols", list(candidate.get("target_symbols") or parameters.get("target_symbols") or []))
                _assign_if_present(parameters, "stock_pool", dict(candidate.get("stock_pool") or parameters.get("stock_pool") or {}))
                _assign_if_present(parameters, "research_task", research_task)
                _assign_if_present(parameters, "event_context", event_context)
                _assign_if_present(parameters, "candidate_contract_snapshot", contract_snapshot)
                _assign_if_present(parameters, "candidate_lineage_contract", candidate_lineage_contract)
                _assign_if_present(parameters, "candidate_provenance", candidate_provenance)
                _assign_if_present(parameters, "source_candidate_artifact_id", candidate_provenance.get("source_candidate_artifact_id"))
                _assign_if_present(parameters, "candidate_family", candidate_provenance.get("candidate_family"))
                _assign_if_present(parameters, "strategy_profile", candidate_provenance.get("strategy_profile"))
                _assign_if_present(parameters, "candidate_family_id", candidate_provenance.get("candidate_family_id"))
                _assign_if_present(parameters, "holding_period_bucket", candidate_provenance.get("holding_period_bucket"))
                _assign_if_present(parameters, "alpha_source", candidate_provenance.get("alpha_source"))
                _assign_if_present(parameters, "risk_level", candidate_provenance.get("risk_level"))
                _assign_if_present(parameters, "regime_fit", candidate_provenance.get("regime_fit"))
                _assign_if_present(parameters, "generator_mode", candidate_provenance.get("generator_mode"))
                _assign_if_present(parameters, "direction_bias", candidate_provenance.get("direction_bias"))
                _assign_if_present(parameters, "validation_profile_name", candidate_provenance.get("validation_profile"))
                _assign_if_present(parameters, "target_symbol_count", candidate_provenance.get("target_symbol_count"))
                _assign_if_present(parameters, "candidate_contract_hash", candidate.get("candidate_contract_hash"))
                _assign_if_present(parameters, "execution_contract_hash", candidate.get("execution_contract_hash"))
                _assign_if_present(parameters, "tested_object_hash", candidate.get("tested_object_hash"))
                _assign_if_present(parameters, "candidate_identity_signature", candidate.get("candidate_identity_signature"))
                _assign_if_present(parameters, "logic_signature", candidate.get("logic_signature"))
                _assign_if_present(parameters, "dsl_signature", candidate.get("dsl_signature"))
                _assign_if_present(parameters, "factor_signature", candidate.get("factor_signature"))
                _assign_if_present(parameters, "entry_exit_signature", candidate.get("entry_exit_signature"))
                for field_name in _SEMANTIC_CONTRACT_FIELDS:
                    _assign_if_present(parameters, field_name, candidate.get(field_name))

                strategy_spec = dict(existing_spec)
                _assign_if_present(strategy_spec, "strategy_type", candidate.get("strategy_type"))
                _assign_if_present(strategy_spec, "name", name)
                _assign_if_present(strategy_spec, "params", candidate.get("params") or existing_spec.get("params"))
                _assign_if_present(strategy_spec, "target_symbols", list(candidate.get("target_symbols") or existing_spec.get("target_symbols") or []))
                _assign_if_present(strategy_spec, "stock_pool", dict(candidate.get("stock_pool") or existing_spec.get("stock_pool") or {}))
                _assign_if_present(strategy_spec, "selection_logic", list(candidate.get("selection_logic") or existing_spec.get("selection_logic") or []))
                _assign_if_present(strategy_spec, "research_scope", dict(candidate.get("research_scope") or existing_spec.get("research_scope") or {}))
                _assign_if_present(strategy_spec, "research_task", research_task)
                _assign_if_present(strategy_spec, "event_context", event_context)
                _assign_if_present(strategy_spec, "candidate_provenance", candidate_provenance)
                _assign_if_present(strategy_spec, "source_candidate_artifact_id", candidate_provenance.get("source_candidate_artifact_id"))
                _assign_if_present(strategy_spec, "candidate_family", candidate_provenance.get("candidate_family"))
                _assign_if_present(strategy_spec, "strategy_profile", candidate_provenance.get("strategy_profile"))
                _assign_if_present(strategy_spec, "candidate_family_id", candidate_provenance.get("candidate_family_id"))
                _assign_if_present(strategy_spec, "holding_period_bucket", candidate_provenance.get("holding_period_bucket"))
                _assign_if_present(strategy_spec, "alpha_source", candidate_provenance.get("alpha_source"))
                _assign_if_present(strategy_spec, "risk_level", candidate_provenance.get("risk_level"))
                _assign_if_present(strategy_spec, "regime_fit", candidate_provenance.get("regime_fit"))
                _assign_if_present(strategy_spec, "generator_mode", candidate_provenance.get("generator_mode"))
                _assign_if_present(strategy_spec, "direction_bias", candidate_provenance.get("direction_bias"))
                _assign_if_present(strategy_spec, "validation_profile_name", candidate_provenance.get("validation_profile"))
                _assign_if_present(strategy_spec, "target_symbol_count", candidate_provenance.get("target_symbol_count"))
                _assign_if_present(strategy_spec, "candidate_contract_hash", candidate.get("candidate_contract_hash"))
                _assign_if_present(strategy_spec, "execution_contract_hash", candidate.get("execution_contract_hash"))
                _assign_if_present(strategy_spec, "tested_object_hash", candidate.get("tested_object_hash"))
                _assign_if_present(strategy_spec, "candidate_identity_signature", candidate.get("candidate_identity_signature"))
                _assign_if_present(strategy_spec, "candidate_contract_snapshot", contract_snapshot)
                _assign_if_present(strategy_spec, "candidate_lineage_contract", candidate_lineage_contract)
                _assign_if_present(strategy_spec, "logic_signature", candidate.get("logic_signature"))
                _assign_if_present(strategy_spec, "dsl_signature", candidate.get("dsl_signature"))
                _assign_if_present(strategy_spec, "factor_signature", candidate.get("factor_signature"))
                _assign_if_present(strategy_spec, "entry_exit_signature", candidate.get("entry_exit_signature"))
                for field_name in _SEMANTIC_CONTRACT_FIELDS:
                    _assign_if_present(strategy_spec, field_name, candidate.get(field_name))

                evaluation = dict(existing_evaluation)
                _assign_if_present(evaluation, "generation_reason", candidate.get("generation_reason") or existing_evaluation.get("generation_reason"))
                _assign_if_present(evaluation, "llm_prompt", candidate.get("llm_prompt") or existing_evaluation.get("llm_prompt"))
                _assign_if_present(evaluation, "llm_response", candidate.get("llm_response") or existing_evaluation.get("llm_response"))
                _assign_if_present(evaluation, "target_symbols", list(candidate.get("target_symbols") or strategy_spec.get("target_symbols") or []))
                _assign_if_present(evaluation, "stock_pool", dict(candidate.get("stock_pool") or strategy_spec.get("stock_pool") or {}))
                _assign_if_present(evaluation, "selection_logic", list(candidate.get("selection_logic") or strategy_spec.get("selection_logic") or []))
                _assign_if_present(evaluation, "research_scope", dict(candidate.get("research_scope") or strategy_spec.get("research_scope") or {}))
                _assign_if_present(evaluation, "research_task", research_task)
                _assign_if_present(evaluation, "event_context", event_context)
                _assign_if_present(evaluation, "candidate_provenance", candidate_provenance)
                _assign_if_present(evaluation, "source_candidate_artifact_id", candidate_provenance.get("source_candidate_artifact_id"))
                _assign_if_present(evaluation, "candidate_family", candidate_provenance.get("candidate_family"))
                _assign_if_present(evaluation, "strategy_profile", candidate_provenance.get("strategy_profile"))
                _assign_if_present(evaluation, "candidate_family_id", candidate_provenance.get("candidate_family_id"))
                _assign_if_present(evaluation, "holding_period_bucket", candidate_provenance.get("holding_period_bucket"))
                _assign_if_present(evaluation, "alpha_source", candidate_provenance.get("alpha_source"))
                _assign_if_present(evaluation, "risk_level", candidate_provenance.get("risk_level"))
                _assign_if_present(evaluation, "regime_fit", candidate_provenance.get("regime_fit"))
                _assign_if_present(evaluation, "generator_mode", candidate_provenance.get("generator_mode"))
                _assign_if_present(evaluation, "direction_bias", candidate_provenance.get("direction_bias"))
                _assign_if_present(evaluation, "validation_profile_name", candidate_provenance.get("validation_profile"))
                _assign_if_present(evaluation, "target_symbol_count", candidate_provenance.get("target_symbol_count"))
                _assign_if_present(evaluation, "candidate_contract_hash", candidate.get("candidate_contract_hash"))
                _assign_if_present(evaluation, "execution_contract_hash", candidate.get("execution_contract_hash"))
                _assign_if_present(evaluation, "tested_object_hash", candidate.get("tested_object_hash"))
                _assign_if_present(evaluation, "candidate_identity_signature", candidate.get("candidate_identity_signature"))
                _assign_if_present(evaluation, "candidate_contract_snapshot", contract_snapshot)
                _assign_if_present(evaluation, "candidate_lineage_contract", candidate_lineage_contract)
                _assign_if_present(evaluation, "logic_signature", candidate.get("logic_signature"))
                _assign_if_present(evaluation, "dsl_signature", candidate.get("dsl_signature"))
                _assign_if_present(evaluation, "factor_signature", candidate.get("factor_signature"))
                _assign_if_present(evaluation, "entry_exit_signature", candidate.get("entry_exit_signature"))
                for field_name in _SEMANTIC_CONTRACT_FIELDS:
                    _assign_if_present(evaluation, field_name, candidate.get(field_name))
                evaluation["quality_gate"] = compact_gate
                _assign_if_present(evaluation, "quality_gate_summary", compact_gate)
                if validation_report is not None or "validation_report" not in evaluation:
                    evaluation["validation_report"] = compact_json(validation_report or {})
                if risk_report is not None or "risk_report" not in evaluation:
                    evaluation["risk_report"] = compact_json(risk_report or {})
                _assign_if_present(evaluation, "quality_summary", quality_summary)
                _assign_if_present(evaluation, "backtest_metrics", backtest_payload)
                _assign_if_present(evaluation, "event_window_config", event_window_config)
                _assign_if_present(evaluation, "event_window_metrics", event_window_metrics)
                _assign_if_present(evaluation, "position_assumption", quality_payload.get("position_assumption") or backtest_payload.get("position_assumption"))
                _assign_if_present(evaluation, "cost_assumptions", cost_assumptions)
                _assign_if_present(evaluation, "backtest_assumptions", backtest_assumptions)
                _assign_if_present(evaluation, "execution_reality", execution_reality)
                _assign_if_present(evaluation, "constraint_check", quality_payload.get("constraint_check") or backtest_payload.get("constraint_check"))
                _assign_if_present(evaluation, "admission_stage", gate.get("admission_stage"))
                _assign_if_present(evaluation, "incubation_pass_mode", gate.get("incubation_pass_mode"))
                _assign_if_present(
                    evaluation,
                    "submission_lane",
                    quality_payload.get("submission_lane") or quality_summary.get("submission_lane"),
                )
                evaluation["research_candidate_ready"] = bool(gate.get("research_candidate_ready"))
                evaluation["incubation_candidate_ready"] = bool(gate.get("incubation_candidate_ready"))
                evaluation["live_candidate_ready"] = bool(gate.get("live_candidate_ready"))
                evaluation["direct_trade_candidate"] = bool(
                    quality_payload.get("direct_trade_candidate") or quality_summary.get("direct_trade_candidate")
                )
                evaluation["admission_block_reasons"] = list(gate.get("admission_block_reasons") or [])
                evaluation["admission_evaluations"] = compact_json(gate.get("admission_evaluations") or {})

                record_only_status = str(status or "").startswith("gate3_record_")
                experiment_gate3_quality_record = (
                    _build_gate3_quality_record_contract(
                        gate=gate,
                        quality_summary=quality_summary,
                        read_only=False,
                        gate3_record_only=True,
                    )
                    if record_only_status
                    else {}
                )
                if experiment_gate3_quality_record:
                    evaluation.update(experiment_gate3_quality_record)
                    evaluation["record_only_scope"] = "gate3_audit_evidence"
                    evaluation["automatic_downstream_action"] = False
                result = dict(existing_result)
                if record_only_status:
                    result.update(
                        {
                            "candidate_id": strategy_id,
                            "strategy_id": None,
                            "generated_strategy_id": None,
                            "status": status,
                            "record_only": True,
                            "gate3_record_only": True,
                            **experiment_gate3_quality_record,
                            "record_only_scope": "gate3_audit_evidence",
                            "automatic_downstream_action": False,
                            "strategy_created": False,
                            "lifecycle_action_executed": False,
                        }
                    )
                else:
                    result.update({"strategy_id": strategy_id, "generated_strategy_id": strategy_id, "status": status})
                _assign_if_present(result, "candidate_provenance", candidate_provenance)
                _assign_if_present(result, "source_candidate_artifact_id", candidate_provenance.get("source_candidate_artifact_id"))
                _assign_if_present(result, "candidate_family", candidate_provenance.get("candidate_family"))
                _assign_if_present(result, "strategy_profile", candidate_provenance.get("strategy_profile"))
                _assign_if_present(result, "candidate_family_id", candidate_provenance.get("candidate_family_id"))
                _assign_if_present(result, "holding_period_bucket", candidate_provenance.get("holding_period_bucket"))
                _assign_if_present(result, "alpha_source", candidate_provenance.get("alpha_source"))
                _assign_if_present(result, "risk_level", candidate_provenance.get("risk_level"))
                _assign_if_present(result, "regime_fit", candidate_provenance.get("regime_fit"))
                _assign_if_present(result, "generator_mode", candidate_provenance.get("generator_mode"))
                _assign_if_present(result, "direction_bias", candidate_provenance.get("direction_bias"))
                _assign_if_present(result, "validation_profile_name", candidate_provenance.get("validation_profile"))
                _assign_if_present(result, "target_symbol_count", candidate_provenance.get("target_symbol_count"))
                _assign_if_present(result, "candidate_contract_hash", candidate.get("candidate_contract_hash"))
                _assign_if_present(result, "execution_contract_hash", candidate.get("execution_contract_hash"))
                _assign_if_present(result, "tested_object_hash", candidate.get("tested_object_hash"))
                _assign_if_present(result, "candidate_identity_signature", candidate.get("candidate_identity_signature"))
                _assign_if_present(result, "candidate_contract_snapshot", contract_snapshot)
                _assign_if_present(result, "candidate_lineage_contract", candidate_lineage_contract)
                _assign_if_present(result, "logic_signature", candidate.get("logic_signature"))
                _assign_if_present(result, "dsl_signature", candidate.get("dsl_signature"))
                _assign_if_present(result, "factor_signature", candidate.get("factor_signature"))
                _assign_if_present(result, "entry_exit_signature", candidate.get("entry_exit_signature"))
                for field_name in _SEMANTIC_CONTRACT_FIELDS:
                    _assign_if_present(result, field_name, candidate.get(field_name))
                _assign_if_present(result, "quality_summary", quality_summary)
                _assign_if_present(result, "backtest_metrics", backtest_payload)
                _assign_if_present(result, "event_window_config", event_window_config)
                _assign_if_present(result, "event_window_metrics", event_window_metrics)
                _assign_if_present(result, "position_assumption", quality_payload.get("position_assumption") or backtest_payload.get("position_assumption"))
                _assign_if_present(result, "cost_assumptions", cost_assumptions)
                _assign_if_present(result, "backtest_assumptions", backtest_assumptions)
                _assign_if_present(result, "execution_reality", execution_reality)
                _assign_if_present(result, "constraint_check", quality_payload.get("constraint_check") or backtest_payload.get("constraint_check"))
                _assign_if_present(result, "admission_stage", gate.get("admission_stage"))
                _assign_if_present(result, "incubation_pass_mode", gate.get("incubation_pass_mode"))
                _assign_if_present(
                    result,
                    "submission_lane",
                    quality_payload.get("submission_lane") or quality_summary.get("submission_lane"),
                )
                result["research_candidate_ready"] = bool(gate.get("research_candidate_ready"))
                result["incubation_candidate_ready"] = bool(gate.get("incubation_candidate_ready"))
                result["live_candidate_ready"] = bool(gate.get("live_candidate_ready"))
                result["direct_trade_candidate"] = bool(
                    quality_payload.get("direct_trade_candidate") or quality_summary.get("direct_trade_candidate")
                )
                result["admission_block_reasons"] = list(gate.get("admission_block_reasons") or [])
                result["admission_evaluations"] = compact_json(gate.get("admission_evaluations") or {})
                if incubation_pipeline:
                    evaluation["incubation_pipeline"] = (incubation_pipeline or {}).get("snapshot") or {}
                    result["incubation_pipeline"] = (incubation_pipeline or {}).get("snapshot") or {}

                parent_strategy_id = existing.get("parent_strategy_id") or candidate.get("parent_strategy_id")
                experiment_strategy_id = None if record_only_status else existing.get("strategy_id") or parent_strategy_id or strategy_id
                prompt_payload = existing.get("prompt") or (str(candidate.get("llm_prompt")) if candidate.get("llm_prompt") else str(snapshot.get("date") or ""))

                await db.save_strategy_generation_experiment(
                    {
                        **existing,
                        "experiment_id": experiment_id,
                        "strategy_id": experiment_strategy_id,
                        "parent_strategy_id": parent_strategy_id,
                        "generated_strategy_id": None if record_only_status else strategy_id,
                        "task_run_id": candidate.get("task_run_id") or existing.get("task_run_id"),
                        "source": candidate.get("source") or existing.get("source") or "strategy_factory",
                        "generator_type": candidate.get("generator_type") or existing.get("generator_type") or "rule",
                        "optimizer_type": candidate.get("optimizer_type") or existing.get("optimizer_type"),
                        "status": status,
                        "hypothesis": candidate.get("spawn_reason") or existing.get("hypothesis"),
                        "prompt": prompt_payload,
                        "parameters": parameters,
                        "strategy_spec": strategy_spec,
                        "evaluation": evaluation,
                        "result": result,
                        "parent_experiment_id": existing.get("parent_experiment_id"),
                        "artifact_id": existing.get("artifact_id"),
                    }
                )
            except Exception as exc:
                logger.warning("StrategySubmitter: record experiment failed for %s: %s", strategy_id, exc)

        @classmethod
        async def _record_gate3_audit_evidence(
            cls,
            db,
            candidate: dict,
            strategy_id: str,
            snapshot: dict,
            gate: dict,
            quality_report: Optional[dict],
            backtest_metrics: Optional[dict],
            *,
            status: str,
            planned_submission_lane: str,
            planned_final_status: str,
        ) -> Optional[dict]:
            if not hasattr(db, "save_factory_task_evidence"):
                return None
            research_task = _normalize_research_task_contract(candidate.get("research_task") or {})
            target_symbols = _normalize_target_codes(candidate.get("target_symbols") or [])
            task_key = str(
                research_task.get("task_key")
                or research_task.get("task_id")
                or candidate.get("task_run_id")
                or candidate.get("experiment_id")
                or strategy_id
            ).strip()
            if not task_key:
                task_key = strategy_id
            quality_summary = dict((quality_report or {}).get("summary") or {})
            gate3_quality_record = _build_gate3_quality_record_contract(
                gate=gate,
                quality_summary=quality_summary,
                read_only=False,
                gate3_record_only=True,
            )
            payload = {
                "task_key": task_key,
                "event_id": research_task.get("event_id"),
                "theme_code": str(research_task.get("theme_code") or "").strip(),
                "symbol": next(iter(target_symbols), None),
                "evidence_type": "gate3_record_only_audit",
                "weight": 1.0 if gate3_quality_record.get("gate3_record_quality_qualified") else 0.0,
                "evidence_payload": {
                    "contract_version": "strategy_factory.gate3_record_only_audit.v1",
                    "candidate_id": strategy_id,
                    "experiment_id": candidate.get("experiment_id"),
                    "factory_run_id": candidate.get("factory_run_id") or snapshot.get("factory_run_id"),
                    "status": status,
                    "passed": bool((gate or {}).get("passed")),
                    "validation_grade": gate3_quality_record.get("gate3_record_grade")
                    or quality_summary.get("validation_grade"),
                    "validation_total_score": quality_summary.get("validation_total_score"),
                    **gate3_quality_record,
                    "planned_submission_lane": planned_submission_lane,
                    "planned_final_status": planned_final_status,
                    "submission_lane": "gate3_record_only",
                    "strategy_created": False,
                    "lifecycle_action_executed": False,
                    "quality_report_persisted": False,
                    "gate3_audit_evidence_persisted": True,
                    "record_only_scope": "gate3_audit_evidence",
                    "automatic_downstream_action": False,
                    "quality_gate": compact_json(gate or {}),
                    "quality_summary": compact_json(dict((quality_report or {}).get("summary") or {})),
                    "quality_report": compact_json(quality_report or {}),
                    "backtest_metrics": compact_scalar_metrics(backtest_metrics or {}),
                },
            }
            try:
                result = db.save_factory_task_evidence(payload)
                if inspect.isawaitable(result):
                    result = await result
                return dict(result or {}) if isinstance(result, dict) else None
            except Exception as exc:
                logger.warning("StrategySubmitter: record gate3 audit evidence failed for %s: %s", strategy_id, exc)
                return None
