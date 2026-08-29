-- SQLite schema repair script kept for archived maintenance runs.

-- 1. Standard stock identifiers.
ALTER TABLE stocks
ADD COLUMN IF NOT EXISTS stock_code TEXT;

-- Historical code -> stock_code backfill needs dynamic SQL when the legacy
-- column exists; runtime schema initialization performs that guarded backfill.

CREATE UNIQUE INDEX IF NOT EXISTS idx_stocks_stock_code
ON stocks(stock_code);

-- 2. Common compatibility columns.
ALTER TABLE financials
ADD COLUMN IF NOT EXISTS eps REAL;

ALTER TABLE sync_tasks
ADD COLUMN IF NOT EXISTS updated_at TEXT DEFAULT CURRENT_TIMESTAMP;

DELETE FROM watchlist WHERE code IS NULL;

-- 3. Valuation and backtest tables.
CREATE TABLE IF NOT EXISTS valuation_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    stock_code TEXT NOT NULL,
    date TEXT NOT NULL,
    pe REAL,
    pb REAL,
    ps REAL,
    market_cap REAL,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(stock_code, date)
);

CREATE INDEX IF NOT EXISTS idx_valuation_history_stock_code
ON valuation_history(stock_code, date DESC);

CREATE TABLE IF NOT EXISTS backtest_results (
    id TEXT PRIMARY KEY,
    code TEXT,
    strategy TEXT NOT NULL,
    start_date TEXT,
    end_date TEXT,
    initial_capital REAL DEFAULT 100000,
    final_value REAL,
    total_return REAL,
    annual_return REAL,
    sharpe_ratio REAL,
    max_drawdown REAL,
    win_rate REAL,
    total_trades INTEGER,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
);

-- 4. Repair summary.
SELECT 'stocks.stock_code' AS check_name,
       EXISTS (SELECT 1 FROM pragma_table_info('stocks') WHERE name = 'stock_code') AS ok
UNION ALL
SELECT 'financials.eps',
       EXISTS (SELECT 1 FROM pragma_table_info('financials') WHERE name = 'eps')
UNION ALL
SELECT 'sync_tasks.updated_at',
       EXISTS (SELECT 1 FROM pragma_table_info('sync_tasks') WHERE name = 'updated_at');
