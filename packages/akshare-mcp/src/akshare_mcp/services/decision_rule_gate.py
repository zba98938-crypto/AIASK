"""Rule and suitability gates for unified decision."""

from __future__ import annotations

from typing import Any

from ..tools.managers.compliance_manager import evaluate_order_compliance
from .decision_pipeline_shared import clamp, safe_float


STYLE_POSITION_CAP = {
    "aggressive": 0.35,
    "balanced": 0.20,
    "conservative": 0.10,
}

USER_RISK_CAP = {
    "aggressive": 0.35,
    "moderate": 0.20,
    "conservative": 0.10,
}


def _normalize_style(style: str | None) -> str:
    raw = str(style or "").strip().lower()
    if raw in {"aggressive", "balanced", "conservative"}:
        return raw
    return "balanced"


def _flag(
    name: str,
    status: str,
    severity: str,
    message: str,
    *,
    blocking: bool = False,
    source: str | None = None,
    detail: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "name": name,
        "status": status,
        "severity": severity,
        "blocking": blocking,
        "message": message,
        "source": source,
        "detail": detail or {},
    }


def build_rule_gates(
    *,
    code: str,
    investment_style: str,
    stock_context: dict[str, Any],
    quant_context: dict[str, Any],
    event_context: dict[str, Any],
    user_context: dict[str, Any],
) -> dict[str, Any]:
    """Build non-blocking and blocking decision gates."""
    style = _normalize_style(investment_style)
    user_bucket = str(user_context.get("risk_bucket") or "moderate")
    position_cap_pct = min(STYLE_POSITION_CAP.get(style, 0.20), USER_RISK_CAP.get(user_bucket, 0.20))
    flags: list[dict[str, Any]] = []
    veto_reason: str | None = None
    gate_adjustment = 0.0

    current_price = stock_context.get("current_price") or 1.0
    compliance = evaluate_order_compliance(code, "buy", 100, current_price)
    if compliance.get("blocked"):
        veto_reason = "indicative_order_blocked"
        flags.append(
            _flag(
                "compliance",
                "blocked",
                "high",
                "指示性买入订单未通过合规校验",
                blocking=True,
                source="compliance_manager",
            )
        )
    elif compliance.get("warnings"):
        flags.append(
            _flag(
                "compliance",
                "warn",
                "low",
                str(compliance["warnings"][0]),
                source="compliance_manager",
            )
        )

    market_snapshot = stock_context.get("market_snapshot") or {}
    security_status = stock_context.get("security_status") or {}
    liquidity_score = safe_float(market_snapshot.get("liquidity_score"))
    spread_pct = safe_float(market_snapshot.get("spread_pct"))
    is_st = bool(security_status.get("is_st"))
    suspended = bool(security_status.get("suspended") or market_snapshot.get("suspended"))
    extreme_volatility = bool(market_snapshot.get("extreme_volatility"))

    if is_st:
        veto_reason = veto_reason or "market_gate_st"
        flags.append(
            _flag(
                "market_gate",
                "blocked",
                "critical",
                "标的处于 ST/退市风险状态，统一闸门阻断新增买入",
                blocking=True,
                source="decision_context_builder",
                detail={"reason": "st"},
            )
        )
        gate_adjustment -= 25.0

    if suspended:
        veto_reason = veto_reason or "market_gate_suspension"
        flags.append(
            _flag(
                "market_gate",
                "blocked",
                "high",
                "行情快照显示近似停牌或无成交状态，统一闸门阻断执行",
                blocking=True,
                source="decision_context_builder",
                detail={"reason": "suspended"},
            )
        )
        gate_adjustment -= 18.0

    if liquidity_score is not None:
        liquidity_warn = {"aggressive": 0.24, "balanced": 0.32, "conservative": 0.38}[style]
        if liquidity_score < 0.18:
            veto_reason = veto_reason or "market_gate_liquidity"
            flags.append(
                _flag(
                    "market_gate",
                    "blocked",
                    "high",
                    f"流动性评分仅 {liquidity_score:.2f}，盘口过薄，不适合执行新增仓位",
                    blocking=True,
                    source="decision_context_builder",
                    detail={"reason": "liquidity", "liquidity_score": liquidity_score},
                )
            )
            gate_adjustment -= 16.0
        elif liquidity_score < liquidity_warn:
            flags.append(
                _flag(
                    "market_gate",
                    "warn",
                    "medium",
                    f"流动性评分 {liquidity_score:.2f} 低于 {style} 风格阈值，需降低冲击成本预期",
                    source="decision_context_builder",
                    detail={"reason": "liquidity", "liquidity_score": liquidity_score},
                )
            )
            gate_adjustment -= 6.0
            position_cap_pct *= 0.8

    if spread_pct is not None and spread_pct >= 1.2:
        flags.append(
            _flag(
                "market_gate",
                "warn",
                "medium",
                f"盘口价差 {spread_pct:.2f}% 偏宽，执行成本可能抬升",
                source="decision_context_builder",
                detail={"reason": "spread", "spread_pct": spread_pct},
            )
        )
        gate_adjustment -= 3.0
        position_cap_pct *= 0.9

    if extreme_volatility:
        flags.append(
            _flag(
                "market_gate",
                "warn",
                "medium",
                "标的处于极端波动区间，统一决策已下调试仓比例",
                source="decision_context_builder",
                detail={"reason": "extreme_volatility"},
            )
        )
        gate_adjustment -= 4.0
        position_cap_pct *= 0.85

    if event_context.get("hard_veto_eligible") or event_context.get("hard_risk"):
        top_candidate = ((event_context.get("veto_candidates") or []) or [{}])[0]
        veto_reason = veto_reason or f"event_gate_{top_candidate.get('category') or 'risk'}"
        flags.append(
            _flag(
                "event_gate",
                "blocked",
                "high",
                "事件面识别到强风险事件，统一闸门阻断新增买入",
                blocking=True,
                source="decision_event_builder",
                detail=top_candidate if isinstance(top_candidate, dict) else {},
            )
        )
        gate_adjustment -= 20.0
    elif str(event_context.get("event_intensity") or "") in {"high", "critical"} and str(event_context.get("event_direction") or "") == "bearish":
        flags.append(
            _flag(
                "event_gate",
                "warn",
                "high",
                "事件面偏空且强度较高，需要等待事件不确定性收敛",
                source="decision_event_builder",
                detail={"event_intensity": event_context.get("event_intensity")},
            )
        )
        gate_adjustment -= 8.0
        position_cap_pct *= 0.75

    volatility_20d = stock_context.get("volatility_20d")
    volatility_threshold = {"aggressive": 0.06, "balanced": 0.045, "conservative": 0.03}[style]
    if isinstance(volatility_20d, (int, float)) and volatility_20d > volatility_threshold:
        flags.append(
            _flag(
                "volatility",
                "warn",
                "medium",
                f"20 日波动率 {volatility_20d:.3f} 高于 {style} 风格阈值",
                source="decision_context_builder",
            )
        )
        gate_adjustment -= 6.0
        position_cap_pct *= 0.8

    if style == "aggressive" and user_bucket == "conservative":
        flags.append(
            _flag(
                "user_suitability",
                "warn",
                "medium",
                "请求风格偏激进，但用户画像更接近稳健型",
                source="decision_context_builder",
            )
        )
        gate_adjustment -= 8.0
        position_cap_pct *= 0.7

    weighted_profile = user_context.get("weighted_profile") or {}
    herd_tendency = weighted_profile.get("herd_tendency")
    greed_fear_axis = weighted_profile.get("greed_fear_axis")
    if isinstance(herd_tendency, (int, float)) and herd_tendency >= 0.7:
        flags.append(
            _flag(
                "behavior_bias",
                "warn",
                "medium",
                "用户画像显示从众倾向偏高，建议降低单次试仓幅度",
                source="user_profile_snapshots",
            )
        )
        gate_adjustment -= 4.0
        position_cap_pct *= 0.9
    if isinstance(greed_fear_axis, (int, float)) and abs(greed_fear_axis) >= 0.65:
        flags.append(
            _flag(
                "emotion_bias",
                "warn",
                "low",
                "近期情绪偏离中性，统一决策已主动下调仓位建议",
                source="user_profile_snapshots",
            )
        )
        gate_adjustment -= 2.0
        position_cap_pct *= 0.95

    if safe_float((quant_context.get("confidence_meta") or {}).get("stability_ratio")) is not None and safe_float((quant_context.get("confidence_meta") or {}).get("stability_ratio")) < 0.25:
        flags.append(
            _flag(
                "quant_stability",
                "warn",
                "low",
                "量化证据稳定性偏弱，建议降低对短期统计信号的依赖",
                source="decision_quant_builder",
            )
        )
        gate_adjustment -= 3.0

    if not flags:
        flags.append(
            _flag(
                "baseline",
                "pass",
                "low",
                "未触发额外硬闸门，按统一决策常规流程输出",
                source="decision_rule_gate",
            )
        )

    blocking_flags = [flag for flag in flags if flag.get("blocking")]
    warning_flags = [flag for flag in flags if flag.get("status") == "warn"]
    return {
        "blocked": veto_reason is not None,
        "veto_reason": veto_reason,
        "flags": flags,
        "blocking_flags": blocking_flags,
        "warning_flags": warning_flags,
        "gate_adjustment": round(gate_adjustment, 2),
        "position_cap_pct": round(clamp(float(position_cap_pct), 0.0, 0.35), 4),
        "requested_style": style,
        "user_risk_level": user_context.get("risk_level"),
        "market_gate": {
            "is_st": is_st,
            "suspended": suspended,
            "liquidity_score": liquidity_score,
            "spread_pct": spread_pct,
            "extreme_volatility": extreme_volatility,
        },
        "event_gate": {
            "event_direction": event_context.get("event_direction"),
            "event_intensity": event_context.get("event_intensity"),
            "hard_veto_eligible": bool(event_context.get("hard_veto_eligible")),
            "veto_candidates": list(event_context.get("veto_candidates") or [])[:3],
        },
        "gate_summary": {
            "blocking_count": len(blocking_flags),
            "warning_count": len(warning_flags),
            "position_cap_pct": round(clamp(float(position_cap_pct), 0.0, 0.35), 4),
        },
    }
