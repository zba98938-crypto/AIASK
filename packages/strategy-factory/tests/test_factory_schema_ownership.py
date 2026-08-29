"""Strategy Factory owns the factory-domain schema (Phase 2).

Ownership moved ahead of the DDL text, deliberately. ``schema_strategy.py`` is
1641 lines defining 51 live tables; copying it would create two diverging
definitions — the worst possible intermediate state. So this package registers
itself as the schema's initializer of record while the DDL body still lives in
quant-core (a declared dependency, so a downward reference).

What these tests pin is that the ownership seam is real: registration takes
effect, the registered initializer actually creates the tables, and the migration
ledger records Strategy Factory rather than quant-core.
"""

from __future__ import annotations

import asyncio
import sqlite3

import pytest

from strategy_factory.api.runtime import (
    FACTORY_SCHEMA_NAMESPACE,
    factory_domain_tables,
    init_factory_schema,
    register_factory_schema,
    verify_factory_schema,
)


quant_core = pytest.importorskip(
    "aiask_quant_core.storage.sqlite.schema_providers",
    reason="quant-core is required for the schema seam",
)


@pytest.fixture(autouse=True)
def _clean_registry():
    quant_core.clear_schema_initializers()
    yield
    quant_core.clear_schema_initializers()


class _Conn:
    """Tolerant shim: the real DDL uses an ``ADD COLUMN IF NOT EXISTS`` dialect."""

    def __init__(self, connection: sqlite3.Connection):
        self._connection = connection
        connection.row_factory = sqlite3.Row

    async def execute(self, query: str, *args):
        for statement in (part.strip() for part in query.split(";")):
            if not statement:
                continue
            try:
                self._connection.execute(statement)
            except sqlite3.Error:
                continue

    async def fetch(self, query: str, *args):
        return list(self._connection.execute(query, args).fetchall())


# --- public surface ------------------------------------------------------------


def test_schema_ownership_is_on_the_public_api() -> None:
    from strategy_factory.api import runtime

    for name in (
        "FACTORY_SCHEMA_NAMESPACE",
        "register_factory_schema",
        "init_factory_schema",
        "verify_factory_schema",
        "factory_domain_tables",
    ):
        assert name in runtime.__all__


def test_the_owned_namespace_is_the_strategy_domain() -> None:
    assert FACTORY_SCHEMA_NAMESPACE == "strategy_runtime"


# --- registration --------------------------------------------------------------


def test_registration_makes_strategy_factory_the_owner() -> None:
    assert quant_core.schema_source(FACTORY_SCHEMA_NAMESPACE) is None

    outcome = register_factory_schema()

    assert outcome["registered"] is True
    assert outcome["source_module"] == "strategy_factory.infrastructure.factory_schema"
    assert quant_core.schema_source(FACTORY_SCHEMA_NAMESPACE) == (
        "strategy_factory.infrastructure.factory_schema"
    )


def test_registration_is_idempotent() -> None:
    first = register_factory_schema()
    second = register_factory_schema()

    assert first["source_module"] == second["source_module"]
    assert quant_core.registered_namespaces() == (FACTORY_SCHEMA_NAMESPACE,)


def test_registered_initializer_is_the_one_that_runs() -> None:
    register_factory_schema()

    outcome = asyncio.run(
        quant_core.init_domain_schema(
            FACTORY_SCHEMA_NAMESPACE,
            _Conn(sqlite3.connect(":memory:")),
        )
    )

    assert outcome["initialized"] is True
    assert outcome["source_module"] == "strategy_factory.infrastructure.factory_schema"


# --- the DDL actually runs -----------------------------------------------------


def test_the_owned_initializer_creates_the_factory_tables() -> None:
    """Ownership is only meaningful if the registered initializer works."""
    connection = sqlite3.connect(":memory:")
    asyncio.run(init_factory_schema(_Conn(connection), False))

    tables = {
        row[0]
        for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }

    for expected in ("strategies", "strategy_factory_runs", "strategy_domain_events"):
        assert expected in tables, f"{expected} was not created"
    assert len(tables) >= 45, f"only {len(tables)} tables created"


def test_initialization_is_idempotent() -> None:
    connection = sqlite3.connect(":memory:")
    conn = _Conn(connection)

    asyncio.run(init_factory_schema(conn, False))
    first = connection.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table'"
    ).fetchone()[0]
    asyncio.run(init_factory_schema(conn, False))
    second = connection.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table'"
    ).fetchone()[0]

    assert first == second


# --- inventory and verification ------------------------------------------------


def test_the_table_inventory_is_derived_not_duplicated() -> None:
    """One inventory, shared with the fingerprint module, so it cannot drift."""
    from aiask_quant_core.storage.sqlite.factory_schema_fingerprint import (
        FACTORY_TABLE_NAMES,
    )

    assert set(factory_domain_tables()) == set(FACTORY_TABLE_NAMES)


def test_verification_reports_a_complete_schema() -> None:
    connection = sqlite3.connect(":memory:")
    conn = _Conn(connection)
    asyncio.run(init_factory_schema(conn, False))

    report = asyncio.run(verify_factory_schema(conn))

    assert report["verified"] is True
    assert report["complete"] is True, f"missing: {report['missing_tables']}"
    assert report["fingerprint"].startswith("sha256:")
    assert report["table_count"] >= 45


def test_verification_names_missing_tables_without_raising() -> None:
    """A caller decides whether a gap is a migration or a regression."""
    connection = sqlite3.connect(":memory:")
    connection.execute("CREATE TABLE strategies (id TEXT PRIMARY KEY)")

    report = asyncio.run(verify_factory_schema(_Conn(connection)))

    assert report["verified"] is True
    assert report["complete"] is False
    # ``missing_expected_tables`` reports the *named* inventory only; tables like
    # ``strategy_factory_runs`` are recognised by prefix and are not in that list.
    assert "strategy_domain_events" in report["missing_tables"]
    assert "strategies" not in report["missing_tables"], "the created table is present"


def test_verification_excludes_vector_platform_tables() -> None:
    """Those live in schema_strategy.py but are host-owned."""
    connection = sqlite3.connect(":memory:")
    asyncio.run(init_factory_schema(_Conn(connection), False))

    report = asyncio.run(verify_factory_schema(_Conn(connection)))

    # 56 tables are created; the vector-platform ones must not be counted.
    total = connection.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table'"
    ).fetchone()[0]
    assert report["table_count"] < total


# --- no host dependency --------------------------------------------------------


def test_schema_ownership_does_not_reference_a_host_package() -> None:
    import inspect

    import strategy_factory.infrastructure.factory_schema as module

    source = inspect.getsource(module)
    assert "akshare" not in source.lower(), (
        "schema ownership must not depend on a host package"
    )
