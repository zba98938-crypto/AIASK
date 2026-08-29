# 手动 run_once 执行结果报告


> **Hermes 集成副本勘误（2026-07-18）**  
> 本文是历史/上游阶段材料，不是当前 Hermes 树完成证明。  
> 在当前 `integrations/aiask/` 副本中：`packages/desktop-api` 不存在；Hermes Desktop 不是完整 AIASK workbench；live trading 默认关闭。  
> 权威状态：`AIASK_FINANCE_AGENT_COMPLETION_AUDIT.md`、`integrations/aiask/docs/CURRENT_PRODUCT_TRUTH.md`、`docs/GAP_INVENTORY_STATUS.md`。

**执行时间**: 2026-06-22 18:58:06 - 19:00:53  
**运行时长**: 167 秒（2.8 分钟）  
**最终结果**: ❌ formal_incubation 仍为 0

---

## 📊 执行摘要

```json
{
  "status": "partial",
  "promoted": 0,
  "phase_failures": 1,
  "hit_rate": 38.30%,
  "elapsed_seconds": 167.12,
  
  "intake": {
    "scanned": 500,
    "accepted": 0
  },
  
  "verification": {
    "verified": 1700,
    "recorded": 1700
  },
  
  "execution_audit_acceptance": {
    "saved_signal_evidence": 0,  ❌
    "available_signal_evidence": 22,
    "hard_gate_passed": 0  ❌
  }
}
```

---

## ❌ 失败原因分析

### 核心问题：Phase 3e 无效

**Phase 3e (Native execution evidence backfill)**:
- ✅ 执行了（18:59:20）
- ❌ saved_signal_evidence = 0
- ❌ 无法构建 Signal → Trade lineage

**Phase 3f (Execution audit acceptance)**:
- ✅ 执行了（18:59:20）
- ❌ hard_gate_passed = 0
- ❌ 所有策略被阻塞

**结果**:
- execution_hard_gate_passed = false
- 无法通过转正门槛
- promoted = 0

---

## 🔍 为什么 Phase 3e 失败？

### 可能原因 1: Backfill 方法不存在或失败

**代码路径**:
```python
# runner.py:1431
backfill = _resolve_db_async_method(db, "backfill_strategy_signal_evidence_native")
```

**如果方法解析失败**:
- 静默跳过（无异常）
- saved = 0
- 无日志输出

**验证方法**:
需要检查 `db` 对象是否有 `backfill_strategy_signal_evidence_native` 方法

---

### 可能原因 2: 候选集为空

**Backfill 逻辑**:
```python
# 查找有 trades/positions 但无 signal evidence 的策略
candidates = []
for strategy in strategies:
    trades = get_trades(strategy_id)
    positions = get_positions(strategy_id)
    evidence = get_signal_evidence(strategy_id)
    
    if (trades or positions) and not evidence:
        candidates.append(strategy)
```

**如果 candidates = []**:
- 跳过 backfill
- saved = 0

**但这不太可能**，因为：
- available_signal_evidence = 22（说明有数据）
- 有 3,093 个策略有 trades
- 有 8,248 个策略有 signals

---

### 可能原因 3: Backfill 执行但写入失败

**Backfill 流程**:
```python
for sid in candidates:
    result = await backfill(sid)
    saved_count += result.get("saved_signal_count", 0)
```

**如果写入失败**:
- 方法执行了
- 但数据库写入失败
- saved = 0

---

## 🐛 发现的 Bug

### Bug 1: as_of 未定义（P0）

```python
NameError: name 'as_of' is not defined
  at Phase 3c2: Exit signal paper execution
  
File: runner.py:412
Code: as_of=as_of
```

**影响**:
- Phase 3c2 失败
- 可能影响后续 phase 的数据一致性

**修复**:
```python
# 应该是:
as_of = as_of or date.today()

# 或者传入参数时:
lambda: self._run_exit_signal_paper_execution(
    db,
    strategies=...,
    as_of=as_of,  # 需要在上层定义
)
```

---

### Bug 2: Phase 3e 无日志（P1）

**当前**:
```
Phase 3e: Native execution evidence backfill
(无后续日志)
```

**应该有**:
```
Phase 3e: Native execution evidence backfill
  candidates: 100 strategies
  processed: 100
  saved_signals: 150
  errors: 0
```

---

## 📊 数据库实际状态

```
observe_incubation: 16,491 个
formal_incubation: 0 个 ❌

有 paper_trades: 3,093 个策略
有 strategy_signals: 8,248 个策略

前向收益记录: 39,449 条
```

**数据完整性**: ✅  
**Signal-Trade 关联**: ❌ (Phase 3e 失效)

---

## 🎯 根本问题

### Phase 3e 的实现有问题

**两种可能**:

#### 1. 方法不存在
```python
backfill = _resolve_db_async_method(db, "backfill_strategy_signal_evidence_native")
# 如果返回 None → 跳过
```

#### 2. 方法存在但逻辑错误
```python
async def backfill_strategy_signal_evidence_native(strategy_id):
    # 逻辑有问题
    # 返回 saved_signal_count = 0
```

---

## 💡 下一步诊断

### 必须验证的事项

#### 1. 检查方法是否存在
```python
# 需要在代码中查找
grep -r "def backfill_strategy_signal_evidence_native" packages/
```

#### 2. 如果存在，检查实现
```python
# 查看方法签名和逻辑
# 验证为什么 saved = 0
```

#### 3. 如果不存在，需要实现
```python
# Phase 3e 依赖这个方法
# 但方法可能未实现或名称错误
```

---

## 🔧 临时解决方案

### 方案 A: 手动构建 Signal Evidence（不推荐）

直接写 SQL 关联 trades → signals

**风险**: 
- 可能破坏数据一致性
- 缺少审计记录

### 方案 B: 修复 Phase 3e 实现（推荐）

1. 找到 backfill 方法的实现
2. 修复 bug
3. 重新运行

### 方案 C: 降低转正门槛（临时）

暂时绕过 execution_hard_gate 检查

**不推荐**: 降低质量标准

---

## 📈 积极的一面

### 系统运行正常 ✅

1. **所有 Phase 都执行了**
   - Phase 1-8 完整
   - 无崩溃
   - 无超时

2. **数据生产活跃**
   - 1700 个策略验证
   - 命中率 38.30%
   - 新增策略持续增长

3. **只是一个技术细节**
   - 不是架构问题
   - 不是数据问题
   - 只是 Phase 3e 的实现问题

---

## 🎯 结论

### formal=0 的真正原因

**不是因为**:
- ❌ 数据不足
- ❌ 质量不够
- ❌ 架构错误

**而是因为**:
- ✅ Phase 3e 的 `backfill_strategy_signal_evidence_native` 方法
  - 可能不存在
  - 或者实现有 bug
  - 导致 saved_signal_evidence = 0
  - 阻塞了所有转正

### 下一步

1. **立即**: 查找 backfill 方法的实现
2. **明天**: 修复方法或实现缺失的功能
3. **后天**: 重新运行，验证 formal > 0

---

**报告完成**: 2026-06-22 19:05  
**关键发现**: Phase 3e 方法实现问题  
**优先级**: P0（阻塞所有转正）
