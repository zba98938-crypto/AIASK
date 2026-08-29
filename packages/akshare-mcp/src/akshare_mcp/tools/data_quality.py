"""Shared response-level data quality metadata helpers."""

from __future__ import annotations

import re
from datetime import date, datetime, time, timezone
from typing import Any, Iterable, Optional

STALE_AFTER_SEC = 24 * 60 * 60


def normalize_reason_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        text = value.strip()
        return [text] if text else []
    reasons: list[str] = []
    for item in list(value or []):
        text = str(item or "").strip()
        if text and text not in reasons:
            reasons.append(text)
    return reasons


def _parse_quarter_string(value: str) -> Optional[datetime]:
    text = str(value or "").strip().upper()
    match = re.fullmatch(r"(\d{4})[- ]?Q([1-4])", text)
    if not match:
        return None
    year = int(match.group(1))
    quarter = int(match.group(2))
    month_day = {
        1: (3, 31),
        2: (6, 30),
        3: (9, 30),
        4: (12, 31),
    }[quarter]
    return datetime(year, month_day[0], month_day[1]).astimezone()


def _parse_epoch_value(value: Any) -> Optional[datetime]:
    if isinstance(value, bool):
        return None

    raw: float | None = None
    if isinstance(value, (int, float)):
        raw = float(value)
    else:
        text = str(value or "").strip()
        if not text or not re.fullmatch(r"\d+(?:\.\d+)?", text):
            return None
        try:
            raw = float(text)
        except ValueError:
            return None

    if raw is None:
        return None

    raw = abs(raw)
    if raw >= 1_000_000_000_000:
        seconds = raw / 1000.0
    elif raw >= 1_000_000_000:
        seconds = raw
    else:
        return None

    try:
        return datetime.fromtimestamp(seconds, tz=timezone.utc).astimezone()
    except (OverflowError, OSError, ValueError):
        return None


def parse_asof_time(value: Any) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.astimezone() if value.tzinfo else value.astimezone()
    if isinstance(value, date):
        return datetime.combine(value, time.min).astimezone()

    text = str(value).strip()
    if not text:
        return None

    quarter_dt = _parse_quarter_string(text)
    if quarter_dt is not None:
        return quarter_dt

    epoch_dt = _parse_epoch_value(value)
    if epoch_dt is not None:
        return epoch_dt

    if text.isdigit() and len(text) == 8:
        try:
            return datetime.strptime(text, "%Y%m%d").astimezone()
        except ValueError:
            return None

    if len(text) == 10:
        try:
            return datetime.fromisoformat(text).astimezone()
        except ValueError:
            return None

    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone()
    except ValueError:
        return None


def freshness_seconds(asof_value: Any, *, now: Optional[datetime] = None) -> float:
    parsed = parse_asof_time(asof_value)
    if parsed is None:
        return 0.0
    current = now.astimezone() if isinstance(now, datetime) else datetime.now().astimezone()
    return round(max((current - parsed).total_seconds(), 0.0), 3)


def isoformat_or_none(value: Any) -> Optional[str]:
    parsed = parse_asof_time(value)
    return parsed.isoformat() if parsed is not None else None


def infer_missing_fields(payload: Any, fields: Iterable[str]) -> list[str]:
    if not isinstance(payload, dict):
        return list(fields)
    missing: list[str] = []
    for field in list(fields or []):
        value = payload.get(field)
        if value is None or value == "":
            missing.append(str(field))
    return missing


def build_quality_meta(
    *,
    source: str,
    source_chain: Optional[Iterable[str]] = None,
    fallback_reason: Any = None,
    asof_value: Any = None,
    missing_fields: Optional[Iterable[str]] = None,
    degraded: bool = False,
    success: bool = True,
    started_at: Optional[datetime] = None,
    stale_after_sec: float = STALE_AFTER_SEC,
    accepted_count: Optional[int] = None,
    rejected_count: Optional[int] = None,
    minimum_quality_threshold: Optional[float] = None,
    minimum_quality_passed: Optional[bool] = None,
) -> dict:
    chain = [str(item).strip() for item in list(source_chain or []) if str(item).strip()]
    reasons = normalize_reason_list(fallback_reason)
    missing = [str(item).strip() for item in list(missing_fields or []) if str(item).strip()]
    now = datetime.now().astimezone()
    freshness_sec = freshness_seconds(asof_value, now=now)
    fallback_used = bool(reasons) or len(chain) > 1
    flags: list[str] = []
    if fallback_used:
        flags.append("fallback")
    if missing:
        flags.append("partial")
    if degraded:
        flags.append("degraded")
    if not success:
        flags.append("failed")
    if minimum_quality_passed is False:
        flags.append("quality_gate_failed")
    if freshness_sec > float(stale_after_sec or STALE_AFTER_SEC):
        flags.append("stale")
    quality_flags = list(dict.fromkeys(flags))
    latency_ms = 0.0
    if isinstance(started_at, datetime):
        latency_ms = round(max((now - started_at.astimezone()).total_seconds() * 1000, 0.0), 3)
    return {
        "source": source,
        "asof_time": isoformat_or_none(asof_value) or now.isoformat(),
        "freshness_sec": freshness_sec,
        "quality_flags": quality_flags,
        "missing_fields": missing,
        "source_chain": chain,
        "fallback_chain": chain,
        "fallback_used": fallback_used,
        "fallback_reason": reasons or None,
        "backend_requested": chain[0] if chain else source,
        "backend_used": source,
        "degraded": bool(degraded),
        "latency_ms": latency_ms,
        "accepted_count": int(accepted_count) if accepted_count is not None else None,
        "rejected_count": int(rejected_count) if rejected_count is not None else None,
        "minimum_quality_threshold": (
            round(float(minimum_quality_threshold), 6)
            if minimum_quality_threshold is not None
            else None
        ),
        "minimum_quality_passed": minimum_quality_passed if minimum_quality_passed is not None else None,
    }
