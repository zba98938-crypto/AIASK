"""Shared market evidence normalization helpers.

These helpers intentionally stay dependency-light so host adapters can reuse
the same market fact gate semantics.
"""

from __future__ import annotations

from typing import Any, Optional

HARD_MARKET_FACT_METRICS = frozenset(
    {
        "close",
        "close_price",
        "收盘价",
        "change_pct",
        "pct_change",
        "涨跌幅",
        "amplitude",
        "amplitude_pct",
        "振幅",
        "turnover",
        "turnover_rate",
        "换手",
        "换手率",
    }
)
_SAME_DAY_WINDOW_SCOPES = frozenset({"same_day", "1d", "daily", "session"})
_INVALID_UNIT_MARKERS = frozenset({"unknown", "mixed", "inconsistent"})


def normalize_market_evidence_fact(item: Optional[dict[str, Any]]) -> dict[str, Any]:
    payload = dict(item or {})
    metric = str(payload.get("metric") or "").strip()
    trade_date = str(payload.get("trade_date") or "").strip() or None
    source_as_of_date = str(payload.get("source_as_of_date") or "").strip() or None
    window_scope = str(payload.get("window_scope") or "").strip().lower() or "same_day"
    unit = str(payload.get("unit") or "").strip() or None
    if payload.get("same_day_pass") is None:
        same_day_pass = bool(trade_date and source_as_of_date and trade_date == source_as_of_date)
    else:
        same_day_pass = bool(payload.get("same_day_pass"))
    if payload.get("unit_pass") is None:
        unit_pass = str(unit or "").strip().lower() not in _INVALID_UNIT_MARKERS
    else:
        unit_pass = bool(payload.get("unit_pass"))
    cross_window = window_scope not in _SAME_DAY_WINDOW_SCOPES
    hard_fact_eligible = bool(
        metric
        and metric.lower() in {value.lower() for value in HARD_MARKET_FACT_METRICS}
        and same_day_pass
        and unit_pass
        and not cross_window
    )
    downgrade_reason = str(payload.get("downgrade_reason") or "").strip() or None
    if not downgrade_reason and not hard_fact_eligible:
        if not same_day_pass:
            downgrade_reason = "non_same_day_source"
        elif not unit_pass:
            downgrade_reason = "unit_mismatch"
        elif cross_window:
            downgrade_reason = "cross_window_metric"
        else:
            downgrade_reason = "unsupported_metric_for_hard_fact"
    return {
        **payload,
        "metric": metric or None,
        "trade_date": trade_date,
        "source_as_of_date": source_as_of_date,
        "window_scope": window_scope,
        "unit": unit,
        "same_day_pass": same_day_pass,
        "unit_pass": unit_pass,
        "hard_fact_eligible": hard_fact_eligible,
        "downgrade_reason": downgrade_reason,
    }


def normalize_market_evidence_facts(*values: Any) -> list[dict[str, Any]]:
    facts: list[dict[str, Any]] = []
    for value in values:
        items = value if isinstance(value, list) else []
        for item in items:
            if not isinstance(item, dict):
                continue
            fact = normalize_market_evidence_fact(item)
            if fact.get("metric"):
                facts.append(fact)
    return facts


def build_market_fact_gate_audit(market_facts: list[dict[str, Any]]) -> dict[str, Any]:
    hard_fact_count = sum(1 for item in market_facts if item.get("hard_fact_eligible") is True)
    degraded = [item for item in market_facts if item.get("hard_fact_eligible") is not True]
    degraded_fact_count = len(degraded)
    evidence_debt_reasons = list(
        dict.fromkeys(
            str(item.get("downgrade_reason") or "").strip()
            for item in degraded
            if str(item.get("downgrade_reason") or "").strip()
        )
    )
    if not market_facts:
        status = "missing"
    elif hard_fact_count > 0 and degraded_fact_count == 0:
        status = "hard_facts_only"
    elif hard_fact_count > 0:
        status = "mixed_with_degraded"
    else:
        status = "degraded_only"
    return {
        "market_fact_gate_status": status,
        "hard_fact_count": hard_fact_count,
        "degraded_fact_count": degraded_fact_count,
        "evidence_debt_reasons": evidence_debt_reasons,
    }


def summarize_market_fact_gate(*values: Any) -> dict[str, Any]:
    market_facts = normalize_market_evidence_facts(*values)
    return {
        "market_facts": market_facts,
        **build_market_fact_gate_audit(market_facts),
    }
