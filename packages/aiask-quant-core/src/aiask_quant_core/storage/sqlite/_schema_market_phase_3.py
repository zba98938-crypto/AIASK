from ._schema_market_common import (
    _ensure_foreign_key,
    _run_market_migration_once,
    logger,
)


async def init_market_tables_phase_3(conn) -> None:
    # 7. 回测结果表
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS backtest_results (
            id TEXT PRIMARY KEY,
            code TEXT,
            strategy TEXT NOT NULL,
            params TEXT,
            stocks TEXT,
            start_date TEXT NOT NULL,
            end_date TEXT NOT NULL,
            initial_capital REAL NOT NULL,
            final_capital REAL NOT NULL,
            total_return REAL,
            annual_return REAL,
            max_drawdown REAL,
            sharpe_ratio REAL,
            sortino_ratio REAL,
            win_rate REAL,
            profit_factor REAL,
            avg_win REAL,
            avg_loss REAL,
            expectancy REAL,
            avg_holding_days REAL,
            exposure_rate REAL,
            max_consecutive_loss INTEGER,
            trades_count INTEGER,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );

        CREATE INDEX IF NOT EXISTS idx_backtest_results_code ON backtest_results(code);

        CREATE TABLE IF NOT EXISTS backtest_trades (
            id TEXT PRIMARY KEY,
            backtest_id TEXT NOT NULL,
            stock_code TEXT NOT NULL,
            action TEXT NOT NULL,
            price REAL NOT NULL,
            shares INTEGER NOT NULL,
            gross_value REAL NOT NULL,
            fee REAL DEFAULT 0,
            slippage REAL DEFAULT 0,
            net_value REAL NOT NULL,
            cash_balance REAL NOT NULL,
            equity REAL NOT NULL,
            trade_date TEXT NOT NULL,
            reason TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS backtest_equity (
            id INTEGER PRIMARY KEY,
            backtest_id TEXT NOT NULL,
            date TEXT NOT NULL,
            close REAL,
            cash REAL NOT NULL,
            shares INTEGER,
            equity REAL NOT NULL,
            daily_return REAL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(backtest_id, date)
        );

        CREATE INDEX IF NOT EXISTS idx_backtest_trades_id
        ON backtest_trades(backtest_id, trade_date);

        CREATE INDEX IF NOT EXISTS idx_backtest_equity_id
        ON backtest_equity(backtest_id, date);
    """)
    await _ensure_foreign_key(
        conn,
        table_name="backtest_trades",
        constraint_name="fk_backtest_trades_backtest_id",
        definition="FOREIGN KEY (backtest_id) REFERENCES backtest_results(id) ON DELETE CASCADE",
    )
    await _ensure_foreign_key(
        conn,
        table_name="backtest_equity",
        constraint_name="fk_backtest_equity_backtest_id",
        definition="FOREIGN KEY (backtest_id) REFERENCES backtest_results(id) ON DELETE CASCADE",
    )

    # 8. 告警表
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS alerts (
            id INTEGER PRIMARY KEY,
            user_id TEXT DEFAULT 'default',
            code TEXT,
            indicator TEXT,
            condition TEXT,
            value REAL,
            status TEXT DEFAULT 'active',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS price_alerts (
            id INTEGER PRIMARY KEY,
            stock_code TEXT NOT NULL,
            target_price REAL,
            condition TEXT,
            status TEXT DEFAULT 'active',
            triggered_at TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS combo_alerts (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            conditions TEXT NOT NULL,
            logic TEXT NOT NULL DEFAULT 'and',
            status TEXT DEFAULT 'active',
            triggered_at TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS indicator_alerts (
            id INTEGER PRIMARY KEY,
            stock_code TEXT NOT NULL,
            indicator TEXT NOT NULL,
            condition TEXT NOT NULL,
            threshold REAL,
            status TEXT DEFAULT 'active',
            triggered_at TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
    """)

    # 8.1 兼容旧库：补齐 alerts.user_id
    await conn.execute("""
        ALTER TABLE alerts
        ADD COLUMN IF NOT EXISTS user_id TEXT DEFAULT 'default';
    """)

    # 8.2 事件审计表（告警/订单）
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS alert_events (
            id INTEGER PRIMARY KEY,
            alert_id TEXT,
            event_type TEXT NOT NULL,
            payload TEXT DEFAULT '{}',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS idx_alert_events_alert ON alert_events(alert_id, created_at DESC);

        CREATE TABLE IF NOT EXISTS order_events (
            id INTEGER PRIMARY KEY,
            order_id TEXT NOT NULL,
            account_id TEXT,
            code TEXT,
            event_type TEXT NOT NULL,
            payload TEXT DEFAULT '{}',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS idx_order_events_order ON order_events(order_id, created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_order_events_account ON order_events(account_id, created_at DESC);
    """)

    # 9. 自选股表
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS watchlist_groups (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            user_id TEXT DEFAULT 'default',
            color TEXT DEFAULT '#6366f1',
            sort_order INTEGER DEFAULT 0,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );

        INSERT INTO watchlist_groups (id, name, sort_order)
        VALUES ('default', '默认分组', 0) ON CONFLICT DO NOTHING;

        CREATE TABLE IF NOT EXISTS watchlist (
            id INTEGER PRIMARY KEY,
            user_id TEXT DEFAULT 'default',
            code TEXT NOT NULL,
            name TEXT,
            group_id TEXT DEFAULT 'default',
            sort_order INTEGER DEFAULT 0,
            tags TEXT DEFAULT '[]',
            notes TEXT,
            note TEXT,
            added_at TEXT DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(user_id, code)
        );

        ALTER TABLE watchlist_groups
        ADD COLUMN IF NOT EXISTS color TEXT DEFAULT '#6366f1';

        ALTER TABLE watchlist
        ADD COLUMN IF NOT EXISTS sort_order INTEGER DEFAULT 0;
    """)

    # 10. 向量检索表（基础，非 sqlite_python）
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS stock_embeddings (
            stock_code TEXT PRIMARY KEY,
            embedding TEXT,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS pattern_vectors (
            id INTEGER PRIMARY KEY,
            stock_code TEXT,
            window_size INTEGER,
            embedding TEXT,
            start_date TEXT,
            end_date TEXT,
            pattern_type TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS vector_documents (
            id INTEGER PRIMARY KEY,
            stock_code TEXT,
            doc_type TEXT,
            content TEXT,
            date TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );

        CREATE INDEX IF NOT EXISTS idx_vector_doc_stock_type_date
        ON vector_documents(stock_code, doc_type, date DESC);
    """)

    # 11. 市场板块表
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS market_blocks (
            id INTEGER PRIMARY KEY,
            block_code TEXT NOT NULL,
            block_name TEXT NOT NULL,
            block_type TEXT NOT NULL,
            stock_count INTEGER DEFAULT 0,
            avg_change_pct REAL,
            total_amount REAL,
            leader_code TEXT,
            leader_name TEXT,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(block_code, block_type)
        );

        CREATE INDEX IF NOT EXISTS idx_market_blocks_type
        ON market_blocks(block_type);

        CREATE INDEX IF NOT EXISTS idx_market_blocks_updated
        ON market_blocks(updated_at DESC);
    """)

    # 12. 板块成分股表
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS block_stocks (
            id INTEGER PRIMARY KEY,
            block_code TEXT NOT NULL,
            stock_code TEXT NOT NULL,
            stock_name TEXT,
            weight REAL,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(block_code, stock_code)
        );

        CREATE INDEX IF NOT EXISTS idx_block_stocks_block
        ON block_stocks(block_code);

        CREATE INDEX IF NOT EXISTS idx_block_stocks_stock
        ON block_stocks(stock_code);
    """)

    # 13. 数据质量表
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS data_quality_issues (
            id INTEGER PRIMARY KEY,
            dataset TEXT,
            stock_code TEXT,
            reason TEXT,
            source TEXT,
            payload TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
    """)

    # 14. 数据同步任务表
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS sync_tasks (
            id INTEGER PRIMARY KEY,
            task_id TEXT UNIQUE NOT NULL,
            task_type TEXT NOT NULL,
            codes TEXT,
            priority TEXT DEFAULT 'normal',
            status TEXT DEFAULT 'pending',
            progress INTEGER DEFAULT 0,
            total INTEGER DEFAULT 0,
            error_message TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
            completed_at TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_sync_tasks_status ON sync_tasks(status);
        CREATE INDEX IF NOT EXISTS idx_sync_tasks_created ON sync_tasks(created_at DESC);
    """)

    # 15. 数据同步调度表
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS sync_schedules (
            id INTEGER PRIMARY KEY,
            schedule_id TEXT UNIQUE NOT NULL,
            task_type TEXT NOT NULL,
            codes TEXT,
            schedule TEXT NOT NULL,
            params TEXT DEFAULT '{}',
            enabled INTEGER DEFAULT 1,
            last_run TEXT,
            next_run TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        );

        CREATE INDEX IF NOT EXISTS idx_sync_schedules_enabled ON sync_schedules(enabled);
        CREATE INDEX IF NOT EXISTS idx_sync_schedules_next_run ON sync_schedules(next_run);
    """)
    await conn.execute("""
        ALTER TABLE sync_schedules
        ADD COLUMN IF NOT EXISTS params TEXT DEFAULT '{}'
    """)

    # 16. 事件表
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY,
            code TEXT NOT NULL,
            event_type TEXT NOT NULL,
            event_date TEXT NOT NULL,
            title TEXT,
            description TEXT,
            status TEXT DEFAULT 'pending',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        );

        CREATE INDEX IF NOT EXISTS idx_events_code ON events(code);
        CREATE INDEX IF NOT EXISTS idx_events_date ON events(event_date);
        CREATE INDEX IF NOT EXISTS idx_events_type ON events(event_type);
    """)

    # 17. 用户表
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id TEXT PRIMARY KEY,
            username TEXT UNIQUE NOT NULL,
            email TEXT,
            settings TEXT DEFAULT '{}',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        );

        INSERT INTO users (id, username)
        VALUES ('default', 'default') ON CONFLICT DO NOTHING;
    """)

    # 18. 选股策略表
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS screener_strategies (
            id INTEGER PRIMARY KEY,
            user_id TEXT DEFAULT 'default',
            name TEXT NOT NULL,
            criteria TEXT NOT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        );

        CREATE INDEX IF NOT EXISTS idx_screener_strategies_user ON screener_strategies(user_id);
    """)
