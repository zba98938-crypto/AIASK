"""Shared market-event source constants + shared helpers (sliced out)."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

SOURCE_TIER_A = "tier_a"
SOURCE_TIER_B = "tier_b"
SOURCE_TIER_C = "tier_c"
BRIDGE_SOURCE = "market_events_normalized"
CNINFO_ANNOUNCEMENT_QUERY_URL = "https://www.cninfo.com.cn/new/hisAnnouncement/query"
CNINFO_STATIC_BASE_URL = "https://static.cninfo.com.cn/"
SSE_ANNOUNCEMENT_QUERY_URL = "https://query.sse.com.cn/security/stock/queryCompanyBulletin.do"
SSE_STATIC_BASE_URL = "https://static.sse.com.cn/"


_TIER_DEFAULT_RELIABILITY = {
    SOURCE_TIER_A: 0.92,
    SOURCE_TIER_B: 0.82,
    SOURCE_TIER_C: 0.45,
}
_SINGLE_ANCHOR_CONFIDENCE_CAP = 0.65
_MULTI_SOURCE_CONFIDENCE_CAP = 0.92
_CONFLICT_CONFIDENCE_CAP = 0.55

_OFFICIAL_PROVIDER_TOKENS = (
    "cninfo",
    "sse",
    "szse",
    "bse",
    "csrc",
    "巨潮",
    "上交所",
    "深交所",
    "北交所",
    "证监会",
)
_PAID_PROVIDER_TOKENS = ("wind", "ifind", "choice", "tushare")


def _coerce_date_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).isoformat()
    except Exception:
        pass
    digits = "".join(ch for ch in text if ch.isdigit())
    if len(digits) >= 8:
        return f"{digits[:4]}-{digits[4:6]}-{digits[6:8]}"
    return text[:40]


def _unique_list(values: Any, *, limit: int = 50) -> list[str]:
    out: list[str] = []
    queue = list(values or []) if isinstance(values, (list, tuple, set)) else [values]
    while queue:
        value = queue.pop(0)
        if isinstance(value, (list, tuple, set)):
            queue[:0] = list(value)
            continue
        token = str(value or "").strip()
        if token and token not in out:
            out.append(token)
        if len(out) >= limit:
            break
    return out
