"""SQLite persistence helpers for the factor mining factory pool."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)


FACTOR_POOL_DDL = """
CREATE TABLE IF NOT EXISTS factor_pool_active (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    factor_id TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    family TEXT NOT NULL,
    expression_dsl TEXT NOT NULL,
    inputs TEXT,
    status TEXT DEFAULT 'active',
    admission_date TEXT,
    admission_ic REAL,
    admission_grade TEXT,
    current_ic REAL,
    decay_rate REAL DEFAULT 0.0,
    orthogonal_ratio REAL,
    pool_weight REAL DEFAULT 0.0,
    generation_engine TEXT,
    generation_trace TEXT,
    validation_summary TEXT,
    hypothesis TEXT,
    fitness REAL DEFAULT 0.0,
    last_evaluated_at TEXT,
    retired_at TEXT,
    retired_reason TEXT,
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_factor_pool_status ON factor_pool_active(status);
CREATE INDEX IF NOT EXISTS idx_factor_pool_family ON factor_pool_active(family);
CREATE INDEX IF NOT EXISTS idx_factor_pool_fitness ON factor_pool_active(fitness);
"""

FACTOR_DECAY_HISTORY_DDL = """
CREATE TABLE IF NOT EXISTS factor_pool_decay_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    factor_id TEXT NOT NULL,
    measured_at TEXT NOT NULL,
    rolling_ic_20d REAL,
    rolling_ic_60d REAL,
    admission_ic REAL,
    current_ic REAL,
    decay_rate REAL,
    estimated_half_life REAL,
    alert_triggered INTEGER DEFAULT 0,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_decay_history_factor ON factor_pool_decay_history(factor_id);
"""

FACTOR_MINING_RUNS_DDL = """
CREATE TABLE IF NOT EXISTS factor_mining_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL UNIQUE,
    trigger TEXT,
    started_at TEXT,
    completed_at TEXT,
    status TEXT,
    engines_used TEXT,
    raw_candidate_count INTEGER DEFAULT 0,
    evolved_count INTEGER DEFAULT 0,
    validated_count INTEGER DEFAULT 0,
    admitted_count INTEGER DEFAULT 0,
    pool_size_after INTEGER DEFAULT 0,
    report TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_mining_runs_status ON factor_mining_runs(status);
"""


async def _execute_script(db: Any, *scripts: str) -> bool:
    if hasattr(db, "acquire"):
        async with db.acquire() as conn:
            for script in scripts:
                await conn.execute(script)
        return True
    if hasattr(db, "execute_raw"):
        for script in scripts:
            await db.execute_raw(script)
        return True
    raw_conn = getattr(db, "conn", None) or getattr(db, "connection", None)
    if raw_conn is not None:
        for script in scripts:
            raw_conn.executescript(script)
        raw_conn.commit()
        return True
    return False


async def ensure_factor_pool_tables(db: Any) -> bool:
    """Ensure factor pool persistence tables exist."""
    try:
        return await _execute_script(
            db,
            FACTOR_POOL_DDL,
            FACTOR_DECAY_HISTORY_DDL,
            FACTOR_MINING_RUNS_DDL,
        )
    except Exception as exc:
        logger.warning("ensure_factor_pool_tables failed: %s", exc)
        return False


def _encode_record(record: dict[str, Any]) -> tuple[Any, ...]:
    return (
        record.get("factor_id"),
        record.get("name"),
        record.get("family"),
        record.get("expression_dsl"),
        json.dumps(record.get("inputs", []), ensure_ascii=False),
        record.get("status", "active"),
        record.get("admission_date"),
        record.get("admission_ic"),
        record.get("admission_grade"),
        record.get("current_ic"),
        record.get("decay_rate", 0.0),
        record.get("orthogonal_ratio"),
        record.get("pool_weight", 0.0),
        record.get("generation_engine"),
        json.dumps(record.get("generation_trace", {}), ensure_ascii=False),
        json.dumps(record.get("validation_summary", {}), ensure_ascii=False),
        record.get("hypothesis"),
        record.get("fitness", 0.0),
        record.get("last_evaluated_at"),
        record.get("retired_at"),
        record.get("retired_reason"),
        datetime.now(timezone.utc).isoformat(),
    )


async def save_factor_to_pool(db: Any, record: dict[str, Any]) -> dict[str, Any]:
    """Persist one factor into the active pool table."""
    values = _encode_record(record)
    sql_pg = """
        INSERT OR REPLACE INTO factor_pool_active
            (factor_id, name, family, expression_dsl, inputs, status,
             admission_date, admission_ic, admission_grade, current_ic,
             decay_rate, orthogonal_ratio, pool_weight, generation_engine,
             generation_trace, validation_summary, hypothesis, fitness,
             last_evaluated_at, retired_at, retired_reason, updated_at)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10,
                $11, $12, $13, $14, $15, $16, $17, $18, $19, $20,
                $21, $22)
    """
    sql_qmark = """
        INSERT OR REPLACE INTO factor_pool_active
            (factor_id, name, family, expression_dsl, inputs, status,
             admission_date, admission_ic, admission_grade, current_ic,
             decay_rate, orthogonal_ratio, pool_weight, generation_engine,
             generation_trace, validation_summary, hypothesis, fitness,
             last_evaluated_at, retired_at, retired_reason, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """
    try:
        if hasattr(db, "acquire"):
            async with db.acquire() as conn:
                await conn.execute(sql_pg, *values)
        else:
            raw_conn = getattr(db, "conn", None) or getattr(db, "connection", None)
            if raw_conn is None:
                return {"saved": False, "reason": "db_not_supported"}
            raw_conn.execute(sql_qmark, values)
            raw_conn.commit()
        return {"saved": True, "factor_id": record.get("factor_id")}
    except Exception as exc:
        logger.debug("save_factor_to_pool failed: %s", exc)
        return {"saved": False, "error": str(exc)}


def _decode_factor_row(row: dict[str, Any]) -> dict[str, Any]:
    record = dict(row)
    for json_field in ("inputs", "generation_trace", "validation_summary"):
        if isinstance(record.get(json_field), str):
            try:
                record[json_field] = json.loads(record[json_field])
            except Exception:
                pass
    return record


async def load_active_pool_from_db(db: Any) -> list[dict[str, Any]]:
    """Load active factors from the database."""
    try:
        if hasattr(db, "acquire"):
            async with db.acquire() as conn:
                rows = await conn.fetch(
                    "SELECT * FROM factor_pool_active WHERE status = 'active' ORDER BY fitness DESC"
                )
            return [_decode_factor_row(dict(row)) for row in rows or []]
        raw_conn = getattr(db, "conn", None) or getattr(db, "connection", None)
        if raw_conn is None:
            return []
        cursor = raw_conn.execute(
            "SELECT * FROM factor_pool_active WHERE status = 'active' ORDER BY fitness DESC"
        )
        columns = [desc[0] for desc in cursor.description]
        return [_decode_factor_row(dict(zip(columns, row))) for row in cursor.fetchall()]
    except Exception as exc:
        logger.debug("load_active_pool_from_db failed: %s", exc)
        return []


async def load_factor_pool_from_db(
    db: Any,
    *,
    statuses: tuple[str, ...] = ("active",),
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Load factor pool rows for diagnostics and gateway consumers."""
    statuses = tuple(str(item) for item in statuses if str(item))
    if not statuses:
        return []
    try:
        if hasattr(db, "acquire"):
            placeholders = ", ".join(f"${idx}" for idx in range(1, len(statuses) + 1))
            async with db.acquire() as conn:
                rows = await conn.fetch(
                    f"""
                    SELECT * FROM factor_pool_active
                    WHERE status IN ({placeholders})
                    ORDER BY fitness DESC
                    LIMIT ${len(statuses) + 1}
                    """,
                    *statuses,
                    int(limit),
                )
            return [_decode_factor_row(dict(row)) for row in rows or []]
        raw_conn = getattr(db, "conn", None) or getattr(db, "connection", None)
        if raw_conn is None:
            return []
        placeholders = ", ".join("?" for _ in statuses)
        cursor = raw_conn.execute(
            f"""
            SELECT * FROM factor_pool_active
            WHERE status IN ({placeholders})
            ORDER BY fitness DESC
            LIMIT ?
            """,
            [*statuses, int(limit)],
        )
        columns = [desc[0] for desc in cursor.description]
        return [_decode_factor_row(dict(zip(columns, row))) for row in cursor.fetchall()]
    except Exception as exc:
        logger.debug("load_factor_pool_from_db failed: %s", exc)
        return []


def _encode_run(report: dict[str, Any]) -> tuple[Any, ...]:
    return (
        report.get("run_id"),
        report.get("trigger"),
        report.get("started_at"),
        report.get("completed_at"),
        "completed" if report.get("success") else "failed",
        json.dumps(report.get("engines_used", []), ensure_ascii=False),
        report.get("raw_candidate_count", 0),
        report.get("evolved_count", 0),
        report.get("validated_count", 0),
        report.get("admitted_count", 0),
        report.get("pool_size", 0),
        json.dumps(report, ensure_ascii=False, default=str),
    )


async def save_mining_run(db: Any, report: dict[str, Any]) -> dict[str, Any]:
    """Persist a mining cycle report."""
    values = _encode_run(report)
    sql_pg = """
        INSERT OR REPLACE INTO factor_mining_runs
            (run_id, trigger, started_at, completed_at, status,
             engines_used, raw_candidate_count, evolved_count, validated_count,
             admitted_count, pool_size_after, report)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)
    """
    sql_qmark = """
        INSERT OR REPLACE INTO factor_mining_runs
            (run_id, trigger, started_at, completed_at, status,
             engines_used, raw_candidate_count, evolved_count, validated_count,
             admitted_count, pool_size_after, report)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """
    try:
        if hasattr(db, "acquire"):
            async with db.acquire() as conn:
                await conn.execute(sql_pg, *values)
        else:
            raw_conn = getattr(db, "conn", None) or getattr(db, "connection", None)
            if raw_conn is None:
                return {"saved": False, "reason": "db_not_supported"}
            raw_conn.execute(sql_qmark, values)
            raw_conn.commit()
        return {"saved": True, "run_id": report.get("run_id")}
    except Exception as exc:
        logger.debug("save_mining_run failed: %s", exc)
        return {"saved": False, "error": str(exc)}


async def save_decay_measurement(db: Any, measurement: dict[str, Any]) -> dict[str, Any]:
    """Persist one factor decay measurement."""
    values = (
        measurement.get("factor_id"),
        measurement.get("measured_at"),
        measurement.get("rolling_ic_20d"),
        measurement.get("rolling_ic_60d"),
        measurement.get("admission_ic"),
        measurement.get("current_ic"),
        measurement.get("decay_rate"),
        measurement.get("estimated_half_life_days") or measurement.get("estimated_half_life"),
        1 if measurement.get("decay_rate", 0) > 0.3 else 0,
    )
    sql_pg = """
        INSERT INTO factor_pool_decay_history
            (factor_id, measured_at, rolling_ic_20d, rolling_ic_60d,
             admission_ic, current_ic, decay_rate, estimated_half_life, alert_triggered)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
    """
    sql_qmark = """
        INSERT INTO factor_pool_decay_history
            (factor_id, measured_at, rolling_ic_20d, rolling_ic_60d,
             admission_ic, current_ic, decay_rate, estimated_half_life, alert_triggered)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """
    try:
        if hasattr(db, "acquire"):
            async with db.acquire() as conn:
                await conn.execute(sql_pg, *values)
        else:
            raw_conn = getattr(db, "conn", None) or getattr(db, "connection", None)
            if raw_conn is None:
                return {"saved": False, "reason": "db_not_supported"}
            raw_conn.execute(sql_qmark, values)
            raw_conn.commit()
        return {"saved": True}
    except Exception as exc:
        logger.debug("save_decay_measurement failed: %s", exc)
        return {"saved": False, "error": str(exc)}
