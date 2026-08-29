"""Factory-domain schema ownership.

Phase 2 of the extraction plan. Strategy Factory declares which tables it owns
and registers itself as their schema initializer, so ``quant-core`` no longer
hardcodes the lookup.

Ownership is now both nominal and physical:

* the DDL body lives in :mod:`strategy_factory.infrastructure.factory_schema_ddl`,
  moved verbatim from quant-core (the module was self-contained, so the move was
  mechanical and the structural fingerprint verifies it);
* quant-core keeps a re-export shim for one major version, so existing importers
  of ``schema_strategy`` keep working;
* this module is the registered initializer of record, so the migration ledger
  attributes the tables to Strategy Factory.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


#: Schema namespace this package owns.
FACTORY_SCHEMA_NAMESPACE = "strategy_runtime"

#: Migration key for the factory-domain bootstrap.
FACTORY_SCHEMA_MIGRATION_KEY = "sqlite_bootstrap_v1"

#: Module recorded as the owner in the migration ledger.
FACTORY_SCHEMA_SOURCE = "strategy_factory.infrastructure.factory_schema"


def _owned_initializer() -> Any:
    """The DDL body, now physically owned by this package."""
    from .factory_schema_ddl import init_strategy_tables

    return init_strategy_tables


async def init_factory_schema(conn: Any, sqlite_python_enabled: bool = False) -> None:
    """Create or migrate every factory-domain table. Idempotent."""
    initializer = _owned_initializer()
    await initializer(conn, sqlite_python_enabled)


def register_factory_schema() -> dict[str, Any]:
    """Register Strategy Factory as the owner of the factory-domain schema.

    Idempotent. Returns what was registered so a caller can report provenance
    instead of assuming it succeeded.
    """
    try:
        from aiask_quant_core.storage.sqlite.schema_providers import (
            register_schema_initializer,
            schema_source,
        )
    except ModuleNotFoundError as exc:
        logger.debug("factory schema registration skipped: %s", exc)
        return {"registered": False, "reason": f"schema_registry_unavailable:{exc}"}

    register_schema_initializer(
        FACTORY_SCHEMA_NAMESPACE,
        init_factory_schema,
        source_module=FACTORY_SCHEMA_SOURCE,
    )
    return {
        "registered": True,
        "namespace": FACTORY_SCHEMA_NAMESPACE,
        "source_module": schema_source(FACTORY_SCHEMA_NAMESPACE),
    }


def factory_domain_tables() -> tuple[str, ...]:
    """The factory-domain table inventory, sorted.

    Derived from the structural fingerprint module so there is one inventory, not
    two that can drift.
    """
    try:
        from aiask_quant_core.storage.sqlite.factory_schema_fingerprint import (
            FACTORY_TABLE_NAMES,
            FACTORY_TABLE_PREFIXES,
            VECTOR_PLATFORM_TABLES,
        )
    except ModuleNotFoundError:  # pragma: no cover - standalone without quant-core
        return ()
    del FACTORY_TABLE_PREFIXES, VECTOR_PLATFORM_TABLES
    return tuple(sorted(FACTORY_TABLE_NAMES))


async def verify_factory_schema(conn: Any) -> dict[str, Any]:
    """Report whether the live schema matches what this package claims to own.

    Never raises on mismatch: a caller decides whether a difference is a
    legitimate migration or a regression.
    """
    try:
        from aiask_quant_core.storage.sqlite.factory_schema_fingerprint import (
            collect_factory_schema,
            fingerprint_schema,
            missing_expected_tables,
        )
    except ModuleNotFoundError as exc:
        return {"verified": False, "reason": f"fingerprint_unavailable:{exc}"}

    schema = await collect_factory_schema(conn)
    missing = missing_expected_tables(schema)
    return {
        "verified": True,
        "namespace": FACTORY_SCHEMA_NAMESPACE,
        "table_count": len(schema),
        "missing_tables": missing,
        "complete": not missing,
        "fingerprint": fingerprint_schema(schema),
    }


__all__ = [
    "FACTORY_SCHEMA_MIGRATION_KEY",
    "FACTORY_SCHEMA_NAMESPACE",
    "FACTORY_SCHEMA_SOURCE",
    "factory_domain_tables",
    "init_factory_schema",
    "register_factory_schema",
    "verify_factory_schema",
]
