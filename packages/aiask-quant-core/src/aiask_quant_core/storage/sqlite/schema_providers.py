"""Registry for domain schema initializers.

Phase 2 of the extraction plan. ``schema_base._init_tables`` used to hardcode
``from .schema_strategy import init_strategy_tables``, which made quant-core the
structural owner of the 51 factory-domain tables. Ownership belongs to Strategy
Factory.

Rather than move 1641 lines of DDL and its 4070 lines of repository mixins in one
irreversible step, this inverts the *lookup*: a domain can register its own
initializer, and quant-core falls back to its bundled copy when nothing is
registered. That makes the move incremental and reversible — the DDL can relocate
without quant-core changing again, and a standalone Strategy Factory can supply
its own schema with no host present.

Registration is process-global and idempotent by namespace, matching how the
runtime provider registry already works.
"""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable, Protocol

logger = logging.getLogger(__name__)


class SchemaInitializer(Protocol):
    """A domain schema initializer.

    ``sqlite_python_enabled`` gates driver-side vector DDL; initializers that do
    not need it must still accept it so every domain has one call shape.
    """

    def __call__(self, conn: Any, sqlite_python_enabled: bool = False) -> Awaitable[None]:
        ...


#: Domains whose schema may be supplied by an owning package.
KNOWN_SCHEMA_NAMESPACES: tuple[str, ...] = ("strategy_runtime", "market_runtime", "vector_runtime")

_registry: dict[str, SchemaInitializer] = {}
#: Records who registered each namespace, for honest provenance reporting.
_sources: dict[str, str] = {}


def register_schema_initializer(
    namespace: str,
    initializer: SchemaInitializer,
    *,
    source_module: str,
) -> None:
    """Register the initializer that owns ``namespace``.

    Later registrations replace earlier ones so a host can override a bundled
    default. The replacement is logged rather than silent, because two packages
    claiming the same schema is worth noticing.
    """
    if namespace not in KNOWN_SCHEMA_NAMESPACES:
        raise ValueError(
            f"unknown schema namespace {namespace!r}; "
            f"expected one of {KNOWN_SCHEMA_NAMESPACES}"
        )
    if not callable(initializer):
        raise TypeError(f"initializer for {namespace!r} must be callable")

    previous = _sources.get(namespace)
    if previous and previous != source_module:
        logger.info(
            "schema namespace %s re-registered: %s -> %s",
            namespace,
            previous,
            source_module,
        )
    _registry[namespace] = initializer
    _sources[namespace] = source_module


def get_schema_initializer(namespace: str) -> SchemaInitializer | None:
    return _registry.get(namespace)


def schema_source(namespace: str) -> str | None:
    """Which module currently owns ``namespace``, if any is registered."""
    return _sources.get(namespace)


def registered_namespaces() -> tuple[str, ...]:
    return tuple(sorted(_registry))


def clear_schema_initializers() -> None:
    """Reset the registry. For tests and for host teardown."""
    _registry.clear()
    _sources.clear()


async def init_domain_schema(
    namespace: str,
    conn: Any,
    *,
    sqlite_python_enabled: bool = False,
    fallback: SchemaInitializer | None = None,
    fallback_source: str = "",
) -> dict[str, Any]:
    """Initialize one domain's schema, preferring the registered owner.

    Returns which initializer ran and where it came from, so a caller can report
    provenance instead of assuming the bundled copy was used.
    """
    initializer = get_schema_initializer(namespace)
    source = schema_source(namespace) or ""
    if initializer is None:
        if fallback is None:
            return {"namespace": namespace, "initialized": False, "reason": "no_initializer"}
        initializer = fallback
        source = fallback_source or "bundled_fallback"

    await initializer(conn, sqlite_python_enabled)
    return {
        "namespace": namespace,
        "initialized": True,
        "source_module": source,
        "registered": get_schema_initializer(namespace) is not None,
    }


__all__ = [
    "KNOWN_SCHEMA_NAMESPACES",
    "SchemaInitializer",
    "clear_schema_initializers",
    "get_schema_initializer",
    "init_domain_schema",
    "register_schema_initializer",
    "registered_namespaces",
    "schema_source",
]
