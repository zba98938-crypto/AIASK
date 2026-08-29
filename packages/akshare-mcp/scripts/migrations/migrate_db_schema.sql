-- 数据库架构迁移脚本
-- 用于统一Node.js和Python版本的表结构

-- 1. 修改 stock_quotes 表，添加缺失字段
ALTER TABLE stock_quotes ADD COLUMN IF NOT EXISTS name TEXT;
ALTER TABLE stock_quotes ADD COLUMN IF NOT EXISTS change_amt REAL;
ALTER TABLE stock_quotes ADD COLUMN IF NOT EXISTS pe REAL;
ALTER TABLE stock_quotes ADD COLUMN IF NOT EXISTS pb REAL;
ALTER TABLE stock_quotes ADD COLUMN IF NOT EXISTS mkt_cap REAL;

-- 重命名字段以统一命名（如果存在旧字段）
-- SQLite 脚本不做条件重命名；运行时 schema 已创建标准列。

-- 2. 确保 stocks 表使用统一的列名
ALTER TABLE stocks ADD COLUMN IF NOT EXISTS stock_code TEXT;
ALTER TABLE stocks ADD COLUMN IF NOT EXISTS stock_name TEXT;

-- 3. 创建索引以提高查询性能
CREATE INDEX IF NOT EXISTS idx_stock_quotes_code_time ON stock_quotes(code, time DESC);
CREATE INDEX IF NOT EXISTS idx_stocks_code ON stocks(stock_code);
CREATE INDEX IF NOT EXISTS idx_stocks_name ON stocks(stock_name);
CREATE INDEX IF NOT EXISTS idx_stocks_market_cap ON stocks(market_cap DESC);

-- 4. SQLite 不支持 COMMENT ON COLUMN。


-- 5. 创建 market_blocks 表（如果不存在）
CREATE TABLE IF NOT EXISTS market_blocks (
    block_code TEXT NOT NULL,
    block_name TEXT NOT NULL,
    block_type TEXT NOT NULL,
    stock_count INTEGER DEFAULT 0,
    avg_change_pct REAL,
    total_amount REAL,
    leader_code TEXT,
    leader_name TEXT,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (block_code, block_type)
);

CREATE INDEX IF NOT EXISTS idx_market_blocks_type ON market_blocks(block_type);
CREATE INDEX IF NOT EXISTS idx_market_blocks_updated ON market_blocks(updated_at DESC);

COMMENT ON TABLE market_blocks IS '市场板块数据';
COMMENT ON COLUMN market_blocks.block_code IS '板块代码';
COMMENT ON COLUMN market_blocks.block_name IS '板块名称';
COMMENT ON COLUMN market_blocks.block_type IS '板块类型(industry/concept/region)';
COMMENT ON COLUMN market_blocks.stock_count IS '成分股数量';
COMMENT ON COLUMN market_blocks.avg_change_pct IS '平均涨跌幅';
COMMENT ON COLUMN market_blocks.total_amount IS '总成交额';
COMMENT ON COLUMN market_blocks.leader_code IS '领涨股代码';
COMMENT ON COLUMN market_blocks.leader_name IS '领涨股名称';
