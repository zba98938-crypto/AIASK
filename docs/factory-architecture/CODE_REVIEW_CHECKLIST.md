# 策略工厂代码审查清单


> **Hermes 集成副本勘误（2026-07-18）**  
> 本文是历史/上游阶段材料，不是当前 Hermes 树完成证明。  
> 在当前 `integrations/aiask/` 副本中：`packages/desktop-api` 不存在；Hermes Desktop 不是完整 AIASK workbench；live trading 默认关闭。  
> 权威状态：`AIASK_FINANCE_AGENT_COMPLETION_AUDIT.md`、`integrations/aiask/docs/CURRENT_PRODUCT_TRUTH.md`、`docs/GAP_INVENTORY_STATUS.md`。

## ⚠️ 强制性检查清单

**所有策略工厂相关的代码修改，必须在提交前通过本清单的所有检查项。**

任何一项检查不通过，代码不得合并到主分支。

---

## 提交前检查（开发人员自查）

### 1. 规范符合性检查

- [ ] 我已阅读 [02-策略工厂全链路生命周期规范.md](02-策略工厂全链路生命周期规范.md)
- [ ] 我已阅读 [11-禁止的修复方式与正确路径.md](11-禁止的修复方式与正确路径.md)
- [ ] 我的代码修改不在"禁止行为"列表中
- [ ] 我的代码修改符合"强制要求"

### 2. 统一状态机检查

- [ ] 我没有绕过 `StrategyLifecycleLedger` 统一入口（如果已实现）
- [ ] 我没有从多个表（strategies, strategy_signals, paper_orders, paper_trades）拼接状态
- [ ] 我没有把业务覆盖层状态（如 `paper_signalled`）写入数据库主状态字段
- [ ] 如果我查询了策略状态，我能明确说出"这个策略卡在哪个组件"

### 3. Quality Session 纯验证模式检查

- [ ] 我没有在 `run_strategy_factory_quality_session.py` 中添加新的环境变量
- [ ] 我没有启用 `INCUBATION_FACTORY_PAPER_EXECUTION_BACKLOG_ENABLED`
- [ ] 我没有启用 `INCUBATION_FACTORY_EXECUTION_AUDIT_NATIVE_EVIDENCE_BACKFILL_ENABLED`
- [ ] 我没有启用 `INCUBATION_FACTORY_STALE_PAPER_POSITION_CLOSURE_ENABLED`
- [ ] 如果我需要补偿逻辑，我已将其移到生产控制面（Incubation Factory 主线）

### 4. 调度预算检查

- [ ] 我没有单纯增加 `STRATEGY_FACTORY_RUN_ONCE_TIMEOUT_SEC` 数值
- [ ] 如果我修改了 timeout，我已分析是否为调度预算失配
- [ ] 如果涉及批量 LLM 调用，我已实现批次限流
- [ ] 如果涉及并行任务，我已为每个阶段设置独立 timeout

### 5. 执行宇宙一致性检查

- [ ] 我没有在 SignalTracker 和 Incubation 中分别实现策略查询逻辑
- [ ] 如果我修改了可执行策略的查询，我已检查两边是否一致
- [ ] 我能解释为什么某个策略"在 SignalTracker 有信号但 Incubation 没订单"

### 6. 证据链路完整性检查

- [ ] 我没有只实现 buy 而不实现 sell
- [ ] 如果我修改了 paper execution，我已确保 exit signal 和 stale close 逻辑存在
- [ ] 我没有通过 backfill 手工插入 paper_trades
- [ ] 我的代码能让 `realized_trade_count` 自然增长

### 7. Audit Gate 语义检查

- [ ] 我没有把 `bootstrap_pending` 当作 production hard gate 通过条件
- [ ] 我没有放宽 production hard gate 标准（默认 20 笔 realized trades）
- [ ] 如果 audit gate 不通过，我能区分"链路断裂（missing）"和"样本债（insufficient_samples）"
- [ ] 我没有在报告中掩盖 hard gate 未通过的真相

### 8. 健康报告诚实性检查

- [ ] 我没有用 `success` 掩盖 `partial_infra` 或 `failed`
- [ ] 我没有在底层 timeout 时报告 `success`
- [ ] 我没有把 `pending_evidence`（正常状态）报告为"问题"
- [ ] 我没有把 `degraded`/`blocked`（真问题）报告为"健康"

### 9. 数据库操作检查

- [ ] 我没有手工修改数据库来"修复"证据链路
- [ ] 我没有手工插入 strategy_signals/paper_orders/paper_trades
- [ ] 我没有绕过 ORM/repository 直接执行 UPDATE/DELETE
- [ ] 如果我修改了 schema，我已更新相关文档

### 10. 文档更新检查

- [ ] 如果我实现了新的组件，我已在规范文档中标注"已实现"
- [ ] 如果我修改了状态机，我已更新 Mermaid 状态图
- [ ] 如果我发现规范与代码不一致，我已提交规范更新
- [ ] 我没有在代码注释中承诺"后续实现"而不更新规范

---

## 代码审查检查（审查人员）

### 1. 根因修复检查

- [ ] 这个修改是在修复根因，而不是打补丁？
- [ ] 这个修改没有引入新的 Phase 3x？
- [ ] 这个修改没有通过补偿逻辑掩盖生产缺陷？
- [ ] 这个修改符合 [11-禁止的修复方式与正确路径.md](11-禁止的修复方式与正确路径.md) 的正确路径？

### 2. 架构一致性检查

- [ ] 这个修改没有引入新的"局部真相来源"？
- [ ] 这个修改没有绕过已有的统一入口/契约？
- [ ] 这个修改没有在不同组件中重复实现相同逻辑？
- [ ] 这个修改的责任边界清晰（属于哪个工厂/组件）？

### 3. 测试覆盖检查

- [ ] 这个修改有对应的测试用例？
- [ ] 测试用例覆盖了正常路径和异常路径？
- [ ] 测试用例能验证"修复后诊断状态明确改善"？
- [ ] 测试用例没有依赖 Quality Session 的补偿逻辑？

### 4. 诊断能力检查

- [ ] 这个修改增强了诊断能力（能明确说出 blocker_component）？
- [ ] 这个修改的失败日志足够诊断根因？
- [ ] 这个修改没有用 try-except 吞掉关键错误信息？
- [ ] 这个修改的 skip_reason 足够明确？

### 5. 运维友好性检查

- [ ] 这个修改没有增加运维复杂度（如需要手工干预）？
- [ ] 这个修改的配置路径清晰（环境变量/配置文件）？
- [ ] 这个修改的失败恢复路径明确？
- [ ] 这个修改符合 [06-运行与诊断手册.md](06-运行与诊断手册.md) 的运维规范？

---

## 特殊场景检查

### 场景 1：修改 Quality Session

**如果你在修改 `run_strategy_factory_quality_session.py`，必须额外检查**：

- [ ] 我是在**移除**补偿逻辑，而不是添加？
- [ ] 我是在增强诊断能力，而不是掩盖问题？
- [ ] 我没有让验证脚本承担生产职责？
- [ ] 我的修改让报告更诚实，而不是更"好看"？

### 场景 2：修改 Timeout 参数

**如果你在修改任何 `*_TIMEOUT_SEC` 环境变量，必须额外检查**：

- [ ] 我已诊断根因是否为调度预算失配？
- [ ] 我已评估是否应该分阶段预算而不是增加整轮 timeout？
- [ ] 我已评估是否应该批次限流而不是增加单批 timeout？
- [ ] 我能用数学证明新 timeout 值是合理的（任务数 × 单任务耗时 < timeout）？

### 场景 3：修改 Audit Gate

**如果你在修改 `evaluate_execution_audit_gate` 或相关逻辑，必须额外检查**：

- [ ] 我没有放宽 production hard gate 标准（默认 20 笔）？
- [ ] 我没有把 `bootstrap_pending`/`bootstrap_ready` 当作 `passed`？
- [ ] 我能解释为什么修改是必要的（而不是等待样本成熟）？
- [ ] 我的修改有业务负责人的明确批准？

### 场景 4：添加补偿逻辑

**如果你在添加 backlog/backfill/stale close 等补偿逻辑，必须额外检查**：

- [ ] 我已确认这是生产控制面，而不是验证脚本？
- [ ] 我已实现相应的诊断工具来区分"自然证据"和"补偿证据"？
- [ ] 我已在规范文档中说明为什么需要补偿（而不是修复根因）？
- [ ] 我已添加开关让补偿逻辑可以禁用？

### 场景 5：修改状态机

**如果你在修改 `strategies.status` 或 `strategy_incubation_accounts.stage` 的取值，必须额外检查**：

- [ ] 我没有添加业务覆盖层状态（如 `paper_signalled`）到数据库？
- [ ] 我已更新物理状态机的 Mermaid 图？
- [ ] 我已更新 [02-策略工厂全链路生命周期规范.md](02-策略工厂全链路生命周期规范.md) 的映射规则？
- [ ] 我能解释新状态的进入条件和退出条件？

---

## 验收测试清单

### 自动化测试（待实现）

提交代码前运行：

```bash
# 1. 生命周期账本一致性测试
uv run python scripts/factories/test_lifecycle_ledger_consistency.py

# 2. 执行宇宙一致性测试
uv run python scripts/factories/test_execution_universe_consistency.py

# 3. 证据链路完整性测试
uv run python scripts/factories/test_evidence_chain_completeness.py

# 4. Quality Session 纯验证模式
uv run python scripts/factories/run_strategy_factory_quality_session.py --no-compensation
```

**如果上述测试尚未实现，必须手工验证对应的检查项。**

### 手工验证（当前必须）

1. **健康诊断验证**：
   ```bash
   uv run python scripts/factories/diagnose_factory_health.py --verbose
   ```
   确认修复后诊断状态明确改善。

2. **规范符合性验证**：
   - 打开 [10-规范符合性清单.md](10-规范符合性清单.md)
   - 逐项确认修改符合规范

3. **证据链路验证**：
   ```sql
   -- 检查信号是否转成订单
   SELECT 
     (SELECT COUNT(DISTINCT strategy_id) FROM strategy_signals WHERE signal != 0) as strategies_with_signal,
     (SELECT COUNT(DISTINCT strategy_id) FROM paper_orders) as strategies_with_orders;
   
   -- 检查订单是否转成成交
   SELECT 
     (SELECT COUNT(*) FROM paper_orders) as total_orders,
     (SELECT COUNT(*) FROM paper_trades) as total_trades;
   
   -- 检查是否有 closed positions
   SELECT status, COUNT(*) FROM strategy_trade_positions GROUP BY status;
   ```

4. **执行宇宙一致性验证**（手工版）：
   ```python
   # 在 Python REPL 中
   from akshare_mcp.services.incubation_parts.runtime import IncubationRuntime
   
   # 检查 SignalTracker 和 Incubation 看到的策略集合
   signal_tracker_strategies = await list_execution_universe_candidates(db)
   incubation_strategies = await list_active_paper_observation_strategies(db)
   
   # 对比差异
   only_in_signal_tracker = set(signal_tracker_strategies) - set(incubation_strategies)
   only_in_incubation = set(incubation_strategies) - set(signal_tracker_strategies)
   
   print(f"Only in SignalTracker: {only_in_signal_tracker}")
   print(f"Only in Incubation: {only_in_incubation}")
   ```

---

## 拒绝合并的典型理由

以下情况代码将被拒绝合并，必须修复后重新提交：

1. ❌ **在 Quality Session 中启用补偿逻辑**
   - 理由：违反 "Quality Session 纯验证模式" 强制性规范
   - 修复：移除补偿开关，或将补偿逻辑移到生产控制面

2. ❌ **单纯增加 timeout 数值**
   - 理由：掩盖调度预算失配问题
   - 修复：分析根因，实现分阶段预算或批次限流

3. ❌ **绕过统一状态机查询**
   - 理由：引入新的"局部真相来源"
   - 修复：使用 `StrategyLifecycleLedger.get_state()` 统一入口

4. ❌ **放宽 production hard gate 标准**
   - 理由：违反生命周期规范的晋级规则
   - 修复：补全证据生产链路，等待样本成熟

5. ❌ **用 `success` 掩盖 `failed`/`partial_infra`**
   - 理由：健康报告不诚实
   - 修复：诚实报告底层状态，不外层包装

6. ❌ **手工修改数据库"修复"证据链路**
   - 理由：掩盖生产链路缺陷
   - 修复：修复代码让系统自动生产证据

7. ❌ **添加新的 Phase 3x 补偿阶段**
   - 理由：继续打补丁而非修复根因
   - 修复：按照 [11-禁止的修复方式与正确路径.md](11-禁止的修复方式与正确路径.md) 的正确路径修复

---

## 签署确认

**开发人员签署**：

我已阅读并理解本检查清单，我的代码修改通过了所有检查项。

- 开发人员：_____________
- 日期：_____________
- 提交 SHA：_____________

**审查人员签署**：

我已审查该代码修改，确认其符合策略工厂强制性规范。

- 审查人员：_____________
- 日期：_____________
- 审查意见：_____________

---

## 附录：检查清单版本历史

| 版本 | 日期 | 修改内容 |
| --- | --- | --- |
| 1.0 | 2026-06-21 | 初始版本 - 根据规范文档创建 |
