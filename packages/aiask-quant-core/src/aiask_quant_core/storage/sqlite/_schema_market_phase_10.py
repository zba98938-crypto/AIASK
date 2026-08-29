"""Market temperature snapshot cache schema."""

from ._schema_market_common import logger


async def init_market_tables_phase_10(conn) -> None:
    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS market_temperature_snapshots (
            as_of TEXT PRIMARY KEY,
            contract_version TEXT NOT NULL,
            market_temperature REAL,
            market_state TEXT,
            stock_count INTEGER DEFAULT 0,
            industry_count INTEGER DEFAULT 0,
            quality_status TEXT,
            warnings TEXT DEFAULT '[]',
            snapshot_json TEXT NOT NULL,
            source_chain TEXT DEFAULT '[]',
            request_json TEXT DEFAULT '{}',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        );

        CREATE INDEX IF NOT EXISTS idx_market_temperature_as_of
            ON market_temperature_snapshots(as_of DESC);
        CREATE INDEX IF NOT EXISTS idx_market_temperature_state
            ON market_temperature_snapshots(market_state, as_of DESC);
        CREATE INDEX IF NOT EXISTS idx_market_temperature_quality
            ON market_temperature_snapshots(quality_status, as_of DESC);
        """
    )
    logger.info("Market tables phase 10 (market temperature snapshots) initialized")
