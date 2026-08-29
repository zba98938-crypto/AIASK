"""Quant context builder for unified decision."""

from __future__ import annotations

import asyncio
import math
from typing import Any

from ..services.conditional_returns import calculate_conditional_returns
from ..services.data_pipeline import compute_signal_hit_rate, normalize_klines
from ..storage import get_db
from ..tools.factor_profile import (
    _FACTOR_REGISTRY,
    _OVERSOLD_SIGNAL_MAP,
    _build_factor_profile,
    _fetch_peer_codes,
)
from ..tools.quant import _build_similar_pattern_report
from ..tools.quant_analysis import run_factor_oos_validation
from ..utils import resolve_security_code
from .decision_pipeline_shared import build_context_meta, clamp, safe_float
from .decision_context_builder import _sanitize_warning_message
from .signal_quality_registry import get_default_signal_quality_registry


def _pick_period_stat(container: dict[str, Any] | None, period: int) -> dict[str, Any]:
    if not isinstance(container, dict):
        return {}
    data = container.get(f"{period}d")
    return dict(data) if isinstance(data, dict) else {}


def _blended_probability(
    *,
    period: int,
    signal_stats: dict[str, Any] | None,
    conditional_returns: dict[str, Any] | None,
    similar_patterns: dict[str, Any] | None,
    rsi_current: float | None,
    macd_current: float | None,
    momentum_current: float | None,
) -> dict[str, Any]:
    signal = _pick_period_stat((signal_stats or {}).get("forward_returns", {}), period)
    conditional = _pick_period_stat((conditional_returns or {}).get("forward_returns", {}), period)
    similar = _pick_period_stat((similar_patterns or {}).get("aggregate_prediction", {}), period)

    components: list[tuple[str, float]] = []
    for label, payload in (
        ("signal", safe_float(signal.get("hit_rate"))),
        ("conditional", safe_float(conditional.get("win_rate"))),
        ("similar", safe_float(similar.get("hit_rate"))),
    ):
        if payload is not None:
            components.append((label, payload))

    heuristic = 0.5
    if rsi_current is not None:
        heuristic += 0.08 if rsi_current <= 32 else (-0.06 if rsi_current >= 72 else 0.0)
    if macd_current is not None:
        heuristic += 0.06 if macd_current > 0 else (-0.04 if macd_current < 0 else 0.0)
    if momentum_current is not None:
        heuristic += 0.08 if momentum_current > 0 else (-0.08 if momentum_current < 0 else 0.0)
    components.append(("heuristic", clamp(heuristic, 0.2, 0.8)))

    weights = {"signal": 0.35, "conditional": 0.30, "similar": 0.20, "heuristic": 0.15}
    numerator = sum(value * weights.get(label, 0.0) for label, value in components)
    denominator = sum(weights.get(label, 0.0) for label, _ in components)
    probability = clamp(numerator / denominator if denominator > 0 else 0.5, 0.05, 0.95)

    expected_return = safe_float(signal.get("avg_return"))
    if expected_return is None:
        expected_return = safe_float(conditional.get("mean"))
    if expected_return is None:
        expected_return = safe_float(similar.get("avg_return"))

    return {
        "up_probability": round(probability, 4),
        "expected_return": round(expected_return, 4) if expected_return is not None else None,
        "sources": [label for label, _ in components],
        "sample_hint": int(
            max(
                safe_float(signal.get("samples")) or 0,
                safe_float(conditional.get("count")) or 0,
                safe_float(similar.get("samples")) or 0,
            )
        ),
    }


def _quality_label(sample_size: int, stability_ratio: float | None, warnings: list[str]) -> str:
    if sample_size >= 100 and (stability_ratio or 0.0) >= 0.6 and not warnings:
        return "high"
    if sample_size >= 40 and (stability_ratio or 0.0) >= 0.35:
        return "medium"
    return "low"


def _extract_recent_forward_samples(entries: list[dict[str, Any]] | None, period: int) -> list[float]:
    if not isinstance(entries, list):
        return []
    period_key = f"{int(period)}d"
    values: list[float] = []
    for item in entries:
        if not isinstance(item, dict):
            continue
        forward_returns = item.get("forward_returns") or {}
        value = safe_float((forward_returns or {}).get(period_key))
        if value is not None:
            values.append(float(value))
    return values


def _historical_forward_samples(closes: list[float], period: int, limit: int = 120) -> list[float]:
    horizon = max(1, int(period))
    if len(closes) <= horizon + 1:
        return []
    start = max(0, len(closes) - int(limit) - horizon)
    samples: list[float] = []
    for idx in range(start, len(closes) - horizon):
        price_now = safe_float(closes[idx])
        price_future = safe_float(closes[idx + horizon])
        if price_now is None or price_future is None or price_now <= 0:
            continue
        samples.append(float((price_future - price_now) / price_now))
    return samples


def _build_target_probability_quality(
    *,
    period: int,
    up_probability: float,
    signal_stats: dict[str, Any] | None,
    conditional_returns: dict[str, Any] | None,
    similar_patterns: dict[str, Any] | None,
    stability_ratio: float | None,
) -> dict[str, Any]:
    signal = _pick_period_stat((signal_stats or {}).get("forward_returns", {}), period)
    conditional = _pick_period_stat((conditional_returns or {}).get("forward_returns", {}), period)
    similar = _pick_period_stat((similar_patterns or {}).get("aggregate_prediction", {}), period)

    components = []
    for label, payload, hit_key, avg_key, sample_key in (
        ("signal", signal, "hit_rate", "avg_return", "samples"),
        ("conditional", conditional, "win_rate", "mean", "count"),
        ("similar", similar, "hit_rate", "avg_return", "samples"),
    ):
        hit_rate = safe_float((payload or {}).get(hit_key))
        avg_return = safe_float((payload or {}).get(avg_key))
        support_samples = max(0, int(safe_float((payload or {}).get(sample_key)) or 0))
        if hit_rate is None or support_samples <= 0:
            continue
        components.append(
            {
                "source": label,
                "support_samples": support_samples,
                "hit_rate": float(hit_rate),
                "avg_return": float(avg_return) if avg_return is not None else None,
                "weight": math.sqrt(float(support_samples)),
            }
        )

    if not components:
        return {
            "quality": "low",
            "support_samples": 0,
            "effective_sample_size": 0,
            "empirical_hit_rate": None,
            "empirical_avg_forward_return": None,
            "calibration_gap": None,
            "stability_ratio": round(stability_ratio, 4) if stability_ratio is not None else None,
            "component_count": 0,
            "components": [],
            "method": "ensemble_empirical_blend",
        }

    weight_sum = sum(float(item["weight"]) for item in components)
    empirical_hit_rate = (
        sum(float(item["hit_rate"]) * float(item["weight"]) for item in components) / weight_sum
        if weight_sum > 0
        else None
    )
    avg_components = [item for item in components if item.get("avg_return") is not None]
    avg_weight_sum = sum(float(item["weight"]) for item in avg_components)
    empirical_avg_return = (
        sum(float(item["avg_return"]) * float(item["weight"]) for item in avg_components) / avg_weight_sum
        if avg_weight_sum > 0
        else None
    )
    effective_sample_size = int(
        round(
            (weight_sum ** 2) / sum(float(item["weight"]) ** 2 for item in components)
        )
    ) if components else 0
    support_samples = int(sum(int(item["support_samples"]) for item in components))
    calibration_gap = (
        round(float(up_probability) - float(empirical_hit_rate), 4)
        if empirical_hit_rate is not None
        else None
    )

    quality = "low"
    if (
        support_samples >= 60
        and len(components) >= 2
        and calibration_gap is not None
        and abs(calibration_gap) <= 0.08
        and (stability_ratio or 0.0) >= 0.55
    ):
        quality = "high"
    elif (
        support_samples >= 20
        and calibration_gap is not None
        and abs(calibration_gap) <= 0.16
    ):
        quality = "medium"

    return {
        "quality": quality,
        "support_samples": support_samples,
        "effective_sample_size": effective_sample_size,
        "empirical_hit_rate": round(float(empirical_hit_rate), 4) if empirical_hit_rate is not None else None,
        "empirical_avg_forward_return": round(float(empirical_avg_return), 4) if empirical_avg_return is not None else None,
        "calibration_gap": calibration_gap,
        "stability_ratio": round(stability_ratio, 4) if stability_ratio is not None else None,
        "component_count": len(components),
        "components": [
            {
                "source": str(item["source"]),
                "support_samples": int(item["support_samples"]),
                "hit_rate": round(float(item["hit_rate"]), 4),
                "avg_return": round(float(item["avg_return"]), 4) if item.get("avg_return") is not None else None,
            }
            for item in components
        ],
        "method": "ensemble_empirical_blend",
    }


def _build_target_prediction_interval(
    *,
    period: int,
    expected_return: float | None,
    signal_stats: dict[str, Any] | None,
    conditional_returns: dict[str, Any] | None,
    similar_patterns: dict[str, Any] | None,
    closes: list[float],
    confidence: float = 0.8,
) -> dict[str, Any] | None:
    signal_samples = _extract_recent_forward_samples((signal_stats or {}).get("recent_signals"), period)
    conditional_samples = _extract_recent_forward_samples((conditional_returns or {}).get("recent_matches"), period)
    similar_samples = _extract_recent_forward_samples((similar_patterns or {}).get("matches"), period)

    samples: list[float] = []
    source_mix: list[str] = []
    for label, values in (
        ("signal_recent_matches", signal_samples),
        ("conditional_recent_matches", conditional_samples),
        ("similar_patterns", similar_samples),
    ):
        if values:
            samples.extend(values)
            source_mix.append(label)

    regime_conditioned = bool(samples)
    if len(samples) < 10:
        historical_samples = _historical_forward_samples(closes, period, limit=120)
        if historical_samples:
            samples.extend(historical_samples)
            source_mix.append("close_history_fallback")

    if len(samples) < 10:
        return None

    ordered = sorted(float(item) for item in samples)
    alpha = max(0.01, min(0.49, (1.0 - float(confidence)) / 2.0))
    lower_idx = int(math.floor(alpha * (len(ordered) - 1)))
    upper_idx = int(math.ceil((1.0 - alpha) * (len(ordered) - 1)))
    lower = ordered[max(0, min(len(ordered) - 1, lower_idx))]
    upper = ordered[max(0, min(len(ordered) - 1, upper_idx))]
    median = ordered[len(ordered) // 2]
    hit_rate = sum(1 for item in ordered if item > 0) / len(ordered)
    mean_val = sum(ordered) / len(ordered)

    return {
        "horizon_days": int(period),
        "confidence_level": round(float(confidence), 4),
        "sample_count": len(ordered),
        "expected_return": round(float(expected_return), 4) if expected_return is not None else round(float(mean_val), 4),
        "median_return": round(float(median), 4),
        "lower_return": round(float(lower), 4),
        "upper_return": round(float(upper), 4),
        "hit_rate": round(float(hit_rate), 4),
        "coverage_proxy": round(float(confidence), 4),
        "source_mix": list(dict.fromkeys(source_mix)),
        "regime_conditioned": regime_conditioned,
        "method": "ensemble_historical_quantiles",
    }


async def build_quant_context(
    code: str,
    *,
    factors: tuple[str, ...] = ("rsi", "macd", "momentum"),
    lookback_days: int = 250,
) -> dict[str, Any]:
    """Build a quant evidence bundle used by the unified decision pipeline."""
    normalized_code = resolve_security_code(code)
    if not normalized_code:
        raise ValueError("需要提供股票代码")

    db = get_db()
    klines = await db.get_klines(normalized_code, limit=max(int(lookback_days), 260))
    if not klines or len(klines) < 30:
        raise RuntimeError("K 线数据不足，无法构建量化上下文")

    ordered = normalize_klines(klines)
    closes = [float(item.get("close", 0) or 0) for item in ordered if item.get("close") is not None]
    warnings: list[str] = []
    fallback_reasons: list[str] = []
    source_chain = [
        "decision_quant_builder",
        "db.get_klines",
        "factor_profile._FACTOR_REGISTRY",
        "data_pipeline.compute_signal_hit_rate",
    ]
    result_factors: dict[str, Any] = {}
    reasons: list[str] = []
    risks: list[str] = []
    score = 50.0

    for factor_name in factors:
        compute_fn = _FACTOR_REGISTRY.get(factor_name)
        if compute_fn is None:
            warnings.append(f"unsupported_factor:{factor_name}")
            continue
        try:
            series = compute_fn(closes)
            profile = _build_factor_profile(series, len(closes))
            result_factors[factor_name] = profile
        except Exception as exc:
            warnings.append(f"{factor_name}:{_sanitize_warning_message(str(exc))}")

    rsi_profile = result_factors.get("rsi", {})
    rsi_current = safe_float(rsi_profile.get("current"))
    if rsi_current is not None:
        if rsi_current <= 32:
            score += 8.0
            reasons.append(f"RSI 处于低位({rsi_current:.1f})，存在均值回归空间")
        elif rsi_current >= 72:
            score -= 8.0
            risks.append(f"RSI 偏高({rsi_current:.1f})，短线追高风险上升")

    macd_profile = result_factors.get("macd", {})
    macd_current = safe_float(macd_profile.get("current"))
    if macd_current is not None:
        if macd_current > 0:
            score += 10.0
            reasons.append("MACD 位于零轴上方，趋势延续信号偏正面")
        elif macd_current < 0:
            score -= 8.0
            risks.append("MACD 位于零轴下方，趋势确认仍偏弱")

    momentum_profile = result_factors.get("momentum", {})
    momentum_current = safe_float(momentum_profile.get("current"))
    if momentum_current is not None:
        if momentum_current > 0:
            score += 12.0
            reasons.append(f"20 日动量为正({momentum_current:.3f})")
        elif momentum_current < 0:
            score -= 12.0
            risks.append(f"20 日动量为负({momentum_current:.3f})")

    signal_stats = None
    signal_meta = _OVERSOLD_SIGNAL_MAP.get("rsi")
    if signal_meta:
        try:
            signal_stats = compute_signal_hit_rate(
                ordered,
                signal=signal_meta["signal"],
                forward_days=[5, 10, 20],
                signal_params=signal_meta["signal_params"],
            )
            result_factors.setdefault("rsi", {})["historical_oversold_recovery"] = signal_stats
            forward_10d = (signal_stats.get("forward_returns") or {}).get("10d", {})
            win_rate = safe_float(forward_10d.get("hit_rate"))
            mean_return = safe_float(forward_10d.get("avg_return"))
            if win_rate is not None:
                if win_rate >= 0.55:
                    score += 8.0
                    reasons.append(f"历史 RSI 超卖后 10 日胜率较高({win_rate:.0%})")
                elif win_rate <= 0.45:
                    score -= 8.0
                    risks.append(f"历史 RSI 超卖后 10 日胜率一般({win_rate:.0%})")
            if mean_return is not None and mean_return > 0:
                score += 4.0
        except Exception as exc:
            warnings.append(f"signal_hit_rate:{_sanitize_warning_message(str(exc))}")

    conditional_returns = None
    try:
        conditional_returns = calculate_conditional_returns(
            ordered,
            conditions=[
                {"field": "rsi_14", "op": "<=", "value": 35},
                {"field": "volume_ratio", "op": ">=", "value": 0.9},
            ],
            forward_days=[1, 3, 5, 10, 20],
            logic="AND",
        )
        source_chain.append("services.conditional_returns")
        forward_5d = (conditional_returns.get("forward_returns") or {}).get("5d", {})
        condition_win_rate = safe_float(forward_5d.get("win_rate"))
        if condition_win_rate is not None:
            if condition_win_rate >= 0.58:
                score += 5.0
                reasons.append(f"条件收益统计显示 5 日胜率偏高({condition_win_rate:.0%})")
            elif condition_win_rate <= 0.42:
                score -= 5.0
                risks.append(f"条件收益统计显示 5 日胜率偏低({condition_win_rate:.0%})")
    except Exception as exc:
        _msg = _sanitize_warning_message(str(exc))
        warnings.append(f"conditional_returns:{_msg}")
        fallback_reasons.append(f"conditional_returns:{_msg}")

    similar_patterns = None
    try:
        similar_patterns = _build_similar_pattern_report(
            ordered,
            window_days=20,
            top_n=6,
            forward_days=[5, 10, 20],
        )
        source_chain.append("quant._build_similar_pattern_report")
        similar_10d = (similar_patterns.get("aggregate_prediction") or {}).get("10d", {})
        similar_hit_rate = safe_float(similar_10d.get("hit_rate"))
        if similar_hit_rate is not None:
            if similar_hit_rate >= 0.60:
                score += 4.0
                reasons.append(f"历史相似形态 10 日命中率较高({similar_hit_rate:.0%})")
            elif similar_hit_rate <= 0.40:
                score -= 4.0
                risks.append(f"历史相似形态 10 日表现偏弱({similar_hit_rate:.0%})")
    except Exception as exc:
        _msg = _sanitize_warning_message(str(exc))
        warnings.append(f"similar_patterns:{_msg}")
        fallback_reasons.append(f"similar_patterns:{_msg}")

    oos_validation = None
    try:
        industry = ""
        stock_info = await db.get_stock_info(normalized_code)
        if isinstance(stock_info, dict):
            industry = str(stock_info.get("industry") or "")
        peer_codes, market_codes = await _fetch_peer_codes(db, normalized_code, industry)
        candidate_codes = [normalized_code, *peer_codes[:8], *market_codes[:4]]
        candidate_codes = list(dict.fromkeys(code for code in candidate_codes if code))
        if len(candidate_codes) >= 3:
            validation_resp = await asyncio.wait_for(
                run_factor_oos_validation(
                    candidate_codes[:12],
                    "momentum",
                    factor_lookback=20,
                    forward_period=10,
                    panel_periods=120,
                    wf_train_window=50,
                    wf_test_window=20,
                    bootstrap_n=300,
                    bootstrap_confidence=0.9,
                    validation_parallel=False,
                    bootstrap_mode="fast",
                    include_perf_breakdown=False,
                ),
                timeout=20.0,
            )
            if validation_resp.get("success"):
                oos_validation = dict(validation_resp.get("data") or {})
                source_chain.append("quant.run_factor_oos_validation")
            else:
                raise RuntimeError(str(validation_resp.get("error") or "oos_validation_failed"))
        else:
            fallback_reasons.append("oos_validation:peer_codes_insufficient")
            warnings.append("oos_validation:peer_codes_insufficient")
    except Exception as exc:
        _msg = _sanitize_warning_message(str(exc))
        warnings.append(f"oos_validation:{_msg}")
        fallback_reasons.append(f"oos_validation:{_msg}")

    walk_forward = ((oos_validation or {}).get("validation_report") or {}).get("walk_forward", {})
    bootstrap_ci = ((oos_validation or {}).get("validation_report") or {}).get("bootstrap_ci", {})
    stability_ratio = safe_float(walk_forward.get("stability_ratio"))
    probability_targets: dict[str, Any] = {}
    horizon_quality: dict[str, str] = {}
    interval_available_periods: list[str] = []
    for period in (1, 3, 5, 10, 20):
        period_key = f"{period}d"
        target = _blended_probability(
            period=period,
            signal_stats=signal_stats,
            conditional_returns=conditional_returns,
            similar_patterns=similar_patterns,
            rsi_current=rsi_current,
            macd_current=macd_current,
            momentum_current=momentum_current,
        )
        prediction_quality = _build_target_probability_quality(
            period=period,
            up_probability=float(target.get("up_probability") or 0.5),
            signal_stats=signal_stats,
            conditional_returns=conditional_returns,
            similar_patterns=similar_patterns,
            stability_ratio=stability_ratio,
        )
        prediction_interval = _build_target_prediction_interval(
            period=period,
            expected_return=safe_float(target.get("expected_return")),
            signal_stats=signal_stats,
            conditional_returns=conditional_returns,
            similar_patterns=similar_patterns,
            closes=closes,
            confidence=0.8,
        )
        target["prediction_quality"] = prediction_quality
        if prediction_interval is not None:
            target["prediction_interval"] = prediction_interval
            interval_available_periods.append(period_key)
        probability_targets[period_key] = target
        horizon_quality[period_key] = str(prediction_quality.get("quality") or "low")

    similar_match_count = len((similar_patterns or {}).get("matches") or [])
    sample_size = max(
        int((signal_stats or {}).get("sample_count") or 0),
        int((conditional_returns or {}).get("condition_matches") or 0),
        similar_match_count,
        int((bootstrap_ci or {}).get("sample_size") or 0),
    )
    confidence_meta = {
        "sample_size": sample_size,
        "stability_ratio": round(stability_ratio, 4) if stability_ratio is not None else None,
        "oos_rank_ic_mean": safe_float(walk_forward.get("oos_rank_ic_mean")),
        "ci_bounds": {
            "lower": safe_float(bootstrap_ci.get("ci_lower")),
            "upper": safe_float(bootstrap_ci.get("ci_upper")),
        },
        "quality": _quality_label(sample_size, stability_ratio, warnings),
        "horizon_quality": horizon_quality,
        "interval_available_periods": interval_available_periods,
        "warning_count": len(warnings),
    }

    score = round(clamp(score, 0.0, 100.0), 2)
    missing_fields = []
    if not conditional_returns:
        missing_fields.append("conditional_returns")
    if not similar_patterns:
        missing_fields.append("similar_patterns")
    if not oos_validation:
        missing_fields.append("oos_validation")

    meta = build_context_meta(
        source="quant_context",
        source_chain=source_chain,
        asof_value=(ordered[-1] or {}).get("date"),
        warnings=warnings,
        fallback_reason=fallback_reasons,
        missing_fields=missing_fields,
        degraded=bool(warnings or missing_fields),
        cached=False,
    )
    default_target = probability_targets.get("10d") or {}
    result = {
        "code": normalized_code,
        "score": score,
        "factors": result_factors,
        "signal_stats": signal_stats,
        "conditional_returns": conditional_returns,
        "similar_patterns": similar_patterns,
        "oos_validation": oos_validation,
        "probability_targets": probability_targets,
        "prediction_quality": default_target.get("prediction_quality"),
        "prediction_interval": default_target.get("prediction_interval"),
        "confidence_meta": confidence_meta,
        "reasons": reasons,
        "risks": risks,
        **meta,
    }
    try:
        get_default_signal_quality_registry().register_probability(
            code=normalized_code,
            as_of=str((ordered[-1] or {}).get("date") or ""),
            payload=result,
        )
    except Exception:
        pass
    return result
