"""Target basket resolution from theme impacts (PR-3 + PR-D / Phase 2).

Implements Layer C: given a ThemeImpact, resolve which stocks to target
based on theme exposure scores, with industry diversification.

PR-D (2026-05-24): the local ``resolve_target_count`` formula was a
duplicate of the canonical implementation in
``strategy_factory.domain.target_count_resolver``. The canonical
implementation is now re-exported (alias) so all event task generators
hit the same formula. ``resolve_target_basket`` defaults to
``task_source="event_driven"`` to align with PR-C unified semantics.

Usage:
    basket = await resolve_target_basket(db, impact)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .theme_graph import ThemeImpact

# PR-D: single source of truth for target count formula. Re-exporting the
# canonical resolver keeps existing call sites
# (``from .target_basket import resolve_target_count``) working without
# duplicating the math.
from ...domain.target_count_resolver import (
    resolve_target_count as _domain_resolve_target_count,
)


def resolve_target_count(
    *,
    confidence: float,
    intensity: float,
    theme_breadth: str,
    task_source: str,
    feature_flag_target_max: int = 12,
) -> int:
    """Backwards-compatible alias for the canonical target count resolver.

    Forwards every keyword argument to
    ``strategy_factory.domain.target_count_resolver.resolve_target_count``
    so legacy callers (``apply_industry_diversification``,
    ``resolve_target_basket`` itself, ``test_theme_graph_propagation``)
    continue to work without code changes.
    """

    return _domain_resolve_target_count(
        confidence=confidence,
        intensity=intensity,
        theme_breadth=theme_breadth,
        task_source=task_source,
        feature_flag_target_max=feature_flag_target_max,
    )


@dataclass
class TargetBasket:
    """Resolved stock basket for a theme impact."""

    theme_code: str
    symbols: list[str] = field(default_factory=list)
    weights: dict[str, float] = field(default_factory=dict)
    evidence: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "theme_code": self.theme_code,
            "symbols": self.symbols,
            "symbol_count": len(self.symbols),
            "weights": self.weights,
            "evidence": self.evidence,
        }


def apply_industry_diversification(
    rows: list[dict[str, Any]],
    *,
    max_per_industry: int = 3,
    target_count: int = 30,
) -> list[dict[str, Any]]:
    """Select stocks with industry diversification constraint.

    Args:
        rows: Sorted by exposure_score descending.
        max_per_industry: Max stocks per industry.
        target_count: Total target count.

    Returns:
        Diversified subset of rows.
    """
    selected: list[dict[str, Any]] = []
    industry_counts: dict[str, int] = {}

    for row in rows:
        if len(selected) >= target_count:
            break
        industry = str(row.get("industry") or row.get("industry_tag") or "unknown").strip()
        current = industry_counts.get(industry, 0)
        if current >= max_per_industry:
            continue
        selected.append(row)
        industry_counts[industry] = current + 1

    return selected


async def resolve_target_basket(
    db: Any,
    impact: ThemeImpact,
    *,
    target_count: int | None = None,
    max_per_industry: int = 3,
    min_exposure: float = 0.3,
    feature_flag_target_max: int = 12,
) -> TargetBasket:
    """Resolve target stock basket for a theme impact.

    Args:
        db: Database adapter with `list_theme_exposure` or equivalent.
        impact: ThemeImpact from propagation.
        target_count: Override target count (if None, computed dynamically).
        max_per_industry: Industry diversification limit.
        min_exposure: Minimum exposure score threshold.
        feature_flag_target_max: Max target count allowed by feature flag.

    Returns:
        TargetBasket with selected symbols.
    """
    # Compute dynamic target count if not overridden.
    # PR-D: use canonical "event_driven" instead of legacy "manual_event"
    # (the resolver still recognizes both for backwards compatibility).
    if target_count is None:
        target_count = resolve_target_count(
            confidence=impact.confidence,
            intensity=impact.magnitude,
            theme_breadth=impact.breadth,
            task_source="event_driven",
            feature_flag_target_max=feature_flag_target_max,
        )

    # Load exposure data
    rows: list[dict[str, Any]] = []
    fallback_used = False
    fallback_reason: str | None = None
    if hasattr(db, "list_theme_exposure"):
        try:
            rows = await db.list_theme_exposure(
                theme_code=impact.theme_code,
                min_exposure=min_exposure,
                limit=50,
            )
        except Exception as exc:
            rows = []
            fallback_reason = f"list_theme_exposure_failed: {exc}"

    if not rows:
        # PR-D: surface "为什么空" 让上游 (preview / lineage) 可以解释。
        # Phase 6 会接入 concept-block fallback；Phase 2 仅记录原因。
        return TargetBasket(
            theme_code=impact.theme_code,
            symbols=[],
            evidence={
                "reason": "no_exposure_data",
                "theme_code": impact.theme_code,
                "min_exposure": min_exposure,
                "fallback": True,
                "fallback_reason": fallback_reason or "exposure_table_empty",
                "exposure_rule_version": "v1",
                "target_count_resolved": target_count,
                "direction_sign": impact.direction_sign,
                "magnitude": impact.magnitude,
                "confidence": impact.confidence,
            },
        )

    # For negative impacts, filter out stocks already down significantly
    if impact.direction_sign < 0:
        rows = [r for r in rows if float(r.get("return_20d") or 0) > -0.15]

    # Apply industry diversification
    selected = apply_industry_diversification(
        rows,
        max_per_industry=max_per_industry,
        target_count=target_count,
    )

    symbols = [str(r.get("symbol") or "").strip() for r in selected if r.get("symbol")]

    # Compute exposure-weighted weights
    total_exposure = sum(float(r.get("exposure_score") or 0) for r in selected)
    weights = {}
    if total_exposure > 0:
        for r in selected:
            sym = str(r.get("symbol") or "").strip()
            if sym:
                weights[sym] = round(float(r.get("exposure_score") or 0) / total_exposure, 4)

    return TargetBasket(
        theme_code=impact.theme_code,
        symbols=symbols[:target_count],
        weights=weights,
        evidence={
            "exposure_rule_version": "v1",
            "target_count_resolved": target_count,
            "avg_exposure_score": round(
                sum(float(r.get("exposure_score") or 0) for r in selected) / max(len(selected), 1),
                4,
            ),
            "industry_count": len(set(
                str(r.get("industry") or "unknown") for r in selected
            )),
            "direction_sign": impact.direction_sign,
            "magnitude": impact.magnitude,
            "confidence": impact.confidence,
        },
    )


__all__ = [
    "TargetBasket",
    "apply_industry_diversification",
    "resolve_target_basket",
    "resolve_target_count",
]
