# 🎯 Phase 3e 失败真相 - 2026-06-22 19:15


> **Hermes 集成副本勘误（2026-07-18）**  
> 本文是历史/上游阶段材料，不是当前 Hermes 树完成证明。  
> 在当前 `integrations/aiask/` 副本中：`packages/desktop-api` 不存在；Hermes Desktop 不是完整 AIASK workbench；live trading 默认关闭。  
> 权威状态：`AIASK_FINANCE_AGENT_COMPLETION_AUDIT.md`、`integrations/aiask/docs/CURRENT_PRODUCT_TRUTH.md`、`docs/GAP_INVENTORY_STATUS.md`。

---

## ✅ **Phase 3e 并没有失败！**

### 数据对比
```
paper_orders (有 signal_id): 3,611
strategy_signal_evidence:    3,612

重叠率: ~100%
```

**结论**: Phase 3e 在之前的某次运行中**已经成功**回填了所有 evidence！

---

## 🔍 **那为什么还是 saved_signal_evidence = 0？**

### Backfill 逻辑
```python
# signal_evidence.py:75-79
existing_signal_ids = {
    _string(row.get("signal_id"))
    for row in list(existing_rows or [])
    if _string(row.get("signal_id"))
}

# signal_evidence.py:182
if signal_id in existing_signal_ids:
    skipped_existing_signal_count += 1
    continue  # 所有 signal 都跳过了！
```

**Phase 3e 的职责**: 回填**缺失**的 signal evidence

**当前状态**: 没有缺失的 evidence（都已存在）

**结果**: saved = 0（正确行为！）

---

## 🎯 **那为什么 hard_gate_passed = 0？**

### Phase 3f (Execution audit acceptance) 的真正作用

Phase 3f 不是简单地检查 "是否有 evidence"，而是检查 **"evidence 是否通过质量门槛"**

```python
# Phase 3f 检查项：
1. Evidence 存在 ✅ (已有 3,612 条)
2. Evidence 质量合格 ❌ (hard_gate_passed = 0)
   - compile_stable_ready
   - 语义契约完整性
   - 置信度阈值
   - 其他质量指标
```

**结论**: Evidence 存在但质量不达标！

---

## 📊 **实际瓶颈**

### 不是 Phase 3e（evidence 回填）

Phase 3e 已经完成，3,612 条 evidence 都已关联。

### 而是 Phase 3f（质量门槛）

**Phase 3f 检查结果**:
```
evaluated: 80 个策略
hard_gate_passed: 0 ❌
available_signal_evidence: 22
```

**问题**: 
- 80 个策略被评估
- 有 22 条可用的 signal evidence
- 但 0 个通过 hard_gate

---

## 🔍 **为什么无法通过 hard_gate？**

### 可能原因 1: compile_stable 不就绪

```python
if not semantic_status.get("compile_stable_ready"):
    # 策略语义契约不完整
    # 无法通过 gate
```

**检查方法**:
```sql
SELECT 
    COUNT(*) as total,
    SUM(CASE WHEN json_extract(params, '$.compiled_dsl') IS NOT NULL THEN 1 ELSE 0 END) as has_dsl
FROM strategies
WHERE incubating = 'observe_incubation';
```

---

### 可能原因 2: Evidence proxy_only = true

```python
# signal_evidence.py:327
"proxy_only": True,  # 代理证据，非原生生成
```

**如果所有 evidence 都是 proxy_only**:
- 不算作高质量证据
- 无法通过 hard_gate

**检查方法**:
```sql
SELECT 
    COUNT(*) as total,
    SUM(CASE WHEN json_extract(payload, '$.proxy_only') = 'true' THEN 1 ELSE 0 END) as proxy_only
FROM strategy_signal_evidence;
```

---

### 可能原因 3: 置信度缺失

```python
"raw_confidence": None,
"calibrated_confidence": None,
```

**如果置信度为空**:
- 无法评估信号质量
- 无法通过 gate

---

## 🎯 **修正后的根因分析**

### Phase 3e (Evidence Backfill) ✅

**状态**: 成功  
**证据**: 3,612 条 evidence 已存在  
**结论**: 不是瓶颈

### Phase 3f (Quality Gate) ❌

**状态**: 失败  
**证据**: hard_gate_passed = 0  
**原因**: Evidence 质量不达标
  - 可能是 proxy_only = true
  - 可能是语义契约不完整
  - 可能是置信度缺失

---

## 🔧 **下一步诊断**

### 1. 检查 strategy_signal_evidence 的质量

```python
# 查看 evidence 的 payload
SELECT 
    signal_id,
    json_extract(payload, '$.proxy_only') as proxy_only,
    json_extract(payload, '$.build_mode') as build_mode,
    json_extract(payload, '$.semantic_contract_status') as contract_status
FROM strategy_signal_evidence
LIMIT 10;
```

### 2. 检查策略的语义契约状态

```python
SELECT
    strategy_id,
    json_extract(params, '$.compiled_dsl') as has_dsl,
    json_extract(params, '$.evidence_chain') as has_evidence_chain
FROM strategies
WHERE incubating = 'observe_incubation'
LIMIT 10;
```

### 3. 理解 hard_gate 的具体条件

查看代码中 `_fallback_execution_audit_gate` 的实现

---

## 💡 **可能的修复方案**

### 方案 A: 提升 Evidence 质量

如果问题是 proxy_only:
- 生成真实的原生 evidence
- 而不是回填的代理 evidence

### 方案 B: 修复语义契约

如果问题是 compile_stable 不就绪:
- 补全策略的语义契约字段
- 确保 compiled_dsl 存在

### 方案 C: 降低 hard_gate 阈值（临时）

如果 gate 条件过严:
- 临时放宽条件
- 让一些策略先转正
- 后续再提升质量

---

## 📈 **乐观的理由**

### 1. 数据完整 ✅
- Orders 存在且有 signal_id
- Evidence 已经关联好
- 不需要重新回填

### 2. 只差质量提升 ✅
- 不是架构问题
- 不是数据缺失
- 只需要提升现有 evidence 的质量

### 3. 修复路径清晰 ✅
- 定位到具体的质量字段
- 批量修复或生成
- 重新评估即可转正

---

## 🎯 **今晚的收获**

### 错误的假设
❌ Phase 3e 失败了  
❌ Evidence 数据缺失  
❌ Signal-Trade 关联断裂

### 正确的理解
✅ Phase 3e 已经成功  
✅ Evidence 数据完整  
✅ 关键在质量门槛

### 下一步
1. 检查 evidence 的 proxy_only 字段
2. 检查策略的 compile_stable 状态
3. 理解 hard_gate 的具体条件
4. 针对性修复

---

**报告完成**: 2026-06-22 19:20  
**真正瓶颈**: Phase 3f (Quality Gate)  
**修复方向**: 提升 Evidence 质量或补全语义契约
