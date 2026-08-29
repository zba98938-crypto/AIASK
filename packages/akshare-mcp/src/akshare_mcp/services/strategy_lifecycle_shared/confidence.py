"""Confidence and execution-audit gates."""

from __future__ import annotations

from typing import Any, Optional

from strategy_factory.api.constants import BACKTEST_TYPE_THRESHOLDS, PROVISIONAL_PASS_THRESHOLDS

from .common import _contract_version_stable, _safe_float, _safe_int, _string

def evaluate_confidence_contract(
    confidence_contract: Optional[dict[str, Any]],
    *,
    signal_quality: Optional[dict[str, Any]] = None,
) -> tuple[str, dict[str, Any]]:
    contract = dict(confidence_contract or {})
    prediction_quality = dict(
        contract.get("prediction_quality")
        or contract.get("probability_quality")
        or {}
    )
    prediction_interval = dict(contract.get("prediction_interval") or {})
    support_samples = _safe_int(
        prediction_quality.get("support_samples")
        or prediction_quality.get("sample_size")
        or contract.get("support_samples")
        or contract.get("sample_size"),
        _safe_int(dict(signal_quality or {}).get("primary_effective_n")),
    )
    brier_score = _safe_float(
        prediction_quality.get("brier_score")
        if prediction_quality.get("brier_score") is not None
        else contract.get("brier_score")
    )
    ece = _safe_float(
        prediction_quality.get("ece")
        if prediction_quality.get("ece") is not None
        else contract.get("ece")
    )
    calibration_gap = _safe_float(
        prediction_quality.get("calibration_gap")
        if prediction_quality.get("calibration_gap") is not None
        else contract.get("calibration_gap")
    )
    coverage_proxy = _safe_float(
        prediction_interval.get("coverage_proxy")
        if prediction_interval.get("coverage_proxy") is not None
        else contract.get("coverage_proxy")
    )
    observed_coverage = _safe_float(
        prediction_interval.get("observed_coverage")
        if prediction_interval.get("observed_coverage") is not None
        else contract.get("observed_coverage")
    )
    coverage_gap = _safe_float(
        prediction_interval.get("coverage_gap")
        if prediction_interval.get("coverage_gap") is not None
        else contract.get("coverage_gap")
    )
    quality_label = _string(
        prediction_quality.get("quality")
        or contract.get("quality")
    ).lower() or None
    contract_version = (
        _string(
            prediction_quality.get("contract_version")
            or contract.get("contract_version")
            or prediction_quality.get("version")
            or contract.get("version")
        )
        or None
    )
    contract_version_stable = _contract_version_stable(
        contract_version,
        explicit_flag=(
            prediction_quality.get("contract_version_stable")
            if prediction_quality.get("contract_version_stable") is not None
            else contract.get("contract_version_stable")
        ),
    )

    diagnostics = {
        "contract_present": bool(contract),
        "sample_size": support_samples,
        "support_samples": support_samples,
        "brier_score": brier_score,
        "ece": ece,
        "calibration_gap": calibration_gap,
        "quality": quality_label,
        "contract_version": contract_version,
        "contract_version_stable": contract_version_stable,
        "prediction_interval": {
            "coverage_proxy": coverage_proxy,
            "observed_coverage": observed_coverage,
            "coverage_gap": coverage_gap,
        },
        "diagnostic_only": True,
    }

    if not contract:
        status = "missing"
    elif support_samples < 50:
        status = "insufficient"
    elif support_samples < 100:
        status = "diagnostic_ready"
    elif contract_version_stable:
        status = "comparable_ready"
        diagnostics["diagnostic_only"] = False
    else:
        status = "diagnostic_ready"
    diagnostics["status"] = status
    return status, diagnostics


def evaluate_execution_audit_gate(
    audit_summary: Optional[dict[str, Any]],
    *,
    strategy_type: Optional[str] = None,
    bootstrap_trade_floor: Optional[int] = None,
    production_trade_floor: int = 20,
) -> tuple[str, list[str], dict[str, bool], dict[str, float | int | None]]:
    summary = dict(audit_summary or {})
    normalized_strategy_type = _string(
        summary.get("strategy_type") or strategy_type
    ).lower()
    resolved_bootstrap_trade_floor = max(
        1,
        _safe_int(
            bootstrap_trade_floor,
            max(
                _safe_int(PROVISIONAL_PASS_THRESHOLDS.get("trades_min"), 3),
                _safe_int(
                    dict(BACKTEST_TYPE_THRESHOLDS.get(normalized_strategy_type) or {}).get("trades_min"),
                    0,
                ),
            ),
        ),
    )
    resolved_production_trade_floor = max(int(production_trade_floor or 20), resolved_bootstrap_trade_floor)
    realized_trade_count = _safe_int(summary.get("realized_trade_count"))
    mapped_position_count = _safe_int(summary.get("mapped_position_count"))
    incomplete_position_count = _safe_int(summary.get("incomplete_position_count"))
    order_count = _safe_int(summary.get("order_count"))
    filled_order_count = _safe_int(summary.get("filled_order_count"))
    trade_count = _safe_int(summary.get("trade_count"))
    nav_observation_days = _safe_int(summary.get("nav_observation_days"))
    evidence_status = _string(summary.get("evidence_status")) or None
    runtime_evidence_present = bool(
        mapped_position_count > 0
        or incomplete_position_count > 0
        or order_count > 0
        or filled_order_count > 0
        or trade_count > 0
        or nav_observation_days > 0
        or evidence_status in {"ready", "empty", "bootstrap_pending"}
        or _string(summary.get("account_id"))
        or _string(summary.get("paper_account_id"))
    )
    trade_expectancy = _safe_float(summary.get("trade_expectancy"))
    pnl_conversion_efficiency = _safe_float(summary.get("pnl_conversion_efficiency"))
    execution_conversion_efficiency = _safe_float(summary.get("execution_conversion_efficiency"))
    hard_gate_metrics = {
        "realized_trade_count": realized_trade_count,
        "trade_expectancy": trade_expectancy,
        "pnl_conversion_efficiency": pnl_conversion_efficiency,
        "execution_conversion_efficiency": execution_conversion_efficiency,
        "bootstrap_trade_floor": resolved_bootstrap_trade_floor,
        "required_trade_count": resolved_production_trade_floor,
    }
    hard_gate_metric_passes = {
        "bootstrap_trade_count": realized_trade_count >= resolved_bootstrap_trade_floor,
        "realized_trade_count": realized_trade_count >= resolved_production_trade_floor,
        "trade_expectancy": trade_expectancy is not None and trade_expectancy > 0.0,
        "pnl_conversion_efficiency": (
            pnl_conversion_efficiency is not None and pnl_conversion_efficiency > 0.0
        ),
        "execution_conversion_efficiency": (
            execution_conversion_efficiency is not None
            and execution_conversion_efficiency >= 0.20
        ),
    }
    reasons: list[str] = []
    if not summary or (realized_trade_count <= 0 and not runtime_evidence_present):
        status = "missing"
        reasons.append("execution_audit_missing")
    elif realized_trade_count <= 0:
        status = "bootstrap_pending"
        reasons.append("execution_audit_bootstrap_pending")
    elif realized_trade_count < resolved_bootstrap_trade_floor:
        status = "insufficient_samples"
        reasons.append(f"realized_trade_count<{resolved_bootstrap_trade_floor}")
    else:
        if not hard_gate_metric_passes["trade_expectancy"]:
            reasons.append("trade_expectancy<=0")
        if not hard_gate_metric_passes["pnl_conversion_efficiency"]:
            reasons.append("pnl_conversion_efficiency<=0")
        if not hard_gate_metric_passes["execution_conversion_efficiency"]:
            reasons.append("execution_conversion_efficiency<0.20")
        if reasons:
            status = "failed_metrics"
        elif realized_trade_count < resolved_production_trade_floor:
            status = "bootstrap_ready"
            reasons.append(f"realized_trade_count<{resolved_production_trade_floor}")
        else:
            status = "passed"
    return status, reasons, hard_gate_metric_passes, hard_gate_metrics


EXPECTED_FORWARD_DAYS = (1, 5, 10, 20)
SIGNAL_QUALITY_PRIMARY_DEFAULT = (5, 10)
SIGNAL_QUALITY_OVERLAP_FACTORS = {
    1: 1.0,
    5: 3.0,
    10: 5.0,
    20: 8.0,
}
EXECUTION_SIGNAL_TO_FILL_WEAK = 0.30
EXECUTION_SIGNAL_TO_FILL_STRONG = 0.60
EXECUTION_FILLED_ORDER_WEAK = 0.50
EXECUTION_FILLED_ORDER_STRONG = 0.70
EXECUTION_NAV_RETURN_STRONG = 0.01
EXECUTION_NAV_CONVERSION_WEAK = 0.10
EXECUTION_NAV_CONVERSION_STRONG = 0.20
