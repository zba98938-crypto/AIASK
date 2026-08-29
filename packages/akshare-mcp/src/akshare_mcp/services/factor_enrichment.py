"""Factor candidate enrichment service.

Computes lightweight heuristic scores for factor candidates:
- originality_score: similarity distance from existing factor pool
- complexity_score: AST depth / operator count of the expression
- crowding_proxy: estimated crowding risk based on factor category
- hypothesis_alignment: semantic alignment between hypothesis and expression

Usage::

    from akshare_mcp.services.factor_enrichment import build_factor_enrichment

    enrichment = build_factor_enrichment(
        expression="close / sma(close, 20) - 1",
        hypothesis="均线偏离度因子",
        existing_pool=["momentum_20d", "sma_ratio_60"],
        validation_result={"ic_mean": 0.03, "rank_ic_mean": 0.04},
    )
    result["factor_enrichment"] = enrichment.to_dict()
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any


# ── Complexity scoring ────────────────────────────────────────────────────────

_OPERATOR_PATTERN = re.compile(r"[+\-*/^%]|(?:sma|ema|std|rank|zscore|log|abs|max|min|corr|cov|delta|shift|ts_)\w*", re.IGNORECASE)
_FUNCTION_PATTERN = re.compile(r"\b(sma|ema|std|rank|zscore|log|abs|max|min|corr|cov|delta|shift|ts_\w+)\s*\(", re.IGNORECASE)

def _compute_complexity_score(expression: str) -> dict[str, Any]:
    """Compute complexity of a factor expression.

    Returns a dict with operator_count, function_count, nesting_depth,
    and a normalized score [0, 1] where higher = more complex.
    """
    if not expression:
        return {"score": 0.0, "operator_count": 0, "function_count": 0, "nesting_depth": 0, "band": "trivial"}

    operators = _OPERATOR_PATTERN.findall(expression)
    functions = _FUNCTION_PATTERN.findall(expression)
    # Estimate nesting depth by counting max parenthesis depth
    max_depth = 0
    depth = 0
    for ch in expression:
        if ch == "(":
            depth += 1
            max_depth = max(max_depth, depth)
        elif ch == ")":
            depth = max(0, depth - 1)

    raw_score = len(operators) * 0.3 + len(functions) * 0.5 + max_depth * 0.2
    # Normalize to [0, 1] with diminishing returns
    normalized = min(1.0, raw_score / 10.0)

    if normalized < 0.2:
        band = "simple"
    elif normalized < 0.5:
        band = "moderate"
    elif normalized < 0.8:
        band = "complex"
    else:
        band = "very_complex"

    return {
        "score": round(normalized, 3),
        "operator_count": len(operators),
        "function_count": len(functions),
        "nesting_depth": max_depth,
        "band": band,
    }


# ── Originality scoring ──────────────────────────────────────────────────────

def _tokenize_expression(expr: str) -> set[str]:
    """Extract meaningful tokens from a factor expression."""
    tokens = re.findall(r"[a-zA-Z_]\w*", expr.lower())
    # Remove common noise words
    noise = {"def", "return", "if", "else", "for", "in", "and", "or", "not", "none", "true", "false"}
    return {t for t in tokens if t not in noise and len(t) > 1}


def _compute_originality_score(expression: str, existing_pool: list[str] | None = None) -> dict[str, Any]:
    """Compute originality by measuring token-level distance from existing factors.

    Returns a dict with score [0, 1] where higher = more original.
    """
    if not expression:
        return {"score": 0.0, "similar_factors": [], "band": "unknown"}

    expr_tokens = _tokenize_expression(expression)
    if not expr_tokens:
        return {"score": 0.5, "similar_factors": [], "band": "moderate"}

    if not existing_pool:
        return {"score": 1.0, "similar_factors": [], "band": "novel"}

    # Compute Jaccard distance to each existing factor
    similarities: list[tuple[str, float]] = []
    for existing in existing_pool:
        existing_tokens = _tokenize_expression(existing)
        if not existing_tokens:
            continue
        intersection = len(expr_tokens & existing_tokens)
        union = len(expr_tokens | existing_tokens)
        jaccard = intersection / union if union > 0 else 0.0
        if jaccard > 0.3:
            similarities.append((existing, round(jaccard, 3)))

    similarities.sort(key=lambda x: -x[1])
    max_similarity = similarities[0][1] if similarities else 0.0
    originality = round(1.0 - max_similarity, 3)

    if originality >= 0.8:
        band = "novel"
    elif originality >= 0.5:
        band = "moderate"
    elif originality >= 0.3:
        band = "derivative"
    else:
        band = "duplicate_risk"

    return {
        "score": originality,
        "similar_factors": [{"name": n, "similarity": s} for n, s in similarities[:3]],
        "band": band,
    }


# ── Crowding proxy ───────────────────────────────────────────────────────────

_CROWDING_CATEGORY_RISK: dict[str, float] = {
    "momentum": 0.7,
    "value": 0.5,
    "quality": 0.3,
    "size": 0.6,
    "volatility": 0.5,
    "reversal": 0.6,
    "liquidity": 0.4,
    "growth": 0.4,
    "sentiment": 0.3,
    "alternative": 0.2,
    "custom": 0.2,
}


def _estimate_crowding_proxy(expression: str, category: str | None = None) -> dict[str, Any]:
    """Estimate crowding risk based on factor category and expression patterns.

    Returns a dict with risk_level [0, 1] and category.
    """
    # Infer category from expression if not provided
    expr_lower = (expression or "").lower()
    inferred_category = category or "custom"

    if not category:
        for cat in ("momentum", "reversal", "value", "quality", "size", "volatility", "liquidity", "growth", "sentiment"):
            if cat in expr_lower:
                inferred_category = cat
                break
        # Heuristic detection
        if any(kw in expr_lower for kw in ("return", "pct_change", "shift")):
            inferred_category = "momentum"
        elif any(kw in expr_lower for kw in ("pe", "pb", "ps", "ev", "earnings")):
            inferred_category = "value"

    risk = _CROWDING_CATEGORY_RISK.get(inferred_category, 0.3)

    if risk >= 0.6:
        band = "high"
    elif risk >= 0.4:
        band = "medium"
    else:
        band = "low"

    return {
        "risk_level": round(risk, 3),
        "inferred_category": inferred_category,
        "band": band,
        "note": f"基于因子类别 '{inferred_category}' 的市场拥挤度预估",
    }


# ── Hypothesis alignment ─────────────────────────────────────────────────────

def _compute_hypothesis_alignment(expression: str, hypothesis: str | None = None) -> dict[str, Any]:
    """Measure alignment between hypothesis text and expression tokens.

    Simple keyword overlap approach — production would use embeddings.
    """
    if not hypothesis or not expression:
        return {"score": None, "status": "not_provided"}

    expr_tokens = _tokenize_expression(expression)
    hyp_tokens = _tokenize_expression(hypothesis)

    if not expr_tokens or not hyp_tokens:
        return {"score": None, "status": "insufficient_tokens"}

    overlap = len(expr_tokens & hyp_tokens)
    alignment = round(overlap / max(len(hyp_tokens), 1), 3)

    return {
        "score": alignment,
        "status": "computed",
        "overlapping_concepts": sorted(expr_tokens & hyp_tokens),
    }


# ── Enrichment report ────────────────────────────────────────────────────────

@dataclass
class FactorEnrichmentReport:
    """Comprehensive enrichment report for a factor candidate."""

    originality: dict[str, Any]
    complexity: dict[str, Any]
    crowding_proxy: dict[str, Any]
    hypothesis_alignment: dict[str, Any]
    validation_summary: dict[str, Any]
    registry_status: str  # "unregistered" / "candidate" / "active" / "retired"
    decay_monitor_status: str  # "not_monitored" / "stable" / "decaying" / "decayed"

    def to_dict(self) -> dict[str, Any]:
        return {
            "originality": self.originality,
            "complexity": self.complexity,
            "crowding_proxy": self.crowding_proxy,
            "hypothesis_alignment": self.hypothesis_alignment,
            "validation_summary": self.validation_summary,
            "registry_status": self.registry_status,
            "decay_monitor_status": self.decay_monitor_status,
        }


def build_factor_enrichment(
    *,
    expression: str = "",
    hypothesis: str | None = None,
    existing_pool: list[str] | None = None,
    category: str | None = None,
    validation_result: dict[str, Any] | None = None,
    registry_status: str = "unregistered",
    decay_monitor_status: str = "not_monitored",
) -> FactorEnrichmentReport:
    """Build a comprehensive factor enrichment report."""

    # Validation summary
    vr = validation_result or {}
    validation_summary: dict[str, Any] = {
        "ic_mean": vr.get("ic_mean"),
        "rank_ic_mean": vr.get("rank_ic_mean"),
        "ic_ir": vr.get("ic_ir"),
        "turnover": vr.get("turnover"),
        "oos_sharpe": vr.get("oos_sharpe"),
        "composite_rating": vr.get("composite_rating") or vr.get("rating"),
        "passed": vr.get("passed", vr.get("success")),
    }

    return FactorEnrichmentReport(
        originality=_compute_originality_score(expression, existing_pool),
        complexity=_compute_complexity_score(expression),
        crowding_proxy=_estimate_crowding_proxy(expression, category),
        hypothesis_alignment=_compute_hypothesis_alignment(expression, hypothesis),
        validation_summary=validation_summary,
        registry_status=registry_status,
        decay_monitor_status=decay_monitor_status,
    )
