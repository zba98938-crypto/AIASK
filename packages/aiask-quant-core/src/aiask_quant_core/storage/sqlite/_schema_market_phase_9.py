"""Factory runtime market schema extensions.

These tables persist factor-mining factory state. They live in the SQLite
bootstrap so factory output is durable and can be consumed only through DB
reads after restart.
"""

from ._schema_market_common import logger


async def init_market_tables_phase_9(conn) -> None:
    await conn.execute(
        """
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

        CREATE INDEX IF NOT EXISTS idx_factor_pool_status
            ON factor_pool_active(status);
        CREATE INDEX IF NOT EXISTS idx_factor_pool_family
            ON factor_pool_active(family);
        CREATE INDEX IF NOT EXISTS idx_factor_pool_fitness
            ON factor_pool_active(fitness);

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

        CREATE INDEX IF NOT EXISTS idx_decay_history_factor
            ON factor_pool_decay_history(factor_id);

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

        CREATE INDEX IF NOT EXISTS idx_mining_runs_status
            ON factor_mining_runs(status);
        """
    )
    logger.info("Market tables phase 9 (factory runtime) initialized")
