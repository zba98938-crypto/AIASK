"""User-facing strategy presentation and ownership state helpers."""

from __future__ import annotations

from typing import Any

from aiask_quant_core.strategy_explanation import build_strategy_explanation


def _string(value: Any) -> str:
    return str(value or "").strip()


def _list(value: Any) -> list[Any]:
    return list(value) if isinstance(value, (list, tuple, set)) else []


def normalize_actor_roles(value: Any) -> list[str]:
    if isinstance(value, str):
        raw = [item.strip().lower() for item in value.split(",")]
    else:
        raw = [str(item or "").strip().lower() for item in _list(value)]
    return [item for item in raw if item]


def is_admin_actor(actor_roles: Any) -> bool:
    roles = set(normalize_actor_roles(actor_roles))
    return "admin" in roles


def is_personal_strategy(strategy: dict[str, Any] | None) -> bool:
    payload = dict(strategy or {})
    tags = {str(item or "").strip().lower() for item in _list(payload.get("tags"))}
    if "personal_strategy" in tags:
        return True
    metadata = dict(dict(payload.get("params") or {}).get("metadata") or {})
    if metadata.get("source_strategy_id"):
        return True
    return _string(payload.get("status")) == "draft"


def build_owner_state(
    strategy: dict[str, Any] | None,
    *,
    actor_id: str | None = None,
    actor_roles: Any = None,
) -> dict[str, Any]:
    payload = dict(strategy or {})
    resolved_actor_id = _string(actor_id)
    author_id = _string(payload.get("author_id"))
    personal = is_personal_strategy(payload)
    owned = bool(resolved_actor_id) and author_id == resolved_actor_id
    admin = is_admin_actor(actor_roles)
    editable = admin or (owned and personal)
    if not resolved_actor_id:
        kind = "anonymous"
    elif admin:
        kind = "operator_admin"
    elif owned and personal:
        kind = "owned_personal_strategy"
    elif owned:
        kind = "owned_strategy"
    else:
        kind = "market_strategy"
    return {
        "kind": kind,
        "owned": owned,
        "editable": editable,
        "author_id": author_id or None,
        "personal_strategy": personal,
        "admin_override": admin,
    }


def build_favorite_state(
    *,
    actor_id: str | None = None,
    is_favorited: bool = False,
) -> dict[str, Any]:
    resolved_actor_id = _string(actor_id)
    return {
        "available": bool(resolved_actor_id),
        "favorited": bool(is_favorited),
        "label": "已收藏" if is_favorited else "未收藏",
    }


def build_paper_session_state(
    session: dict[str, Any] | None,
    *,
    actor_id: str | None = None,
) -> dict[str, Any]:
    payload = dict(session or {})
    resolved_actor_id = _string(actor_id)
    has_session = bool(payload.get("account_id"))
    return {
        "available": bool(resolved_actor_id),
        "has_session": has_session,
        "session_type": _string(payload.get("session_type")) or "personal_paper",
        "account_id": _string(payload.get("account_id")) or None,
        "account_name": _string(payload.get("account_name")) or None,
        "account_status": _string(payload.get("account_status")) or None,
        "mode": "personal-strategy" if has_session else "none",
    }


def build_strategy_presentation(
    strategy: dict[str, Any] | None,
    *,
    owner_state: dict[str, Any] | None = None,
    favorite_state: dict[str, Any] | None = None,
    paper_session_state: dict[str, Any] | None = None,
    overview: dict[str, Any] | None = None,
    report: dict[str, Any] | None = None,
    runtime_control: dict[str, Any] | None = None,
    risk_events: list[dict[str, Any]] | None = None,
    execution_audit_acceptance: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload = dict(strategy or {})
    params = dict(payload.get("params") or {})
    strategy_explanation = dict(
        payload.get("strategy_explanation")
        or params.get("strategy_explanation")
        or {}
    ) or build_strategy_explanation(
        payload,
        source="strategy_lifecycle_presentation",
    )
    owner = dict(owner_state or {})
    favorite = dict(favorite_state or {})
    paper_state = dict(paper_session_state or {})
    overview_payload = dict(overview or {})
    report_payload = dict(report or {})
    report_summary = dict(report_payload.get("summary") or {})
    runtime = dict(runtime_control or {})
    acceptance = dict(execution_audit_acceptance or {})
    blockers = list(acceptance.get("blockers") or [])
    risk_count = len(list(risk_events or []))
    status = _string(payload.get("status")) or "unknown"
    quality_grade = _string(report_summary.get("validation_grade")) or None

    if owner.get("kind") == "owned_personal_strategy" and paper_state.get("has_session"):
        stage_label = "个人测试中"
    elif owner.get("kind") == "owned_personal_strategy":
        stage_label = "个人草稿"
    elif status in {"listed", "published"}:
        stage_label = "可上架"
    elif status in {"incubating", "submitted"}:
        stage_label = "孵化观察"
    else:
        stage_label = "策略观察"

    stage_summary_parts = [
        f"当前状态 {status}",
        f"质量评级 {quality_grade}" if quality_grade else None,
        f"孵化决策 {overview_payload.get('decision')}" if overview_payload.get("decision") else None,
    ]
    stage_summary = "，".join(part for part in stage_summary_parts if part) or "当前仍需结合工厂和运行证据继续判断。"

    why_watch = (
        "这是你的个人策略草稿，可以继续编辑、AI 优化并拉起个人模拟盘测试。"
        if owner.get("kind") == "owned_personal_strategy"
        else "这条策略已经进入公开观察面，可以先看质量门、孵化证据和风险状态，再决定是否收藏或加入组合。"
    )

    current_risks: list[str] = []
    if risk_count > 0:
        current_risks.append(f"当前存在 {risk_count} 条开放风险事件。")
    if blockers:
        current_risks.append(f"执行审计仍有 {len(blockers)} 个阻塞项。")
    if runtime.get("control_mode"):
        current_risks.append(f"运行控制模式为 {runtime.get('control_mode')}。")
    if not current_risks:
        current_risks.append("当前没有发现需要立刻处理的开放风险事件。")

    if owner.get("editable"):
        recommended_action = (
            "继续完善参数后发起个人模拟盘测试。"
            if not paper_state.get("has_session")
            else "打开个人模拟盘测试，观察订单与净值轨迹，再决定是否继续优化。"
        )
    elif favorite.get("favorited"):
        recommended_action = "已收藏，建议继续跟踪工厂审查和运行告警，再决定是否加入组合。"
    else:
        recommended_action = "先收藏或加入组合购物车，再继续比较同类策略。"

    return {
        "stage_label": stage_label,
        "stage_summary": stage_summary,
        "strategy_explanation": strategy_explanation,
        "strategy_summary": strategy_explanation.get("summary"),
        "strategy_labels": list(strategy_explanation.get("labels") or []),
        "why_generated": strategy_explanation.get("why_generated"),
        "why_watch": why_watch,
        "current_risks": current_risks,
        "recommended_action": recommended_action,
    }
