"""Trade-quality policy adjustments for strategy validation panels."""

from __future__ import annotations

from typing import List

import numpy as np

from ._panels_normalizers import (
    _annualized_sharpe_ratio,
    _grade_for_total_score,
    _resolve_execution_semantic_status,
    _resolve_validation_focus,
    _resolve_validation_focus_layer,
    _reverse_threshold_score,
    _threshold_score,
)


def _apply_trade_quality_rating_adjustment(
    report: dict,
    *,
    strategy_type: str,
    params: dict,
    strategy_returns: np.ndarray | None,
    sample_codes: List[str],
    sample_selection: dict | None = None,
) -> dict:
    rating = dict(report.get("rating") or {})
    if not rating:
        return report
    normalized_strategy_type = str(strategy_type or "").strip().lower()
    if normalized_strategy_type not in {"momentum", "quality_factor", "ma_cross"}:
        return report
    validation_focus = _resolve_validation_focus(params)
    sample_meta = dict(sample_selection or {})
    validation_focus_layer = str(
        sample_meta.get("validation_focus_layer")
        or _resolve_validation_focus_layer(validation_focus)
        or "broad_market"
    ).strip().lower()
    sample_selection_mode = str(sample_meta.get("sample_selection_mode") or "").strip().lower()
    if validation_focus_layer not in {"target_only", "family_peer"}:
        return report
    baseline_total = float(rating.get("total_score") or 0.0)
    baseline_grade = str(rating.get("grade") or "").strip().upper() or _grade_for_total_score(baseline_total)
    returns = np.asarray(strategy_returns if strategy_returns is not None else [], dtype=np.float64)
    returns = returns[np.isfinite(returns)]
    if returns.size < 60:
        return report

    realized_sharpe = _annualized_sharpe_ratio(returns)
    positive_ratio = float(np.mean(returns > 0.0)) if returns.size else 0.0
    multiple_testing = dict(report.get("multiple_testing") or {})
    dsr = float(dict(multiple_testing.get("deflated_sharpe") or {}).get("dsr") or 0.0)
    pbo = float(dict(multiple_testing.get("pbo") or {}).get("pbo") or 1.0)
    rc_pvalue = float(dict(multiple_testing.get("white_reality_check") or {}).get("p_value") or 1.0)
    spa_pvalue = float(dict(multiple_testing.get("hansen_spa") or {}).get("p_value") or 1.0)
    holding_horizon = dict(params.get("holding_horizon") or {})
    rebalance_rule = dict(params.get("rebalance_rule") or {})
    market_regime_assumption = dict(params.get("market_regime_assumption") or {})
    family_specialization = dict(
        params.get("family_specialization")
        or params.get("family_specific_hypothesis")
        or {}
    )
    execution_semantic_status = _resolve_execution_semantic_status(normalized_strategy_type, params)
    execution_semantic_ready = bool(execution_semantic_status.get("execution_semantic_ready"))
    expected_turnover_band = str(
        params.get("expected_turnover_band")
        or holding_horizon.get("expected_turnover_band")
        or rebalance_rule.get("expected_turnover_band")
        or ""
    ).strip().lower()
    rebalance_frequency_days = int(rebalance_rule.get("frequency_days") or 0)
    max_holding_days = int(holding_horizon.get("max_days") or 0)
    aligned_dynamic_panel = sample_selection_mode in {
        "target_plus_dynamic_family_peer",
        "family_peer_dynamic_panel",
    }

    bonus_breakdown: dict[str, float] = {}
    trade_score_breakdown: dict[str, float] = {}
    if normalized_strategy_type == "momentum":
        trade_score_breakdown["realized_sharpe"] = _threshold_score(
            realized_sharpe,
            [(1.5, 24.0), (1.15, 20.0), (0.9, 16.0), (0.65, 12.0)],
        )
        trade_score_breakdown["positive_ratio"] = _threshold_score(
            positive_ratio,
            [(0.56, 8.0), (0.53, 6.0), (0.5, 4.0)],
        )
        trade_score_breakdown["deflated_sharpe"] = _threshold_score(
            dsr,
            [(0.1, 10.0), (0.05, 6.0), (0.0, 2.0)],
        )
        trade_score_breakdown["pbo"] = _reverse_threshold_score(
            pbo,
            [(0.4, 10.0), (0.6, 8.0), (0.8, 5.0), (0.9, 3.0)],
        )
        trade_score_breakdown["multiple_testing_consensus"] = (
            8.0 if rc_pvalue <= 0.2 and spa_pvalue <= 0.2
            else 5.0 if rc_pvalue <= 0.35 and spa_pvalue <= 0.35
            else 2.0 if rc_pvalue <= 0.5 and spa_pvalue <= 0.5
            else 0.0
        )
        trade_score_breakdown["trend_persistence_holding"] = (
            7.0 if max_holding_days >= 36
            else 5.0 if max_holding_days >= 24
            else 2.0 if max_holding_days >= 18
            else 0.0
        )
        trade_score_breakdown["rebalance_pacing"] = (
            5.0 if rebalance_frequency_days >= 14
            else 4.0 if rebalance_frequency_days >= 10
            else 2.0 if rebalance_frequency_days >= 7
            else 0.0
        )
        trade_score_breakdown["turnover_discipline"] = (
            4.0 if expected_turnover_band == "low"
            else 2.0 if expected_turnover_band == "medium"
            else 0.0
        )
        trade_score_breakdown["aligned_dynamic_panel"] = 5.0 if aligned_dynamic_panel else 0.0
        trade_score_breakdown["regime_alignment"] = (
            6.0 if "trend_expansion" in str(market_regime_assumption.get("preferred_regime") or "")
            else 0.0
        )
        trade_score_breakdown["false_breakout_filter"] = 6.0 if family_specialization.get("false_breakout_filter") else 0.0
        trade_score_breakdown["target_cohort_consensus"] = (
            10.0
            if validation_focus_layer == "target_only"
            and aligned_dynamic_panel
            and pbo <= 0.2
            and rc_pvalue <= 0.35
            and spa_pvalue <= 0.35
            else 7.0
            if validation_focus_layer == "target_only"
            and aligned_dynamic_panel
            and pbo <= 0.35
            and rc_pvalue <= 0.5
            and spa_pvalue <= 0.5
            else 4.0
            if validation_focus_layer == "target_only"
            and aligned_dynamic_panel
            and pbo <= 0.5
            and rc_pvalue <= 0.65
            and spa_pvalue <= 0.65
            else 0.0
        )
        trade_score_breakdown["target_cohort_trade_alignment"] = (
            6.0
            if validation_focus_layer == "target_only"
            and aligned_dynamic_panel
            and max_holding_days >= 20
            and rebalance_frequency_days >= 14
            and "trend_expansion" in str(market_regime_assumption.get("preferred_regime") or "")
            and bool(family_specialization.get("false_breakout_filter"))
            else 0.0
        )
        trade_score_breakdown["target_cohort_low_turnover_consensus"] = (
            1.0
            if validation_focus_layer == "target_only"
            and aligned_dynamic_panel
            and pbo <= 0.25
            and expected_turnover_band == "low"
            and max_holding_days >= 20
            and rebalance_frequency_days >= 14
            else 0.0
        )
        if realized_sharpe >= 1.0:
            bonus_breakdown["realized_sharpe"] = 7.0
        elif realized_sharpe >= 0.7:
            bonus_breakdown["realized_sharpe"] = 4.0
        if positive_ratio >= 0.53:
            bonus_breakdown["positive_ratio"] = 2.0
        elif positive_ratio >= 0.51:
            bonus_breakdown["positive_ratio"] = 1.0
        if dsr >= 0.12:
            bonus_breakdown["deflated_sharpe"] = 4.0
        elif dsr >= 0.06:
            bonus_breakdown["deflated_sharpe"] = 2.0
        if pbo <= 0.75:
            bonus_breakdown["pbo"] = 3.0
        elif pbo <= 0.88:
            bonus_breakdown["pbo"] = 1.5
        if rc_pvalue <= 0.25 and spa_pvalue <= 0.2:
            bonus_breakdown["multiple_testing_consensus"] = 2.0
        if max_holding_days >= 20:
            bonus_breakdown["trend_persistence_holding"] = 1.5
        if rebalance_frequency_days >= 6:
            bonus_breakdown["rebalance_pacing"] = 1.0
        if expected_turnover_band == "low":
            bonus_breakdown["turnover_discipline"] = 1.5
        elif expected_turnover_band == "medium":
            bonus_breakdown["turnover_discipline"] = 0.5
        if aligned_dynamic_panel:
            bonus_breakdown["aligned_dynamic_panel"] = 1.0
        if "trend_expansion" in str(market_regime_assumption.get("preferred_regime") or ""):
            bonus_breakdown["regime_alignment"] = 1.5
        if family_specialization.get("false_breakout_filter"):
            bonus_breakdown["false_breakout_filter"] = 1.5
        if (
            validation_focus_layer == "target_only"
            and aligned_dynamic_panel
            and pbo <= 0.35
            and rc_pvalue <= 0.5
            and spa_pvalue <= 0.5
        ):
            bonus_breakdown["target_cohort_consensus"] = 1.5
        if (
            validation_focus_layer == "target_only"
            and aligned_dynamic_panel
            and max_holding_days >= 20
            and rebalance_frequency_days >= 14
            and "trend_expansion" in str(market_regime_assumption.get("preferred_regime") or "")
            and bool(family_specialization.get("false_breakout_filter"))
        ):
            bonus_breakdown["target_cohort_trade_alignment"] = 1.0
    elif normalized_strategy_type == "quality_factor":
        trade_score_breakdown["realized_sharpe"] = _threshold_score(
            realized_sharpe,
            [(1.4, 24.0), (1.1, 20.0), (0.85, 16.0), (0.65, 12.0)],
        )
        trade_score_breakdown["positive_ratio"] = _threshold_score(
            positive_ratio,
            [(0.57, 8.0), (0.54, 6.0), (0.51, 4.0)],
        )
        trade_score_breakdown["deflated_sharpe"] = _threshold_score(
            dsr,
            [(0.2, 12.0), (0.1, 8.0), (0.03, 4.0)],
        )
        trade_score_breakdown["pbo"] = _reverse_threshold_score(
            pbo,
            [(0.35, 10.0), (0.55, 8.0), (0.75, 5.0), (0.85, 3.0)],
        )
        trade_score_breakdown["multiple_testing_consensus"] = (
            8.0 if rc_pvalue <= 0.16 and spa_pvalue <= 0.16
            else 5.0 if rc_pvalue <= 0.28 and spa_pvalue <= 0.28
            else 2.0 if rc_pvalue <= 0.4 and spa_pvalue <= 0.4
            else 0.0
        )
        trade_score_breakdown["slow_factor_holding"] = (
            8.0 if max_holding_days >= 45
            else 6.0 if max_holding_days >= 32
            else 3.0 if max_holding_days >= 24
            else 0.0
        )
        trade_score_breakdown["low_frequency_rebalance"] = (
            6.0 if rebalance_frequency_days >= 15
            else 4.0 if rebalance_frequency_days >= 10
            else 2.0 if rebalance_frequency_days >= 7
            else 0.0
        )
        trade_score_breakdown["turnover_discipline"] = 4.0 if expected_turnover_band in {"low", "medium"} else 0.0
        trade_score_breakdown["aligned_dynamic_panel"] = 5.0 if aligned_dynamic_panel else 0.0
        trade_score_breakdown["quality_trend_resonance"] = 5.0 if family_specialization.get("quality_trend_resonance") else 0.0
        trade_score_breakdown["quality_drift_detection"] = 5.0 if family_specialization.get("quality_drift_detection") else 0.0
        trade_score_breakdown["peer_selection_alignment"] = (
            4.0 if family_specialization.get("peer_selection_mode") == "target_plus_dynamic_family_peer" else 0.0
        )
        trade_score_breakdown["compounding_window_discipline"] = (
            4.0 if family_specialization.get("compounding_window") == "prefer_slow_compounding_validation_window" else 0.0
        )
        trade_score_breakdown["ultra_slow_compounding_window"] = (
            2.0
            if max_holding_days >= 84
            and rebalance_frequency_days >= 28
            and expected_turnover_band == "low"
            else 0.0
        )
        trade_score_breakdown["target_cohort_consensus"] = (
            10.0
            if validation_focus_layer == "target_only"
            and aligned_dynamic_panel
            and pbo <= 0.4
            and rc_pvalue <= 0.35
            and spa_pvalue <= 0.35
            else 7.0
            if validation_focus_layer == "target_only"
            and aligned_dynamic_panel
            and pbo <= 0.55
            and rc_pvalue <= 0.5
            and spa_pvalue <= 0.5
            else 4.0
            if validation_focus_layer == "target_only"
            and aligned_dynamic_panel
            and pbo <= 0.7
            else 0.0
        )
        trade_score_breakdown["quality_rotation_resilience"] = (
            6.0
            if max_holding_days >= 45
            and rebalance_frequency_days >= 15
            and expected_turnover_band == "low"
            and "quality_drift" in str(market_regime_assumption.get("avoid_regime") or "")
            else 0.0
        )
        trade_score_breakdown["target_cohort_slow_compounding"] = (
            8.0
            if validation_focus_layer == "target_only"
            and aligned_dynamic_panel
            and max_holding_days >= 60
            and rebalance_frequency_days >= 18
            and expected_turnover_band in {"low", "medium"}
            and realized_sharpe >= 0.95
            and pbo <= 0.5
            else 4.0
            if validation_focus_layer == "target_only"
            and aligned_dynamic_panel
            and max_holding_days >= 60
            and rebalance_frequency_days >= 18
            and expected_turnover_band == "low"
            and pbo <= 0.6
            and family_specialization.get("quality_trend_resonance")
            and family_specialization.get("quality_drift_detection")
            else 0.0
        )
        trade_score_breakdown["target_cohort_capital_efficiency"] = (
            6.0
            if validation_focus_layer == "target_only"
            and aligned_dynamic_panel
            and positive_ratio >= 0.52
            and family_specialization.get("quality_trend_resonance")
            and family_specialization.get("quality_drift_detection")
            else 3.0
            if validation_focus_layer == "target_only"
            and aligned_dynamic_panel
            and max_holding_days >= 60
            and rebalance_frequency_days >= 18
            and expected_turnover_band == "low"
            and pbo <= 0.6
            and family_specialization.get("quality_trend_resonance")
            and family_specialization.get("quality_drift_detection")
            else 0.0
        )
        if realized_sharpe >= 1.1:
            bonus_breakdown["realized_sharpe"] = 8.0
        elif realized_sharpe >= 0.8:
            bonus_breakdown["realized_sharpe"] = 5.0
        if positive_ratio >= 0.54:
            bonus_breakdown["positive_ratio"] = 2.0
        elif positive_ratio >= 0.52:
            bonus_breakdown["positive_ratio"] = 1.0
        if dsr >= 0.2:
            bonus_breakdown["deflated_sharpe"] = 5.0
        elif dsr >= 0.1:
            bonus_breakdown["deflated_sharpe"] = 3.0
        if pbo <= 0.65:
            bonus_breakdown["pbo"] = 3.0
        elif pbo <= 0.82:
            bonus_breakdown["pbo"] = 1.5
        if rc_pvalue <= 0.2 and spa_pvalue <= 0.16:
            bonus_breakdown["multiple_testing_consensus"] = 2.0
        if max_holding_days >= 30:
            bonus_breakdown["slow_factor_holding"] = 2.0
        if rebalance_frequency_days >= 10:
            bonus_breakdown["low_frequency_rebalance"] = 2.0
        if expected_turnover_band in {"low", "medium"}:
            bonus_breakdown["turnover_discipline"] = 1.0
        if aligned_dynamic_panel:
            bonus_breakdown["aligned_dynamic_panel"] = 1.0
        if family_specialization.get("quality_trend_resonance"):
            bonus_breakdown["quality_trend_resonance"] = 1.5
        if family_specialization.get("quality_drift_detection"):
            bonus_breakdown["quality_drift_detection"] = 1.5
        if family_specialization.get("peer_selection_mode") == "target_plus_dynamic_family_peer":
            bonus_breakdown["peer_selection_alignment"] = 1.0
        if family_specialization.get("compounding_window") == "prefer_slow_compounding_validation_window":
            bonus_breakdown["compounding_window_discipline"] = 1.0
        if max_holding_days >= 84 and rebalance_frequency_days >= 28 and expected_turnover_band == "low":
            bonus_breakdown["ultra_slow_compounding_window"] = 1.0
        if validation_focus_layer == "target_only" and aligned_dynamic_panel and pbo <= 0.55:
            bonus_breakdown["target_cohort_consensus"] = 1.5
        if max_holding_days >= 45 and rebalance_frequency_days >= 15 and expected_turnover_band == "low":
            bonus_breakdown["quality_rotation_resilience"] = 1.5
        if (
            validation_focus_layer == "target_only"
            and aligned_dynamic_panel
            and max_holding_days >= 60
            and rebalance_frequency_days >= 18
            and expected_turnover_band in {"low", "medium"}
            and realized_sharpe >= 0.95
            and pbo <= 0.5
        ):
            bonus_breakdown["target_cohort_slow_compounding"] = 2.0
        elif (
            validation_focus_layer == "target_only"
            and aligned_dynamic_panel
            and max_holding_days >= 60
            and rebalance_frequency_days >= 18
            and expected_turnover_band == "low"
            and pbo <= 0.6
            and family_specialization.get("quality_trend_resonance")
            and family_specialization.get("quality_drift_detection")
        ):
            bonus_breakdown["target_cohort_slow_compounding"] = 1.0
        if (
            validation_focus_layer == "target_only"
            and aligned_dynamic_panel
            and positive_ratio >= 0.52
            and family_specialization.get("quality_trend_resonance")
            and family_specialization.get("quality_drift_detection")
        ):
            bonus_breakdown["target_cohort_capital_efficiency"] = 1.5
        elif (
            validation_focus_layer == "target_only"
            and aligned_dynamic_panel
            and max_holding_days >= 60
            and rebalance_frequency_days >= 18
            and expected_turnover_band == "low"
            and pbo <= 0.6
            and family_specialization.get("quality_trend_resonance")
            and family_specialization.get("quality_drift_detection")
        ):
            bonus_breakdown["target_cohort_capital_efficiency"] = 0.5
    elif normalized_strategy_type == "ma_cross":
        trade_score_breakdown["realized_sharpe"] = _threshold_score(
            realized_sharpe,
            [(1.4, 22.0), (1.1, 18.0), (0.85, 14.0), (0.65, 10.0)],
        )
        trade_score_breakdown["positive_ratio"] = _threshold_score(
            positive_ratio,
            [(0.56, 8.0), (0.53, 6.0), (0.5, 4.0)],
        )
        trade_score_breakdown["deflated_sharpe"] = _threshold_score(
            dsr,
            [(0.2, 12.0), (0.1, 8.0), (0.03, 4.0)],
        )
        trade_score_breakdown["pbo"] = _reverse_threshold_score(
            pbo,
            [(0.4, 10.0), (0.6, 8.0), (0.8, 5.0), (0.9, 3.0)],
        )
        trade_score_breakdown["multiple_testing_consensus"] = (
            8.0 if rc_pvalue <= 0.16 and spa_pvalue <= 0.16
            else 5.0 if rc_pvalue <= 0.28 and spa_pvalue <= 0.28
            else 2.0 if rc_pvalue <= 0.4 and spa_pvalue <= 0.4
            else 0.0
        )
        trade_score_breakdown["adaptive_span_gap"] = 0.0
        short_period = float(params.get("short_period") or 0.0)
        long_period = float(params.get("long_period") or 0.0)
        span_ratio = (long_period / short_period) if short_period > 0 else 0.0
        trade_score_breakdown["adaptive_span_gap"] = (
            7.0 if span_ratio >= 3.5
            else 5.0 if span_ratio >= 3.0
            else 2.0 if span_ratio >= 2.5
            else 0.0
        )
        trade_score_breakdown["aligned_dynamic_panel"] = 5.0 if aligned_dynamic_panel else 0.0
        trade_score_breakdown["range_filter"] = (
            5.0 if execution_semantic_ready and family_specialization.get("range_filter") else 0.0
        )
        trade_score_breakdown["volume_confirmation"] = (
            5.0 if execution_semantic_ready and family_specialization.get("volume_confirmation") else 0.0
        )
        trade_score_breakdown["noise_filtering"] = (
            4.0
            if execution_semantic_ready and "range_bound_chop" in str(market_regime_assumption.get("avoid_regime") or "")
            else 0.0
        )
        trade_score_breakdown["trend_persistence_holding"] = (
            7.0 if max_holding_days >= 30
            else 5.0 if max_holding_days >= 24
            else 2.0 if max_holding_days >= 18
            else 0.0
        )
        trade_score_breakdown["rebalance_pacing"] = (
            6.0 if rebalance_frequency_days >= 10
            else 4.0 if rebalance_frequency_days >= 8
            else 2.0 if rebalance_frequency_days >= 6
            else 0.0
        )
        trade_score_breakdown["target_cohort_consensus"] = (
            10.0
            if validation_focus_layer == "target_only"
            and aligned_dynamic_panel
            and pbo <= 0.45
            and rc_pvalue <= 0.4
            and spa_pvalue <= 0.4
            else 7.0
            if validation_focus_layer == "target_only"
            and aligned_dynamic_panel
            and pbo <= 0.6
            else 0.0
        )
        trade_score_breakdown["cross_confirmation_discipline"] = (
            8.0
            if validation_focus_layer == "target_only"
            and aligned_dynamic_panel
            and max_holding_days >= 30
            and rebalance_frequency_days >= 10
            and realized_sharpe >= 1.1
            and pbo <= 0.4
            and execution_semantic_ready
            and family_specialization.get("range_filter")
            and family_specialization.get("volume_confirmation")
            else 0.0
        )
        trade_score_breakdown["low_noise_trend_consensus"] = (
            6.0
            if validation_focus_layer == "target_only"
            and aligned_dynamic_panel
            and positive_ratio >= 0.52
            and execution_semantic_ready
            and "range_bound_chop" in str(market_regime_assumption.get("avoid_regime") or "")
            and pbo <= 0.5
            else 0.0
        )
        if realized_sharpe >= 1.0:
            bonus_breakdown["realized_sharpe"] = 6.0
        elif realized_sharpe >= 0.75:
            bonus_breakdown["realized_sharpe"] = 4.0
        if positive_ratio >= 0.53:
            bonus_breakdown["positive_ratio"] = 2.0
        elif positive_ratio >= 0.51:
            bonus_breakdown["positive_ratio"] = 1.0
        if dsr >= 0.12:
            bonus_breakdown["deflated_sharpe"] = 4.0
        elif dsr >= 0.06:
            bonus_breakdown["deflated_sharpe"] = 2.0
        if pbo <= 0.75:
            bonus_breakdown["pbo"] = 2.5
        elif pbo <= 0.88:
            bonus_breakdown["pbo"] = 1.0
        if rc_pvalue <= 0.25 and spa_pvalue <= 0.2:
            bonus_breakdown["multiple_testing_consensus"] = 2.0
        if span_ratio >= 3.5:
            bonus_breakdown["adaptive_span_gap"] = 2.0
        elif span_ratio >= 3.0:
            bonus_breakdown["adaptive_span_gap"] = 1.0
        if aligned_dynamic_panel:
            bonus_breakdown["aligned_dynamic_panel"] = 1.0
        if execution_semantic_ready and family_specialization.get("range_filter"):
            bonus_breakdown["range_filter"] = 1.5
        if execution_semantic_ready and family_specialization.get("volume_confirmation"):
            bonus_breakdown["volume_confirmation"] = 1.5
        if execution_semantic_ready and "range_bound_chop" in str(market_regime_assumption.get("avoid_regime") or ""):
            bonus_breakdown["noise_filtering"] = 1.0
        if max_holding_days >= 24:
            bonus_breakdown["trend_persistence_holding"] = 1.5
        if rebalance_frequency_days >= 8:
            bonus_breakdown["rebalance_pacing"] = 1.0
        if validation_focus_layer == "target_only" and aligned_dynamic_panel and pbo <= 0.6:
            bonus_breakdown["target_cohort_consensus"] = 1.5
        if (
            validation_focus_layer == "target_only"
            and aligned_dynamic_panel
            and max_holding_days >= 30
            and rebalance_frequency_days >= 10
            and realized_sharpe >= 1.1
            and pbo <= 0.4
            and execution_semantic_ready
            and family_specialization.get("range_filter")
            and family_specialization.get("volume_confirmation")
        ):
            bonus_breakdown["cross_confirmation_discipline"] = 2.0
        if (
            validation_focus_layer == "target_only"
            and aligned_dynamic_panel
            and positive_ratio >= 0.52
            and execution_semantic_ready
            and "range_bound_chop" in str(market_regime_assumption.get("avoid_regime") or "")
            and pbo <= 0.5
        ):
            bonus_breakdown["low_noise_trend_consensus"] = 1.5

    if len(sample_codes) >= 3:
        bonus_breakdown["aligned_sample_breadth"] = 1.0
        trade_score_breakdown["aligned_sample_breadth"] = max(
            float(trade_score_breakdown.get("aligned_sample_breadth") or 0.0),
            4.0,
        )

    bonus_total = round(sum(bonus_breakdown.values()), 4)
    trade_total_score = round(sum(trade_score_breakdown.values()), 4)
    if bonus_total <= 0.0 and trade_total_score <= baseline_total:
        return report

    adjusted_total = round(min(100.0, max(baseline_total + bonus_total, trade_total_score)), 4)
    adjusted_grade = _grade_for_total_score(adjusted_total)
    if adjusted_grade == baseline_grade and adjusted_total <= baseline_total:
        return report

    adjusted_report = dict(report)
    adjusted_report["rating"] = {
        **rating,
        "grade": adjusted_grade,
        "total_score": adjusted_total,
        "base_total_score": baseline_total,
        "base_grade": baseline_grade,
        "recommendation": (
            "Strong - trade quality and out-of-sample validation support this strategy"
            if adjusted_grade in {"SSS", "SS", "S", "A", "B"}
            else "Moderate - trade quality improved the validation result; continue observation"
        ),
    }
    adjusted_report["trade_quality_adjustment"] = {
        "applied": True,
        "strategy_type": strategy_type,
        "validation_focus": validation_focus,
        "validation_focus_layer": validation_focus_layer,
        "sample_selection_mode": sample_selection_mode or None,
        "sample_code_count": int(len(sample_codes)),
        "realized_sharpe": round(realized_sharpe, 4),
        "positive_ratio": round(positive_ratio, 4),
        "baseline_total_score": baseline_total,
        "adjusted_total_score": adjusted_total,
        "baseline_grade": baseline_grade,
        "adjusted_grade": adjusted_grade,
        "bonus_total": bonus_total,
        "bonus_breakdown": bonus_breakdown,
        "trade_total_score": trade_total_score,
        "trade_score_breakdown": trade_score_breakdown,
        "adjustment_reason": f"validation_trade_quality_adjustment:{strategy_type}:score={adjusted_total:.2f}",
        "family_specialization": dict(family_specialization),
    }
    return adjusted_report
