"""Quality policy helpers for the factor mining factory.

The factory is intentionally conservative: a factor without enough observable
cross-section IC evidence must not become consumable by strategy generation.

P1-5 fix (诊断报告 §S19-F12 / §S21 quality_baseline):
  - 默认 strict 阈值不变(60 sample_dates / 80 avg_cross_section / 60 ic_history_rows)
  - 新增 AKSHARE_QUALITY_PROFILE 环境变量,值可选 strict / lite / minimum
  - lite 适用于 db 数据稀疏的早期开发期 (factor_ic_history < 5000 rows)
  - minimum 仅用于 smoke test,不应在生产使用
"""

from __future__ import annotations

import json
import math
import os
from typing import Any


def unmeasured_seed_reason(record: dict[str, Any]) -> str | None:
    """Reject legacy seed promotion labels without measured validation evidence.

    Daily sync historically wrote active/B/IC=0.02 constants. Keep those rows
    intact for audit, but never use their labels as measured factor evidence.
    """
    trace = record.get("generation_trace") or {}
    for _ in range(2):
        if not isinstance(trace, str):
            break
        try:
            trace = json.loads(trace)
        except (TypeError, ValueError):
            trace = {}
    is_seed = str(record.get("factor_id") or "").startswith(("rule_seed_", "free_seed_"))
    is_seed = is_seed or (isinstance(trace, dict) and trace.get("source") == "free_data_daily_sync")
    if not is_seed:
        return None
    summary = record.get("validation_summary") or {}
    evidence = summary.get("evidence_summary") if isinstance(summary, dict) else None
    if not isinstance(evidence, dict):
        return "seed_validation_evidence_missing"
    # Presence only: the existing quality thresholds remain authoritative.
    for field in ("sample_dates", "avg_cross_section_n", "ic_history_rows"):
        value = evidence.get(field)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
            return "seed_validation_evidence_missing"
    for field in ("rank_ic_mean", "rank_ic_ir"):
        value = evidence.get(field)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            return "seed_validation_evidence_missing"
    return None


# 默认严苛阈值 (生产标准)
_STRICT_THRESHOLDS: dict[str, float] = {
    "min_sample_dates": 60.0,
    "min_avg_cross_section_n": 80.0,
    "min_ic_history_rows": 60.0,
    "min_abs_rank_ic_mean": 0.025,
    "min_rank_ic_ir": 0.25,
    "min_positive_ratio": 0.52,
}

# Lite 阈值 (开发期 / 数据稀疏期可用,但不应 promote 到 live trading)
_LITE_THRESHOLDS: dict[str, float] = {
    "min_sample_dates": 30.0,
    "min_avg_cross_section_n": 30.0,
    "min_ic_history_rows": 20.0,
    "min_abs_rank_ic_mean": 0.015,
    "min_rank_ic_ir": 0.10,
    "min_positive_ratio": 0.50,
}

# Minimum 阈值 (仅用于 smoke test / unit test,不应 promote 任何因子)
_MINIMUM_THRESHOLDS: dict[str, float] = {
    "min_sample_dates": 10.0,
    "min_avg_cross_section_n": 10.0,
    "min_ic_history_rows": 5.0,
    "min_abs_rank_ic_mean": 0.005,
    "min_rank_ic_ir": 0.05,
    "min_positive_ratio": 0.45,
}


def _resolve_quality_profile() -> str:
    """Resolve quality profile from env: strict | lite | minimum.

    Default: strict (production standard).
    """
    raw = os.getenv("AKSHARE_QUALITY_PROFILE", "strict").strip().lower()
    if raw in ("strict", "lite", "minimum"):
        return raw
    return "strict"


def _resolve_thresholds(profile: str | None = None) -> dict[str, float]:
    profile = (profile or _resolve_quality_profile()).strip().lower()
    if profile == "lite":
        return dict(_LITE_THRESHOLDS)
    if profile == "minimum":
        return dict(_MINIMUM_THRESHOLDS)
    return dict(_STRICT_THRESHOLDS)


# Public alias kept for backward compatibility (callers reference this dict directly)
QUALITY_THRESHOLDS: dict[str, float] = _resolve_thresholds()
QUALITY_PROFILE_ACTIVE: str = _resolve_quality_profile()


PROMOTION_CRITERIA: dict[str, Any] = {
    "sample_dates": int(QUALITY_THRESHOLDS["min_sample_dates"]),
    "avg_cross_section_n": int(QUALITY_THRESHOLDS["min_avg_cross_section_n"]),
    "ic_history_rows": int(QUALITY_THRESHOLDS["min_ic_history_rows"]),
    "abs_rank_ic_mean": QUALITY_THRESHOLDS["min_abs_rank_ic_mean"],
    "rank_ic_ir": QUALITY_THRESHOLDS["min_rank_ic_ir"],
    "positive_ratio": QUALITY_THRESHOLDS["min_positive_ratio"],
    "lookahead_risk": "not_high",
}


def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def safe_int(value: Any, default: int = 0) -> int:
    try:
        if value is None:
            return default
        return int(value)
    except (TypeError, ValueError):
        return default


def _metric(result: dict[str, Any], key: str) -> Any:
    metrics = dict(result.get("metrics") or {})
    cross_section = dict(result.get("cross_section") or {})
    summary = dict(cross_section.get("summary") or {})
    coverage = dict(result.get("coverage") or {})
    return metrics.get(key, summary.get(key, coverage.get(key)))


def evaluate_validation_evidence(
    result: dict[str, Any] | None,
    *,
    require_persisted_history: bool = True,
    thresholds: dict[str, float] | None = None,
) -> dict[str, Any]:
    """Return pass/fail plus structured reasons for validation evidence."""
    thresholds = {**QUALITY_THRESHOLDS, **dict(thresholds or {})}
    if not result:
        return {
            "passed": False,
            "reasons": ["missing_validation_result"],
            "summary": {},
            "thresholds": thresholds,
        }

    metrics = dict(result.get("metrics") or {})
    cross_section = dict(result.get("cross_section") or {})
    summary = dict(cross_section.get("summary") or {})
    coverage = dict(result.get("coverage") or {})
    persisted_outputs = dict(result.get("persisted_outputs") or {})
    lookahead = dict(result.get("lookahead_audit") or {})

    sample_dates = max(
        safe_int(metrics.get("sample_dates")),
        safe_int(summary.get("sample_dates")),
    )
    avg_cross_section_n = safe_float(coverage.get("avg_cross_section_n"))
    if avg_cross_section_n <= 0 and cross_section.get("dates"):
        sizes = [
            safe_int(item.get("sample_size"))
            for item in list(cross_section.get("dates") or [])
        ]
        sizes = [item for item in sizes if item > 0]
        avg_cross_section_n = sum(sizes) / len(sizes) if sizes else 0.0

    rank_ic_mean = safe_float(metrics.get("rank_ic_mean", summary.get("rank_ic_mean")))
    raw_rank_ic_ir = safe_float(metrics.get("rank_ic_ir", summary.get("rank_ic_ir")))
    # Direction-agnostic IR for inverse/hedged expressions: magnitude matters for
    # signal quality; sign can be flipped by the consumer.
    rank_ic_ir = abs(raw_rank_ic_ir)
    positive_ratio = safe_float(metrics.get("positive_ratio", summary.get("positive_ratio")))
    # If the factor is inverse (negative mean IC), evaluate hit-rate on the flipped side.
    if rank_ic_mean < 0.0 and positive_ratio > 0.0:
        positive_ratio = max(positive_ratio, 1.0 - positive_ratio)
    # 晋升证据用 DB 累计 IC 历史行数(ic_history_rows_total),回退到本轮新增(ic_history_rows)。
    # 累计口径避免"单轮验证窗口产不出 min_ic_history_rows 行 → 永远进不了 active"的死锁。
    ic_history_rows = max(
        safe_int(persisted_outputs.get("ic_history_rows_total")),
        safe_int(persisted_outputs.get("ic_history_rows")),
    )
    lookahead_risk = str(lookahead.get("risk_level") or "unknown").lower()

    reasons: list[str] = []
    if sample_dates < thresholds["min_sample_dates"]:
        reasons.append("sample_dates_below_min")
    if avg_cross_section_n < thresholds["min_avg_cross_section_n"]:
        reasons.append("avg_cross_section_n_below_min")
    if abs(rank_ic_mean) < thresholds["min_abs_rank_ic_mean"]:
        reasons.append("rank_ic_mean_below_min")
    if rank_ic_ir < thresholds["min_rank_ic_ir"]:
        reasons.append("rank_ic_ir_below_min")
    if positive_ratio < thresholds["min_positive_ratio"]:
        reasons.append("positive_ratio_below_min")
    if require_persisted_history and ic_history_rows < thresholds["min_ic_history_rows"]:
        reasons.append("ic_history_rows_below_min")
    if lookahead_risk == "high":
        reasons.append("lookahead_risk_high")

    evidence_summary = {
        "sample_dates": sample_dates,
        "avg_cross_section_n": round(float(avg_cross_section_n), 4),
        "rank_ic_mean": round(rank_ic_mean, 6),
        "rank_ic_ir": round(rank_ic_ir, 6),
        "rank_ic_ir_raw": round(raw_rank_ic_ir, 6),
        "positive_ratio": round(positive_ratio, 6),
        "ic_history_rows": ic_history_rows,
        "lookahead_risk": lookahead_risk,
        "direction_normalized": True,
    }
    if metrics.get("available", summary.get("available")) is False:
        reason = str(metrics.get("reason") or summary.get("reason") or "no_measurable_rank_ic")
        reasons.append(reason)
        evidence_summary.update(
            available=False, reason=reason, rank_ic_mean=None,
            rank_ic_ir=None, rank_ic_ir_raw=None, positive_ratio=None,
        )
    return {
        "passed": not reasons,
        "reasons": reasons,
        "summary": evidence_summary,
        "thresholds": thresholds,
    }


def compute_quality_score(
    result: dict[str, Any] | None,
    *,
    structural_score: float = 0.0,
    novelty_score: float = 0.0,
) -> float:
    """Compute a bounded quality score for validated candidates."""
    evidence = evaluate_validation_evidence(result, require_persisted_history=False)
    summary = dict(evidence.get("summary") or {})
    rank_ic = abs(safe_float(summary.get("rank_ic_mean")))
    rank_ir = max(0.0, safe_float(summary.get("rank_ic_ir")))
    positive_ratio = safe_float(summary.get("positive_ratio"))
    sample_dates = safe_float(summary.get("sample_dates"))
    avg_n = safe_float(summary.get("avg_cross_section_n"))

    sample_score = min(sample_dates / QUALITY_THRESHOLDS["min_sample_dates"], 1.0)
    breadth_score = min(avg_n / QUALITY_THRESHOLDS["min_avg_cross_section_n"], 1.0)
    ic_score = min(rank_ic / 0.05, 1.0)
    ir_score = min(rank_ir / 0.75, 1.0)
    pos_score = min(max((positive_ratio - 0.5) / 0.12, 0.0), 1.0)
    structure_score = min(max(float(structural_score or 0.0) / 2.5, 0.0), 1.0)
    novelty = min(max(float(novelty_score or 0.0), 0.0), 1.0)

    score = (
        35.0 * ic_score
        + 20.0 * ir_score
        + 15.0 * pos_score
        + 10.0 * sample_score
        + 10.0 * breadth_score
        + 5.0 * structure_score
        + 5.0 * novelty
    )
    if not evidence.get("passed"):
        score *= 0.5
    return round(max(0.0, min(100.0, score)), 4)


def cap_rating_for_quality(
    rating: dict[str, Any],
    evidence: dict[str, Any],
    *,
    required_audits_available: bool,
) -> dict[str, Any]:
    """Cap A/B ratings when the validation evidence is incomplete."""
    capped = dict(rating or {})
    grade = str(capped.get("grade") or "D")
    reasons = list(evidence.get("reasons") or [])
    if not required_audits_available:
        reasons.append("required_audits_unavailable")

    if reasons and grade in {"A", "B"}:
        capped["grade"] = "C"
        capped["recommendation"] = "watch"
        governance = dict(capped.get("governance") or {})
        governance["governance_grade"] = "C"
        governance["governance_recommendation"] = "watch"
        governance["admission_blocked"] = True
        existing = list(governance.get("admission_block_reasons") or [])
        governance["admission_block_reasons"] = list(dict.fromkeys(existing + reasons))
        capped["governance"] = governance
    capped["quality_evidence"] = evidence
    return capped
