"""Fusion logic for unified decision summary/details."""

from __future__ import annotations

from statistics import pstdev
from typing import Any

from .decision_pipeline_shared import clamp, safe_float, unique_texts


STYLE_WEIGHTS = {
    "aggressive": {"stock_context": 0.45, "quant": 0.35, "event": 0.20},
    "balanced": {"stock_context": 0.55, "quant": 0.25, "event": 0.20},
    "conservative": {"stock_context": 0.50, "quant": 0.15, "event": 0.35},
}


def _decide_action(final_score: float, veto_reason: str | None, stock_recommendation: str, raw_action: str) -> str:
    if veto_reason:
        return "watch"
    if final_score >= 76:
        return "buy"
    if final_score >= 61:
        return "hold" if raw_action not in {"sell", "reduce"} else "watch"
    if final_score >= 46:
        return raw_action if raw_action in {"watch", "reduce"} else "watch"
    if stock_recommendation == "sell" or raw_action == "sell":
        return "sell"
    return "reduce"


def _position_label(suggested_position_pct: float, action: str, veto_reason: str | None) -> str:
    if veto_reason or action in {"watch", "sell"} or suggested_position_pct <= 0:
        return "暂不出手"
    if suggested_position_pct >= 0.20:
        return "可分批布局"
    if suggested_position_pct >= 0.10:
        return "小仓试探"
    return "轻仓观察"


def _normalize_style(style: str | None) -> str:
    raw = str(style or "").strip().lower()
    if raw in STYLE_WEIGHTS:
        return raw
    return "balanced"


def _build_raw_ai_output(
    *,
    stock_context: dict[str, Any],
    quant_context: dict[str, Any],
    event_context: dict[str, Any],
    gate: dict[str, Any],
) -> dict[str, Any]:
    bull_points = unique_texts(
        stock_context.get("highlights", [])[:3],
        quant_context.get("reasons", [])[:3],
        event_context.get("reasons", [])[:2],
    )[:6]
    bear_points = unique_texts(
        stock_context.get("risks", [])[:3],
        quant_context.get("risks", [])[:3],
        event_context.get("risks", [])[:3],
        [flag.get("message", "") for flag in gate.get("warning_flags", [])],
    )[:6]
    uncertainties = unique_texts(
        stock_context.get("warnings", []),
        quant_context.get("warnings", []),
        event_context.get("warnings", []),
        [flag.get("message", "") for flag in gate.get("flags", []) if flag.get("status") == "warn"],
    )[:6]

    event_direction = str(event_context.get("event_direction") or "neutral")
    event_intensity = str(event_context.get("event_intensity") or "low")
    momentum_current = safe_float(((quant_context.get("factors") or {}).get("momentum") or {}).get("current"))
    stock_recommendation = str(stock_context.get("recommendation") or "wait")

    if gate.get("blocked"):
        raw_action = "watch"
    elif len(bull_points) >= len(bear_points) + 2 and event_direction != "bearish":
        raw_action = "buy"
    elif len(bear_points) >= len(bull_points) + 2 or stock_recommendation == "sell":
        raw_action = "reduce"
    else:
        raw_action = "hold" if stock_recommendation in {"buy", "hold"} else "watch"

    if event_intensity in {"critical", "high"} and event_direction == "bearish":
        recommended_horizon = "short"
    elif momentum_current is not None and momentum_current > 0 and stock_recommendation == "buy":
        recommended_horizon = "mid"
    else:
        recommended_horizon = "short" if raw_action == "watch" else "mid"

    summary_parts = []
    if raw_action == "buy":
        summary_parts.append("原始融合判断偏向积极，当前更适合分批验证。")
    elif raw_action == "hold":
        summary_parts.append("原始融合判断偏中性偏多，更适合持有或等待更优价格。")
    elif raw_action == "reduce":
        summary_parts.append("原始融合判断认为风险敞口偏高，应先控制节奏。")
    else:
        summary_parts.append("原始融合判断认为当前并非高把握度交易窗口。")
    if bull_points:
        summary_parts.append(f"主要支撑：{bull_points[0]}")
    if bear_points:
        summary_parts.append(f"主要压制：{bear_points[0]}")

    return {
        "raw_action": raw_action,
        "raw_summary": " ".join(summary_parts).strip(),
        "bull_points": bull_points,
        "bear_points": bear_points,
        "uncertainties": uncertainties,
        "recommended_horizon": recommended_horizon,
        "event_bias": event_direction,
    }


def fuse_unified_decision(
    *,
    stock_context: dict[str, Any],
    quant_context: dict[str, Any],
    event_context: dict[str, Any],
    user_context: dict[str, Any],
    gate: dict[str, Any],
) -> dict[str, Any]:
    """Fuse stock/quant/event/user evidence into a single decision payload."""
    stock_score = float(stock_context.get("score", 50.0) or 50.0)
    quant_score = float(quant_context.get("score", 50.0) or 50.0)
    event_score = float(event_context.get("score", 50.0) or 50.0)
    gate_adjustment = float(gate.get("gate_adjustment", 0.0) or 0.0)
    style = _normalize_style(gate.get("requested_style"))
    weights = STYLE_WEIGHTS.get(style, STYLE_WEIGHTS["balanced"])

    score_breakdown = {
        "stock_context": round(stock_score, 2),
        "quant": round(quant_score, 2),
        "event": round(event_score, 2),
        "gate_adjustment": round(gate_adjustment, 2),
    }
    weighted_score = (
        stock_score * weights["stock_context"]
        + quant_score * weights["quant"]
        + event_score * weights["event"]
    )
    final_score = round(clamp(weighted_score + gate_adjustment, 0.0, 100.0), 2)

    inputs = [stock_score, quant_score, event_score]
    dispersion = pstdev(inputs) if len(inputs) > 1 else 0.0
    confidence = 0.52 + max(0.0, 18.0 - dispersion) / 100.0
    quant_quality = str(((quant_context.get("confidence_meta") or {}).get("quality") or "")).lower()
    if quant_quality == "high":
        confidence += 0.04
    elif quant_quality == "low":
        confidence -= 0.05
    if any((section.get("data_quality") or {}).get("degraded") for section in (stock_context, quant_context, event_context)):
        confidence -= 0.04
    if gate.get("blocked"):
        confidence -= 0.08
    confidence = round(clamp(confidence, 0.32, 0.93), 4)

    raw_ai_output = _build_raw_ai_output(
        stock_context=stock_context,
        quant_context=quant_context,
        event_context=event_context,
        gate=gate,
    )

    stock_recommendation = str(stock_context.get("recommendation") or "wait")
    veto_reason = gate.get("veto_reason")
    action = _decide_action(final_score, veto_reason, stock_recommendation, raw_ai_output["raw_action"])

    reasons = unique_texts(
        stock_context.get("highlights", [])[:3],
        quant_context.get("reasons", [])[:3],
        event_context.get("reasons", [])[:2],
        raw_ai_output.get("bull_points", [])[:2],
    )[:8]
    risks = unique_texts(
        stock_context.get("risks", [])[:3],
        quant_context.get("risks", [])[:3],
        event_context.get("risks", [])[:3],
        [flag.get("message", "") for flag in gate.get("flags", []) if flag.get("status") != "pass"],
        raw_ai_output.get("bear_points", [])[:2],
    )[:8]

    cap_pct = float(gate.get("position_cap_pct", 0.0) or 0.0)
    if veto_reason or action in {"watch", "sell"}:
        suggested_position_pct = 0.0
    elif final_score >= 82:
        suggested_position_pct = cap_pct
    elif final_score >= 68:
        suggested_position_pct = cap_pct * 0.75
    elif final_score >= 54:
        suggested_position_pct = cap_pct * 0.45
    else:
        suggested_position_pct = cap_pct * 0.20
    suggested_position_pct = round(clamp(suggested_position_pct, 0.0, cap_pct), 4)

    summary_parts = []
    if veto_reason:
        summary_parts.append("统一闸门触发风险阻断，当前不建议直接执行买入动作。")
    elif action == "buy":
        summary_parts.append("多维证据整体偏正面，可以按节奏分批布局。")
    elif action == "hold":
        summary_parts.append("正面证据仍占优，但更适合耐心持有或等待更优入场点。")
    elif action == "reduce":
        summary_parts.append("综合分数偏弱，需控制仓位并降低决策激进度。")
    else:
        summary_parts.append("当前证据尚未形成高把握度交易窗口，建议继续观察。")
    if raw_ai_output.get("raw_summary"):
        summary_parts.append(str(raw_ai_output["raw_summary"]))
    if risks:
        summary_parts.append(f"主要风险集中在：{risks[0]}")

    position_signal = {
        "label": _position_label(suggested_position_pct, action, veto_reason),
        "suggested_position_pct": suggested_position_pct,
        "position_cap_pct": round(cap_pct, 4),
        "requested_style": gate.get("requested_style"),
        "user_risk_level": gate.get("user_risk_level"),
    }

    return {
        "action": action,
        "confidence": confidence,
        "final_score": final_score,
        "summary": " ".join(summary_parts).strip(),
        "reasons": reasons,
        "risks": risks,
        "veto_reason": veto_reason,
        "position_signal": position_signal,
        "score_breakdown": score_breakdown,
        "weights": weights,
        "raw_ai_output": raw_ai_output,
        "fusion_reasoning": {
            "style": style,
            "dispersion": round(dispersion, 4),
            "weighted_score_before_gate": round(weighted_score, 4),
            "gate_adjustment": round(gate_adjustment, 4),
            "confidence": confidence,
        },
        "raw_ai_action": raw_ai_output.get("raw_action"),
        "recommended_horizon": raw_ai_output.get("recommended_horizon"),
    }
