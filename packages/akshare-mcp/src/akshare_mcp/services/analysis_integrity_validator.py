"""Integrity checks for stock deep-analysis artifacts."""

from __future__ import annotations

from typing import Any


DEEP_ANALYSIS_REQUIRED_FIELDS = (
    "target.code",
    "target.name",
    "profile.realtime_quote.price",
    "financials.reportDate",
    "financials.revenue",
    "financials.netProfit",
    "decision.action",
    "decision.confidence",
    "contexts.stock.score",
    "contexts.quant.score",
    "contexts.event.score",
)

QUICK_SCAN_REQUIRED_FIELDS = (
    "target.code",
    "target.name",
    "profile.realtime_quote.price",
    "decision.action",
    "decision.confidence",
)

ENRICHMENT_FIELDS = (
    "financials.roe",
    "financials.grossProfitMargin",
    "contexts.stock.market_snapshot.change_pct",
    "contexts.stock.fund_flow_snapshot.main_net_inflow",
    "contexts.event.sentiment",
)


def _read_path(payload: dict[str, Any], path: str) -> Any:
    current: Any = payload
    for part in path.split("."):
        if not isinstance(current, dict):
            return None
        current = current.get(part)
    return current


def _has_value(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, tuple, set, dict)):
        return len(value) > 0
    return True


def _gap_entry(
    *,
    field: str,
    severity: str,
    message: str,
    recovery_action: str,
) -> dict[str, Any]:
    return {
        "field": field,
        "severity": severity,
        "message": message,
        "recovery_action": recovery_action,
    }


def validate_analysis_integrity(
    payload: dict[str, Any],
    *,
    task: str = "deep_analysis",
) -> dict[str, Any]:
    """Validate that a deep-analysis payload is complete enough to proceed."""

    required_fields = (
        QUICK_SCAN_REQUIRED_FIELDS
        if str(task or "").strip().lower() == "quick_scan"
        else DEEP_ANALYSIS_REQUIRED_FIELDS
    )

    critical_missing: list[dict[str, Any]] = []
    non_critical_missing: list[dict[str, Any]] = []
    for field in required_fields:
        if _has_value(_read_path(payload, field)):
            continue
        critical_missing.append(
            _gap_entry(
                field=field,
                severity="critical",
                message=f"missing required field: {field}",
                recovery_action="retry data assembly and rebuild evidence before continuing",
            )
        )

    enrichment_missing: list[str] = []
    for field in ENRICHMENT_FIELDS:
        if _has_value(_read_path(payload, field)):
            continue
        enrichment_missing.append(field)
        non_critical_missing.append(
            _gap_entry(
                field=field,
                severity="warning",
                message=f"missing enrichment field: {field}",
                recovery_action="mark fallback and keep report narrative conservative",
            )
        )

    contexts = dict(payload.get("contexts") or {})
    fallback_flags: list[str] = []
    warnings: list[str] = []
    for section_name in ("stock", "quant", "event", "user"):
        section = dict(contexts.get(section_name) or {})
        warnings.extend(str(item) for item in list(section.get("warnings") or []) if str(item or "").strip())
        fallback_flags.extend(
            str(item)
            for item in list(section.get("fallback_reason") or [])
            if str(item or "").strip()
        )
        if section.get("degraded"):
            fallback_flags.append(f"{section_name}:degraded")
        missing_fields = [str(item) for item in list(section.get("missing_fields") or []) if str(item or "").strip()]
        for field in missing_fields:
            non_critical_missing.append(
                _gap_entry(
                    field=f"{section_name}.{field}",
                    severity="warning",
                    message=f"context reported missing field: {field}",
                    recovery_action="surface in gap panel and reduce confidence",
                )
            )

    coverage_denominator = max(1, len(required_fields) + len(ENRICHMENT_FIELDS))
    present_required = len(required_fields) - len(critical_missing)
    present_enrichment = len(ENRICHMENT_FIELDS) - len(enrichment_missing)
    completeness_score = round((present_required + present_enrichment) / coverage_denominator, 4)

    blocked = bool(critical_missing)
    recoverable = blocked or bool(non_critical_missing) or bool(fallback_flags)
    status = "blocked" if blocked else "recoverable" if recoverable else "passed"

    recovery_actions = list(
        dict.fromkeys(
            [
                *(item["recovery_action"] for item in critical_missing),
                *(item["recovery_action"] for item in non_critical_missing[:6]),
            ]
        )
    )

    return {
        "status": status,
        "blocked": blocked,
        "recoverable": recoverable,
        "completeness_score": completeness_score,
        "critical_missing": critical_missing,
        "non_critical_missing": non_critical_missing,
        "fallback_flags": sorted(dict.fromkeys(fallback_flags)),
        "warning_count": len(warnings),
        "warnings": sorted(dict.fromkeys(warnings)),
        "required_field_count": len(required_fields),
        "enrichment_field_count": len(ENRICHMENT_FIELDS),
        "recovery_actions": recovery_actions,
    }
