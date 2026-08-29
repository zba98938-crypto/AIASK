"""Lifecycle state-machine helpers."""

from __future__ import annotations

# ── Lifecycle state machine ──────────────────────────────────────────────────

LIFECYCLE_TRANSITIONS: dict[str, list[str]] = {
    "draft": ["submitted"],
    "submitted": ["incubating", "rejected"],
    "rejected": ["draft"],
    "incubating": ["listed", "deprecated", "suspended"],
    "listed": ["deprecated", "suspended", "archived"],
    "suspended": ["listed", "deprecated", "incubating"],
    "deprecated": [],
    "published": ["deprecated", "suspended", "archived", "listed"],
    "archived": [],
}


def normalize_status_alias(status: str | None) -> str:
    normalized = str(status or "").strip().lower()
    return "listed" if normalized == "published" else normalized


def validate_transition(current: str, target: str) -> bool:
    current_normalized = normalize_status_alias(current)
    target_normalized = normalize_status_alias(target)
    return target_normalized in LIFECYCLE_TRANSITIONS.get(current_normalized, [])


async def update_status(db, strategy_id: str, status: str, **kwargs) -> None:
    normalized = normalize_status_alias(status)
    try:
        await db.update_strategy_status(strategy_id, normalized, **kwargs)
    except TypeError:
        await db.update_strategy_status(strategy_id, normalized)

