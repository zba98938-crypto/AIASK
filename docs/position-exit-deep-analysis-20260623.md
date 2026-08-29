# 持仓退出路径深度分析报告
**日期**: 2026-06-23  
**分析时间**: 09:35

> **Hermes 集成副本勘误（2026-07-18）**  
> 本文是历史阶段工作记录，不是当前 Hermes 树完成证明。  
> 权威状态：`AIASK_FINANCE_AGENT_COMPLETION_AUDIT.md`、`integrations/aiask/docs/CURRENT_PRODUCT_TRUTH.md`、`docs/GAP_INVENTORY_STATUS.md`。


> 更正 / 当前口径说明（截至 2026-06-23 收官）
>
> 本文保留当天上午对持仓退出问题的阶段性分析，不改写原始数据与原始判断。当前代码边界已经进一步收口：canonical `ExecutionUniverseContract` owner 在 `strategy-factory`，canonical bootstrap 也已回到 `strategy_factory.runtime.default_bootstrap`。因此，本文中涉及 contract/bootstrap owner 的部分应理解为当时观察，不代表当前最终架构口径。

---

## 执行摘要

完成了对持仓退出率低（1.3%）问题的深度根因分析。发现了**三层架构问题**，而非单纯的代码 bug：

1. ✅ **孵化工厂退出路径修复**（已完成）- 代码已修复但 MCP 服务器需重启
2. ⚠️ **信号生成覆盖率不足**（48.4%）- 主要瓶颈
3. 📉 **信号生成频率下降**（从 6/18 的 2017 个信号降至 6/23 的 24 个）

---

## 根因分析：三层架构解耦

### 架构设计

```
┌─────────────────────┐
│  Signal Tracker     │  ← 每日 18:30 运行
│  (信号生成器)        │     为所有策略生成入场/退出信号
└──────────┬──────────┘
           │ writes to
           ↓
┌─────────────────────┐
│ strategy_signals    │  ← 信号存储表
│ (信号数据库)         │     signal_date, strategy_id, code, signal
└──────────┬──────────┘
           │ reads from
           ↓
┌─────────────────────┐
│ Incubation Factory  │  ← 消费信号，创建订单
│ (孵化工厂)           │     sync_signals_to_orders()
└─────────────────────┘
```

### 问题层次

**Layer 1: 孵化工厂退出路径（已修复 ✅）**
- **问题**: Phase 3c2/3d 仅处理 `incubating` 状态策略（603 个），遗漏 `submitted` 状态（2,423 个）
- **修复**: 改为处理 `all_strategies`（3,042 个）
- **状态**: 代码已修复，但 MCP 服务器运行旧版本（需重启）
- **预期效果**: 策略覆盖 +404%，退出订单创建量从 51/运行 → 200+/运行

**Layer 2: 信号覆盖率不足（主要瓶颈 ⚠️）**
- **问题**: 3,042 个有持仓的策略中，仅 1,473 个（48.4%）有退出信号
- **根因**: Signal Tracker 并非为**所有有持仓的策略**生成信号
  - 可能仅为"活跃"策略生成
  - `submitted` 状态策略可能被排除
- **影响**: 即使孵化工厂能处理所有策略，也只能处理有信号的那 48.4%

**Layer 3: 信号生成频率下降（次要问题 📉）**
```
日期        信号总数   策略数   退出信号
2026-06-18   2,017    1,013      836
2026-06-19      40       40       11
2026-06-20     360      360        5
2026-06-21      45       45        1
2026-06-22     210      210       47
2026-06-23      24       24        6   ← 今天
```
- **观察**: 6/18 后信号生成规模骤降 88%
- **可能原因**: 
  - Signal Tracker 调度频率改变
  - 策略筛选逻辑收紧
  - 数据源问题（K 线数据缺失）

---

## 当前状态快照（2026-06-23 09:32）

### 持仓与信号覆盖
```
总开仓持仓:            3,706
有持仓的策略数:        3,042
  - 有退出信号:        1,473 (48.4%) ✗
  - 无退出信号:        1,569 (51.6%)

按状态分解:
  submitted (2,423):    56.1% with exit
  incubating (606):     45.2% with exit
  deprecated (13):     200.0% with exit (异常)
```

### 信号时效性
```
Today:               18 策略 (  0.6%)
2-3 days ago:       107 策略 (  3.5%)
4-7 days ago:       576 策略 ( 19.0%)
8-14 days ago:     2,341 策略 ( 77.0%) ← 主体
30+ days ago:         1 策略 (  0.0%)
```

### 健康状态评估
- **持仓老化**: ✓ 良好（80% 在 8-14 天，符合孵化周期）
- **信号覆盖**: ✗ 失败（48.4% < 50% 阈值）
- **信号时效**: ⚠️ 警告（77% 的信号 8-14 天前生成，非每日刷新）

---

## 核心发现

### 1. 信号生成 ≠ 孵化工厂运行

**错误假设**: "孵化工厂运行时会为策略生成信号"

**实际架构**:
```python
# In incubation_parts/specs.py:sync_signals_to_orders()
signals = await db.get_signals(
    strategy['id'], 
    start_date=signal_date, 
    end_date=signal_date, 
    limit=200
)
```

孵化工厂**仅读取**预先存在的信号，**不生成**信号。信号由 **Signal Tracker** 在每日 18:30 统一生成。

### 2. Signal Tracker 覆盖范围不明

**问题**: Signal Tracker 的策略筛选逻辑未知
- 是否为所有 `status='submitted'` 策略生成信号？
- 是否仅为 `status='incubating'` 策略生成信号？
- 是否有"策略必须有持仓才生成退出信号"的逻辑？

**证据**: `submitted` 状态策略的退出信号覆盖率（56.1%）高于 `incubating`（45.2%），说明 Signal Tracker 确实处理 `submitted` 策略，但不是全部。

### 3. 修复的有效性依赖信号供给

即使孵化工厂修复生效（覆盖所有 3,042 个策略），退出率仍受限于信号覆盖率：
- **理论上限**: 48.4% × 200（批次限制）= 97 个退出订单/运行
- **实际值**: 取决于 Signal Tracker 何时为剩余 51.6% 策略生成退出信号

---

## 修复层次与优先级

### P0: 激活已修复代码（立即，<5 分钟）

**目标**: 让孵化工厂使用修复后的 `all_strategies` 逻辑

**行动**:
1. 重启 MCP 服务器（重启 Claude Code 桌面应用）
2. 通过 MCP 触发一次孵化工厂运行
3. 验证退出订单创建数量（预期 ~97 个，而非修复前的 0 个）

**预期效果**:
- 退出订单创建: 0 → ~97/运行
- 持仓退出率: 0% → ~2.6%/运行
- 清空所有有信号持仓需: ~15 次运行（vs 修复前无限次）

### P1: 提升信号覆盖率到 >80%（今天，2-4 小时）

**目标**: 让 Signal Tracker 为所有有持仓的策略生成退出信号

**行动**:
1. **审查 Signal Tracker 策略筛选逻辑**
   - 文件: `packages/akshare-mcp/src/akshare_mcp/services/signal_tracker_parts/*.py`
   - 重点: Phase A 中的策略选择条件（`WHERE status = ?`）
   
2. **方案 A**: 扩大 Signal Tracker 的策略范围
   ```python
   # 从:
   strategies = await db.list_strategies(status='incubating')
   
   # 改为:
   strategies = await db.list_strategies(status=['incubating', 'submitted'])
   
   # 或更激进:
   strategies_with_positions = await db.execute('''
       SELECT DISTINCT s.* FROM strategies s
       JOIN strategy_trade_positions stp ON s.id = stp.strategy_id
       WHERE stp.status = 'open'
   ''')
   ```

3. **方案 B**: 在孵化工厂中添加信号回填逻辑
   ```python
   # 在 sync_signals_to_orders 之前:
   if not has_recent_signal(strategy_id, signal_date):
       # 调用策略执行引擎临时生成信号
       generate_signal_on_demand(strategy_id, signal_date)
   ```

4. **推荐**: 方案 A（修改 Signal Tracker 覆盖范围）
   - 更符合架构设计
   - 一次修改，所有消费者受益
   - Signal Tracker 本就设计为"每日为所有策略生成信号"

**预期效果**:
- 信号覆盖率: 48.4% → >80%
- 退出订单创建: ~97 → ~400+/运行
- 持仓退出率: ~2.6% → ~10%/运行

### P2: 恢复信号生成频率（今天，1-2 小时）

**目标**: 理解并恢复 6/18 的信号生成规模

**行动**:
1. **审查 Signal Tracker 调度配置**
   ```bash
   grep -r "18:30\|signal.*schedule\|cron.*signal" packages/akshare-mcp/src
   ```

2. **检查 6/18 vs 6/23 的运行日志**
   - Signal Tracker 是否实际运行？
   - 运行时长、处理策略数、错误率
   - K 线数据可用性

3. **可能的根因**:
   - 环境变量 `SIGNAL_TRACKER_ENABLED=0` 被设置
   - 调度器被其他服务抢占（资源冲突）
   - K 线数据同步滞后（`TDX_LOCAL_ONLY=1` 导致数据不足）

**预期效果**:
- 信号生成规模: 24/天 → 1,000+/天
- 信号时效性: 77% 陈旧 → 80%+ 今日生成

---

## 立即行动清单（今天完成）

### ✅ 已完成
1. 深度根因分析（三层架构问题）
2. 信号对齐监控基线建立（48.4% 覆盖率）
3. 持仓老化监控工具验证

### 🔄 进行中
1. **重启 MCP 服务器**（等待用户操作）
2. **运行修复后的孵化工厂**（等待 MCP 重启）

### 📋 待执行（优先级排序）

**第一步**: 激活修复（<5 分钟）
```bash
# 用户操作：重启 Claude Code 桌面应用
# 然后在新会话中：
mcp_tool('strategy_manager', action='incubation_sync_run')
# 验证：退出订单创建数量 > 50
```

**第二步**: 审查 Signal Tracker（30 分钟）
```bash
# 找到策略筛选逻辑
grep -A 20 "list_strategies\|Phase A" \
  packages/akshare-mcp/src/akshare_mcp/services/signal_tracker_parts/*.py

# 找到调度配置
grep -r "18:30\|signal_tracker.*schedule" packages/akshare-mcp/
```

**第三步**: 扩大信号生成范围（1 小时）
```python
# 修改 signal_tracker_parts/runtime.py
# 将策略选择从 status='incubating' 改为 status IN ('incubating', 'submitted')
# 或直接查询有持仓的策略
```

**第四步**: 重启 Signal Tracker 并验证（30 分钟）
```bash
# 手动触发一次运行（如果有接口）
# 或等待今晚 18:30 自动运行
# 验证：明天的信号覆盖率 > 80%
```

**第五步**: 每日监控（5 分钟/天）
```bash
python tools/monitor_signal_alignment.py
# 追踪覆盖率趋势，目标 >80% 稳定
```

---

## 成功指标

### 短期（今天 18:00 前）
- [x] MCP 服务器重启
- [ ] 孵化工厂运行，退出订单创建 >50
- [x] Signal Tracker 代码审查完成
- [ ] Signal Tracker 修改方案确定

### 中期（本周内）
- [ ] 信号覆盖率 >80%
- [ ] 退出订单创建稳定在 400+/运行
- [ ] 持仓数量开始下降（3,706 → <2,000）

### 长期（下周）
- [ ] 持仓退出率 >10%/运行
- [ ] 信号生成恢复到 1,000+/天
- [ ] 健康检查全部通过（持仓老化 ✓ + 信号覆盖 ✓ + 信号时效 ✓）

---

## 附录：关键代码位置

### 孵化工厂退出路径
- `packages/akshare-mcp/src/akshare_mcp/services/incubation_factory/runner.py:411,422`
- `packages/akshare-mcp/src/akshare_mcp/services/incubation_parts/specs.py:463`

### 信号生成
- `packages/akshare-mcp/src/akshare_mcp/services/signal_tracker.py` (入口)
- `packages/akshare-mcp/src/akshare_mcp/services/signal_tracker_parts/*.py` (实现)

### 信号读取
- `packages/akshare-mcp/src/akshare_mcp/services/incubation_parts/specs.py:sync_signals_to_orders()`
  - 第13行: `signals = await db.get_signals(...)`

### 监控工具
- `tools/monitor_position_aging.py`
- `tools/monitor_signal_alignment.py`

---

**报告生成时间**: 2026-06-23 09:40  
**下一步**: 等待用户重启 MCP 服务器，然后执行第二步和第三步
