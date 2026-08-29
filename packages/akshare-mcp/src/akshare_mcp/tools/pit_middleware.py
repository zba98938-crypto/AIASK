"""PIT (Point-In-Time) middleware for AI workflow tools.

Provides helper functions that workflow tools can call to apply PIT semantics
uniformly, without needing to know the internals of PITContext.

Usage in a workflow tool::

    from ..tools.pit_middleware import create_pit_context, build_pit_meta

    ctx = create_pit_context(as_of)
    filtered = apply_pit_to_records(records, ctx)
    pit_meta = build_pit_meta(ctx, total=len(records), filtered=len(filtered))
"""

from __future__ import annotations

from typing import Any

from ..services.pit_utils import PITContext, as_of as pit_as_of, as_of_now, pit_filter_records


def create_pit_context(as_of: str | None = None, strict: bool = False) -> PITContext:
    """Create a PITContext from an optional as_of string.

    Parameters
    ----------
    as_of:
        ISO date or datetime string. None means "use current time" (no truncation).
    strict:
        If True, records missing available_time are flagged.
    """
    if as_of is None or str(as_of).strip() == "":
        return as_of_now(strict=strict)
    return pit_as_of(str(as_of).strip(), strict=strict)


def apply_pit_to_records(
    records: list[dict[str, Any]],
    ctx: PITContext,
    available_time_key: str = "available_time,date,report_date,日期",
) -> list[dict[str, Any]]:
    """Apply PIT filtering to a list of records.

    Tries multiple common date column names to find the one representing
    the time-of-availability.
    """
    if not records:
        return records
    return pit_filter_records(records, ctx, available_time_key=available_time_key)


def build_pit_meta(
    ctx: PITContext,
    *,
    total_records: int = 0,
    filtered_records: int = 0,
    audit_result: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build standard PIT metadata to inject into tool response meta.

    Returns a dict suitable for ``meta["pit"]``.
    """
    pit_passed = total_records == 0 or filtered_records == total_records
    meta: dict[str, Any] = {
        "as_of": ctx.as_of_datetime.isoformat(),
        "pit_passed": pit_passed,
        "records_total": total_records,
        "records_after_filter": filtered_records,
        "strict": ctx.strict,
    }
    if audit_result is not None:
        meta["audit"] = audit_result
    return meta


def build_pit_meta_simple(
    as_of: str | None = None,
    *,
    event_time: str | None = None,
    event_time_window: dict[str, Any] | None = None,
    feature_time_window: dict[str, Any] | None = None,
    pit_passed: bool = True,
) -> dict[str, Any]:
    """Build minimal PIT meta when no record-level filtering is done.

    Suitable for tools that call sub-tools (which handle their own PIT),
    so we just record the as_of context.
    """
    ctx = create_pit_context(as_of)
    return {
        "as_of": ctx.as_of_datetime.isoformat(),
        "event_time": event_time,
        "pit_passed": bool(pit_passed),
        "event_time_window": event_time_window,
        "feature_time_window": feature_time_window,
    }
