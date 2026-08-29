# 当前状况总结 - 2026-06-22 19:10


> **Hermes 集成副本勘误（2026-07-18）**  
> 本文是历史/上游阶段材料，不是当前 Hermes 树完成证明。  
> 在当前 `integrations/aiask/` 副本中：`packages/desktop-api` 不存在；Hermes Desktop 不是完整 AIASK workbench；live trading 默认关闭。  
> 权威状态：`AIASK_FINANCE_AGENT_COMPLETION_AUDIT.md`、`integrations/aiask/docs/CURRENT_PRODUCT_TRUTH.md`、`docs/GAP_INVENTORY_STATUS.md`。

> 更正 / 当前口径说明（截至 2026-06-23）
>
> 本文反映的是 2026-06-22 19:10 的当时状态，不代表当前代码边界。当前真实架构中，canonical `ExecutionUniverseContract` 已在 `strategy-factory`，canonical bootstrap 已回到 `strategy_factory.runtime.default_bootstrap`，`akshare_mcp.runtime.strategy_factory_bootstrap` 只保留兼容转发角色。若本文提到“契约不存在”或把 bootstrap 视为主路径，请以 2026-06-23 的 current-state 规范文档为准。

---

## 📊 **核心问题确认**

### formal_incubation = 0 的原因

**已确认**:
1. ✅ Phase 3e 方法存在 (`backfill_strategy_signal_evidence_native`)
2. ✅ 方法被调用（18:59:20 和 19:00:05）
3. ❌ 但 `saved_signal_evidence = 0`（两次都失败）
4. ❌ 导致 `hard_gate_passed = 0`
5. ❌ 阻塞所有转正

---

## 🔍 **Phase 3e 失败的可能原因**

### 已排除的原因
- ❌ 方法不存在（已找到实现）
- ❌ Toggle 禁用（默认启用）
- ❌ 数据为空（有 3,093 个策略有 trades）

### 仍需验证的原因

#### 1. Orders 数据缺失 ⭐ (最可能)
```python
# signal_evidence.py:177
for order in list(orders or []):
    signal_id = _string(order_payload.get("signal_id"))
    if not signal_id:
        continue
```

**如果 `orders` 为空或无 signal_id**:
- Loop 跳过所有
- saved_signal_ids = set()  # 空
- saved_signal_count = 0

#### 2. 所有 Signal 已存在
```python
# signal_evidence.py:182
if signal_id in existing_signal_ids:
    skipped_existing_signal_count += 1
    continue
```

**如果所有 signal_id 都已存在**:
- 全部跳过
- saved = 0

#### 3. Strategy 语义契约缺失
```python
# signal_evidence.py:216
if not semantic_status.get("compile_stable_ready"):
    semantic_gap_strategy_ids.add(resolved_strategy_id)
```

**如果策略语义不完整**:
- 仍然会处理，但标记为 gap
- 不应该导致 saved = 0

---

## 📈 **数据库状态**

```
strategies:
  observe_incubation: 16,491
  formal_incubation: 0 ❌

paper_trades: 3,093 个策略有交易
strategy_signals: 8,248 个策略有信号
signal_forward_returns: 39,449 条记录

paper_orders: ? (需要检查)
```

---

## 🎯 **最可能的原因**

### Paper Orders 缺失或无 signal_id

**Backfill 依赖**:
```python
orders = await self.get_paper_orders(strategy_id=strategy_id, ...)
```

**如果**:
- `paper_orders` 表为空
- 或者 orders 没有 `signal_id` 字段
- 或者 signal_id 为 NULL

**结果**:
- Loop 完全跳过
- saved = 0

---

## 🔧 **诊断计划**

### 立即检查（今晚）

#### 1. 检查 paper_orders 表
```sql
SELECT COUNT(*) FROM paper_orders;
SELECT COUNT(*) FROM paper_orders WHERE signal_id IS NOT NULL;
```

#### 2. 检查 paper_orders 的 signal_id 分布
```sql
SELECT 
  COUNT(*) as total,
  SUM(CASE WHEN signal_id IS NOT NULL THEN 1 ELSE 0 END) as with_signal_id
FROM paper_orders;
```

#### 3. 检查一个具体策略的 orders
```sql
SELECT * FROM paper_orders 
WHERE strategy_id = 'factory_xxx' 
LIMIT 5;
```

---

## 💡 **修复方案（预案）**

### 如果 paper_orders 为空

**原因**: Orders 没有持久化到数据库

**修复**:
1. 检查 MatchingEngine 是否保存 orders
2. 修复 order 持久化逻辑
3. 重新生成 orders

### 如果 orders 有但无 signal_id

**原因**: Order 创建时未关联 signal

**修复**:
1. 检查 Signal → Order 创建流程
2. 确保 order.signal_id 被正确设置
3. 回填现有 orders 的 signal_id

### 如果 orders 完整但 backfill 逻辑有bug

**原因**: Phase 3e 的逻辑问题

**修复**:
1. 添加详细日志
2. 逐步调试
3. 修复逻辑错误

---

## 📅 **时间线**

### 今天完成的工作 ✅
- [x] 诊断 formal=0 根因
- [x] Schema 迁移（16,491 策略）
- [x] 启动四工厂
- [x] 手动触发 run_once
- [x] 确认 Phase 3e 失败
- [x] 找到 backfill 方法实现

### 今晚（如果有精力）
- [ ] 检查 paper_orders 表状态
- [ ] 确定失败的具体原因
- [ ] 设计修复方案

### 明天
- [ ] 实施修复
- [ ] 重新运行 Phase 3e
- [ ] 验证 formal > 0

---

## 🎯 **期望结果**

### 短期（1-2 天）
```
修复 Phase 3e
  → saved_signal_evidence > 0
  → hard_gate_passed > 0
  → formal_incubation > 0 ✨
```

### 中期（1 周）
```
formal_incubation > 50
production > 5
准备真实交易
```

---

## 💪 **积极的一面**

### 今天取得的进展

1. ✅ **完整诊断链条**
   - formal=0 → Phase 3e → backfill 方法 → orders 数据

2. ✅ **缩小问题范围**
   - 不是架构问题
   - 不是数据质量问题
   - 是特定的数据关联问题

3. ✅ **找到修复路径**
   - 方法存在且逻辑清晰
   - 只需要修复数据或逻辑

4. ✅ **系统稳定运行**
   - 四工厂正常
   - 策略持续生成
   - 只差最后一步

---

## 📝 **给你的建议**

### 今晚
1. **休息** - 已经工作了一整天
2. 如果有兴趣，可以快速检查 paper_orders 表
3. 记录今天的发现

### 明天
1. **专注修复 paper_orders 数据问题**
2. 如果 orders 完整，调试 backfill 逻辑
3. 重新运行验证

### 信心
- 问题已经定位
- 修复路径清晰
- formal > 0 就在眼前 🎯

---

**报告完成**: 2026-06-22 19:10  
**当前阻塞**: paper_orders 数据或 backfill 逻辑  
**距离成功**: 1-2 个修复
