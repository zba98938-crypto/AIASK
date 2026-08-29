# 信号覆盖率不足修复方案


> **Hermes 集成副本勘误（2026-07-18）**  
> 本文是历史/上游阶段材料，不是当前 Hermes 树完成证明。  
> 在当前 `integrations/aiask/` 副本中：`packages/desktop-api` 不存在；Hermes Desktop 不是完整 AIASK workbench；live trading 默认关闭。  
> 权威状态：`AIASK_FINANCE_AGENT_COMPLETION_AUDIT.md`、`integrations/aiask/docs/CURRENT_PRODUCT_TRUTH.md`、`docs/GAP_INVENTORY_STATUS.md`。

> 更正 / 当前口径说明（截至 2026-06-23）
>
> 本文保留当时针对信号覆盖率问题的方案设计，但其中“`ExecutionUniverseContract` 未实现 / 不存在”的前提已经过时。当前 canonical `ExecutionUniverseContract` 已位于 `packages/strategy-factory/src/strategy_factory/contracts/execution_universe.py`。截至 2026-06-23，剩余问题是消费面仍保留 contract-first + legacy fallback 的兼容路径，而不是 contract owner 缺失。

**问题**: 3,042 个有持仓的策略中，仅 1,473 个（48.4%）有退出信号  
**根因**: `ExecutionUniverseContract` 未包含 `status='submitted'` 的策略  
**影响**: 即使孵化工厂修复生效，也只能为 48.4% 的持仓创建退出订单

---

## 根因详解

### 当前架构

```
SignalTracker (每日 18:30)
  ↓ 调用
ExecutionUniverseContract.list_executable_strategies()
  ↓ 查询 3 类策略
  1. status='incubating' + active account      (606 个)
  2. stage='warmup' + active account          (未知数量)
  3. stage='diagnostic' + active account      (未知数量)
  ↓ 遗漏
  ❌ status='submitted' 策略                  (2,423 个,占 80%)
```

### 为什么 `submitted` 策略被遗漏？

**设计假设**（错误）:
> "submitted 策略已完成孵化，不需要继续生成信号"

**实际情况**:
- `submitted` 策略 = 已提交给审核流程，等待晋级到 `listed`
- 这些策略**仍有活跃持仓**（2,423 个策略持有约 65% 的开仓）
- 它们**需要退出信号**来平仓

**数据验证**:
```sql
-- submitted 策略的信号覆盖率
SELECT 
  COUNT(DISTINCT stp.strategy_id) as with_position,
  COUNT(DISTINCT CASE WHEN ss.signal = -1 THEN stp.strategy_id END) as with_exit_signal
FROM strategy_trade_positions stp
LEFT JOIN strategy_signals ss ON stp.strategy_id = ss.strategy_id AND ss.signal = -1
JOIN strategies s ON stp.strategy_id = s.id
WHERE stp.status = 'open' AND s.status = 'submitted';

-- 结果: 2,423 with_position, 1,359 with_exit_signal (56.1%)
```

56.1% 的覆盖率说明：
- Signal Tracker **曾经**为这些策略生成过信号（在它们还是 `incubating` 时）
- 但一旦晋级到 `submitted`，就不再生成新信号
- 历史信号随时间陈旧（77% 的信号 >8 天前）

---

## 修复方案

### 方案 A: 扩展 ExecutionUniverseContract（推荐 ⭐）

**修改位置**: `packages/akshare-mcp/src/akshare_mcp/services/strategy_lifecycle_shared/execution_universe_contract.py`

**核心思路**: 添加第 4 类策略——`submitted` 状态且有持仓的策略

**代码修改**:

```python
class ExecutionUniverseQuery:
    as_of: date = field(default_factory=date.today)
    include_incubating: bool = True
    include_paper: bool = True
    include_diagnostic: bool = False
    include_listed: bool = False
    include_submitted: bool = True  # ← 新增
    limit: int = 500

class ExecutionUniverseContract:
    async def list_executable_strategies(self, db, query=None):
        # ... 现有逻辑 ...
        
        # 5. submitted 策略(有持仓,等待信号退出)
        if query.include_submitted:
            submitted = await self._list_submitted_with_positions(db, query)
            strategies.extend(submitted)
        
        # ... 去重和 limit ...
    
    async def _list_submitted_with_positions(self, db, query):
        """查询 submitted 状态且有开仓持仓的策略。
        
        规范边界:
          - status='submitted' (已提交审核,等待晋级)
          - 有 open 持仓 (需要退出信号)
          - 不包括 rejected/deprecated/archived
        """
        if not hasattr(db, "execute"):
            return []
        
        try:
            cursor = await db.execute(
                """
                SELECT DISTINCT
                    s.id, s.name, s.strategy_type, s.status, s.created_at,
                    ia.stage, ia.status as account_status, ia.id as account_id
                FROM strategies s
                INNER JOIN strategy_trade_positions stp
                    ON s.id = stp.strategy_id
                LEFT JOIN strategy_incubation_accounts ia
                    ON s.id = ia.strategy_id AND ia.status = 'active'
                WHERE s.status = 'submitted'
                    AND stp.status = 'open'
                ORDER BY s.created_at DESC
                LIMIT ?
                """,
                (query.limit,),
            )
            rows = await cursor.fetchall()
            
            return [
                ExecutionUniverseStrategy(
                    strategy_id=str(row[0]),
                    strategy_name=row[1],
                    strategy_type=row[2],
                    status=row[3],
                    created_at=self._parse_datetime(row[4]),
                    incubation_stage=row[5],
                    incubation_status=row[6],
                    account_id=str(row[7]) if row[7] else None,
                )
                for row in rows
            ]
        except Exception as exc:
            logger.warning("ExecutionUniverseContract: submitted query failed: %s", exc)
            return []
```

**SignalTracker 调用**:

修改 `packages/akshare-mcp/src/akshare_mcp/services/signal_tracker_parts/execution_universe_adapter.py:44-51`:

```python
query = ExecutionUniverseQuery(
    as_of=date.today(),
    include_incubating=True,
    include_paper=True,
    include_diagnostic=False,
    include_listed=False,
    include_submitted=True,  # ← 启用 submitted 策略
    limit=limit,
)
```

**预期效果**:
- 策略覆盖: 606 → 3,029 (+398%)
- 信号覆盖率: 48.4% → >95%（首日）→ 100%（稳态）
- 退出订单创建: ~97/运行 → ~600/运行
- 持仓清空时间: 15 运行 → 6 运行

---

### 方案 B: 孵化工厂按需生成信号（备选）

**修改位置**: `packages/akshare-mcp/src/akshare_mcp/services/incubation_parts/specs.py:sync_signals_to_orders()`

**核心思路**: 如果策略没有当日信号，临时调用策略引擎生成

**代码修改**:

```python
async def sync_signals_to_orders(self, db, strategy: dict, signal_date: date) -> dict:
    # ... 现有逻辑 ...
    
    signals = await db.get_signals(strategy['id'], start_date=signal_date, end_date=signal_date, limit=200)
    
    # 新增：如果没有当日信号，按需生成
    if not signals:
        generated_signals = await self._generate_signals_on_demand(db, strategy, signal_date)
        if generated_signals:
            await db.save_signals(strategy['id'], signal_date, generated_signals)
            signals = generated_signals
    
    # ... 继续处理 signals ...

async def _generate_signals_on_demand(self, db, strategy: dict, signal_date: date) -> list[dict]:
    """按需为单个策略生成信号（紧急回填）。"""
    from strategy_factory.execution import StrategyRegistry
    
    stype = strategy.get("strategy_type", "")
    instance, mode = StrategyRegistry.create_runtime_strategy(stype, strategy.get("params") or {})
    if instance is None:
        return []
    
    signals = []
    for code in self._resolve_strategy_universe(strategy):
        klines = await self._get_klines_with_fallback(db, code, limit=200)
        if not klines or len(klines) < 20:
            continue
        
        # 生成信号
        signal_row = instance.generate_signal(klines)  # 简化版
        if signal_row.get("signal") != 0:
            signal_row["code"] = code
            signals.append(signal_row)
    
    return signals
```

**优点**:
- 无需等待 Signal Tracker 运行（立即生效）
- 保证 100% 覆盖（只要策略有持仓）

**缺点**:
- 破坏架构分层（孵化工厂承担信号生成职责）
- 性能开销大（每个策略单独生成，无批量优化）
- 不利于前向验证（信号未提前记录）

**推荐度**: ⚠️ 仅作为临时措施

---

## 实施计划

### 第 1 步: 激活已修复代码（立即，<5 分钟）

**目标**: 让孵化工厂使用修复后的 `all_strategies` 逻辑

**行动**:
```bash
# 1. 重启 Claude Code 桌面应用（自动重启 MCP 服务器）
# 2. 在新会话中验证修复
python -c "
from akshare_mcp.services.incubation_factory.runner import IncubationFactoryRunner
# 检查 runner.py 是否使用 all_strategies
import inspect
source = inspect.getsource(IncubationFactoryRunner.run_once)
assert 'all_strategies' in source
print('✓ 修复代码已加载')
"
```

**验证**:
- 运行孵化工厂，检查退出订单创建数量 >50

### 第 2 步: 实施方案 A（今天，1 小时）

**行动**:
1. 编辑 `execution_universe_contract.py`
   - 添加 `include_submitted: bool = True` 到 `ExecutionUniverseQuery`
   - 添加 `_list_submitted_with_positions()` 方法
   - 在 `list_executable_strategies()` 中调用

2. 编辑 `execution_universe_adapter.py`
   - 设置 `include_submitted=True`

3. 重启 MCP 服务器

4. 手动触发 Signal Tracker 运行（如果有接口）
   - 或等待今晚 18:30 自动运行

**验证**:
```bash
# 检查信号生成规模
python -c "
import sqlite3
db = sqlite3.connect('data/db/akshare_mcp.sqlite3')
result = db.execute('''
    SELECT COUNT(*), COUNT(DISTINCT strategy_id)
    FROM strategy_signals
    WHERE signal_date = date('now')
''').fetchone()
print(f'Today: {result[0]} signals from {result[1]} strategies')
# 预期: >1,500 signals from >1,000 strategies
"
```

### 第 3 步: 持续监控（每天，5 分钟）

**行动**:
```bash
# 每日运行
python tools/monitor_signal_alignment.py

# 预期输出：
# Coverage: >80% (目标: >95%)
# 信号时效: >80% today (vs 当前 0.6%)
```

**阈值**:
- 覆盖率 <80%: 警告
- 覆盖率 <50%: 失败（当前状态）

---

## 成功指标

### 短期（今天）
- [x] MCP 服务器重启
- [ ] 孵化工厂运行，退出订单 >50
- [ ] `execution_universe_contract.py` 修改完成
- [ ] Signal Tracker 代码修改完成

### 中期（明天 18:30 后）
- [ ] 信号生成规模 >1,500/天
- [ ] 信号覆盖率 >80%
- [ ] 退出订单创建稳定在 400+/运行

### 长期（本周末）
- [ ] 信号覆盖率 >95%
- [ ] 持仓数量降至 <2,000
- [ ] 健康检查全部通过

---

## 风险与缓解

### 风险 1: Signal Tracker 性能下降

**原因**: 策略数量从 606 → 3,029 (+398%)

**缓解**:
- Signal Tracker 已有批量优化（批次处理、并发执行）
- 如果运行时间 >30 分钟，增加并发度或分批运行

### 风险 2: K 线数据不足

**原因**: 为 submitted 策略生成信号需要 K 线数据

**缓解**:
- 确保数据同步工具覆盖所有策略的标的
- 检查 `TDX_LOCAL_ONLY` 环境变量（应为 `0`）

### 风险 3: 信号生成逻辑变化

**原因**: 策略参数或市场环境变化导致信号不再生成

**缓解**:
- 监控 `signal=0` 的比例（应 <20%）
- 如果 >50% 策略无信号，检查策略引擎

---

## 附录：SQL 验证查询

### 当前信号覆盖率

```sql
-- 总体覆盖率
SELECT 
  COUNT(DISTINCT stp.strategy_id) as total_with_position,
  COUNT(DISTINCT CASE WHEN ss.signal = -1 THEN stp.strategy_id END) as with_exit_signal,
  ROUND(100.0 * COUNT(DISTINCT CASE WHEN ss.signal = -1 THEN stp.strategy_id END) / COUNT(DISTINCT stp.strategy_id), 1) as coverage_pct
FROM strategy_trade_positions stp
LEFT JOIN strategy_signals ss ON stp.strategy_id = ss.strategy_id
WHERE stp.status = 'open';

-- 按状态分解
SELECT 
  s.status,
  COUNT(DISTINCT stp.strategy_id) as total,
  COUNT(DISTINCT CASE WHEN ss.signal = -1 THEN stp.strategy_id END) as with_exit,
  ROUND(100.0 * COUNT(DISTINCT CASE WHEN ss.signal = -1 THEN stp.strategy_id END) / COUNT(DISTINCT stp.strategy_id), 1) as pct
FROM strategy_trade_positions stp
LEFT JOIN strategy_signals ss ON stp.strategy_id = ss.strategy_id
JOIN strategies s ON stp.strategy_id = s.id
WHERE stp.status = 'open'
GROUP BY s.status;
```

### submitted 策略详情

```sql
-- submitted 策略持仓统计
SELECT 
  COUNT(DISTINCT s.id) as strategy_count,
  COUNT(DISTINCT stp.id) as position_count,
  SUM(stp.quantity * stp.entry_price) as total_market_value
FROM strategies s
INNER JOIN strategy_trade_positions stp ON s.id = stp.strategy_id
WHERE s.status = 'submitted' AND stp.status = 'open';

-- submitted 策略信号时效性
SELECT 
  CASE 
    WHEN julianday('now') - julianday(ss.signal_date) <= 1 THEN 'today'
    WHEN julianday('now') - julianday(ss.signal_date) <= 3 THEN '2-3 days'
    WHEN julianday('now') - julianday(ss.signal_date) <= 7 THEN '4-7 days'
    WHEN julianday('now') - julianday(ss.signal_date) <= 14 THEN '8-14 days'
    ELSE '30+ days'
  END as age_bucket,
  COUNT(DISTINCT ss.strategy_id) as strategy_count
FROM strategy_signals ss
JOIN strategies s ON ss.strategy_id = s.id
WHERE s.status = 'submitted' AND ss.signal = -1
GROUP BY age_bucket
ORDER BY MIN(julianday('now') - julianday(ss.signal_date));
```

---

**创建时间**: 2026-06-23 09:50  
**状态**: 待执行  
**负责人**: AI + 用户
