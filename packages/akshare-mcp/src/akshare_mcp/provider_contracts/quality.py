"""Report-only provider quality gates for AIASK financial tool metadata."""

from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any

from .base import dedupe_text


def _dict_or_empty(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _result_meta(result: dict[str, Any]) -> dict[str, Any]:
    return _dict_or_empty(result.get("meta"))


def _result_success(result: dict[str, Any]) -> bool:
    if "success" in result:
        return bool(result.get("success"))
    return result.get("error") in (None, "")


def _result_data(result: dict[str, Any]) -> Any:
    return result.get("data") if isinstance(result, dict) else None


def _source_chain(result: dict[str, Any]) -> list[str]:
    meta = _result_meta(result)
    return dedupe_text(
        result.get("source_chain")
        or result.get("fallback_chain")
        or meta.get("source_chain")
        or [result.get("source") or meta.get("source")]
    )


def _parse_timestamp(value: Any) -> datetime | None:
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%Y%m%d"):
        try:
            parsed = datetime.strptime(text.replace("+08:00", "+0800"), fmt)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed
        except Exception:
            continue
    try:
        parsed = datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed
    except Exception:
        return None


def _data_timestamp(result: dict[str, Any]) -> Any:
    meta = _result_meta(result)
    for candidate in (
        result.get("data_timestamp"),
        result.get("asof_time"),
        meta.get("data_timestamp"),
        meta.get("asof_time"),
    ):
        if candidate:
            return candidate
    data = _result_data(result)
    if isinstance(data, dict):
        for key in ("tradeDate", "trade_date", "date", "reportDate", "listDate"):
            if data.get(key):
                return data.get(key)
    if isinstance(data, list) and data and isinstance(data[0], dict):
        for key in ("tradeDate", "trade_date", "date", "reportDate"):
            if data[0].get(key):
                return data[0].get(key)
    return None


def _finite_numeric_scan(value: Any, *, limit: int = 200) -> tuple[bool, int]:
    checked = 0
    stack = [value]
    while stack and checked < limit:
        item = stack.pop()
        if isinstance(item, dict):
            stack.extend(item.values())
            continue
        if isinstance(item, list):
            stack.extend(item)
            continue
        if isinstance(item, (int, float)):
            checked += 1
            if isinstance(item, float) and not math.isfinite(item):
                return False, checked
    return True, checked


def evaluate_provider_quality_gate(result: dict[str, Any], contract: dict[str, Any] | None) -> dict[str, Any]:
    """Evaluate non-blocking quality checks against an existing tool result."""

    contract = contract or {}
    freshness = _dict_or_empty(contract.get("freshness"))
    source_policy = _dict_or_empty(contract.get("source_policy"))
    expected_sources = dedupe_text(source_policy.get("priority") if isinstance(source_policy.get("priority"), list) else [])
    actual_sources = _source_chain(result)
    success = _result_success(result)
    data = _result_data(result)
    checks: list[dict[str, Any]] = []

    checks.append(
        {
            "name": "schema_completeness",
            "passed": bool(success and data is not None),
            "severity": "error" if success and data is None else "info",
        }
    )

    timestamp = _data_timestamp(result)
    parsed_timestamp = _parse_timestamp(timestamp)
    max_stale = freshness.get("max_stale_seconds")
    # P1-3.5 fix: freshness_sla must NOT silent-pass when data_timestamp is null.
    # Previous behavior: freshness_passed defaulted to True regardless of timestamp availability,
    # which made the check meaningless for any source missing data_timestamp (e.g. north_fund 21mo stale).
    # New behavior: when timestamp cannot be verified, mark passed=False with severity='warning'
    # and emit cannot_verify_freshness flag so AI agents/clients can see the data is unverified.
    freshness_passed: bool
    age_seconds = None
    cannot_verify = False
    if parsed_timestamp is not None and isinstance(max_stale, int):
        age_seconds = max(0, int((datetime.now(timezone.utc) - parsed_timestamp.astimezone(timezone.utc)).total_seconds()))
        freshness_passed = age_seconds <= max_stale
    elif isinstance(max_stale, int):
        # contract expected a max_stale_seconds bound but no parseable timestamp was provided.
        freshness_passed = False
        cannot_verify = True
    else:
        # no freshness contract at all (e.g. static metadata tools); treat as not applicable, pass.
        freshness_passed = True
    freshness_check = {
        "name": "freshness_sla",
        "passed": freshness_passed,
        "severity": "warning" if not freshness_passed else "info",
        "data_timestamp": str(timestamp) if timestamp else None,
        "age_seconds": age_seconds,
        "max_stale_seconds": max_stale,
    }
    if cannot_verify:
        freshness_check["cannot_verify_freshness"] = True
        freshness_check["reason"] = "data_timestamp_missing_or_unparseable"
    checks.append(freshness_check)

    degraded = bool(result.get("degraded") or _result_meta(result).get("degraded"))
    fallback_used = bool(result.get("fallback_used") or len(actual_sources) > 1)
    checks.append(
        {
            "name": "fallback_degraded_flag",
            "passed": not degraded,
            "severity": "warning" if degraded or fallback_used else "info",
            "degraded": degraded,
            "fallback_used": fallback_used,
        }
    )

    checks.append(
        {
            "name": "source_availability",
            "passed": bool(actual_sources or expected_sources),
            "severity": "error" if not (actual_sources or expected_sources) else "info",
            "actual_sources": actual_sources,
            "expected_sources": expected_sources,
        }
    )

    numeric_ok, numeric_count = _finite_numeric_scan(data)
    checks.append(
        {
            "name": "numeric_sanity",
            "passed": numeric_ok,
            "severity": "error" if not numeric_ok else "info",
            "checked_numeric_values": numeric_count,
        }
    )

    mismatch = bool(expected_sources and actual_sources and actual_sources[0] not in expected_sources)
    reconciliation = {
        "mode": "sampled_report_only",
        "enabled": len(expected_sources) > 1,
        "primary_expected": expected_sources[0] if expected_sources else None,
        "actual_primary": actual_sources[0] if actual_sources else None,
        "mismatch": mismatch,
    }
    if reconciliation["enabled"]:
        checks.append(
            {
                "name": "multi_source_reconciliation",
                "passed": not mismatch,
                "severity": "warning" if mismatch else "info",
                **reconciliation,
            }
        )

    blocking = bool(_dict_or_empty(contract.get("quality_gate")).get("blocking"))
    failed = [item for item in checks if not item.get("passed") and item.get("severity") == "error"]
    warnings = [item for item in checks if not item.get("passed") and item.get("severity") == "warning"]
    status = "failed" if failed else ("degraded" if warnings or degraded or fallback_used else "passed")
    return {
        "status": status,
        "mode": "blocking" if blocking else "report_only",
        "blocking": blocking,
        "standard_model": contract.get("standard_model"),
        "checks": checks,
        "failed_checks": [item["name"] for item in failed],
        "warning_checks": [item["name"] for item in warnings],
        "reconciliation": reconciliation,
    }
