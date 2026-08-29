"""CNINFO/SSE announcement mappers + text cleaners (sliced out)."""

from __future__ import annotations

import hashlib
import html
import re
from datetime import datetime, timezone
from typing import Any

from .event_constants import (
    CNINFO_STATIC_BASE_URL,
    SSE_STATIC_BASE_URL,
    SOURCE_TIER_A,
    SOURCE_TIER_B,
    SOURCE_TIER_C,
    _OFFICIAL_PROVIDER_TOKENS,
    _PAID_PROVIDER_TOKENS,
    _coerce_date_text,
    _TIER_DEFAULT_RELIABILITY,
)

def _clean(value: Any, limit: int = 1000) -> str:
    text = str(value or "").replace("\r", " ").replace("\n", " ").strip()
    return " ".join(text.split())[:limit] if text else ""


def _clean_html(value: Any, limit: int = 1000) -> str:
    text = html.unescape(str(value or ""))
    text = re.sub(r"<[^>]+>", "", text)
    return _clean(text, limit)


def _digits_code(value: Any) -> str:
    digits = "".join(ch for ch in str(value or "") if ch.isdigit())
    return digits[:6] if len(digits) >= 6 else ""


def _normalize_source_tier(value: Any, *, provider: Any = None, source: Any = None) -> str:
    token = str(value or "").strip().lower()
    if token in {"a", "official", "official_disclosure", SOURCE_TIER_A}:
        return SOURCE_TIER_A
    if token in {"b", "institutional", "paid", SOURCE_TIER_B}:
        return SOURCE_TIER_B
    if token in {"c", "media", "open_media", SOURCE_TIER_C}:
        return SOURCE_TIER_C
    provider_text = str(provider or source or "").strip().lower()
    if any(item in provider_text for item in _OFFICIAL_PROVIDER_TOKENS):
        return SOURCE_TIER_A
    if any(item in provider_text for item in _PAID_PROVIDER_TOKENS):
        return SOURCE_TIER_B
    return SOURCE_TIER_C


def _millis_to_date_text(value: Any) -> str:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return _coerce_date_text(value)
    if number <= 0:
        return ""
    try:
        return datetime.fromtimestamp(number / 1000, timezone.utc).date().isoformat()
    except Exception:
        return ""


def _cninfo_pdf_url(value: Any) -> str:
    path = _clean(value, 1000)
    if not path:
        return ""
    if path.startswith(("http://", "https://")):
        return path
    return f"{CNINFO_STATIC_BASE_URL}{path.lstrip('/')}"


def _map_cninfo_announcement(row: dict[str, Any]) -> dict[str, Any] | None:
    code = _digits_code(row.get("secCode"))
    announcement_id = _clean(row.get("announcementId") or row.get("id"), 200)
    title = _clean_html(row.get("announcementTitle") or row.get("shortTitle"), 500)
    if not code or not title:
        return None
    published_at = _millis_to_date_text(row.get("announcementTime") or row.get("storageTime"))
    sec_name = _clean_html(row.get("secName") or row.get("tileSecName"), 120)
    notice_type = _clean_html(row.get("announcementTypeName") or row.get("announcementType"), 200)
    url = _cninfo_pdf_url(row.get("adjunctUrl"))
    original_id = announcement_id or _clean(url or f"{code}:{published_at}:{title}", 240)
    body = " ".join(part for part in (title, sec_name, notice_type) if part)
    return {
        "doc_uid": f"cninfo:{original_id}",
        "title": title,
        "summary": body,
        "content": body,
        "date": published_at,
        "published_at": published_at,
        "evidence_time": published_at,
        "source": "cninfo",
        "source_tier": SOURCE_TIER_A,
        "provider": "cninfo",
        "original_id": original_id,
        "reliability_score": _TIER_DEFAULT_RELIABILITY[SOURCE_TIER_A],
        "crawl_status": "ok",
        "url": url,
        "notice_type": notice_type,
        "code": code,
        "stock_code": code,
        "stock_name": sec_name,
        "cross_source_count": 1,
    }


def _sse_pdf_url(value: Any) -> str:
    path = _clean(value, 1000)
    if not path:
        return ""
    if path.startswith(("http://", "https://")):
        return path
    return f"{SSE_STATIC_BASE_URL.rstrip('/')}/{path.lstrip('/')}"


def _map_sse_announcement(row: dict[str, Any]) -> dict[str, Any] | None:
    code = _digits_code(row.get("SECURITY_CODE") or row.get("securityCode"))
    title = _clean_html(row.get("TITLE") or row.get("title"), 500)
    if not code or not title:
        return None
    published_at = _coerce_date_text(row.get("SSEDATE") or row.get("SSEDate") or row.get("ADDDATE"))
    sec_name = _clean_html(row.get("SECURITY_NAME") or row.get("securityName"), 120)
    notice_type = _clean_html(row.get("BULLETIN_TYPE") or row.get("BULLETIN_HEADING") or row.get("bulletinType"), 200)
    url = _sse_pdf_url(row.get("URL") or row.get("url"))
    original_id = _clean(row.get("BULLETIN_ID") or row.get("file_Serial") or url or f"{code}:{published_at}:{title}", 240)
    body = " ".join(part for part in (title, sec_name, notice_type) if part)
    return {
        "doc_uid": f"sse:{hashlib.sha1(original_id.encode('utf-8')).hexdigest()[:24]}",
        "title": title,
        "summary": body,
        "content": body,
        "date": published_at,
        "published_at": published_at,
        "evidence_time": published_at,
        "source": "sse",
        "source_tier": SOURCE_TIER_A,
        "provider": "sse",
        "original_id": original_id,
        "reliability_score": _TIER_DEFAULT_RELIABILITY[SOURCE_TIER_A],
        "crawl_status": "ok",
        "url": url,
        "notice_type": notice_type,
        "code": code,
        "stock_code": code,
        "stock_name": sec_name,
        "cross_source_count": 1,
    }
