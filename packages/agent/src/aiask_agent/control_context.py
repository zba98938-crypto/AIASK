from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import Iterator


_CONTROL_CONTEXT: ContextVar[str | None] = ContextVar("aiask_agent_control_context", default=None)


def control_context_active() -> bool:
    return bool(_CONTROL_CONTEXT.get())


def control_context_reason() -> str | None:
    return _CONTROL_CONTEXT.get()


@contextmanager
def internal_control_context(reason: str) -> Iterator[None]:
    token = _CONTROL_CONTEXT.set(str(reason or "control"))
    try:
        yield
    finally:
        _CONTROL_CONTEXT.reset(token)
