from __future__ import annotations

import math
from typing import Any


def bounded_float(
    value: Any,
    *,
    default: float,
    minimum: float | None = None,
    maximum: float | None = None,
) -> float:
    try:
        if value is None or (isinstance(value, str) and not value.strip()):
            raise ValueError("missing float value")
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        parsed = float(default)
    if not math.isfinite(parsed):
        parsed = float(default)
    if not math.isfinite(parsed):
        parsed = 0.0
    if minimum is not None:
        parsed = max(float(minimum), parsed)
    if maximum is not None:
        parsed = min(float(maximum), parsed)
    return parsed


def bounded_int(
    value: Any,
    *,
    default: int,
    minimum: int | None = None,
    maximum: int | None = None,
) -> int:
    try:
        if value is None or (isinstance(value, str) and not value.strip()):
            raise ValueError("missing int value")
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        parsed = int(default)
    if minimum is not None:
        parsed = max(int(minimum), parsed)
    if maximum is not None:
        parsed = min(int(maximum), parsed)
    return parsed
