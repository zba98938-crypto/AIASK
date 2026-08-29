"""Canonical physical stages for strategy incubation accounts and metrics."""

from __future__ import annotations

from enum import Enum


class IncubationStage(str, Enum):
    DIAGNOSTIC = "diagnostic"
    WARMUP = "warmup"
    PAPER = "paper"
    CANDIDATE = "candidate"
    LISTED = "listed"
    FAILED = "failed"
    RETIRED = "retired"


_LEGACY_STAGE_ALIASES = {
    "observe": IncubationStage.PAPER,
    "incubating": IncubationStage.CANDIDATE,
    "graduation_ready": IncubationStage.CANDIDATE,
    "promoted": IncubationStage.LISTED,
}


def normalize_incubation_stage(
    value: object,
    *,
    default: IncubationStage = IncubationStage.WARMUP,
) -> str:
    """Normalize a physical stage, rejecting unknown free-form values."""

    token = str(value or "").strip().lower()
    if not token:
        return default.value
    try:
        return IncubationStage(token).value
    except ValueError:
        alias = _LEGACY_STAGE_ALIASES.get(token)
        if alias is not None:
            return alias.value
        raise ValueError(f"unsupported incubation stage: {token}") from None


def incubation_stage_from_pipeline(value: object) -> str:
    """Map business pipeline stages onto the physical account vocabulary."""

    return normalize_incubation_stage(value)


__all__ = [
    "IncubationStage",
    "incubation_stage_from_pipeline",
    "normalize_incubation_stage",
]
