"""Domain schema initializers must be looked up, not hardcoded.

Phase 2 of the extraction plan. ``schema_base._init_tables`` used to import
``schema_strategy`` directly, which made quant-core the structural owner of the
51 factory-domain tables. The registry inverts that lookup so the owning package
can supply its own schema, while the bundled copy stays as a fallback — which is
what makes the ownership move incremental rather than a flag day.
"""

from __future__ import annotations

import asyncio

import pytest

from aiask_quant_core.storage.sqlite.schema_providers import (
    KNOWN_SCHEMA_NAMESPACES,
    clear_schema_initializers,
    get_schema_initializer,
    init_domain_schema,
    register_schema_initializer,
    registered_namespaces,
    schema_source,
)


@pytest.fixture(autouse=True)
def _clean_registry():
    clear_schema_initializers()
    yield
    clear_schema_initializers()


async def _noop(_conn, _sqlite_python_enabled=False):
    return None


def _recorder(log: list, label: str):
    async def _initializer(conn, sqlite_python_enabled=False):
        log.append((label, conn, sqlite_python_enabled))

    return _initializer


# --- registration --------------------------------------------------------------


def test_registry_starts_empty() -> None:
    assert registered_namespaces() == ()
    assert get_schema_initializer("strategy_runtime") is None


def test_registration_records_the_owner() -> None:
    register_schema_initializer("strategy_runtime", _noop, source_module="pkg.schema")

    assert get_schema_initializer("strategy_runtime") is _noop
    assert schema_source("strategy_runtime") == "pkg.schema"
    assert registered_namespaces() == ("strategy_runtime",)


def test_reregistration_replaces_the_owner() -> None:
    """A host may override a bundled default; the change must take effect."""
    register_schema_initializer("strategy_runtime", _noop, source_module="first")
    second = _recorder([], "second")
    register_schema_initializer("strategy_runtime", second, source_module="second")

    assert get_schema_initializer("strategy_runtime") is second
    assert schema_source("strategy_runtime") == "second"


def test_unknown_namespace_is_rejected() -> None:
    with pytest.raises(ValueError, match="unknown schema namespace"):
        register_schema_initializer("not_a_domain", _noop, source_module="x")


def test_non_callable_initializer_is_rejected() -> None:
    with pytest.raises(TypeError):
        register_schema_initializer("strategy_runtime", "nope", source_module="x")


def test_known_namespaces_cover_the_three_domains() -> None:
    assert set(KNOWN_SCHEMA_NAMESPACES) == {
        "strategy_runtime",
        "market_runtime",
        "vector_runtime",
    }


def test_clearing_removes_both_initializer_and_source() -> None:
    register_schema_initializer("strategy_runtime", _noop, source_module="pkg")
    clear_schema_initializers()

    assert get_schema_initializer("strategy_runtime") is None
    assert schema_source("strategy_runtime") is None


# --- dispatch ------------------------------------------------------------------


def test_registered_owner_wins_over_the_fallback() -> None:
    log: list = []
    register_schema_initializer(
        "strategy_runtime", _recorder(log, "owner"), source_module="owner.module"
    )

    outcome = asyncio.run(
        init_domain_schema(
            "strategy_runtime",
            object(),
            fallback=_recorder(log, "fallback"),
            fallback_source="bundled",
        )
    )

    assert [entry[0] for entry in log] == ["owner"]
    assert outcome["source_module"] == "owner.module"
    assert outcome["registered"] is True


def test_fallback_runs_when_nothing_is_registered() -> None:
    log: list = []

    outcome = asyncio.run(
        init_domain_schema(
            "strategy_runtime",
            object(),
            fallback=_recorder(log, "fallback"),
            fallback_source="bundled",
        )
    )

    assert [entry[0] for entry in log] == ["fallback"]
    assert outcome["source_module"] == "bundled"
    assert outcome["registered"] is False, (
        "provenance must say the bundled copy ran, not that an owner is registered"
    )


def test_no_initializer_and_no_fallback_reports_rather_than_raising() -> None:
    outcome = asyncio.run(init_domain_schema("strategy_runtime", object()))

    assert outcome["initialized"] is False
    assert outcome["reason"] == "no_initializer"


def test_sqlite_python_flag_is_passed_through() -> None:
    log: list = []
    register_schema_initializer("vector_runtime", _recorder(log, "v"), source_module="v")

    asyncio.run(init_domain_schema("vector_runtime", object(), sqlite_python_enabled=True))

    assert log[0][2] is True


def test_the_connection_is_passed_through_unchanged() -> None:
    log: list = []
    sentinel = object()
    register_schema_initializer("strategy_runtime", _recorder(log, "s"), source_module="s")

    asyncio.run(init_domain_schema("strategy_runtime", sentinel))

    assert log[0][1] is sentinel


# --- integration with the real DDL ---------------------------------------------


def test_the_strategy_owner_ddl_works_through_the_registry() -> None:
    """The registered owner must provide the factory schema."""
    import sqlite3

    from strategy_factory.infrastructure.factory_schema import (
        FACTORY_SCHEMA_SOURCE,
        init_factory_schema,
    )

    class _Conn:
        def __init__(self, connection):
            self._connection = connection

        async def execute(self, query, *args):
            for statement in (part.strip() for part in query.split(";")):
                if not statement:
                    continue
                try:
                    self._connection.execute(statement)
                except sqlite3.Error:
                    continue

    register_schema_initializer(
        "strategy_runtime",
        init_factory_schema,
        source_module=FACTORY_SCHEMA_SOURCE,
    )
    connection = sqlite3.connect(":memory:")
    outcome = asyncio.run(
        init_domain_schema("strategy_runtime", _Conn(connection))
    )

    assert outcome["initialized"] is True
    assert outcome["source_module"] == FACTORY_SCHEMA_SOURCE
    tables = connection.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table'"
    ).fetchone()[0]
    assert tables >= 45, f"the owner DDL created only {tables} tables"


def test_strategy_schema_without_a_registered_owner_is_reported() -> None:
    outcome = asyncio.run(init_domain_schema("strategy_runtime", object()))

    assert outcome["initialized"] is False
    assert outcome["reason"] == "no_initializer"
