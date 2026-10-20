"""Tests for the factory-domain schema fingerprint.

This is the safety net for the planned ownership move of the factory tables out
of ``aiask-quant-core``. The existing namespace checkpoint hashes only a module
name string and refreshes itself on drift, so it cannot prove a move preserved
structure. These tests prove the fingerprint can.
"""

from __future__ import annotations

import asyncio
import re
import sqlite3

import pytest

from aiask_quant_core.storage.sqlite.factory_schema_fingerprint import (
    FACTORY_FINGERPRINT_NAMESPACE,
    FACTORY_TABLE_NAMES,
    VECTOR_PLATFORM_TABLES,
    assert_migration_preserved_structure,
    collect_factory_schema,
    diff_schemas,
    fingerprint_schema,
    is_factory_table,
    missing_expected_tables,
    record_factory_schema_fingerprint,
)


class _Conn:
    """Minimal async shim over sqlite3 matching the ``fetch`` interface used."""

    def __init__(self, connection: sqlite3.Connection):
        self._connection = connection
        self._connection.row_factory = sqlite3.Row

    async def fetch(self, query: str, *args):
        return list(self._connection.execute(query, args).fetchall())


def collect(conn) -> dict:
    """Synchronous wrapper so these tests need no pytest-asyncio dependency.

    ``aiask-quant-core`` declares only ``pytest`` in its test extra, so relying
    on an async plugin here would pass locally and fail in CI.
    """
    return asyncio.run(collect_factory_schema(conn))


@pytest.fixture()
def conn():
    connection = sqlite3.connect(":memory:")
    connection.executescript(
        """
        CREATE TABLE strategies (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            status TEXT DEFAULT 'draft'
        );
        CREATE TABLE strategy_factory_runs (
            run_id TEXT PRIMARY KEY,
            status TEXT,
            started_at TEXT
        );
        CREATE TABLE strategy_incubation_metrics (
            id INTEGER PRIMARY KEY,
            strategy_id TEXT NOT NULL
        );
        CREATE TABLE factory_run_ledger (
            id INTEGER PRIMARY KEY,
            factory TEXT NOT NULL
        );
        -- Not factory-domain: must be ignored by the fingerprint.
        CREATE TABLE stock_daily_bars (
            code TEXT,
            trade_date TEXT
        );
        """
    )
    return _Conn(connection)


# --- ownership classification --------------------------------------------------


@pytest.mark.parametrize(
    "name",
    [
        "strategies",
        "strategy_factory_runs",
        "strategy_factory_task_evidence",
        "strategy_incubation_accounts",
        "factory_run_locks",
        "strategy_domain_events",
        "matching_engine_worker_state",
    ],
)
def test_factory_domain_tables_are_recognised(name) -> None:
    assert is_factory_table(name) is True


@pytest.mark.parametrize(
    "name",
    ["stock_daily_bars", "market_schema_migrations", "", "north_fund_flows"],
)
def test_non_factory_tables_are_excluded(name) -> None:
    assert is_factory_table(name) is False


def test_market_data_tables_are_not_claimed_by_the_factory_domain() -> None:
    """Ownership boundary: raw market data stays with the data provider."""
    for name in ("stock_daily_bars", "index_daily_bars", "north_fund_flows"):
        assert not is_factory_table(name), f"{name} must not move with the factory domain"


# --- collection ----------------------------------------------------------------


def test_collect_reads_only_factory_tables(conn) -> None:
    schema = collect(conn)

    assert set(schema) == {
        "strategies",
        "strategy_factory_runs",
        "strategy_incubation_metrics",
        "factory_run_ledger",
    }
    assert "stock_daily_bars" not in schema


def test_collected_columns_capture_type_and_constraints(conn) -> None:
    schema = collect(conn)
    columns = {column["name"]: column for column in schema["strategies"]}

    assert columns["id"]["pk"] == 1
    assert columns["name"]["notnull"] == 1
    assert columns["status"]["type"] == "TEXT"


def test_columns_are_sorted_so_ddl_order_does_not_matter(conn) -> None:
    schema = collect(conn)
    names = [column["name"] for column in schema["strategy_factory_runs"]]
    assert names == sorted(names)


# --- fingerprint ---------------------------------------------------------------


def test_fingerprint_is_deterministic(conn) -> None:
    first = fingerprint_schema(collect(conn))
    second = fingerprint_schema(collect(conn))
    assert first == second
    assert first.startswith("sha256:")


def test_fingerprint_changes_when_a_column_is_added(conn) -> None:
    before = collect(conn)
    conn._connection.execute("ALTER TABLE strategy_factory_runs ADD COLUMN engine TEXT")
    after = collect(conn)

    assert fingerprint_schema(before) != fingerprint_schema(after)


def test_fingerprint_ignores_non_factory_changes(conn) -> None:
    """A change to market data must not look like a factory-domain change."""
    before = collect(conn)
    conn._connection.execute("ALTER TABLE stock_daily_bars ADD COLUMN turnover REAL")
    after = collect(conn)

    assert fingerprint_schema(before) == fingerprint_schema(after)


# --- diff and migration assertion ---------------------------------------------


def test_diff_reports_identical_schemas() -> None:
    schema = {"strategies": [{"name": "id", "type": "TEXT", "notnull": 0, "pk": 1}]}
    diff = diff_schemas(schema, dict(schema))

    assert diff["identical"] is True
    assert diff["dropped_tables"] == []


def test_diff_names_the_dropped_table() -> None:
    before = {"strategies": [], "strategy_factory_runs": []}
    after = {"strategies": []}

    diff = diff_schemas(before, after)

    assert diff["dropped_tables"] == ["strategy_factory_runs"]
    assert diff["identical"] is False


def test_diff_distinguishes_added_dropped_and_altered_columns() -> None:
    before = {
        "strategies": [
            {"name": "id", "type": "TEXT", "notnull": 0, "pk": 1},
            {"name": "legacy", "type": "TEXT", "notnull": 0, "pk": 0},
        ]
    }
    after = {
        "strategies": [
            {"name": "id", "type": "TEXT", "notnull": 1, "pk": 1},
            {"name": "fresh", "type": "TEXT", "notnull": 0, "pk": 0},
        ]
    }

    detail = diff_schemas(before, after)["changed_tables"]["strategies"]

    assert detail["dropped_columns"] == ["legacy"]
    assert detail["added_columns"] == ["fresh"]
    assert detail["altered_columns"] == ["id"]


def test_migration_assertion_allows_additions() -> None:
    before = {"strategies": [{"name": "id", "type": "TEXT", "notnull": 0, "pk": 1}]}
    after = {
        "strategies": [
            {"name": "id", "type": "TEXT", "notnull": 0, "pk": 1},
            {"name": "new_col", "type": "TEXT", "notnull": 0, "pk": 0},
        ],
        "strategy_factory_runs": [],
    }

    assert_migration_preserved_structure(before, after)


def test_migration_assertion_rejects_a_dropped_table() -> None:
    before = {"strategies": [], "strategy_factory_runs": []}
    after = {"strategies": []}

    with pytest.raises(AssertionError) as excinfo:
        assert_migration_preserved_structure(before, after)

    assert "strategy_factory_runs" in str(excinfo.value)


def test_migration_assertion_rejects_a_dropped_column() -> None:
    before = {
        "strategies": [
            {"name": "id", "type": "TEXT", "notnull": 0, "pk": 1},
            {"name": "status", "type": "TEXT", "notnull": 0, "pk": 0},
        ]
    }
    after = {"strategies": [{"name": "id", "type": "TEXT", "notnull": 0, "pk": 1}]}

    with pytest.raises(AssertionError, match="dropped columns"):
        assert_migration_preserved_structure(before, after)


def test_migration_assertion_rejects_a_retyped_column() -> None:
    """A silent type change is exactly the drift the string checksum misses."""
    before = {"strategies": [{"name": "id", "type": "TEXT", "notnull": 0, "pk": 1}]}
    after = {"strategies": [{"name": "id", "type": "INTEGER", "notnull": 0, "pk": 1}]}

    with pytest.raises(AssertionError, match="altered columns"):
        assert_migration_preserved_structure(before, after)


def test_strict_mode_rejects_unexpected_new_tables() -> None:
    before = {"strategies": []}
    after = {"strategies": [], "surprise_table_strategy_factory_x": []}

    with pytest.raises(AssertionError, match="unexpected new tables"):
        assert_migration_preserved_structure(before, after, allow_added=False)


# --- expected-table inventory --------------------------------------------------


def test_missing_expected_tables_reports_the_gap(conn) -> None:
    schema = collect(conn)
    missing = missing_expected_tables(schema)

    # The fixture only creates a few of the named tables.
    assert "strategy_domain_events" in missing
    assert "strategies" not in missing


def test_expected_table_inventory_is_non_trivial() -> None:
    """Guards against the named-table set being emptied and silently passing."""
    assert len(FACTORY_TABLE_NAMES) >= 25


# --- against the real schema ---------------------------------------------------


class _TolerantConn(_Conn):
    """Executes the real DDL, tolerating this codebase's custom SQL dialect.

    ``schema_strategy`` uses ``ADD COLUMN IF NOT EXISTS``, which the production
    driver understands but stdlib sqlite3 does not. Those statements are
    idempotent column top-ups, so skipping them still yields the full table set.
    """

    async def execute(self, query: str, *args):
        for statement in (part.strip() for part in query.split(";")):
            if not statement:
                continue
            try:
                self._connection.execute(statement)
            except sqlite3.Error:
                continue


def test_fingerprint_covers_the_real_strategy_schema() -> None:
    """The fingerprint must work on the actual DDL, not just a toy fixture."""
    from strategy_factory.infrastructure.factory_schema import init_factory_schema

    conn = _TolerantConn(sqlite3.connect(":memory:"))
    asyncio.run(init_factory_schema(conn, False))

    schema = collect(conn)

    # The real schema defines dozens of factory-domain tables.
    assert len(schema) >= 45, f"only {len(schema)} factory tables collected"
    # Every table named in the inventory must actually exist in the real DDL,
    # so the inventory cannot rot away from the schema it describes.
    assert missing_expected_tables(schema) == []
    assert fingerprint_schema(schema).startswith("sha256:")


def test_vector_domain_tables_are_not_claimed_by_the_factory_domain() -> None:
    """Vector-platform tables live in schema_strategy.py but are host-owned.

    Verified by reference count: strategy-factory reads none of them, while
    akshare-mcp uses all of them. The ``strategy_`` prefix would otherwise
    sweep them in.
    """
    from strategy_factory.infrastructure.factory_schema import init_factory_schema

    conn = _TolerantConn(sqlite3.connect(":memory:"))
    asyncio.run(init_factory_schema(conn, False))
    schema = collect(conn)

    for name in (
        "vector_index_registry",
        "strategy_vector_index_items",
        "strategy_vector_profiles",
    ):
        assert name not in schema, f"{name} belongs to the vector platform"


@pytest.mark.parametrize("name", sorted(VECTOR_PLATFORM_TABLES))
def test_vector_platform_tables_are_excluded_explicitly(name: str) -> None:
    """Explicit exclusion, not merely absence from the named inventory."""
    assert is_factory_table(name) is False


def test_vector_exclusion_beats_the_strategy_prefix() -> None:
    """Guards the precedence: exclusion must win over prefix matching."""
    assert is_factory_table("strategy_vector_profiles") is False
    # A genuinely factory-domain strategy_ table is still recognised.
    assert is_factory_table("strategy_factory_runs") is True


# --- ledger persistence --------------------------------------------------------


def _ledger_conn() -> _TolerantConn:
    connection = sqlite3.connect(":memory:")
    connection.executescript(
        """
        CREATE TABLE strategies (id TEXT PRIMARY KEY, name TEXT NOT NULL);
        CREATE TABLE strategy_factory_runs (run_id TEXT PRIMARY KEY, status TEXT);
        """
    )
    return _TolerantConn(connection)


def _to_qmark(query: str) -> str:
    """Translate the driver's ``$N`` placeholders to stdlib sqlite3 ``?``.

    Production uses a ``$N`` dialect; stdlib sqlite3 wants qmark. Without this
    the shim emits DeprecationWarnings and will hard-fail on Python 3.14.
    """
    return re.sub(r"\$\d+", "?", query)


class _LedgerConn(_TolerantConn):
    """Adds the fetchrow/execute surface the ledger writer needs."""

    async def fetch(self, query: str, *args):
        return list(self._connection.execute(_to_qmark(query), args).fetchall())

    async def fetchrow(self, query: str, *args):
        rows = await self.fetch(query, *args)
        return rows[0] if rows else None

    async def execute(self, query: str, *args):
        for statement in (part.strip() for part in query.split(";")):
            if not statement:
                continue
            translated = _to_qmark(statement)
            try:
                self._connection.execute(translated, args)
            except sqlite3.Error:
                # DDL statements carry no parameters.
                try:
                    self._connection.execute(translated)
                except sqlite3.Error:
                    continue
        self._connection.commit()


def test_fingerprint_is_recorded_on_first_run() -> None:
    connection = sqlite3.connect(":memory:")
    connection.executescript(
        """
        CREATE TABLE strategies (id TEXT PRIMARY KEY, name TEXT NOT NULL);
        CREATE TABLE strategy_factory_runs (run_id TEXT PRIMARY KEY, status TEXT);
        """
    )
    conn = _LedgerConn(connection)

    outcome = asyncio.run(record_factory_schema_fingerprint(conn))

    assert outcome["namespace"] == FACTORY_FINGERPRINT_NAMESPACE
    assert outcome["fingerprint"].startswith("sha256:")
    assert outcome["previous_fingerprint"] is None
    assert outcome["changed"] is False
    assert outcome["table_count"] == 2


def test_recording_twice_is_idempotent_and_reports_no_change() -> None:
    connection = sqlite3.connect(":memory:")
    connection.executescript(
        "CREATE TABLE strategies (id TEXT PRIMARY KEY);"
    )
    conn = _LedgerConn(connection)

    first = asyncio.run(record_factory_schema_fingerprint(conn))
    second = asyncio.run(record_factory_schema_fingerprint(conn))

    assert second["fingerprint"] == first["fingerprint"]
    assert second["previous_fingerprint"] == first["fingerprint"]
    assert second["changed"] is False


def test_structural_drift_is_reported_not_hidden() -> None:
    """The whole point: unlike the string checkpoint, drift must surface."""
    connection = sqlite3.connect(":memory:")
    connection.executescript("CREATE TABLE strategy_factory_runs (run_id TEXT PRIMARY KEY);")
    conn = _LedgerConn(connection)

    asyncio.run(record_factory_schema_fingerprint(conn))
    connection.execute("ALTER TABLE strategy_factory_runs ADD COLUMN engine TEXT")
    after = asyncio.run(record_factory_schema_fingerprint(conn))

    assert after["changed"] is True, (
        "a structural change must be reported; the string-signature checkpoint "
        "would have silently refreshed its checksum here"
    )
    assert after["previous_fingerprint"] != after["fingerprint"]
