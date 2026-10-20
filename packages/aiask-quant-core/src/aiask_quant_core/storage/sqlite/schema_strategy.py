"""Factory-domain schema compatibility shim.

The DDL is owned by Strategy Factory.  This module no longer imports that
package: it resolves the registered ``strategy_runtime`` initializer and calls
it.  New code should use ``strategy_factory.api.runtime.init_factory_schema``;
this shim exists for existing callers and for quant-core's schema bootstrap.

When no provider is registered (a standalone quant-core process that does not
need factory tables), ``init_strategy_tables`` raises a named error rather than
silently importing a package that is not a dependency of this one.
"""

from __future__ import annotations

from typing import Any

from .schema_providers import get_schema_initializer


#: Where the DDL physically lives.
_OWNER_MODULE = "strategy_factory.infrastructure.factory_schema_ddl"
_NAMESPACE = "strategy_runtime"


class FactorySchemaUnavailable(ImportError):
    """No registered initializer owns the factory-domain schema."""

    def __init__(self, detail: str = ""):
        super().__init__(
            "the factory-domain schema is owned by strategy-factory; "
            "register it with "
            "strategy_factory.infrastructure.factory_schema.register_factory_schema() "
            "before initializing the database"
            f"{f': {detail}' if detail else ''}"
        )


async def init_strategy_tables(conn: Any, sqlite_python_enabled: bool = False) -> None:
    """Create or migrate the factory-domain tables through the registry."""
    initializer = get_schema_initializer(_NAMESPACE)
    if initializer is None:
        raise FactorySchemaUnavailable(
            f"no initializer registered for namespace {_NAMESPACE!r}"
        )
    await initializer(conn, sqlite_python_enabled)


def schema_owner() -> str:
    """The module that owns this DDL, for provenance reporting."""
    return _OWNER_MODULE


__all__ = ["FactorySchemaUnavailable", "init_strategy_tables", "schema_owner"]
