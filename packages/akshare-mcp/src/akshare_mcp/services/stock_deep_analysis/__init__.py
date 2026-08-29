"""Unified stock deep-analysis workflow service."""

from __future__ import annotations

import asyncio
from typing import Any
from uuid import uuid4

from ..analysis_integrity_validator import validate_analysis_integrity
from ..artifact_registry import get_artifact_async
from ...storage import get_db
from ...tools.trade_plan import generate_plan
from .artifacts import (
    _load_existing_analysis_input,
    _load_existing_run_summary,
    _persist_artifact,
    compact_run_snapshot,
    find_latest_analysis_run_id,
    get_analysis_report_bundle,
    get_analysis_run_summary,
    get_latest_analysis_summary_for_code,
)
from .constants import ANALYSIS_STRATEGY, ANALYSIS_VERSION
from .context_assembly import (
    _assemble_contexts,
    _safe_decision_summary,
    _safe_financial_payload,
    _safe_profile_payload,
)
from .evidence import _build_evidence_pack, _build_gap_report
from .reporting import _build_report_bundle, _build_task_blocked_payload, _orchestrated_summary
from .shared import _artifact_id, _extract_lineage, _response_data, _stage_result, _utcnow_iso
from .synthesis import _build_agent_review, _build_synthesis, _final_check
from .target_resolution import (
    _existing_target_from_payload,
    _normalize_task,
    _resolve_target as _resolve_target_impl,
    _task_profile,
)


async def _resolve_target(query: str) -> dict[str, Any]:
    return await _resolve_target_impl(query, db_factory=get_db)

async def _rebuild_report_from_artifacts(run_id: str) -> dict[str, Any]:
    summary_artifact, input_artifact, gap_artifact, synthesis_artifact = await asyncio.gather(
        get_artifact_async(run_id),
        get_artifact_async(_artifact_id(run_id, "input")),
        get_artifact_async(_artifact_id(run_id, "gaps")),
        get_artifact_async(_artifact_id(run_id, "synthesis")),
    )
    summary_payload = dict((summary_artifact or {}).get("payload") or {})
    assembled = dict((input_artifact or {}).get("payload") or summary_payload.get("analysis_input") or {})
    gap_report = dict((gap_artifact or {}).get("payload") or summary_payload.get("analysis_gap_report") or {})
    synthesis = dict((synthesis_artifact or {}).get("payload") or summary_payload.get("analysis_synthesis") or {})
    artifact_ids = {
        "run": run_id,
        "input": _artifact_id(run_id, "input"),
        "gaps": _artifact_id(run_id, "gaps"),
        "synthesis": _artifact_id(run_id, "synthesis"),
        "report": _artifact_id(run_id, "report"),
    }
    missing_prerequisites = [
        name
        for name, value in (
            ("analysis_input", assembled),
            ("analysis_synthesis", synthesis),
        )
        if not value
    ]
    if missing_prerequisites:
        blocked_payload = _build_task_blocked_payload(
            run_id=run_id,
            task="rebuild_report",
            assembled=assembled or {"run_id": run_id, "target": _existing_target_from_payload(summary_payload)},
            artifact_ids=artifact_ids,
            reason=f"missing rebuild prerequisites for run_id={run_id}",
            missing_prerequisites=missing_prerequisites,
            gap_report=gap_report,
            summary_seed=summary_payload,
        )
        await _persist_artifact(
            artifact_id=run_id,
            artifact_type="analysis_run_summary",
            code=str((assembled.get("code") if assembled else "") or ((assembled.get("target") if assembled else {}) or {}).get("code") or ""),
            run_id=run_id,
            task="rebuild_report",
            payload=blocked_payload,
        )
        return blocked_payload

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
        task="rebuild_report",
        payload=report_bundle,
    )
    summary_payload = dict(summary_payload)
    summary_payload["analysis_report_bundle"] = report_bundle
    summary_payload["summary"] = dict(summary_payload.get("summary") or {})
    summary_payload["summary"]["report_ready"] = True
    summary_payload["summary"]["updated_at"] = _utcnow_iso()
    summary_payload["task"] = "rebuild_report"
    await _persist_artifact(
        artifact_id=run_id,
        artifact_type="analysis_run_summary",
        code=str(assembled.get("code") or ""),
        run_id=run_id,
        task="rebuild_report",
        payload=summary_payload,
    )
    return summary_payload


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

    requested_run_id = str(run_id or "").strip()
    existing_input = await _load_existing_analysis_input(requested_run_id) if requested_run_id else {}
    target = (
        _existing_target_from_payload(existing_input)
        if requested_run_id and not str(code or "").strip()
        else {}
    )
    if not target:
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
        "name": target.get("name") or str(existing_input.get("name") or ""),
        "investment_style": investment_style or str(existing_input.get("investment_style") or "balanced"),
        "requested_by_user_id": user_id or existing_input.get("requested_by_user_id"),
        "requested_at": _utcnow_iso(),
        "recovery_source_run_id": requested_run_id or None,
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
