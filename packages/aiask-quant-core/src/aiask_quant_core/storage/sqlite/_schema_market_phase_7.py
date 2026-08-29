from ._schema_market_common import _run_market_migration_once, logger


async def init_market_tables_phase_7(conn) -> None:
    # NOTE: The CREATE TABLE for strategy_signal_event_snapshots was
    # previously duplicated here and in schema_strategy_parts/schema_definitions.py.
    # The strategy_parts version is the authoritative one (it lives in the
    # strategy schema namespace where the table belongs); this file only
    # covers signals-related ALTERs / indexes / view / backfill, which are
    # market-side migrations.
    await conn.execute(
        """
        ALTER TABLE strategy_signals ADD COLUMN IF NOT EXISTS execution_semantic_mode TEXT;
        ALTER TABLE strategy_signals ADD COLUMN IF NOT EXISTS action_source TEXT;
        ALTER TABLE strategy_signals ADD COLUMN IF NOT EXISTS event_action TEXT;
        ALTER TABLE strategy_signals ADD COLUMN IF NOT EXISTS action_reason TEXT;
        ALTER TABLE strategy_signals ADD COLUMN IF NOT EXISTS signal_metadata TEXT DEFAULT '{}';

        CREATE INDEX IF NOT EXISTS idx_strategy_signals_semantic_mode
            ON strategy_signals(execution_semantic_mode, signal_date DESC);
        CREATE INDEX IF NOT EXISTS idx_strategy_signals_action_source
            ON strategy_signals(action_source, signal_date DESC);

        CREATE INDEX IF NOT EXISTS idx_strategy_signal_event_snapshots_lookup
            ON strategy_signal_event_snapshots(strategy_id, code, as_of_date DESC, updated_at DESC);
        CREATE INDEX IF NOT EXISTS idx_strategy_signal_event_snapshots_strategy
            ON strategy_signal_event_snapshots(strategy_id, as_of_date DESC, updated_at DESC);
        """
    )

    await conn.execute(
        """
        DROP VIEW IF EXISTS strategy_signal_event_snapshots_latest;
        CREATE VIEW IF NOT EXISTS strategy_signal_event_snapshots_latest AS
        SELECT
            id,
            strategy_id,
            code,
            as_of_date,
            latest_bar_date,
            latest_bar_signal,
            execution_semantic_mode,
            latest_event_index,
            latest_event_date,
            latest_event_signal,
            latest_event_action,
            latest_event_action_source,
            latest_event_reason,
            latest_event_units,
            latest_entry_date,
            latest_exit_date,
            event_count,
            recent_events,
            metadata,
            created_at,
            updated_at
        FROM (
            SELECT *,
                   ROW_NUMBER() OVER (
                       PARTITION BY strategy_id, code
                       ORDER BY as_of_date DESC, updated_at DESC, id DESC
                   ) AS rn
            FROM strategy_signal_event_snapshots
        )
        WHERE rn = 1;
        """
    )

    strategy_signals_exists = await conn.fetchval(
        "SELECT 1 FROM sqlite_master WHERE type IN ('table', 'view') AND name = $1",
        "strategy_signals",
    )
    if strategy_signals_exists:
        await _run_market_migration_once(
            conn,
            "strategy_signals_action_metadata_backfill_v1",
            """
            UPDATE strategy_signals
            SET execution_semantic_mode = COALESCE(
                    execution_semantic_mode,
                    NULLIF(json_extract(signal_metadata, '$.execution_semantic_mode'), '')
                ),
                action_source = COALESCE(
                    action_source,
                    NULLIF(json_extract(signal_metadata, '$.action_source'), '')
                ),
                event_action = COALESCE(
                    event_action,
                    NULLIF(json_extract(signal_metadata, '$.event_action'), '')
                ),
                action_reason = COALESCE(
                    action_reason,
                    NULLIF(json_extract(signal_metadata, '$.action_reason'), '')
                )
            WHERE signal_metadata IS NOT NULL;
            """,
        )
    logger.info("Market tables phase 7 initialized")
