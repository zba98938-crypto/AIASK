# 18:30 运行实际情况报告


> **Hermes 集成副本勘误（2026-07-18）**  
> 本文是历史/上游阶段材料，不是当前 Hermes 树完成证明。  
> 在当前 `integrations/aiask/` 副本中：`packages/desktop-api` 不存在；Hermes Desktop 不是完整 AIASK workbench；live trading 默认关闭。  
> 权威状态：`AIASK_FINANCE_AGENT_COMPLETION_AUDIT.md`、`integrations/aiask/docs/CURRENT_PRODUCT_TRUTH.md`、`docs/GAP_INVENTORY_STATUS.md`。

**时间**: 2026-06-22 18:35  
**状态**: Catchup 完成，但 formal 仍为 0

---

## 📊 实际运行结果

### Incubation Factory Catchup 执行
```
启动时间: 18:30:57
完成时间: 18:34:26
运行时长: 208.7 秒

Phase 执行:
  ✅ Phase 1: Intake
  ✅ Phase 2: 200 incubating + 1500 paper
  ✅ Phase 3: signals=0, filled=69, verified=1700
  ✅ Phase 3e: Native execution evidence backfill
  ✅ Phase 3f: Execution audit acceptance
  ✅ Phase 3g: Execution audit remediation
  ✅ Phase 4: Pipeline evaluation
  ✅ Phase 5: Hit rate report
  ✅ Phase 6: Feedback write
  ✅ Phase 7: Acceleration check
  ✅ Phase 8: Alert check

最终状态:
  promoted: 0 ❌
  phase_failures: 1
  hit_rate: 38.30%
  status: partial
```

---

## ❌ 关键问题发现

### 1. Phase 3e 执行但无效果
```
Phase 3f 结果:
  saved_signal_evidence: 0 ❌
  available_signal_evidence: 108
  hard_gate_passed: 0 ❌
  
结论: Phase 3e 执行了，但没有保存任何 signal evidence
```

### 2. 数据库状态
```
observe_incubation: 16,491 个
formal_incubation: 0 个 ❌

有 paper_trades: 3,093 个策略
有 strategy_signals: 8,248 个策略

数据完整性: ✅
Signal-Trade 关联: ❌（Phase 3e 未生效）
```

### 3. 发现的 Bug
```python
NameError: name 'as_of' is not defined
  at Phase 3c2: Exit signal paper execution
```

---

## 🔍 根本原因分析

### 为什么 Phase 3e 无效？

#### 可能原因 1: 候选集为空
Phase 3e 的逻辑：
```python
# 查找有 trades 但无 signal evidence 的策略
candidates = strategies with (trades or positions) and not evidence
```

如果候选集为空 → 跳过 backfill → saved = 0

#### 可能原因 2: Backfill 方法失败
```python
backfill = _resolve_db_async_method(db, "backfill_strategy_signal_evidence_native")
```

如果方法不存在或失败 → 静默跳过 → saved = 0

#### 可能原因 3: as_of 参数缺失
Phase 3c2 报错：`name 'as_of' is not defined`

可能影响后续 phase 的执行

---

## 🎯 为什么 formal 仍为 0？

### 必要条件检查

#### ✅ 1. 数据就绪
```
observe策略: 16,491 个
高 skill: 868 个
前向收益: 39,449 条
```

#### ❌ 2. Signal Evidence
```
saved_signal_evidence: 0
→ execution_hard_gate_passed: false
→ 无法通过转正门槛
```

#### ❌ 3. Phase 1.5 未执行
```
Catchup 不包含 Phase 1.5!
```

**关键发现**: 
- **Catchup 没有 Phase 1.5（转正决策）**
- **只有主循环（daemon run_once）才有 Phase 1.5**
- **但主循环跳到了明天 18:30**

---

## 🚨 主要问题：Daemon 调度逻辑错误

### 当前逻辑
```python
if now >= target:  # 18:30:57 >= 18:30:00
    target += timedelta(days=1)  # 跳到明天
    
await asyncio.sleep(wait_seconds)  # 等待 24 小时
result = await self.run_once()  # 然后才执行
```

### 问题
- 启动时间 18:30:57 **已过** 目标时间 18:30:00
- 直接跳到明天 18:30
- **今天不会执行 run_once**
- **Phase 1.5 不会运行**
- **formal 转正不会发生**

### 应该的逻辑
```python
if now >= target and (now - target) < grace_period:
    # 在宽限期内，立即执行
    result = await self.run_once()
    target += timedelta(days=1)
else:
    # 超过宽限期，等待下一次
    if now >= target:
        target += timedelta(days=1)
```

---

## 📅 实际时间线修正

### ❌ 原预期
```
18:30 - Daemon 运行 run_once
18:32 - Phase 3e (evidence backfill)
18:35 - Phase 3f (bootstrap acceptance)
18:40 - Phase 1.5 (observe → formal) ⭐
→ formal_incubation > 0
```

### ✅ 实际情况
```
18:30 - Daemon 启动
18:31 - Catchup 运行
        ├─ Phase 3e 执行（但 saved=0）
        ├─ Phase 3f 执行（hard_gate=0）
        └─ 没有 Phase 1.5 ❌
18:34 - Catchup 完成
下次运行: 明天 18:30 ⏰
```

---

## 🔧 需要的修复

### 紧急修复（P0）
1. **修复 Daemon 调度逻辑**
   - 允许启动后宽限期内执行
   - 或者：首次启动立即执行一次
   
2. **修复 Phase 3c2 的 as_of bug**
   ```python
   NameError: name 'as_of' is not defined
   ```

### 重要修复（P1）
3. **Phase 3e 添加日志输出**
   - 当前无法诊断为什么 saved=0
   - 需要记录候选集大小、backfill 结果

4. **验证 backfill 方法可用性**
   - `backfill_strategy_signal_evidence_native`
   - 可能不存在或未实现

---

## 💡 临时解决方案

### 方案 A: 手动触发 run_once（推荐）
```bash
# 创建一次性运行脚本
python scripts/factories/run_incubation_factory.py --json
```

这会立即执行完整的 run_once，包括 Phase 1.5

### 方案 B: 修改 run_time 到未来几分钟
```bash
# 停止当前 daemon
# 修改启动参数: --run-time 18:50
# 重启
```

### 方案 C: 等待明天 18:30
- 最保守
- 但延迟 24 小时

---

## 📊 新增数据发现

### 策略增长巨大
```
最近 24 小时:
  新增策略: 900 个（从 101 → 1000）
  新增信号: 243 个（从 89 → 332）
  
Strategy Factory 非常活跃（Cycle 1025）
```

### 孵化积压
```
Alert: 孵化中策略数 500 超过阈值 100
  
这是好事: 说明生产运营正常
需要关注: 转正流程必须启动
```

---

## 🎯 下一步行动

### 立即（今晚）
1. **决定是否手动触发 run_once**
   - 优点: 立即看到结果
   - 缺点: 需要手动干预
   
2. **或者等待明天 18:30**
   - 优点: 自动运行
   - 缺点: 延迟 24 小时

### 明天
1. 修复 Daemon 调度逻辑
2. 修复 Phase 3c2 的 as_of bug
3. 增强 Phase 3e 日志
4. 验证 backfill 方法

### 本周
1. 监控 formal 转正速度
2. 分析转正策略特征
3. 调优转正条件

---

## 📝 经验教训

### 1. Catchup ≠ 主循环
- Catchup: 快速状态检查，不含转正决策
- 主循环: 完整周期，包含 Phase 1.5

### 2. Daemon 调度需要宽限期
- 精确时间启动几乎不可能
- 需要宽限期逻辑（如 5 分钟）

### 3. 关键 Phase 需要详细日志
- Phase 3e 无日志 = 无法诊断
- 应该记录候选集、执行结果

---

**报告完成**: 2026-06-22 18:35  
**formal_incubation**: 仍为 0  
**下次机会**: 明天 18:30 或手动触发
