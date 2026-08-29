"""Extracted advisory-oriented skill workflows."""

from __future__ import annotations

from typing import Any, Dict

from . import skills_support as skill_support


def _skill_support():
    return skill_support


def exec_investor_protection(params: Dict[str, Any]) -> Dict[str, Any]:
    skill_support = _skill_support()

    task = str(params.get("task") or "protection_brief").strip().lower()
    supported_tasks = ["protection_brief", "audit_log", "smoke_test"]
    if task not in supported_tasks:
        return skill_support._unsupported_task_result(task, supported_tasks)

    region = str(params.get("region") or "CN").strip().upper()
    broker_region = str(params.get("broker_region") or region).strip().upper()
    protection_scope = {
        "region": region,
        "broker_region": broker_region,
        "protected_items": [
            "Custody/process failures under the applicable investor protection regime",
            "Disclosure and account-operation checks before acting on recommendations",
        ],
        "not_protected_items": [
            "Normal market loss and strategy drawdown",
            "Guarantees of profit or timing certainty",
        ],
    }
    audit_payload = {
        "user_intent": str(params.get("user_intent") or "investor_education"),
        "recommendation_context": dict(params.get("recommendation_context") or {}),
        "retention_rule": "Record recommendation rationale, risk boundary, and non-protected items together.",
    }
    steps = [
        skill_support._static_step("explain_protection_scope", protection_scope),
        skill_support._static_step(
            "explain_risk_boundary",
            {
                "core_message": "Investor protection does not replace diversification, risk budgeting, or suitability checks.",
                "next_actions": [
                    "Verify broker/legal entity",
                    "Review custody and claims process",
                    "Confirm loss-bearing capacity",
                ],
            },
        ),
    ]
    if task in {"audit_log", "smoke_test"}:
        steps.append(skill_support._static_step("prepare_recommendation_audit_payload", audit_payload))
    result = skill_support._finalize_skill_result(task, steps)
    result["summary"]["region"] = region
    return result


def exec_ips_discipline(params: Dict[str, Any]) -> Dict[str, Any]:
    skill_support = _skill_support()

    task = str(params.get("task") or "draft_ips").strip().lower()
    supported_tasks = ["draft_ips", "discipline_checklist", "smoke_test"]
    if task not in supported_tasks:
        return skill_support._unsupported_task_result(task, supported_tasks)

    ips_draft = {
        "goal": str(params.get("goal") or "Grow capital within explicit drawdown limits"),
        "horizon_years": max(1.0, skill_support._safe_float(params.get("horizon_years"), 5.0)),
        "risk_profile": str(params.get("risk_profile") or "balanced").strip().lower(),
        "max_drawdown": max(0.05, min(skill_support._safe_float(params.get("max_drawdown"), 0.18), 0.50)),
        "liquidity_need": str(params.get("liquidity_need") or "medium"),
        "rebalance_frequency": str(params.get("rebalance_frequency") or "monthly"),
        "rebalance_threshold": skill_support._normalize_rebalance_threshold(
            params.get("rebalance_threshold"),
            0.08,
        ),
        "behavior_rules": [
            "No ad-hoc position doubling after a loss",
            "Any exception to IPS must be documented with reason and expiry",
            "New strategies require a review window before capital increase",
        ],
    }
    steps = [
        skill_support._static_step("collect_ips_constraints", ips_draft),
        skill_support._static_step(
            "draft_behavior_discipline",
            {
                "discipline_checklist": [
                    "Target and constraint fields filled",
                    "Risk budget and rebalance trigger recorded",
                    "Temporary override rule documented",
                ]
            },
        ),
    ]
    result = skill_support._finalize_skill_result(task, steps)
    result["summary"]["ips_draft"] = ips_draft
    return result
