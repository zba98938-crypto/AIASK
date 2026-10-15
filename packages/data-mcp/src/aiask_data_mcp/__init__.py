"""Neutral data-provider facade for AIASK.

Phase 5 of the extraction plan. ``akshare-mcp`` names one specific upstream data
source in a package that has grown to own the MCP host, the SQLite provider, the
manager facade and the broker connectors. That name is wrong for the role and
wrong for a standalone product where AKShare may be one source among several.

This package is the vendor-neutral entry point. It re-exports the current
implementation rather than forking it — one implementation, two names — so
adopting the neutral name carries no behavioural risk.

Compatibility policy:

* ``akshare_mcp`` keeps working for one major version. It is not deprecated in
  code yet, because every existing caller and every MCP manifest still uses it.
* New code should import ``aiask_data_mcp``.
* When the compatibility window closes, the implementation moves here and
  ``akshare_mcp`` becomes the thin re-export.

What this facade deliberately does **not** do: re-export factory orchestration.
That belongs to ``strategy_factory``, reached through ``strategy_factory.api``.
A data provider should not be a route to the factory contract.
"""

from __future__ import annotations

import importlib
from typing import Any

#: The implementation this facade currently delegates to.
BACKING_DISTRIBUTION = "akshare-mcp"
BACKING_MODULE = "akshare_mcp"

#: Facade contract version. Bumped on a breaking change to the neutral surface.
FACADE_PROTOCOL_VERSION = "1.0"

#: Submodules re-exported under the neutral name. Data-provider concerns only —
#: factory orchestration is intentionally absent.
NEUTRAL_SUBMODULES: tuple[str, ...] = (
    "storage",
    "env_loader",
    "server",
)


class BackingImplementationMissing(ImportError):
    """The backing provider is not installed."""

    def __init__(self, detail: str = ""):
        super().__init__(
            f"{BACKING_DISTRIBUTION} is required by aiask-data-mcp but is not "
            f"importable{f': {detail}' if detail else ''}"
        )


def backing_module() -> Any:
    """Import the backing implementation, failing with a clear message."""
    try:
        return importlib.import_module(BACKING_MODULE)
    except ImportError as exc:  # pragma: no cover - depends on install state
        raise BackingImplementationMissing(str(exc)) from exc


def resolve(name: str) -> Any:
    """Resolve a neutral submodule to its backing implementation.

    Only names in :data:`NEUTRAL_SUBMODULES` resolve, so the facade cannot become
    an unbounded passthrough to every internal of the provider package.
    """
    if name not in NEUTRAL_SUBMODULES:
        raise AttributeError(
            f"{name!r} is not part of the neutral data-provider surface; "
            f"available: {', '.join(NEUTRAL_SUBMODULES)}"
        )
    try:
        return importlib.import_module(f"{BACKING_MODULE}.{name}")
    except ImportError as exc:
        raise BackingImplementationMissing(f"{BACKING_MODULE}.{name}: {exc}") from exc


#: Names this package implements itself. They must never be proxied, or
#: ``from aiask_data_mcp import server`` would resolve to the provider's server
#: instead of this package's entry point.
_OWN_SUBMODULES: frozenset[str] = frozenset({"server"})


def __getattr__(name: str) -> Any:
    """Lazily expose the neutral surface as attributes.

    Lazy so importing this package does not pull in the whole provider — which
    matters because the provider imports pandas and other heavy dependencies.

    Names this package owns are resolved as its own submodules first; only the
    remainder are proxied to the backing implementation.
    """
    if name in _OWN_SUBMODULES:
        return importlib.import_module(f"{__name__}.{name}")
    return resolve(name)


def facade_status() -> dict[str, Any]:
    """Report what the facade resolves to, without importing the provider."""
    import importlib.util

    available = importlib.util.find_spec(BACKING_MODULE) is not None
    return {
        "facade": "aiask-data-mcp",
        "protocol_version": FACADE_PROTOCOL_VERSION,
        "backing_distribution": BACKING_DISTRIBUTION,
        "backing_module": BACKING_MODULE,
        "backing_available": available,
        "neutral_submodules": list(NEUTRAL_SUBMODULES),
        "ready": available,
    }


__all__ = [
    "BACKING_DISTRIBUTION",
    "BACKING_MODULE",
    "FACADE_PROTOCOL_VERSION",
    "NEUTRAL_SUBMODULES",
    "BackingImplementationMissing",
    "backing_module",
    "facade_status",
    "resolve",
]
