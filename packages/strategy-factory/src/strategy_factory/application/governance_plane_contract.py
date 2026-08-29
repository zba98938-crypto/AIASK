"""Governance plane artifact contracts for strategy factory runs.

P2 goal: make gates, dedup, submission, and governance evidence observable as
an explicit governance plane instead of remaining an implicit by-product of the
submission pipeline.
"""
from __future__ import annotations

# --- governance_plane_contract_parts/normalizers.py ---
from typing import Any

GOVERNANCE_PLANE_CONTRACT_VERSION = "strategy_factory.governance_plane.v1"
GATE_ARTIFACT_CONTRACT_VERSION = "strategy_factory.gate_artifact.v1"
GOVERNANCE_PLANE_V2_CONTRACT_VERSION = "strategy_factory.governance_plane.v2"
GATE_ARTIFACT_V2_CONTRACT_VERSION = "strategy_factory.gate_artifact.v2"
PREDICTION_TRACE_CONTRACT_VERSION = "strategy_factory.prediction_trace.v2"
PREDICTION_TRACE_LEDGER_CONTRACT_VERSION = "strategy_factory.prediction_trace_ledger.v2"
DEDUP_ARTIFACT_CONTRACT_VERSION = "strategy_factory.dedup_artifact.v1"
SUBMISSION_ARTIFACT_CONTRACT_VERSION = "strategy_factory.submission_artifact.v1"
GOVERNANCE_EVIDENCE_ARTIFACT_CONTRACT_VERSION = "strategy_factory.governance_evidence_artifact.v1"


def _string(value: Any) -> str:
    return str(value or "").strip()


def _normalized_text(value: Any) -> str:
    return _string(value).lower()


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value or 0)
    except Exception:
        return int(default)


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return float(default)


def _confidence_contract_status(confidence_contract: dict[str, Any]) -> str | None:
    contract = dict(confidence_contract or {})
    if not contract:
        return None
    prediction_quality = dict(
        contract.get("prediction_quality")
        or contract.get("probability_quality")
        or {}
    )
    support_samples = _safe_int(
        prediction_quality.get("support_samples")
        or prediction_quality.get("sample_size")
        or contract.get("support_samples")
        or contract.get("sample_size"),
        0,
    )
    contract_version = _normalized_text(
        prediction_quality.get("contract_version")
        or contract.get("contract_version")
        or prediction_quality.get("version")
        or contract.get("version")
    )
    explicit_stable = prediction_quality.get("contract_version_stable")
    if explicit_stable is None:
        explicit_stable = contract.get("contract_version_stable")
    version_stable = (
        bool(explicit_stable)
        if explicit_stable is not None
        else bool(contract_version)
        and not any(token in contract_version for token in ("draft", "unstable", "experimental", "preview", "beta", "alpha"))
    )
    if support_samples < 50:
        return "insufficient"
    if support_samples < 100:
        return "diagnostic_ready"
    return "comparable_ready" if version_stable else "diagnostic_ready"


def _compact_list(values: Any, *, limit: int = 8) -> list[str]:
    items: list[str] = []
    for value in list(values or []):
        token = _string(value)
        if token and token not in items:
            items.append(token)
        if len(items) >= limit:
            break
    return items


def _compact_mapping(
    value: Any,
    *,
    allowed_keys: tuple[str, ...] | None = None,
    limit: int | None = None,
) -> dict[str, Any]:
    payload = dict(value or {})
    if not payload:
        return {}
    items = (
        [(key, payload.get(key)) for key in allowed_keys]
        if allowed_keys is not None
        else list(payload.items())
    )
    result: dict[str, Any] = {}
    for key, raw in items:
        if raw in (None, "", [], {}):
            continue
        result[key] = raw
        if limit is not None and len(result) >= limit:
            break
    return result


def _candidate_payload_value(candidate: dict[str, Any], key: str, default: Any = None) -> Any:
    payload = dict(candidate or {})
    if key in payload and payload.get(key) is not None:
        return payload.get(key)
    params = dict(payload.get("params") or {})
    if key in params and params.get(key) is not None:
        return params.get(key)
    return default


def _candidate_prediction_trace_id(candidate: dict[str, Any]) -> str:
    payload = dict(candidate or {})
    return _string(
        payload.get("prediction_trace_id")
        or payload.get("trace_id")
        or _candidate_payload_value(payload, "prediction_trace_id")
        or _candidate_payload_value(payload, "trace_id")
    )


def _candidate_trace_ids(candidate: dict[str, Any]) -> list[str]:
    trace_id = _candidate_prediction_trace_id(candidate)
    return [trace_id] if trace_id else []


def _candidate_research_protocol_version(candidate: dict[str, Any]) -> str:
    payload = dict(candidate or {})
    contract = dict(
        payload.get("research_validation_contract")
        or _candidate_payload_value(payload, "research_validation_contract")
        or {}
    )
    return _string(
        payload.get("research_protocol_version")
        or _candidate_payload_value(payload, "research_protocol_version")
        or contract.get("contract_version")
    )


def _candidate_contract_version(candidate: dict[str, Any]) -> str:
    payload = dict(candidate or {})
    return _string(
        payload.get("candidate_contract_version")
        or _candidate_payload_value(payload, "candidate_contract_version")
    )


def _candidate_spec_completeness(candidate: dict[str, Any]) -> str:
    payload = dict(candidate or {})
    contract = dict(
        payload.get("research_validation_contract")
        or _candidate_payload_value(payload, "research_validation_contract")
        or {}
    )
    return _string(
        payload.get("spec_completeness")
        or _candidate_payload_value(payload, "spec_completeness")
        or contract.get("spec_completeness")
    )


def _candidate_field_provenance_summary(candidate: dict[str, Any]) -> dict[str, Any]:
    payload = dict(candidate or {})
    contract = dict(
        payload.get("research_validation_contract")
        or _candidate_payload_value(payload, "research_validation_contract")
        or {}
    )
    return _compact_mapping(
        payload.get("field_provenance_summary")
        or _candidate_payload_value(payload, "field_provenance_summary")
        or contract.get("field_provenance_summary")
        or {},
    )


def _candidate_family(candidate: dict[str, Any]) -> str:
    payload = dict(candidate or {})
    params = dict(payload.get("params") or {})
    candidate_provenance = dict(payload.get("candidate_provenance") or params.get("candidate_provenance") or {})
    return _normalized_text(
        candidate_provenance.get("candidate_family")
        or payload.get("candidate_family")
        or params.get("candidate_family")
        or payload.get("strategy_type")
    )


def _candidate_artifact_ids(candidate: dict[str, Any]) -> list[str]:
    payload = dict(candidate or {})
    params = dict(payload.get("params") or {})
    candidate_provenance = dict(payload.get("candidate_provenance") or params.get("candidate_provenance") or {})
    return _compact_list(
        [
            payload.get("strategy_id"),
            payload.get("id"),
            payload.get("source_candidate_artifact_id"),
            payload.get("source_generation_artifact_id"),
            payload.get("source_validation_artifact_id"),
            payload.get("candidate_memory_record_id"),
            payload.get("hypothesis_artifact_id"),
            payload.get("multiple_testing_registry_record_id"),
            payload.get("vector_profile_id"),
            payload.get("paper_account_id"),
            payload.get("incubation_account_id"),
            payload.get("promotion_review_id"),
            candidate_provenance.get("source_candidate_artifact_id"),
            candidate_provenance.get("source_generation_artifact_id"),
            candidate_provenance.get("source_validation_artifact_id"),
            candidate_provenance.get("memory_record_id"),
            params.get("source_candidate_artifact_id"),
            params.get("source_generation_artifact_id"),
            params.get("source_validation_artifact_id"),
            params.get("candidate_memory_record_id"),
            params.get("multiple_testing_registry_record_id"),
            params.get("vector_profile_id"),
        ],
        limit=16,
    )


def _candidate_retrieval_context_ids(candidate: dict[str, Any]) -> list[str]:
    payload = dict(candidate or {})
    params = dict(payload.get("params") or {})
    candidate_provenance = dict(payload.get("candidate_provenance") or params.get("candidate_provenance") or {})
    research_task = dict(payload.get("research_task") or params.get("research_task") or {})
    return _compact_list(
        [
            research_task.get("task_id"),
            research_task.get("event_id"),
            payload.get("task_run_id"),
            payload.get("multiple_testing_registry_record_id"),
            payload.get("candidate_memory_record_id"),
            payload.get("vector_profile_id"),
            candidate_provenance.get("memory_record_id"),
            params.get("task_run_id"),
            params.get("multiple_testing_registry_record_id"),
            params.get("candidate_memory_record_id"),
            params.get("vector_profile_id"),
        ],
        limit=16,
    )


def _normalize_hard_failure_entry(
    reason_code: Any,
    *,
    issue: str,
    field: Any = None,
    detail: Any = None,
) -> dict[str, Any] | None:
    code = _string(reason_code)
    field_name = _string(field)
    if not code:
        return None
    payload = {
        "reason_code": code,
        "issue": issue,
        "severity": "reject",
        "decision": "reject",
    }
    if field_name:
        payload["field"] = field_name
    if _string(detail):
        payload["detail"] = _string(detail)
    return payload


def _unique_hard_failures(entries: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    unique: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for item in list(entries or []):
        payload = dict(item or {})
        reason_code = _string(payload.get("reason_code"))
        field_name = _string(payload.get("field"))
        if not reason_code:
            continue
        dedupe_key = (field_name, reason_code)
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        unique.append(payload)
    return unique


def _family_outcome_summary(items: list[dict[str, Any]]) -> dict[str, Any]:
    family_counts: dict[str, int] = {}
    status_counts: dict[str, int] = {}
    lane_counts: dict[str, int] = {}
    for item in list(items or []):
        family = _candidate_family(item)
        if family:
            family_counts[family] = family_counts.get(family, 0) + 1
        status = _normalized_text(item.get("status") or item.get("final_status"))
        if status:
            status_counts[status] = status_counts.get(status, 0) + 1
        lane = _normalized_text(item.get("submission_lane"))
        if lane:
            lane_counts[lane] = lane_counts.get(lane, 0) + 1
    return {
        "family_counts": family_counts,
        "status_counts": status_counts,
        "submission_lane_counts": lane_counts,
    }


def _top_counts(counts: dict[str, int], *, label_key: str = "name", limit: int = 5) -> list[dict[str, Any]]:
    items = [
        {label_key: _string(name), "count": _safe_int(count)}
        for name, count in dict(counts or {}).items()
        if _string(name) and _safe_int(count) > 0
    ]
    items.sort(key=lambda item: (-_safe_int(item.get("count")), _string(item.get(label_key))))
    return items[: max(1, int(limit or 5))]


def _count_by(items: list[dict[str, Any]], resolver) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in list(items or []):
        key = _normalized_text(resolver(dict(item or {})))
        if not key:
            continue
        counts[key] = counts.get(key, 0) + 1
    return counts


def _compact_reason_topn(items: Any, *, limit: int = 5) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for item in list(items or [])[:limit]:
        payload = dict(item or {})
        reason = _string(payload.get("reason_code") or payload.get("reason"))
        if not reason:
            continue
        result.append(
            {
                "reason": reason,
                "count": _safe_int(payload.get("count"), 0),
            }
        )
    return result


def _has_mapping(item: dict[str, Any], key: str) -> bool:
    return bool(_compact_mapping(dict(item or {}).get(key)))


def _has_text(item: dict[str, Any], key: str) -> bool:
    return bool(_string(dict(item or {}).get(key)))


def _primary_validation_layer(item: dict[str, Any]) -> str:
    payload = dict(item or {})
    validation_profile = dict(payload.get("validation_profile") or {})
    return _string(payload.get("primary_validation_layer") or validation_profile.get("primary_validation_layer"))


def _validation_profile_name(item: dict[str, Any]) -> str:
    payload = dict(item or {})
    validation_profile = dict(payload.get("validation_profile") or {})
    return _string(validation_profile.get("profile"))


def _constraint_violation(item: dict[str, Any]) -> str:
    payload = dict(item or {})
    constraint_check = dict(payload.get("constraint_check") or {})
    return _string(constraint_check.get("constraint_violation"))


def _committee_decision(item: dict[str, Any]) -> str:
    payload = dict(item or {})
    committee_review = dict(payload.get("committee_review") or {})
    return _string(committee_review.get("decision"))


def _candidate_family(item: dict[str, Any]) -> str:
    payload = dict(item or {})
    candidate_provenance = dict(payload.get("candidate_provenance") or {})
    quality_summary = dict(payload.get("quality_summary") or {})
    return _string(
        payload.get("candidate_family")
        or candidate_provenance.get("candidate_family")
        or quality_summary.get("candidate_family")
        or payload.get("strategy_type")
    )


def _holding_bucket(item: dict[str, Any]) -> str:
    payload = dict(item or {})
    candidate_provenance = dict(payload.get("candidate_provenance") or {})
    quality_summary = dict(payload.get("quality_summary") or {})
    return _string(
        payload.get("holding_period_bucket")
        or candidate_provenance.get("holding_period_bucket")
        or quality_summary.get("holding_period_bucket")
    )


def _target_universe_key(item: dict[str, Any]) -> str:
    payload = dict(item or {})
    contract_snapshot = dict(payload.get("candidate_contract_snapshot") or {})
    targeting = dict(contract_snapshot.get("targeting") or {})
    target_pool_id = _string(payload.get("target_pool_id") or targeting.get("target_pool_id"))
    if target_pool_id:
        return target_pool_id
    target_symbols = _compact_list(
        payload.get("target_symbols") or targeting.get("target_symbols"),
        limit=12,
    )
    return ",".join(target_symbols)


def _validation_grade(item: dict[str, Any]) -> str:
    payload = dict(item or {})
    quality_summary = dict(payload.get("quality_summary") or {})
    gate = dict(payload.get("gate_3") or {})
    admission_review_context = dict(gate.get("admission_review_context") or {})
    incubation_review_context = dict(
        dict((gate.get("admission_evaluations") or {}).get("incubation") or {}).get("review_context") or {}
    )
    return (
        _string(
            payload.get("validation_grade")
            or quality_summary.get("validation_grade")
            or gate.get("validation_grade")
            or admission_review_context.get("validation_grade")
            or incubation_review_context.get("validation_grade")
        ).upper()
    )


def _raw_validation_grade(item: dict[str, Any]) -> str:
    payload = dict(item or {})
    quality_summary = dict(payload.get("quality_summary") or {})
    gate = dict(payload.get("gate_3") or {})
    admission_review_context = dict(gate.get("admission_review_context") or {})
    incubation_review_context = dict(
        dict((gate.get("admission_evaluations") or {}).get("incubation") or {}).get("review_context") or {}
    )
    return (
        _string(
            payload.get("raw_validation_grade")
            or quality_summary.get("raw_validation_grade")
            or gate.get("raw_validation_grade")
            or admission_review_context.get("validation_grade")
            or incubation_review_context.get("validation_grade")
            or payload.get("validation_grade")
            or quality_summary.get("validation_grade")
            or gate.get("validation_grade")
        ).upper()
    )


def _effective_validation_grade(item: dict[str, Any]) -> str:
    payload = dict(item or {})
    quality_summary = dict(payload.get("quality_summary") or {})
    gate = dict(payload.get("gate_3") or {})
    admission_review_context = dict(gate.get("admission_review_context") or {})
    incubation_review_context = dict(
        dict((gate.get("admission_evaluations") or {}).get("incubation") or {}).get("review_context") or {}
    )
    return (
        _string(
            payload.get("effective_validation_grade")
            or quality_summary.get("effective_validation_grade")
            or gate.get("effective_validation_grade")
            or payload.get("validation_grade")
            or quality_summary.get("validation_grade")
            or gate.get("validation_grade")
            or admission_review_context.get("validation_grade")
            or incubation_review_context.get("validation_grade")
        ).upper()
    )


def _validation_grade_adjustment_reason(item: dict[str, Any]) -> str:
    payload = dict(item or {})
    quality_summary = dict(payload.get("quality_summary") or {})
    gate = dict(payload.get("gate_3") or {})
    trade_quality_adjustment = dict(payload.get("trade_quality_adjustment") or {})
    admission_review_context = dict(gate.get("admission_review_context") or {})
    incubation_review_context = dict(
        dict((gate.get("admission_evaluations") or {}).get("incubation") or {}).get("review_context")
        or {}
    )
    return _string(
        payload.get("validation_grade_adjustment_reason")
        or quality_summary.get("validation_grade_adjustment_reason")
        or gate.get("validation_grade_adjustment_reason")
        or admission_review_context.get("validation_grade_adjustment_reason")
        or incubation_review_context.get("validation_grade_adjustment_reason")
        or trade_quality_adjustment.get("adjustment_reason")
    )


def _validation_focus(item: dict[str, Any]) -> str:
    payload = dict(item or {})
    validation_profile = dict(payload.get("validation_profile") or {})
    quality_summary = dict(payload.get("quality_summary") or {})
    gate = dict(payload.get("gate_3") or {})
    return _string(
        payload.get("validation_focus")
        or validation_profile.get("validation_focus")
        or quality_summary.get("validation_focus")
        or gate.get("validation_focus")
    )


def _validation_total_score(item: dict[str, Any], *, raw: bool) -> float | None:
    payload = dict(item or {})
    quality_summary = dict(payload.get("quality_summary") or {})
    gate = dict(payload.get("gate_3") or {})
    keys = (
        (
            "raw_validation_total_score",
            "validation_total_score",
            "raw_total_score",
            "total_score",
            "candidate_validation_score",
        )
        if raw
        else ("validation_total_score", "effective_validation_total_score", "total_score")
    )
    for source in (payload, quality_summary, gate):
        for key in keys:
            value = source.get(key)
            if value is None:
                continue
            try:
                return float(value)
            except Exception:
                continue
    return None


def _bool_payload(item: dict[str, Any], *keys: str) -> bool:
    payload = dict(item or {})
    quality_summary = dict(payload.get("quality_summary") or {})
    gate = dict(payload.get("gate_3") or {})
    for source in (payload, quality_summary, gate):
        for key in keys:
            if key in source and source.get(key) is not None:
                return bool(source.get(key))
    return False

# --- governance_plane_contract_parts/policy.py ---
def _metric_payload(item: dict[str, Any], *keys: str) -> float | None:
    payload = dict(item or {})
    quality_summary = dict(payload.get("quality_summary") or {})
    run_correction = dict(payload.get("run_correction") or {})
    gate = dict(payload.get("gate_3") or {})
    for source in (payload, quality_summary, run_correction, gate):
        for key in keys:
            value = source.get(key)
            if value is None:
                continue
            try:
                return float(value)
            except Exception:
                continue
    return None


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


def _is_raw_b_or_above(grade: str) -> bool:
    return str(grade or "").strip().upper() in {"A", "B"}


def _aggregate_family_quality_panel(strategies: list[dict[str, Any]]) -> list[dict[str, Any]]:
    buckets: dict[tuple[str, str, str], dict[str, Any]] = {}
    for item in list(strategies or []):
        family = _candidate_family(item) or "unknown"
        holding_bucket = _holding_bucket(item) or "unknown"
        validation_focus = _validation_focus(item) or "unknown"
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
        raw_grade = _raw_validation_grade(item)
        effective_grade = _effective_validation_grade(item)
        if raw_grade:
            bucket["raw_validation_grade_distribution"][raw_grade] = (
                bucket["raw_validation_grade_distribution"].get(raw_grade, 0) + 1
            )
        if effective_grade:
            bucket["effective_validation_grade_distribution"][effective_grade] = (
                bucket["effective_validation_grade_distribution"].get(effective_grade, 0) + 1
            )
        raw_score = _validation_total_score(item, raw=True)
        if raw_score is not None:
            bucket["raw_validation_total_scores"].append(float(raw_score))
        strict_ready = _bool_payload(item, "strict_incubation_ready")
        live_ready = _bool_payload(item, "live_candidate_ready")
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
        trade_density = _metric_payload(item, "trade_density")
        if trade_density is not None:
            bucket["trade_density_values"].append(float(trade_density))
        post_cost_sharpe = _metric_payload(item, "post_cost_sharpe")
        if post_cost_sharpe is not None:
            bucket["post_cost_sharpe_values"].append(float(post_cost_sharpe))
        dsr = _metric_payload(item, "deflated_sharpe_ratio")
        if dsr is not None:
            bucket["deflated_sharpe_ratio_values"].append(float(dsr))
        pbo = _metric_payload(item, "pbo")
        if pbo is not None:
            bucket["pbo_values"].append(float(pbo))

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


def _attempt_adjustment_payload(item: dict[str, Any]) -> dict[str, Any]:
    return dict(dict(item or {}).get("attempt_adjustment") or {})


def _multiple_testing_registry_payload(item: dict[str, Any]) -> dict[str, Any]:
    return dict(dict(item or {}).get("multiple_testing_registry") or {})


def _run_correction_payload(item: dict[str, Any]) -> dict[str, Any]:
    return dict(dict(item or {}).get("run_correction") or {})


def _candidate_local_attempt_count(item: dict[str, Any]) -> int:
    payload = dict(item or {})
    attempt_adjustment = _attempt_adjustment_payload(payload)
    registry = _multiple_testing_registry_payload(payload)
    return _safe_int(
        payload.get("candidate_local_attempt_count")
        or attempt_adjustment.get("attempt_count")
        or registry.get("attempt_count")
    )


def _task_local_attempt_count(item: dict[str, Any]) -> int:
    payload = dict(item or {})
    registry = _multiple_testing_registry_payload(payload)
    return _safe_int(
        payload.get("task_local_attempt_count")
        or registry.get("task_attempt_count")
        or payload.get("task_attempt_count")
    )


def _cohort_effective_trials(item: dict[str, Any]) -> float:
    payload = dict(item or {})
    run_correction = _run_correction_payload(payload)
    registry = _multiple_testing_registry_payload(payload)
    multiple_testing = dict(registry.get("multiple_testing") or {})
    gate = dict(payload.get("gate_3") or {})
    return _safe_float(
        payload.get("cohort_effective_trials")
        or gate.get("cohort_effective_trials")
        or run_correction.get("deflated_sharpe_effective_trials")
        or multiple_testing.get("deflated_sharpe_effective_trials")
    )


def _research_only(item: dict[str, Any]) -> bool:
    payload = dict(item or {})
    return bool(payload.get("research_candidate_ready")) and not bool(
        payload.get("incubation_candidate_ready")
    )


def _economic_semantics_missing(item: dict[str, Any]) -> bool:
    payload = dict(item or {})
    quality_summary = dict(payload.get("quality_summary") or {})
    execution_reality = dict(payload.get("execution_reality") or {})
    backtest_assumptions = dict(payload.get("backtest_assumptions") or {})
    cost_assumptions = dict(payload.get("cost_assumptions") or {})
    candidate_provenance = dict(payload.get("candidate_provenance") or {})

    holding_rationale = _string(
        payload.get("holding_rationale")
        or quality_summary.get("holding_rationale")
        or candidate_provenance.get("holding_rationale")
    )
    alpha_half_life = payload.get("alpha_half_life") or quality_summary.get("alpha_half_life")
    cost_sensitivity_grid = (
        payload.get("cost_sensitivity_grid")
        or quality_summary.get("cost_sensitivity_grid")
        or cost_assumptions.get("cost_sensitivity_grid")
    )
    position_model = _string(
        payload.get("position_model")
        or quality_summary.get("position_model")
        or execution_reality.get("position_model")
        or payload.get("position_assumption")
        or quality_summary.get("position_assumption")
        or execution_reality.get("position_assumption")
        or backtest_assumptions.get("position_assumption")
        or backtest_assumptions.get("target_weight_scheme")
    )
    capacity_assumption = (
        payload.get("capacity_assumption")
        or quality_summary.get("capacity_assumption")
        or backtest_assumptions.get("capacity_participation_rate")
        or backtest_assumptions.get("capacity_bucket")
    )
    market_regime_assumption = (
        payload.get("market_regime_assumption")
        or quality_summary.get("market_regime_assumption")
        or execution_reality.get("market_regime_assumption")
    )

    holding_present = bool(holding_rationale or alpha_half_life or _holding_bucket(payload))
    position_present = bool(position_model)
    cost_present = bool(cost_sensitivity_grid) or bool(
        cost_assumptions.get("slippage_bps") is not None
        or cost_assumptions.get("market_impact_bps") is not None
        or payload.get("explicit_cost_breakdown")
        or payload.get("implicit_cost_breakdown")
    )
    capacity_present = bool(capacity_assumption)
    regime_present = bool(market_regime_assumption)
    default_position = _normalized_text(position_model) in {
        "single_name_full_notional",
        "single_name",
        "equal_weight",
        "equal_weight_proxy",
    }
    return (
        not (holding_present and position_present and cost_present and capacity_present and regime_present)
        or (default_position and not bool(cost_sensitivity_grid) and not capacity_present)
    )


def _compact_committee_review(value: Any) -> dict[str, Any]:
    payload = dict(value or {})
    if not payload:
        return {}
    result = _compact_mapping(
        payload,
        allowed_keys=(
            "decision",
            "final_score",
            "rank",
            "is_champion",
            "execution_score",
            "capacity_score",
            "task_alignment_score",
            "novelty_score",
        ),
        limit=8,
    )
    for key in ("alignment_issues", "execution_issues", "capacity_issues", "accept_blockers"):
        items = _compact_list(payload.get(key), limit=4)
        if items:
            result[key] = items
    return result


def _strategy_brief(strategy: dict[str, Any]) -> dict[str, Any]:
    payload = dict(strategy or {})
    params = dict(payload.get("params") or {})
    candidate_provenance = dict(payload.get("candidate_provenance") or {})
    evidence_alignment_audit = dict(
        payload.get("evidence_alignment_audit")
        or params.get("evidence_alignment_audit")
        or {}
    )
    confidence_contract = dict(
        payload.get("confidence_contract") or params.get("confidence_contract") or {}
    )
    constraint_check = _compact_mapping(
        payload.get("constraint_check"),
        allowed_keys=(
            "constraint_violation",
            "intersection_ratio",
            "expansion_applied",
            "expansion_reason",
            "expansion_source",
            "alignment_contract_violation",
        ),
        limit=6,
    )
    validation_profile = _compact_mapping(
        payload.get("validation_profile"),
        allowed_keys=("profile", "validation_focus", "primary_validation_layer"),
        limit=3,
    )
    event_window_config = _compact_mapping(payload.get("event_window_config"), limit=6)
    cost_assumptions = _compact_mapping(payload.get("cost_assumptions"), limit=6)
    explicit_cost_breakdown = _compact_mapping(payload.get("explicit_cost_breakdown"), limit=6)
    implicit_cost_breakdown = _compact_mapping(payload.get("implicit_cost_breakdown"), limit=6)
    attempt_adjustment = _compact_mapping(
        payload.get("attempt_adjustment"),
        allowed_keys=("attempt_count", "selected_count", "selection_ratio", "penalty", "applied"),
        limit=5,
    )
    primary_validation_layer = (
        _string(payload.get("primary_validation_layer"))
        or _string(validation_profile.get("primary_validation_layer"))
        or None
    )
    refresh_mode = _string(payload.get("refresh_mode")) or None
    position_assumption = _string(payload.get("position_assumption")) or None
    task_signature = _string(payload.get("task_signature")) or None
    committee_review = _compact_committee_review(payload.get("committee_review"))
    validation_grade = _effective_validation_grade(payload) or None
    raw_validation_grade = _raw_validation_grade(payload) or None
    effective_validation_grade = _effective_validation_grade(payload) or None
    validation_grade_adjustment_reason = _validation_grade_adjustment_reason(payload) or None
    confidence_contract_status = (
        _string(payload.get("confidence_contract_status"))
        or _string(params.get("confidence_contract_status"))
        or _confidence_contract_status(confidence_contract)
    )
    return {
        "strategy_id": _string(payload.get("strategy_id")) or None,
        "name": _string(payload.get("name")) or None,
        "status": _string(payload.get("status")) or None,
        "submission_lane": _string(payload.get("submission_lane")) or None,
        "submission_action_type": _string(payload.get("submission_action_type")) or None,
        "primary_validation_layer": primary_validation_layer,
        "refresh_mode": refresh_mode,
        "position_assumption": position_assumption,
        "task_signature": task_signature,
        "candidate_family": (
            _string(payload.get("candidate_family"))
            or _string(candidate_provenance.get("candidate_family"))
            or None
        ),
        "holding_period_bucket": _holding_bucket(payload) or None,
        "validation_grade": validation_grade,
        "raw_validation_grade": raw_validation_grade,
        "effective_validation_grade": effective_validation_grade,
        "validation_grade_adjustment_reason": validation_grade_adjustment_reason,
        "raw_validation_total_score": _validation_total_score(payload, raw=True),
        "validation_total_score": _validation_total_score(payload, raw=False),
        "raw_b_or_above": _is_raw_b_or_above(raw_validation_grade),
        "prediction_quality_label": (
            _string(payload.get("prediction_quality_label"))
            or _string(params.get("prediction_quality_label"))
            or None
        ),
        "execution_quality_label": (
            _string(payload.get("execution_quality_label"))
            or _string(params.get("execution_quality_label"))
            or None
        ),
        "confidence_contract_status": confidence_contract_status,
        "evidence_alignment_status": (
            _string(evidence_alignment_audit.get("evidence_alignment_status")) or None
        ),
        "legacy_semantic_contract": bool(
            payload.get("legacy_semantic_contract")
            if payload.get("legacy_semantic_contract") is not None
            else params.get("legacy_semantic_contract")
        )
        if (
            payload.get("legacy_semantic_contract") is not None
            or params.get("legacy_semantic_contract") is not None
        )
        else None,
        "generator_mode": (
            _string(payload.get("generator_mode"))
            or _string(candidate_provenance.get("generator_mode"))
            or None
        ),
        "source_candidate_artifact_id": _string(payload.get("source_candidate_artifact_id")) or None,
        "target_pool_id": _string(payload.get("target_pool_id")) or None,
        "vector_profile_id": _string(payload.get("vector_profile_id")) or None,
        "multiple_testing_registry_record_id": (
            _string(payload.get("multiple_testing_registry_record_id")) or None
        ),
        "constraint_check": constraint_check,
        "validation_profile": validation_profile,
        "event_window_config": event_window_config,
        "cost_assumptions": cost_assumptions,
        "explicit_cost_breakdown": explicit_cost_breakdown,
        "implicit_cost_breakdown": implicit_cost_breakdown,
        "attempt_adjustment": attempt_adjustment,
        "committee_review": committee_review,
        "candidate_local_attempt_count": _candidate_local_attempt_count(payload),
        "task_local_attempt_count": _task_local_attempt_count(payload),
        "cohort_effective_trials": round(_cohort_effective_trials(payload), 4),
        "economic_semantics_missing": _economic_semantics_missing(payload),
        "has_constraint_check": bool(constraint_check),
        "has_validation_profile": bool(validation_profile),
        "has_event_window_config": bool(event_window_config),
        "has_cost_assumptions": bool(cost_assumptions),
        "has_explicit_cost_breakdown": bool(explicit_cost_breakdown),
        "has_implicit_cost_breakdown": bool(implicit_cost_breakdown),
        "has_attempt_adjustment": bool(attempt_adjustment),
        "has_committee_review": bool(committee_review),
        "created_strategy_pool": bool(payload.get("created_strategy_pool")),
        "created_audit_only": bool(payload.get("created_audit_only")),
        "refreshed_existing": bool(payload.get("refreshed_existing")),
        "live_candidate_ready": bool(payload.get("live_candidate_ready")),
        "live_review_ready": bool(payload.get("live_review_ready")),
        "runtime_bootstrap_eligible": (
            bool(payload.get("runtime_bootstrap_eligible"))
            if payload.get("runtime_bootstrap_eligible") is not None
            else None
        ),
        "runtime_bootstrap_reason": _string(payload.get("runtime_bootstrap_reason")) or None,
        "runtime_bootstrap_budget_tier": _string(payload.get("runtime_bootstrap_budget_tier")) or None,
        "runtime_playbook_present": (
            bool(payload.get("runtime_playbook_present"))
            if payload.get("runtime_playbook_present") is not None
            else None
        ),
        "stage_clock_days": _safe_int(payload.get("stage_clock_days")) if payload.get("stage_clock_days") is not None else None,
        "signal_vacuum_days": _safe_int(payload.get("signal_vacuum_days")) if payload.get("signal_vacuum_days") is not None else None,
        "remediation_action": _string(payload.get("remediation_action")) or None,
        "remediation_reason": _string(payload.get("remediation_reason")) or None,
        "paper_lane_ready": (
            bool(payload.get("paper_lane_ready"))
            if payload.get("paper_lane_ready") is not None
            else None
        ),
        "direct_trade_candidate": bool(payload.get("direct_trade_candidate")),
    }


def _dedup_brief(candidate: dict[str, Any]) -> dict[str, Any]:
    payload = dict(candidate or {})
    dedup = dict(payload.get("dedup_result") or {})
    profile = dict(payload.get("strategy_profile") or {})
    return {
        "strategy_type": _string(payload.get("strategy_type")) or None,
        "generator_type": _string(payload.get("generator_type")) or None,
        "candidate_family_id": (
            _string(payload.get("candidate_family_id"))
            or _string(profile.get("candidate_family_id"))
            or None
        ),
        "target_symbols": _compact_list(payload.get("target_symbols"), limit=12),
        "duplicate": bool(dedup.get("duplicate")),
        "duplicate_level": _string(dedup.get("duplicate_level")) or None,
        "refresh_existing": bool(dedup.get("refresh_existing")),
        "refresh_mode": _string(dedup.get("refresh_mode")) or None,
        "matched_strategy_id": _string(dedup.get("matched_strategy_id")) or None,
        "refresh_decision_basis": _string(dedup.get("refresh_decision_basis")) or None,
        "revision_trigger_reason": _string(dedup.get("revision_trigger_reason")) or None,
        "target_overlap": round(_safe_float(dedup.get("target_overlap")), 4),
    }


def build_gate_artifact(
    *,
    quality_gate_report: dict[str, Any] | None = None,
    backtest_report: dict[str, Any] | None = None,
) -> dict[str, Any]:
    quality = dict(quality_gate_report or {})
    backtest = dict(backtest_report or {})
    backtest_summary = dict(backtest.get("summary") or {})
    gate_0 = dict(quality.get("gate_0") or {})
    pre_gate = dict(quality.get("pre_gate") or {})
    gate_1 = dict(quality.get("gate_1") or {})
    gate_2 = dict(quality.get("gate_2") or {})
    gate_3 = dict(quality.get("gate_3") or {})
    legacy_gate_executed = quality.get("legacy_gate_executed")
    if legacy_gate_executed is None:
        legacy_gate_executed = bool(gate_0 or pre_gate or gate_1 or gate_2)
    legacy_funnel_executed = quality.get("legacy_funnel_executed")
    if legacy_funnel_executed is None:
        legacy_funnel_executed = bool(legacy_gate_executed)
    evidence_scoring_mode = _string(quality.get("evidence_scoring_mode")) or None
    return {
        "contract_version": GATE_ARTIFACT_CONTRACT_VERSION,
        "available": bool(quality or backtest),
        "artifact_mode": evidence_scoring_mode or "legacy_gate_report",
        "legacy_gate_executed": bool(legacy_gate_executed),
        "legacy_funnel_executed": bool(legacy_funnel_executed),
        "legacy_gate_report_mode": _string(quality.get("legacy_gate_report_mode")) or None,
        "evidence_scoring_mode": evidence_scoring_mode,
        "pre_observe_gate_removed": bool(quality.get("pre_observe_gate_removed")),
        "gate_0_passed": _safe_int(gate_0.get("passed_count")),
        "gate_0_failed": _safe_int(gate_0.get("failed_count")),
        "pre_gate_passed": _safe_int(pre_gate.get("passed_count")),
        "pre_gate_failed": _safe_int(pre_gate.get("failed_count")),
        "gate_1_passed": _safe_int(gate_1.get("passed_count")),
        "gate_1_failed": _safe_int(gate_1.get("failed_count")),
        "gate_2_input": _safe_int(gate_2.get("input_count"), _safe_int(backtest_summary.get("input_count"))),
        "gate_2_passed": _safe_int(gate_2.get("passed_count"), _safe_int(backtest_summary.get("passed_count"))),
        "gate_2_failed": _safe_int(gate_2.get("failed_count"), _safe_int(backtest_summary.get("failed_count"))),
        "gate_3_input": _safe_int(gate_3.get("input_count")),
        "gate_3_pending_count": _safe_int(gate_3.get("pending_count")),
        "gate_3_passed": _safe_int(gate_3.get("passed_count")),
        "gate_3_failed": _safe_int(gate_3.get("failed_count")),
        "gate_3_provisional_passed": _safe_int(gate_3.get("provisional_passed_count")),
        "backtest_failed_reason_counts": dict(backtest_summary.get("failed_reason_counts") or {}),
        "backtest_thresholds_by_type": dict(backtest_summary.get("thresholds_by_type") or {}),
        "gate_3_failure_reason_topn": _compact_reason_topn(gate_3.get("failure_reason_topn")),
    }

# --- governance_plane_contract_parts/evaluation.py ---
def build_dedup_artifact(
    *,
    dedup_report: dict[str, Any] | None = None,
) -> dict[str, Any]:
    report = dict(dedup_report or {})
    summary = dict(report.get("summary") or {})
    kept = [dict(item or {}) for item in list(report.get("kept") or []) if isinstance(item, dict)]
    dropped = [dict(item or {}) for item in list(report.get("dropped") or []) if isinstance(item, dict)]
    refresh_mode_counts = _count_by([*kept, *dropped], lambda item: dict(item.get("dedup_result") or {}).get("refresh_mode"))
    duplicate_level_counts = _count_by(
        dropped,
        lambda item: dict(item.get("dedup_result") or {}).get("duplicate_level"),
    )
    return {
        "contract_version": DEDUP_ARTIFACT_CONTRACT_VERSION,
        "available": bool(report),
        "input_count": _safe_int(summary.get("input_count")),
        "existing_count": _safe_int(summary.get("existing_count")),
        "existing_scan_count": _safe_int(summary.get("existing_scan_count")),
        "kept_count": _safe_int(summary.get("kept_count"), len(kept)),
        "dropped_count": _safe_int(summary.get("dropped_count"), len(dropped)),
        "refreshed_existing_count": _safe_int(summary.get("refreshed_existing_count")),
        "vector_checks": _safe_int(summary.get("vector_checks")),
        "coarse_hit_ratio": round(_safe_float(summary.get("coarse_hit_ratio")), 4),
        "refresh_mode_counts": refresh_mode_counts,
        "duplicate_level_counts": duplicate_level_counts,
        "refresh_decision_basis_counts": dict(summary.get("refresh_decision_basis_counts") or {}),
        "revision_trigger_reason_counts": dict(summary.get("revision_trigger_reason_counts") or {}),
        "tested_object_hash_changed_count": _safe_int(summary.get("tested_object_hash_changed_count")),
        "existing_identity_available_count": _safe_int(summary.get("existing_identity_available_count")),
        "existing_tested_object_available_count": _safe_int(
            summary.get("existing_tested_object_available_count")
        ),
        "kept_briefs": [_dedup_brief(item) for item in kept[:12]],
        "dropped_briefs": [_dedup_brief(item) for item in dropped[:12]],
    }


def build_submission_artifact(
    *,
    submit_result: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload = dict(submit_result or {})
    strategies = [dict(item or {}) for item in list(payload.get("strategies") or []) if isinstance(item, dict)]
    submission_lane_counts = _count_by(strategies, lambda item: item.get("submission_lane"))
    submission_action_type_counts = _count_by(strategies, lambda item: item.get("submission_action_type"))
    strategy_status_counts = _count_by(strategies, lambda item: item.get("status"))
    committee_decision_counts = _count_by(strategies, _committee_decision)
    refresh_mode_counts = _count_by(strategies, lambda item: item.get("refresh_mode"))
    primary_validation_layer_counts = _count_by(strategies, _primary_validation_layer)
    validation_profile_counts = _count_by(strategies, _validation_profile_name)
    constraint_violation_counts = _count_by(strategies, _constraint_violation)
    committee_review_count = sum(1 for item in strategies if _has_mapping(item, "committee_review"))
    constraint_check_count = sum(1 for item in strategies if _has_mapping(item, "constraint_check"))
    validation_profile_count = sum(1 for item in strategies if _has_mapping(item, "validation_profile"))
    event_window_config_count = sum(1 for item in strategies if _has_mapping(item, "event_window_config"))
    position_assumption_count = sum(1 for item in strategies if _has_text(item, "position_assumption"))
    cost_assumptions_count = sum(1 for item in strategies if _has_mapping(item, "cost_assumptions"))
    explicit_cost_breakdown_count = sum(
        1 for item in strategies if _has_mapping(item, "explicit_cost_breakdown")
    )
    implicit_cost_breakdown_count = sum(
        1 for item in strategies if _has_mapping(item, "implicit_cost_breakdown")
    )
    attempt_adjustment_count = sum(1 for item in strategies if _has_mapping(item, "attempt_adjustment"))
    task_signature_count = sum(1 for item in strategies if _has_text(item, "task_signature"))
    research_only_count = sum(1 for item in strategies if _research_only(item))
    deferred_submission_count = sum(
        1
        for item in strategies
        if _normalized_text(item.get("submission_lane")) == "deferred_submission"
    )
    validation_grade_distribution: dict[str, int] = {}
    raw_validation_grade_distribution: dict[str, int] = {}
    effective_validation_grade_distribution: dict[str, int] = {}
    raw_validation_total_scores: list[float] = []
    strict_incubation_ready_count = 0
    live_candidate_ready_count = 0
    raw_b_or_above_count = 0
    strict_ready_given_raw_b_count = 0
    live_ready_given_raw_b_count = 0
    for item in strategies:
        effective_grade = _effective_validation_grade(item)
        raw_grade = _raw_validation_grade(item)
        strict_ready = _bool_payload(item, "strict_incubation_ready")
        live_ready = _bool_payload(item, "live_candidate_ready")
        if effective_grade:
            validation_grade_distribution[effective_grade] = (
                validation_grade_distribution.get(effective_grade, 0) + 1
            )
            effective_validation_grade_distribution[effective_grade] = (
                effective_validation_grade_distribution.get(effective_grade, 0) + 1
            )
        if raw_grade:
            raw_validation_grade_distribution[raw_grade] = (
                raw_validation_grade_distribution.get(raw_grade, 0) + 1
            )
        raw_score = _validation_total_score(item, raw=True)
        if raw_score is not None:
            raw_validation_total_scores.append(float(raw_score))
        if strict_ready:
            strict_incubation_ready_count += 1
        if live_ready:
            live_candidate_ready_count += 1
        if _is_raw_b_or_above(raw_grade):
            raw_b_or_above_count += 1
            if strict_ready:
                strict_ready_given_raw_b_count += 1
            if live_ready:
                live_ready_given_raw_b_count += 1
    candidate_local_attempt_count = sum(_candidate_local_attempt_count(item) for item in strategies)
    task_local_attempt_count = sum(_task_local_attempt_count(item) for item in strategies)
    cohort_effective_trials = round(
        sum(_cohort_effective_trials(item) for item in strategies),
        4,
    )
    unique_family_holding_universe_count = len(
        {
            (
                _candidate_family(item) or "unknown",
                _holding_bucket(item) or "unknown",
                _target_universe_key(item) or "unknown",
            )
            for item in strategies
        }
    )
    economic_semantics_missing_count = sum(
        1 for item in strategies if _economic_semantics_missing(item)
    )
    family_quality_panel = _aggregate_family_quality_panel(strategies)
    raw_validation_total_score_mean = round(
        sum(raw_validation_total_scores) / len(raw_validation_total_scores),
        4,
    ) if raw_validation_total_scores else 0.0
    return {
        "contract_version": SUBMISSION_ARTIFACT_CONTRACT_VERSION,
        "available": bool(payload),
        "strategy_count": len(strategies),
        "created_count": _safe_int(payload.get("created")),
        "created_total_count": _safe_int(payload.get("created_total")),
        "created_strategy_pool_count": _safe_int(payload.get("created_strategy_pool")),
        "created_audit_only_count": _safe_int(payload.get("created_audit_only")),
        "refreshed_count": _safe_int(payload.get("refreshed")),
        "gate_3_input": _safe_int(payload.get("gate_3_input")),
        "submitted_count": _safe_int(payload.get("submitted")),
        "passed_quality_gate_count": _safe_int(payload.get("passed_quality_gate")),
        "gate_3_passed": _safe_int(payload.get("gate_3_passed")),
        "gate_3_failed": _safe_int(payload.get("gate_3_failed")),
        "gate_3_provisional_passed": _safe_int(payload.get("gate_3_provisional_passed")),
        "incubation_budget_summary": dict(payload.get("incubation_budget_summary") or {}),
        "gate_3_failure_reason_topn": _compact_reason_topn(payload.get("gate_3_failure_reason_topn")),
        "submission_lane_counts": submission_lane_counts,
        "submission_action_type_counts": submission_action_type_counts,
        "strategy_status_counts": strategy_status_counts,
        "committee_decision_counts": committee_decision_counts,
        "refresh_mode_counts": refresh_mode_counts,
        "committee_review_count": committee_review_count,
        "primary_validation_layer_counts": primary_validation_layer_counts,
        "validation_profile_counts": validation_profile_counts,
        "constraint_violation_counts": constraint_violation_counts,
        "constraint_check_count": constraint_check_count,
        "validation_profile_count": validation_profile_count,
        "event_window_config_count": event_window_config_count,
        "position_assumption_count": position_assumption_count,
        "cost_assumptions_count": cost_assumptions_count,
        "explicit_cost_breakdown_count": explicit_cost_breakdown_count,
        "implicit_cost_breakdown_count": implicit_cost_breakdown_count,
        "attempt_adjustment_count": attempt_adjustment_count,
        "task_signature_count": task_signature_count,
        "research_only_count": research_only_count,
        "deferred_submission_count": deferred_submission_count,
        "validation_grade_distribution": validation_grade_distribution,
        "raw_validation_grade_distribution": raw_validation_grade_distribution,
        "effective_validation_grade_distribution": effective_validation_grade_distribution,
        "raw_validation_total_score_mean": raw_validation_total_score_mean,
        "raw_validation_total_score_p50": _percentile(raw_validation_total_scores, 0.5),
        "raw_validation_total_score_p90": _percentile(raw_validation_total_scores, 0.9),
        **_grade_rates(raw_validation_grade_distribution, len(strategies)),
        "strict_incubation_ready_count": strict_incubation_ready_count,
        "strict_incubation_ready_rate": _rate(strict_incubation_ready_count, len(strategies)),
        "live_candidate_ready_count": live_candidate_ready_count,
        "live_candidate_ready_rate": _rate(live_candidate_ready_count, len(strategies)),
        "raw_b_or_above_count": raw_b_or_above_count,
        "raw_b_or_above_rate": _rate(raw_b_or_above_count, len(strategies)),
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
        "validation_family_quality_panel": family_quality_panel,
        "candidate_local_attempt_count": candidate_local_attempt_count,
        "task_local_attempt_count": task_local_attempt_count,
        "cohort_effective_trials": cohort_effective_trials,
        "unique_family_holding_universe_count": unique_family_holding_universe_count,
        "economic_semantics_missing_count": economic_semantics_missing_count,
        "strategy_briefs": [_strategy_brief(item) for item in strategies[:12]],
    }


def build_governance_evidence_artifact(
    *,
    submit_result: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload = dict(submit_result or {})
    strategies = [dict(item or {}) for item in list(payload.get("strategies") or []) if isinstance(item, dict)]

    def _has_numeric(item: dict[str, Any], key: str) -> bool:
        source = dict(item.get("backtest_assumptions") or {})
        cost = dict(item.get("cost_assumptions") or {})
        if source.get(key) is not None or cost.get(key) is not None:
            return True
        return False

    vector_backend_counts = _count_by(
        strategies,
        lambda item: item.get("vector_backend_used") or item.get("vector_backend"),
    )
    quality_report_count = len(strategies)
    multiple_testing_registry_count = sum(1 for item in strategies if _has_mapping(item, "multiple_testing_registry"))
    multiple_testing_registry_record_count = sum(
        1 for item in strategies if _string(item.get("multiple_testing_registry_record_id"))
    )
    lineage_contract_count = sum(1 for item in strategies if _has_mapping(item, "candidate_lineage_contract"))
    lineage_id_count = sum(
        1
        for item in strategies
        if _string(dict(item.get("candidate_lineage_contract") or {}).get("lineage_id"))
    )
    committee_review_count = sum(1 for item in strategies if _has_mapping(item, "committee_review"))
    constraint_check_count = sum(1 for item in strategies if _has_mapping(item, "constraint_check"))
    validation_profile_count = sum(1 for item in strategies if _has_mapping(item, "validation_profile"))
    event_window_config_count = sum(1 for item in strategies if _has_mapping(item, "event_window_config"))
    position_assumption_count = sum(1 for item in strategies if _has_text(item, "position_assumption"))
    vector_profile_count = sum(1 for item in strategies if _string(item.get("vector_profile_id")))
    cost_assumptions_count = sum(1 for item in strategies if _has_mapping(item, "cost_assumptions"))
    explicit_cost_breakdown_count = sum(1 for item in strategies if _has_mapping(item, "explicit_cost_breakdown"))
    implicit_cost_breakdown_count = sum(1 for item in strategies if _has_mapping(item, "implicit_cost_breakdown"))
    execution_reality_count = sum(1 for item in strategies if _has_mapping(item, "execution_reality"))
    attempt_adjustment_count = sum(1 for item in strategies if _has_mapping(item, "attempt_adjustment"))
    task_signature_count = sum(1 for item in strategies if _has_text(item, "task_signature"))
    refresh_mode_count = sum(1 for item in strategies if _has_text(item, "refresh_mode"))
    primary_validation_layer_count = sum(1 for item in strategies if bool(_primary_validation_layer(item)))
    slippage_assumption_count = sum(1 for item in strategies if _has_numeric(item, "slippage_bps"))
    market_impact_assumption_count = sum(1 for item in strategies if _has_numeric(item, "market_impact_bps"))
    capacity_assumption_count = sum(
        1
        for item in strategies
        if dict(item.get("backtest_assumptions") or {}).get("capacity_participation_rate") is not None
        or _string(dict(item.get("backtest_assumptions") or {}).get("capacity_bucket"))
    )
    tradability_filter_count = sum(
        1
        for item in strategies
        if dict(item.get("backtest_assumptions") or {}).get("tradability_filter") is not None
    )
    evidence_briefs = [
        {
            **_strategy_brief(item),
            "lineage_id": _string(dict(item.get("candidate_lineage_contract") or {}).get("lineage_id")) or None,
            "vector_backend": (
                _string(item.get("vector_backend_used"))
                or _string(item.get("vector_backend"))
                or None
            ),
            "has_cost_assumptions": _has_mapping(item, "cost_assumptions"),
            "has_execution_reality": _has_mapping(item, "execution_reality"),
            "has_multiple_testing_registry": _has_mapping(item, "multiple_testing_registry"),
        }
        for item in strategies[:12]
    ]
    return {
        "contract_version": GOVERNANCE_EVIDENCE_ARTIFACT_CONTRACT_VERSION,
        "available": bool(strategies),
        "quality_report_count": quality_report_count,
        "multiple_testing_registry_count": multiple_testing_registry_count,
        "multiple_testing_registry_record_count": multiple_testing_registry_record_count,
        "lineage_contract_count": lineage_contract_count,
        "lineage_id_count": lineage_id_count,
        "committee_review_count": committee_review_count,
        "constraint_check_count": constraint_check_count,
        "validation_profile_count": validation_profile_count,
        "event_window_config_count": event_window_config_count,
        "position_assumption_count": position_assumption_count,
        "vector_profile_count": vector_profile_count,
        "vector_backend_counts": vector_backend_counts,
        "cost_assumptions_count": cost_assumptions_count,
        "explicit_cost_breakdown_count": explicit_cost_breakdown_count,
        "implicit_cost_breakdown_count": implicit_cost_breakdown_count,
        "execution_reality_count": execution_reality_count,
        "attempt_adjustment_count": attempt_adjustment_count,
        "task_signature_count": task_signature_count,
        "refresh_mode_count": refresh_mode_count,
        "primary_validation_layer_count": primary_validation_layer_count,
        "slippage_assumption_count": slippage_assumption_count,
        "market_impact_assumption_count": market_impact_assumption_count,
        "capacity_assumption_count": capacity_assumption_count,
        "tradability_filter_count": tradability_filter_count,
        "extension_interface_support": {
            "constraint_check_supported": bool(constraint_check_count),
            "validation_profile_supported": bool(validation_profile_count),
            "event_window_supported": bool(event_window_config_count),
            "position_assumption_supported": bool(position_assumption_count),
            "committee_review_supported": bool(committee_review_count),
            "cost_assumptions_supported": bool(cost_assumptions_count),
            "execution_reality_supported": bool(execution_reality_count),
            "attempt_adjustment_supported": bool(attempt_adjustment_count),
            "task_signature_supported": bool(task_signature_count),
            "refresh_mode_supported": bool(refresh_mode_count),
            "primary_validation_layer_supported": bool(primary_validation_layer_count),
            "slippage_supported": bool(slippage_assumption_count),
            "market_impact_supported": bool(market_impact_assumption_count),
            "capacity_supported": bool(capacity_assumption_count),
            "tradability_filter_supported": bool(tradability_filter_count),
        },
        "strategy_evidence_briefs": evidence_briefs,
    }


def _legacy_gate_mapping(
    *,
    quality_gate_report: dict[str, Any] | None = None,
    submit_result: dict[str, Any] | None = None,
) -> dict[str, Any]:
    quality = dict(quality_gate_report or {})
    submit = dict(submit_result or {})
    legacy_gate_executed = quality.get("legacy_gate_executed")
    if legacy_gate_executed is None:
        legacy_gate_executed = bool(
            quality.get("gate_0")
            or quality.get("pre_gate")
            or quality.get("gate_1")
            or quality.get("gate_2")
        )
    legacy_funnel_executed = quality.get("legacy_funnel_executed")
    if legacy_funnel_executed is None:
        legacy_funnel_executed = bool(legacy_gate_executed)
    return {
        "artifact_mode": _string(quality.get("evidence_scoring_mode")) or "legacy_gate_mapping",
        "legacy_gate_executed": bool(legacy_gate_executed),
        "legacy_funnel_executed": bool(legacy_funnel_executed),
        "legacy_gate_report_mode": _string(quality.get("legacy_gate_report_mode")) or None,
        "evidence_scoring_mode": _string(quality.get("evidence_scoring_mode")) or None,
        "gate_a": ["gate_0", "pre_gate", "gate_1", "semantic_contract", "strategy_reviewer"],
        "gate_b": ["gate_2", "gate_3", "submission_gate", "dedup", "submit"],
        "gate_c": [
            "signal_quality",
            "execution_quality",
            "execution_audit_gate_status",
            "hard_gate_result",
            "promotion_ready",
        ],
        "legacy_counts": {
            "gate_0_passed": _safe_int(dict(quality.get("gate_0") or {}).get("passed_count")),
            "pre_gate_passed": _safe_int(dict(quality.get("pre_gate") or {}).get("passed_count")),
            "gate_1_passed": _safe_int(dict(quality.get("gate_1") or {}).get("passed_count")),
            "gate_2_passed": _safe_int(dict(quality.get("gate_2") or {}).get("passed_count")),
            "gate_3_passed": _safe_int(dict(quality.get("gate_3") or {}).get("passed_count")),
            "submitted": _safe_int(submit.get("submitted")),
        },
    }


def build_gate_a_artifact(
    *,
    candidates: list[dict[str, Any]] | None = None,
    quality_gate_report: dict[str, Any] | None = None,
) -> dict[str, Any]:
    quality = dict(quality_gate_report or {})
    gate_0 = dict(quality.get("gate_0") or {})
    pre_gate = dict(quality.get("pre_gate") or {})
    gate_1 = dict(quality.get("gate_1") or {})
    items = [dict(item or {}) for item in list(candidates or []) if isinstance(item, dict)]
    completeness_counts = _count_by(
        items,
        lambda item: _candidate_spec_completeness(item) or "unknown",
    )
    blocking_reason_counts: dict[str, int] = {}
    evidence_gap_codes: list[str] = []
    revision_actions: list[str] = []
    evidence_refs: list[str] = []
    hard_failures: list[dict[str, Any]] = []
    artifact_ids: list[str] = []
    retrieval_context_ids: list[str] = []
    for item in items:
        for issue in list(_candidate_payload_value(item, "completion_issues") or []):
            payload = dict(issue or {})
            reason_code = _string(payload.get("reason_code") or payload.get("issue") or payload.get("field"))
            if reason_code:
                blocking_reason_counts[reason_code] = blocking_reason_counts.get(reason_code, 0) + 1
                decision = _normalized_text(payload.get("decision") or payload.get("severity"))
                if decision == "reject":
                    failure = _normalize_hard_failure_entry(
                        reason_code,
                        issue=_string(payload.get("issue")) or "hard_failure",
                        field=payload.get("field"),
                    )
                    if failure:
                        hard_failures.append(failure)
                else:
                    evidence_gap_codes.append(reason_code)
            field_name = _string(payload.get("field"))
            if field_name:
                action = f"provide_required_research_field:{field_name}"
                if action not in revision_actions:
                    revision_actions.append(action)
        for failure in list(_candidate_payload_value(item, "hard_failures") or []):
            payload = dict(failure or {})
            normalized = _normalize_hard_failure_entry(
                payload.get("reason_code") or payload.get("issue"),
                issue=_string(payload.get("issue")) or "hard_failure",
                field=payload.get("field"),
                detail=payload.get("detail"),
            )
            if normalized:
                hard_failures.append(normalized)
        if bool(_candidate_payload_value(item, "execution_semantic_gap")):
            blocking_reason_counts["execution_semantic_gap"] = blocking_reason_counts.get("execution_semantic_gap", 0) + 1
            failure = _normalize_hard_failure_entry(
                "execution_semantic_gap",
                issue="execution_semantic_gap",
            )
            if failure:
                hard_failures.append(failure)
            if "repair_execution_semantic_gap" not in revision_actions:
                revision_actions.append("repair_execution_semantic_gap")
        for field_name in list(_candidate_payload_value(item, "semantic_contract_missing_fields") or []):
            key = _string(field_name)
            if key:
                reason_code = f"semantic_contract_missing:{key}"
                blocking_reason_counts[reason_code] = blocking_reason_counts.get(reason_code, 0) + 1
                failure = _normalize_hard_failure_entry(
                    reason_code,
                    issue="semantic_contract_missing_field",
                    field=key,
                )
                if failure:
                    hard_failures.append(failure)
                action = f"repair_semantic_contract_field:{key}"
                if action not in revision_actions:
                    revision_actions.append(action)
        trace_id = _candidate_prediction_trace_id(item)
        if trace_id and trace_id not in evidence_refs:
            evidence_refs.append(trace_id)
        for artifact_id in _candidate_artifact_ids(item):
            if artifact_id not in artifact_ids:
                artifact_ids.append(artifact_id)
        for context_id in _candidate_retrieval_context_ids(item):
            if context_id not in retrieval_context_ids:
                retrieval_context_ids.append(context_id)
    has_activity = bool(items or quality)
    blocked = bool(
        _safe_int(gate_0.get("failed_count"))
        or _safe_int(pre_gate.get("failed_count"))
        or _safe_int(gate_1.get("failed_count"))
        or blocking_reason_counts
    )
    hard_failures = _unique_hard_failures(hard_failures)
    decision = "reject" if hard_failures else "revise" if blocked else "pass"
    return {
        "contract_version": GATE_ARTIFACT_V2_CONTRACT_VERSION,
        "gate_name": "gate_a",
        "stage": "gate_a",
        "decision": decision,
        "status": "pending" if not has_activity else "blocked" if blocked else "passed",
        "hard_failures": hard_failures,
        "evidence_gap_codes": _compact_list(evidence_gap_codes, limit=16),
        "artifact_ids": artifact_ids[:16],
        "retrieval_context_ids": retrieval_context_ids[:16],
        "trace_ids": evidence_refs[:12],
        "family_outcome_summary": _family_outcome_summary(items),
        "blocking_reasons": _top_counts(blocking_reason_counts, label_key="reason_code"),
        "warnings": ["spec_completeness_incomplete"] if completeness_counts.get("incomplete") else [],
        "revision_actions": revision_actions[:12],
        "evidence_refs": evidence_refs[:12],
        "legacy_gate_mapping": ["gate_0", "pre_gate", "gate_1"],
        "input_count": len(items),
        "gate_0_passed": _safe_int(gate_0.get("passed_count")),
        "pre_gate_passed": _safe_int(pre_gate.get("passed_count")),
        "gate_1_passed": _safe_int(gate_1.get("passed_count")),
        "spec_completeness_counts": completeness_counts,
    }


def build_gate_b_artifact(
    *,
    quality_gate_report: dict[str, Any] | None = None,
    backtest_report: dict[str, Any] | None = None,
    submit_result: dict[str, Any] | None = None,
) -> dict[str, Any]:
    quality = dict(quality_gate_report or {})
    backtest = dict(backtest_report or {})
    submit = dict(submit_result or {})
    gate_2 = dict(quality.get("gate_2") or {})
    gate_3 = dict(quality.get("gate_3") or {})
    backtest_summary = dict(backtest.get("summary") or {})
    strategies = [dict(item or {}) for item in list(submit.get("strategies") or []) if isinstance(item, dict)]
    failure_reason_counts = {
        _string(item.get("reason") or item.get("reason_code")): _safe_int(item.get("count"))
        for item in list(gate_3.get("failure_reason_topn") or submit.get("gate_3_failure_reason_topn") or [])
        if _string(item.get("reason") or item.get("reason_code"))
    }
    blocked = bool(_safe_int(gate_3.get("failed_count") or submit.get("gate_3_failed")))
    has_activity = bool(quality or backtest or submit)
    hard_failures = _unique_hard_failures(
        [
            _normalize_hard_failure_entry(code, issue="submission_gate_blocker")
            for code in failure_reason_counts
        ]
    )
    warnings = _compact_list(gate_3.get("warning_codes") or submit.get("warning_codes") or [])
    trace_ids: list[str] = []
    artifact_ids: list[str] = []
    retrieval_context_ids: list[str] = []
    review_decision_counts: dict[str, int] = {}
    sample_business_admission_decision: dict[str, Any] = {}
    sample_benchmark_comparison: dict[str, Any] = {}
    sample_cost_sensitivity_summary: dict[str, Any] = {}
    sample_cash_sleeve_audit: dict[str, Any] = {}
    for item in strategies:
        review_decision = _normalized_text(
            dict(item.get("gate_b") or {}).get("review_decision")
            or dict(item.get("business_admission_decision") or {}).get("decision")
            or item.get("gate_b_review_decision")
        )
        if review_decision:
            review_decision_counts[review_decision] = review_decision_counts.get(review_decision, 0) + 1
        if not sample_business_admission_decision:
            sample_business_admission_decision = dict(
                dict(item.get("gate_b") or {}).get("business_admission_decision")
                or item.get("business_admission_decision")
                or {}
            )
        if not sample_benchmark_comparison:
            sample_benchmark_comparison = dict(
                dict(item.get("gate_b") or {}).get("benchmark_comparison")
                or item.get("benchmark_comparison")
                or {}
            )
        if not sample_cost_sensitivity_summary:
            sample_cost_sensitivity_summary = dict(
                dict(item.get("gate_b") or {}).get("cost_sensitivity_summary")
                or item.get("cost_sensitivity_summary")
                or {}
            )
        if not sample_cash_sleeve_audit:
            sample_cash_sleeve_audit = dict(
                dict(item.get("gate_b") or {}).get("cash_sleeve_audit")
                or item.get("cash_sleeve_audit")
                or {}
            )
        for trace_id in _candidate_trace_ids(item):
            if trace_id not in trace_ids:
                trace_ids.append(trace_id)
        for artifact_id in _candidate_artifact_ids(item):
            if artifact_id not in artifact_ids:
                artifact_ids.append(artifact_id)
        for context_id in _candidate_retrieval_context_ids(item):
            if context_id not in retrieval_context_ids:
                retrieval_context_ids.append(context_id)
    decision = "block" if blocked else "pass" if has_activity else "pending"
    review_decision = "pending"
    if review_decision_counts.get("reject"):
        review_decision = "reject"
    elif review_decision_counts.get("revise"):
        review_decision = "revise"
    elif has_activity:
        review_decision = "pass"
    return {
        "contract_version": GATE_ARTIFACT_V2_CONTRACT_VERSION,
        "gate_name": "gate_b",
        "stage": "gate_b",
        "decision": decision,
        "review_decision": review_decision,
        "status": (
            "pending"
            if not has_activity
            else "blocked"
            if blocked
            else "passed"
            if _safe_int(submit.get("submitted") or gate_3.get("passed_count") or gate_2.get("passed_count"))
            else "pending"
        ),
        "hard_failures": hard_failures,
        "evidence_gap_codes": warnings,
        "artifact_ids": artifact_ids[:16],
        "retrieval_context_ids": retrieval_context_ids[:16],
        "trace_ids": trace_ids[:12],
        "family_outcome_summary": _family_outcome_summary(strategies),
        "blocking_reasons": _top_counts(failure_reason_counts, label_key="reason_code"),
        "warnings": warnings,
        "evidence_refs": [],
        "legacy_gate_mapping": ["gate_2", "gate_3"],
        "business_admission_decision_distribution": review_decision_counts,
        "business_admission_decision": sample_business_admission_decision,
        "benchmark_comparison": sample_benchmark_comparison,
        "cost_sensitivity_summary": sample_cost_sensitivity_summary,
        "cash_sleeve_audit": sample_cash_sleeve_audit,
        "gate_2_input": _safe_int(gate_2.get("input_count"), _safe_int(backtest_summary.get("input_count"))),
        "gate_2_passed": _safe_int(gate_2.get("passed_count"), _safe_int(backtest_summary.get("passed_count"))),
        "gate_2_failed": _safe_int(gate_2.get("failed_count"), _safe_int(backtest_summary.get("failed_count"))),
        "gate_3_input": _safe_int(gate_3.get("input_count"), _safe_int(submit.get("gate_3_input"))),
        "gate_3_passed": _safe_int(gate_3.get("passed_count"), _safe_int(submit.get("gate_3_passed"))),
        "gate_3_failed": _safe_int(gate_3.get("failed_count"), _safe_int(submit.get("gate_3_failed"))),
        "gate_3_provisional_passed": _safe_int(
            gate_3.get("provisional_passed_count"),
            _safe_int(submit.get("gate_3_provisional_passed")),
        ),
    }

# --- governance_plane_contract_parts/reporting.py ---
def build_gate_c_artifact(
    *,
    submit_result: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload = dict(submit_result or {})
    strategies = [dict(item or {}) for item in list(payload.get("strategies") or []) if isinstance(item, dict)]
    signal_quality_counts = _count_by(
        strategies,
        lambda item: dict(item.get("signal_quality_snapshot") or {}).get("status") or item.get("signal_quality"),
    )
    execution_quality_counts = _count_by(
        strategies,
        lambda item: dict(item.get("execution_quality_snapshot") or {}).get("status") or item.get("execution_quality"),
    )
    execution_audit_counts = _count_by(strategies, lambda item: item.get("execution_audit_gate_status"))
    hard_gate_counts = _count_by(strategies, lambda item: item.get("hard_gate_result"))
    promotion_ready_count = sum(1 for item in strategies if bool(item.get("promotion_ready")))
    gate_blocker_counts: dict[str, int] = {}
    evidence_refs: list[str] = []
    evidence_gap_codes: list[str] = []
    hard_failures: list[dict[str, Any]] = []
    artifact_ids: list[str] = []
    retrieval_context_ids: list[str] = []
    for item in strategies:
        trace_id = _string(item.get("prediction_trace_id") or item.get("trace_id"))
        if trace_id and trace_id not in evidence_refs:
            evidence_refs.append(trace_id)
        for reason in list(item.get("gate_blockers") or item.get("reasons") or []):
            code = _string(reason)
            if code:
                gate_blocker_counts[code] = gate_blocker_counts.get(code, 0) + 1
                failure = _normalize_hard_failure_entry(code, issue="promotion_gate_blocker")
                if failure:
                    hard_failures.append(failure)
        execution_audit_gate_status = _normalized_text(item.get("execution_audit_gate_status"))
        if execution_audit_gate_status in {
            "missing",
            "bootstrap_pending",
            "insufficient_samples",
            "bootstrap_ready",
            "insufficient_evidence",
        }:
            evidence_gap_codes.append(f"execution_audit_gate:{execution_audit_gate_status}")
        for code in list(item.get("evidence_gap_codes") or []):
            token = _string(code)
            if token:
                evidence_gap_codes.append(token)
        for artifact_id in _candidate_artifact_ids(item):
            if artifact_id not in artifact_ids:
                artifact_ids.append(artifact_id)
        for context_id in _candidate_retrieval_context_ids(item):
            if context_id not in retrieval_context_ids:
                retrieval_context_ids.append(context_id)
    blocked = bool(gate_blocker_counts)
    hard_failures = _unique_hard_failures(hard_failures)
    return {
        "contract_version": GATE_ARTIFACT_V2_CONTRACT_VERSION,
        "gate_name": "gate_c",
        "stage": "gate_c",
        "decision": "block" if blocked else "observe" if strategies else "pending",
        "status": "blocked" if blocked else "observe" if strategies else "pending",
        "hard_failures": hard_failures,
        "evidence_gap_codes": _compact_list(evidence_gap_codes, limit=16),
        "artifact_ids": artifact_ids[:16],
        "retrieval_context_ids": retrieval_context_ids[:16],
        "trace_ids": evidence_refs[:12],
        "family_outcome_summary": _family_outcome_summary(strategies),
        "blocking_reasons": _top_counts(gate_blocker_counts, label_key="reason_code"),
        "warnings": [],
        "evidence_refs": evidence_refs[:12],
        "legacy_gate_mapping": [
            "signal_quality",
            "execution_quality",
            "execution_audit_gate_status",
            "hard_gate_result",
            "promotion_ready",
        ],
        "signal_quality_distribution": signal_quality_counts,
        "execution_quality_distribution": execution_quality_counts,
        "execution_audit_gate_status_distribution": execution_audit_counts,
        "hard_gate_result_distribution": hard_gate_counts,
        "promotion_ready_count": promotion_ready_count,
        "promotion_ready_rate": round(promotion_ready_count / max(len(strategies), 1), 4) if strategies else 0.0,
    }


def _build_protocol_version_summary(
    *,
    candidates: list[dict[str, Any]] | None = None,
    submit_result: dict[str, Any] | None = None,
) -> dict[str, Any]:
    candidate_items = [dict(item or {}) for item in list(candidates or []) if isinstance(item, dict)]
    strategy_items = [dict(item or {}) for item in list(dict(submit_result or {}).get("strategies") or []) if isinstance(item, dict)]
    research_protocol_counts = _count_by(
        [*candidate_items, *strategy_items],
        lambda item: _candidate_research_protocol_version(item) or item.get("research_protocol_version"),
    )
    candidate_contract_counts = _count_by(
        [*candidate_items, *strategy_items],
        lambda item: _candidate_contract_version(item) or item.get("candidate_contract_version"),
    )
    spec_completeness_counts = _count_by(
        [*candidate_items, *strategy_items],
        lambda item: _candidate_spec_completeness(item) or item.get("spec_completeness"),
    )
    return {
        "research_protocol_version_counts": research_protocol_counts,
        "candidate_contract_version_counts": candidate_contract_counts,
        "spec_completeness_counts": spec_completeness_counts,
    }


def _build_prediction_trace_summary(
    *,
    candidates: list[dict[str, Any]] | None = None,
    submit_result: dict[str, Any] | None = None,
) -> dict[str, Any]:
    candidate_items = [dict(item or {}) for item in list(candidates or []) if isinstance(item, dict)]
    strategy_items = [dict(item or {}) for item in list(dict(submit_result or {}).get("strategies") or []) if isinstance(item, dict)]
    trace_ids: list[str] = []
    for item in [*candidate_items, *strategy_items]:
        trace_id = _candidate_prediction_trace_id(item) or _string(item.get("prediction_trace_id") or item.get("trace_id"))
        if trace_id and trace_id not in trace_ids:
            trace_ids.append(trace_id)
    total = len(candidate_items) + len(strategy_items)
    return {
        "contract_version": PREDICTION_TRACE_CONTRACT_VERSION,
        "trace_count": len(trace_ids),
        "missing_count": max(total - len(trace_ids), 0),
        "sample_trace_ids": trace_ids[:12],
    }


def _default_prediction_trace_gate_decisions() -> dict[str, Any]:
    return {
        "execution_audit_gate_status": None,
        "promotion_ready": False,
        "hard_gate_passed": False,
        "failure_reasons": [],
    }


def _default_prediction_trace_node(*, source_mode: str = "summary_fallback") -> dict[str, Any]:
    return {
        "available": False,
        "source_mode": source_mode,
        "count": 0,
        "ids": [],
    }


def _normalize_prediction_trace_gate_decisions(value: Any) -> dict[str, Any]:
    if isinstance(value, list):
        return {
            **_default_prediction_trace_gate_decisions(),
            "failure_reasons": _compact_list(value, limit=16),
        }
    payload = dict(value or {})
    return {
        "execution_audit_gate_status": _string(payload.get("execution_audit_gate_status")) or None,
        "promotion_ready": bool(payload.get("promotion_ready")),
        "hard_gate_passed": bool(payload.get("hard_gate_passed")),
        "failure_reasons": _compact_list(payload.get("failure_reasons") or [], limit=16),
    }


def _normalize_prediction_trace_node(
    value: Any,
    *,
    default_source_mode: str = "summary_fallback",
) -> dict[str, Any]:
    payload = dict(value or {})
    count = _safe_int(
        payload.get("count")
        or payload.get("signal_evidence_count")
        or payload.get("order_count")
        or payload.get("trade_count")
        or payload.get("realized_trade_count")
        or payload.get("position_count")
        or payload.get("mapped_position_count")
        or payload.get("nav_row_count")
    )
    ids = _compact_list(
        payload.get("ids")
        or payload.get("recent_signal_ids")
        or payload.get("order_ids")
        or payload.get("trade_ids")
        or payload.get("position_ids")
        or [],
        limit=8,
    )
    result: dict[str, Any] = {
        "available": bool(payload.get("available")),
        "source_mode": _string(payload.get("source_mode")) or default_source_mode,
        "count": count,
        "ids": ids,
    }
    if _string(payload.get("status")):
        result["status"] = _string(payload.get("status"))
    if _string(payload.get("as_of")):
        result["as_of"] = _string(payload.get("as_of"))
    for key, raw in payload.items():
        if key in {"available", "source_mode", "count", "ids", "status", "as_of"}:
            continue
        if raw in (None, "", [], {}):
            continue
        if isinstance(raw, list):
            result[key] = _compact_list(raw, limit=16 if key == "failure_reasons" else 8)
        else:
            result[key] = raw
    return result


def _merge_count_maps(existing: Any, incoming: Any) -> dict[str, int]:
    result = {
        _string(key): _safe_int(value)
        for key, value in dict(existing or {}).items()
        if _string(key)
    }
    for key, value in dict(incoming or {}).items():
        token = _string(key)
        if not token:
            continue
        result[token] = result.get(token, 0) + _safe_int(value)
    return result


def _merge_prediction_trace_node(existing: Any, incoming: Any) -> dict[str, Any]:
    left = _normalize_prediction_trace_node(existing)
    right = _normalize_prediction_trace_node(incoming, default_source_mode=left.get("source_mode") or "summary_fallback")
    result = dict(left)
    result["available"] = bool(left.get("available")) or bool(right.get("available"))
    result["source_mode"] = (
        "entity_backed"
        if "entity_backed" in {_string(left.get("source_mode")), _string(right.get("source_mode"))}
        else _string(right.get("source_mode")) or _string(left.get("source_mode")) or "summary_fallback"
    )
    result["count"] = max(_safe_int(left.get("count")), _safe_int(right.get("count")))
    result["ids"] = _compact_list([*list(left.get("ids") or []), *list(right.get("ids") or [])], limit=8)
    if _string(right.get("status")):
        result["status"] = _string(right.get("status"))
    elif _string(left.get("status")):
        result["status"] = _string(left.get("status"))
    if _string(right.get("as_of")):
        result["as_of"] = _string(right.get("as_of"))
    elif _string(left.get("as_of")):
        result["as_of"] = _string(left.get("as_of"))

    for key in set(left.keys()) | set(right.keys()):
        if key in {"available", "source_mode", "count", "ids", "status", "as_of"}:
            continue
        left_value = left.get(key)
        right_value = right.get(key)
        if isinstance(left_value, dict) or isinstance(right_value, dict):
            result[key] = _merge_count_maps(left_value, right_value)
        elif isinstance(left_value, list) or isinstance(right_value, list):
            result[key] = _compact_list([*list(left_value or []), *list(right_value or [])], limit=16)
        elif right_value not in (None, "", [], {}):
            result[key] = right_value
        elif left_value not in (None, "", [], {}):
            result[key] = left_value
    return result


def _merge_prediction_trace_gate_decisions(existing: Any, incoming: Any) -> dict[str, Any]:
    left = _normalize_prediction_trace_gate_decisions(existing)
    right = _normalize_prediction_trace_gate_decisions(incoming)
    return {
        "execution_audit_gate_status": _string(right.get("execution_audit_gate_status"))
        or _string(left.get("execution_audit_gate_status"))
        or None,
        "promotion_ready": bool(left.get("promotion_ready")) or bool(right.get("promotion_ready")),
        "hard_gate_passed": bool(left.get("hard_gate_passed")) or bool(right.get("hard_gate_passed")),
        "failure_reasons": _compact_list(
            [*list(left.get("failure_reasons") or []), *list(right.get("failure_reasons") or [])],
            limit=16,
        ),
    }


def _prediction_trace_entry_template(trace_id: str | None) -> dict[str, Any]:
    return {
        "prediction_trace_id": trace_id or None,
        "source_count": 0,
        "artifact_ids": [],
        "retrieval_context_ids": [],
        "family_outcome_summary": {"family_counts": {}, "status_counts": {}, "submission_lane_counts": {}},
        "hypothesis_spec": _default_prediction_trace_node(),
        "signal_event": _default_prediction_trace_node(),
        "intended_order": _default_prediction_trace_node(),
        "actual_fill": _default_prediction_trace_node(),
        "position_round_trip": _default_prediction_trace_node(),
        "pnl_audit_summary": _default_prediction_trace_node(),
        "gate_decisions": _default_prediction_trace_gate_decisions(),
        "evidence_gap_codes": [],
    }


def _build_prediction_trace_ledger_fallback_entry(item: dict[str, Any]) -> dict[str, Any]:
    signal_quality = dict(item.get("signal_quality_snapshot") or item.get("signal_quality") or {})
    execution_quality = dict(item.get("execution_quality_snapshot") or item.get("execution_quality") or {})
    audit = dict(execution_quality.get("audit") or {})
    trace_id = _candidate_prediction_trace_id(item) or _string(item.get("prediction_trace_id") or item.get("trace_id")) or None
    research_protocol_version = _candidate_research_protocol_version(item) or None
    candidate_contract_version = _candidate_contract_version(item) or None
    dsl_signature = _string(item.get("dsl_signature") or _candidate_payload_value(item, "dsl_signature")) or None
    order_count = _safe_int(
        execution_quality.get("order_count")
        or execution_quality.get("filled_order_count")
        or execution_quality.get("trade_count")
    )
    trade_count = _safe_int(execution_quality.get("trade_count"))
    realized_trade_count = _safe_int(
        execution_quality.get("realized_trade_count")
        or audit.get("realized_trade_count")
    )
    mapped_position_count = _safe_int(
        execution_quality.get("mapped_position_count")
        or audit.get("mapped_position_count")
    )
    position_count = _safe_int(execution_quality.get("position_count"))
    closed_position_count = _safe_int(execution_quality.get("closed_position_count"))
    incomplete_position_count = _safe_int(
        execution_quality.get("incomplete_position_count")
        or audit.get("incomplete_position_count")
    )
    nav_row_count = _safe_int(execution_quality.get("nav_row_count"))
    realized_pnl_total = execution_quality.get("realized_pnl_total")
    if realized_pnl_total in (None, ""):
        realized_pnl_total = audit.get("realized_pnl_total")
    trade_expectancy = execution_quality.get("trade_expectancy")
    execution_audit_gate_status = _string(
        item.get("execution_audit_gate_status")
        or execution_quality.get("execution_audit_gate_status")
        or audit.get("execution_audit_gate_status")
    ) or None
    failure_reasons = _compact_list(
        list(dict(item.get("hard_gate_result") or {}).get("failure_reasons") or [])
        or list(dict(item.get("hard_gate_result") or {}).get("reasons") or [])
        or list(item.get("execution_audit_gate_reasons") or []),
        limit=16,
    )
    evidence_gap_codes = _compact_list(item.get("evidence_gap_codes") or execution_quality.get("evidence_gap_codes") or [], limit=16)
    result = {
        "prediction_trace_id": trace_id,
        "hypothesis_spec": {
            "available": bool(research_protocol_version or candidate_contract_version or dsl_signature),
            "source_mode": "summary_fallback",
            "count": 1 if (research_protocol_version or candidate_contract_version or dsl_signature) else 0,
            "research_protocol_version": research_protocol_version,
            "candidate_contract_version": candidate_contract_version,
            "dsl_signature": dsl_signature,
        },
        "signal_event": {
            "available": bool(signal_quality),
            "source_mode": "summary_fallback",
            "count": 1 if signal_quality else 0,
            "status": _string(signal_quality.get("status") or signal_quality.get("coverage_status")) or None,
        },
        "intended_order": {
            "available": order_count > 0,
            "source_mode": "summary_fallback",
            "count": order_count,
            "order_count": order_count,
            "paper_account_id": _string(item.get("paper_account_id") or item.get("incubation_account_id")) or None,
        },
        "actual_fill": {
            "available": trade_count > 0 or realized_trade_count > 0,
            "source_mode": "summary_fallback",
            "count": max(trade_count, realized_trade_count),
            "trade_count": trade_count,
            "realized_trade_count": realized_trade_count,
        },
        "position_round_trip": {
            "available": position_count > 0 or mapped_position_count > 0 or closed_position_count > 0,
            "source_mode": "summary_fallback",
            "count": max(position_count, mapped_position_count, closed_position_count),
            "position_count": position_count,
            "mapped_position_count": mapped_position_count,
            "closed_position_count": closed_position_count,
            "round_trip_close_rate": execution_quality.get("round_trip_close_rate"),
            "incomplete_position_count": incomplete_position_count,
        },
        "pnl_audit_summary": {
            "available": nav_row_count > 0 or realized_pnl_total not in (None, "") or trade_expectancy not in (None, ""),
            "source_mode": "summary_fallback",
            "count": max(nav_row_count, realized_trade_count),
            "nav_row_count": nav_row_count,
            "realized_pnl_total": realized_pnl_total,
            "trade_expectancy": trade_expectancy,
            "pnl_conversion_efficiency": execution_quality.get("pnl_conversion_efficiency"),
            "execution_conversion_efficiency": execution_quality.get("execution_conversion_efficiency"),
            "execution_audit_gate_status": execution_audit_gate_status,
        },
        "gate_decisions": {
            "execution_audit_gate_status": execution_audit_gate_status,
            "promotion_ready": bool(item.get("promotion_ready") or dict(item.get("hard_gate_result") or {}).get("promotion_ready")),
            "hard_gate_passed": bool(
                dict(item.get("hard_gate_result") or {}).get("passed")
                or item.get("execution_hard_gate_passed")
            ),
            "failure_reasons": failure_reasons,
        },
        "evidence_gap_codes": evidence_gap_codes,
    }
    if not trace_id:
        result["evidence_gap_codes"] = _compact_list([*result["evidence_gap_codes"], "missing_prediction_trace_id"], limit=16)
    if not bool(result["signal_event"]["available"]):
        result["evidence_gap_codes"] = _compact_list([*result["evidence_gap_codes"], "missing_signal_event"], limit=16)
    if not bool(result["intended_order"]["available"]):
        result["evidence_gap_codes"] = _compact_list([*result["evidence_gap_codes"], "missing_intended_order"], limit=16)
    if not bool(result["actual_fill"]["available"]):
        result["evidence_gap_codes"] = _compact_list([*result["evidence_gap_codes"], "missing_actual_fill"], limit=16)
    if not bool(result["position_round_trip"]["available"]):
        result["evidence_gap_codes"] = _compact_list([*result["evidence_gap_codes"], "missing_position_round_trip"], limit=16)
    if not bool(result["pnl_audit_summary"]["available"]):
        result["evidence_gap_codes"] = _compact_list([*result["evidence_gap_codes"], "missing_pnl_audit_summary"], limit=16)
    return result


def _build_prediction_trace_ledger(
    *,
    candidates: list[dict[str, Any]] | None = None,
    submit_result: dict[str, Any] | None = None,
) -> dict[str, Any]:
    candidate_items = [dict(item or {}) for item in list(candidates or []) if isinstance(item, dict)]
    strategy_items = [dict(item or {}) for item in list(dict(submit_result or {}).get("strategies") or []) if isinstance(item, dict)]
    all_items = [*candidate_items, *strategy_items]
    entries_by_trace: dict[str, dict[str, Any]] = {}
    missing_trace_count = 0
    for index, item in enumerate(all_items):
        trace_id = _candidate_prediction_trace_id(item) or _string(item.get("prediction_trace_id") or item.get("trace_id"))
        entry_key = trace_id or f"missing:{index}"
        if not trace_id:
            missing_trace_count += 1
        entry = entries_by_trace.setdefault(entry_key, _prediction_trace_entry_template(trace_id or None))
        entry["source_count"] = _safe_int(entry.get("source_count")) + 1
        for artifact_id in _candidate_artifact_ids(item):
            if artifact_id not in entry["artifact_ids"]:
                entry["artifact_ids"].append(artifact_id)
        for context_id in _candidate_retrieval_context_ids(item):
            if context_id not in entry["retrieval_context_ids"]:
                entry["retrieval_context_ids"].append(context_id)

        family_summary = _family_outcome_summary([item])
        for bucket_name in ("family_counts", "status_counts", "submission_lane_counts"):
            bucket = dict(entry["family_outcome_summary"].get(bucket_name) or {})
            for key, value in dict(family_summary.get(bucket_name) or {}).items():
                bucket[key] = bucket.get(key, 0) + _safe_int(value)
            entry["family_outcome_summary"][bucket_name] = bucket

        strategy_ledger = dict(item.get("prediction_trace_ledger") or {})
        incoming_entry = (
            {
                "prediction_trace_id": _string(strategy_ledger.get("prediction_trace_id")) or trace_id or None,
                "hypothesis_spec": strategy_ledger.get("hypothesis_spec"),
                "signal_event": strategy_ledger.get("signal_event"),
                "intended_order": strategy_ledger.get("intended_order"),
                "actual_fill": strategy_ledger.get("actual_fill"),
                "position_round_trip": strategy_ledger.get("position_round_trip"),
                "pnl_audit_summary": strategy_ledger.get("pnl_audit_summary"),
                "gate_decisions": strategy_ledger.get("gate_decisions"),
                "evidence_gap_codes": list(strategy_ledger.get("evidence_gap_codes") or []),
            }
            if strategy_ledger
            else _build_prediction_trace_ledger_fallback_entry(item)
        )
        if incoming_entry.get("prediction_trace_id") and not entry.get("prediction_trace_id"):
            entry["prediction_trace_id"] = incoming_entry.get("prediction_trace_id")
        for node_name in (
            "hypothesis_spec",
            "signal_event",
            "intended_order",
            "actual_fill",
            "position_round_trip",
            "pnl_audit_summary",
        ):
            entry[node_name] = _merge_prediction_trace_node(entry.get(node_name), incoming_entry.get(node_name))
        entry["gate_decisions"] = _merge_prediction_trace_gate_decisions(
            entry.get("gate_decisions"),
            incoming_entry.get("gate_decisions"),
        )
        entry["evidence_gap_codes"] = _compact_list(
            [*list(entry.get("evidence_gap_codes") or []), *list(incoming_entry.get("evidence_gap_codes") or [])],
            limit=16,
        )

    entries: list[dict[str, Any]] = []
    for entry in entries_by_trace.values():
        if not entry.get("prediction_trace_id") and "missing_prediction_trace_id" not in entry["evidence_gap_codes"]:
            entry["evidence_gap_codes"].append("missing_prediction_trace_id")
        if not bool(dict(entry.get("signal_event") or {}).get("available")):
            entry["evidence_gap_codes"].append("missing_signal_event")
        if not bool(dict(entry.get("intended_order") or {}).get("available")):
            entry["evidence_gap_codes"].append("missing_intended_order")
        if not bool(dict(entry.get("actual_fill") or {}).get("available")):
            entry["evidence_gap_codes"].append("missing_actual_fill")
        if not bool(dict(entry.get("position_round_trip") or {}).get("available")):
            entry["evidence_gap_codes"].append("missing_position_round_trip")
        if not bool(dict(entry.get("pnl_audit_summary") or {}).get("available")):
            entry["evidence_gap_codes"].append("missing_pnl_audit_summary")
        entry["evidence_gap_codes"] = _compact_list(entry.get("evidence_gap_codes") or [], limit=16)
        entry["artifact_ids"] = _compact_list(entry.get("artifact_ids") or [], limit=16)
        entry["retrieval_context_ids"] = _compact_list(entry.get("retrieval_context_ids") or [], limit=16)
        entry["gate_decisions"] = _normalize_prediction_trace_gate_decisions(entry.get("gate_decisions"))
        entries.append(entry)

    entries.sort(key=lambda item: (_string(item.get("prediction_trace_id")) == "", _string(item.get("prediction_trace_id"))))
    return {
        "contract_version": PREDICTION_TRACE_LEDGER_CONTRACT_VERSION,
        "trace_count": len(entries_by_trace),
        "missing_trace_count": missing_trace_count,
        "entries": entries[:24],
    }


def build_gate_artifact_v2(
    *,
    candidates: list[dict[str, Any]] | None = None,
    quality_gate_report: dict[str, Any] | None = None,
    backtest_report: dict[str, Any] | None = None,
    submit_result: dict[str, Any] | None = None,
) -> dict[str, Any]:
    gate_a = build_gate_a_artifact(
        candidates=candidates,
        quality_gate_report=quality_gate_report,
    )
    gate_b = build_gate_b_artifact(
        quality_gate_report=quality_gate_report,
        backtest_report=backtest_report,
        submit_result=submit_result,
    )
    gate_c = build_gate_c_artifact(
        submit_result=submit_result,
    )
    return {
        "contract_version": GATE_ARTIFACT_V2_CONTRACT_VERSION,
        "available": any(
            gate.get("status") not in {"pending", ""}
            for gate in (gate_a, gate_b, gate_c)
        ),
        "gate_a": gate_a,
        "gate_b": gate_b,
        "gate_c": gate_c,
        "legacy_gate_mapping": _legacy_gate_mapping(
            quality_gate_report=quality_gate_report,
            submit_result=submit_result,
        ),
        "protocol_versions": _build_protocol_version_summary(
            candidates=candidates,
            submit_result=submit_result,
        ),
        "prediction_trace_summary": _build_prediction_trace_summary(
            candidates=candidates,
            submit_result=submit_result,
        ),
        "prediction_trace_ledger": _build_prediction_trace_ledger(
            candidates=candidates,
            submit_result=submit_result,
        ),
    }

# --- governance_plane_contract_parts/models.py ---
def build_governance_plane_artifact(
    *,
    candidates: list[dict[str, Any]] | None = None,
    quality_gate_report: dict[str, Any] | None = None,
    backtest_report: dict[str, Any] | None = None,
    dedup_report: dict[str, Any] | None = None,
    submit_result: dict[str, Any] | None = None,
) -> dict[str, Any]:
    gate_artifact = build_gate_artifact(
        quality_gate_report=quality_gate_report,
        backtest_report=backtest_report,
    )
    dedup_artifact = build_dedup_artifact(
        dedup_report=dedup_report,
    )
    submission_artifact = build_submission_artifact(
        submit_result=submit_result,
    )
    evidence_artifact = build_governance_evidence_artifact(
        submit_result=submit_result,
    )
    gate_artifact_v2 = build_gate_artifact_v2(
        candidates=candidates,
        quality_gate_report=quality_gate_report,
        backtest_report=backtest_report,
        submit_result=submit_result,
    )
    available = any(
        bool(item.get("available"))
        for item in (
            gate_artifact,
            dedup_artifact,
            submission_artifact,
            evidence_artifact,
            gate_artifact_v2,
        )
    )
    source_chain: list[str] = []
    if gate_artifact.get("available"):
        source_chain.append("governance.gate_artifact")
    if dedup_artifact.get("available"):
        source_chain.append("governance.dedup_artifact")
    if submission_artifact.get("available"):
        source_chain.append("governance.submission_artifact")
    if evidence_artifact.get("available"):
        source_chain.append("governance.evidence_artifact")
    if gate_artifact_v2.get("available"):
        source_chain.append("governance.gate_artifact_v2")
    governance_plane_v2 = {
        "contract_version": GOVERNANCE_PLANE_V2_CONTRACT_VERSION,
        "available": bool(gate_artifact_v2.get("available")),
        "plane": "governance_v2",
        "gate_artifact_v2": gate_artifact_v2,
        "gate_a": dict(gate_artifact_v2.get("gate_a") or {}),
        "gate_b": dict(gate_artifact_v2.get("gate_b") or {}),
        "gate_c": dict(gate_artifact_v2.get("gate_c") or {}),
        "legacy_gate_mapping": dict(gate_artifact_v2.get("legacy_gate_mapping") or {}),
        "protocol_versions": dict(gate_artifact_v2.get("protocol_versions") or {}),
        "prediction_trace_summary": dict(gate_artifact_v2.get("prediction_trace_summary") or {}),
        "prediction_trace_ledger": dict(gate_artifact_v2.get("prediction_trace_ledger") or {}),
    }
    return {
        "contract_version": GOVERNANCE_PLANE_CONTRACT_VERSION,
        "available": available,
        "plane": "governance",
        "gate_artifact": gate_artifact,
        "gate_artifact_v2": gate_artifact_v2,
        "dedup_artifact": dedup_artifact,
        "submission_artifact": submission_artifact,
        "evidence_artifact": evidence_artifact,
        "governance_plane_v2": governance_plane_v2,
        "gate_a": dict(gate_artifact_v2.get("gate_a") or {}),
        "gate_b": dict(gate_artifact_v2.get("gate_b") or {}),
        "gate_c": dict(gate_artifact_v2.get("gate_c") or {}),
        "legacy_gate_mapping": dict(gate_artifact_v2.get("legacy_gate_mapping") or {}),
        "protocol_versions": dict(gate_artifact_v2.get("protocol_versions") or {}),
        "prediction_trace_summary": dict(gate_artifact_v2.get("prediction_trace_summary") or {}),
        "prediction_trace_ledger": dict(gate_artifact_v2.get("prediction_trace_ledger") or {}),
        "source_chain": source_chain,
    }


__all__ = [
    "DEDUP_ARTIFACT_CONTRACT_VERSION",
    "GATE_ARTIFACT_CONTRACT_VERSION",
    "GATE_ARTIFACT_V2_CONTRACT_VERSION",
    "GOVERNANCE_EVIDENCE_ARTIFACT_CONTRACT_VERSION",
    "GOVERNANCE_PLANE_CONTRACT_VERSION",
    "GOVERNANCE_PLANE_V2_CONTRACT_VERSION",
    "PREDICTION_TRACE_CONTRACT_VERSION",
    "PREDICTION_TRACE_LEDGER_CONTRACT_VERSION",
    "SUBMISSION_ARTIFACT_CONTRACT_VERSION",
    "build_gate_artifact_v2",
    "build_dedup_artifact",
    "build_gate_artifact",
    "build_governance_evidence_artifact",
    "build_governance_plane_artifact",
    "build_submission_artifact",
]

