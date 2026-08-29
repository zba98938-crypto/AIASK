import os
import sqlite3
import time

try:
    import akshare as ak
except ImportError:
    ak = None

from ..utils import (
    attach_argument_contract_meta,
    fail,
    format_period,
    normalize_code,
    ok,
    parse_numeric,
    resolve_canonical_arg,
    resolve_existing_security_code_sync,
)
from ..provider_contracts import attach_tool_provider_contract_meta

# Import optimization modules
from ..core.cache_manager import cached
from ..core.rate_limiter import get_limiter


from typing import Any, Optional, Callable, TypeVar
import sys
from datetime import datetime, timedelta
from ..baostock_api import baostock_client
from ..cache import cache
from ..services.financial_schema import (
    FINANCIAL_PRIMARY_FIELDS as _FINANCIAL_PRIMARY_FIELDS,
    merge_financial_payload,
    normalize_financial_payload,
    financial_gap_summary,
    financial_payload_is_complete,
    financial_payload_is_usable,
    financial_payload_needs_enrichment,
)
from .data_quality import build_quality_meta, infer_missing_fields, normalize_reason_list
from ..date_utils import get_latest_trading_date
from ..data_source import data_source
from ..storage import run_with_db_cleanup
from ..storage.sqlite.schema_base import default_sqlite_path

def _ok_stock_info_degraded(
    payload: dict,
    *,
    source_chain: list[str],
    fallback_reason: Any,
    started_at: datetime | None = None,
) -> dict:
    result = ok(payload)
    result.update(
        build_quality_meta(
            source=source_chain[-1] if source_chain else "none",
            source_chain=source_chain,
            fallback_reason=fallback_reason,
            asof_value=payload.get("listDate") or datetime.now().date().isoformat(),
            missing_fields=infer_missing_fields(
                payload,
                ["name", "industry", "listDate", "totalShares", "floatShares"],
            ),
            degraded=True,
            success=True,
            started_at=started_at,
            accepted_count=1 if payload else 0,
            rejected_count=0,
        )
    )
    return result


def _build_financial_cache_entry(
    payload: dict,
    *,
    source_chain: list[str],
    fallback_reason: Optional[list[str]] = None,
) -> dict:
    normalized_payload = normalize_financial_payload(payload, source_label=payload.get("source") if isinstance(payload, dict) else None)
    return {
        "payload": dict(normalized_payload or payload or {}),
        "source_chain": [str(item).strip() for item in list(source_chain or []) if str(item).strip()],
        "fallback_reason": normalize_reason_list(fallback_reason),
    }


def _read_financial_cache_entry(entry: Any) -> tuple[Optional[dict], list[str], list[str]]:
    if not isinstance(entry, dict):
        return None, [], []

    payload = entry.get("payload")
    if isinstance(payload, dict):
        return (
            dict(payload),
            [str(item).strip() for item in list(entry.get("source_chain") or []) if str(item).strip()],
            normalize_reason_list(entry.get("fallback_reason")),
        )

    payload = dict(entry)
    source_chain = payload.pop("source_chain", None)
    fallback_reason = payload.pop("fallback_reason", None)
    return (
        payload,
        [str(item).strip() for item in list(source_chain or []) if str(item).strip()],
        normalize_reason_list(fallback_reason),
    )


def _financial_missing_fields(payload: Optional[dict]) -> list[str]:
    normalized = normalize_financial_payload(payload, include_aliases=False)
    return infer_missing_fields(
        normalized,
        _FINANCIAL_PRIMARY_FIELDS,
    )


def _ok_financial(
    payload: dict,
    *,
    source_chain: list[str],
    fallback_reason: Optional[list[str]] = None,
    started_at: Optional[datetime] = None,
    cached_result: bool = False,
) -> dict:
    data = normalize_financial_payload(payload, source_label=payload.get("source") if isinstance(payload, dict) else None) or dict(payload or {})
    missing_fields = _financial_missing_fields(data)
    degraded = bool(missing_fields)
    response = ok(data, cached=cached_result)
    response.update(
        build_quality_meta(
            source=str(data.get("source") or "unknown"),
            source_chain=source_chain,
            fallback_reason=fallback_reason,
            asof_value=data.get("reportDate"),
            missing_fields=missing_fields,
            degraded=degraded,
            success=True,
            started_at=started_at,
        )
    )
    return response


def _fail_financial(
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


def _row_non_null_count(row: Any, fields: tuple[str, ...]) -> int:
    count = 0
    for field in fields:
        try:
            value = row.get(field)
        except Exception:
            value = None
        if value is not None and value == value and value != "":
            count += 1
    return count


def _pick_best_statement_row(df: Any, fields: tuple[str, ...], date_field: str = "end_date", scan_limit: int = 6):
    if df is None or getattr(df, "empty", True):
        return None
    try:
        if date_field in getattr(df, "columns", []):
            df = df.sort_values(date_field, ascending=False)
        candidates = df.head(scan_limit)
        best_row = None
        best_key = (-1, "")
        for _, row in candidates.iterrows():
            raw_period = row.get(date_field)
            period = str(raw_period or "")
            score = _row_non_null_count(row, fields)
            key = (score, period)
            if best_row is None or key > best_key:
                best_key = key
                best_row = row
        return best_row if best_row is not None else df.iloc[0]
    except Exception:
        try:
            return df.iloc[0]
        except Exception:
            return None


def _calc_ratio(numerator: Any, denominator: Any, multiplier: float = 100.0) -> Optional[float]:
    num = parse_numeric(numerator)
    den = parse_numeric(denominator)
    if num is None or den in (None, 0):
        return None
    return (num / den) * multiplier


def _first_not_none(*values: Any) -> Any:
    for value in values:
        if value is not None:
            return value
    return None
