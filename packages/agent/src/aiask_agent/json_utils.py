from __future__ import annotations

import json
import math
from typing import Any


def sanitize_json_value(value: Any) -> Any:
    """Return a JSON-compatible value with non-finite floats converted to null."""
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {key: sanitize_json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [sanitize_json_value(item) for item in value]
    return value


def dumps_json(value: Any, *, ensure_ascii: bool = False, sort_keys: bool = False) -> str:
    return json.dumps(
        sanitize_json_value(value),
        ensure_ascii=ensure_ascii,
        sort_keys=sort_keys,
        allow_nan=False,
    )


def dumps_json_bytes(value: Any, *, ensure_ascii: bool = False, sort_keys: bool = False) -> bytes:
    return dumps_json(value, ensure_ascii=ensure_ascii, sort_keys=sort_keys).encode("utf-8")
