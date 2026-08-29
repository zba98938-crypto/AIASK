"""Compatibility shim for the canonical Strategy Factory runtime services."""

from __future__ import annotations

from .runtime_services import *  # noqa: F401,F403
from .runtime_services import __all__ as _RUNTIME_SERVICES_ALL


__all__ = list(_RUNTIME_SERVICES_ALL)
