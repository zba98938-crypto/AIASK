-- 数据库迁移脚本：修复字段名不一致问题
-- 运行方式: sqlite script runner

-- 1. 为 watchlist 表添加 user_id 字段（如果不存在）
ALTER TABLE watchlist ADD COLUMN IF NOT EXISTS user_id TEXT DEFAULT 'default';

-- 2. 为 watchlist 表添加 note 字段（如果不存在）
ALTER TABLE watchlist ADD COLUMN IF NOT EXISTS note TEXT;

-- 3. 为 paper_accounts 表添加 user_id 字段（如果不存在）
ALTER TABLE paper_accounts ADD COLUMN IF NOT EXISTS user_id TEXT DEFAULT 'default';

-- 4. 为 backtest_results 表添加 code 字段（如果不存在）
ALTER TABLE backtest_results ADD COLUMN IF NOT EXISTS code TEXT;
CREATE INDEX IF NOT EXISTS idx_backtest_results_code ON backtest_results(code);

-- 5. 创建 events 表（如果不存在）
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
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

-- 6. 创建 users 表（如果不存在）
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

-- 7. 创建 screener_strategies 表（如果不存在）
CREATE TABLE IF NOT EXISTS screener_strategies (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT DEFAULT 'default',
    name TEXT NOT NULL,
    criteria TEXT NOT NULL,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_screener_strategies_user ON screener_strategies(user_id);

-- 完成
SELECT 'Migration completed successfully' as status;
