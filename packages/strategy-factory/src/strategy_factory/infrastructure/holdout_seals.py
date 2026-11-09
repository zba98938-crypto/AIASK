"""Append-only SQLite storage for prospective research holdout seals.

This store is initialized only by the explicit freeze operation. It has no
foreign key to mutable strategy rows and never uses the bounded JSON writer.
Triggers prevent accidental application overwrites, not filesystem-owner edits.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Any


TABLE = "strategy_holdout_seals"
SCHEMA = """
CREATE TABLE IF NOT EXISTS strategy_holdout_seals (
    seal_id TEXT PRIMARY KEY,
    experiment_key TEXT NOT NULL UNIQUE,
    source_run_id TEXT NOT NULL,
    candidate_id TEXT NOT NULL,
    frozen_at_utc TEXT NOT NULL,
    planned_start TEXT NOT NULL,
    planned_end TEXT NOT NULL,
    min_sessions INTEGER NOT NULL CHECK (min_sessions >= 20),
    snapshot_sha256 TEXT NOT NULL,
    snapshot_json TEXT NOT NULL,
    CHECK (planned_start <= planned_end),
    CHECK (planned_start > date(frozen_at_utc, '+8 hours'))
);
CREATE TRIGGER IF NOT EXISTS strategy_holdout_seals_no_update
BEFORE UPDATE ON strategy_holdout_seals
BEGIN SELECT RAISE(ABORT, 'holdout_seal_immutable'); END;
CREATE TRIGGER IF NOT EXISTS strategy_holdout_seals_no_delete
BEFORE DELETE ON strategy_holdout_seals
BEGIN SELECT RAISE(ABORT, 'holdout_seal_immutable'); END;
CREATE TRIGGER IF NOT EXISTS strategy_holdout_seals_no_replace
BEFORE INSERT ON strategy_holdout_seals
WHEN EXISTS (SELECT 1 FROM strategy_holdout_seals
             WHERE seal_id = NEW.seal_id OR experiment_key = NEW.experiment_key)
BEGIN SELECT RAISE(ABORT, 'holdout_seal_immutable'); END;
"""


def canonical_json(value: Any) -> str:
    """Reject non-JSON/nonfinite values instead of coercing or truncating them."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def json_digest(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def open_seal_writer(path: str | Path) -> sqlite3.Connection:
    connection = sqlite3.connect(str(Path(path).expanduser().resolve()), timeout=30)
    connection.row_factory = sqlite3.Row
    try:
        connection.executescript(SCHEMA)
    except BaseException:
        connection.close()
        raise
    return connection


def database_utc_now(connection: sqlite3.Connection) -> str:
    # No supplied clock, environment variable or caller timestamp is accepted.
    return connection.execute("SELECT strftime('%Y-%m-%dT%H:%M:%fZ', 'now')").fetchone()[0]


def decode_seal(row: sqlite3.Row) -> dict[str, Any]:
    seal = dict(row)
    raw = seal.pop("snapshot_json")
    if hashlib.sha256(raw.encode("utf-8")).hexdigest() != seal["snapshot_sha256"]:
        raise ValueError("holdout_seal_snapshot_hash_mismatch")
    snapshot = json.loads(raw)
    for key in ("seal_id", "experiment_key", "source_run_id", "candidate_id", "frozen_at_utc", "planned_start", "planned_end", "min_sessions"):
        if snapshot.get(key) != seal[key]:
            raise ValueError("holdout_seal_snapshot_binding_mismatch")
    seal["snapshot"] = snapshot
    return seal


def insert_seal(connection: sqlite3.Connection, snapshot: dict[str, Any]) -> dict[str, Any]:
    raw = canonical_json(snapshot)
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    connection.execute(
        "INSERT INTO strategy_holdout_seals "
        "(seal_id, experiment_key, source_run_id, candidate_id, frozen_at_utc, planned_start, planned_end, min_sessions, snapshot_sha256, snapshot_json) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        tuple(snapshot[key] for key in ("seal_id", "experiment_key", "source_run_id", "candidate_id", "frozen_at_utc", "planned_start", "planned_end", "min_sessions")) + (digest, raw),
    )
    return decode_seal(connection.execute("SELECT * FROM strategy_holdout_seals WHERE seal_id=?", (snapshot["seal_id"],)).fetchone())


def read_seals(path: str | Path) -> tuple[str | None, list[dict[str, Any]], str | None]:
    """Read only: never create a file, table, trigger or migration."""
    database = Path(path).expanduser().resolve()
    if not database.is_file():
        return None, [], "holdout_seal_database_missing"
    connection = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=30)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA query_only=ON")
        now = database_utc_now(connection)
        if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (TABLE,)).fetchone() is None:
            return now, [], "holdout_seal_table_missing"
        rows = connection.execute("SELECT * FROM strategy_holdout_seals ORDER BY frozen_at_utc, seal_id").fetchall()
        return now, [decode_seal(row) for row in rows], None
    finally:
        connection.close()
