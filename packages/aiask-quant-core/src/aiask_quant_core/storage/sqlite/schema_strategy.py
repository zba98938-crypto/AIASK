"""Factory-domain table DDL — compatibility re-export.

The DDL moved to ``strategy_factory.infrastructure.factory_schema_ddl`` (Phase 2
of the extraction plan): the factory-domain schema is owned by Strategy Factory,
not by quant-core.

This module stays as a re-export so existing importers keep working for one major
version. It is also the fallback the schema registry uses when Strategy Factory is
not installed — quant-core must remain usable on its own, and a bare
``ImportError`` here would break every caller that only wanted market data.

New code should not import this module. Reach the schema through
``strategy_factory.api.runtime.init_factory_schema``.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

#: Where the DDL now lives.
_OWNER_MODULE = "strategy_factory.infrastructure.factory_schema_ddl"


class FactorySchemaUnavailable(ImportError):
    """Neither the owning package nor a local copy of the DDL is importable."""

    def __init__(self, detail: str = ""):
        super().__init__(
            "the factory-domain schema now lives in strategy-factory "
            f"({_OWNER_MODULE}); install it to initialize these tables"
            f"{f': {detail}' if detail else ''}"
        )


async def init_strategy_tables(conn: Any, sqlite_python_enabled: bool = False) -> None:
    """Create or migrate the factory-domain tables. Idempotent.

    Delegates to the owning package. Raises a named error rather than a bare
    ``ImportError`` when that package is absent, so the message says what to
    install instead of where the import failed.
    """
    try:
        from strategy_factory.infrastructure.factory_schema_ddl import (
            init_strategy_tables as _owned,
        )
    except ImportError as exc:  # pragma: no cover - depends on install state
        raise FactorySchemaUnavailable(str(exc)) from exc

    await _owned(conn, sqlite_python_enabled)


def schema_owner() -> str:
    """The module that owns this DDL, for provenance reporting."""
    return _OWNER_MODULE


__all__ = ["FactorySchemaUnavailable", "init_strategy_tables", "schema_owner"]
