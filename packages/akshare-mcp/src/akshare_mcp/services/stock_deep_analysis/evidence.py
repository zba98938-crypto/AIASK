"""Evidence and gap-report helpers for stock deep analysis."""

from __future__ import annotations

from typing import Any

from .constants import ANALYSIS_VERSION
from .shared import _utcnow_iso

def _append_evidence(
    evidence: list[dict[str, Any]],
    *,
    section: str,
    label: str,
    statement: str,
    value: Any,
    source: str,
    source_field: str,
    kind: str = "fact",
) -> None:
    if value in (None, "", [], {}):
        return
    evidence_id = f"ev{len(evidence) + 1:02d}"
    evidence.append(
        {
            "evidence_id": evidence_id,
            "section": section,
            "kind": kind,
            "label": label,
            "statement": statement,
            "value": value,
            "source": source,
            "source_field": source_field,
        }
    )


def _build_evidence_pack(assembled: dict[str, Any], *, task: str) -> dict[str, Any]:
    evidence: list[dict[str, Any]] = []
    target = dict(assembled.get("target") or {})
    profile = dict(assembled.get("profile") or {})
    profile_stock = dict(profile.get("stock") or {})
    quote = dict(profile.get("realtime_quote") or {})
    financials = dict(assembled.get("financials") or {})
    decision = dict(assembled.get("decision") or {})
    contexts = dict(assembled.get("contexts") or {})
    stock_context = dict(contexts.get("stock") or {})
    quant_context = dict(contexts.get("quant") or {})
    event_context = dict(contexts.get("event") or {})

    _append_evidence(
        evidence,
        section="overview",
        label="实时价格",
        statement=f"最新价格约为 {quote.get('price')}",
        value=quote.get("price"),
        source="stock_profile",
        source_field="realtime_quote.price",
    )
    _append_evidence(
        evidence,
        section="valuation",
        label="市盈率",
        statement=f"资源画像中的 PE 为 {profile_stock.get('pe_ratio')}",
        value=profile_stock.get("pe_ratio"),
        source="stock_profile",
        source_field="stock.pe_ratio",
    )
    _append_evidence(
        evidence,
        section="financial_quality",
        label="营业收入",
        statement=f"最新财报收入为 {financials.get('revenue')}",
        value=financials.get("revenue"),
        source="financials",
        source_field="revenue",
    )
    _append_evidence(
        evidence,
        section="financial_quality",
        label="净利润",
        statement=f"最新财报净利润为 {financials.get('netProfit')}",
        value=financials.get("netProfit"),
        source="financials",
        source_field="netProfit",
    )
    _append_evidence(
        evidence,
        section="financial_quality",
        label="ROE",
        statement=f"ROE 为 {financials.get('roe')}",
        value=financials.get("roe"),
        source="financials",
        source_field="roe",
    )
    _append_evidence(
        evidence,
        section="trend_and_structure",
        label="市场上下文评分",
        statement=f"市场上下文评分为 {stock_context.get('score')}",
        value=stock_context.get("score"),
        source="stock_context",
        source_field="score",
    )
    _append_evidence(
        evidence,
        section="trend_and_structure",
        label="主力资金净流入",
        statement=f"主力资金净流入为 {((stock_context.get('fund_flow_snapshot') or {}).get('main_net_inflow'))}",
        value=((stock_context.get("fund_flow_snapshot") or {}).get("main_net_inflow")),
        source="stock_context",
        source_field="fund_flow_snapshot.main_net_inflow",
    )
    _append_evidence(
        evidence,
        section="trend_and_structure",
        label="量化评分",
        statement=f"量化上下文评分为 {quant_context.get('score')}",
        value=quant_context.get("score"),
        source="quant_context",
        source_field="score",
    )
    _append_evidence(
        evidence,
        section="events_and_catalysts",
        label="事件情绪",
        statement=f"事件情绪为 {event_context.get('sentiment')}",
        value=event_context.get("sentiment"),
        source="event_context",
        source_field="sentiment",
    )
    _append_evidence(
        evidence,
        section="events_and_catalysts",
        label="事件评分",
        statement=f"事件上下文评分为 {event_context.get('score')}",
        value=event_context.get("score"),
        source="event_context",
        source_field="score",
    )
    _append_evidence(
        evidence,
        section="action_plan",
        label="统一决策动作",
        statement=f"统一决策建议为 {decision.get('action')}",
        value=decision.get("action"),
        source="unified_decision",
        source_field="action",
        kind="inference",
    )
    _append_evidence(
        evidence,
        section="action_plan",
        label="统一决策置信度",
        statement=f"统一决策置信度为 {decision.get('confidence')}",
        value=decision.get("confidence"),
        source="unified_decision",
        source_field="confidence",
        kind="inference",
    )

    if str(task) != "quick_scan":
        for reason in list(decision.get("reasons") or [])[:4]:
            _append_evidence(
                evidence,
                section="overview",
                label="决策理由",
                statement=str(reason),
                value=str(reason),
                source="unified_decision",
                source_field="reasons",
                kind="inference",
            )
        for risk in list(decision.get("risks") or [])[:4]:
            _append_evidence(
                evidence,
                section="risks_and_counterpoints",
                label="决策风险",
                statement=str(risk),
                value=str(risk),
                source="unified_decision",
                source_field="risks",
                kind="inference",
            )

    return {
        "version": ANALYSIS_VERSION,
        "code": target.get("code"),
        "task": task,
        "evidence": evidence,
        "summary": {
            "count": len(evidence),
            "sections": sorted(dict.fromkeys(item["section"] for item in evidence)),
            "fact_count": sum(1 for item in evidence if item.get("kind") == "fact"),
            "inference_count": sum(1 for item in evidence if item.get("kind") == "inference"),
        },
    }


def _build_gap_report(
    *,
    run_id: str,
    code: str,
    integrity: dict[str, Any],
    target: dict[str, Any],
) -> dict[str, Any]:
    resolution_message = None
    if not target.get("resolved"):
        mode = str(target.get("resolution_mode") or "unresolved")
        candidate_count = len(list(target.get("candidates") or []))
        resolution_message = f"target resolution blocked: {mode}, candidates={candidate_count}"

    return {
        "run_id": run_id,
        "code": code,
        "status": integrity.get("status"),
        "blocked": bool(integrity.get("blocked")),
        "resolution_message": resolution_message,
        "critical_missing": list(integrity.get("critical_missing") or []),
        "non_critical_missing": list(integrity.get("non_critical_missing") or []),
        "fallback_flags": list(integrity.get("fallback_flags") or []),
        "recovery_actions": list(integrity.get("recovery_actions") or []),
        "checked_at": _utcnow_iso(),
    }

