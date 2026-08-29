# 今晚 18:30 监控清单


> **Hermes 集成副本勘误（2026-07-18）**  
> 本文是历史/上游阶段材料，不是当前 Hermes 树完成证明。  
> 在当前 `integrations/aiask/` 副本中：`packages/desktop-api` 不存在；Hermes Desktop 不是完整 AIASK workbench；live trading 默认关闭。  
> 权威状态：`AIASK_FINANCE_AGENT_COMPLETION_AUDIT.md`、`integrations/aiask/docs/CURRENT_PRODUCT_TRUTH.md`、`docs/GAP_INVENTORY_STATUS.md`。

**关键时间**: 2026-06-22 18:30  
**任务**: 监控 Incubation Factory 首次完整运行  
**预期**: 解除 bootstrap_pending + 首批 formal 转正

---

## ⏰ 时间表

```
18:25 - 准备监控
18:30 - Incubation Factory 启动
18:30-18:35 - Phase 3e (evidence backfill) ⭐
18:35-18:40 - Phase 3f (bootstrap acceptance)
18:40-18:45 - Phase 1.5 (observe → formal) ⭐⭐⭐
18:45-18:50 - 验证结果
```

---

## 📋 监控命令

### 1️⃣ 18:25 - 打开日志监控

```bash
# 终端 1: 实时监控 Incubation Factory
tail -f logs/three_factories/incubation_factory.log

# 终端 2: 过滤关键事件
tail -f logs/three_factories/incubation_factory.log | grep -E "Phase|formal|bootstrap|promoted"
```

---

## 🎯 关键检查点

### Checkpoint 1: 18:30 - 启动确认
```bash
# 应该看到：
# IncubationFactory: next run at 2026-06-22 18:30
# IncubationFactory [xxx] Phase 1: Intake

✅ 启动成功
❌ 如果没启动 → 检查 Supervisor 日志
```

### Checkpoint 2: 18:32 - Phase 3e 执行
```bash
# 查找 Phase 3e 日志
grep "Phase 3e" logs/three_factories/incubation_factory.log

# 应该看到：
# Phase 3e: Native execution evidence backfill
# saved_signal_evidence: X strategies updated

✅ Evidence backfill 完成
❌ 如果没看到 → 继续等待 Phase 3f
```

### Checkpoint 3: 18:35 - Phase 3f 执行
```bash
# 查找 Phase 3f 日志
grep "Phase 3f" logs/three_factories/incubation_factory.log

# 应该看到：
# Phase 3f: Execution audit acceptance
# bootstrap_ready: X strategies

✅ Bootstrap 解除阻塞
❌ 如果仍然 pending → 记录策略 ID，明天诊断
```

### Checkpoint 4: 18:40 - Phase 1.5 转正决策 ⭐⭐⭐
```bash
# 查找 Phase 1.5 日志
grep "Phase 1.5" logs/three_factories/incubation_factory.log

# 应该看到：
# Phase 1.5: observe → formal transition
# promoted: X strategies to formal_incubation

✅ 首批转正成功！🎉
❌ 如果 promoted = 0 → 检查阻塞原因
```

### Checkpoint 5: 18:45 - 完成确认
```bash
# 查看完成摘要
grep "completed" logs/three_factories/incubation_factory.log | tail -5

# 应该看到：
# IncubationFactory [xxx]: completed in X.Xs
# (intake=X, verified=X, promoted=X, ...)

✅ 运行完成
```

---

## 📊 验证命令

### 立即验证（18:50）
```bash
# 快速检查
python scripts/factories/daily_check.py

# 应该看到：
# formal_incubation: > 0  ⭐
```

### 详细验证
```bash
# 检查转正数量
python scripts/factories/diagnose_formal_simple.py

# 应该看到：
# formal_incubation: X 个（X > 0）
# 前 5 个高 skill 策略列表
```

### 数据库验证
```bash
# 直接查询
sqlite3 data/db/akshare_mcp.sqlite3 "SELECT COUNT(*) FROM strategies WHERE incubating = 'formal_incubation';"

# 应该返回：> 0
```

---

## 🎯 成功标准

### 最低目标
- ✅ Phase 3e 执行完成
- ✅ Phase 3f 解除部分 bootstrap_pending
- ✅ Phase 1.5 执行（即使 promoted = 0）
- ✅ 无 phase_failures

### 理想目标
- ✅ bootstrap_pending → 0
- ✅ formal_incubation > 0 ⭐
- ✅ 转正策略有完整契约

### 超预期目标
- ✅ formal_incubation > 10
- ✅ 转正成功率 > 50%

---

## ⚠️ 如果出现问题

### 问题 1: Phase 3e 没执行
```bash
# 诊断
grep -A 20 "Phase 3" logs/three_factories/incubation_factory.log

# 可能原因
- 代码路径错误
- 超时
- 策略列表为空
```

### 问题 2: Bootstrap 仍然 pending
```bash
# 诊断
grep "bootstrap_pending" logs/three_factories/incubation_factory_catchup.log

# 可能原因
- Signal evidence 仍未保存
- Lineage 不完整
- 其他阻塞条件
```

### 问题 3: Promoted = 0
```bash
# 诊断
python scripts/factories/diagnose_formal_blockers.py

# 可能原因
- 样本量不足（< 3）
- 命中率不足（< 55%）
- 语义契约缺失
- execution_hard_gate_passed = false
```

### 紧急联系
```bash
# 如果完全失败，记录以下信息：
1. 最后一条日志
2. phase_failures 内容
3. 运行时长
4. 策略数量分布

# 明天早上诊断
```

---

## 📸 预期日志示例

### 成功的 Phase 3e
```
2026-06-22 18:32:15 [INFO] IncubationFactory [abc123] Phase 3e: Native execution evidence backfill
2026-06-22 18:32:18 [INFO] Backfill: saved_signal_evidence updated for 200 strategies
2026-06-22 18:32:18 [INFO] Phase 3e completed: trades_with_evidence=200
```

### 成功的 Phase 1.5
```
2026-06-22 18:40:22 [INFO] IncubationFactory [abc123] Phase 1.5: observe → formal transition
2026-06-22 18:40:25 [INFO] Evaluated 200 strategies for formal promotion
2026-06-22 18:40:25 [INFO] Promoted 15 strategies to formal_incubation
2026-06-22 18:40:25 [INFO] Blockers: 185 strategies (skill=120, contracts=30, structure=25, execution=10)
```

### 成功的完成摘要
```
2026-06-22 18:45:30 [INFO] IncubationFactory [abc123]: completed in 15.2s
  (intake=5, verified=200, promoted=15, hit_rate=7.50%, phase_failures=0)
```

---

## 🎉 如果成功

### 庆祝 🎊
- **首批 formal 转正！**
- 从 "formal=0" 到 "formal>0" 的历史性突破
- P0-P2 修复全部验证通过

### 下一步
1. 明早运行完整监控
2. 分析转正策略特征
3. 调整转正条件（如果需要）
4. 规划 production 晋升

### 记录里程碑
```bash
# 创建里程碑记录
echo "2026-06-22 18:30 - 首批 formal 转正成功" >> docs/factory-architecture/MILESTONES.txt
```

---

## 📝 监控笔记模板

```
=== 2026-06-22 18:30 Incubation Factory 监控 ===

启动时间: 18:30:XX
Phase 3e 完成: 18:3X:XX (saved_evidence=X)
Phase 3f 完成: 18:3X:XX (bootstrap_ready=X)
Phase 1.5 完成: 18:4X:XX (promoted=X)
总耗时: X.X秒
phase_failures: X

formal_incubation 数量:
  - 运行前: 0
  - 运行后: X

成功标准:
  ✅/❌ Phase 3e 执行
  ✅/❌ Bootstrap 解除
  ✅/❌ Formal 转正
  ✅/❌ 无 failures

备注:
[记录任何异常或特殊情况]
```

---

**清单创建**: 2026-06-22 12:30  
**下次检查**: 2026-06-22 18:25  
**预期结果**: formal_incubation > 0 🎯
