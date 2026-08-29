"""TDX Phase 8 — TDX-side data tables.

Schema for the 8 dedicated TDX tables consumed by the new sync chain
(``data_sync_scheduler`` Phase 2). All tables follow the project conventions:
SQLite-flavored idempotent DDL with ``CREATE TABLE IF NOT EXISTS`` /
``ADD COLUMN IF NOT EXISTS``.

Table summary (rationale and field source):

- ``tdx_financial_pro``  专业财务数据 (FN1‥FN584). Stored long-format so that
  field set can grow without ALTER. Source: ``tq.get_financial_data``.
- ``tdx_stock_extra``    Per-stock daily extra metrics from
  ``tq.get_more_info`` 88-field snapshot (PE/PB/换手率/量比/封单额/连板天/
  最近大事日 等).
- ``tdx_consensus``      盈利预测 / 一致预期 / 业绩预告 / 业绩快报
  (GO1-GO47). Source: ``tq.get_gp_one_data``.
- ``tdx_gpjy_daily``     龙虎榜/融资融券/陆股通/大宗交易/股息率/涨跌停盘中
  字段 (GP01-GP46). Source: ``tq.get_gpjy_value``.
- ``tdx_bkjy_daily``     板块统计 (BK5-BK19, PE/PB/PS/市值/陆股通/融资融券).
  Source: ``tq.get_bkjy_value``.
- ``tdx_scjy_daily``     市场统计 (SC01-SC42, 北向/融资融券/龙虎榜/ETF/央行
  投放/新高新低 等). Source: ``tq.get_scjy_value``.
- ``tdx_kzz_basic``      可转债基础信息 (转股价/强赎触发价/回售触发价/到期
  日/评级 等). Source: ``tq.get_kzz_info``.
- ``tdx_relation``       股票↔板块归属（行业/概念/风格/地区/指数 5 类）.
  Source: ``tq.get_relation``.
"""

from ._schema_market_common import logger


async def init_market_tables_phase_8(conn) -> None:
    """Create / migrate the 8 TDX-specific tables. Idempotent."""

    # 1. tdx_financial_pro — long-format FN snapshot
    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS tdx_financial_pro (
            code TEXT NOT NULL,
            report_date TEXT NOT NULL,         -- YYYYMMDD (tag_time)
            announce_date TEXT,                -- YYYYMMDD (announce_time)
            fn_code TEXT NOT NULL,             -- e.g. 'FN1', 'FN232'
            value REAL,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (code, report_date, fn_code)
        );
        CREATE INDEX IF NOT EXISTS idx_tdx_financial_pro_code_date
            ON tdx_financial_pro (code, report_date DESC);
        CREATE INDEX IF NOT EXISTS idx_tdx_financial_pro_fn
            ON tdx_financial_pro (fn_code, report_date DESC);
        CREATE INDEX IF NOT EXISTS idx_tdx_financial_pro_announce
            ON tdx_financial_pro (announce_date DESC);
        """
    )

    # 2. tdx_stock_extra — per-day snapshot of get_more_info
    # 用列存储热门数值字段，剩余原始 JSON 兜底，避免每天爆 ALTER。
    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS tdx_stock_extra (
            code TEXT NOT NULL,
            trade_date TEXT NOT NULL,          -- YYYY-MM-DD
            pe_ttm REAL,
            pe_dynamic REAL,
            pb_mrq REAL,
            ps_ttm REAL,
            dy_ratio REAL,                     -- 股息率 %
            turnover_rate REAL,                -- fHSL 换手率
            volume_ratio REAL,                 -- fLianB 量比
            zsz REAL,                          -- 总市值（亿）
            ltsz REAL,                         -- 流通市值（亿）
            free_float_shares REAL,            -- 自由流通股本（万）
            up_limit REAL,                     -- 涨停价
            down_limit REAL,                   -- 跌停价
            zaf REAL,                          -- 当日涨幅 %
            ma5 REAL,
            hist_high_52w REAL,
            hist_low_52w REAL,
            fc_amo REAL,                       -- 封单额（万）
            fc_b REAL,                         -- 封成比
            ever_zt_count INTEGER,             -- 连板天
            con_zaf_date_num INTEGER,          -- 连涨天
            year_zt_day INTEGER,               -- 年涨停天数
            zjl_hb REAL,                       -- 主力净流入（万）
            kf_earn_money REAL,                -- 扣非净利润（万）
            rd_input_fee REAL,                 -- 研发费用（万）
            cash_zj REAL,                      -- 货币资金（万）
            staff_num INTEGER,                 -- 员工人数
            ipo_price REAL,
            beta_value REAL,
            recent_buyback_date TEXT,
            recent_release_date TEXT,
            recent_dz_date TEXT,
            report_date TEXT,                  -- 最近财报公告日
            zt_date_recent TEXT,
            dt_date_recent TEXT,
            top_date_recent TEXT,
            stop_jy_date_recent TEXT,
            tp_flag TEXT,                      -- 停牌标识
            raw_json TEXT,                     -- 原始 88 字段 JSON
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (code, trade_date)
        );
        CREATE INDEX IF NOT EXISTS idx_tdx_stock_extra_code_date
            ON tdx_stock_extra (code, trade_date DESC);
        CREATE INDEX IF NOT EXISTS idx_tdx_stock_extra_pe_pb
            ON tdx_stock_extra (trade_date, pe_ttm, pb_mrq);
        """
    )

    # 3. tdx_consensus — GO1..GO47 横表
    # GO 字段都是单点（最新值），存为列方便上层 JOIN。
    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS tdx_consensus (
            code TEXT NOT NULL,
            snapshot_date TEXT NOT NULL,       -- YYYY-MM-DD
            ipo_price REAL,                    -- GO1
            issue_volume_wan REAL,             -- GO2
            target_price REAL,                 -- GO3
            consensus_year_t INTEGER,          -- GO4
            eps_t REAL,                        -- GO5
            eps_t1 REAL,                       -- GO6
            eps_t2 REAL,                       -- GO7
            net_profit_t REAL,                 -- GO8
            net_profit_t1 REAL,                -- GO9
            net_profit_t2 REAL,                -- GO10
            revenue_t REAL,                    -- GO11
            revenue_t1 REAL,                   -- GO12
            revenue_t2 REAL,                   -- GO13
            op_profit_t REAL,                  -- GO14
            op_profit_t1 REAL,                 -- GO15
            op_profit_t2 REAL,                 -- GO16
            bvps_t REAL,                       -- GO17
            bvps_t1 REAL,                      -- GO18
            bvps_t2 REAL,                      -- GO19
            roe_t REAL,                        -- GO20
            roe_t1 REAL,                       -- GO21
            roe_t2 REAL,                       -- GO22
            pe_t REAL,                         -- GO23
            pe_t1 REAL,                        -- GO24
            pe_t2 REAL,                        -- GO25
            recent_release_date TEXT,          -- GO26
            recent_release_volume REAL,        -- GO27
            next_report_date TEXT,             -- GO28
            inst_holding_count INTEGER,        -- GO29
            inst_holding_volume REAL,          -- GO30
            fund_holding_count INTEGER,        -- GO31
            fund_holding_volume REAL,          -- GO32
            total_shares_wan REAL,             -- GO33
            float_shares_wan REAL,             -- GO34
            forecast_report_date TEXT,         -- GO35
            forecast_low REAL,                 -- GO36
            forecast_high REAL,                -- GO37
            forecast_yoy_low REAL,             -- GO38
            forecast_yoy_high REAL,            -- GO39
            flash_report_date TEXT,            -- GO40
            flash_net_profit REAL,             -- GO41
            dividend_total REAL,               -- GO42
            ipo_total REAL,                    -- GO43
            forecast_ex_low REAL,              -- GO44
            forecast_ex_high REAL,             -- GO45
            forecast_ex_yoy_low REAL,          -- GO46
            forecast_ex_yoy_high REAL,         -- GO47
            raw_json TEXT,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (code, snapshot_date)
        );
        CREATE INDEX IF NOT EXISTS idx_tdx_consensus_target_pe
            ON tdx_consensus (snapshot_date, target_price, pe_t);
        """
    )

    # 4. tdx_gpjy_daily — 个股交易数据 long-format（每日多 GP 字段）
    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS tdx_gpjy_daily (
            code TEXT NOT NULL,
            trade_date TEXT NOT NULL,
            gp_code TEXT NOT NULL,             -- GP01..GP46
            value_a REAL,                      -- 第一字段（如 龙虎榜买入额）
            value_b REAL,                      -- 第二字段（如 龙虎榜卖出额）
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (code, trade_date, gp_code)
        );
        CREATE INDEX IF NOT EXISTS idx_tdx_gpjy_code_date
            ON tdx_gpjy_daily (code, trade_date DESC);
        CREATE INDEX IF NOT EXISTS idx_tdx_gpjy_gp_date
            ON tdx_gpjy_daily (gp_code, trade_date DESC);
        """
    )

    # 5. tdx_bkjy_daily — 板块统计 long-format
    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS tdx_bkjy_daily (
            block_code TEXT NOT NULL,
            trade_date TEXT NOT NULL,
            bk_code TEXT NOT NULL,             -- BK5..BK19
            value_a REAL,
            value_b REAL,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (block_code, trade_date, bk_code)
        );
        CREATE INDEX IF NOT EXISTS idx_tdx_bkjy_block_date
            ON tdx_bkjy_daily (block_code, trade_date DESC);
        CREATE INDEX IF NOT EXISTS idx_tdx_bkjy_bk_date
            ON tdx_bkjy_daily (bk_code, trade_date DESC);
        """
    )

    # 6. tdx_scjy_daily — 市场统计 long-format
    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS tdx_scjy_daily (
            trade_date TEXT NOT NULL,
            sc_code TEXT NOT NULL,             -- SC01..SC42
            value_a REAL,
            value_b REAL,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (trade_date, sc_code)
        );
        CREATE INDEX IF NOT EXISTS idx_tdx_scjy_sc_date
            ON tdx_scjy_daily (sc_code, trade_date DESC);
        """
    )

    # 7. tdx_kzz_basic — 可转债基础数据（每日刷新一次）
    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS tdx_kzz_basic (
            kzz_code TEXT PRIMARY KEY,
            stock_code TEXT,
            set_code TEXT,
            convert_price REAL,
            current_rate REAL,
            remain_size_wan REAL,
            putback_price REAL,
            force_redeem_price REAL,
            convert_date TEXT,
            end_price REAL,
            end_date TEXT,
            convert_rate REAL,
            real_value REAL,
            expire_yield REAL,
            kzz_score TEXT,
            stock_score TEXT,
            redeem_date TEXT,
            redeem_price REAL,
            put_date TEXT,
            put_price REAL,
            convert_code TEXT,
            stock_price REAL,
            kzz_price REAL,
            premium_rate REAL,
            convert_value REAL,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS idx_tdx_kzz_stock
            ON tdx_kzz_basic (stock_code);
        """
    )

    # 8. tdx_relation — 股票板块归属
    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS tdx_relation (
            code TEXT NOT NULL,
            block_code TEXT NOT NULL,
            block_name TEXT,
            block_type TEXT,                   -- 行业/概念/风格/地区/指数
            gp_num INTEGER,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (code, block_code)
        );
        CREATE INDEX IF NOT EXISTS idx_tdx_relation_block
            ON tdx_relation (block_code);
        CREATE INDEX IF NOT EXISTS idx_tdx_relation_type
            ON tdx_relation (block_type, code);
        """
    )

    # 9. stocks 表扩展 TDX 字段 (additive only — 不破坏旧字段)
    await conn.execute(
        """
        ALTER TABLE stocks ADD COLUMN IF NOT EXISTS tdx_industry TEXT;
        ALTER TABLE stocks ADD COLUMN IF NOT EXISTS tdx_region TEXT;
        ALTER TABLE stocks ADD COLUMN IF NOT EXISTS tdx_listed_date TEXT;
        ALTER TABLE stocks ADD COLUMN IF NOT EXISTS tdx_total_shares REAL;
        ALTER TABLE stocks ADD COLUMN IF NOT EXISTS tdx_float_shares REAL;
        ALTER TABLE stocks ADD COLUMN IF NOT EXISTS list_status TEXT DEFAULT 'L';
        ALTER TABLE stocks ADD COLUMN IF NOT EXISTS dividend_yield REAL;
        ALTER TABLE stocks ADD COLUMN IF NOT EXISTS turnover_rate REAL;
        """
    )

    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS tdx_data_completeness (
            data_key TEXT PRIMARY KEY,
            status TEXT NOT NULL,
            as_of_date TEXT,
            row_count INTEGER DEFAULT 0,
            detail TEXT DEFAULT '{}',
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        """
    )

    logger.info("Market tables phase 8 (TDX) initialized")
