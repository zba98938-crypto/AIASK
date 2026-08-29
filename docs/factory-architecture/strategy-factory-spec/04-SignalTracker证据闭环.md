# SignalTracker证据闭环


> **Hermes 集成副本勘误（2026-07-18）**  
> 本文是历史/上游阶段材料，不是当前 Hermes 树完成证明。  
> 在当前 `integrations/aiask/` 副本中：`packages/desktop-api` 不存在；Hermes Desktop 不是完整 AIASK workbench；live trading 默认关闭。  
> 权威状态：`AIASK_FINANCE_AGENT_COMPLETION_AUDIT.md`、`integrations/aiask/docs/CURRENT_PRODUCT_TRUTH.md`、`docs/GAP_INVENTORY_STATUS.md`。

## 文档信息

| 项目 | 内容 |
|---|---|
| 项目名称 | AIASK 策略工厂体系 |
| 文档类型 | 信号追踪与前向收益 |
| 文档版本 | 1.0.0 |
| 更新日期 | 2026-06-21 |
| 功能编号前缀 | `SF-SIGNAL` |
| 代码基准 | `packages/strategy-factory/src/strategy_factory/application/signal_tracker.py` |

### 术语定义

| 术语 | 定义 | 代码证据 |
|---|---|---|
| SignalTracker | 孵化闭环必需的sidecar，负责信号生成和前向收益追踪 | `signal_tracker.py` |
| Phase | SignalTracker的执行阶段，A-H覆盖不同策略状态 | Phase A-H定义 |
| 前向收益 | 信号产生后的未来N天收益 | `signal_forward_returns` 表 |
| Signal Evidence | 信号证据，修复trades without signal问题 | `strategy_signal_evidence` 表 |

## 1. 功能设计

### 1.1 需求背景与价值

| 项 | 内容 |
|---|---|
| 问题陈述 | 策略产生信号后，需要追踪信号质量和前向收益，为execution audit提供证据 |
| 解决方案 | 建立SignalTracker sidecar，定期扫描策略宇宙，生成信号和前向收益 |
| 业务价值 | 1. 提供信号→订单→成交的完整证据链<br>2. 计算前向收益用于孵化指标<br>3. 修复trades without signal问题 |

### 1.2 用户故事

| 编号 | 优先级 | 用户故事 | 成功标准 |
|---|---|---|---|
| SF-SIG-US-001 | P0 | 作为孵化工厂，我需要SignalTracker定期运行以产生信号 | SignalTracker每天至少运行1次 |
| SF-SIG-US-002 | P0 | 作为审计模块，我需要信号的前向收益数据 | signal_forward_returns表有数据 |
| SF-SIG-US-003 | P0 | 作为运维人员，我需要监控SignalTracker健康状态 | 健康检查显示最近运行时间和phase状态 |

### 1.3 功能分解与优先级

| 功能编号 | 功能点 | 优先级 | 当前代码依据 | 待开发事项 | 验收标准 |
|---|---|---|---|---|---|
| SF-SIG-F001 | 信号生成 | P0 | `signal_tracker.py` | 无 | strategy_signals表有记录 |
| SF-SIG-F002 | 前向收益计算 | P0 | `signal_forward_returns` | 无 | forward_returns表有记录 |
| SF-SIG-F003 | Signal Evidence记录 | P0 | `strategy_signal_evidence` | 无 | evidence表有记录 |
| SF-SIG-F004 | Phase A-H覆盖 | P0 | Phase定义 | 无 | 所有phase正常运行 |
| SF-SIG-F005 | Phase timeout控制 | P0 | 代码中已实现 | 无 | 长时间运行phase可超时 |
| SF-SIG-F006 | 健康状态监控 | P1 | 诊断脚本 | 需显式依赖检查 | 健康检查包含SignalTracker |

## 2. Phase定义

### 2.1 Phase覆盖

| Phase | 覆盖策略状态 | 主要任务 | 运行频率 |
|-------|-------------|----------|----------|
| A | submitted | 新提交策略首次信号生成 | 每次运行 |
| B | incubating (observe) | 观察阶段信号追踪 | 每次运行 |
| C | incubating (paper) | Paper交易信号追踪 | 每次运行 |
| D | candidate | 候选策略信号追踪 | 每次运行 |
| E | listed | 已上线策略信号追踪 | 每次运行 |
| F | diagnostic | 诊断策略信号追踪 | 每次运行 |
| G | warmup | 预热阶段信号生成 | 每次运行 |
| H | all active | 全量活跃策略补充扫描 | 每次运行 |

**实际运行记录** (2026-06-21):
- 最近运行: 1天前
- Phase覆盖: A-H (submitted/paper observation)
- Phase timeout: 已实现

## 3. 数据流向

```
策略宇宙 (submitted/incubating/listed)
    ↓
SignalTracker Phase Scan
    ↓
策略执行器 → 信号生成
    ↓
strategy_signals 表
    ↓
strategy_signal_evidence 表
    ↓
市场数据 (未来N天价格)
    ↓
前向收益计算
    ↓
signal_forward_returns 表
    ↓
strategy_incubation_metrics 表 (派生)
    ↓
Execution Audit → Hard Gate
```

## 4. 数据模型

### 4.1 信号表

| 字段 | 类型 | 说明 |
|------|------|------|
| signal_id | UUID | 信号ID |
| strategy_id | UUID | 策略ID |
| symbol | VARCHAR | 股票代码 |
| direction | ENUM | buy/sell/hold |
| timestamp | TIMESTAMP | 信号时间 |
| strength | FLOAT | 信号强度 |
| context | JSON | 信号上下文 |

**实际数据** (2026-06-21):
- 信号总数: 19,312

### 4.2 前向收益表

| 字段 | 类型 | 说明 |
|------|------|------|
| id | UUID | 记录ID |
| signal_id | UUID | 信号ID |
| horizon | INT | 前向天数 (5/10/20) |
| forward_return | FLOAT | 前向收益率 |
| benchmark_return | FLOAT | 基准收益率 |
| excess_return | FLOAT | 超额收益率 |

**实际数据** (2026-06-21):
- 前向收益记录: 39,449

### 4.3 Signal Evidence表

| 字段 | 类型 | 说明 |
|------|------|------|
| evidence_id | UUID | 证据ID |
| signal_id | UUID | 信号ID |
| order_id | UUID | 订单ID (可空) |
| trade_id | UUID | 成交ID (可空) |
| position_id | UUID | 持仓ID (可空) |
| lineage_type | ENUM | native/backfill |

**用途**: 修复"trades without signal evidence"问题

## 5. 证据链完整性

### 5.1 转换链

```
信号生成 (19,312)
    ↓ 99.99%
订单生成 (3,835)
    ↓ 98.15%
成交记录 (3,764)
    ↓ 100%
持仓记录 (3,660)
    ↓ 1.4%
持仓退出 (51)
```

### 5.2 证据完整性检查

| 检查项 | SQL查询 | 预期 | 实际 (2026-06-21) |
|--------|---------|------|-------------------|
| 信号→订单转换 | `SELECT COUNT(DISTINCT strategy_id) FROM strategy_signals WHERE EXISTS(...)` | >95% | 99.99% ✅ |
| Signal Evidence覆盖 | `SELECT COUNT(*) FROM strategy_signal_evidence` | >0 | - |
| 前向收益覆盖 | `SELECT COUNT(*) FROM signal_forward_returns` | >10,000 | 39,449 ✅ |

### 5.3 缺失证据诊断

```sql
-- 查找有信号但无订单的策略
SELECT DISTINCT s.strategy_id
FROM strategy_signals s
WHERE NOT EXISTS (
    SELECT 1 FROM paper_orders o
    WHERE o.strategy_id = s.strategy_id
);

-- 查找有订单但无信号证据的订单
SELECT o.order_id, o.strategy_id
FROM paper_orders o
WHERE NOT EXISTS (
    SELECT 1 FROM strategy_signal_evidence e
    WHERE e.order_id = o.order_id
);

-- 查找有成交但无前向收益的信号
SELECT s.signal_id
FROM strategy_signals s
WHERE NOT EXISTS (
    SELECT 1 FROM signal_forward_returns f
    WHERE f.signal_id = s.signal_id
);
```

## 6. 接口说明

### 6.1 触发SignalTracker运行

```bash
# 手动触发
python scripts/factories/run_signal_tracker.py

# Daemon模式
python scripts/factories/run_signal_tracker.py --daemon --interval 3600
```

### 6.2 查询信号状态

```
GET /v1/signal-tracker/status

Response:
{
    "last_run": "2026-06-20T10:00:00Z",
    "phase_status": {
        "A": "completed",
        "B": "completed",
        "C": "completed",
        "D": "completed",
        "E": "completed",
        "F": "completed",
        "G": "completed",
        "H": "completed"
    },
    "evidence_delta": {
        "signals": 150,
        "forward_returns": 450,
        "signal_evidence": 150
    }
}
```

### 6.3 查询策略信号

```
GET /v1/strategies/{strategy_id}/signals

Response:
{
    "strategy_id": "stg_001",
    "signals": [
        {
            "signal_id": "sig_001",
            "symbol": "600519",
            "direction": "buy",
            "timestamp": "2026-06-20T09:30:00Z",
            "strength": 0.8,
            "forward_returns": {
                "5d": 0.02,
                "10d": 0.05,
                "20d": 0.08
            }
        }
    ],
    "count": 10
}
```

## 7. 运维要求

### 7.1 运行频率

**推荐**: 每天运行1-2次

**最低要求**: 每天至少运行1次

**原因**:
- 信号时效性要求
- 前向收益需要及时更新
- Execution audit依赖最新证据

### 7.2 监控指标

| 指标 | 阈值 | 告警 |
|------|------|------|
| 最近运行时间 | <24小时 | 超过24小时告警 |
| Phase完成状态 | 所有phase完成 | 任何phase失败告警 |
| 证据增量 | >0 | 增量为0告警 |
| 运行时长 | <30分钟 | 超过1小时告警 |

### 7.3 健康检查

```bash
# 执行健康检查
python scripts/factories/diagnose_factory_health.py

# 输出包含SignalTracker状态
# [2] 检查SignalTracker最近运行...
# [OK] 最近运行: 1天前
# [OK] Phase A-H: 全部完成
```

## 8. 依赖关系

### 8.1 SignalTracker依赖

- 策略执行器: 执行策略逻辑生成信号
- 市场数据: 计算前向收益
- 数据库: 读取策略宇宙、写入信号和前向收益

### 8.2 被依赖方

- Incubation Factory: 依赖信号生成paper orders
- Execution Audit: 依赖前向收益评估策略质量
- 孵化指标: 依赖前向收益计算IC、Sharpe等指标

### 8.3 架构警告 (P2-3)

**问题**: SignalTracker是孵化必需sidecar，但不在四工厂supervisor内

**影响**:
- 隐式依赖，运维需记忆启动顺序
- 证据断链风险
- 无显式健康检查

**根治方向**:
- 四工厂健康报告必须显示SignalTracker状态
- Incubation启动前preflight检查SignalTracker
- 显示证据增量和phase覆盖

## 9. 验收标准

| 编号 | 验收项 | 验收方法 | 通过标准 |
|------|--------|----------|----------|
| TC-SIG-001 | SignalTracker运行 | 检查最近运行时间 | <24小时 |
| TC-SIG-002 | 信号生成 | 查询strategy_signals表 | 有新增记录 |
| TC-SIG-003 | 前向收益生成 | 查询signal_forward_returns表 | 有新增记录 |
| TC-SIG-004 | Signal Evidence生成 | 查询strategy_signal_evidence表 | 有新增记录 |
| TC-SIG-005 | Phase A-H覆盖 | 检查phase运行日志 | 所有phase完成 |
| TC-SIG-006 | 证据链完整 | SQL验证转换率 | 信号→订单>95% |

## 10. 已知问题

### 10.1 P2-2: SignalTracker未作为显式依赖

**严重性**: 📘 P2 - MEDIUM  
**状态**: 🎯 架构目标

**建议**:
1. 健康检查包含SignalTracker状态
2. 显示最近运行时间和phase状态
3. 显示证据增量

### 10.2 Signal-only Backlog

**现象**: 1/8078 (0.01%) 策略有信号但无订单

**可能原因**:
- 订单生成逻辑跳过该信号
- 价格数据缺失
- 账户状态异常

**检查方法**:
```sql
SELECT s.strategy_id, s.signal_id, s.direction, s.timestamp
FROM strategy_signals s
WHERE NOT EXISTS (
    SELECT 1 FROM paper_orders o
    WHERE o.strategy_id = s.strategy_id
    AND ABS(JULIANDAY(o.created_at) - JULIANDAY(s.timestamp)) < 1
);
```

## 11. 运行示例

### 11.1 手动运行

```bash
# 单次运行
python scripts/factories/run_signal_tracker.py

# 查看运行日志
tail -f logs/signal_tracker.log

# 查询信号增量
python scripts/factories/query_signal_delta.py
```

### 11.2 Daemon模式

```bash
# 启动daemon，每小时运行一次
python scripts/factories/run_signal_tracker.py --daemon --interval 3600

# 查看daemon状态
ps aux | grep signal_tracker

# 停止daemon
pkill -f signal_tracker
```

### 11.3 SQL查询示例

```sql
-- 查询最近生成的信号
SELECT *
FROM strategy_signals
ORDER BY timestamp DESC
LIMIT 10;

-- 查询前向收益统计
SELECT 
    horizon,
    COUNT(*) as count,
    AVG(forward_return) as avg_return,
    AVG(excess_return) as avg_excess
FROM signal_forward_returns
GROUP BY horizon;

-- 查询signal evidence覆盖率
SELECT 
    COUNT(DISTINCT s.signal_id) as total_signals,
    COUNT(DISTINCT e.signal_id) as signals_with_evidence,
    ROUND(100.0 * COUNT(DISTINCT e.signal_id) / COUNT(DISTINCT s.signal_id), 2) as coverage_rate
FROM strategy_signals s
LEFT JOIN strategy_signal_evidence e ON s.signal_id = e.signal_id;
```

## 相关文档

- [00-四工厂体系总览](00-四工厂体系总览.md)
- [03-孵化工厂](03-孵化工厂.md)
- [06-数据库Schema与证据表](06-数据库Schema与证据表.md)
- [07-运行与诊断](07-运行与诊断.md)
- [深度架构审查报告](../09-深度架构审查报告.md)
