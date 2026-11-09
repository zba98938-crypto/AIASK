"""Versioned, persisted measurements of each factor-mining funnel stage."""

from __future__ import annotations

from typing import Any


def _get(candidate: Any, key: str, default: Any = None) -> Any:
    return candidate.get(key, default) if isinstance(candidate, dict) else getattr(candidate, key, default)


def _identity(candidate: Any) -> dict[str, Any]:
    return {
        "name": str(_get(candidate, "name", "") or ""),
        "generation_engine": str(_get(candidate, "generation_engine", "") or "unknown"),
        "blueprint_id": str(_get(candidate, "blueprint_id", "") or "none"),
        "family": str(_get(candidate, "factor_family") or _get(candidate, "family", "") or ""),
        "expression_dsl": str(_get(candidate, "expression_dsl", "") or ""),
        "generation_trace": dict(_get(candidate, "generation_trace") or {}),
    }


def pool_admission_decision(candidate: Any, *, strict_passed: bool) -> dict[str, Any]:
    """Return the actual pool decision; absence is never inferred from a name."""
    trace = dict(_get(candidate, "generation_trace") or {})
    decision = dict(trace.get("pool_admission") or {})
    if decision:
        return decision
    return {"attempted": None if strict_passed else False,
            "status": "unknown_legacy" if strict_passed else "not_attempted",
            "admitted": False,
            "reason": "pool_admission_decision_missing" if strict_passed else None}


def build_quality_summary(raw, evolved, quick_passed, validated, admitted, context) -> dict[str, Any]:
    """Count actual stage inputs/outputs; retain the decisions used by learning."""
    by_engine: dict[str, dict[str, int]] = {}
    by_blueprint: dict[str, dict[str, int]] = {}
    fields = ("raw", "evolved", "quick_passed", "validated", "accepted", "promoted")

    def count(candidate, field):
        identity = _identity(candidate)
        for groups, key in ((by_engine, identity["generation_engine"]), (by_blueprint, identity["blueprint_id"])):
            groups.setdefault(key, dict.fromkeys(fields, 0))[field] += 1

    for field, candidates in (("raw", raw), ("evolved", evolved), ("quick_passed", quick_passed), ("validated", validated)):
        for candidate in candidates:
            count(candidate, field)
    active_count = quarantine_count = 0
    for item in admitted:
        record = dict(item.get("record") or {})
        record["blueprint_id"] = (record.get("generation_trace") or {}).get("blueprint_id", "none")
        count(record, "accepted")
        status = record.get("status")
        active = status == "active" or (not status and bool(item.get("promoted_active")))
        quarantine = status == "quarantine" or (not status and bool(item.get("quarantined")))
        active_count += int(active)
        quarantine_count += int(quarantine)
        if active:
            count(record, "promoted")

    quick_ids = {id(c) for c in quick_passed}
    validated_ids = {id(c) for c in validated}
    quick_rows, strict_rows = [], []
    reasons: dict[str, int] = {}
    pool_reasons: dict[str, int] = {}
    diagnostic_counts: dict[str, int] = {}
    for candidate in evolved:
        identity = _identity(candidate)
        quick = dict(_get(candidate, "quick_evidence") or {})
        if quick:
            quick_rows.append({
                **identity, "passed": id(candidate) in quick_ids,
                "fail_reasons": list(quick.get("fail_reasons") or []),
                "quality_score": quick.get("quality_score"),
                "evidence_summary": dict(quick.get("evidence_summary") or {}),
            })
            if id(candidate) not in quick_ids:
                for reason in quick.get("fail_reasons") or ["quick_evidence_failed"]:
                    reasons[str(reason)] = reasons.get(str(reason), 0) + 1
        for key, value in dict(quick.get("diagnostic_counts") or {}).items():
            diagnostic_counts[str(key)] = diagnostic_counts.get(str(key), 0) + int(value or 0)
        result = dict(_get(candidate, "validation_result") or {})
        trace = identity["generation_trace"]
        attempted = bool(trace.get("strict_validation_attempted") or result or id(candidate) in validated_ids)
        if not attempted:
            continue
        strict_passed = id(candidate) in validated_ids
        rating = dict(result.get("rating") or {})
        governance = dict(rating.get("governance") or {})
        evidence = dict(rating.get("quality_evidence") or {})
        blockers = [] if strict_passed else list(governance.get("admission_block_reasons") or []) + list(evidence.get("reasons") or [])
        if not strict_passed and not blockers:
            blockers = ["strict_validation_failed" if result.get("success") is False else "strict_gate_failed"]
        pool_decision = pool_admission_decision(candidate, strict_passed=strict_passed)
        pool_reason = pool_decision.get("reason") if not pool_decision.get("admitted") else None
        if pool_reason:
            blockers.append(str(pool_reason))
            pool_reasons[str(pool_reason)] = pool_reasons.get(str(pool_reason), 0) + 1
        for reason in blockers:
            reasons[str(reason)] = reasons.get(str(reason), 0) + 1
        strict_rows.append({
            **identity,
            "quick_evidence": quick,
            "validation_result": result,
            "admission_decision": {
                "strict_gate_passed": strict_passed,
                "admitted": bool(pool_decision.get("admitted")),
                "pool_admission": pool_decision,
                "rating_grade": rating.get("grade"),
                "evidence_reasons": list(evidence.get("reasons") or []),
                "blockers": blockers,
            },
        })

    strict_attempted = len(strict_rows)
    return {
        "contract_version": "factor_mining.quality.v2",
        "engine_runs": {key: dict(value) for key, value in dict(getattr(context, "engine_runs", {}) or {}).items()},
        "raw_candidate_count": len(raw),
        "evolved_count": len(evolved),
        "quick_evaluated_count": len(quick_rows),
        "quick_passed_count": len(quick_passed),
        "evidence_passed_count": len(validated),
        "quarantine_count": quarantine_count,
        "active_promoted_count": active_count,
        "quality_funnel": {
            "raw": len(raw), "generated": len(raw), "evolved": len(evolved),
            "quick_evaluated": len(quick_rows), "quick_passed": len(quick_passed),
            "strict_attempted": strict_attempted,
            "strict_deferred": max(0, len(quick_passed) - strict_attempted),
            "validated": len(validated), "strict_validated": len(validated),
            "admitted": len(admitted), "promoted": active_count, "quarantine": quarantine_count,
        },
        "rejection_counts": {
            "evolution": max(0, len(raw) - len(evolved)),
            "quick_evidence": max(0, len(evolved) - len(quick_passed)),
            "validation": max(0, strict_attempted - len(validated)),
            "admission": max(0, len(validated) - len(admitted)),
        },
        "reject_reasons": reasons,
        "pool_reject_reasons": pool_reasons,
        "diagnostic_counts": diagnostic_counts,
        "by_engine": by_engine,
        "by_blueprint": by_blueprint,
        "strict_candidate_results": strict_rows,
        "quick_candidate_results": quick_rows,
        "validation_universe_health": getattr(context, "validation_universe_health", {}),
    }
