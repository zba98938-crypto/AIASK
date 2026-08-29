from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, TypeVar

from .session_store_utils import redact_audit_text


T = TypeVar("T")


@dataclass(frozen=True)
class ClassifiedError:
    category: str
    retryable: bool
    message: str


def classify_exception(exc: BaseException) -> ClassifiedError:
    text = str(exc).lower()
    if isinstance(exc, asyncio.TimeoutError) or "timeout" in text or "timed out" in text:
        return ClassifiedError("timeout", True, str(exc))
    if "rate" in text and "limit" in text:
        return ClassifiedError("rate_limit", True, str(exc))
    if "overload" in text or "temporarily" in text or "503" in text or "502" in text or "500" in text:
        return ClassifiedError("server_error", True, str(exc))
    if "context" in text and ("length" in text or "window" in text or "too long" in text):
        return ClassifiedError("context_overflow", False, str(exc))
    if "auth" in text or "api key" in text or "401" in text:
        return ClassifiedError("auth", False, str(exc))
    if "json" in text or "tool" in text and "argument" in text:
        return ClassifiedError("format_error", False, str(exc))
    return ClassifiedError("unknown", False, str(exc))


async def retry_async(
    operation: Callable[[], Awaitable[T]],
    *,
    attempts: int,
    base_delay: float = 0.25,
    max_delay: float = 4.0,
    audit_events: list[dict[str, Any]] | None = None,
    event_factory: Callable[[dict[str, Any]], None] | None = None,
) -> T:
    last_exc: BaseException | None = None
    total_attempts = max(1, int(attempts or 1))
    for attempt in range(1, total_attempts + 1):
        try:
            return await operation()
        except Exception as exc:
            classified = classify_exception(exc)
            last_exc = exc
            event = {
                "event": "operation_retry_classified",
                "attempt": attempt,
                "category": classified.category,
                "retryable": classified.retryable,
                "error": redact_audit_text(classified.message),
            }
            if audit_events is not None:
                audit_events.append(event)
            if event_factory is not None:
                event_factory(event)
            if attempt >= total_attempts or not classified.retryable:
                raise
            delay = min(max_delay, base_delay * (2 ** (attempt - 1)))
            await asyncio.sleep(delay + random.uniform(0, delay / 2))
    assert last_exc is not None
    raise last_exc
