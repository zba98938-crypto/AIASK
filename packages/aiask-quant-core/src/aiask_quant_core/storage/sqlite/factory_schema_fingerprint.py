"""Structural fingerprint for the factory-domain tables.

Why this exists: ``record_schema_namespace_checkpoint`` hashes only the string
``(namespace, migration_key, source_module)``. That proves *which module claimed
to own a namespace*, not *what shape the tables actually have* — and on drift it
deliberately refreshes the checksum instead of failing. So it cannot detect a
table being renamed, dropped, or moved to another package.

That matters for the planned ownership move of the factory-domain tables from
``aiask-quant-core`` into ``strategy-factory``. A move must be provably
structure-preserving. This module reads the live schema and produces a
deterministic fingerprint that a migration test can compare before and after.

Read-only: nothing here creates, alters, or drops anything.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Iterable
import logging

logger = logging.getLogger(__name__)


#: Prefixes identifying tables owned by the factory domain, i.e. the ones whose
#: ownership is expected to move. Kept as prefixes because the set grows.
FACTORY_TABLE_PREFIXES: tuple[str, ...] = (
    "strategy_factory_",
    "strategy_incubation_",
    "factory_run_",
)

#: ``strategy_vector_*`` / ``vector_index_registry`` are declared in
#: ``schema_strategy.py`` but belong to the host's vector platform, not the
#: factory domain: verified by reference count — ``strategy-factory`` reads none
#: of them, while ``akshare-mcp`` uses all of them. Excluded explicitly so the
#: ``strategy_`` prefix cannot sweep them in.
VECTOR_PLATFORM_TABLES: frozenset[str] = frozenset(
    {
        "strategy_vector_profiles",
        "strategy_vector_profile_store",
        "strategy_vector_index_snapshots",
        "strategy_vector_index_items",
        "strategy_vector_index_item_store",
        "vector_index_registry",
    }
)

#: Factory-domain tables that do not share a common prefix.
FACTORY_TABLE_NAMES: frozenset[str] = frozenset(
    {
        "strategies",
        "strategy_domain_events",
        "strategy_signals",
        "strategy_metrics",
        "strategy_reviews",
        "strategy_lineage",
        "strategy_status_events",
        "strategy_task_runs",
        "strategy_elimination_log",
        "strategy_promotion_reviews",
        "strategy_quality_reports",
        "strategy_paper_sessions",
        "strategy_closure_snapshots",
        "strategy_subscriptions",
        "strategy_signal_event_snapshots",
        "strategy_projection_snapshots",
        "strategy_generation_experiments",
        "strategy_execution_audit_snapshots",
        "strategy_runtime_alerts",
        "strategy_runtime_controls",
        "strategy_runtime_risk_events",
        "strategy_runtime_risk_snapshots",
        "strategy_trade_predictions",
        "strategy_trade_prediction_outcomes",
        "signal_forward_returns",
        "daily_snapshot_history",
        "governance_report_snapshots",
        "matching_engine_worker_state",
    }
)


def is_factory_table(name: str) -> bool:
    """Whether ``name`` belongs to the factory domain."""
    table = str(name or "")
    # Explicit exclusion beats prefix matching: these live in schema_strategy.py
    # but are owned by the host's vector platform.
    if table in VECTOR_PLATFORM_TABLES:
        return False
    if table in FACTORY_TABLE_NAMES:
        return True
    return any(table.startswith(prefix) for prefix in FACTORY_TABLE_PREFIXES)


def _normalize_column(row: Any) -> dict[str, Any]:
    """Reduce a ``PRAGMA table_info`` row to the fields that matter structurally.

    Column order and declared type are significant; the ``cid`` index is not, so
    it is dropped to keep the fingerprint stable across harmless reorderings of
    the DDL that produce the same logical shape.
    """
    def _get(key: str, index: int) -> Any:
        try:
            return row[key]
        except (KeyError, IndexError, TypeError):
            try:
                return row[index]
            except (IndexError, TypeError, KeyError) as _swallowed:
                logger.warning("packages/aiask-quant-core/src/aiask_quant_core/storage/sqlite/factory_schema_fingerprint.py:109: swallowed %s and continued: %s", type(_swallowed).__name__, _swallowed)
                return None

    return {
        "name": str(_get("name", 1) or ""),
        "type": str(_get("type", 2) or "").upper(),
        "notnull": int(_get("notnull", 3) or 0),
        "pk": int(_get("pk", 5) or 0),
    }


async def collect_factory_schema(conn) -> dict[str, list[dict[str, Any]]]:
    """Read the live structure of every factory-domain table present.

    Returns a mapping of table name to its column list, sorted by column name so
    the result does not depend on physical ordering.
    """
    rows = await conn.fetch(
        "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
    )
    schema: dict[str, list[dict[str, Any]]] = {}
    for row in rows or []:
        try:
            name = str(row["name"])
        except (KeyError, IndexError, TypeError) as _swallowed:
            logger.warning("packages/aiask-quant-core/src/aiask_quant_core/storage/sqlite/factory_schema_fingerprint.py:133: swallowed %s and continued: %s", type(_swallowed).__name__, _swallowed)
            continue
        if not is_factory_table(name):
            continue
        info = await conn.fetch(f"PRAGMA table_info({name})")
        columns = [_normalize_column(item) for item in info or []]
        schema[name] = sorted(columns, key=lambda column: column["name"])
    return schema


def fingerprint_schema(schema: dict[str, list[dict[str, Any]]]) -> str:
    """Deterministic digest of a collected schema."""
    canonical = json.dumps(schema, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


async def fingerprint_factory_schema(conn) -> str:
    """Convenience wrapper: collect then digest."""
    return fingerprint_schema(await collect_factory_schema(conn))


def diff_schemas(
    before: dict[str, list[dict[str, Any]]],
    after: dict[str, list[dict[str, Any]]],
) -> dict[str, Any]:
    """Structural difference between two collected schemas.

    Reports table-level and column-level changes so a failed ownership move
    names exactly what drifted instead of only reporting a digest mismatch.
    """
    before_tables = set(before)
    after_tables = set(after)

    changed: dict[str, Any] = {}
    for table in sorted(before_tables & after_tables):
        before_columns = {column["name"]: column for column in before[table]}
        after_columns = {column["name"]: column for column in after[table]}
        dropped = sorted(set(before_columns) - set(after_columns))
        added = sorted(set(after_columns) - set(before_columns))
        altered = sorted(
            name
            for name in set(before_columns) & set(after_columns)
            if before_columns[name] != after_columns[name]
        )
        if dropped or added or altered:
            changed[table] = {
                "dropped_columns": dropped,
                "added_columns": added,
                "altered_columns": altered,
            }

    return {
        "dropped_tables": sorted(before_tables - after_tables),
        "added_tables": sorted(after_tables - before_tables),
        "changed_tables": changed,
        "identical": not (before_tables - after_tables)
        and not (after_tables - before_tables)
        and not changed,
    }


def assert_migration_preserved_structure(
    before: dict[str, list[dict[str, Any]]],
    after: dict[str, list[dict[str, Any]]],
    *,
    allow_added: bool = True,
) -> None:
    """Raise unless a migration preserved the factory-domain structure.

    Additions are permitted by default (a migration may introduce tables or
    columns); losses never are, because a dropped table or column is data loss.
    """
    diff = diff_schemas(before, after)
    problems: list[str] = []
    if diff["dropped_tables"]:
        problems.append(f"dropped tables: {diff['dropped_tables']}")
    for table, detail in diff["changed_tables"].items():
        if detail["dropped_columns"]:
            problems.append(f"{table}: dropped columns {detail['dropped_columns']}")
        if detail["altered_columns"]:
            problems.append(f"{table}: altered columns {detail['altered_columns']}")
    if not allow_added and diff["added_tables"]:
        problems.append(f"unexpected new tables: {diff['added_tables']}")
    if problems:
        raise AssertionError(
            "factory-domain schema migration was not structure-preserving: "
            + "; ".join(problems)
        )


FACTORY_FINGERPRINT_NAMESPACE = "factory_domain_structure"


async def record_factory_schema_fingerprint(
    conn,
    *,
    migration_key: str = "factory_domain_v1",
) -> dict[str, Any]:
    """Persist the current structural fingerprint into the migration ledger.

    Unlike ``record_schema_namespace_checkpoint`` — which hashes a module-name
    string and refreshes its checksum on drift — this stores a digest of the
    actual table/column structure and **reports** drift instead of hiding it.

    Returns the observed fingerprint, the previously recorded one (if any), and
    whether they differ. Never raises on drift: the caller decides whether a
    change is a legitimate migration or a regression.
    """
    from .schema_app_core import ensure_schema_migration_table

    await ensure_schema_migration_table(conn)

    schema = await collect_factory_schema(conn)
    fingerprint = fingerprint_schema(schema)

    existing = await conn.fetchrow(
        """
        SELECT checksum
          FROM mcp_schema_migrations
         WHERE namespace = $1
           AND migration_key = $2
         LIMIT 1
        """,
        FACTORY_FINGERPRINT_NAMESPACE,
        migration_key,
    )
    previous = str(existing["checksum"]) if existing else None

    if previous is None:
        await conn.execute(
            """
            INSERT INTO mcp_schema_migrations
                (namespace, migration_key, checksum, source_module)
            VALUES ($1, $2, $3, $4)
            """,
            FACTORY_FINGERPRINT_NAMESPACE,
            migration_key,
            fingerprint,
            "aiask_quant_core.storage.sqlite.factory_schema_fingerprint",
        )

    return {
        "namespace": FACTORY_FINGERPRINT_NAMESPACE,
        "migration_key": migration_key,
        "fingerprint": fingerprint,
        "previous_fingerprint": previous,
        "changed": previous is not None and previous != fingerprint,
        "table_count": len(schema),
    }


def missing_expected_tables(schema: Iterable[str]) -> list[str]:
    """Named factory tables absent from a collected schema."""
    present = set(schema)
    return sorted(FACTORY_TABLE_NAMES - present)


__all__ = [
    "FACTORY_FINGERPRINT_NAMESPACE",
    "FACTORY_TABLE_NAMES",
    "FACTORY_TABLE_PREFIXES",
    "VECTOR_PLATFORM_TABLES",
    "assert_migration_preserved_structure",
    "collect_factory_schema",
    "diff_schemas",
    "fingerprint_factory_schema",
    "fingerprint_schema",
    "is_factory_table",
    "missing_expected_tables",
    "record_factory_schema_fingerprint",
]
