"""K线数据模块"""

import asyncio
import os
import re
import json
import requests
from datetime import datetime, timedelta
from typing import Optional
from ...market.helpers import (
    normalize_code, safe_float, safe_int, parse_date_input,
    run_with_retry as _run_with_retry,
    _parse_timeout_list as _parse_timeout_list,
    KLINE_TIMEOUTS as _KLINE_TIMEOUTS,
    ok, fail
)
from ....core.cache_manager import cached
from ....core.rate_limiter import get_limiter
from ....core.validators import validate_kline_list
from ....data_source import data_source
from ....provider_contracts import attach_provider_contract_meta
from ....storage import get_db
from ....utils import (
    attach_argument_contract_meta,
    resolve_canonical_arg,
    safe_stderr_print,
    validate_stock_code_format,
)
from ...data_quality import build_quality_meta, infer_missing_fields
try:
    from ...baostock_api import baostock_client
except (ImportError, Exception):
    baostock_client = None
try:
    import akshare as ak
except ImportError:
    ak = None
import pandas as pd

_KLINE_TOTAL_TIMEOUT = float(os.getenv("KLINE_TOTAL_TIMEOUT", "45"))
_MINUTE_KLINE_TIMEOUTS = _parse_timeout_list("AKSHARE_MINUTE_KLINE_TIMEOUTS", [4.0, 8.0])
_MINUTE_SINA_TIMEOUT = float(os.getenv("AKSHARE_MINUTE_SINA_TIMEOUT", "6"))


_SOFT_KLINE_FIELDS = frozenset({"turnover", "change_pct"})


_DB_STALE_DAYS = int(os.getenv("KLINE_DB_STALE_DAYS", "5"))

def _is_db_data_fresh(klines: list[dict], max_stale_days: int = _DB_STALE_DAYS) -> bool:
    """检查 DB K 线数据是否足够新鲜（最后一根距今不超过 max_stale_days 天）。"""
    if not klines:
        return False
    last_date = klines[-1].get("date", "") if klines else ""
    if not last_date:
        return False
    try:
        last_dt = datetime.strptime(str(last_date)[:10], "%Y-%m-%d")
        days = (datetime.now() - last_dt).days
        return days <= max_stale_days
    except (ValueError, TypeError):
        return False


def _append_chain_step(chain: list[str], step: str) -> None:
    text = str(step or "").strip()
    if text and text not in chain:
        chain.append(text)


def _kline_row_date(row: dict) -> Optional[datetime]:
    raw_value = str((row or {}).get("date") or "").strip()
    if not raw_value:
        return None
    if len(raw_value) >= 10 and raw_value[4:5] in {"-", "/"}:
        parsed = parse_date_input(raw_value[:10])
    elif len(raw_value) >= 8 and raw_value[:8].isdigit():
        parsed = parse_date_input(raw_value[:8])
    else:
        parsed = parse_date_input(raw_value)
    if parsed is None:
        return None
    return datetime.combine(parsed, datetime.min.time())


def _latest_kline_row(rows: list[dict]) -> dict:
    latest_row: Optional[dict] = None
    latest_date: Optional[datetime] = None
    for item in rows or []:
        if not isinstance(item, dict):
            continue
        row_date = _kline_row_date(item)
        if latest_row is None:
            latest_row = item
        if row_date is not None and (latest_date is None or row_date > latest_date):
            latest_date = row_date
            latest_row = item
    return dict(latest_row or {})


def _kline_missing_fields(rows: list[dict]) -> list[str]:
    latest = _latest_kline_row(rows)
    return infer_missing_fields(
        latest,
        ("date", "open", "close", "high", "low", "volume", "amount", "turnover", "change_pct"),
    )


def _kline_missing_core_fields(rows: list[dict]) -> list[str]:
    return [field for field in _kline_missing_fields(rows) if field not in _SOFT_KLINE_FIELDS]


def _kline_rows_usable(rows: list[dict]) -> bool:
    return bool(rows) and not _kline_missing_core_fields(rows)


def _is_fund_like_code(code: str) -> bool:
    normalized = normalize_code(code)
    return normalized.startswith(("1", "5"))


def _validated_kline_rows(rows: list[dict]) -> list[dict]:
    return validate_kline_list(rows)


def _has_validated_kline_rows(rows: list[dict]) -> bool:
    report = getattr(rows, "validation_report", {}) if rows is not None else {}
    accepted_count = int(report.get("accepted_count") or len(rows or []))
    return accepted_count > 0


def _ok_kline_response(
    rows: list[dict],
    *,
    source: Optional[str] = None,
    source_chain: list[str],
    fallback_reason: Optional[list[str]] = None,
    started_at: Optional[datetime] = None,
) -> dict:
    response = ok(rows)
    latest_row = _latest_kline_row(rows)
    validation_report = getattr(rows, "validation_report", {}) if rows is not None else {}
    accepted_count = validation_report.get("accepted_count")
    rejected_count = validation_report.get("rejected_count")
    minimum_quality_threshold = validation_report.get("minimum_quality_threshold")
    minimum_quality_passed = validation_report.get("minimum_quality_passed")
    resolved_source = str(
        source
        or latest_row.get("source")
        or ((rows or [None])[0] or {}).get("source")
        or "unknown"
    )
    response.update(
        build_quality_meta(
            source=resolved_source,
            source_chain=source_chain,
            fallback_reason=fallback_reason,
            asof_value=latest_row.get("date"),
            missing_fields=_kline_missing_fields(rows),
            degraded=bool(_kline_missing_fields(rows)) or minimum_quality_passed is False,
            success=True,
            started_at=started_at,
            accepted_count=accepted_count,
            rejected_count=rejected_count,
            minimum_quality_threshold=minimum_quality_threshold,
            minimum_quality_passed=minimum_quality_passed,
        )
    )
    response = attach_provider_contract_meta(
        response,
        tool_name="get_kline",
        standard_model="EquityHistorical",
        provider_requested=source_chain[0] if source_chain else "data_source.get_kline",
        provider_used=resolved_source,
        source_chain=source_chain,
        fallback_reason=fallback_reason,
        data_timestamp=latest_row.get("date"),
        freshness={
            "expectation": "daily_kline_t0_to_t1_or_requested_period_snapshot",
            "data_timestamp_field": "latest_bar.date",
        },
    )
    return response


def _fail_kline_response(
    message: str,
    *,
    source_chain: list[str],
    fallback_reason: Optional[list[str]] = None,
    started_at: Optional[datetime] = None,
) -> dict:
    response = fail(message)
    response.update(
        build_quality_meta(
            source="none",
            source_chain=source_chain,
            fallback_reason=fallback_reason or [message],
            asof_value=None,
            missing_fields=[],
            degraded=True,
            success=False,
            started_at=started_at,
        )
    )
    response["source"] = "none"
    return response


def _process_kline_akshare(df: pd.DataFrame, code: str) -> list[dict]:
    results = []
    for _, row in df.iterrows():
        date = str(row.get("日期", ""))[:10]
        open_ = safe_float(row.get("开盘"))
        close = safe_float(row.get("收盘"))
        high = safe_float(row.get("最高"))
        low = safe_float(row.get("最低"))
        if not date or open_ is None or close is None or high is None or low is None:
             continue

        results.append(
            {
                "date": date,
                "open": open_,
                "close": close,
                "high": high,
                "low": low,
                "volume": safe_int(row.get("成交量")),
                "amount": safe_float(row.get("成交额")),
                "turnover": safe_float(row.get("换手率")),
                "change_pct": safe_float(row.get("涨跌幅")),
                "source": "akshare"
            }
        )
    return results
