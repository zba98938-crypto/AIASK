"""Small retrieval-quality summaries for vector and text tools."""

from __future__ import annotations

import math
from statistics import mean, pstdev
from typing import Any, Iterable


def _to_float(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def summarize_ranked_results(
    results: Iterable[dict[str, Any]],
    *,
    score_key: str = "score",
    backend_requested: str | None = None,
    backend_used: str | None = None,
    fallback_used: bool = False,
    fallback_reason: str | None = None,
) -> dict[str, Any]:
    items = [dict(item) for item in list(results or []) if isinstance(item, dict)]
    scores = [_to_float(item.get(score_key)) for item in items]
    valid_scores = [score for score in scores if score is not None]
    codes = [str(item.get("code") or "").strip() for item in items if item.get("code")]

    quality_flags: list[str] = []
    if not items:
        quality_flags.append("empty_results")
    if fallback_used:
        quality_flags.append("fallback")
    if valid_scores and max(valid_scores) - min(valid_scores) < 1e-6:
        quality_flags.append("flat_scores")

    return {
        "result_count": len(items),
        "unique_codes": len(set(codes)),
        "score_key": score_key,
        "score_mean": round(mean(valid_scores), 6) if valid_scores else None,
        "score_std": round(pstdev(valid_scores), 6) if len(valid_scores) >= 2 else 0.0 if valid_scores else None,
        "score_min": round(min(valid_scores), 6) if valid_scores else None,
        "score_max": round(max(valid_scores), 6) if valid_scores else None,
        "top_score": round(valid_scores[0], 6) if valid_scores else None,
        "backend_requested": backend_requested,
        "backend_used": backend_used,
        "fallback_used": bool(fallback_used),
        "fallback_reason": fallback_reason,
        "quality_flags": quality_flags,
    }
