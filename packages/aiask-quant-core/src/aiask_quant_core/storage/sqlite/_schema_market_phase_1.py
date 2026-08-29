from ._schema_market_common import (
    _keep_time_series_table,
    _ensure_foreign_key,
    _run_market_migration_once,
    _table_columns,
    logger,
)


async def init_market_tables_phase_1(conn) -> None:
    async def init_market_tables(conn) -> None:
        """Create / migrate all market-data tables.

        Args:
            conn: an async SQLite-compatible connection.
        """

        # 1. K线表
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS kline_1d (
                time TEXT NOT NULL,
                code TEXT NOT NULL,
                open REAL NOT NULL,
                high REAL NOT NULL,
                low REAL NOT NULL,
                close REAL NOT NULL,
                volume INTEGER NOT NULL,
                amount REAL,
                turnover REAL,
                change_pct REAL,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (time, code)
            );

            CREATE INDEX IF NOT EXISTS idx_kline_1d_code_time_desc
            ON kline_1d (code, time DESC);

            CREATE INDEX IF NOT EXISTS idx_kline_1d_code_updated_desc
            ON kline_1d (code, updated_at DESC);
        """)
        await _keep_time_series_table(conn, "kline_1d", "time")
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS kline_intraday (
                code TEXT NOT NULL,
                period TEXT NOT NULL,
                "timestamp" TEXT NOT NULL,
                adjust TEXT NOT NULL DEFAULT '',
                open REAL NOT NULL,
                high REAL NOT NULL,
                low REAL NOT NULL,
                close REAL NOT NULL,
                volume REAL,
                amount REAL,
                source TEXT,
                source_chain TEXT DEFAULT '[]',
                data_quality_status TEXT DEFAULT 'ok',
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (code, period, "timestamp", adjust)
            );

            CREATE INDEX IF NOT EXISTS idx_kline_intraday_code_period_time
            ON kline_intraday (code, period, "timestamp");

            CREATE INDEX IF NOT EXISTS idx_kline_intraday_quality_time
            ON kline_intraday (data_quality_status, "timestamp");
        """)

        # 2. 财务数据表
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS financials (
                stock_code TEXT NOT NULL,
                report_date TEXT NOT NULL,
                revenue REAL,
                net_profit REAL,
                gross_margin REAL,
                net_margin REAL,
                debt_ratio REAL,
                current_ratio REAL,
                eps REAL,
                roe REAL,
                bvps REAL,
                roa REAL,
                revenue_growth REAL,
                profit_growth REAL,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (stock_code, report_date)
            );

            CREATE INDEX IF NOT EXISTS idx_financials_report_date_desc
            ON financials (report_date DESC);

            CREATE INDEX IF NOT EXISTS idx_financials_updated_desc
            ON financials (updated_at DESC);
        """)
        await conn.execute("""
            ALTER TABLE financials
            ADD COLUMN IF NOT EXISTS stock_code TEXT;
        """)
        if "code" in await _table_columns(conn, "financials"):
            await _run_market_migration_once(conn, "financials_backfill_stock_code", """
                UPDATE financials
                SET stock_code = COALESCE(NULLIF(stock_code, ''), code)
                WHERE stock_code IS NULL OR stock_code = '';
            """)
        await conn.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS idx_financials_stock_code_report_date
            ON financials (stock_code, report_date);
        """)

        # 3. 股票信息表
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS stocks (
                stock_code TEXT PRIMARY KEY,
                stock_name TEXT NOT NULL,
                market TEXT,
                sector TEXT,
                industry TEXT,
                list_date TEXT,
                market_cap REAL,
                pe_ratio REAL,
                pb_ratio REAL,
                kline_sync_attempted TEXT,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP
            );
        """)
        await conn.execute("""
            ALTER TABLE stocks
            ADD COLUMN IF NOT EXISTS stock_code TEXT;
        """)
        if "code" in await _table_columns(conn, "stocks"):
            await _run_market_migration_once(conn, "stocks_backfill_stock_code", """
                UPDATE stocks
                SET stock_code = COALESCE(NULLIF(stock_code, ''), code)
                WHERE stock_code IS NULL OR stock_code = '';
            """)
        await conn.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS idx_stocks_stock_code
            ON stocks (stock_code);
        """)

    await init_market_tables(conn)
