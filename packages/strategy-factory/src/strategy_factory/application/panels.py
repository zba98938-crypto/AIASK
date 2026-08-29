"""策略工厂面板、验证与风险报告的稳定门面。"""

from ._panels_evaluation import (
    _build_family_returns,
    _build_strategy_panels,
    _run_risk_report,
    _run_validation_report,
)
from ._panels_normalizers import (
    _annualized_sharpe_ratio,
    _build_validation_focus_annotation,
    _generate_strategy_signal_series,
    _grade_for_total_score,
    _resolve_execution_semantic_status,
    _resolve_validation_focus,
    _reverse_threshold_score,
    _signal_series_from_events,
    _signal_series_from_masks,
    _threshold_score,
)
from ._panels_policy import _apply_trade_quality_rating_adjustment

__all__ = [
    "_annualized_sharpe_ratio",
    "_apply_trade_quality_rating_adjustment",
    "_build_family_returns",
    "_build_strategy_panels",
    "_build_validation_focus_annotation",
    "_generate_strategy_signal_series",
    "_grade_for_total_score",
    "_resolve_execution_semantic_status",
    "_resolve_validation_focus",
    "_reverse_threshold_score",
    "_run_risk_report",
    "_run_validation_report",
    "_signal_series_from_events",
    "_signal_series_from_masks",
    "_threshold_score",
]
