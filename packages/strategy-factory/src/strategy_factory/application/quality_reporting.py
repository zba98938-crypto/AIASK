"""Shared quality-gate normalization and reporting facade.

These helpers live in the strategy_factory service layer so both the
factory pipeline and strategy_manager lifecycle can share the same gate
report contract without creating reverse dependencies on manager helpers.
"""

from ._quality_reporting_evaluation import build_quality_report
from ._quality_reporting_normalizers import (
    PROVISIONAL_TECHNICAL_STRATEGY_TYPES,
    has_degenerate_validation_statistics,
    has_only_statistical_gate_failures,
    is_factory_ai_prototype_strategy,
    is_factory_provisional_candidate,
    is_provisional_technical_strategy,
    normalize_quality_gate_result,
    optional_metric_value,
    quality_gate_reason_code,
    safe_metric_value,
)
from ._quality_reporting_policy import (
    PROVISIONAL_MIN_STATISTICAL_CHECKS_PASSED,
    maybe_grant_provisional_incubation,
)

__all__ = [
    "PROVISIONAL_MIN_STATISTICAL_CHECKS_PASSED",
    "PROVISIONAL_TECHNICAL_STRATEGY_TYPES",
    "build_quality_report",
    "has_degenerate_validation_statistics",
    "has_only_statistical_gate_failures",
    "is_factory_ai_prototype_strategy",
    "is_factory_provisional_candidate",
    "is_provisional_technical_strategy",
    "maybe_grant_provisional_incubation",
    "normalize_quality_gate_result",
    "optional_metric_value",
    "quality_gate_reason_code",
    "safe_metric_value",
]
