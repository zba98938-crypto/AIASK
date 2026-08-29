"""Trade-prediction diagnostics for Strategy Factory promotion review."""

from __future__ import annotations

import inspect
from datetime import datetime, timezone
from typing import Any

from ._runtime_toggles import strategy_trade_prediction_promotion_gate_enabled


TRADE_PREDICTION_PROMOTION_SCORE_VERSION = "trade_prediction_score_v2"
TRADE_PREDICTION_PROMOTION_MIN_SAMPLE_N = 30
TRADE_PREDICTION_PROMOTION_MIN_SCORE_LCB_95 = 0.55

_PARTIAL_SCORE_STATUSES = {
    "pending_market_data",
    "partial_daily_only",
    "partial_intraday_missing",
    "insufficient_samples",
    "post_hoc_rejected",
}
_BAD_DATA_QUALITY_STATUSES = {
    "daily_bar_missing",
    "intraday_missing",
    "partial_gap",
    "invalid_ohlc",
}


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


def _string(value: Any) -> str:
    return str(value or "").strip()


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return default


def _safe_float(value: Any) -> float | None:
    try:
        result = float(value)
    except Exception:
        return None
    if result != result:
        return None
    return max(0.0, min(result, 1.0))


def _increment(target: dict[str, int], value: Any) -> None:
    key = _string(value).lower() or "unknown"
    target[key] = int(target.get(key) or 0) + 1


def _score_lcb_95(avg: float | None, sample_n: int) -> float | None:
    if avg is None or sample_n <= 0:
        return None
    return max(0.0, avg - 1.96 * ((avg * (1.0 - avg)) / sample_n) ** 0.5)


def _aggregate_outcomes(outcomes: list[dict[str, Any]], *, score_version: str) -> dict[str, Any]:
    version = _string(score_version)
    v2_outcomes = [
        dict(item or {})
        for item in list(outcomes or [])
        if _string((item or {}).get("score_version")) == version
    ]
    status_counts: dict[str, int] = {}
    data_quality_counts: dict[str, int] = {}
    score_values: list[float] = []
    ok_prediction_ids: set[str] = set()
    partial_count = 0
    data_gap_count = 0
    for outcome in v2_outcomes:
        score_status = _string(outcome.get("score_status")).lower()
        data_quality_status = _string(outcome.get("data_quality_status")).lower()
        _increment(status_counts, score_status)
        _increment(data_quality_counts, data_quality_status)
        if score_status in _PARTIAL_SCORE_STATUSES:
            partial_count += 1
        if data_quality_status in _BAD_DATA_QUALITY_STATUSES:
            data_gap_count += 1
        if score_status != "ok" or data_quality_status != "ok":
            continue
        score = _safe_float(outcome.get("trade_prediction_score"))
        if score is None:
            continue
        score_values.append(score)
        prediction_id = _string(outcome.get("prediction_id"))
        if prediction_id:
            ok_prediction_ids.add(prediction_id)
    ok_sample_n = len(ok_prediction_ids) if ok_prediction_ids else len(score_values)
    score_avg = sum(score_values) / len(score_values) if score_values else None
    lcb = _score_lcb_95(score_avg, ok_sample_n)
    return {
        "score_version": version,
        "outcome_count": len(v2_outcomes),
        "ok_sample_n": ok_sample_n,
        "partial_count": partial_count,
        "data_gap_count": data_gap_count,
        "score_status_counts": status_counts,
        "data_quality_status_counts": data_quality_counts,
        "score_avg": round(score_avg, 6) if score_avg is not None else None,
        "score_lcb_95": round(lcb, 6) if lcb is not None else None,
    }


async def _read_prediction_payloads(
    db: Any,
    *,
    strategy_id: str | None,
    stock_code: str | None,
    score_version: str,
    limit: int,
) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
    summary_method = getattr(db, "summarize_strategy_trade_predictions", None)
    matrix_method = getattr(db, "aggregate_trade_prediction_matrix", None)
    outcomes_method = getattr(db, "list_strategy_trade_prediction_outcomes", None)
    if not callable(summary_method) or not callable(matrix_method) or not callable(outcomes_method):
        raise RuntimeError("trade prediction storage helpers are unavailable")
    summary = await _maybe_await(
        summary_method(strategy_id=strategy_id, stock_code=stock_code, limit=limit)
    )
    matrix = await _maybe_await(
        matrix_method(
            strategy_id=strategy_id,
            stock_code=stock_code,
            score_version=score_version,
            dimensions=["family", "stage", "regime", "event", "factor"],
            limit=limit,
        )
    )
    outcomes = await _maybe_await(
        outcomes_method(
            strategy_id=strategy_id,
            stock_code=stock_code,
            score_version=score_version,
            limit=limit,
        )
    )
    return dict(summary or {}), dict(matrix or {}), [dict(item or {}) for item in list(outcomes or [])]


async def evaluate_trade_prediction_promotion_gate(
    db: Any | None = None,
    *,
    strategy_id: str | None = None,
    stock_code: str | None = None,
    summary: dict[str, Any] | None = None,
    matrix: dict[str, Any] | None = None,
    outcomes: list[dict[str, Any]] | None = None,
    enabled: bool | None = None,
    score_version: str = TRADE_PREDICTION_PROMOTION_SCORE_VERSION,
    min_sample_n: int = TRADE_PREDICTION_PROMOTION_MIN_SAMPLE_N,
    min_score_lcb_95: float = TRADE_PREDICTION_PROMOTION_MIN_SCORE_LCB_95,
    limit: int = 1000,
) -> dict[str, Any]:
    """Evaluate prediction aggregates as a diagnostic or hard promotion gate."""
    gate_enabled = (
        strategy_trade_prediction_promotion_gate_enabled()
        if enabled is None
        else bool(enabled)
    )
    degraded = False
    error = None
    if outcomes is None:
        if db is None:
            degraded = True
            error = "trade prediction storage is not configured"
            summary = dict(summary or {})
            matrix = dict(matrix or {})
            outcomes = []
        else:
            try:
                summary, matrix, outcomes = await _read_prediction_payloads(
                    db,
                    strategy_id=strategy_id,
                    stock_code=stock_code,
                    score_version=score_version,
                    limit=limit,
                )
            except Exception as exc:
                degraded = True
                error = str(exc)
                summary = dict(summary or {})
                matrix = dict(matrix or {})
                outcomes = []
    else:
        summary = dict(summary or {})
        matrix = dict(matrix or {})
        outcomes = [dict(item or {}) for item in list(outcomes or [])]

    min_sample_n = max(1, _safe_int(min_sample_n, TRADE_PREDICTION_PROMOTION_MIN_SAMPLE_N))
    try:
        min_lcb = max(0.0, min(float(min_score_lcb_95), 1.0))
    except Exception:
        min_lcb = TRADE_PREDICTION_PROMOTION_MIN_SCORE_LCB_95

    aggregate = _aggregate_outcomes(outcomes, score_version=score_version)
    diagnostic_reasons: list[str] = []
    if degraded:
        diagnostic_reasons.append("trade_prediction_gate_degraded")
    if not aggregate["outcome_count"]:
        diagnostic_reasons.append("trade_prediction_score_v2_missing")
    if aggregate["ok_sample_n"] < min_sample_n:
        diagnostic_reasons.append("trade_prediction_insufficient_samples")
    if aggregate["partial_count"] > 0:
        diagnostic_reasons.append("trade_prediction_partial_outcomes")
    if aggregate["data_gap_count"] > 0:
        diagnostic_reasons.append("trade_prediction_data_quality_gap")
    lcb = _safe_float(aggregate.get("score_lcb_95"))
    if lcb is None or lcb < min_lcb:
        diagnostic_reasons.append("trade_prediction_lcb_below_threshold")

    diagnostic_passed = not diagnostic_reasons
    reasons = list(diagnostic_reasons)
    if not gate_enabled:
        reasons = ["promotion_gate_disabled_diagnostic_only", *reasons]
    hard_block = bool(gate_enabled and not diagnostic_passed)
    return {
        "object": "strategy_factory.trade_prediction_promotion_gate",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "enabled": gate_enabled,
        "diagnostic_only": not gate_enabled,
        "passed": diagnostic_passed if gate_enabled else True,
        "diagnostic_passed": diagnostic_passed,
        "hard_block": hard_block,
        "reasons": reasons,
        "score_version_required": score_version,
        "min_sample_n": min_sample_n,
        "min_score_lcb_95": min_lcb,
        "strategy_id": strategy_id,
        "stock_code": stock_code,
        "degraded": degraded,
        "error": error,
        "aggregate": aggregate,
        "summary": summary,
        "matrix": matrix,
    }


__all__ = [
    "TRADE_PREDICTION_PROMOTION_MIN_SAMPLE_N",
    "TRADE_PREDICTION_PROMOTION_MIN_SCORE_LCB_95",
    "TRADE_PREDICTION_PROMOTION_SCORE_VERSION",
    "evaluate_trade_prediction_promotion_gate",
]
