"""Re-export of the factory control-plane views owned by strategy_factory.

The read-only views used to be duplicated here. The two copies diverged in how
they located the project root, so readiness answers depended on which copy the
caller happened to import. ``strategy_factory.mcp_control`` is the single
definition now, re-exported through ``strategy_factory.api`` so this package
keeps crossing the boundary only at the public surface; this module stays only
so existing importers keep working.
"""

from __future__ import annotations

from strategy_factory.api import (
    FACTORY_DESCRIPTORS,
    factory_readiness,
    factory_run_detail,
    factory_runs,
    factory_status,
    list_factories,
)


__all__ = [
    "FACTORY_DESCRIPTORS",
    "factory_readiness",
    "factory_run_detail",
    "factory_runs",
    "factory_status",
    "list_factories",
]
