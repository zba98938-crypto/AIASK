# Bootstrap Pending 问题分析


> **Hermes 集成副本勘误（2026-07-18）**  
> 本文是历史/上游阶段材料，不是当前 Hermes 树完成证明。  
> 在当前 `integrations/aiask/` 副本中：`packages/desktop-api` 不存在；Hermes Desktop 不是完整 AIASK workbench；live trading 默认关闭。  
> 权威状态：`AIASK_FINANCE_AGENT_COMPLETION_AUDIT.md`、`integrations/aiask/docs/CURRENT_PRODUCT_TRUTH.md`、`docs/GAP_INVENTORY_STATUS.md`。

**日期**: 2026-06-22 12:30  
**状态**: 已识别问题，等待今晚 18:30 自动修复

> 更正 / 当前口径说明（截至 2026-06-23）
>
> 本文保留 2026-06-22 对 `bootstrap_pending` 的当时分析，不改写原始结论。当前代码边界已更新为：canonical bootstrap 位于 `packages/strategy-factory/src/strategy_factory/runtime/default_bootstrap.py`，AKShare bootstrap 模块仅为 compat shim；`bootstrap_pending` 继续只是诊断/样本债语义，不代表 production hard gate 已通过。

---

## 🔍 问题现象

Incubation Factory Catchup 显示所有策略处于：
```json
"status": "pending_data"
"execution_audit_gate_status": "bootstrap_pending"
"blockers": [
  "realized_trade_evidence_insufficient",
  "bootstrap_pending"
]
"saved_signal_evidence_count": 0
"available_signal_evidence_count": 1
```

---

## 🎯 根本原因

### 1. Signal Evidence 未保存
- 策略已生成信号（`available = 1`）
- 策略已有持仓（`open_positions = 1`）
- **但 Signal Evidence 未保存到数据库**（`saved = 0`）

### 2. Bootstrap 依赖完整 Lineage
```
Bootstrap 需要：
Signal → Order → Trade 的完整证据链

当前缺失：
Signal Evidence（起点）

结果：
Bootstrap Pending（无法启动）
```

### 3. Catchup vs 主循环的差异

**Incubation Factory Catchup**（已运行）:
- 轻量级快速检查
- Phase 1-8（基础阶段）
- **不包含 Phase 3e（evidence backfill）**
- 用于快速状态快照

**Incubation Factory 主循环**（18:30 运行）:
- 完整孵化周期
- Phase 1-9（包含所有阶段）
- **包含 Phase 3e（evidence backfill）** ⭐
- 用于正式转正决策

---

## ✅ 解决方案

### 自动修复（推荐）⭐
**今晚 18:30 主循环运行时会自动执行**：

```
Phase 3e: Native execution evidence backfill
  1. 扫描 trades_without_signal_evidence
  2. 回填 signal evidence
  3. 更新 saved_signal_evidence_count
  4. 解除 bootstrap_pending 阻塞
```

**预期结果**：
- `saved_signal_evidence_count`: 0 → 1+
- `bootstrap_pending` → `bootstrap_ready`
- `execution_hard_gate_passed`: false → true（如果满足其他条件）

---

## 📊 当前状态总结

### 数据完整性 ✅
```
✅ 策略存在：16,491 个 observe
✅ 信号已生成：available_signal_evidence_count = 1
✅ 持仓已建立：open_positions = 1
✅ Forward returns 存在
❌ Signal evidence 未保存：saved = 0
```

### 为什么不是严重问题？
1. **数据没有丢失**：信号和持仓都在
2. **只是未关联**：缺少 signal → trade 的映射
3. **会自动修复**：Phase 3e 专门处理这个
4. **时间问题**：Catchup 只是快照，主循环才修复

---

## ⏰ 时间线预测

### 今晚 18:30（Phase 3e 首次运行）
```
Phase 3e 执行:
  - 扫描所有策略
  - 回填 signal evidence
  - saved_signal_evidence_count: 0 → 1+
```

### 18:30 - 18:35（Phase 3f 执行）
```
Phase 3f: Execution audit acceptance
  - 重新评估 bootstrap 状态
  - bootstrap_pending → bootstrap_ready（如果 lineage 完整）
  - execution_hard_gate_passed: false → true
```

### 18:35 - 18:40（Phase 1.5 执行）
```
Phase 1.5: observe → formal 转正决策
  - 检查转正条件
  - 如果满足所有条件：
    - 样本量 ≥3
    - 命中率 ≥55%
    - 语义契约完整
    - 结构性字段完整
    - execution_hard_gate_passed = true ⭐
  - 执行转正：incubating = 'formal_incubation'
```

### 预期：formal_incubation > 0 🎉

---

## 🔍 如何监控

### 18:25 - 开始监控
```bash
# 打开日志监控
tail -f logs/three_factories/incubation_factory.log
```

### 18:30 - 观察 Phase 3e
```bash
# 关键词：Phase 3e, backfill, saved_signal_evidence
grep -E "Phase 3e|backfill|saved.*evidence" logs/three_factories/incubation_factory.log
```

### 18:35 - 观察 Phase 3f 和 1.5
```bash
# 关键词：bootstrap, formal, promoted
grep -E "Phase 3f|Phase 1.5|bootstrap|formal|promoted" logs/three_factories/incubation_factory.log
```

### 第二天早上 - 验证结果
```bash
# 运行快速检查
python scripts/factories/daily_check.py

# 预期看到：
# formal_incubation: > 0
```

---

## 🎯 成功标准

### Phase 3e 成功
```json
{
  "saved_signal_evidence_count": 1,  // 从 0 增加
  "bootstrap_pending": false,        // 解除阻塞
}
```

### Phase 3f 成功
```json
{
  "execution_audit_gate_status": "hard_gate_passed",  // 从 bootstrap_pending 升级
  "execution_hard_gate_passed": true,                 // 从 false 变 true
}
```

### Phase 1.5 成功
```json
{
  "incubating": "formal_incubation",  // 从 observe_incubation 转正
}
```

### 数据库验证
```sql
-- 应该 > 0
SELECT COUNT(*) FROM strategies WHERE incubating = 'formal_incubation';
```

---

## ⚠️ 如果 18:30 后仍然 bootstrap_pending？

### 诊断步骤

1. **检查 Phase 3e 是否执行**
```bash
grep "Phase 3e" logs/three_factories/incubation_factory.log
```

2. **检查 backfill 结果**
```bash
grep "backfill" logs/three_factories/incubation_factory.log
```

3. **手动运行诊断**
```bash
python scripts/factories/diagnose_formal_blockers.py
```

4. **检查数据库**
```sql
SELECT 
  COUNT(*) as total,
  SUM(CASE WHEN json_extract(params, '$.signal_evidence_count') > 0 THEN 1 ELSE 0 END) as with_evidence
FROM strategies 
WHERE incubating = 'observe_incubation';
```

---

## 💡 为什么不手动修复？

### 理由 1: 自动化已就绪
- Phase 3e 已实现并测试
- 会在主循环中自动运行
- 无需手动干预

### 理由 2: 避免数据不一致
- 手动 SQL 可能遗漏审计记录
- Phase 3e 包含完整的 lineage 构建
- 自动化保证一致性

### 理由 3: 时间充足
- 距离 18:30 只有 6 小时
- 不是紧急阻塞问题
- 等待自动修复更安全

---

## 📚 相关代码

### Phase 3e 实现
```python
# packages/akshare-mcp/src/akshare_mcp/services/incubation_factory/runner.py:427

logger.info("IncubationFactory [%s] Phase 3e: Native execution evidence backfill", run_id)
native_evidence_backfill_result = await _run_phase(
    "native_execution_evidence_backfill",
    lambda: self._run_native_execution_evidence_backfill(
        db,
        strategies=list(incubating) + list(paper_observation),
    ),
    timeout=BATCH_TIMEOUT_SEC,
) or {}
```

### Phase 3f 实现
```python
# packages/akshare-mcp/src/akshare_mcp/services/incubation_factory/runner.py:438

logger.info("IncubationFactory [%s] Phase 3f: Execution audit acceptance", run_id)
execution_audit_acceptance_result = await _run_phase(
    "execution_audit_acceptance",
    lambda: self._run_execution_audit_acceptance(
        db,
        strategies=list(incubating) + list(paper_observation),
    ),
    timeout=BATCH_TIMEOUT_SEC,
) or {}
```

---

## 🎉 结论

**现状**: Bootstrap Pending 是预期状态
- Catchup 只是快照，不执行 evidence backfill
- Signal Evidence 存在但未保存映射
- 数据完整，只是关联缺失

**行动**: 等待今晚 18:30 自动修复
- Phase 3e 会回填 signal evidence
- Phase 3f 会解除 bootstrap pending
- Phase 1.5 会执行首批转正

**预期**: 明早看到 formal_incubation > 0 🎯

---

**报告完成**: 2026-06-22 12:30
