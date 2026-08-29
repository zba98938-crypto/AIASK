"""Unified stock deep-analysis workflow service."""
from __future__ import annotations

# --- stock_deep_analysis_parts/context.py ---
import asyncio
from datetime import datetime, timezone
from html import escape
from typing import Any
from uuid import uuid4

from ..storage import get_db
from ..tools.finance import get_financials
from ..tools.manager_protocol import LINEAGE_REFERENCE_KEYS
from ..tools.trade_plan import generate_plan
from ..utils import normalize_code
from .analysis_integrity_validator import validate_analysis_integrity
from .artifact_registry import get_artifact_async, list_artifacts_async, register_artifact_async
from .decision_context_builder import build_stock_context, build_user_context
from .decision_contracts import get_unified_decision_summary_payload
from .decision_event_builder import build_event_context
from .decision_quant_builder import build_quant_context

ANALYSIS_STRATEGY = "stock_deep_analysis"
ANALYSIS_VERSION = "stock-deep-analysis.v1"
SUPPORTED_ANALYSIS_TASKS = {"quick_scan", "deep_analysis", "recover_gaps", "rebuild_report", "trade_plan"}
_SUMMARY_ONLY_FIELDS = (
    "run_id",
    "task",
    "status",
    "code",
    "name",
    "market",
    "current_stage",
    "report_ready",
    "digest",
    "gap_count",
    "artifact_ids",
    "resource_uris",
    "updated_at",
)


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _artifact_id(run_id: str, suffix: str | None = None) -> str:
    return run_id if not suffix else f"{run_id}:{suffix}"


def _safe_float(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number:
        return None
    return number


def _response_data(response: Any) -> dict[str, Any]:
    if not isinstance(response, dict):
        return {}
    data = response.get("data")
    return dict(data) if isinstance(data, dict) else {}


def _extract_lineage(payload: dict[str, Any] | None) -> dict[str, Any]:
    source = dict(payload or {})
    lineage: dict[str, Any] = {}
    for key in LINEAGE_REFERENCE_KEYS:
        value = source.get(key)
        if value not in (None, "", []):
            lineage[key] = value
    explicit = source.get("lineage")
    if isinstance(explicit, dict):
        for key, value in explicit.items():
            if value not in (None, "", []):
                lineage.setdefault(str(key), value)
    return lineage


def _stage_result(stage: str, *, status: str, success: bool, detail: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "stage": stage,
        "status": status,
        "success": success,
        "detail": dict(detail or {}),
        "updated_at": _utcnow_iso(),
    }


def _task_profile(task: str) -> dict[str, Any]:
    normalized = str(task or "deep_analysis").strip().lower()
    if normalized == "quick_scan":
        return {
            "task": normalized,
            "include_report": True,
            "include_trade_plan": False,
            "section_keys": ["overview", "trend_and_structure", "risks_and_counterpoints", "action_plan"],
            "title": "Quick Scan",
        }
    if normalized == "trade_plan":
        return {
            "task": normalized,
            "include_report": True,
            "include_trade_plan": True,
            "section_keys": [
                "overview",
                "trend_and_structure",
                "events_and_catalysts",
                "risks_and_counterpoints",
                "action_plan",
            ],
            "title": "Trade Plan",
        }
    return {
        "task": normalized if normalized in SUPPORTED_ANALYSIS_TASKS else "deep_analysis",
        "include_report": normalized != "recover_gaps",
        "include_trade_plan": False,
        "section_keys": [
            "overview",
            "valuation",
            "financial_quality",
            "trend_and_structure",
            "events_and_catalysts",
            "risks_and_counterpoints",
            "action_plan",
            "evidence_and_gaps",
        ],
        "title": "Deep Analysis",
    }


def _normalize_task(task: str | None) -> str:
    normalized = str(task or "deep_analysis").strip().lower()
    return normalized if normalized in SUPPORTED_ANALYSIS_TASKS else "deep_analysis"


async def _resolve_target(query: str) -> dict[str, Any]:
    raw = str(query or "").strip()
    normalized = normalize_code(raw)
    if len(normalized) == 6 and normalized.isdigit():
        return {
            "query": raw,
            "resolved": True,
            "code": normalized,
            "name": "",
            "resolution_mode": "direct_code",
            "candidates": [],
        }

    if not raw:
        return {
            "query": raw,
            "resolved": False,
            "code": "",
            "name": "",
            "resolution_mode": "empty_query",
            "candidates": [],
        }

    db = get_db()
    rows: list[dict[str, Any]] = []
    try:
        async with db.acquire() as conn:
            fetched = await conn.fetch(
                """
                SELECT code, stock_name, industry, market_cap
                FROM stocks
                WHERE stock_name LIKE $1
                   OR code LIKE $2
                   OR (industry IS NOT NULL AND industry LIKE $1)
                ORDER BY
                  CASE WHEN stock_name = $3 THEN 0 ELSE 1 END,
                  market_cap DESC NULLS LAST
                LIMIT 8
                """,
                f"%{raw}%",
                f"%{raw}%",
                raw,
            )
            rows = [dict(row) for row in fetched]
    except Exception:
        rows = []

    candidates = [
        {
            "code": str(item.get("code") or ""),
            "name": str(item.get("stock_name") or item.get("name") or ""),
            "industry": item.get("industry"),
            "market_cap": _safe_float(item.get("market_cap")),
        }
        for item in rows
        if str(item.get("code") or "").strip()
    ]

    exact_name = [item for item in candidates if item.get("name") == raw]
    if len(exact_name) == 1:
        selected = exact_name[0]
        return {
            "query": raw,
            "resolved": True,
            "code": selected["code"],
            "name": selected["name"],
            "resolution_mode": "exact_name_match",
            "candidates": candidates,
        }

    if len(candidates) == 1:
        selected = candidates[0]
        return {
            "query": raw,
            "resolved": True,
            "code": selected["code"],
            "name": selected["name"],
            "resolution_mode": "single_candidate",
            "candidates": candidates,
        }

    return {
        "query": raw,
        "resolved": False,
        "code": "",
        "name": "",
        "resolution_mode": "ambiguous_name" if candidates else "not_found",
        "candidates": candidates,
    }


async def _persist_artifact(
    *,
    artifact_id: str,
    artifact_type: str,
    code: str,
    run_id: str,
    task: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    return await register_artifact_async(
        {
            "artifact_id": artifact_id,
            "artifact_type": artifact_type,
            "strategy": ANALYSIS_STRATEGY,
            "strategy_version": ANALYSIS_VERSION,
            "code": code,
            "run_id": run_id,
            "task": task,
            "payload": payload,
            "lineage": {"run_id": run_id, "security_code": code},
        }
    )


async def _assemble_contexts(code: str, user_id: str | None) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]]:
    async def _wrap(coro, fallback: dict[str, Any]) -> dict[str, Any]:
        try:
            result = await coro
            return dict(result or {})
        except Exception as exc:
            payload = dict(fallback)
            payload.setdefault("warnings", []).append(f"{type(exc).__name__}: {exc}")
            payload.setdefault("fallback_reason", []).append(f"{type(exc).__name__}: {exc}")
            payload["degraded"] = True
            return payload

    stock_context, quant_context, event_context, user_context = await asyncio.gather(
        _wrap(build_stock_context(code), {"code": code, "score": None, "warnings": [], "fallback_reason": []}),
        _wrap(build_quant_context(code), {"code": code, "score": None, "warnings": [], "fallback_reason": []}),
        _wrap(build_event_context(code), {"code": code, "score": None, "warnings": [], "fallback_reason": []}),
        _wrap(build_user_context(user_id), {"user_id": user_id, "warnings": [], "fallback_reason": [], "degraded": True}),
    )
    return stock_context, quant_context, event_context, user_context


async def _safe_profile_payload(code: str) -> dict[str, Any]:
    try:
        from ...resources.stock_and_watchlist import build_stock_profile_resource_payload

        return await build_stock_profile_resource_payload(code)
    except Exception as exc:
        return {
            "uri": f"resource://stock/{code}/profile",
            "code": code,
            "found": False,
            "error": f"{type(exc).__name__}: {exc}",
            "stock": {"code": code, "name": ""},
            "realtime_quote": {},
        }


async def _safe_financial_payload(code: str) -> dict[str, Any]:
    try:
        result = await get_financials(code)
        return dict(result or {})
    except Exception as exc:
        return {
            "success": False,
            "error": f"{type(exc).__name__}: {exc}",
            "data": {},
        }


async def _safe_decision_summary(code: str, investment_style: str, user_id: str | None) -> dict[str, Any]:
    try:
        return await get_unified_decision_summary_payload(
            code=code,
            investment_style=investment_style,
            user_id=user_id,
        )
    except Exception as exc:
        return {
            "code": code,
            "action": "wait",
            "confidence": None,
            "summary": "统一决策摘要构建失败，已降级为等待确认。",
            "reasons": [],
            "risks": [f"{type(exc).__name__}: {exc}"],
            "fallback_reason": [f"{type(exc).__name__}: {exc}"],
        }


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

# --- stock_deep_analysis_parts/specs.py ---
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


def _build_manifest(
    *,
    run_id: str,
    code: str,
    task: str,
    status: str,
    artifact_ids: dict[str, str],
    gap_report: dict[str, Any],
) -> dict[str, Any]:
    return {
        "run_id": run_id,
        "code": code,
        "task": task,
        "status": status,
        "artifact_ids": artifact_ids,
        "generated_at": _utcnow_iso(),
        "gap_status": gap_report.get("status"),
        "critical_gap_count": len(list(gap_report.get("critical_missing") or [])),
        "warning_gap_count": len(list(gap_report.get("non_critical_missing") or [])),
    }

# --- stock_deep_analysis_parts/runtime.py ---
def _render_report_html(
    *,
    target: dict[str, Any],
    summary_card: dict[str, Any],
    sections: list[dict[str, Any]],
    perspective_cards: list[dict[str, Any]],
    manifest: dict[str, Any],
) -> str:
    bullet_items = "".join(f"<li>{escape(str(item))}</li>" for item in list(summary_card.get("bullets") or []))
    section_html = "".join(
        (
            f"<section class='section'>"
            f"<h2>{escape(str(section.get('title') or ''))}</h2>"
            f"<p>{escape(str(section.get('narrative') or ''))}</p>"
            f"<div class='evidence'>Evidence: {escape(', '.join(str(item) for item in list(section.get('evidence_ids') or [])))}</div>"
            f"</section>"
        )
        for section in sections
    )
    cards_html = "".join(
        (
            "<div class='card'>"
            f"<h3>{escape(str(card.get('title') or ''))}</h3>"
            f"<strong>{escape(str(card.get('value') or '-'))}</strong>"
            f"<p>{escape(str(card.get('note') or ''))}</p>"
            "</div>"
        )
        for card in perspective_cards
    )
    return (
        "<!doctype html><html><head><meta charset='utf-8' />"
        f"<title>{escape(str(target.get('name') or target.get('code') or 'Stock Analysis'))}</title>"
        "<style>"
        "body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#f3f7fb;color:#0f172a;"
        "margin:0;padding:36px;} .wrap{max-width:1120px;margin:0 auto;} .hero{padding:28px;border-radius:24px;"
        "background:linear-gradient(135deg,#fef7ed,#eff6ff);box-shadow:0 16px 48px rgba(15,23,42,.08);} h1{margin:0 0 8px;"
        "font-size:34px;} .grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:14px;margin:22px 0;}"
        ".card,.section{background:#fff;border:1px solid rgba(148,163,184,.18);border-radius:20px;padding:18px;"
        "box-shadow:0 12px 36px rgba(15,23,42,.06);} .sections{display:grid;gap:16px;margin-top:18px;} .evidence{margin-top:10px;"
        "font-size:12px;color:#475569;} ul{margin:10px 0 0 18px;} .manifest{margin-top:18px;font-size:12px;color:#475569;}"
        "</style></head><body><div class='wrap'><div class='hero'>"
        f"<div class='eyebrow'>{escape(str(target.get('code') or ''))} / 机构风格个股分析</div>"
        f"<h1>{escape(str(target.get('name') or target.get('code') or ''))}</h1>"
        f"<p>{escape(str(summary_card.get('subtitle') or ''))}</p>"
        f"<ul>{bullet_items}</ul>"
        "</div><div class='grid'>"
        f"{cards_html}</div><div class='sections'>{section_html}</div>"
        f"<div class='manifest'>Manifest: {escape(str(manifest))}</div>"
        "</div></body></html>"
    )


def _build_report_bundle(
    assembled: dict[str, Any],
    synthesis: dict[str, Any],
    gap_report: dict[str, Any],
    artifact_ids: dict[str, str],
    *,
    status: str,
) -> dict[str, Any]:
    target = dict(assembled.get("target") or {})
    perspective_cards = _perspective_cards(assembled, gap_report)
    summary_card = _build_summary_card(assembled, synthesis, gap_report)
    manifest = _build_manifest(
        run_id=str(assembled.get("run_id") or ""),
        code=str(target.get("code") or ""),
        task=str(synthesis.get("task") or ""),
        status=status,
        artifact_ids=artifact_ids,
        gap_report=gap_report,
    )
    standalone_html = _render_report_html(
        target=target,
        summary_card=summary_card,
        sections=list(synthesis.get("sections") or []),
        perspective_cards=perspective_cards,
        manifest=manifest,
    )
    return {
        "run_id": assembled.get("run_id"),
        "code": target.get("code"),
        "task": synthesis.get("task"),
        "summary_card": summary_card,
        "one_paragraph_digest": synthesis.get("digest"),
        "perspective_cards": perspective_cards,
        "sections": list(synthesis.get("sections") or []),
        "standalone_html": standalone_html,
        "manifest": manifest,
    }


def _orchestrated_summary(
    *,
    run_id: str,
    task: str,
    status: str,
    assembled: dict[str, Any],
    stages: list[dict[str, Any]],
    evidence_pack: dict[str, Any] | None,
    gap_report: dict[str, Any] | None,
    agent_review: dict[str, Any] | None,
    synthesis: dict[str, Any] | None,
    report_bundle: dict[str, Any] | None,
    artifact_ids: dict[str, str],
    trade_plan: dict[str, Any] | None = None,
) -> dict[str, Any]:
    current_stage = next((stage["stage"] for stage in reversed(stages) if stage.get("status")), "requested")
    target = dict(assembled.get("target") or {})
    result = {
        "task": task,
        "status": status,
        "steps": stages,
        "summary": {
            "run_id": run_id,
            "code": target.get("code"),
            "name": target.get("name") or assembled.get("name"),
            "market": assembled.get("market", "cn"),
            "current_stage": current_stage,
            "report_ready": bool(report_bundle),
            "digest": (report_bundle or {}).get("one_paragraph_digest") or (synthesis or {}).get("digest"),
            "gap_count": len(list((gap_report or {}).get("critical_missing") or []))
            + len(list((gap_report or {}).get("non_critical_missing") or [])),
            "artifact_ids": artifact_ids,
            "resource_uris": {
                "latest_stock": f"resource://stock/{target.get('code')}/deep-analysis" if target.get("code") else None,
                "summary": f"resource://analysis-run/{run_id}/summary",
                "report": f"resource://analysis-run/{run_id}/report",
            },
            "updated_at": _utcnow_iso(),
        },
        "run_id": run_id,
        "code": target.get("code"),
        "name": target.get("name") or assembled.get("name"),
        "market": assembled.get("market", "cn"),
        "analysis_input": assembled,
        "analysis_evidence": evidence_pack,
        "analysis_gap_report": gap_report,
        "analysis_agent_review": agent_review,
        "analysis_synthesis": synthesis,
        "analysis_report_bundle": report_bundle,
        "trade_plan": trade_plan,
    }
    return result


async def _rebuild_report_from_artifacts(run_id: str) -> dict[str, Any]:
    summary_artifact, input_artifact, gap_artifact, synthesis_artifact = await asyncio.gather(
        get_artifact_async(run_id),
        get_artifact_async(_artifact_id(run_id, "input")),
        get_artifact_async(_artifact_id(run_id, "gaps")),
        get_artifact_async(_artifact_id(run_id, "synthesis")),
    )
    if not summary_artifact or not input_artifact or not synthesis_artifact:
        raise ValueError(f"missing rebuild prerequisites for run_id={run_id}")

    assembled = dict(input_artifact.get("payload") or {})
    gap_report = dict((gap_artifact or {}).get("payload") or {})
    synthesis = dict(synthesis_artifact.get("payload") or {})
    artifact_ids = {
        "run": run_id,
        "input": _artifact_id(run_id, "input"),
        "gaps": _artifact_id(run_id, "gaps"),
        "synthesis": _artifact_id(run_id, "synthesis"),
        "report": _artifact_id(run_id, "report"),
    }
    report_bundle = _build_report_bundle(
        assembled,
        synthesis,
        gap_report,
        artifact_ids,
        status="completed",
    )
    await _persist_artifact(
        artifact_id=_artifact_id(run_id, "report"),
        artifact_type="analysis_report_bundle",
        code=str(assembled.get("code") or ""),
        run_id=run_id,
        task=str(assembled.get("task") or "rebuild_report"),
        payload=report_bundle,
    )
    summary_payload = dict(summary_artifact.get("payload") or {})
    summary_payload["analysis_report_bundle"] = report_bundle
    summary_payload["summary"] = dict(summary_payload.get("summary") or {})
    summary_payload["summary"]["report_ready"] = True
    summary_payload["summary"]["updated_at"] = _utcnow_iso()
    await _persist_artifact(
        artifact_id=run_id,
        artifact_type="analysis_run_summary",
        code=str(assembled.get("code") or ""),
        run_id=run_id,
        task=str(assembled.get("task") or "rebuild_report"),
        payload=summary_payload,
    )
    return summary_payload

# --- stock_deep_analysis_parts/postprocess.py ---
async def run_stock_deep_analysis(
    *,
    code: str = "",
    task: str = "deep_analysis",
    user_id: str | None = None,
    investment_style: str = "balanced",
    market: str = "cn",
    run_id: str | None = None,
) -> dict[str, Any]:
    normalized_task = _normalize_task(task)
    if normalized_task == "rebuild_report":
        resolved_run_id = str(run_id or "").strip()
        if not resolved_run_id:
            raise ValueError("run_id is required for rebuild_report")
        return await _rebuild_report_from_artifacts(resolved_run_id)

    target = await _resolve_target(code)
    resolved_code = str(target.get("code") or normalize_code(code))
    resolved_run_id = str(run_id or f"stock-analysis-run-{resolved_code or 'unknown'}-{uuid4().hex[:12]}")
    profile = _task_profile(normalized_task)
    stages: list[dict[str, Any]] = []
    artifact_ids = {
        "run": resolved_run_id,
        "input": _artifact_id(resolved_run_id, "input"),
        "evidence": _artifact_id(resolved_run_id, "evidence"),
        "gaps": _artifact_id(resolved_run_id, "gaps"),
        "review": _artifact_id(resolved_run_id, "review"),
        "synthesis": _artifact_id(resolved_run_id, "synthesis"),
        "report": _artifact_id(resolved_run_id, "report"),
        "trade_plan": _artifact_id(resolved_run_id, "trade_plan"),
    }

    assembled = {
        "version": ANALYSIS_VERSION,
        "run_id": resolved_run_id,
        "task": normalized_task,
        "market": market,
        "target": target,
        "code": resolved_code,
        "name": target.get("name") or "",
        "investment_style": investment_style,
        "requested_by_user_id": user_id,
        "requested_at": _utcnow_iso(),
        "resource_uris": {
            "profile": f"resource://stock/{resolved_code}/profile" if resolved_code else None,
            "latest_stock": f"resource://stock/{resolved_code}/deep-analysis" if resolved_code else None,
        },
    }
    await _persist_artifact(
        artifact_id=artifact_ids["input"],
        artifact_type="analysis_input",
        code=resolved_code,
        run_id=resolved_run_id,
        task=normalized_task,
        payload=assembled,
    )

    if not target.get("resolved"):
        stages.append(
            _stage_result(
                "target_resolution",
                status="blocked",
                success=False,
                detail={
                    "resolution_mode": target.get("resolution_mode"),
                    "candidate_count": len(list(target.get("candidates") or [])),
                },
            )
        )
        gap_report = {
            "run_id": resolved_run_id,
            "code": resolved_code,
            "status": "blocked",
            "blocked": True,
            "critical_missing": [
                {
                    "field": "target.code",
                    "severity": "critical",
                    "message": f"unable to resolve stock target: {target.get('resolution_mode')}",
                    "recovery_action": "ask user to confirm one candidate code before continuing",
                }
            ],
            "non_critical_missing": [],
            "fallback_flags": [],
            "recovery_actions": ["确认股票代码或从候选列表中选择一个标的"],
            "candidates": list(target.get("candidates") or []),
            "checked_at": _utcnow_iso(),
        }
        await _persist_artifact(
            artifact_id=artifact_ids["gaps"],
            artifact_type="analysis_gap_report",
            code=resolved_code,
            run_id=resolved_run_id,
            task=normalized_task,
            payload=gap_report,
        )
        payload = _orchestrated_summary(
            run_id=resolved_run_id,
            task=normalized_task,
            status="partial_failed",
            assembled=assembled,
            stages=stages,
            evidence_pack=None,
            gap_report=gap_report,
            agent_review=None,
            synthesis=None,
            report_bundle=None,
            artifact_ids=artifact_ids,
        )
        await _persist_artifact(
            artifact_id=resolved_run_id,
            artifact_type="analysis_run_summary",
            code=resolved_code,
            run_id=resolved_run_id,
            task=normalized_task,
            payload=payload,
        )
        return payload

    stages.append(_stage_result("target_resolution", status="completed", success=True, detail={"code": resolved_code}))

    profile_payload, financial_payload, contexts_bundle, decision_summary = await asyncio.gather(
        _safe_profile_payload(resolved_code),
        _safe_financial_payload(resolved_code),
        _assemble_contexts(resolved_code, user_id),
        _safe_decision_summary(resolved_code, investment_style, user_id),
    )
    stock_context, quant_context, event_context, user_context = contexts_bundle

    assembled.update(
        {
            "name": str(target.get("name") or (profile_payload.get("stock") or {}).get("name") or ""),
            "profile": profile_payload,
            "financials": _response_data(financial_payload),
            "financials_meta": {
                "success": bool(financial_payload.get("success")) if isinstance(financial_payload, dict) else False,
                "source": (financial_payload or {}).get("source") if isinstance(financial_payload, dict) else None,
            },
            "decision": decision_summary,
            "contexts": {
                "stock": stock_context,
                "quant": quant_context,
                "event": event_context,
                "user": user_context,
            },
        }
    )
    if not str(target.get("name") or "").strip():
        resolved_name = str(
            assembled.get("name")
            or (decision_summary.get("name") if isinstance(decision_summary, dict) else "")
            or ""
        ).strip()
        if resolved_name:
            target["name"] = resolved_name
            assembled["name"] = resolved_name
    stages.append(
        _stage_result(
            "data_assembly",
            status="completed",
            success=True,
            detail={
                "profile_found": bool(profile_payload.get("found")),
                "financials_present": bool(assembled["financials"]),
                "decision_action": decision_summary.get("action"),
            },
        )
    )
    await _persist_artifact(
        artifact_id=artifact_ids["input"],
        artifact_type="analysis_input",
        code=resolved_code,
        run_id=resolved_run_id,
        task=normalized_task,
        payload=assembled,
    )

    evidence_pack = _build_evidence_pack(assembled, task=normalized_task)
    stages.append(
        _stage_result(
            "evidence_normalization",
            status="completed",
            success=True,
            detail={"evidence_count": (evidence_pack.get("summary") or {}).get("count")},
        )
    )
    await _persist_artifact(
        artifact_id=artifact_ids["evidence"],
        artifact_type="analysis_evidence",
        code=resolved_code,
        run_id=resolved_run_id,
        task=normalized_task,
        payload=evidence_pack,
    )

    integrity = validate_analysis_integrity(assembled, task=normalized_task)
    gap_report = _build_gap_report(
        run_id=resolved_run_id,
        code=resolved_code,
        integrity=integrity,
        target=target,
    )
    stages.append(
        _stage_result(
            "integrity_gate",
            status=str(integrity.get("status")),
            success=not bool(integrity.get("blocked")),
            detail={
                "completeness_score": integrity.get("completeness_score"),
                "critical_gap_count": len(list(integrity.get("critical_missing") or [])),
            },
        )
    )
    await _persist_artifact(
        artifact_id=artifact_ids["gaps"],
        artifact_type="analysis_gap_report",
        code=resolved_code,
        run_id=resolved_run_id,
        task=normalized_task,
        payload=gap_report,
    )

    if integrity.get("blocked") and normalized_task != "recover_gaps":
        payload = _orchestrated_summary(
            run_id=resolved_run_id,
            task=normalized_task,
            status="partial_failed",
            assembled=assembled,
            stages=stages,
            evidence_pack=evidence_pack,
            gap_report=gap_report,
            agent_review=None,
            synthesis=None,
            report_bundle=None,
            artifact_ids=artifact_ids,
        )
        await _persist_artifact(
            artifact_id=resolved_run_id,
            artifact_type="analysis_run_summary",
            code=resolved_code,
            run_id=resolved_run_id,
            task=normalized_task,
            payload=payload,
        )
        return payload

    agent_review = _build_agent_review(assembled, evidence_pack, gap_report)
    stages.append(
        _stage_result(
            "agent_review",
            status=agent_review.get("verdict", "pass"),
            success=agent_review.get("verdict") != "needs_recovery",
            detail={"conflict_count": len(list(agent_review.get("conflicts") or []))},
        )
    )
    await _persist_artifact(
        artifact_id=artifact_ids["review"],
        artifact_type="analysis_agent_review",
        code=resolved_code,
        run_id=resolved_run_id,
        task=normalized_task,
        payload=agent_review,
    )

    synthesis = _build_synthesis(assembled, evidence_pack, gap_report, task=normalized_task)
    stages.append(
        _stage_result(
            "synthesis",
            status="completed",
            success=True,
            detail={"section_count": len(list(synthesis.get("sections") or []))},
        )
    )
    await _persist_artifact(
        artifact_id=artifact_ids["synthesis"],
        artifact_type="analysis_synthesis",
        code=resolved_code,
        run_id=resolved_run_id,
        task=normalized_task,
        payload=synthesis,
    )

    final_check = _final_check(synthesis)
    stages.append(
        _stage_result(
            "final_check",
            status="passed" if final_check.get("passed") else "blocked",
            success=bool(final_check.get("passed")),
            detail=final_check,
        )
    )
    report_bundle: dict[str, Any] | None = None
    trade_plan_payload: dict[str, Any] | None = None

    if final_check.get("passed") and profile.get("include_report"):
        report_bundle = _build_report_bundle(
            assembled,
            synthesis,
            gap_report,
            artifact_ids,
            status="completed",
        )
        await _persist_artifact(
            artifact_id=artifact_ids["report"],
            artifact_type="analysis_report_bundle",
            code=resolved_code,
            run_id=resolved_run_id,
            task=normalized_task,
            payload=report_bundle,
        )
        stages.append(_stage_result("report_render", status="completed", success=True, detail={"has_html": True}))

    if profile.get("include_trade_plan"):
        trade_plan_payload = await generate_plan(
            resolved_code,
            capital=1_000_000,
            risk_per_trade=0.02,
            style=investment_style,
        )
        await _persist_artifact(
            artifact_id=artifact_ids["trade_plan"],
            artifact_type="trade_plan_bundle",
            code=resolved_code,
            run_id=resolved_run_id,
            task=normalized_task,
            payload=dict(trade_plan_payload or {}),
        )
        stages.append(_stage_result("trade_plan", status="completed", success=True, detail={"source": "generate_trade_plan"}))

    overall_status = "completed" if final_check.get("passed") and (report_bundle or not profile.get("include_report")) else "partial_failed"
    payload = _orchestrated_summary(
        run_id=resolved_run_id,
        task=normalized_task,
        status=overall_status,
        assembled=assembled,
        stages=stages,
        evidence_pack=evidence_pack,
        gap_report=gap_report,
        agent_review=agent_review,
        synthesis=synthesis,
        report_bundle=report_bundle,
        artifact_ids=artifact_ids,
        trade_plan=trade_plan_payload,
    )
    await _persist_artifact(
        artifact_id=resolved_run_id,
        artifact_type="analysis_run_summary",
        code=resolved_code,
        run_id=resolved_run_id,
        task=normalized_task,
        payload=payload,
    )
    return payload


async def get_analysis_run_summary(run_id: str) -> dict[str, Any]:
    artifact = await get_artifact_async(str(run_id or "").strip())
    if artifact:
        payload = dict(artifact.get("payload") or {})
        if payload:
            return payload
    return {
        "run_id": str(run_id or "").strip(),
        "found": False,
        "error": f"analysis run not found: {run_id}",
    }


async def get_analysis_report_bundle(run_id: str) -> dict[str, Any]:
    artifact = await get_artifact_async(_artifact_id(str(run_id or "").strip(), "report"))
    if artifact:
        payload = dict(artifact.get("payload") or {})
        if payload:
            return payload
    return {
        "run_id": str(run_id or "").strip(),
        "found": False,
        "error": f"analysis report not found: {run_id}",
    }


async def find_latest_analysis_run_id(code: str) -> str | None:
    resolved_code = normalize_code(code)
    if not resolved_code:
        return None
    rows = await list_artifacts_async(limit=200, strategy=ANALYSIS_STRATEGY)
    for row in rows:
        artifact_id = str((row or {}).get("artifact_id") or "").strip()
        if not artifact_id or ":" in artifact_id:
            continue
        if str((row or {}).get("code") or "").strip() != resolved_code:
            continue
        return artifact_id
    return None


async def get_latest_analysis_summary_for_code(code: str) -> dict[str, Any]:
    latest_run_id = await find_latest_analysis_run_id(code)
    if not latest_run_id:
        resolved_code = normalize_code(code)
        return {
            "code": resolved_code,
            "found": False,
            "error": f"no deep analysis run found for {resolved_code}",
        }
    payload = await get_analysis_run_summary(latest_run_id)
    if payload.get("found") is False:
        return payload
    return payload


def compact_run_snapshot(payload: dict[str, Any]) -> dict[str, Any]:
    return {field: payload.get(field) for field in _SUMMARY_ONLY_FIELDS if field in payload}

