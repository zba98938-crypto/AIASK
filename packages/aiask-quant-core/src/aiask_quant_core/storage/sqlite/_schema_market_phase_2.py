from ._schema_market_common import (
    _keep_time_series_table,
    _ensure_foreign_key,
    _ensure_market_schema_migration_table,
    _run_market_migration_once,
    _table_columns,
    logger,
)


async def init_market_tables_phase_2(conn) -> None:
    # 4. 实时行情表
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS stock_quotes (
            time TEXT NOT NULL,
            code TEXT NOT NULL,
            name TEXT,
            price REAL,
            change_pct REAL,
            change_amt REAL,
            open REAL,
            high REAL,
            low REAL,
            prev_close REAL,
            volume INTEGER,
            amount REAL,
            pe REAL,
            pb REAL,
            mkt_cap REAL,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        );

        CREATE UNIQUE INDEX IF NOT EXISTS idx_stock_quotes_time_code
        ON stock_quotes (time, code);

        CREATE INDEX IF NOT EXISTS idx_stock_quotes_code_time_desc
        ON stock_quotes (code, time DESC);
    """)
    await _keep_time_series_table(conn, "stock_quotes", "time")
    await _ensure_market_schema_migration_table(conn)

    # 4.1 北向资金日汇总
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS north_fund_flow (
            trade_date TEXT PRIMARY KEY,
            north_money REAL,
            south_money REAL,
            net_amount REAL,
            ggt_ss REAL,
            ggt_sz REAL,
            hgt REAL,
            sgt REAL,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        );

        CREATE INDEX IF NOT EXISTS idx_north_fund_flow_trade_date_desc
        ON north_fund_flow (trade_date DESC);
    """)
    await _keep_time_series_table(conn, "north_fund_flow", "trade_date")
    await conn.execute("""
        ALTER TABLE north_fund_flow
        ADD COLUMN IF NOT EXISTS ggt_ss REAL;
    """)
    await conn.execute("""
        ALTER TABLE north_fund_flow
        ADD COLUMN IF NOT EXISTS ggt_sz REAL;
    """)
    await conn.execute("""
        ALTER TABLE north_fund_flow
        ADD COLUMN IF NOT EXISTS hgt REAL;
    """)
    await conn.execute("""
        ALTER TABLE north_fund_flow
        ADD COLUMN IF NOT EXISTS sgt REAL;
    """)
    await conn.execute("""
        ALTER TABLE north_fund_flow
        ADD COLUMN IF NOT EXISTS updated_at TEXT DEFAULT CURRENT_TIMESTAMP;
    """)
    await conn.execute("""
        ALTER TABLE north_fund_flow
        ADD COLUMN IF NOT EXISTS source TEXT;
    """)
    await conn.execute("""
        ALTER TABLE north_fund_flow
        ADD COLUMN IF NOT EXISTS source_priority TEXT;
    """)

    # 4.1.1 个股资金流快照
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS stock_fund_flow (
            code TEXT NOT NULL,
            trade_date TEXT NOT NULL,
            name TEXT,
            main_net_inflow REAL,
            main_inflow_percent REAL,
            super_large_net_inflow REAL,
            large_net_inflow REAL,
            middle_net_inflow REAL,
            small_net_inflow REAL,
            source TEXT,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (code, trade_date)
        );

        CREATE INDEX IF NOT EXISTS idx_stock_fund_flow_code_trade_date_desc
        ON stock_fund_flow (code, trade_date DESC);
    """)
    await _keep_time_series_table(conn, "stock_fund_flow", "trade_date")

    # 4.2 融资融券市场汇总
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS margin_market_flow (
            trade_date TEXT NOT NULL,
            exchange_id TEXT NOT NULL DEFAULT 'SSE',
            rzye REAL,
            rzmre REAL,
            rzche REAL,
            rqye REAL,
            rqmcl REAL,
            rqyl REAL,
            rzrqye REAL,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (trade_date, exchange_id)
        );

        CREATE INDEX IF NOT EXISTS idx_margin_market_flow_trade_date_desc
        ON margin_market_flow (trade_date DESC, exchange_id);
    """)
    await conn.execute("""
        ALTER TABLE margin_market_flow
        ADD COLUMN IF NOT EXISTS source TEXT;
    """)
    await conn.execute("""
        ALTER TABLE margin_market_flow
        ADD COLUMN IF NOT EXISTS source_priority TEXT;
    """)

    # 4.3 融资融券个股明细
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS margin_detail (
            trade_date TEXT NOT NULL,
            ts_code TEXT NOT NULL,
            rzye REAL,
            rqye REAL,
            rzmre REAL,
            rqyl REAL,
            rzche REAL,
            rqchl REAL,
            rqmcl REAL,
            rzrqye REAL,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (trade_date, ts_code)
        );

        CREATE INDEX IF NOT EXISTS idx_margin_detail_ts_code_trade_date_desc
        ON margin_detail (ts_code, trade_date DESC);
    """)
    await conn.execute("""
        ALTER TABLE margin_detail
        ADD COLUMN IF NOT EXISTS source TEXT;
    """)
    await conn.execute("""
        ALTER TABLE margin_detail
        ADD COLUMN IF NOT EXISTS source_priority TEXT;
    """)

    # 4.1 兼容旧库：补齐 stock_quotes 历史缺失列
    await conn.execute("""
        ALTER TABLE stock_quotes
        ADD COLUMN IF NOT EXISTS name TEXT;
    """)
    await conn.execute("""
        ALTER TABLE stock_quotes
        ADD COLUMN IF NOT EXISTS prev_close REAL;
    """)
    await conn.execute("""
        ALTER TABLE stock_quotes
        ADD COLUMN IF NOT EXISTS change_amt REAL;
    """)
    await conn.execute("""
        ALTER TABLE stock_quotes
        ADD COLUMN IF NOT EXISTS mkt_cap REAL;
    """)
    await conn.execute("""
        ALTER TABLE stock_quotes
        ADD COLUMN IF NOT EXISTS pe REAL;
    """)
    await conn.execute("""
        ALTER TABLE stock_quotes
        ADD COLUMN IF NOT EXISTS pb REAL;
    """)

    # 4.2 兼容旧库：若历史表未建唯一索引，补齐后确保 UPSERT 可用
    await conn.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_stock_quotes_time_code
        ON stock_quotes (time, code);

        CREATE INDEX IF NOT EXISTS idx_stock_quotes_code_time_desc
        ON stock_quotes (code, time DESC);
    """)

    # 4.3 兼容旧库：将 change 列历史数据回填到标准列 change_amt
    quote_columns = await _table_columns(conn, "stock_quotes")
    if "change" in quote_columns:
        await _run_market_migration_once(conn, "stock_quotes_backfill_change_amt", """
            UPDATE stock_quotes
            SET change_amt = COALESCE(change_amt, "change")
            WHERE change_amt IS NULL;
        """)

    # 4.4 兼容旧库：将 pre_close/market_cap 历史数据回填到标准列
    if "pre_close" in quote_columns:
        await _run_market_migration_once(conn, "stock_quotes_backfill_prev_close", """
            UPDATE stock_quotes
            SET prev_close = COALESCE(prev_close, pre_close)
            WHERE prev_close IS NULL;
        """)
    if "market_cap" in quote_columns:
        await _run_market_migration_once(conn, "stock_quotes_backfill_mkt_cap", """
            UPDATE stock_quotes
            SET mkt_cap = COALESCE(mkt_cap, market_cap)
            WHERE mkt_cap IS NULL;
        """)

    # 4.5 兼容旧库：补齐 updated_at 列并回填
    await conn.execute("""
        ALTER TABLE stock_quotes
        ADD COLUMN IF NOT EXISTS updated_at TEXT DEFAULT CURRENT_TIMESTAMP;
    """)
    await _run_market_migration_once(conn, "stock_quotes_backfill_updated_at", """
        UPDATE stock_quotes
        SET updated_at = COALESCE(updated_at, CURRENT_TIMESTAMP)
        WHERE updated_at IS NULL;
    """)

    # 4.6 兼容旧库：SQLite 不能安全删除/改写已有列约束，这里只保留标准列并做数据回填。

    # 5. 组合管理表
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS portfolios (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            description TEXT,
            metadata TEXT DEFAULT '{}',
            user_id TEXT DEFAULT 'default',
            initial_capital REAL NOT NULL,
            current_value REAL NOT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS holdings (
            id INTEGER PRIMARY KEY,
            portfolio_id INTEGER NOT NULL,
            code TEXT NOT NULL,
            shares INTEGER NOT NULL,
            cost_price REAL NOT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(portfolio_id, code)
        );
    """)

    # 5.1 兼容旧库：补齐 portfolios.description
    await conn.execute("""
        ALTER TABLE portfolios
        ADD COLUMN IF NOT EXISTS description TEXT;
    """)

    await conn.execute("""
        ALTER TABLE portfolios
        ADD COLUMN IF NOT EXISTS metadata TEXT DEFAULT '{}';
    """)
    await _ensure_foreign_key(
        conn,
        table_name="holdings",
        constraint_name="fk_holdings_portfolio_id",
        definition="FOREIGN KEY (portfolio_id) REFERENCES portfolios(id) ON DELETE CASCADE",
    )

    # 6. 模拟交易表
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS paper_accounts (
            id TEXT PRIMARY KEY,
            user_id TEXT DEFAULT 'default',
            name TEXT NOT NULL,
            initial_capital REAL NOT NULL,
            current_capital REAL NOT NULL,
            total_value REAL NOT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS paper_positions (
            id INTEGER PRIMARY KEY,
            account_id TEXT NOT NULL,
            stock_code TEXT NOT NULL,
            stock_name TEXT NOT NULL,
            quantity INTEGER NOT NULL,
            cost_price REAL NOT NULL,
            current_price REAL,
            market_value REAL,
            profit_rate REAL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(account_id, stock_code)
        );

        CREATE TABLE IF NOT EXISTS paper_trades (
            id TEXT PRIMARY KEY,
            account_id TEXT NOT NULL,
            stock_code TEXT NOT NULL,
            stock_name TEXT NOT NULL,
            trade_type TEXT NOT NULL,
            price REAL NOT NULL,
            quantity INTEGER NOT NULL,
            amount REAL NOT NULL,
            commission REAL DEFAULT 0,
            trade_time TEXT NOT NULL,
            reason TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );

        CREATE INDEX IF NOT EXISTS idx_paper_trades_account
        ON paper_trades(account_id, trade_time DESC);
    """)
