"""Shared financial payload normalization and merge helpers."""

from __future__ import annotations

from typing import Any, Optional

from ..utils import format_period, normalize_code, parse_numeric

FINANCIAL_PRIMARY_FIELDS = ("reportDate", "revenue", "netProfit", "roe", "debtRatio")
FINANCIAL_COMPLETENESS_FIELDS = (
    "reportDate",
    "revenue",
    "netProfit",
    "roe",
    "debtRatio",
    "eps",
    "currentRatio",
    "bvps",
    "roa",
    "grossProfitMargin",
    "netProfitMargin",
    "revenueGrowth",
    "profitGrowth",
    "operatingCashFlow",
)
FINANCIAL_ENRICHMENT_FIELDS = ("reportDate", "revenue", "netProfit", "roe", "debtRatio", "eps", "currentRatio")
FINANCIAL_ALIAS_MAP: dict[str, tuple[str, ...]] = {
    "reportDate": ("reportDate", "report_date", "end_date", "statDate", "报告期"),
    "revenue": (
        "revenue", "total_revenue", "totalRevenue", "operating_revenue", "operatingRevenue",
        "oper_rev", "main_business_income", "营业总收入", "营业收入",
    ),
    "netProfit": (
        "netProfit", "net_profit", "net_income", "n_income", "profit", "parent_net_profit",
        "n_income_attr_p", "net_profit_atsopc", "归母净利润", "净利润",
    ),
    "grossProfitMargin": (
        "grossProfitMargin", "gross_profit_margin", "grossprofit_margin", "grossMargin", "gross_margin", "销售毛利率",
    ),
    "netProfitMargin": (
        "netProfitMargin", "net_profit_margin", "netprofit_margin", "net_margin", "销售净利率",
    ),
    "roe": ("roe", "roeAvg", "净资产收益率", "净资产收益率-摊薄"),
    "roa": ("roa", "roa_value", "总资产收益率", "总资产报酬率", "总资产净利率", "资产收益率"),
    "debtRatio": ("debtRatio", "debt_ratio", "debt_to_assets", "debtToAssets", "资产负债率"),
    "currentRatio": ("currentRatio", "current_ratio", "流动比率"),
    "eps": ("eps", "basic_eps", "epsTTM", "基本每股收益"),
    "bvps": ("bvps", "book_value_per_share", "每股净资产"),
    "revenueGrowth": ("revenueGrowth", "revenue_growth", "revenue_yoy", "growth_rate", "营收同比"),
    "profitGrowth": ("profitGrowth", "profit_growth", "net_profit_growth", "profit_yoy", "净利润同比"),
    "operatingCashFlow": (
        "operatingCashFlow",
        "operating_cash_flow",
        "net_operate_cash_flow",
        "net_cash_flow_from_operating_activities",
        "cashflow_from_operations",
        "经营活动产生的现金流量净额",
        "经营现金流",
    ),
}
FINANCIAL_SNAKE_EXPORTS: dict[str, str] = {
    "reportDate": "report_date",
    "netProfit": "net_profit",
    "grossProfitMargin": "gross_profit_margin",
    "netProfitMargin": "net_profit_margin",
    "debtRatio": "debt_ratio",
    "currentRatio": "current_ratio",
    "revenueGrowth": "revenue_growth",
    "profitGrowth": "profit_growth",
    "operatingCashFlow": "operating_cash_flow",
}
FINANCIAL_NUMERIC_FIELDS = tuple(field for field in FINANCIAL_ALIAS_MAP if field != "reportDate")


def pick_first_present(payload: dict[str, Any], aliases: tuple[str, ...]) -> tuple[bool, Any, Optional[str]]:
    fallback_value = None
    fallback_alias = None
    for alias in aliases:
        if alias not in payload:
            continue
        value = payload.get(alias)
        if fallback_alias is None:
            fallback_alias = alias
            fallback_value = value
        if value not in (None, ""):
            return True, value, alias
    if fallback_alias is not None:
        return True, fallback_value, fallback_alias
    return False, None, None


def financial_field_state(*, present: bool, value: Any) -> str:
    if not present:
        return "missing"
    if value is None:
        return "null"
    if isinstance(value, (int, float)) and float(value) == 0.0:
        return "present_zero"
    return "present"


def normalize_financial_payload(
    payload: Optional[dict],
    *,
    source_label: Optional[str] = None,
    include_aliases: bool = True,
) -> Optional[dict]:
    if not isinstance(payload, dict):
        return None

    normalized: dict[str, Any] = {}
    field_state: dict[str, str] = {}
    resolved_from: dict[str, Optional[str]] = {}
    null_fields: list[str] = []
    missing_fields: list[str] = []

    code_present, code_raw, _ = pick_first_present(payload, ("code", "stock_code", "symbol", "ticker"))
    if code_present and code_raw not in (None, ""):
        normalized["code"] = normalize_code(str(code_raw))
    elif payload.get("code") not in (None, ""):
        normalized["code"] = normalize_code(str(payload.get("code")))

    for canonical, aliases in FINANCIAL_ALIAS_MAP.items():
        present, raw_value, matched_alias = pick_first_present(payload, aliases)
        if canonical == "reportDate":
            value = format_period(raw_value) if present else None
            if present and value is None and raw_value not in (None, ""):
                value = str(raw_value).strip() or None
        else:
            value = parse_numeric(raw_value) if present else None

        state = financial_field_state(present=present, value=value)
        field_state[canonical] = state
        resolved_from[canonical] = matched_alias
        if state == "missing":
            missing_fields.append(canonical)
        elif state == "null":
            null_fields.append(canonical)
        normalized[canonical] = value

    source_present, source_raw, _ = pick_first_present(payload, ("source", "data_source"))
    normalized["source"] = (
        str(source_raw).strip()
        if source_present and str(source_raw).strip()
        else str(source_label or payload.get("source") or "unknown")
    )

    if include_aliases:
        for canonical, snake_case in FINANCIAL_SNAKE_EXPORTS.items():
            normalized[snake_case] = normalized.get(canonical)

    normalized["data_quality"] = {
        "field_state": field_state,
        "missing_fields": list(missing_fields),
        "null_fields": list(null_fields),
        "normalized_from": resolved_from,
    }
    return normalized


def financial_payload_metrics(payload: Optional[dict]) -> tuple[Optional[dict], dict[str, int]]:
    normalized = normalize_financial_payload(payload, include_aliases=False)
    if not isinstance(normalized, dict):
        return None, {"primary": 0, "enrichment": 0, "total": 0}

    primary_count = sum(1 for field in FINANCIAL_PRIMARY_FIELDS if normalized.get(field) is not None)
    enrichment_count = sum(1 for field in FINANCIAL_ENRICHMENT_FIELDS if normalized.get(field) is not None)
    total_count = sum(1 for field in FINANCIAL_COMPLETENESS_FIELDS if normalized.get(field) is not None)
    return normalized, {
        "primary": primary_count,
        "enrichment": enrichment_count,
        "total": total_count,
    }


def financial_payload_score(payload: Optional[dict]) -> tuple[int, int, int]:
    normalized, metrics = financial_payload_metrics(payload)
    if not isinstance(normalized, dict):
        return (-1, -1, -1)
    return (
        metrics["primary"],
        metrics["enrichment"],
        metrics["total"],
    )


def financial_payload_is_complete(payload: Optional[dict]) -> bool:
    normalized, metrics = financial_payload_metrics(payload)
    if not isinstance(normalized, dict):
        return False
    return normalized.get("reportDate") is not None and metrics["primary"] >= len(FINANCIAL_PRIMARY_FIELDS)


def financial_payload_needs_enrichment(payload: Optional[dict]) -> bool:
    normalized, metrics = financial_payload_metrics(payload)
    if not isinstance(normalized, dict):
        return True
    return normalized.get("reportDate") is None or metrics["primary"] < len(FINANCIAL_PRIMARY_FIELDS)


def financial_payload_is_usable(payload: Optional[dict]) -> bool:
    normalized, metrics = financial_payload_metrics(payload)
    if not isinstance(normalized, dict):
        return False
    has_date = normalized.get("reportDate") is not None
    has_any_ratio = any(
        normalized.get(f) is not None
        for f in ("roe", "debtRatio", "eps", "grossProfitMargin", "netProfitMargin")
    )
    return has_date and (metrics["primary"] >= 3 or has_any_ratio)


def financial_gap_summary(payload: Optional[dict]) -> str:
    normalized = normalize_financial_payload(payload, include_aliases=False)
    if not isinstance(normalized, dict):
        return "no_payload"
    missing = [field for field in FINANCIAL_PRIMARY_FIELDS if normalized.get(field) is None]
    if not missing:
        return "complete"
    preview = ",".join(missing[:4])
    if len(missing) > 4:
        preview += f"(+{len(missing) - 4})"
    return preview


def merge_financial_payload(
    primary: Optional[dict],
    fallback: Optional[dict],
    source_label: Optional[str] = None,
) -> Optional[dict]:
    primary_norm = normalize_financial_payload(primary, source_label=source_label, include_aliases=False)
    fallback_norm = normalize_financial_payload(fallback, source_label=source_label, include_aliases=False)
    if not isinstance(primary_norm, dict) and not isinstance(fallback_norm, dict):
        return None
    if not isinstance(primary_norm, dict):
        merged = dict(fallback_norm or {})
        if merged:
            merged["source"] = str(source_label or merged.get("source") or "unknown")
        return normalize_financial_payload(merged, source_label=source_label)

    primary_score = financial_payload_score(primary_norm)
    fallback_score = financial_payload_score(fallback_norm)
    preferred_source = source_label
    if preferred_source is None:
        fallback_adds_primary = isinstance(fallback_norm, dict) and any(
            primary_norm.get(field) is None and fallback_norm.get(field) is not None
            for field in FINANCIAL_PRIMARY_FIELDS
        )
        if isinstance(fallback_norm, dict) and (fallback_score > primary_score or fallback_adds_primary):
            preferred_source = str(fallback_norm.get("source") or primary_norm.get("source") or "unknown")
        else:
            preferred_source = str(primary_norm.get("source") or (fallback_norm or {}).get("source") or "unknown")

    merged_core = {
        "code": primary_norm.get("code") or (fallback_norm or {}).get("code"),
        "source": preferred_source,
    }
    for field in FINANCIAL_ALIAS_MAP:
        value = primary_norm.get(field)
        if value is None and isinstance(fallback_norm, dict):
            value = fallback_norm.get(field)
        merged_core[field] = value
    return normalize_financial_payload(merged_core, source_label=preferred_source)
