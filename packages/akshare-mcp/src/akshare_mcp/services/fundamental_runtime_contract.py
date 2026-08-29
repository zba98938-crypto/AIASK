"""Build fundamental-runtime contracts for factor-family strategies (P0-b).

quality_factor / value_factor / growth_factor 默认会退化成 ``price_proxy_runtime``,触发
``runtime_family_semantic_mismatch`` + ``proxy_runtime_not_allowed_for_formal_incubation``
阻塞,压回 observe。本模块用真实财务数据(``db.get_financials``)构建一份
``fundamental_runtime_contract``,使 ``dsl_builder._has_true_fundamental_runtime`` 识别为
真实 fundamental runtime,从而 ``runtime_family_data_source=fundamental_runtime`` /
``proxy_runtime_used=False``。

诚实原则:财务数据不足以支撑该家族核心字段时返回 None,保持 proxy(observe),
绝不构建空壳契约伪装 formal。
"""

from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any, Mapping, Optional, Sequence

_FACTOR_FAMILY_TYPES = {"quality_factor", "value_factor", "growth_factor"}

# 每个家族依赖的财务字段,以及"可信"所需的最少非空核心字段数。
_FAMILY_FIELD_SPEC: dict[str, dict[str, Any]] = {
    "quality_factor": {
        "fields": ("roe", "roa", "gross_margin", "net_margin"),
        "min_present": 2,
    },
    "value_factor": {
        "fields": ("eps", "bvps", "debt_ratio", "current_ratio"),
        "min_present": 2,
    },
    "growth_factor": {
        "fields": ("revenue_growth", "profit_growth", "revenue", "net_profit"),
        "min_present": 2,
    },
}


def _to_float(value: Any) -> Optional[float]:
    try:
        if value is None:
            return None
        result = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(result) or math.isinf(result):
        return None
    return result


def build_fundamental_runtime_contract(
    strategy_type: str,
    financials_rows: Sequence[Mapping[str, Any]],
    *,
    code: Optional[str] = None,
) -> Optional[dict[str, Any]]:
    """从最新一期财务数据构建 fundamental_runtime_contract。

    Args:
        strategy_type: 因子家族类型。
        financials_rows: db.get_financials 返回(按 report_date 降序),取最新一期。
        code: 标的代码(可选,写入契约审计)。

    Returns:
        契约 dict;当类型非因子家族或财务数据不足时返回 None(保持 proxy)。
    """
    family = str(strategy_type or "").strip().lower()
    spec = _FAMILY_FIELD_SPEC.get(family)
    if spec is None:
        return None
    rows = [dict(item) for item in (financials_rows or [])]
    if not rows:
        return None
    latest = rows[0]
    fields: tuple[str, ...] = spec["fields"]
    measured_fields: dict[str, float] = {}
    for field in fields:
        value = _to_float(latest.get(field))
        if value is not None:
            measured_fields[field] = round(value, 6)
    if len(measured_fields) < int(spec["min_present"]):
        return None
    report_date = str(latest.get("report_date") or "").strip() or None
    data_quality = "complete" if len(measured_fields) == len(fields) else "partial"
    return {
        "contract_version": "factor.fundamental_runtime_contract.v1",
        "runtime_family_data_source": "fundamental_runtime",
        "strategy_type": family,
        "code": str(code or latest.get("code") or "").strip() or None,
        "factor_inputs": list(fields),
        "measured_fields": measured_fields,
        "report_date": report_date,
        "data_quality": data_quality,
        "source": "db.financials",
        "measured_at": datetime.now(timezone.utc).isoformat(),
    }


async def build_fundamental_runtime_contract_from_db(
    db: Any,
    strategy_type: str,
    code: str,
    *,
    limit: int = 4,
) -> Optional[dict[str, Any]]:
    """拉取 db.get_financials 并构建契约。失败/不足返回 None。"""
    getter = getattr(db, "get_financials", None)
    if getter is None or not str(code or "").strip():
        return None
    try:
        rows = await getter(code, limit=int(limit))
    except Exception:  # noqa: BLE001 - 数据不可用不得阻断
        return None
    return build_fundamental_runtime_contract(strategy_type, rows or [], code=code)
