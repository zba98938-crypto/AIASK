# 数据库Schema与证据表


> **Hermes 集成副本勘误（2026-07-18）**  
> 本文是历史/上游阶段材料，不是当前 Hermes 树完成证明。  
> 在当前 `integrations/aiask/` 副本中：`packages/desktop-api` 不存在；Hermes Desktop 不是完整 AIASK workbench；live trading 默认关闭。  
> 权威状态：`AIASK_FINANCE_AGENT_COMPLETION_AUDIT.md`、`integrations/aiask/docs/CURRENT_PRODUCT_TRUTH.md`、`docs/GAP_INVENTORY_STATUS.md`。

## 文档信息

| 项目 | 内容 |
|---|---|
| 文档版本 | 1.0.0 |
| 更新日期 | 2026-06-21 |
| 数据库路径 | `data/db/akshare_mcp.sqlite3` |
| 数据库大小 | 156 MB (152表) |

## 1. 核心证据表

> 字段定义来源: `packages/aiask-quant-core/src/aiask_quant_core/storage/sqlite/schema_strategy_parts/schema_definitions.py`
> 行数为 2026-06-21 实测值，会随运行持续增长。

### 1.1 策略主表

**表名**: `strategies`

| 字段 | 类型 | 说明 | 约束 |
|------|------|------|------|
| id | TEXT | 主键(业务ID直接作为主键) | PRIMARY KEY |
| name | TEXT | 策略名称 | NOT NULL |
| description | TEXT | 描述 | |
| author_id | TEXT | 作者，默认 'default' | |
| strategy_type | TEXT | 策略类型 | NOT NULL |
| params | TEXT | JSON参数，默认 '{}' | |
| factor_weights | TEXT | JSON因子权重 | |
| status | TEXT | 状态，默认 'draft' | |
| tags | TEXT | JSON标签 | |
| backtest_artifact_id | TEXT | 回测产物ID | |
| subscriber_count | INTEGER | 订阅数 | |
| created_at | TEXT | 创建时间 | |
| updated_at | TEXT | 更新时间 | |

**注意**: 不存在独立的 `strategy_id` 字段，`id` 本身即业务ID（如 `factory_1781953902_bd7f6cd3`）。

**实际数据** (2026-06-21): 23,029行

**索引**:
```sql
CREATE INDEX idx_strategies_status ON strategies(status);
CREATE INDEX idx_strategies_type ON strategies(strategy_type);
CREATE INDEX idx_strategies_author ON strategies(author_id);
```

### 1.2 信号表

**表名**: `strategy_signals`

| 字段 | 类型 | 说明 |
|------|------|------|
| id | INTEGER | 主键(自增) |
| strategy_id | TEXT | 策略ID (NOT NULL) |
| signal_date | TEXT | 信号日期 (NOT NULL) |
| code | TEXT | 股票代码 (NOT NULL) |
| signal | SMALLINT | 信号值(-1/0/1，NOT NULL) |
| score | REAL | 信号分数 |
| execution_semantic_mode | TEXT | 执行语义模式 |
| action_source | TEXT | 动作来源 |
| event_action | TEXT | 事件动作 |
| action_reason | TEXT | 动作原因 |
| signal_metadata | TEXT | JSON元数据 |
| created_at | TEXT | 创建时间 |

**唯一约束**: `(strategy_id, signal_date, code)`

**实际数据**: 19,357行

### 1.3 订单表

**表名**: `paper_orders`

| 字段 | 类型 | 说明 |
|------|------|------|
| id | INTEGER | 主键(自增) |
| account_id | TEXT | 账户ID (NOT NULL) |
| code | TEXT | 股票代码 (NOT NULL) |
| direction | TEXT | 方向(buy/sell，NOT NULL) |
| shares | INTEGER | 股数 (NOT NULL) |
| price | REAL | 价格 |
| status | TEXT | 状态(pending/filled/cancelled) |
| order_type | TEXT | 订单类型 |
| stop_price | REAL | 止损价 |
| filled_at | TEXT | 成交时间 |
| commission | REAL | 佣金 |
| reason | TEXT | 原因 |
| strategy_id | TEXT | 策略ID(可空) |
| signal_date | TEXT | 信号日期 |
| source | TEXT | 来源 |
| signal_id | TEXT | 信号ID(可空) |
| position_id | TEXT | 持仓ID(可空) |
| created_at | TEXT | 创建时间 |
| updated_at | TEXT | 更新时间 |

**注意**: 字段名是 `direction`(非 `side`)、`shares`(非 `quantity`)、`code`(非 `symbol`)。

**实际数据**: 3,840行

### 1.4 成交表

**表名**: `paper_trades`

| 字段 | 类型 | 说明 |
|------|------|------|
| id | TEXT | 主键 |
| account_id | TEXT | 账户ID (NOT NULL) |
| stock_code | TEXT | 股票代码 (NOT NULL) |
| stock_name | TEXT | 股票名称 (NOT NULL) |
| trade_type | TEXT | 交易类型(buy/sell，NOT NULL) |
| price | REAL | 成交价格 (NOT NULL) |
| quantity | INTEGER | 成交数量 (NOT NULL) |
| amount | REAL | 成交金额 (NOT NULL) |
| commission | REAL | 佣金 |
| trade_time | TEXT | 成交时间 (NOT NULL) |
| reason | TEXT | 原因 |
| strategy_id | TEXT | 策略ID(可空) |
| source_order_id | TEXT | 来源订单ID |
| signal_id | TEXT | 信号ID(可空) |
| position_id | TEXT | 持仓ID(可空) |
| created_at | TEXT | 创建时间 |

**注意**: 外键字段是 `source_order_id`(非 `order_id`)、股票字段是 `stock_code`(非 `symbol`)、方向字段是 `trade_type`(非 `side`)。

**实际数据**: 3,769行

### 1.5 持仓表

**表名**: `strategy_trade_positions`

| 字段 | 类型 | 说明 |
|------|------|------|
| position_id | TEXT | 主键 |
| strategy_id | TEXT | 策略ID(可空) |
| account_id | TEXT | 账户ID (NOT NULL) |
| signal_id | TEXT | 信号ID(可空) |
| code | TEXT | 股票代码 (NOT NULL) |
| direction | TEXT | 方向(long/short) |
| status | TEXT | 状态(open/closed) |
| entry_order_id | TEXT | 入场订单ID |
| exit_order_id | TEXT | 出场订单ID |
| entry_trade_id | TEXT | 入场成交ID |
| exit_trade_id | TEXT | 出场成交ID |
| entry_shares | INTEGER | 入场股数 |
| exit_shares | INTEGER | 出场股数 |
| remaining_shares | INTEGER | 剩余股数 |
| entry_amount | REAL | 入场金额 |
| exit_amount | REAL | 出场金额 |
| realized_pnl | REAL | 已实现盈亏 |
| realized_return | REAL | 已实现收益率 |
| trade_expectancy | REAL | 交易期望 |
| audit_eligible | INTEGER | 是否可审计 |
| opened_at | TEXT | 开仓时间 |
| closed_at | TEXT | 平仓时间(可空) |
| hold_days | REAL | 持仓天数 |
| exit_reason | TEXT | 出场原因 |
| price_path_audit_status | TEXT | 价格路径审计状态 |

> 完整字段共 43 个，此处列出关键字段。完整定义见 schema_definitions.py。
> 字段名是 `code`(非 `symbol`)、`entry_shares`(非 `quantity`)、`opened_at`/`closed_at`(非 `entry_time`/`exit_time`)，无 `entry_price`/`exit_price` 字段(用 `entry_amount`/`entry_avg_price`)。

**实际数据**: 3,665行

### 1.6 前向收益表

**表名**: `signal_forward_returns`

| 字段 | 类型 | 说明 |
|------|------|------|
| id | INTEGER | 主键(自增) |
| signal_id | INTEGER | 信号ID (NOT NULL) |
| forward_days | INTEGER | 前向天数 (NOT NULL) |
| actual_return | REAL | 实际收益率 |
| calculated_at | TEXT | 计算时间 |

**唯一约束**: `(signal_id, forward_days)`

**注意**: 字段是 `forward_days`(非 `horizon`)、`actual_return`(非 `forward_return`)，无 `benchmark_return`/`excess_return` 字段。

**实际数据**: 39,449行

### 1.7 孵化指标表

**表名**: `strategy_incubation_metrics`

| 字段 | 类型 | 说明 |
|------|------|------|
| id | INTEGER | 主键(自增) |
| strategy_id | TEXT | 策略ID (NOT NULL) |
| account_id | TEXT | 账户ID |
| metric_date | TEXT | 指标日期 (NOT NULL) |
| stage | TEXT | 阶段 |
| nav | REAL | 净值 |
| daily_return | REAL | 日收益 |
| max_drawdown | REAL | 最大回撤 |
| sharpe_ratio | REAL | Sharpe比率 |
| hit_rate_5d | REAL | 5日命中率 |
| hit_rate_lcb_5d | REAL | 5日命中率置信下界 |
| skill_lcb_5d | REAL | 5日skill置信下界 |
| effective_n_5d | INTEGER | 5日有效样本数 |
| forward_ic_5d | REAL | 5日前向IC |
| forward_sharpe_5d | REAL | 5日前向Sharpe |
| total_signals | INTEGER | 信号总数 |
| total_orders | INTEGER | 订单总数 |
| total_trades | INTEGER | 成交总数 |
| blockers | TEXT | JSON阻塞项 |
| decision | TEXT | 决策 |

> 实际只有 `effective_n_5d`(单一窗口)，无 `effective_n_10d`/`effective_n_20d`。
> 无 `ic_mean_5d`/`sharpe_5d`/`lcb` 字段，对应字段是 `forward_ic_5d`/`sharpe_ratio`/`hit_rate_lcb_5d`/`skill_lcb_5d`。

**实际数据**: 27,406行

### 1.8 审计快照表

**表名**: `strategy_execution_audit_snapshots`

| 字段 | 类型 | 说明 |
|------|------|------|
| strategy_id | TEXT | 主键(每策略一条) |
| snapshot_id | TEXT | 快照ID (NOT NULL, UNIQUE) |
| as_of_date | TEXT | 数据截止日 |
| verdict_status | TEXT | 裁决状态 (NOT NULL，默认 'missing') |
| verdict_reasons | TEXT | JSON裁决原因 |
| execution_hard_gate_passed | INTEGER | hard gate 是否通过 |
| verification | TEXT | JSON验证详情 |
| acceptance | TEXT | JSON验收详情 |
| audit_summary | TEXT | JSON审计摘要 |
| snapshot | TEXT | JSON快照 |
| submission_lane | TEXT | 提交通道 |
| source_run_id | TEXT | 来源运行ID |
| factory_run_id | TEXT | 工厂运行ID |
| created_at | TEXT | 创建时间 |
| updated_at | TEXT | 更新时间 |

> 主键是 `strategy_id`(非 `snapshot_id`)，每策略只保留一条最新快照。
> `realized_trade_count`/`expectancy` 不是独立列，存放在 `audit_summary` JSON 内。
> `verdict_status` 取值: missing/bootstrap_pending/insufficient_samples/failed/bootstrap_ready/passed。

**实际数据**: 20,412行

## 2. 关键查询

> 以下查询已验证可在实际数据库上运行 (SQLite 3.30+)。

### 2.1 证据链完整性

```sql
-- 信号→订单转换率
SELECT 
    COUNT(DISTINCT s.strategy_id) as strategies_with_signals,
    COUNT(DISTINCT o.strategy_id) as strategies_with_orders,
    ROUND(100.0 * COUNT(DISTINCT o.strategy_id) / NULLIF(COUNT(DISTINCT s.strategy_id), 0), 2) as conversion_rate
FROM strategy_signals s
LEFT JOIN paper_orders o ON s.strategy_id = o.strategy_id;

-- 订单→成交转换率（注意: paper_trades 外键字段是 source_order_id）
SELECT 
    COUNT(*) as total_orders,
    COUNT(t.id) as filled_orders,
    ROUND(100.0 * COUNT(t.id) / NULLIF(COUNT(*), 0), 2) as fill_rate
FROM paper_orders o
LEFT JOIN paper_trades t ON CAST(o.id AS TEXT) = t.source_order_id;

-- 持仓退出率（FILTER 语法需 SQLite 3.30+）
SELECT 
    COUNT(*) FILTER (WHERE status = 'open') as open_positions,
    COUNT(*) FILTER (WHERE status = 'closed') as closed_positions,
    ROUND(100.0 * COUNT(*) FILTER (WHERE status = 'closed') / NULLIF(COUNT(*), 0), 2) as close_rate
FROM strategy_trade_positions;
```

### 2.2 Hard Gate统计

```sql
SELECT 
    verdict_status,
    COUNT(*) as count,
    ROUND(100.0 * COUNT(*) / SUM(COUNT(*)) OVER (), 2) as percentage
FROM strategy_execution_audit_snapshots
GROUP BY verdict_status
ORDER BY count DESC;
```

## 3. 数据库配置

### 3.1 路径配置

**默认路径**: `data/db/akshare_mcp.sqlite3`

**环境变量**: `AKSHARE_MCP_SQLITE_PATH`

**代码读取**:
```python
from aiask_quant_core.config import get_settings
settings = get_settings()
db_path = settings.sqlite_path
```

### 3.2 连接管理

**位置**: `packages/strategy-factory/src/strategy_factory/infrastructure/persistence/sqlite/`

**CRUD**: `_strategy_crud_core.py`

## 相关文档

- [00-四工厂体系总览](00-四工厂体系总览.md)
- [05-生命周期与状态机](05-生命周期与状态机.md)
