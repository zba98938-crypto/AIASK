"""Strategy manager helpers: NAV calculation, state management, quality report, incubation overview."""
from __future__ import annotations

# --- strategy_mgr_helpers_parts/parsers.py ---
import asyncio
import logging
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any, Optional

from strategy_factory.api import normalize_run_result_to_detail, normalize_run_result_to_summary
from strategy_factory.api import (
    BACKTEST_AI_PROTOTYPE_THRESHOLDS,
    DEPRECATION_THRESHOLDS,
    PROMOTION_THRESHOLDS,
    PROVISIONAL_PASS_THRESHOLDS,
    QUALITY_GATE_THRESHOLDS,
    RISK_REPORT_THRESHOLDS,
)
from strategy_factory.api.quality_reporting import (
    build_quality_report as _shared_build_quality_report,
    normalize_quality_gate_result as _shared_normalize_quality_gate_result,
    quality_gate_reason_code as _shared_quality_gate_reason_code,
)

from ...services.strategy_lifecycle_shared import (
    LIFECYCLE_TRANSITIONS,
    build_incubation_overview,
    evaluate_confidence_contract,
    get_latest_quality_report,
    list_quality_reports,
    metric_bucket_value,
    normalize_status_alias,
    update_status,
    validate_transition,
)

logger = logging.getLogger(__name__)

_FACTORY_SUMMARY_OBSERVABILITY_FIELDS = (
    "execution_mode",
    "engine_version",
    "parity_status",
    "artifact_refs",
    "external_llm_provider_health_status",
    "external_llm_provider_control_mode",
    "external_llm_provider_control_reasons",
    "suppressed_generator_modes",
    "feedback_generator_mode_control_mode_counts",
    "external_llm_provider_suppressed",
    "external_llm_provider_cooldown",
    "candidate_local_attempt_count",
    "task_local_attempt_count",
    "cohort_effective_trials",
    "refresh_existing_count",
    "spawn_revision_from_existing_count",
    "unique_family_holding_universe_count",
    "economic_semantics_missing_count",
    "research_only_count",
    "deferred_submission_count",
    "validation_grade_distribution",
    "raw_validation_grade_distribution",
    "effective_validation_grade_distribution",
    "raw_validation_total_score_mean",
    "raw_validation_total_score_p50",
    "raw_validation_total_score_p90",
    "raw_validation_a_rate",
    "raw_validation_b_rate",
    "raw_validation_c_rate",
    "raw_validation_d_rate",
    "strict_incubation_ready_count",
    "strict_incubation_ready_rate",
    "live_candidate_ready_count",
    "live_candidate_ready_rate",
    "raw_b_or_above_count",
    "raw_b_or_above_rate",
    "strict_ready_given_raw_b_count",
    "strict_ready_given_raw_b_rate",
    "live_ready_given_raw_b_count",
    "live_ready_given_raw_b_rate",
    "strict_live_alignment_gap_count",
    "strict_live_alignment_gap_rate",
    "strict_live_alignment_status_counts",
    "validation_family_quality_panel",
    "prediction_quality_distribution",
    "execution_quality_distribution",
    "evidence_alignment_distribution",
    "confidence_contract_ready_rate",
)

_FACTORY_BASELINE_FORWARD_DAYS = (1, 5, 10, 20)
_FACTORY_GENERATION_LANE_DEFINITION = (
    "按持久化的 generator_mode / candidate_lane / quota_fill 证据切分生成层级；"
    "若 submitted 策略未保留 historical_guided 痕迹，L1 会并入 L0 规则层。"
)
_FACTORY_GENERATION_LANE_SORT_ORDER = {
    "l0_local_rule": 0,
    "l1_historical_guided": 1,
    "l2_external_known_type": 2,
    "l2_hypothesis_lowering": 3,
    "l3_open_dsl": 4,
    "unknown": 99,
}


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return float(default)


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(float(value) for value in values)
    if len(ordered) == 1:
        return round(ordered[0], 4)
    p = max(0.0, min(float(percentile), 1.0))
    index = p * (len(ordered) - 1)
    lower = int(index)
    upper = min(lower + 1, len(ordered) - 1)
    weight = index - lower
    result = ordered[lower] * (1.0 - weight) + ordered[upper] * weight
    return round(result, 4)


def _grade_rates(distribution: dict[str, int], total: int) -> dict[str, float]:
    denominator = max(int(total or 0), 1)
    return {
        "raw_validation_a_rate": round(int(distribution.get("A") or 0) / denominator, 4) if total else 0.0,
        "raw_validation_b_rate": round(int(distribution.get("B") or 0) / denominator, 4) if total else 0.0,
        "raw_validation_c_rate": round(int(distribution.get("C") or 0) / denominator, 4) if total else 0.0,
        "raw_validation_d_rate": round(int(distribution.get("D") or 0) / denominator, 4) if total else 0.0,
    }


def _rate(count: int, total: int) -> float:
    return round(int(count or 0) / int(total or 0), 4) if total else 0.0


def _mean(values: list[float]) -> float:
    return round(sum(values) / len(values), 4) if values else 0.0


def _is_raw_b_or_above(grade: str) -> bool:
    return str(grade or "").strip().upper() in {"A", "B"}


def _normalized_text(value: Any) -> str:
    return str(value or "").strip().lower()


def _brief_holding_bucket(payload: dict[str, Any]) -> str:
    return str(
        payload.get("holding_period_bucket")
        or payload.get("holding_bucket")
        or "unknown"
    ).strip().lower() or "unknown"


def _brief_target_universe_key(payload: dict[str, Any]) -> str:
    target_pool_id = str(payload.get("target_pool_id") or "").strip()
    if target_pool_id:
        return target_pool_id
    task_signature = str(payload.get("task_signature") or "").strip()
    if task_signature:
        return task_signature
    validation_focus = str(
        payload.get("validation_focus")
        or dict(payload.get("validation_profile") or {}).get("validation_focus")
        or ""
    ).strip()
    if validation_focus:
        return f"focus:{validation_focus}"
    return "unknown"


def _high_confidence_text(payload: dict[str, Any], key: str) -> str | None:
    record = dict(payload or {})
    params = dict(record.get("params") or {})
    incubation_overview = dict(record.get("incubation_overview") or {})
    for value in (
        record.get(key),
        incubation_overview.get(key),
        params.get(key),
    ):
        token = str(value or "").strip().lower()
        if token:
            return token
    return None


def _high_confidence_mapping(payload: dict[str, Any], key: str) -> dict[str, Any]:
    record = dict(payload or {})
    params = dict(record.get("params") or {})
    incubation_overview = dict(record.get("incubation_overview") or {})
    for value in (
        record.get(key),
        incubation_overview.get(key),
        params.get(key),
    ):
        if isinstance(value, dict):
            return dict(value)
    return {}


def _resolve_confidence_contract_status(payload: dict[str, Any]) -> str | None:
    explicit = _high_confidence_text(payload, "confidence_contract_status")
    contract = _high_confidence_mapping(payload, "confidence_contract")
    if contract:
        status, _ = evaluate_confidence_contract(contract)
        return status
    if explicit:
        return explicit
    if not contract:
        return None
    return None


def _resolve_evidence_alignment_status(payload: dict[str, Any]) -> str | None:
    record = dict(payload or {})
    params = dict(record.get("params") or {})
    audit = dict(
        record.get("evidence_alignment_audit")
        or params.get("evidence_alignment_audit")
        or {}
    )
    status = str(
        audit.get("evidence_alignment_status") or record.get("evidence_alignment_status") or ""
    ).strip().lower()
    if status:
        return status
    if record.get("legacy_semantic_contract") is True or params.get("legacy_semantic_contract") is True:
        return "legacy"
    return None


def _summarize_high_confidence_quality(records: list[dict[str, Any]]) -> dict[str, Any]:
    prediction_quality_distribution: dict[str, int] = {}
    execution_quality_distribution: dict[str, int] = {}
    evidence_alignment_distribution: dict[str, int] = {}
    confidence_contract_ready_count = 0
    total = len(records)
    for record in list(records or []):
        payload = dict(record or {})
        prediction_label = _high_confidence_text(payload, "prediction_quality_label")
        execution_label = _high_confidence_text(payload, "execution_quality_label")
        evidence_alignment_status = _resolve_evidence_alignment_status(payload)
        confidence_contract_status = _resolve_confidence_contract_status(payload)
        if prediction_label:
            prediction_quality_distribution[prediction_label] = (
                prediction_quality_distribution.get(prediction_label, 0) + 1
            )
        if execution_label:
            execution_quality_distribution[execution_label] = (
                execution_quality_distribution.get(execution_label, 0) + 1
            )
        if evidence_alignment_status:
            evidence_alignment_distribution[evidence_alignment_status] = (
                evidence_alignment_distribution.get(evidence_alignment_status, 0) + 1
            )
        if confidence_contract_status in {"diagnostic_ready", "comparable_ready"}:
            confidence_contract_ready_count += 1
    return {
        "prediction_quality_distribution": prediction_quality_distribution,
        "execution_quality_distribution": execution_quality_distribution,
        "evidence_alignment_distribution": evidence_alignment_distribution,
        "confidence_contract_ready_rate": (
            round(confidence_contract_ready_count / total, 4) if total else 0.0
        ),
    }


def _build_family_quality_panel(overviews: list[dict[str, Any]]) -> list[dict[str, Any]]:
    buckets: dict[tuple[str, str, str], dict[str, Any]] = {}
    for overview in list(overviews or []):
        payload = dict(overview or {})
        family = str(payload.get("candidate_family") or payload.get("strategy_type") or "unknown").strip().lower() or "unknown"
        holding_bucket = str(payload.get("holding_period_bucket") or "unknown").strip().lower() or "unknown"
        validation_focus = str(payload.get("validation_focus") or "unknown").strip().lower() or "unknown"
        key = (family, holding_bucket, validation_focus)
        bucket = buckets.setdefault(
            key,
            {
                "strategy_family": family,
                "holding_period_bucket": holding_bucket,
                "validation_focus": validation_focus,
                "strategy_count": 0,
                "raw_validation_grade_distribution": {},
                "effective_validation_grade_distribution": {},
                "raw_validation_total_scores": [],
                "strict_incubation_ready_count": 0,
                "live_candidate_ready_count": 0,
                "raw_b_or_above_count": 0,
                "strict_ready_given_raw_b_count": 0,
                "live_ready_given_raw_b_count": 0,
                "trade_density_values": [],
                "post_cost_sharpe_values": [],
                "deflated_sharpe_ratio_values": [],
                "pbo_values": [],
            },
        )
        bucket["strategy_count"] += 1
        raw_grade = str(payload.get("raw_validation_grade") or payload.get("validation_grade") or "").strip().upper()
        effective_grade = str(
            payload.get("effective_validation_grade") or payload.get("validation_grade") or ""
        ).strip().upper()
        if raw_grade:
            bucket["raw_validation_grade_distribution"][raw_grade] = (
                bucket["raw_validation_grade_distribution"].get(raw_grade, 0) + 1
            )
        if effective_grade:
            bucket["effective_validation_grade_distribution"][effective_grade] = (
                bucket["effective_validation_grade_distribution"].get(effective_grade, 0) + 1
            )
        if payload.get("raw_validation_total_score") is not None:
            bucket["raw_validation_total_scores"].append(_safe_float(payload.get("raw_validation_total_score")))
        strict_ready = payload.get("strict_incubation_ready") is True
        live_ready = payload.get("live_candidate_ready") is True
        if strict_ready:
            bucket["strict_incubation_ready_count"] += 1
        if live_ready:
            bucket["live_candidate_ready_count"] += 1
        if _is_raw_b_or_above(raw_grade):
            bucket["raw_b_or_above_count"] += 1
            if strict_ready:
                bucket["strict_ready_given_raw_b_count"] += 1
            if live_ready:
                bucket["live_ready_given_raw_b_count"] += 1
        for metric_key, bucket_key in (
            ("trade_density", "trade_density_values"),
            ("post_cost_sharpe", "post_cost_sharpe_values"),
            ("deflated_sharpe_ratio", "deflated_sharpe_ratio_values"),
            ("pbo", "pbo_values"),
        ):
            if payload.get(metric_key) is not None:
                bucket[bucket_key].append(_safe_float(payload.get(metric_key)))

    panel: list[dict[str, Any]] = []
    for bucket in buckets.values():
        strategy_count = int(bucket.get("strategy_count") or 0)
        raw_distribution = dict(bucket.get("raw_validation_grade_distribution") or {})
        raw_scores = list(bucket.get("raw_validation_total_scores") or [])
        trade_density_values = list(bucket.get("trade_density_values") or [])
        post_cost_sharpe_values = list(bucket.get("post_cost_sharpe_values") or [])
        dsr_values = list(bucket.get("deflated_sharpe_ratio_values") or [])
        pbo_values = list(bucket.get("pbo_values") or [])
        raw_b_or_above_count = int(bucket.get("raw_b_or_above_count") or 0)
        strict_ready_given_raw_b_count = int(
            bucket.get("strict_ready_given_raw_b_count") or 0
        )
        live_ready_given_raw_b_count = int(bucket.get("live_ready_given_raw_b_count") or 0)
        item = {
            "strategy_family": bucket.get("strategy_family"),
            "holding_period_bucket": bucket.get("holding_period_bucket"),
            "validation_focus": bucket.get("validation_focus"),
            "strategy_count": strategy_count,
            "raw_validation_grade_distribution": raw_distribution,
            "effective_validation_grade_distribution": dict(
                bucket.get("effective_validation_grade_distribution") or {}
            ),
            "raw_validation_total_score_mean": round(sum(raw_scores) / len(raw_scores), 4) if raw_scores else 0.0,
            "strict_incubation_ready_count": int(bucket.get("strict_incubation_ready_count") or 0),
            "strict_incubation_ready_rate": round(
                int(bucket.get("strict_incubation_ready_count") or 0) / strategy_count,
                4,
            ) if strategy_count else 0.0,
            "live_candidate_ready_count": int(bucket.get("live_candidate_ready_count") or 0),
            "live_candidate_ready_rate": round(
                int(bucket.get("live_candidate_ready_count") or 0) / strategy_count,
                4,
            ) if strategy_count else 0.0,
            "raw_b_or_above_count": raw_b_or_above_count,
            "raw_b_or_above_rate": _rate(raw_b_or_above_count, strategy_count),
            "strict_ready_given_raw_b_count": strict_ready_given_raw_b_count,
            "strict_ready_given_raw_b_rate": _rate(
                strict_ready_given_raw_b_count,
                raw_b_or_above_count,
            ),
            "live_ready_given_raw_b_count": live_ready_given_raw_b_count,
            "live_ready_given_raw_b_rate": _rate(
                live_ready_given_raw_b_count,
                raw_b_or_above_count,
            ),
            "mean_trade_density": round(sum(trade_density_values) / len(trade_density_values), 4)
            if trade_density_values else 0.0,
            "mean_post_cost_sharpe": round(sum(post_cost_sharpe_values) / len(post_cost_sharpe_values), 4)
            if post_cost_sharpe_values else 0.0,
            "mean_deflated_sharpe_ratio": round(sum(dsr_values) / len(dsr_values), 4)
            if dsr_values else 0.0,
            "mean_pbo": round(sum(pbo_values) / len(pbo_values), 4) if pbo_values else 0.0,
        }
        item.update(_grade_rates(raw_distribution, strategy_count))
        item.update(
            {
                "family_raw_a_rate": item.get("raw_validation_a_rate", 0.0),
                "family_raw_b_rate": item.get("raw_validation_b_rate", 0.0),
                "family_raw_c_rate": item.get("raw_validation_c_rate", 0.0),
                "family_raw_d_rate": item.get("raw_validation_d_rate", 0.0),
                "family_strict_incubation_ready_rate": item.get(
                    "strict_incubation_ready_rate",
                    0.0,
                ),
                "family_live_candidate_ready_rate": item.get(
                    "live_candidate_ready_rate",
                    0.0,
                ),
                "family_mean_trade_density": item.get("mean_trade_density", 0.0),
                "family_mean_post_cost_sharpe": item.get("mean_post_cost_sharpe", 0.0),
                "family_mean_dsr": item.get("mean_deflated_sharpe_ratio", 0.0),
                "family_mean_pbo": item.get("mean_pbo", 0.0),
            }
        )
        panel.append(item)
    panel.sort(
        key=lambda item: (
            int(item.get("strategy_count") or 0),
            float(item.get("raw_validation_b_rate") or 0.0),
            float(item.get("raw_validation_a_rate") or 0.0),
            str(item.get("strategy_family") or ""),
        ),
        reverse=True,
    )
    return panel[:24]


def _extract_generation_lane(payload: dict[str, Any]) -> dict[str, str]:
    data = dict(payload or {})
    params = dict(data.get("params") or {})
    candidate_provenance = dict(
        data.get("candidate_provenance")
        or params.get("candidate_provenance")
        or {}
    )
    quota_fill = dict(data.get("quota_fill") or params.get("quota_fill") or {})
    tags = {
        _normalized_text(tag)
        for tag in [*list(data.get("tags") or []), *list(params.get("tags") or [])]
        if _normalized_text(tag)
    }

    generator_mode = (
        _normalized_text(data.get("generator_mode"))
        or _normalized_text(data.get("generator_type"))
        or _normalized_text(params.get("generator_mode"))
        or _normalized_text(params.get("generator_type"))
        or _normalized_text(candidate_provenance.get("generator_mode"))
        or _normalized_text(candidate_provenance.get("generator_type"))
    )
    candidate_lane = (
        _normalized_text(data.get("candidate_lane"))
        or _normalized_text(params.get("candidate_lane"))
        or _normalized_text(candidate_provenance.get("candidate_lane"))
    )
    parameter_source = (
        _normalized_text(data.get("parameter_source"))
        or _normalized_text(params.get("parameter_source"))
        or _normalized_text(quota_fill.get("parameter_source"))
    )
    fill_source_mode = (
        _normalized_text(data.get("fill_source_mode"))
        or _normalized_text(params.get("fill_source_mode"))
        or _normalized_text(quota_fill.get("fill_source_mode"))
    )

    if (
        candidate_lane == "l3_open_dsl"
        or generator_mode in {"llm_defined", "open_dsl", "llm_defined_dsl"}
        or "llm_defined" in tags
        or "open_dsl" in tags
    ):
        return {
            "lane_key": "l3_open_dsl",
            "lane_label": "L3 Open DSL",
            "generation_tier": "L3",
            "generator_mode": generator_mode or "llm_defined",
        }
    if (
        candidate_lane == "l2_hypothesis_lowering"
        or generator_mode in {"llm_hypothesis_compiler", "hypothesis_replay"}
    ):
        return {
            "lane_key": "l2_hypothesis_lowering",
            "lane_label": "L2 Hypothesis Lowering",
            "generation_tier": "L2",
            "generator_mode": generator_mode or "llm_hypothesis_compiler",
        }
    if parameter_source == "historical_distribution" or fill_source_mode == "historical_guided":
        return {
            "lane_key": "l1_historical_guided",
            "lane_label": "L1 Historical Guided Rule",
            "generation_tier": "L1",
            "generator_mode": generator_mode or "rule",
        }
    if (
        generator_mode in {
            "external_llm",
            "pipeline_staged",
            "llm_proxy",
            "llm_proxy_fallback",
            "external_llm_open_dsl",
            "rl_bandit",
        }
        or "external_llm" in tags
        or "ai_generated" in tags
    ):
        return {
            "lane_key": "l2_external_known_type",
            "lane_label": "L2 External Known-Type",
            "generation_tier": "L2",
            "generator_mode": generator_mode or "external_llm",
        }
    if (
        generator_mode in {"bulk_stock_matrix", "rule", "local_rule", "local_rule_v1"}
        or "rule" in tags
        or data.get("generation_reason")
        or data.get("spawn_reason")
    ):
        return {
            "lane_key": "l0_local_rule",
            "lane_label": "L0 Local Rule",
            "generation_tier": "L0",
            "generator_mode": generator_mode or "rule",
        }
    return {
        "lane_key": "unknown",
        "lane_label": "Unknown / Unlabeled",
        "generation_tier": "unknown",
        "generator_mode": generator_mode or "unknown",
    }

# --- strategy_mgr_helpers_parts/payloads.py ---
def _build_generation_lane_quality_panel(records: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, int]]:
    buckets: dict[str, dict[str, Any]] = {}
    generator_mode_counts: dict[str, int] = {}
    for record in list(records or []):
        payload = dict(record or {})
        lane = _extract_generation_lane(payload)
        lane_key = lane["lane_key"]
        generator_mode = lane["generator_mode"]
        generator_mode_counts[generator_mode] = generator_mode_counts.get(generator_mode, 0) + 1
        bucket = buckets.setdefault(
            lane_key,
            {
                "lane_key": lane_key,
                "lane_label": lane["lane_label"],
                "generation_tier": lane["generation_tier"],
                "strategy_count": 0,
                "status_counts": {},
                "generator_mode_counts": {},
                "strategy_family_counts": {},
                "raw_validation_grade_distribution": {},
                "effective_validation_grade_distribution": {},
                "raw_validation_total_scores": [],
                "strict_incubation_ready_count": 0,
                "live_candidate_ready_count": 0,
                "promotion_ready_count": 0,
                "quality_passed_count": 0,
                "raw_b_or_above_count": 0,
                "strict_ready_given_raw_b_count": 0,
                "live_ready_given_raw_b_count": 0,
            },
        )
        bucket["strategy_count"] += 1
        bucket["generator_mode_counts"][generator_mode] = (
            bucket["generator_mode_counts"].get(generator_mode, 0) + 1
        )

        status_key = normalize_status_alias(payload.get("status"))
        if status_key:
            bucket["status_counts"][status_key] = bucket["status_counts"].get(status_key, 0) + 1

        family = str(
            payload.get("candidate_family")
            or payload.get("strategy_type")
            or "unknown"
        ).strip().lower() or "unknown"
        bucket["strategy_family_counts"][family] = (
            bucket["strategy_family_counts"].get(family, 0) + 1
        )

        raw_grade = str(
            payload.get("raw_validation_grade")
            or payload.get("validation_grade")
            or ""
        ).strip().upper()
        effective_grade = str(
            payload.get("effective_validation_grade")
            or payload.get("validation_grade")
            or ""
        ).strip().upper()
        if raw_grade:
            bucket["raw_validation_grade_distribution"][raw_grade] = (
                bucket["raw_validation_grade_distribution"].get(raw_grade, 0) + 1
            )
        if effective_grade:
            bucket["effective_validation_grade_distribution"][effective_grade] = (
                bucket["effective_validation_grade_distribution"].get(effective_grade, 0) + 1
            )
        if payload.get("raw_validation_total_score") is not None:
            bucket["raw_validation_total_scores"].append(
                _safe_float(payload.get("raw_validation_total_score"))
            )

        strict_ready = payload.get("strict_incubation_ready") is True
        live_ready = payload.get("live_candidate_ready") is True
        if strict_ready:
            bucket["strict_incubation_ready_count"] += 1
        if live_ready:
            bucket["live_candidate_ready_count"] += 1
        if payload.get("promotion_ready"):
            bucket["promotion_ready_count"] += 1
        if payload.get("quality_passed"):
            bucket["quality_passed_count"] += 1
        if _is_raw_b_or_above(raw_grade):
            bucket["raw_b_or_above_count"] += 1
            if strict_ready:
                bucket["strict_ready_given_raw_b_count"] += 1
            if live_ready:
                bucket["live_ready_given_raw_b_count"] += 1

    panel: list[dict[str, Any]] = []
    for bucket in buckets.values():
        strategy_count = int(bucket.get("strategy_count") or 0)
        raw_distribution = dict(bucket.get("raw_validation_grade_distribution") or {})
        raw_scores = list(bucket.get("raw_validation_total_scores") or [])
        raw_b_or_above_count = int(bucket.get("raw_b_or_above_count") or 0)
        panel.append(
            {
                "lane_key": bucket.get("lane_key"),
                "lane_label": bucket.get("lane_label"),
                "generation_tier": bucket.get("generation_tier"),
                "strategy_count": strategy_count,
                "status_counts": dict(bucket.get("status_counts") or {}),
                "generator_mode_counts": dict(bucket.get("generator_mode_counts") or {}),
                "strategy_family_counts": dict(bucket.get("strategy_family_counts") or {}),
                "raw_validation_grade_distribution": raw_distribution,
                "effective_validation_grade_distribution": dict(
                    bucket.get("effective_validation_grade_distribution") or {}
                ),
                "raw_validation_total_score_mean": round(
                    sum(raw_scores) / len(raw_scores),
                    4,
                ) if raw_scores else 0.0,
                **_grade_rates(raw_distribution, strategy_count),
                "strict_incubation_ready_count": int(
                    bucket.get("strict_incubation_ready_count") or 0
                ),
                "strict_incubation_ready_rate": _rate(
                    int(bucket.get("strict_incubation_ready_count") or 0),
                    strategy_count,
                ),
                "live_candidate_ready_count": int(
                    bucket.get("live_candidate_ready_count") or 0
                ),
                "live_candidate_ready_rate": _rate(
                    int(bucket.get("live_candidate_ready_count") or 0),
                    strategy_count,
                ),
                "promotion_ready_count": int(bucket.get("promotion_ready_count") or 0),
                "promotion_ready_rate": _rate(
                    int(bucket.get("promotion_ready_count") or 0),
                    strategy_count,
                ),
                "quality_passed_count": int(bucket.get("quality_passed_count") or 0),
                "quality_pass_rate": _rate(
                    int(bucket.get("quality_passed_count") or 0),
                    strategy_count,
                ),
                "raw_b_or_above_count": raw_b_or_above_count,
                "raw_b_or_above_rate": _rate(raw_b_or_above_count, strategy_count),
                "strict_ready_given_raw_b_count": int(
                    bucket.get("strict_ready_given_raw_b_count") or 0
                ),
                "strict_ready_given_raw_b_rate": _rate(
                    int(bucket.get("strict_ready_given_raw_b_count") or 0),
                    raw_b_or_above_count,
                ),
                "live_ready_given_raw_b_count": int(
                    bucket.get("live_ready_given_raw_b_count") or 0
                ),
                "live_ready_given_raw_b_rate": _rate(
                    int(bucket.get("live_ready_given_raw_b_count") or 0),
                    raw_b_or_above_count,
                ),
            }
        )
    panel.sort(
        key=lambda item: (
            _FACTORY_GENERATION_LANE_SORT_ORDER.get(
                str(item.get("lane_key") or ""),
                _FACTORY_GENERATION_LANE_SORT_ORDER["unknown"],
            ),
            -int(item.get("strategy_count") or 0),
            str(item.get("lane_label") or ""),
        )
    )
    return panel, generator_mode_counts


# ── NAV calculation ──────────────────────────────────────────────────────────

async def compute_nav_series(db, strategy_id: str, max_points: int = 30) -> list:
    """Return paper-trading NAV; fall back to signal_forward_returns derived NAV."""
    try:
        if hasattr(db, "get_paper_account_by_strategy") and hasattr(db, "get_paper_nav_rows"):
            account = await db.get_paper_account_by_strategy(strategy_id)
            if account:
                nav_rows = await db.get_paper_nav_rows(account["id"], limit=max(max_points * 4, 60))
                if nav_rows:
                    nav = [
                        round(
                            float(row.get("total_value") or 0.0)
                            / max(float(account.get("initial_capital") or 1.0), 1.0),
                            4,
                        )
                        for row in reversed(nav_rows)
                    ]
                    if len(nav) > max_points:
                        step = max(1, len(nav) // max_points)
                        nav = nav[::step][:max_points]
                    return nav
        async with db.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT ss.signal_date, ss.signal, sfr.actual_return
                FROM strategy_signals ss
                JOIN signal_forward_returns sfr ON sfr.signal_id = ss.id AND sfr.forward_days = 5
                WHERE ss.strategy_id = $1 AND ss.signal != 0
                ORDER BY ss.signal_date
                """,
                strategy_id,
            )
        if not rows:
            return []
        daily: dict = {}
        for r in rows:
            d = r["signal_date"]
            ret = float(r["actual_return"] or 0) * (1 if r["signal"] == 1 else -1)
            daily.setdefault(d, []).append(ret)
        nav = [1.0]
        for d in sorted(daily):
            avg = sum(daily[d]) / len(daily[d])
            nav.append(round(nav[-1] * (1 + avg), 4))
        if len(nav) > max_points:
            step = max(1, len(nav) // max_points)
            nav = nav[::step][:max_points]
        return nav
    except Exception:
        return []


# ── Lifecycle state management (imported from strategy_lifecycle_shared) ─────


# ── Quality report helpers ───────────────────────────────────────────────────

async def save_quality_report(db, strategy_id: str, report: dict, report_type: str = "submission") -> None:
    if hasattr(db, "save_strategy_quality_report"):
        await db.save_strategy_quality_report(strategy_id, report_type, report)


def is_factory_generated_strategy(strategy: Optional[dict]) -> bool:
    payload = dict(strategy or {})
    tags = {str(tag or "").strip().lower() for tag in list(payload.get("tags") or [])}
    author_id = str(payload.get("author_id") or "").strip().lower()
    source = str(payload.get("source") or "").strip().lower()
    if "factory" in tags or "auto_generated" in tags:
        return True
    if author_id == "strategy_factory":
        return True
    return source.startswith("strategy_factory")

# --- strategy_mgr_helpers_parts/formatters.py ---
async def build_factory_quality_baseline(
    db,
    *,
    latest_run: Optional[dict] = None,
    limit_per_status: int = 200,
) -> dict:
    captured_at = datetime.now(timezone.utc).isoformat()
    latest_run_summary = normalize_factory_run_summary_contract(latest_run or {})
    latest_run_submission_artifact = dict(latest_run_summary.get("submission_artifact") or {})
    latest_run_strategy_briefs = [
        dict(item or {})
        for item in list(latest_run_submission_artifact.get("strategy_briefs") or [])
        if isinstance(item, dict)
    ]
    latest_run_generation_lane_panel, latest_run_generation_mode_counts = (
        _build_generation_lane_quality_panel(latest_run_strategy_briefs)
    )
    latest_run_high_confidence = _summarize_high_confidence_quality(
        latest_run_strategy_briefs
    )
    latest_run_payload = {
        "run_id": str(latest_run_summary.get("run_id") or "").strip() or None,
        "status": str(latest_run_summary.get("status") or "").strip() or None,
        "started_at": latest_run_summary.get("started_at"),
        "completed_at": latest_run_summary.get("completed_at"),
        "candidates_spawned": int(latest_run_summary.get("candidates_spawned") or 0),
        "submitted": int(latest_run_summary.get("submitted") or 0),
        "research_only_count": int(latest_run_summary.get("research_only_count") or 0),
        "deferred_submission_count": int(latest_run_summary.get("deferred_submission_count") or 0),
        "validation_grade_distribution": dict(latest_run_summary.get("validation_grade_distribution") or {}),
        "raw_validation_grade_distribution": dict(
            latest_run_summary.get("raw_validation_grade_distribution")
            or latest_run_summary.get("validation_grade_distribution")
            or {}
        ),
        "effective_validation_grade_distribution": dict(
            latest_run_summary.get("effective_validation_grade_distribution")
            or latest_run_summary.get("validation_grade_distribution")
            or {}
        ),
        "raw_validation_total_score_mean": _safe_float(
            latest_run_summary.get("raw_validation_total_score_mean"),
            0.0,
        ),
        "raw_validation_total_score_p50": _safe_float(
            latest_run_summary.get("raw_validation_total_score_p50"),
            0.0,
        ),
        "raw_validation_total_score_p90": _safe_float(
            latest_run_summary.get("raw_validation_total_score_p90"),
            0.0,
        ),
        "raw_validation_a_rate": _safe_float(latest_run_summary.get("raw_validation_a_rate"), 0.0),
        "raw_validation_b_rate": _safe_float(latest_run_summary.get("raw_validation_b_rate"), 0.0),
        "raw_validation_c_rate": _safe_float(latest_run_summary.get("raw_validation_c_rate"), 0.0),
        "raw_validation_d_rate": _safe_float(latest_run_summary.get("raw_validation_d_rate"), 0.0),
        "strict_incubation_ready_count": int(
            latest_run_summary.get("strict_incubation_ready_count") or 0
        ),
        "strict_incubation_ready_rate": _safe_float(
            latest_run_summary.get("strict_incubation_ready_rate"),
            0.0,
        ),
        "live_candidate_ready_count": int(
            latest_run_summary.get("live_candidate_ready_count") or 0
        ),
        "live_candidate_ready_rate": _safe_float(
            latest_run_summary.get("live_candidate_ready_rate"),
            0.0,
        ),
        "raw_b_or_above_count": int(latest_run_summary.get("raw_b_or_above_count") or 0),
        "raw_b_or_above_rate": _safe_float(latest_run_summary.get("raw_b_or_above_rate"), 0.0),
        "strict_ready_given_raw_b_count": int(
            latest_run_summary.get("strict_ready_given_raw_b_count") or 0
        ),
        "strict_ready_given_raw_b_rate": _safe_float(
            latest_run_summary.get("strict_ready_given_raw_b_rate"),
            0.0,
        ),
        "live_ready_given_raw_b_count": int(
            latest_run_summary.get("live_ready_given_raw_b_count") or 0
        ),
        "live_ready_given_raw_b_rate": _safe_float(
            latest_run_summary.get("live_ready_given_raw_b_rate"),
            0.0,
        ),
        "validation_family_quality_panel": list(
            latest_run_summary.get("validation_family_quality_panel") or []
        ),
        "prediction_quality_distribution": dict(
            latest_run_summary.get("prediction_quality_distribution")
            or latest_run_high_confidence.get("prediction_quality_distribution")
            or {}
        ),
        "execution_quality_distribution": dict(
            latest_run_summary.get("execution_quality_distribution")
            or latest_run_high_confidence.get("execution_quality_distribution")
            or {}
        ),
        "evidence_alignment_distribution": dict(
            latest_run_summary.get("evidence_alignment_distribution")
            or latest_run_high_confidence.get("evidence_alignment_distribution")
            or {}
        ),
        "confidence_contract_ready_rate": _safe_float(
            latest_run_summary.get("confidence_contract_ready_rate"),
            latest_run_high_confidence.get("confidence_contract_ready_rate") or 0.0,
        ),
        "generation_lane_definition": _FACTORY_GENERATION_LANE_DEFINITION,
        "generation_lane_quality_panel": latest_run_generation_lane_panel,
        "generation_mode_counts": latest_run_generation_mode_counts,
        "external_llm_provider_health_status": latest_run_summary.get("external_llm_provider_health_status"),
        "external_llm_provider_control_mode": latest_run_summary.get("external_llm_provider_control_mode"),
    }

    if not hasattr(db, "list_strategies"):
        return {
            "contract_version": "strategy_factory.quality_baseline.v1",
            "captured_at": captured_at,
            "latest_run": latest_run_payload,
            "submitted_strategy_cohort": {
                "statuses": ["submitted", "incubating", "listed"],
                "factory_strategy_count": 0,
                "status_counts": {},
                "validation_grade_distribution": {},
                "raw_validation_grade_distribution": {},
                "effective_validation_grade_distribution": {},
                "raw_validation_total_score_mean": 0.0,
                "raw_validation_total_score_p50": 0.0,
                "raw_validation_total_score_p90": 0.0,
                "raw_validation_a_rate": 0.0,
                "raw_validation_b_rate": 0.0,
                "raw_validation_c_rate": 0.0,
                "raw_validation_d_rate": 0.0,
                "zero_signal_count": 0,
                "zero_signal_rate": 0.0,
                "forward_coverage_count": 0,
                "forward_coverage_rate": 0.0,
                "promotion_ready_count": 0,
                "promotion_ready_rate": 0.0,
                "quality_passed_count": 0,
                "quality_pass_rate": 0.0,
                "baseline_forward_days": list(_FACTORY_BASELINE_FORWARD_DAYS),
                "quality_report_missing_count": 0,
                "zero_signal_definition": "raw_signal_count <= 0",
                "forward_coverage_definition": "observed all baseline forward days",
                "strict_incubation_ready_count": 0,
                "strict_incubation_ready_rate": 0.0,
                "live_candidate_ready_count": 0,
                "live_candidate_ready_rate": 0.0,
                "live_gate_ready_count": 0,
                "live_gate_ready_rate": 0.0,
                "raw_b_or_above_count": 0,
                "raw_b_or_above_rate": 0.0,
                "strict_ready_given_raw_b_count": 0,
                "strict_ready_given_raw_b_rate": 0.0,
                "live_ready_given_raw_b_count": 0,
                "live_ready_given_raw_b_rate": 0.0,
                "strict_live_alignment_gap_count": 0,
                "strict_live_alignment_gap_rate": 0.0,
                "strict_live_alignment_status_counts": {},
                "validation_grade_d_strict_incubation_pass_count": 0,
                "validation_grade_d_strict_incubation_pass_rate": 0.0,
                "validation_grade_d_promotion_ready_count": 0,
                "validation_grade_d_promotion_ready_rate": 0.0,
                "validation_family_quality_panel": [],
                "prediction_quality_distribution": {},
                "execution_quality_distribution": {},
                "evidence_alignment_distribution": {},
                "confidence_contract_ready_rate": 0.0,
                "generation_lane_definition": _FACTORY_GENERATION_LANE_DEFINITION,
                "generation_lane_quality_panel": [],
                "generation_mode_counts": {},
            },
        }

    cohort_statuses = ("submitted", "incubating", "listed")
    listed_rows, incubating_rows, submitted_rows = await asyncio.gather(
        db.list_strategies("listed", limit=limit_per_status),
        db.list_strategies("incubating", limit=limit_per_status),
        db.list_strategies("submitted", limit=limit_per_status),
    )
    strategies_by_id: dict[str, dict] = {}
    status_counts: dict[str, int] = {}
    for row in [*submitted_rows, *incubating_rows, *listed_rows]:
        strategy = dict(row or {})
        strategy_id = str(strategy.get("id") or "").strip()
        if not strategy_id or not is_factory_generated_strategy(strategy):
            continue
        strategies_by_id[strategy_id] = strategy
        status_key = normalize_status_alias(strategy.get("status"))
        status_counts[status_key] = status_counts.get(status_key, 0) + 1

    cohort = list(strategies_by_id.values())
    overviews = await asyncio.gather(
        *(build_incubation_overview(db, strategy) for strategy in cohort),
        return_exceptions=True,
    )
    validation_grade_distribution: dict[str, int] = {}
    raw_validation_grade_distribution: dict[str, int] = {}
    effective_validation_grade_distribution: dict[str, int] = {}
    raw_validation_total_scores: list[float] = []
    zero_signal_count = 0
    forward_coverage_count = 0
    promotion_ready_count = 0
    quality_passed_count = 0
    quality_report_missing_count = 0
    strict_incubation_ready_count = 0
    live_gate_ready_count = 0
    raw_b_or_above_count = 0
    strict_ready_given_raw_b_count = 0
    live_ready_given_raw_b_count = 0
    strict_live_alignment_gap_count = 0
    strict_live_alignment_status_counts: dict[str, int] = {}
    validation_grade_d_strict_incubation_pass_count = 0
    validation_grade_d_promotion_ready_count = 0
    processed_count = 0
    cohort_records: list[dict[str, Any]] = []

    for strategy, overview in zip(cohort, overviews):
        if isinstance(overview, Exception):
            logger.warning("factory quality baseline skipped strategy due to overview error: %s", overview)
            continue
        cohort_records.append(
            {
                **dict(strategy or {}),
                **dict(overview or {}),
                "params": dict(strategy.get("params") or {}),
                "tags": list(strategy.get("tags") or []),
            }
        )
        processed_count += 1
        validation_grade = str(overview.get("validation_grade") or "UNKNOWN").strip().upper()
        raw_validation_grade = str(
            overview.get("raw_validation_grade") or validation_grade or "UNKNOWN"
        ).strip().upper()
        effective_validation_grade = str(
            overview.get("effective_validation_grade") or validation_grade or "UNKNOWN"
        ).strip().upper()
        validation_grade_distribution[validation_grade] = (
            validation_grade_distribution.get(validation_grade, 0) + 1
        )
        raw_validation_grade_distribution[raw_validation_grade] = (
            raw_validation_grade_distribution.get(raw_validation_grade, 0) + 1
        )
        effective_validation_grade_distribution[effective_validation_grade] = (
            effective_validation_grade_distribution.get(effective_validation_grade, 0) + 1
        )
        if overview.get("raw_validation_total_score") is not None:
            raw_validation_total_scores.append(_safe_float(overview.get("raw_validation_total_score")))
        raw_signal_count = int(overview.get("raw_signal_count") or overview.get("total_signals") or 0)
        if raw_signal_count <= 0:
            zero_signal_count += 1
        observed_forward_days = {
            int(item)
            for item in list(overview.get("observed_forward_days") or [])
            if str(item).strip()
        }
        if set(_FACTORY_BASELINE_FORWARD_DAYS).issubset(observed_forward_days):
            forward_coverage_count += 1
        if overview.get("promotion_ready"):
            promotion_ready_count += 1
        if overview.get("quality_passed"):
            quality_passed_count += 1
        strict_ready = overview.get("strict_incubation_ready") is True
        live_ready = overview.get("live_candidate_ready") is True
        if strict_ready:
            strict_incubation_ready_count += 1
        if live_ready:
            live_gate_ready_count += 1
        if raw_validation_grade in {"A", "B"}:
            raw_b_or_above_count += 1
            if strict_ready:
                strict_ready_given_raw_b_count += 1
            if live_ready:
                live_ready_given_raw_b_count += 1
        if overview.get("strict_live_alignment_gap"):
            strict_live_alignment_gap_count += 1
        alignment_status = str(overview.get("strict_live_alignment_status") or "").strip().lower()
        if alignment_status:
            strict_live_alignment_status_counts[alignment_status] = (
                strict_live_alignment_status_counts.get(alignment_status, 0) + 1
            )
        if raw_validation_grade == "D" and overview.get("strict_incubation_ready") is True:
            validation_grade_d_strict_incubation_pass_count += 1
        if raw_validation_grade == "D" and overview.get("promotion_ready"):
            validation_grade_d_promotion_ready_count += 1
        if validation_grade == "UNKNOWN":
            quality_report_missing_count += 1

    denominator = max(processed_count, 1)
    family_quality_panel = _build_family_quality_panel(cohort_records)
    generation_lane_quality_panel, generation_mode_counts = _build_generation_lane_quality_panel(
        cohort_records
    )
    cohort_high_confidence = _summarize_high_confidence_quality(cohort_records)
    return {
        "contract_version": "strategy_factory.quality_baseline.v1",
        "captured_at": captured_at,
        "latest_run": latest_run_payload,
        "submitted_strategy_cohort": {
            "statuses": list(cohort_statuses),
            "factory_strategy_count": processed_count,
            "status_counts": status_counts,
            "validation_grade_distribution": validation_grade_distribution,
            "raw_validation_grade_distribution": raw_validation_grade_distribution,
            "effective_validation_grade_distribution": effective_validation_grade_distribution,
            "raw_validation_total_score_mean": round(
                sum(raw_validation_total_scores) / len(raw_validation_total_scores),
                4,
            ) if raw_validation_total_scores else 0.0,
            "raw_validation_total_score_p50": _percentile(raw_validation_total_scores, 0.5),
            "raw_validation_total_score_p90": _percentile(raw_validation_total_scores, 0.9),
            **_grade_rates(raw_validation_grade_distribution, processed_count),
            "zero_signal_count": zero_signal_count,
            "zero_signal_rate": round(zero_signal_count / denominator, 4) if processed_count else 0.0,
            "forward_coverage_count": forward_coverage_count,
            "forward_coverage_rate": round(forward_coverage_count / denominator, 4) if processed_count else 0.0,
            "promotion_ready_count": promotion_ready_count,
            "promotion_ready_rate": round(promotion_ready_count / denominator, 4) if processed_count else 0.0,
            "quality_passed_count": quality_passed_count,
            "quality_pass_rate": round(quality_passed_count / denominator, 4) if processed_count else 0.0,
            "strict_incubation_ready_count": strict_incubation_ready_count,
            "strict_incubation_ready_rate": round(strict_incubation_ready_count / denominator, 4) if processed_count else 0.0,
            "live_candidate_ready_count": live_gate_ready_count,
            "live_candidate_ready_rate": round(live_gate_ready_count / denominator, 4) if processed_count else 0.0,
            "live_gate_ready_count": live_gate_ready_count,
            "live_gate_ready_rate": round(live_gate_ready_count / denominator, 4) if processed_count else 0.0,
            "raw_b_or_above_count": raw_b_or_above_count,
            "raw_b_or_above_rate": round(raw_b_or_above_count / denominator, 4) if processed_count else 0.0,
            "strict_ready_given_raw_b_count": strict_ready_given_raw_b_count,
            "strict_ready_given_raw_b_rate": _rate(
                strict_ready_given_raw_b_count,
                raw_b_or_above_count,
            ),
            "live_ready_given_raw_b_count": live_ready_given_raw_b_count,
            "live_ready_given_raw_b_rate": _rate(
                live_ready_given_raw_b_count,
                raw_b_or_above_count,
            ),
            "strict_live_alignment_gap_count": strict_live_alignment_gap_count,
            "strict_live_alignment_gap_rate": round(strict_live_alignment_gap_count / denominator, 4) if processed_count else 0.0,
            "strict_live_alignment_status_counts": strict_live_alignment_status_counts,
            "validation_grade_d_strict_incubation_pass_count": validation_grade_d_strict_incubation_pass_count,
            "validation_grade_d_strict_incubation_pass_rate": round(
                validation_grade_d_strict_incubation_pass_count / denominator,
                4,
            ) if processed_count else 0.0,
            "validation_grade_d_promotion_ready_count": validation_grade_d_promotion_ready_count,
            "validation_grade_d_promotion_ready_rate": round(
                validation_grade_d_promotion_ready_count / denominator,
                4,
            ) if processed_count else 0.0,
            "validation_family_quality_panel": family_quality_panel,
            "prediction_quality_distribution": dict(
                cohort_high_confidence.get("prediction_quality_distribution") or {}
            ),
            "execution_quality_distribution": dict(
                cohort_high_confidence.get("execution_quality_distribution") or {}
            ),
            "evidence_alignment_distribution": dict(
                cohort_high_confidence.get("evidence_alignment_distribution") or {}
            ),
            "confidence_contract_ready_rate": _safe_float(
                cohort_high_confidence.get("confidence_contract_ready_rate"),
                0.0,
            ),
            "generation_lane_definition": _FACTORY_GENERATION_LANE_DEFINITION,
            "generation_lane_quality_panel": generation_lane_quality_panel,
            "generation_mode_counts": generation_mode_counts,
            "baseline_forward_days": list(_FACTORY_BASELINE_FORWARD_DAYS),
            "quality_report_missing_count": quality_report_missing_count,
            "zero_signal_definition": "raw_signal_count <= 0",
            "forward_coverage_definition": "observed all baseline forward days",
        },
    }


# metric_bucket_value imported from strategy_lifecycle_shared


def normalize_time_filter(value: Any, *, is_end: bool = False) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if len(text) == 10:
        dt = datetime.fromisoformat(text)
        if is_end:
            dt = dt.replace(hour=23, minute=59, second=59, microsecond=999999)
        else:
            dt = dt.replace(hour=0, minute=0, second=0, microsecond=0)
    else:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def parse_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "y", "on"}:
        return True
    if text in {"0", "false", "no", "n", "off", ""}:
        return False
    return default


def quality_gate_reason_code(reason: str) -> str:
    return _shared_quality_gate_reason_code(reason)


def normalize_quality_gate_result(result: Optional[dict]) -> dict:
    return _shared_normalize_quality_gate_result(result)


def is_factory_ai_prototype_strategy(strategy: Optional[dict]) -> bool:
    payload = dict(strategy or {})
    strategy_type = str(payload.get("strategy_type") or "").strip().lower()
    tags = {str(tag).strip().lower() for tag in list(payload.get("tags") or [])}
    if "factory" not in tags and "auto_generated" not in tags:
        return False
    if "external_llm" in tags or "ai_generated" in tags:
        return True
    return strategy_type == "dsl_rule"


def has_only_statistical_gate_failures(gate_result: Optional[dict]) -> bool:
    gate = normalize_quality_gate_result(gate_result)
    codes = list(gate.get("reason_codes") or [])
    if not codes:
        return False
    allowed_prefixes = (
        "walk_forward_ic_ir",
        "purged_k_fold_ic",
        "bootstrap_ci_lower",
        "parameter_sensitivity",
        "multi_period_ic",
    )
    return all(any(str(code).startswith(prefix) for prefix in allowed_prefixes) for code in codes)


def safe_metric_value(payload: Optional[dict], *keys: str) -> float:
    data = dict(payload or {})
    for key in keys:
        if key in data and data.get(key) is not None:
            try:
                return float(data.get(key) or 0.0)
            except Exception as _swallowed:
                logger.warning("packages/akshare-mcp/src/akshare_mcp/tools/managers/strategy_mgr_helpers.py:1230: swallowed %s and continued: %s", type(_swallowed).__name__, _swallowed)
                return 0.0
    return 0.0


def _count_statistical_checks_passed(gate: dict) -> tuple[int, list[str], list[str]]:
    """统计质量门 5 项统计检查中通过了几项，返回 (通过数, 通过项列表, 失败项列表)。"""
    check_map = {
        "walk_forward_ic_ir": ("wf_ic_ir", QUALITY_GATE_THRESHOLDS["walk_forward_ic_ir_min"], ">="),
        "purged_kfold_ic": ("pkf_ic", QUALITY_GATE_THRESHOLDS["purged_kfold_ic_min"], ">="),
        "bootstrap_ci_lower": ("bootstrap_ci_lower", QUALITY_GATE_THRESHOLDS["bootstrap_ci_lower_min"], ">="),
        "param_sensitivity": ("param_sensitivity", QUALITY_GATE_THRESHOLDS["param_sensitivity_max"], "<="),
    }
    passed_checks: list[str] = []
    failed_checks: list[str] = []
    for check_name, (key, threshold, op) in check_map.items():
        value = gate.get(key)
        if value is None:
            failed_checks.append(check_name)
            continue
        try:
            val = float(value)
        except (TypeError, ValueError):
            failed_checks.append(check_name)
            continue
        if op == ">=" and val >= threshold:
            passed_checks.append(check_name)
        elif op == "<=" and val <= threshold:
            passed_checks.append(check_name)
        else:
            failed_checks.append(check_name)

    # 5th check: multi-period robustness (from period_robustness dict in gate)
    pr = gate.get("period_robustness") or {}
    first_ic = pr.get("first_half_ic")
    second_ic = pr.get("second_half_ic")
    if first_ic is not None and second_ic is not None:
        try:
            f_ic, s_ic = float(first_ic), float(second_ic)
            direction_consistent = not (f_ic > 0.01 and s_ic < -0.01) and not (f_ic < -0.01 and s_ic > 0.01)
            both_non_negative = f_ic >= -0.02 and s_ic >= -0.02
            if both_non_negative and direction_consistent:
                passed_checks.append("multi_period_robustness")
            else:
                failed_checks.append("multi_period_robustness")
        except (TypeError, ValueError):
            failed_checks.append("multi_period_robustness")
    else:
        # Data not available — treat as not checked (don't count as failed)
        pass

    return len(passed_checks), passed_checks, failed_checks


# 临时孵化要求至少通过的统计检查项数（5 项中至少通过 2 项）
PROVISIONAL_MIN_STATISTICAL_CHECKS_PASSED = 2

# --- strategy_mgr_helpers_parts/actions.py ---
def maybe_grant_provisional_incubation(
    strategy: Optional[dict],
    quality_gate: Optional[dict],
    *,
    validation_report: Optional[dict] = None,
    risk_report: Optional[dict] = None,
    backtest_metrics: Optional[dict] = None,
) -> dict:
    gate = normalize_quality_gate_result(quality_gate)
    if gate.get("passed"):
        return gate
    if not is_factory_ai_prototype_strategy(strategy):
        return gate
    if not has_only_statistical_gate_failures(gate):
        return gate

    # Fix #3: 风险报告为空时不能通过临时孵化（0.0 默认值会绕过阈值检查）
    if not risk_report:
        return gate

    # Fix #7: AI 原型不能完全绕过统计验证 — 至少通过 4 项中的 2 项
    checks_passed, passed_names, failed_names = _count_statistical_checks_passed(gate)
    if checks_passed < PROVISIONAL_MIN_STATISTICAL_CHECKS_PASSED:
        logger.info(
            "Provisional incubation denied: only %d/%d statistical checks passed (%s failed)",
            checks_passed, checks_passed + len(failed_names), ", ".join(failed_names),
        )
        return gate

    metrics = dict(backtest_metrics or {})
    # Fix #1/#2: 使用独立的临时孵化阈值，比回测初筛更严格
    sharpe_ratio = safe_metric_value(metrics, "sharpe_ratio")
    max_drawdown = abs(safe_metric_value(metrics, "max_drawdown"))
    trades_count = safe_metric_value(metrics, "trade_count", "trades_count")
    if (
        sharpe_ratio < PROVISIONAL_PASS_THRESHOLDS["sharpe_min"]
        or max_drawdown > PROVISIONAL_PASS_THRESHOLDS["mdd_max"]
        or trades_count < PROVISIONAL_PASS_THRESHOLDS["trades_min"]
    ):
        return gate

    risk = dict(risk_report or {})
    var_percent = safe_metric_value(risk, "var_percent")
    cvar_percent = safe_metric_value(risk, "cvar_percent")
    stress_loss_percent = safe_metric_value(risk, "stress_loss_percent")
    if (
        var_percent > RISK_REPORT_THRESHOLDS["var_percent_max"]
        or cvar_percent > RISK_REPORT_THRESHOLDS["cvar_percent_max"]
        or stress_loss_percent <= RISK_REPORT_THRESHOLDS["stress_loss_percent_min"]
    ):
        return gate

    validation = dict(validation_report or {})
    rating = dict(validation.get("rating") or {})
    validation_grade = str(rating.get("grade") or "").strip().upper()

    warnings = list(gate.get("reasons") or [])
    if validation_grade == "D" and "validation_grade_d" not in warnings:
        warnings.append("validation_grade_d")
    # 将未通过的统计检查加入 warnings（而非彻底忽略）
    for fname in failed_names:
        tag = f"provisional_skip:{fname}"
        if tag not in warnings:
            warnings.append(tag)
    warnings = list(dict.fromkeys(warnings))
    return normalize_quality_gate_result({
        **gate,
        "passed": True,
        "passed_strict": False,
        "provisional_pass": True,
        "review_mode": "incubation_only",
        "reasons": [],
        "reason": "",
        "warnings": warnings,
        "original_reasons": gate.get("reasons") or [],
        "original_reason_codes": gate.get("reason_codes") or [],
        "statistical_checks_passed": checks_passed,
        "statistical_checks_passed_names": passed_names,
        "statistical_checks_failed_names": failed_names,
    })


def build_quality_report(
    strategy_id: str,
    strategy_type: Optional[str],
    quality_gate: Optional[dict],
    validation_report: Optional[dict],
    risk_report: Optional[dict],
    dedup_report: Optional[dict],
    backtest_metrics: Optional[dict],
    snapshot: Optional[dict],
    status_after_review: Optional[str],
    review_source: str,
    report_type: str,
    spawn_reason: Optional[str] = None,
    submission_audit: Optional[dict] = None,
) -> dict:
    return _shared_build_quality_report(
        strategy_id=strategy_id,
        strategy_type=strategy_type,
        quality_gate=quality_gate,
        validation_report=validation_report,
        risk_report=risk_report,
        dedup_report=dedup_report,
        backtest_metrics=backtest_metrics,
        snapshot=snapshot,
        status_after_review=status_after_review,
        review_source=review_source,
        report_type=report_type,
        spawn_reason=spawn_reason,
        submission_audit=submission_audit,
    )


def normalize_quality_report_contract(
    report: Optional[dict],
    *,
    strategy_id: Optional[str] = None,
    strategy_type: Optional[str] = None,
    default_review_source: str = "strategy_manager.review_report",
) -> dict:
    raw = dict(report or {})
    if not raw:
        return {}

    summary = dict(raw.get("summary") or {})
    quality_gate = dict(raw.get("quality_gate") or {})
    validation_profile = dict(raw.get("validation_profile") or {})
    run_correction = dict(raw.get("run_correction") or {})
    attempt_adjustment = dict(raw.get("attempt_adjustment") or {})
    backtest_metrics = dict(raw.get("backtest_metrics") or {})

    mirrored_backtest_fields = (
        "constraint_check",
        "event_window_config",
        "event_window_metrics",
        "position_assumption",
        "cost_assumptions",
        "explicit_cost_breakdown",
        "implicit_cost_breakdown",
        "tradability_summary",
        "capacity_summary",
        "implementation_shortfall_model_source",
        "implementation_shortfall_components",
        "backtest_assumptions",
    )
    for field_name in mirrored_backtest_fields:
        if backtest_metrics.get(field_name) in (None, "", [], {}) and raw.get(field_name) not in (None, "", [], {}):
            backtest_metrics[field_name] = deepcopy(raw.get(field_name))

    if quality_gate.get("attempt_adjustment") in (None, "", [], {}) and attempt_adjustment:
        quality_gate["attempt_adjustment"] = attempt_adjustment
    if not quality_gate.get("primary_validation_layer"):
        quality_gate["primary_validation_layer"] = (
            summary.get("primary_validation_layer")
            or validation_profile.get("primary_validation_layer")
        )
    if not quality_gate.get("profile"):
        quality_gate["profile"] = validation_profile.get("profile")
    if not quality_gate.get("validation_focus"):
        quality_gate["validation_focus"] = validation_profile.get("validation_focus")
    run_correction_key_map = {
        "mode": "run_correction_mode",
        "raw_sharpe_proxy": "raw_sharpe_proxy",
        "deflated_sharpe_proxy": "deflated_sharpe_proxy",
        "pbo_proxy": "pbo_proxy",
        "reality_check_pvalue_proxy": "reality_check_pvalue_proxy",
        "spa_pvalue_proxy": "spa_pvalue_proxy",
        "multiple_testing_mode": "multiple_testing_mode",
        "deflated_sharpe_ratio": "deflated_sharpe_ratio",
        "deflated_sharpe_reference_sharpe": "deflated_sharpe_reference_sharpe",
        "deflated_sharpe_effective_trials": "deflated_sharpe_effective_trials",
        "pbo": "pbo",
        "white_reality_check_pvalue": "white_reality_check_pvalue",
        "hansen_spa_pvalue": "hansen_spa_pvalue",
        "multiple_testing": "multiple_testing",
    }
    for source_key, target_key in run_correction_key_map.items():
        if quality_gate.get(target_key) in (None, "", [], {}) and run_correction.get(source_key) not in (None, "", [], {}):
            quality_gate[target_key] = deepcopy(run_correction.get(source_key))

    submission_audit_fields = (
        "committee_review",
        "task_signature",
        "refresh_mode",
        "submission_lane",
        "direct_trade_candidate",
        "live_review_ready",
        "paper_lane_ready",
        "paper_account_id",
        "paper_account_status",
        "runtime_control_mode",
        "runtime_control_status",
        "promotion_review_id",
        "promotion_review_status",
        "promotion_review_recommendation",
        "pool_admission_applied",
        "promotion_applied_transition",
        "formal_track_requested",
        "formal_track_eligible",
        "formal_track_blockers",
        "submission_action",
        "submission_action_type",
        "submission_action_trigger",
        "submission_action_gaps",
        "submission_action_fallback_conditions",
        "submission_action_next_step",
        "submission_action_completed",
        "task_preference",
        "candidate_provenance",
    )
    submission_audit = {}
    for field_name in submission_audit_fields:
        value = raw.get(field_name)
        if value in (None, "", [], {}):
            value = summary.get(field_name)
        if value not in (None, "", [], {}):
            submission_audit[field_name] = deepcopy(value)

    raw_strategy = raw.get("strategy")
    strategy_payload = dict(raw_strategy) if isinstance(raw_strategy, dict) else {}

    normalized = _shared_build_quality_report(
        strategy_id=str(strategy_id or summary.get("strategy_id") or raw.get("strategy_id") or "").strip(),
        strategy_type=(
            strategy_type
            or summary.get("strategy_type")
            or raw.get("strategy_type")
            or strategy_payload.get("strategy_type")
        ),
        quality_gate=quality_gate,
        validation_report=dict(raw.get("validation_report") or {}),
        risk_report=dict(raw.get("risk_report") or {}),
        dedup_report=dict(raw.get("dedup_report") or {}),
        backtest_metrics=backtest_metrics,
        snapshot=dict(raw.get("snapshot") or {}),
        status_after_review=summary.get("status_after_review") or raw.get("status_after_review"),
        review_source=summary.get("review_source") or default_review_source,
        report_type=str(raw.get("report_type") or "submission"),
        spawn_reason=summary.get("spawn_reason"),
        submission_audit=submission_audit or None,
    )
    normalized_summary = dict(normalized.get("summary") or {})
    for field_name in (
        "prediction_trace_id",
        "trace_id",
        "research_protocol_version",
        "candidate_contract_version",
        "spec_completeness",
        "field_provenance_summary",
        "completion_issues",
        "hard_failures",
        "gate_a",
        "gate_b",
        "gate_c",
    ):
        value = raw.get(field_name)
        if value in (None, "", [], {}):
            value = summary.get(field_name)
        if value in (None, "", [], {}):
            continue
        normalized[field_name] = deepcopy(value)
        normalized_summary[field_name] = deepcopy(value)
    normalized["summary"] = normalized_summary
    return {**raw, **normalized}


def normalize_factory_run_summary_contract(row: Optional[dict]) -> dict:
    raw = dict(row or {})
    if not raw:
        return {}
    dto = normalize_run_result_to_summary(raw).to_dict()
    detail_dto = normalize_run_result_to_detail(raw).to_dict()
    summary_payload = dict(raw.get("summary") or dto.get("summary") or {})
    try:
        from strategy_factory.api.market_views import (
            build_research_window_status,
            hydrate_full_market_topn_payload,
        )

        research_window = {
            **dict(raw.get("research_window") or summary_payload.get("research_window") or {}),
            **build_research_window_status(summary_payload),
        }
        full_market_topn = hydrate_full_market_topn_payload(
            raw.get("full_market_topn")
            or summary_payload.get("full_market_topn")
            or {}
        )
    except Exception:
        research_window = dict(raw.get("research_window") or summary_payload.get("research_window") or {})
        full_market_topn = dict(
            raw.get("full_market_topn")
            or summary_payload.get("full_market_topn")
            or {}
        )
    return {
        **raw,
        **dto,
        "artifact_refs": list(raw.get("artifact_refs") or dto.get("artifact_refs") or []),
        "parity_result": dict(raw.get("parity_result") or dto.get("parity_result") or {}),
        "submission_artifact": dict(
            raw.get("submission_artifact") or detail_dto.get("submission_artifact") or {}
        ),
        "research_window": research_window,
        "full_market_topn": full_market_topn,
    }


def merge_factory_run_summary_observability(
    summary: Optional[dict],
    payload: Optional[dict],
) -> dict:
    merged = dict(summary or {})
    source = dict(payload or {})
    for field in _FACTORY_SUMMARY_OBSERVABILITY_FIELDS:
        value = source.get(field)
        if value in (None, "", [], {}):
            continue
        merged[field] = value
    return merged


def normalize_factory_run_detail_contract(row: Optional[dict]) -> dict:
    raw = dict(row or {})
    if not raw:
        return {}
    dto = normalize_run_result_to_detail(raw).to_dict()
    summary_payload = dict(raw.get("summary") or dto.get("summary") or {})
    try:
        from strategy_factory.api.market_views import (
            build_research_window_status,
            hydrate_full_market_topn_payload,
        )

        research_window = {
            **dict(raw.get("research_window") or summary_payload.get("research_window") or {}),
            **build_research_window_status(summary_payload),
        }
        full_market_topn = hydrate_full_market_topn_payload(
            raw.get("full_market_topn")
            or summary_payload.get("full_market_topn")
            or dict(dict(raw.get("stages") or {}).get("autonomy") or {}).get("full_market_topn")
            or {}
        )
    except Exception:
        research_window = dict(raw.get("research_window") or summary_payload.get("research_window") or {})
        full_market_topn = dict(
            raw.get("full_market_topn")
            or summary_payload.get("full_market_topn")
            or dict(dict(raw.get("stages") or {}).get("autonomy") or {}).get("full_market_topn")
            or {}
        )
    raw_submission_artifact = dict(raw.get("submission_artifact") or {})
    submission_artifact = dict(dto.get("submission_artifact") or {})
    if raw_submission_artifact:
        for key, value in raw_submission_artifact.items():
            if value in (None, "", [], {}):
                continue
            submission_artifact[key] = deepcopy(value)
    raw_stages = dict(raw.get("stages") or {})
    stage_payloads = {
        name: dict(payload or {})
        for name, payload in raw_stages.items()
        if isinstance(payload, dict)
    }
    stage_storage_meta = {
        name: payload
        for name, payload in raw_stages.items()
        if not isinstance(payload, dict)
    }
    return {
        **raw,
        **dto,
        "summary": merge_factory_run_summary_observability(
            raw.get("summary") or {},
            dto,
        ),
        "artifact_refs": list(raw.get("artifact_refs") or dto.get("artifact_refs") or []),
        "parity_result": dict(raw.get("parity_result") or dto.get("parity_result") or {}),
        "stages": stage_payloads or dict(dto.get("stages") or {}),
        "stage_storage_meta": stage_storage_meta,
        "snapshot_summary": dict(raw.get("snapshot_summary") or dto.get("snapshot_summary") or {}),
        "quality_gate": dict(raw.get("quality_gate") or raw.get("gate_report") or dto.get("quality_gate") or {}),
        "research_summary": dict(dto.get("research_summary") or {}),
        "research_plane": dict(dto.get("research_plane") or raw.get("research_plane") or {}),
        "research_artifact": dict(dto.get("research_artifact") or {}),
        "task_artifact": dict(dto.get("task_artifact") or {}),
        "candidate_artifact": dict(dto.get("candidate_artifact") or {}),
        "evidence_artifact": dict(dto.get("evidence_artifact") or {}),
        "governance_plane": dict(dto.get("governance_plane") or raw.get("governance_plane") or {}),
        "gate_artifact": dict(dto.get("gate_artifact") or {}),
        "gate_artifact_v2": dict(dto.get("gate_artifact_v2") or {}),
        "dedup_artifact": dict(dto.get("dedup_artifact") or {}),
        "submission_artifact": submission_artifact,
        "governance_evidence_artifact": dict(dto.get("governance_evidence_artifact") or {}),
        "gate_a": dict(dto.get("gate_a") or {}),
        "gate_b": dict(dto.get("gate_b") or {}),
        "gate_c": dict(dto.get("gate_c") or {}),
        "protocol_versions": dict(dto.get("protocol_versions") or {}),
        "prediction_trace_summary": dict(dto.get("prediction_trace_summary") or {}),
        "prediction_trace_ledger": dict(dto.get("prediction_trace_ledger") or {}),
        "feedback_summary": dict(dto.get("feedback_summary") or {}),
        "incubation_summary": dict(dto.get("incubation_summary") or {}),
        "live_ready_summary": dict(dto.get("live_ready_summary") or {}),
        "research_window": research_window,
        "full_market_topn": full_market_topn,
    }


def _build_capability_health_entry(
    *,
    supported: bool,
    enabled: bool,
    dependency_ready: bool,
    recent_run_ready: bool,
    degraded_reason: Optional[str] = None,
) -> dict[str, Any]:
    healthy = bool(supported and enabled and dependency_ready and recent_run_ready)
    reason = str(degraded_reason or "").strip() or None
    if reason is None:
        if not supported:
            reason = "storage_contract_missing"
        elif not enabled:
            reason = "feature_disabled"
        elif not dependency_ready:
            reason = "dependency_probe_failed"
        elif not recent_run_ready:
            reason = "missing_recent_run_evidence"
    return {
        "supported": bool(supported),
        "enabled": bool(enabled),
        "healthy": bool(healthy),
        "degraded_reason": reason,
        "last_checked_at": datetime.now(timezone.utc).isoformat(),
    }


def build_factory_capability_health(
    db,
    *,
    factory_constants: Optional[dict[str, Any]] = None,
    latest_run: Optional[dict[str, Any]] = None,
) -> dict[str, dict[str, Any]]:
    constants = dict(factory_constants or {})
    run_payload = dict(latest_run or {})
    parity_result = dict(run_payload.get("parity_result") or {})
    latest_status = str(run_payload.get("status") or "").strip().lower()
    parity_status = str(parity_result.get("status") or "").strip().lower()
    recent_run_ready = bool(run_payload) and latest_status not in {"failed", "error"}
    recent_run_reason = None
    if not run_payload:
        recent_run_reason = "missing_recent_run_evidence"
    elif latest_status in {"failed", "error"}:
        recent_run_reason = "latest_run_failed"
    elif parity_status in {"mismatch", "shadow_failed"}:
        recent_run_ready = False
        recent_run_reason = "latest_parity_mismatch"

    def _entry(
        *,
        supported: bool,
        enabled: bool = True,
        dependency_ready: bool = True,
        require_recent_run: bool = False,
    ) -> dict[str, Any]:
        return _build_capability_health_entry(
            supported=supported,
            enabled=enabled,
            dependency_ready=dependency_ready,
            recent_run_ready=(recent_run_ready if require_recent_run else True),
            degraded_reason=(recent_run_reason if require_recent_run else None),
        )

    health = {
        "factory_runs": _entry(
            supported=hasattr(db, "save_strategy_factory_run") and hasattr(db, "get_latest_strategy_factory_run"),
            require_recent_run=True,
        ),
        "factory_dispatch": _entry(
            supported=hasattr(db, "create_strategy_factory_dispatch")
            and hasattr(db, "get_strategy_factory_dispatch"),
            require_recent_run=True,
        ),
        "factory_bulk_lane": _entry(
            supported=hasattr(db, "list_stock_universe") and hasattr(db, "save_strategy_factory_run"),
            enabled=bool(constants.get("STOCK_STRATEGY_MATRIX_ENABLED")),
            require_recent_run=True,
        ),
        "paper_incubation": _entry(
            supported=hasattr(db, "save_strategy_incubation_account")
            and hasattr(db, "save_strategy_incubation_metric"),
        ),
        "paper_trading": _entry(
            supported=hasattr(db, "save_paper_account")
            and hasattr(db, "save_paper_order")
            and hasattr(db, "get_paper_nav_rows"),
        ),
        "incubation_pipeline": _entry(
            supported=hasattr(db, "save_strategy_incubation_pipeline_snapshot")
            and hasattr(db, "list_strategy_incubation_metrics"),
        ),
        "runtime_risk": _entry(
            supported=hasattr(db, "save_strategy_runtime_risk_event"),
        ),
        "runtime_controls": _entry(
            supported=hasattr(db, "save_strategy_runtime_control")
            and hasattr(db, "get_strategy_runtime_control"),
        ),
        "runtime_alerting": _entry(
            supported=hasattr(db, "save_strategy_runtime_alert")
            and hasattr(db, "list_strategy_runtime_alerts"),
        ),
        "promotion_pipeline": _entry(
            supported=hasattr(db, "save_strategy_promotion_review")
            and hasattr(db, "get_latest_strategy_promotion_review"),
        ),
        "projection_snapshots": _entry(
            supported=hasattr(db, "save_strategy_projection_snapshot")
            and hasattr(db, "get_latest_strategy_projection_snapshot"),
        ),
        "vector_platform": _entry(
            supported=hasattr(db, "save_strategy_vector_profile")
            and hasattr(db, "save_vector_index_registry"),
        ),
        "ai_generation": _entry(
            supported=hasattr(db, "save_strategy_generation_experiment")
            and hasattr(db, "save_strategy_task_run"),
        ),
        "quality_governance": _entry(
            supported=hasattr(db, "save_strategy_quality_report")
            and hasattr(db, "list_strategy_status_events"),
        ),
        "runtime_cycle": _entry(
            supported=hasattr(db, "save_strategy_task_run")
            and hasattr(db, "save_strategy_incubation_metric"),
        ),
    }
    return health

# --- strategy_mgr_helpers_parts/orchestrator.py ---
def _summarize_factory_submission_briefs(strategy_briefs: list[dict[str, Any]]) -> dict[str, Any]:
    briefs = [dict(item or {}) for item in list(strategy_briefs or []) if isinstance(item, dict)]
    strategy_count = len(briefs)
    validation_grade_distribution: dict[str, int] = {}
    raw_validation_grade_distribution: dict[str, int] = {}
    effective_validation_grade_distribution: dict[str, int] = {}
    raw_validation_total_scores: list[float] = []
    strict_incubation_ready_count = 0
    live_candidate_ready_count = 0
    raw_b_or_above_count = 0
    strict_ready_given_raw_b_count = 0
    live_ready_given_raw_b_count = 0
    strict_live_alignment_gap_count = 0
    strict_live_alignment_status_counts: dict[str, int] = {}
    candidate_local_attempt_count = 0
    task_local_attempt_count = 0
    cohort_effective_trials = 0.0
    economic_semantics_missing_count = 0
    unique_family_holding_universe: set[tuple[str, str, str]] = set()
    for brief in briefs:
        validation_grade = str(brief.get("validation_grade") or "").strip().upper()
        raw_validation_grade = str(
            brief.get("raw_validation_grade") or validation_grade or ""
        ).strip().upper()
        effective_validation_grade = str(
            brief.get("effective_validation_grade") or validation_grade or ""
        ).strip().upper()
        strict_ready = brief.get("strict_incubation_ready") is True
        live_ready = brief.get("live_candidate_ready") is True
        if strict_ready and live_ready:
            alignment_status = "aligned_live_ready"
        elif strict_ready:
            alignment_status = "strict_only_gap"
        elif live_ready:
            alignment_status = "live_ready_without_strict"
        else:
            alignment_status = "aligned_blocked"
        strict_live_alignment_status_counts[alignment_status] = (
            strict_live_alignment_status_counts.get(alignment_status, 0) + 1
        )
        if validation_grade:
            validation_grade_distribution[validation_grade] = (
                validation_grade_distribution.get(validation_grade, 0) + 1
            )
        if raw_validation_grade:
            raw_validation_grade_distribution[raw_validation_grade] = (
                raw_validation_grade_distribution.get(raw_validation_grade, 0) + 1
            )
        if effective_validation_grade:
            effective_validation_grade_distribution[effective_validation_grade] = (
                effective_validation_grade_distribution.get(effective_validation_grade, 0) + 1
            )
        if brief.get("raw_validation_total_score") is not None:
            raw_validation_total_scores.append(_safe_float(brief.get("raw_validation_total_score")))
        if strict_ready:
            strict_incubation_ready_count += 1
        if live_ready:
            live_candidate_ready_count += 1
        if strict_ready and not live_ready:
            strict_live_alignment_gap_count += 1
        if _is_raw_b_or_above(raw_validation_grade):
            raw_b_or_above_count += 1
            if strict_ready:
                strict_ready_given_raw_b_count += 1
            if live_ready:
                live_ready_given_raw_b_count += 1
        candidate_local_attempt_count += int(brief.get("candidate_local_attempt_count") or 0)
        task_local_attempt_count += int(brief.get("task_local_attempt_count") or 0)
        cohort_effective_trials += _safe_float(brief.get("cohort_effective_trials"), 0.0)
        if brief.get("economic_semantics_missing") is True:
            economic_semantics_missing_count += 1
        unique_family_holding_universe.add(
            (
                str(brief.get("candidate_family") or brief.get("strategy_type") or "unknown").strip().lower() or "unknown",
                _brief_holding_bucket(brief),
                _brief_target_universe_key(brief),
            )
        )

    summary = {
        "strategy_count": strategy_count,
        "validation_grade_distribution": validation_grade_distribution,
        "raw_validation_grade_distribution": raw_validation_grade_distribution,
        "effective_validation_grade_distribution": effective_validation_grade_distribution,
        "raw_validation_total_score_mean": round(
            sum(raw_validation_total_scores) / len(raw_validation_total_scores),
            4,
        ) if raw_validation_total_scores else 0.0,
        "raw_validation_total_score_p50": _percentile(raw_validation_total_scores, 0.5),
        "raw_validation_total_score_p90": _percentile(raw_validation_total_scores, 0.9),
        "strict_incubation_ready_count": strict_incubation_ready_count,
        "strict_incubation_ready_rate": _rate(strict_incubation_ready_count, strategy_count),
        "live_candidate_ready_count": live_candidate_ready_count,
        "live_candidate_ready_rate": _rate(live_candidate_ready_count, strategy_count),
        "raw_b_or_above_count": raw_b_or_above_count,
        "raw_b_or_above_rate": _rate(raw_b_or_above_count, strategy_count),
        "strict_ready_given_raw_b_count": strict_ready_given_raw_b_count,
        "strict_ready_given_raw_b_rate": _rate(
            strict_ready_given_raw_b_count,
            raw_b_or_above_count,
        ),
        "live_ready_given_raw_b_count": live_ready_given_raw_b_count,
        "live_ready_given_raw_b_rate": _rate(
            live_ready_given_raw_b_count,
            raw_b_or_above_count,
        ),
        "strict_live_alignment_gap_count": strict_live_alignment_gap_count,
        "strict_live_alignment_gap_rate": _rate(
            strict_live_alignment_gap_count,
            strategy_count,
        ),
        "strict_live_alignment_status_counts": strict_live_alignment_status_counts,
        "validation_family_quality_panel": _build_family_quality_panel(briefs),
        "candidate_local_attempt_count": candidate_local_attempt_count,
        "task_local_attempt_count": task_local_attempt_count,
        "cohort_effective_trials": round(cohort_effective_trials, 4),
        "unique_family_holding_universe_count": len(unique_family_holding_universe),
        "economic_semantics_missing_count": economic_semantics_missing_count,
    }
    summary.update(_grade_rates(raw_validation_grade_distribution, strategy_count))
    summary.update(_summarize_high_confidence_quality(briefs))
    return summary


def _normalize_reason_codes(values: Any) -> list[str]:
    if values in (None, "", [], {}):
        return []
    items = values if isinstance(values, (list, tuple, set)) else [values]
    codes: list[str] = []
    for value in items:
        code = str(value or "").strip()
        if code and code not in codes:
            codes.append(code)
    return codes


def _top_reason_counts(reason_counts: dict[str, int], *, limit: int = 5) -> list[dict[str, Any]]:
    items = [
        {"reason_code": str(reason_code or ""), "count": int(count or 0)}
        for reason_code, count in dict(reason_counts or {}).items()
        if str(reason_code or "").strip() and int(count or 0) > 0
    ]
    items.sort(key=lambda item: (-int(item.get("count") or 0), str(item.get("reason_code") or "")))
    return items[: max(1, int(limit or 5))]


def _top_named_counts(
    counts: dict[str, int],
    *,
    name_key: str,
    limit: int = 5,
) -> list[dict[str, Any]]:
    items = [
        {name_key: str(name or ""), "count": int(count or 0)}
        for name, count in dict(counts or {}).items()
        if str(name or "").strip() and int(count or 0) > 0
    ]
    items.sort(key=lambda item: (-int(item.get("count") or 0), str(item.get(name_key) or "")))
    return items[: max(1, int(limit or 5))]


def _normalize_count_map(value: Any) -> dict[str, int]:
    if not isinstance(value, dict):
        return {}
    normalized: dict[str, int] = {}
    for raw_key, raw_value in dict(value or {}).items():
        key = str(raw_key or "").strip()
        if not key:
            continue
        try:
            count = int(raw_value or 0)
        except Exception as _swallowed:
            logger.warning("packages/akshare-mcp/src/akshare_mcp/tools/managers/strategy_mgr_helpers.py:2000: swallowed %s and continued: %s", type(_swallowed).__name__, _swallowed)
            continue
        if count <= 0:
            continue
        normalized[key] = count
    return normalized


def _extract_run_metric_value(payload: Optional[dict], field: str) -> Any:
    data = dict(payload or {})
    summary = dict(data.get("summary") or {})
    for container in (data, summary):
        if field not in container:
            continue
        value = container.get(field)
        if value in (None, "", [], {}):
            continue
        return value
    return None


def _extract_run_metric_or_stage_value(
    payload: Optional[dict],
    field: str,
    *,
    stage_name: str | None = None,
) -> Any:
    data = dict(payload or {})
    summary = dict(data.get("summary") or {})
    stages = dict(data.get("stages") or {})
    containers: list[dict[str, Any]] = []
    if stage_name:
        containers.append(dict(stages.get(stage_name) or {}))
    containers.extend((summary, data))
    for container in containers:
        if field not in container:
            continue
        value = container.get(field)
        if value in (None, "", [], {}):
            continue
        return value
    return None


def _append_numeric(values: list[float], raw_value: Any) -> None:
    if raw_value in (None, "", [], {}):
        return
    values.append(_safe_float(raw_value))


def _is_governed_reason_code(reason_code: str) -> bool:
    code = str(reason_code or "").strip().lower()
    return code.startswith("governed_candidate_pool_") or code == "factor_scheduler_recent_success_without_governed_pool"


def _is_evidence_debt_reason_code(reason_code: str) -> bool:
    code = str(reason_code or "").strip().lower()
    return code.startswith("incubating_") or code.startswith("budget_feedback_")


def _extract_factory_run_readiness_snapshot(payload: Optional[dict]) -> dict[str, Any]:
    data = dict(payload or {})
    summary = dict(data.get("summary") or {})
    stages = dict(data.get("stages") or {})
    readiness_stage = dict(stages.get("readiness") or {})
    can_proceed = summary.get("factory_readiness_can_proceed")
    if can_proceed is None:
        can_proceed = readiness_stage.get("can_proceed")
    decision = str(
        summary.get("factory_readiness_decision")
        or readiness_stage.get("decision")
        or ""
    ).strip().lower()
    if decision not in {"proceed", "blocked"}:
        if can_proceed is False or str(data.get("status") or "").strip().lower() == "skipped":
            decision = "blocked"
        else:
            decision = "proceed"
    blocker_codes = _normalize_reason_codes(
        summary.get("factory_readiness_effective_blocking_reason_codes")
        or summary.get("factory_readiness_blocking_reason_codes")
        or readiness_stage.get("effective_blocking_reason_codes")
        or readiness_stage.get("blocking_reason_codes")
        or readiness_stage.get("blockers")
    )
    raw_blocker_codes = _normalize_reason_codes(
        summary.get("factory_readiness_blocking_reason_codes")
        or readiness_stage.get("blocking_reason_codes")
        or readiness_stage.get("blockers")
    )
    warning_codes = _normalize_reason_codes(
        readiness_stage.get("warning_reason_codes")
        or readiness_stage.get("warnings")
    )
    return {
        "decision": decision,
        "can_proceed": bool(can_proceed) if can_proceed is not None else decision != "blocked",
        "score": _extract_run_metric_value(data, "factory_readiness_score"),
        "blocking_stage": summary.get("factory_readiness_blocking_stage") or readiness_stage.get("blocking_stage"),
        "skip_reason": summary.get("skip_reason") or readiness_stage.get("skip_reason"),
        "blocker_count": int(
            summary.get("factory_readiness_blocker_count")
            or readiness_stage.get("blocker_count")
            or len(blocker_codes)
            or 0
        ),
        "warning_count": int(
            summary.get("factory_readiness_warning_count")
            or readiness_stage.get("warning_count")
            or len(warning_codes)
            or 0
        ),
        "blocking_reason_codes": blocker_codes,
        "raw_blocking_reason_codes": raw_blocker_codes,
        "blocking_reason_codes_source": (
            summary.get("factory_readiness_blocking_reason_codes_source")
            or readiness_stage.get("blocking_reason_codes_source")
        ),
        "warning_reason_codes": warning_codes,
        "governed_blocked_ratio": _extract_run_metric_or_stage_value(
            data,
            "governed_blocked_ratio",
            stage_name="readiness",
        ),
        "governed_blocked_candidate_count": _extract_run_metric_or_stage_value(
            data,
            "governed_blocked_candidate_count",
            stage_name="readiness",
        ),
        "governed_source_candidate_count": _extract_run_metric_or_stage_value(
            data,
            "governed_source_candidate_count",
            stage_name="readiness",
        ),
        "governed_candidate_pool_strict_shortfall_count": _extract_run_metric_or_stage_value(
            data,
            "governed_candidate_pool_strict_shortfall_count",
            stage_name="readiness",
        ),
        "budget_feedback_evidence_debt_ratio": _extract_run_metric_or_stage_value(
            data,
            "budget_feedback_evidence_debt_ratio",
            stage_name="readiness",
        ),
        "budget_feedback_zero_signal_ratio": _extract_run_metric_or_stage_value(
            data,
            "budget_feedback_zero_signal_ratio",
            stage_name="readiness",
        ),
        "budget_feedback_forward_window_coverage_ratio": _extract_run_metric_or_stage_value(
            data,
            "budget_feedback_forward_window_coverage_ratio",
            stage_name="readiness",
        ),
        "budget_feedback_promotion_ready_ratio": _extract_run_metric_or_stage_value(
            data,
            "budget_feedback_promotion_ready_ratio",
            stage_name="readiness",
        ),
        "budget_feedback_promotion_review_coverage_ratio": _extract_run_metric_or_stage_value(
            data,
            "budget_feedback_promotion_review_coverage_ratio",
            stage_name="readiness",
        ),
        "governed_blocking_reason_counts": _normalize_count_map(
            _extract_run_metric_or_stage_value(
                data,
                "governed_blocking_reason_counts",
                stage_name="readiness",
            )
        ),
        "governed_exclusion_reason_counts": _normalize_count_map(
            _extract_run_metric_or_stage_value(
                data,
                "governed_exclusion_reason_counts",
                stage_name="readiness",
            )
        ),
        "governed_pending_reason_counts": _normalize_count_map(
            _extract_run_metric_or_stage_value(
                data,
                "governed_pending_reason_counts",
                stage_name="readiness",
            )
        ),
        "governed_ineligible_reason_counts": _normalize_count_map(
            _extract_run_metric_or_stage_value(
                data,
                "governed_ineligible_reason_counts",
                stage_name="readiness",
            )
        ),
    }

# --- strategy_mgr_helpers_parts/part_6.py ---
def build_factory_recent_run_diagnostics(
    run_rows: list[dict[str, Any]] | None,
    *,
    limit: int = 5,
) -> dict[str, Any]:
    rows = [dict(item or {}) for item in list(run_rows or []) if isinstance(item, dict)]
    requested_limit = max(1, int(limit or 5))
    items = rows[:requested_limit]
    status_counts: dict[str, int] = {}
    readiness_decision_counts: dict[str, int] = {}
    blocker_reason_counts: dict[str, int] = {}
    warning_reason_counts: dict[str, int] = {}
    governed_warning_reason_counts: dict[str, int] = {}
    evidence_warning_reason_counts: dict[str, int] = {}
    governed_blocking_reason_counts: dict[str, int] = {}
    governed_exclusion_reason_counts: dict[str, int] = {}
    governed_pending_reason_counts: dict[str, int] = {}
    governed_ineligible_reason_counts: dict[str, int] = {}
    external_llm_provider_control_mode_counts: dict[str, int] = {}
    external_llm_provider_control_reason_counts: dict[str, int] = {}
    suppressed_generator_mode_counts: dict[str, int] = {}
    blocked_count = 0
    submit_stage_entered_count = 0
    submitted_positive_count = 0
    external_llm_provider_suppressed_run_count = 0
    external_llm_provider_cooldown_run_count = 0
    raw_b_or_above_rates: list[float] = []
    strict_ready_given_raw_b_rates: list[float] = []
    live_ready_given_raw_b_rates: list[float] = []
    strict_live_alignment_gap_rates: list[float] = []
    strict_live_gap_run_count = 0
    governed_blocked_ratios: list[float] = []
    governed_strict_shortfall_counts: list[float] = []
    governed_blocked_candidate_counts: list[float] = []
    governed_source_candidate_counts: list[float] = []
    evidence_debt_ratios: list[float] = []
    zero_signal_ratios: list[float] = []
    forward_window_coverage_ratios: list[float] = []
    promotion_ready_ratios: list[float] = []
    promotion_review_coverage_ratios: list[float] = []
    provider_stage_attempt_counts: list[float] = []
    provider_real_request_counts: list[float] = []
    provider_compatibility_skip_ratios: list[float] = []
    provider_compatibility_failure_ratios: list[float] = []
    provider_effective_response_ratios: list[float] = []
    provider_empty_200_response_ratios: list[float] = []
    provider_active_attempt_run_count = 0
    provider_zero_attempt_run_count = 0
    run_briefs: list[dict[str, Any]] = []

    for row in items:
        summary = dict(row.get("summary") or {})
        stages = dict(row.get("stages") or {})
        readiness = _extract_factory_run_readiness_snapshot(row)
        def _summary_metric(field: str) -> Any:
            if field not in summary:
                return None
            value = summary.get(field)
            return None if value in (None, "", [], {}) else value

        status_key = str(row.get("status") or "").strip().lower() or "unknown"
        status_counts[status_key] = status_counts.get(status_key, 0) + 1
        decision = str(readiness.get("decision") or "unknown").strip().lower() or "unknown"
        readiness_decision_counts[decision] = readiness_decision_counts.get(decision, 0) + 1
        if decision == "blocked":
            blocked_count += 1
        for reason_code in list(readiness.get("blocking_reason_codes") or []):
            blocker_reason_counts[reason_code] = blocker_reason_counts.get(reason_code, 0) + 1
        for reason_code in list(readiness.get("warning_reason_codes") or []):
            warning_reason_counts[reason_code] = warning_reason_counts.get(reason_code, 0) + 1
            if _is_governed_reason_code(reason_code):
                governed_warning_reason_counts[reason_code] = governed_warning_reason_counts.get(reason_code, 0) + 1
            if _is_evidence_debt_reason_code(reason_code):
                evidence_warning_reason_counts[reason_code] = evidence_warning_reason_counts.get(reason_code, 0) + 1
        for reason_code, count in dict(readiness.get("governed_blocking_reason_counts") or {}).items():
            governed_blocking_reason_counts[reason_code] = (
                governed_blocking_reason_counts.get(reason_code, 0) + int(count or 0)
            )
        for reason_code, count in dict(readiness.get("governed_exclusion_reason_counts") or {}).items():
            governed_exclusion_reason_counts[reason_code] = (
                governed_exclusion_reason_counts.get(reason_code, 0) + int(count or 0)
            )
        for reason_code, count in dict(readiness.get("governed_pending_reason_counts") or {}).items():
            governed_pending_reason_counts[reason_code] = (
                governed_pending_reason_counts.get(reason_code, 0) + int(count or 0)
            )
        for reason_code, count in dict(readiness.get("governed_ineligible_reason_counts") or {}).items():
            governed_ineligible_reason_counts[reason_code] = (
                governed_ineligible_reason_counts.get(reason_code, 0) + int(count or 0)
            )
        external_llm_provider_control_mode = _normalized_text(
            _summary_metric("external_llm_provider_control_mode")
            or row.get("external_llm_provider_control_mode")
        )
        external_llm_provider_control_reasons = _normalize_reason_codes(
            _summary_metric("external_llm_provider_control_reasons")
            or row.get("external_llm_provider_control_reasons")
        )
        feedback_generator_mode_control_mode_counts = {
            _normalized_text(key): int(value or 0)
            for key, value in dict(
                _summary_metric("feedback_generator_mode_control_mode_counts")
                or row.get("feedback_generator_mode_control_mode_counts")
                or {}
            ).items()
            if _normalized_text(key)
        }
        suppressed_generator_modes = [
            _normalized_text(item)
            for item in _normalize_reason_codes(
                _summary_metric("suppressed_generator_modes")
                or row.get("suppressed_generator_modes")
            )
            if _normalized_text(item)
        ]
        if external_llm_provider_control_mode:
            external_llm_provider_control_mode_counts[external_llm_provider_control_mode] = (
                external_llm_provider_control_mode_counts.get(external_llm_provider_control_mode, 0) + 1
            )
        for reason_code in external_llm_provider_control_reasons:
            external_llm_provider_control_reason_counts[reason_code] = (
                external_llm_provider_control_reason_counts.get(reason_code, 0) + 1
            )
        for generator_mode in suppressed_generator_modes:
            suppressed_generator_mode_counts[generator_mode] = (
                suppressed_generator_mode_counts.get(generator_mode, 0) + 1
            )
        external_llm_provider_suppressed = bool(
            external_llm_provider_control_mode == "suppress"
            or "external_llm" in suppressed_generator_modes
            or int(feedback_generator_mode_control_mode_counts.get("suppress") or 0) > 0
        )
        external_llm_provider_cooldown = external_llm_provider_control_mode == "cooldown"
        if external_llm_provider_suppressed:
            external_llm_provider_suppressed_run_count += 1
        if external_llm_provider_cooldown:
            external_llm_provider_cooldown_run_count += 1

        governed_blocked_ratio = (
            _safe_float(readiness.get("governed_blocked_ratio"))
            if readiness.get("governed_blocked_ratio") not in (None, "", [], {})
            else None
        )
        governed_strict_shortfall_count = (
            int(readiness.get("governed_candidate_pool_strict_shortfall_count") or 0)
            if readiness.get("governed_candidate_pool_strict_shortfall_count") not in (None, "", [], {})
            else None
        )
        governed_blocked_candidate_count = (
            int(readiness.get("governed_blocked_candidate_count") or 0)
            if readiness.get("governed_blocked_candidate_count") not in (None, "", [], {})
            else None
        )
        governed_source_candidate_count = (
            int(readiness.get("governed_source_candidate_count") or 0)
            if readiness.get("governed_source_candidate_count") not in (None, "", [], {})
            else None
        )
        evidence_debt_ratio = (
            _safe_float(readiness.get("budget_feedback_evidence_debt_ratio"))
            if readiness.get("budget_feedback_evidence_debt_ratio") not in (None, "", [], {})
            else None
        )
        zero_signal_ratio = (
            _safe_float(readiness.get("budget_feedback_zero_signal_ratio"))
            if readiness.get("budget_feedback_zero_signal_ratio") not in (None, "", [], {})
            else None
        )
        forward_window_coverage_ratio = (
            _safe_float(readiness.get("budget_feedback_forward_window_coverage_ratio"))
            if readiness.get("budget_feedback_forward_window_coverage_ratio") not in (None, "", [], {})
            else None
        )
        promotion_ready_ratio = (
            _safe_float(readiness.get("budget_feedback_promotion_ready_ratio"))
            if readiness.get("budget_feedback_promotion_ready_ratio") not in (None, "", [], {})
            else None
        )
        promotion_review_coverage_ratio = (
            _safe_float(readiness.get("budget_feedback_promotion_review_coverage_ratio"))
            if readiness.get("budget_feedback_promotion_review_coverage_ratio") not in (None, "", [], {})
            else None
        )
        external_llm_stage_attempt_count = int(_summary_metric("external_llm_stage_attempt_count") or 0)
        external_llm_real_request_count = int(_summary_metric("external_llm_real_request_count") or 0)
        external_llm_compatibility_skip_count = int(
            _summary_metric("external_llm_compatibility_skip_count") or 0
        )
        external_llm_compatibility_failure_count = int(
            _summary_metric("external_llm_compatibility_failure_count") or 0
        )
        external_llm_effective_response_count = int(
            _summary_metric("external_llm_effective_response_count") or 0
        )
        external_llm_empty_200_response_count = int(
            _summary_metric("external_llm_empty_200_response_count") or 0
        )
        external_llm_compatibility_skip_ratio = (
            round(external_llm_compatibility_skip_count / external_llm_stage_attempt_count, 4)
            if external_llm_stage_attempt_count
            else 0.0
        )
        external_llm_compatibility_failure_ratio = (
            round(external_llm_compatibility_failure_count / external_llm_real_request_count, 4)
            if external_llm_real_request_count
            else 0.0
        )
        external_llm_effective_response_ratio = (
            round(external_llm_effective_response_count / external_llm_real_request_count, 4)
            if external_llm_real_request_count
            else 0.0
        )
        external_llm_empty_200_response_ratio = (
            round(external_llm_empty_200_response_count / external_llm_real_request_count, 4)
            if external_llm_real_request_count
            else 0.0
        )
        if external_llm_stage_attempt_count > 0 or external_llm_real_request_count > 0:
            provider_active_attempt_run_count += 1
        else:
            provider_zero_attempt_run_count += 1
        _append_numeric(governed_blocked_ratios, governed_blocked_ratio)
        _append_numeric(governed_strict_shortfall_counts, governed_strict_shortfall_count)
        _append_numeric(governed_blocked_candidate_counts, governed_blocked_candidate_count)
        _append_numeric(governed_source_candidate_counts, governed_source_candidate_count)
        _append_numeric(evidence_debt_ratios, evidence_debt_ratio)
        _append_numeric(zero_signal_ratios, zero_signal_ratio)
        _append_numeric(forward_window_coverage_ratios, forward_window_coverage_ratio)
        _append_numeric(promotion_ready_ratios, promotion_ready_ratio)
        _append_numeric(promotion_review_coverage_ratios, promotion_review_coverage_ratio)
        _append_numeric(provider_stage_attempt_counts, external_llm_stage_attempt_count)
        _append_numeric(provider_real_request_counts, external_llm_real_request_count)
        _append_numeric(provider_compatibility_skip_ratios, external_llm_compatibility_skip_ratio)
        _append_numeric(provider_compatibility_failure_ratios, external_llm_compatibility_failure_ratio)
        _append_numeric(provider_effective_response_ratios, external_llm_effective_response_ratio)
        _append_numeric(provider_empty_200_response_ratios, external_llm_empty_200_response_ratio)

        submit_stage = dict(stages.get("submit") or {})
        submit_stage_entered = bool(submit_stage) or any(
            int(_summary_metric(key) or 0) > 0
            for key in ("submitted", "research_only_count", "deferred_submission_count")
        )
        if submit_stage_entered:
            submit_stage_entered_count += 1
        submitted = int(_summary_metric("submitted") or 0)
        if submitted > 0:
            submitted_positive_count += 1

        raw_b_or_above_rate_value = _summary_metric("raw_b_or_above_rate")
        strict_ready_given_raw_b_rate_value = _summary_metric("strict_ready_given_raw_b_rate")
        live_ready_given_raw_b_rate_value = _summary_metric("live_ready_given_raw_b_rate")
        gap_rate_value = _summary_metric("strict_live_alignment_gap_rate")
        gap_count_value = _summary_metric("strict_live_alignment_gap_count")
        gap_status_counts = _summary_metric("strict_live_alignment_status_counts")
        if gap_count_value is None and isinstance(gap_status_counts, dict):
            gap_count_value = int(dict(gap_status_counts).get("strict_only_gap") or 0)

        raw_b_or_above_rate = (
            _safe_float(raw_b_or_above_rate_value)
            if raw_b_or_above_rate_value not in (None, "", [], {})
            else None
        )
        strict_ready_given_raw_b_rate = (
            _safe_float(strict_ready_given_raw_b_rate_value)
            if strict_ready_given_raw_b_rate_value not in (None, "", [], {})
            else None
        )
        live_ready_given_raw_b_rate = (
            _safe_float(live_ready_given_raw_b_rate_value)
            if live_ready_given_raw_b_rate_value not in (None, "", [], {})
            else None
        )
        strict_live_alignment_gap_rate = (
            _safe_float(gap_rate_value)
            if gap_rate_value not in (None, "", [], {})
            else None
        )
        strict_live_alignment_gap_count = (
            int(gap_count_value or 0)
            if gap_count_value not in (None, "", [], {})
            else None
        )
        if submit_stage_entered and raw_b_or_above_rate is not None:
            raw_b_or_above_rates.append(raw_b_or_above_rate)
        if submit_stage_entered and strict_ready_given_raw_b_rate is not None:
            strict_ready_given_raw_b_rates.append(strict_ready_given_raw_b_rate)
        if submit_stage_entered and live_ready_given_raw_b_rate is not None:
            live_ready_given_raw_b_rates.append(live_ready_given_raw_b_rate)
        if submit_stage_entered and strict_live_alignment_gap_rate is not None:
            strict_live_alignment_gap_rates.append(strict_live_alignment_gap_rate)
        if submit_stage_entered and strict_live_alignment_gap_count and strict_live_alignment_gap_count > 0:
            strict_live_gap_run_count += 1

        run_briefs.append(
            {
                "run_id": str(row.get("run_id") or "").strip() or None,
                "status": status_key,
                "started_at": row.get("started_at"),
                "completed_at": row.get("completed_at"),
                "readiness_decision": decision,
                "readiness_score": _safe_float(readiness.get("score"), 0.0)
                if readiness.get("score") is not None
                else None,
                "submit_stage_entered": submit_stage_entered,
                "submitted": submitted,
                "research_only_count": int(_summary_metric("research_only_count") or 0),
                "deferred_submission_count": int(_summary_metric("deferred_submission_count") or 0),
                "blocking_reason_codes": list(readiness.get("blocking_reason_codes") or []),
                "warning_reason_codes": list(readiness.get("warning_reason_codes") or []),
                "external_llm_provider_control_mode": external_llm_provider_control_mode or None,
                "external_llm_provider_control_reasons": external_llm_provider_control_reasons,
                "suppressed_generator_modes": suppressed_generator_modes,
                "external_llm_provider_suppressed": external_llm_provider_suppressed,
                "external_llm_provider_cooldown": external_llm_provider_cooldown,
                "governed_blocked_ratio": governed_blocked_ratio,
                "governed_candidate_pool_strict_shortfall_count": governed_strict_shortfall_count,
                "governed_blocked_candidate_count": governed_blocked_candidate_count,
                "governed_source_candidate_count": governed_source_candidate_count,
                "budget_feedback_evidence_debt_ratio": evidence_debt_ratio,
                "budget_feedback_zero_signal_ratio": zero_signal_ratio,
                "budget_feedback_forward_window_coverage_ratio": forward_window_coverage_ratio,
                "budget_feedback_promotion_ready_ratio": promotion_ready_ratio,
                "budget_feedback_promotion_review_coverage_ratio": promotion_review_coverage_ratio,
                "external_llm_stage_attempt_count": external_llm_stage_attempt_count,
                "external_llm_real_request_count": external_llm_real_request_count,
                "external_llm_compatibility_skip_ratio": external_llm_compatibility_skip_ratio,
                "external_llm_compatibility_failure_ratio": external_llm_compatibility_failure_ratio,
                "external_llm_effective_response_ratio": external_llm_effective_response_ratio,
                "external_llm_empty_200_response_ratio": external_llm_empty_200_response_ratio,
                "raw_b_or_above_rate": raw_b_or_above_rate,
                "strict_ready_given_raw_b_rate": strict_ready_given_raw_b_rate,
                "live_ready_given_raw_b_rate": live_ready_given_raw_b_rate,
                "strict_live_alignment_gap_count": strict_live_alignment_gap_count,
                "strict_live_alignment_gap_rate": strict_live_alignment_gap_rate,
            }
        )

    latest_run = run_briefs[0] if run_briefs else {}
    return {
        "contract_version": "strategy_factory.recent_run_diagnostics.v1",
        "window_size": requested_limit,
        "analyzed_run_count": len(run_briefs),
        "status_counts": status_counts,
        "readiness_decision_counts": readiness_decision_counts,
        "readiness_blocked_count": blocked_count,
        "readiness_blocked_rate": _rate(blocked_count, len(run_briefs)),
        "submit_stage_entered_count": submit_stage_entered_count,
        "submit_stage_entered_rate": _rate(submit_stage_entered_count, len(run_briefs)),
        "submitted_positive_count": submitted_positive_count,
        "submitted_positive_rate": _rate(submitted_positive_count, len(run_briefs)),
        "blocker_reason_topn": _top_reason_counts(blocker_reason_counts),
        "warning_reason_topn": _top_reason_counts(warning_reason_counts),
        "external_llm_provider_control_mode_counts": external_llm_provider_control_mode_counts,
        "external_llm_provider_suppressed_run_count": external_llm_provider_suppressed_run_count,
        "external_llm_provider_suppressed_run_rate": _rate(
            external_llm_provider_suppressed_run_count,
            len(run_briefs),
        ),
        "external_llm_provider_cooldown_run_count": external_llm_provider_cooldown_run_count,
        "external_llm_provider_cooldown_run_rate": _rate(
            external_llm_provider_cooldown_run_count,
            len(run_briefs),
        ),
        "external_llm_provider_control_reason_topn": _top_reason_counts(
            external_llm_provider_control_reason_counts,
        ),
        "suppressed_generator_mode_topn": _top_named_counts(
            suppressed_generator_mode_counts,
            name_key="mode",
        ),
        "governed_pool_diagnostics": {
            "measurement_run_count": len(governed_blocked_ratios),
            "latest_governed_blocked_ratio": latest_run.get("governed_blocked_ratio") or 0.0,
            "recent_governed_blocked_ratio_mean": _mean(governed_blocked_ratios),
            "latest_governed_candidate_pool_strict_shortfall_count": (
                latest_run.get("governed_candidate_pool_strict_shortfall_count") or 0
            ),
            "recent_governed_candidate_pool_strict_shortfall_mean": _mean(
                governed_strict_shortfall_counts
            ),
            "latest_governed_blocked_candidate_count": (
                latest_run.get("governed_blocked_candidate_count") or 0
            ),
            "recent_governed_blocked_candidate_count_mean": _mean(
                governed_blocked_candidate_counts
            ),
            "latest_governed_source_candidate_count": (
                latest_run.get("governed_source_candidate_count") or 0
            ),
            "recent_governed_source_candidate_count_mean": _mean(
                governed_source_candidate_counts
            ),
            "warning_reason_topn": _top_reason_counts(governed_warning_reason_counts),
            "blocking_reason_topn": _top_reason_counts(governed_blocking_reason_counts),
            "exclusion_reason_topn": _top_reason_counts(governed_exclusion_reason_counts),
            "pending_reason_topn": _top_reason_counts(governed_pending_reason_counts),
            "ineligible_reason_topn": _top_reason_counts(governed_ineligible_reason_counts),
        },
        "evidence_debt_diagnostics": {
            "measurement_run_count": len(evidence_debt_ratios),
            "latest_budget_feedback_evidence_debt_ratio": (
                latest_run.get("budget_feedback_evidence_debt_ratio") or 0.0
            ),
            "recent_budget_feedback_evidence_debt_ratio_mean": _mean(evidence_debt_ratios),
            "latest_budget_feedback_zero_signal_ratio": (
                latest_run.get("budget_feedback_zero_signal_ratio") or 0.0
            ),
            "recent_budget_feedback_zero_signal_ratio_mean": _mean(zero_signal_ratios),
            "latest_budget_feedback_forward_window_coverage_ratio": (
                latest_run.get("budget_feedback_forward_window_coverage_ratio") or 0.0
            ),
            "recent_budget_feedback_forward_window_coverage_ratio_mean": _mean(
                forward_window_coverage_ratios
            ),
            "latest_budget_feedback_promotion_ready_ratio": (
                latest_run.get("budget_feedback_promotion_ready_ratio") or 0.0
            ),
            "recent_budget_feedback_promotion_ready_ratio_mean": _mean(promotion_ready_ratios),
            "latest_budget_feedback_promotion_review_coverage_ratio": (
                latest_run.get("budget_feedback_promotion_review_coverage_ratio") or 0.0
            ),
            "recent_budget_feedback_promotion_review_coverage_ratio_mean": _mean(
                promotion_review_coverage_ratios
            ),
            "warning_reason_topn": _top_reason_counts(evidence_warning_reason_counts),
        },
        "provider_control_diagnostics": {
            "measurement_run_count": len(run_briefs),
            "active_attempt_run_count": provider_active_attempt_run_count,
            "zero_attempt_run_count": provider_zero_attempt_run_count,
            "latest_stage_attempt_count": latest_run.get("external_llm_stage_attempt_count") or 0,
            "recent_stage_attempt_count_mean": _mean(provider_stage_attempt_counts),
            "latest_real_request_count": latest_run.get("external_llm_real_request_count") or 0,
            "recent_real_request_count_mean": _mean(provider_real_request_counts),
            "latest_compatibility_skip_ratio": (
                latest_run.get("external_llm_compatibility_skip_ratio") or 0.0
            ),
            "recent_compatibility_skip_ratio_mean": _mean(provider_compatibility_skip_ratios),
            "latest_compatibility_failure_ratio": (
                latest_run.get("external_llm_compatibility_failure_ratio") or 0.0
            ),
            "recent_compatibility_failure_ratio_mean": _mean(
                provider_compatibility_failure_ratios
            ),
            "latest_effective_response_ratio": (
                latest_run.get("external_llm_effective_response_ratio") or 0.0
            ),
            "recent_effective_response_ratio_mean": _mean(provider_effective_response_ratios),
            "latest_empty_200_response_ratio": (
                latest_run.get("external_llm_empty_200_response_ratio") or 0.0
            ),
            "recent_empty_200_response_ratio_mean": _mean(
                provider_empty_200_response_ratios
            ),
        },
        "quality_progress": {
            "quality_measurement_run_count": len(raw_b_or_above_rates),
            "latest_raw_b_or_above_rate": latest_run.get("raw_b_or_above_rate") or 0.0,
            "recent_raw_b_or_above_rate_mean": _mean(raw_b_or_above_rates),
            "latest_strict_ready_given_raw_b_rate": latest_run.get("strict_ready_given_raw_b_rate") or 0.0,
            "recent_strict_ready_given_raw_b_rate_mean": _mean(strict_ready_given_raw_b_rates),
            "latest_live_ready_given_raw_b_rate": latest_run.get("live_ready_given_raw_b_rate") or 0.0,
            "recent_live_ready_given_raw_b_rate_mean": _mean(live_ready_given_raw_b_rates),
            "strict_live_gap_measurement_run_count": len(strict_live_alignment_gap_rates),
            "latest_strict_live_alignment_gap_rate": latest_run.get("strict_live_alignment_gap_rate") or 0.0,
            "recent_strict_live_alignment_gap_rate_mean": _mean(strict_live_alignment_gap_rates),
            "strict_live_gap_run_count": strict_live_gap_run_count,
            "strict_live_gap_run_rate": _rate(
                strict_live_gap_run_count,
                len(strict_live_alignment_gap_rates),
            ),
        },
        "recent_runs": run_briefs,
    }


_FORMAL_BLOCKER_LABELS = {
    "diagnostic_only_not_allowed_for_incubation": "Diagnostic-only runtime cannot enter formal incubation.",
    "default_profile_not_allowed_for_single_name_runtime": "Default runtime profile is not allowed for single-name formal runtime.",
    "execution_readiness_tier:missing_executable_contract": "Executable contract readiness is missing.",
    "execution_readiness_tier:observe_diagnostic_only": "Runtime is observe/diagnostic only, not formal-ready.",
    "missing_executable_contract": "Executable contract readiness is missing.",
    "proxy_runtime_not_allowed_for_formal_incubation": "Proxy runtime evidence cannot enter formal incubation.",
    "runtime_family_semantic_mismatch": "Runtime family semantics do not match the strategy contract.",
    "semantic_runtime_mismatch": "Runtime semantics do not match the strategy contract.",
    "strict_incubation_pass_required_for_formal_track": "Strict incubation pass is required for formal admission.",
    "trade_prediction_contract_not_ready": "Trade-prediction contract is not ready.",
}

_FORMAL_BLOCKER_ACTIONS = {
    "diagnostic_only_not_allowed_for_incubation": "Route only non-diagnostic runtime evidence to formal incubation; keep diagnostic samples in observe.",
    "default_profile_not_allowed_for_single_name_runtime": "Attach a single-name runtime profile before requesting formal admission.",
    "execution_readiness_tier:missing_executable_contract": "Persist the executable DSL/runtime contract and replay admission.",
    "execution_readiness_tier:observe_diagnostic_only": "Upgrade the runtime contract from observe-diagnostic to formal_runtime_ready.",
    "missing_executable_contract": "Persist the executable DSL/runtime contract and replay admission.",
    "proxy_runtime_not_allowed_for_formal_incubation": "Replace proxy runtime evidence with strategy-family matching runtime evidence.",
    "runtime_family_semantic_mismatch": "Repair the semantic runtime family before replaying formal admission.",
    "semantic_runtime_mismatch": "Repair the semantic runtime contract before replaying formal admission.",
    "strict_incubation_pass_required_for_formal_track": "Improve strict gate evidence before consuming formal incubation slots.",
    "trade_prediction_contract_not_ready": "Complete trade-prediction contract readiness before formal admission.",
}

_FORMAL_BLOCKER_CANONICAL = {
    "diagnostic_only_runtime": "diagnostic_only_not_allowed_for_incubation",
    "execution_readiness_tier:missing": "execution_readiness_tier:missing_executable_contract",
    "missing_executable_contract": "execution_readiness_tier:missing_executable_contract",
    "semantic_runtime_mismatch": "runtime_family_semantic_mismatch",
}


def _canonical_formal_blocker(value: Any) -> str:
    code = str(value or "").strip()
    if not code:
        return ""
    normalized = code.lower().replace(" ", "_")
    if normalized.startswith("execution_readiness_tier:"):
        tier = normalized.split(":", 1)[1].strip() or "unknown"
        if tier == "missing":
            tier = "missing_executable_contract"
        return f"execution_readiness_tier:{tier}"
    return _FORMAL_BLOCKER_CANONICAL.get(normalized, normalized)


def _formal_blocker_values_from(value: Any) -> list[str]:
    if value in (None, "", [], {}):
        return []
    if isinstance(value, dict):
        raw_items: list[Any] = []
        for key in ("reason_code", "reason", "code", "name"):
            if value.get(key) not in (None, "", [], {}):
                raw_items.append(value.get(key))
        return [_canonical_formal_blocker(item) for item in raw_items if _canonical_formal_blocker(item)]
    raw_values = value if isinstance(value, (list, tuple, set)) else [value]
    result: list[str] = []
    for raw in raw_values:
        if isinstance(raw, dict):
            result.extend(_formal_blocker_values_from(raw))
            continue
        code = _canonical_formal_blocker(raw)
        if code:
            result.append(code)
    return result


def _top_formal_blockers(counts: dict[str, int], *, limit: int = 5) -> list[dict[str, Any]]:
    items = []
    for reason_code, count in dict(counts or {}).items():
        code = _canonical_formal_blocker(reason_code)
        if not code or int(count or 0) <= 0:
            continue
        items.append(
            {
                "reason_code": code,
                "count": int(count or 0),
                "label": _FORMAL_BLOCKER_LABELS.get(code, code.replace("_", " ")),
                "next_action": _FORMAL_BLOCKER_ACTIONS.get(
                    code,
                    "Inspect the latest strategy quality report and replay admission after the contract gap is repaired.",
                ),
            }
        )
    items.sort(key=lambda item: (-int(item.get("count") or 0), str(item.get("reason_code") or "")))
    return items[: max(1, int(limit or 5))]


def build_strict_incubation_blocker_summary(
    run_rows: list[dict[str, Any]] | None,
    recent_run_diagnostics: dict[str, Any] | None = None,
    *,
    limit: int = 5,
) -> dict[str, Any]:
    rows = [dict(item or {}) for item in list(run_rows or []) if isinstance(item, dict)]
    requested_limit = max(1, int(limit or 5))
    items = rows[:requested_limit]
    diagnostics = dict(recent_run_diagnostics or {})
    blocker_counts: dict[str, int] = {}
    sample_blocked: list[dict[str, Any]] = []
    analyzed_strategy_count = 0
    strict_not_ready_count = 0
    raw_b_or_above_count = 0
    strict_ready_given_raw_b_count = 0
    submitted_count = 0
    formal_requested_count = 0
    observe_lane_count = 0
    diagnostic_lane_count = 0

    def add_blocker(code: Any, *, count: int = 1) -> None:
        normalized = _canonical_formal_blocker(code)
        if not normalized:
            return
        blocker_counts[normalized] = blocker_counts.get(normalized, 0) + max(1, int(count or 1))

    for row in items:
        summary = dict(row.get("summary") or {})
        submission_artifact = dict(row.get("submission_artifact") or {})
        strategy_briefs = [
            dict(item or {})
            for item in list(submission_artifact.get("strategy_briefs") or [])
            if isinstance(item, dict)
        ]
        analyzed_strategy_count += len(strategy_briefs)
        submitted_count += int(summary.get("submitted") or row.get("submitted") or submission_artifact.get("submitted_count") or 0)
        for reason in _formal_blocker_values_from(summary.get("formal_track_blockers")):
            add_blocker(reason)
        for reason in _formal_blocker_values_from(summary.get("admission_block_reasons")):
            add_blocker(reason)
        for item in _formal_blocker_values_from(submission_artifact.get("gate_3_failure_reason_topn")):
            add_blocker(item)
        for brief in strategy_briefs:
            strict_ready = brief.get("strict_incubation_ready") is True
            raw_b_or_above = bool(brief.get("raw_b_or_above")) or str(
                brief.get("raw_validation_grade") or brief.get("validation_grade") or ""
            ).strip().upper() in {"A", "B"}
            if raw_b_or_above:
                raw_b_or_above_count += 1
                if strict_ready:
                    strict_ready_given_raw_b_count += 1
            if not strict_ready:
                strict_not_ready_count += 1
            lane = str(brief.get("submission_lane") or "").strip().lower()
            if lane == "observe_incubation":
                observe_lane_count += 1
            if lane == "diagnostic_observation":
                diagnostic_lane_count += 1
            if brief.get("formal_track_requested") is True:
                formal_requested_count += 1
            reasons = []
            for key in (
                "formal_track_blockers",
                "admission_block_reasons",
                "hard_fail_reasons",
                "submission_action_gaps",
                "trade_prediction_contract_reject_reasons",
            ):
                reasons.extend(_formal_blocker_values_from(brief.get(key)))
            tier = str(brief.get("execution_readiness_tier") or "").strip()
            if tier and tier != "formal_runtime_ready":
                reasons.append(_canonical_formal_blocker(f"execution_readiness_tier:{tier}"))
            if brief.get("diagnostic_only") is True:
                reasons.append("diagnostic_only_not_allowed_for_incubation")
            if str(brief.get("runtime_bootstrap_reason") or "").strip() == "default_profile":
                reasons.append("default_profile_not_allowed_for_single_name_runtime")
            unique_reasons = list(dict.fromkeys([reason for reason in reasons if reason]))
            for reason in unique_reasons:
                add_blocker(reason)
            if unique_reasons and len(sample_blocked) < 5:
                sample_blocked.append(
                    {
                        "strategy_id": brief.get("strategy_id"),
                        "family": brief.get("candidate_family") or brief.get("strategy_type"),
                        "grade": brief.get("raw_validation_grade") or brief.get("validation_grade"),
                        "submission_lane": brief.get("submission_lane"),
                        "strict_incubation_ready": strict_ready,
                        "blockers": unique_reasons[:6],
                    }
                )

    quality_progress = dict(diagnostics.get("quality_progress") or {})
    recent_runs = [dict(item or {}) for item in list(diagnostics.get("recent_runs") or []) if isinstance(item, dict)]
    if not raw_b_or_above_count:
        raw_b_or_above_rate = float(quality_progress.get("recent_raw_b_or_above_rate_mean") or 0.0)
    else:
        raw_b_or_above_rate = _rate(raw_b_or_above_count, analyzed_strategy_count)
    strict_ready_given_raw_b_rate = (
        _rate(strict_ready_given_raw_b_count, raw_b_or_above_count)
        if raw_b_or_above_count
        else float(quality_progress.get("recent_strict_ready_given_raw_b_rate_mean") or 0.0)
    )
    if not submitted_count:
        submitted_count = sum(int(item.get("submitted") or 0) for item in recent_runs)
    for item in list(diagnostics.get("blocker_reason_topn") or []):
        payload = dict(item or {})
        add_blocker(payload.get("reason_code"), count=int(payload.get("count") or 1))

    top_blockers = _top_formal_blockers(blocker_counts)
    status = "ready"
    if not items and not recent_runs:
        status = "no_recent_runs"
    elif top_blockers or strict_ready_given_raw_b_rate == 0.0:
        status = "blocked"
    headline = (
        "No recent Strategy Factory runs were available for formal-admission analysis."
        if status == "no_recent_runs"
        else "Recent runs still fail formal admission because strict incubation readiness is zero."
        if strict_ready_given_raw_b_rate == 0.0 and (raw_b_or_above_count or submitted_count)
        else "Recent runs expose recurring formal-admission blockers."
        if top_blockers
        else "No recurring strict-incubation blocker was detected in recent runs."
    )
    next_action = (
        top_blockers[0]["next_action"]
        if top_blockers
        else "Run another factory cycle with full contract persistence enabled and inspect the latest quality report."
    )
    return {
        "contract_version": "strategy_factory.strict_incubation_blockers.v1",
        "status": status,
        "headline": headline,
        "window_size": requested_limit,
        "analyzed_run_count": len(items) or int(diagnostics.get("analyzed_run_count") or 0),
        "analyzed_strategy_count": analyzed_strategy_count,
        "submitted_count": submitted_count,
        "strict_not_ready_count": strict_not_ready_count,
        "raw_b_or_above_count": raw_b_or_above_count,
        "raw_b_or_above_rate": raw_b_or_above_rate,
        "strict_ready_given_raw_b_count": strict_ready_given_raw_b_count,
        "strict_ready_given_raw_b_rate": strict_ready_given_raw_b_rate,
        "formal_requested_count": formal_requested_count,
        "observe_lane_count": observe_lane_count,
        "diagnostic_lane_count": diagnostic_lane_count,
        "top_blockers": top_blockers,
        "sample_blocked_strategies": sample_blocked,
        "next_action": next_action,
    }


async def refresh_factory_run_detail_quality_contract(db, row: Optional[dict]) -> dict:
    detail = normalize_factory_run_detail_contract(row)
    if not detail or not db:
        return detail
    submission_artifact = dict(detail.get("submission_artifact") or {})
    strategy_briefs = [
        dict(item or {})
        for item in list(submission_artifact.get("strategy_briefs") or [])
        if isinstance(item, dict)
    ]
    if not strategy_briefs:
        return detail

    refreshed_briefs: list[dict[str, Any]] = []
    refreshed = False
    for brief in strategy_briefs:
        strategy_id = str(brief.get("strategy_id") or "").strip()
        if not strategy_id:
            refreshed_briefs.append(brief)
            continue
        latest_report = await get_latest_quality_report(db, strategy_id)
        if not latest_report:
            refreshed_briefs.append(brief)
            continue
        normalized_report = normalize_quality_report_contract(
            latest_report,
            strategy_id=strategy_id,
            strategy_type=brief.get("candidate_family") or brief.get("strategy_type"),
        )
        latest_summary = dict(normalized_report.get("summary") or {})
        raw_latest_summary = dict(latest_report.get("summary") or {})
        latest_quality_gate = dict(latest_report.get("quality_gate") or {})
        latest_validation_profile = dict(latest_report.get("validation_profile") or {})
        merged_brief = dict(brief)
        for key, value in raw_latest_summary.items():
            if value in (None, "", [], {}):
                continue
            latest_summary[key] = deepcopy(value)
        if latest_validation_profile.get("validation_focus") not in (None, "", [], {}):
            latest_summary["validation_focus"] = deepcopy(latest_validation_profile.get("validation_focus"))
        for metric_key in (
            "trade_density",
            "post_cost_sharpe",
            "deflated_sharpe_ratio",
            "pbo",
            "strict_incubation_ready",
            "live_candidate_ready",
        ):
            if latest_quality_gate.get(metric_key) not in (None, "", [], {}):
                latest_summary[metric_key] = deepcopy(latest_quality_gate.get(metric_key))
        for key, value in latest_summary.items():
            if value in (None, "", [], {}):
                continue
            merged_brief[key] = deepcopy(value)
        refreshed_briefs.append(merged_brief)
        refreshed = True

    if not refreshed:
        return detail

    refreshed_summary = _summarize_factory_submission_briefs(refreshed_briefs)
    submission_artifact["strategy_briefs"] = refreshed_briefs
    for key, value in refreshed_summary.items():
        submission_artifact[key] = deepcopy(value)

    detail["submission_artifact"] = submission_artifact
    for key, value in refreshed_summary.items():
        detail[key] = deepcopy(value)

    summary = dict(detail.get("summary") or {})
    for key, value in refreshed_summary.items():
        summary[key] = deepcopy(value)
    detail["summary"] = summary
    return detail

# --- strategy_mgr_helpers_parts/part_7.py ---
async def refresh_factory_run_summary_quality_contract(db, row: Optional[dict]) -> dict:
    detail = await refresh_factory_run_detail_quality_contract(db, row)
    if not detail:
        return {}
    summary = normalize_factory_run_summary_contract(detail)
    submission_artifact = dict(detail.get("submission_artifact") or summary.get("submission_artifact") or {})
    summary["submission_artifact"] = submission_artifact
    for field in _FACTORY_SUMMARY_OBSERVABILITY_FIELDS:
        value = detail.get(field)
        if value in (None, "", [], {}):
            continue
        summary[field] = deepcopy(value)
    summary["summary"] = merge_factory_run_summary_observability(
        summary.get("summary") or {},
        detail,
    )
    return summary


# list_quality_reports, get_latest_quality_report imported from strategy_lifecycle_shared


# ── Incubation overview builder (imported from strategy_lifecycle_shared) ────


# ── Backward-compatible aliases (underscore-prefixed names) ──────────────────
# External services import these via ``from ..tools.managers.strategy_manager import _xxx``.
# The main strategy_manager.py re-exports them, but we also define them here so that
# the helpers module itself is self-contained for direct imports.

_compute_nav_series = compute_nav_series
_normalize_status_alias = normalize_status_alias
_validate_transition = validate_transition
_update_status = update_status
_save_quality_report = save_quality_report
_metric_bucket_value = metric_bucket_value
_normalize_time_filter = normalize_time_filter
_parse_bool = parse_bool
_quality_gate_reason_code = quality_gate_reason_code
_normalize_quality_gate_result = normalize_quality_gate_result
_is_factory_ai_prototype_strategy = is_factory_ai_prototype_strategy
_has_only_statistical_gate_failures = has_only_statistical_gate_failures
_safe_metric_value = safe_metric_value
_maybe_grant_provisional_incubation = maybe_grant_provisional_incubation
_build_quality_report = build_quality_report
_list_quality_reports = list_quality_reports
_get_latest_quality_report = get_latest_quality_report
_build_incubation_overview = build_incubation_overview

