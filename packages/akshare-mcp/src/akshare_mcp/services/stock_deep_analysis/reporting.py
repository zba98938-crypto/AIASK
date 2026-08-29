"""Reporting helpers for stock deep analysis."""

from __future__ import annotations

from html import escape
from typing import Any

from .shared import _stage_result, _utcnow_iso
from .synthesis import _build_summary_card, _perspective_cards

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


def _build_task_blocked_payload(
    *,
    run_id: str,
    task: str,
    assembled: dict[str, Any],
    artifact_ids: dict[str, str],
    reason: str,
    missing_prerequisites: list[str],
    gap_report: dict[str, Any] | None = None,
    summary_seed: dict[str, Any] | None = None,
) -> dict[str, Any]:
    existing = dict(summary_seed or {})
    base_steps = [dict(item) for item in list(existing.get("steps") or []) if isinstance(item, dict)]
    base_steps.append(
        _stage_result(
            task,
            status="blocked",
            success=False,
            detail={
                "reason": reason,
                "missing_prerequisites": list(missing_prerequisites),
            },
        )
    )
    payload = _orchestrated_summary(
        run_id=run_id,
        task=task,
        status="partial_failed",
        assembled=assembled,
        stages=base_steps,
        evidence_pack=dict(existing.get("analysis_evidence") or {}) or None,
        gap_report=gap_report if gap_report is not None else (dict(existing.get("analysis_gap_report") or {}) or None),
        agent_review=dict(existing.get("analysis_agent_review") or {}) or None,
        synthesis=dict(existing.get("analysis_synthesis") or {}) or None,
        report_bundle=dict(existing.get("analysis_report_bundle") or {}) or None,
        artifact_ids=artifact_ids,
        trade_plan=dict(existing.get("trade_plan") or {}) or None,
    )
    payload["found"] = bool(existing or assembled)
    payload["error"] = reason
    payload["summary"] = dict(payload.get("summary") or {})
    payload["summary"]["report_ready"] = False
    payload["summary"]["error"] = reason
    payload["summary"]["missing_prerequisites"] = list(missing_prerequisites)
    return payload
