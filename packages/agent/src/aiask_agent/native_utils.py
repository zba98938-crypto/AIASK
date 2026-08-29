from __future__ import annotations

import re
from uuid import uuid4

from .numeric import bounded_int


def _safe_slug(value: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(value or "").strip()).strip(".-")
    return slug[:120] or f"item-{uuid4().hex[:8]}"


def _limit(value: str, max_chars: int) -> tuple[str, bool]:
    limit = bounded_int(max_chars, default=20000, minimum=1, maximum=200000)
    return value[:limit], len(value) > limit


