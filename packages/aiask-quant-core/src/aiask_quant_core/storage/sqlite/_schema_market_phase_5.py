from ._schema_market_common import _run_market_migration_once, logger


async def init_market_tables_phase_5(conn) -> None:
    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS strategy_candidate_evidence (
            id TEXT PRIMARY KEY,
            candidate_id TEXT NOT NULL,
            strategy_id TEXT,
            evidence_id TEXT NOT NULL,
            evidence_type TEXT,
            source_task_key TEXT,
            payload TEXT DEFAULT '{}',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(candidate_id, evidence_id)
        );
        CREATE INDEX IF NOT EXISTS idx_strategy_candidate_evidence_candidate
            ON strategy_candidate_evidence(candidate_id, created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_strategy_candidate_evidence_strategy
            ON strategy_candidate_evidence(strategy_id, created_at DESC);

        CREATE TABLE IF NOT EXISTS strategy_signal_evidence (
            id TEXT PRIMARY KEY,
            signal_id TEXT NOT NULL,
            strategy_id TEXT,
            signal_date TEXT,
            code TEXT,
            evidence_id TEXT NOT NULL,
            payload TEXT DEFAULT '{}',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(signal_id, evidence_id)
        );
        CREATE INDEX IF NOT EXISTS idx_strategy_signal_evidence_signal
            ON strategy_signal_evidence(signal_id, created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_strategy_signal_evidence_strategy
            ON strategy_signal_evidence(strategy_id, signal_date DESC);

        CREATE TABLE IF NOT EXISTS strategy_trade_positions (
            position_id TEXT PRIMARY KEY,
            strategy_id TEXT,
            account_id TEXT NOT NULL,
            signal_id TEXT,
            code TEXT NOT NULL,
            direction TEXT DEFAULT 'long',
            status TEXT DEFAULT 'pending_entry',
            entry_order_id TEXT,
            exit_order_id TEXT,
            entry_trade_id TEXT,
            exit_trade_id TEXT,
            entry_shares INTEGER DEFAULT 0,
            exit_shares INTEGER DEFAULT 0,
            remaining_shares INTEGER DEFAULT 0,
            entry_amount REAL DEFAULT 0,
            exit_amount REAL DEFAULT 0,
            entry_commission REAL DEFAULT 0,
            exit_commission REAL DEFAULT 0,
            realized_pnl REAL,
            realized_return REAL,
            pnl_conversion_efficiency REAL,
            execution_conversion_efficiency REAL,
            trade_expectancy REAL,
            audit_eligible INTEGER DEFAULT FALSE,
            opened_at TEXT,
            closed_at TEXT,
            last_trade_time TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS idx_strategy_trade_positions_strategy
            ON strategy_trade_positions(strategy_id, status, updated_at DESC);
        CREATE INDEX IF NOT EXISTS idx_strategy_trade_positions_account
            ON strategy_trade_positions(account_id, code, updated_at DESC);

        CREATE TABLE IF NOT EXISTS strategy_trade_position_fills (
            fill_id TEXT PRIMARY KEY,
            position_id TEXT NOT NULL,
            trade_id TEXT,
            order_id TEXT,
            signal_id TEXT,
            strategy_id TEXT,
            account_id TEXT NOT NULL,
            code TEXT NOT NULL,
            fill_side TEXT NOT NULL,
            quantity INTEGER NOT NULL,
            price REAL NOT NULL,
            amount REAL NOT NULL,
            commission REAL DEFAULT 0,
            trade_time TEXT NOT NULL,
            payload TEXT DEFAULT '{}',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(trade_id)
        );
        CREATE INDEX IF NOT EXISTS idx_strategy_trade_position_fills_position
            ON strategy_trade_position_fills(position_id, trade_time DESC);
        CREATE INDEX IF NOT EXISTS idx_strategy_trade_position_fills_strategy
            ON strategy_trade_position_fills(strategy_id, trade_time DESC);
        """
    )

    await conn.execute(
        """
        ALTER TABLE paper_orders ADD COLUMN IF NOT EXISTS signal_id TEXT;
        ALTER TABLE paper_orders ADD COLUMN IF NOT EXISTS position_id TEXT;
        ALTER TABLE paper_trades ADD COLUMN IF NOT EXISTS signal_id TEXT;
        ALTER TABLE paper_trades ADD COLUMN IF NOT EXISTS position_id TEXT;
        CREATE INDEX IF NOT EXISTS idx_paper_orders_strategy_signal_position
            ON paper_orders(strategy_id, signal_id, position_id);
        CREATE INDEX IF NOT EXISTS idx_paper_trades_strategy_signal_position
            ON paper_trades(strategy_id, signal_id, position_id);
        """
    )

    await _run_market_migration_once(
        conn,
        "paper_trades_best_effort_position_backfill_v1",
        """
        UPDATE paper_trades AS trades
        SET signal_id = COALESCE(trades.signal_id, orders.signal_id),
            position_id = COALESCE(trades.position_id, orders.position_id),
            strategy_id = COALESCE(trades.strategy_id, orders.strategy_id)
        FROM paper_orders AS orders
        WHERE trades.source_order_id = orders.id
          AND orders.position_id IS NOT NULL
          AND (
              trades.signal_id IS NULL
              OR trades.position_id IS NULL
              OR trades.strategy_id IS NULL
          );
        """,
    )
    logger.info("Market tables phase 5 initialized")
