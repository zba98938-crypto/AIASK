"""Synthesis helpers for stock deep analysis."""

from __future__ import annotations

from typing import Any

from .shared import _safe_float, _utcnow_iso
from .target_resolution import _task_profile

def _build_agent_review(assembled: dict[str, Any], evidence_pack: dict[str, Any], gap_report: dict[str, Any]) -> dict[str, Any]:
    decision = dict(assembled.get("decision") or {})
    contexts = dict(assembled.get("contexts") or {})
    quant_context = dict(contexts.get("quant") or {})
    event_context = dict(contexts.get("event") or {})

    conflicts: list[str] = []
    action = str(decision.get("action") or "").lower()
    quant_score = _safe_float(quant_context.get("score"))
    event_sentiment = str(event_context.get("sentiment") or "")
    if action == "buy" and quant_score is not None and quant_score < 45:
        conflicts.append("统一决策偏买入，但量化评分偏弱")
    if action == "buy" and event_sentiment == "bearish":
        conflicts.append("统一决策偏买入，但事件情绪偏空")
    if action == "sell" and quant_score is not None and quant_score > 60:
        conflicts.append("统一决策偏卖出，但量化评分仍偏强")

    risks = list(dict.fromkeys(list(decision.get("risks") or [])[:5] + conflicts))
    cited_evidence_ids = [str(item.get("evidence_id")) for item in list(evidence_pack.get("evidence") or [])[:8]]
    blocked = bool(gap_report.get("blocked"))
    verdict = "needs_recovery" if blocked else "pass_with_caution" if conflicts else "pass"
    next_actions = list(gap_report.get("recovery_actions") or [])
    if not next_actions:
        next_actions = [
            "核对估值与财务口径是否与最新财报日期一致",
            "将结论与统一决策摘要一起展示，避免单维度解释",
        ]

    return {
        "run_id": assembled.get("run_id"),
        "reviewer": "aiask_protocol_reviewer_v1",
        "verdict": verdict,
        "cited_evidence_ids": cited_evidence_ids,
        "risks": risks,
        "conflicts": conflicts,
        "next_actions": next_actions,
        "checked_at": _utcnow_iso(),
    }


def _section_evidence_ids(evidence: list[dict[str, Any]], section: str, minimum: int = 1) -> list[str]:
    matched = [str(item.get("evidence_id")) for item in evidence if item.get("section") == section]
    if len(matched) >= minimum:
        return matched[:6]
    return [str(item.get("evidence_id")) for item in evidence[:minimum]]


def _first_matching_statement(evidence: list[dict[str, Any]], section: str, fallback: str) -> str:
    for item in evidence:
        if item.get("section") == section and item.get("statement"):
            return str(item.get("statement"))
    return fallback


def _build_synthesis(assembled: dict[str, Any], evidence_pack: dict[str, Any], gap_report: dict[str, Any], *, task: str) -> dict[str, Any]:
    evidence = list(evidence_pack.get("evidence") or [])
    decision = dict(assembled.get("decision") or {})
    target = dict(assembled.get("target") or {})
    financials = dict(assembled.get("financials") or {})
    contexts = dict(assembled.get("contexts") or {})
    stock_context = dict(contexts.get("stock") or {})
    quant_context = dict(contexts.get("quant") or {})
    event_context = dict(contexts.get("event") or {})

    sections: list[dict[str, Any]] = [
        {
            "key": "overview",
            "title": "标的概览",
            "narrative": f"{target.get('name') or target.get('code')} 当前统一决策为 {decision.get('action') or '待确认'}，"
            f"置信度 {decision.get('confidence') or '未知'}。"
            f" { _first_matching_statement(evidence, 'overview', '需要结合更多事实证据。') }",
            "evidence_ids": _section_evidence_ids(evidence, "overview", minimum=2),
        },
        {
            "key": "valuation",
            "title": "估值结论",
            "narrative": (
                f"资源画像中的 PE 约为 {((assembled.get('profile') or {}).get('stock') or {}).get('pe_ratio') or '未知'}，"
                f"当前估值判断需要和财务质量一起解读。"
            ),
            "evidence_ids": _section_evidence_ids(evidence, "valuation", minimum=1),
        },
        {
            "key": "financial_quality",
            "title": "财务质量",
            "narrative": (
                f"最新财报日期 {financials.get('reportDate') or '未知'}，收入 {financials.get('revenue') or '未知'}，"
                f"净利润 {financials.get('netProfit') or '未知'}，ROE {financials.get('roe') or '未知'}。"
            ),
            "evidence_ids": _section_evidence_ids(evidence, "financial_quality", minimum=2),
        },
        {
            "key": "trend_and_structure",
            "title": "趋势与交易结构",
            "narrative": (
                f"市场评分 {stock_context.get('score') or '未知'}，量化评分 {quant_context.get('score') or '未知'}，"
                f"更适合作为节奏与仓位判断的依据，而不是单独决定方向。"
            ),
            "evidence_ids": _section_evidence_ids(evidence, "trend_and_structure", minimum=2),
        },
        {
            "key": "events_and_catalysts",
            "title": "事件与催化",
            "narrative": (
                f"事件情绪 {event_context.get('sentiment') or '未知'}，事件评分 {event_context.get('score') or '未知'}，"
                f"候选催化包括 {', '.join(str(item) for item in list(event_context.get('candidate_actions') or [])[:3]) or '暂无明确结构化催化'}。"
            ),
            "evidence_ids": _section_evidence_ids(evidence, "events_and_catalysts", minimum=1),
        },
        {
            "key": "risks_and_counterpoints",
            "title": "风险与反证",
            "narrative": (
                "主要风险来自统一决策风险项、事件情绪冲突以及完整性缺口。"
                f" 当前缺口数量 {len(list(gap_report.get('critical_missing') or [])) + len(list(gap_report.get('non_critical_missing') or []))}。"
            ),
            "evidence_ids": _section_evidence_ids(evidence, "risks_and_counterpoints", minimum=1),
        },
        {
            "key": "action_plan",
            "title": "行动建议",
            "narrative": (
                f"建议以 {decision.get('action') or '观察'} 为主线，结合 {decision.get('recommended_horizon') or '当前阶段'} 的持有期，"
                "先确认缺口是否已经关闭，再决定是否升级为正式交易动作。"
            ),
            "evidence_ids": _section_evidence_ids(evidence, "action_plan", minimum=2),
        },
        {
            "key": "evidence_and_gaps",
            "title": "证据与缺口说明",
            "narrative": (
                f"当前 evidence {len(evidence)} 条，完整性状态 {gap_report.get('status')}，"
                f"fallback 标记 {', '.join(list(gap_report.get('fallback_flags') or [])[:4]) or '无'}。"
            ),
            "evidence_ids": [str(item.get("evidence_id")) for item in evidence[:4]],
        },
    ]

    allowed_sections = set(_task_profile(task)["section_keys"])
    filtered_sections = [section for section in sections if section["key"] in allowed_sections]
    digest = (
        f"{target.get('name') or target.get('code')} 的 {task.replace('_', ' ')} 显示统一决策为 "
        f"{decision.get('action') or '待确认'}，市场/量化/事件评分分别为 "
        f"{stock_context.get('score') or '未知'} / {quant_context.get('score') or '未知'} / {event_context.get('score') or '未知'}。"
    )

    return {
        "run_id": assembled.get("run_id"),
        "code": target.get("code"),
        "task": task,
        "digest": digest,
        "sections": filtered_sections,
        "summary": {
            "action": decision.get("action"),
            "confidence": decision.get("confidence"),
            "gap_status": gap_report.get("status"),
        },
    }


def _final_check(synthesis: dict[str, Any]) -> dict[str, Any]:
    missing_citations = [
        str(section.get("key"))
        for section in list(synthesis.get("sections") or [])
        if not list(section.get("evidence_ids") or [])
    ]
    passed = not missing_citations
    return {
        "passed": passed,
        "missing_citations": missing_citations,
        "checked_at": _utcnow_iso(),
    }


def _perspective_cards(assembled: dict[str, Any], gap_report: dict[str, Any]) -> list[dict[str, Any]]:
    contexts = dict(assembled.get("contexts") or {})
    stock_context = dict(contexts.get("stock") or {})
    quant_context = dict(contexts.get("quant") or {})
    event_context = dict(contexts.get("event") or {})
    financials = dict(assembled.get("financials") or {})
    return [
        {"key": "fundamental", "title": "基本面", "value": financials.get("roe"), "note": f"财报日期 {financials.get('reportDate') or '未知'}"},
        {"key": "valuation", "title": "估值", "value": ((assembled.get("profile") or {}).get("stock") or {}).get("pe_ratio"), "note": "优先与财报口径联读"},
        {"key": "trend", "title": "趋势", "value": quant_context.get("score"), "note": "量化上下文评分"},
        {"key": "risk", "title": "风险", "value": len(list(gap_report.get("critical_missing") or [])), "note": "关键缺口数量"},
        {"key": "fund_flow", "title": "资金/情绪", "value": stock_context.get("score"), "note": f"事件情绪 {event_context.get('sentiment') or '未知'}"},
        {"key": "events", "title": "事件催化", "value": event_context.get("score"), "note": "事件上下文评分"},
    ]


def _build_summary_card(assembled: dict[str, Any], synthesis: dict[str, Any], gap_report: dict[str, Any]) -> dict[str, Any]:
    decision = dict(assembled.get("decision") or {})
    target = dict(assembled.get("target") or {})
    profile = dict(assembled.get("profile") or {})
    quote = dict(profile.get("realtime_quote") or {})
    bullets = [
        f"统一决策: {decision.get('action') or '待确认'} / 置信度 {decision.get('confidence') or '未知'}",
        f"实时价格: {quote.get('price') or '未知'}",
        f"完整性状态: {gap_report.get('status')}",
    ]
    return {
        "title": f"{target.get('name') or target.get('code')} {synthesis.get('task')}",
        "subtitle": synthesis.get("digest"),
        "bullets": bullets,
    }
