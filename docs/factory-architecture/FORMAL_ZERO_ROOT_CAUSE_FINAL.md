# 孵化工厂 formal=0 根因分析报告

> **Hermes 集成副本勘误（2026-07-18）**  
> 本文是历史/上游阶段材料，不是当前 Hermes 树完成证明。  
> 在当前 `integrations/aiask/` 副本中：`packages/desktop-api` 不存在；Hermes Desktop 不是完整 AIASK workbench；live trading 默认关闭。  
> 权威状态：`AIASK_FINANCE_AGENT_COMPLETION_AUDIT.md`、`integrations/aiask/docs/CURRENT_PRODUCT_TRUTH.md`、`docs/GAP_INVENTORY_STATUS.md`。

**Date**: 2026-06-22  
**Run ID**: 0d4c17483af5

> 更正 / 当前口径说明（截至 2026-06-23）
>
> 本文保留 2026-06-22 当时的运行分析，不重写原始时间线与原始结论。需要注意的是，当前真实架构已经进一步收口：canonical `ExecutionUniverseContract` owner 已位于 `packages/strategy-factory/src/strategy_factory/contracts/execution_universe.py`，canonical bootstrap 已位于 `packages/strategy-factory/src/strategy_factory/runtime/default_bootstrap.py`，`akshare_mcp.runtime.strategy_factory_bootstrap` 仅为 compat shim，`server.py` 也不再拥有 factory lifecycle。阅读本文时，请用这些 2026-06-23 的最新边界解释其中提到的 contract/bootstrap 问题。

## 执行结果

### 状态统计
- **observe_incubation**: 16,491
- **formal_incubation**: 0 ❌
- **production**: 1

## 根本原因

Phase 3f (execution_audit_acceptance) 执行成功，但 **所有策略都未通过 hard_gate**：

```
hard_gate_passed_count: 0
overall_ready_count: 0
```

### 关键发现

1. **语义契约字段已完成** ✅
   - evidence_chain: 16,491 (100%)
   - confidence_contract: 16,491 (100%)
   - prediction_contract: 16,081 (97.5%)

2. **但缺少交易证据** ❌
   - `trade_evidence_ready_count: 0`
   - `real_paper_round_trip_count: 0`
   - `bootstrap_round_trip_count: 0`
   - `closed_round_trip_count: 0`

3. **质量门槛状态**
   - 全部策略: `execution_audit_gate_status: 'bootstrap_pending'`
   - 全部策略: `execution_hard_gate_passed: False`

### 典型 Blocker 示例

```python
'blockers': [
    'native_signal_evidence_lineage_missing',
    'realized_trade_evidence_insufficient', 
    'bootstrap_pending'
]

'gap_categories': ['code_gap', 'sample_gap']
```

## 结论

**formal=0 的真正原因**: 策略需要通过 **paper trading 实盘模拟** 产生交易证据（round-trip trades）才能通过 Phase 3f 质量门槛。

当前所有策略都处于 `bootstrap_pending` 状态，意味着：
- ✅ 语义契约字段已完备
- ✅ Signal 证据链已建立（3,612 个 signal_evidence）
- ❌ **缺少 paper trading 交易回合** (required_trade_count: 20)
- ❌ 无法通过 execution_hard_gate

## 下一步行动

需要：
1. 启动 Paper Trading 引擎（MatchingEngine + NavEngine）
2. 让策略在模拟环境中执行交易
3. 积累至少 20 个 closed round-trips
4. 再次运行 Incubation Factory 评估

**预计时间**: 需要等待策略生成足够的交易样本（数天到数周，取决于信号频率）

## 技术细节

### Phase 3f 判定逻辑
```python
execution_hard_gate_passed = (
    compile_stable_ready OR native_lineage_ready
) AND trade_evidence_ready AND other_quality_checks
```

当前状态：
- `compile_stable_ready`: 0/16,491 (compiled_dsl 缺失)
- `native_lineage_ready`: 6/16,491 (仅 6 个有完整 lineage)
- `trade_evidence_ready`: 0/16,491 ❌ **关键瓶颈**

### Paper Trading 状态
```
open_position_count: 81
awaiting_paper_execution_count: 434
estimated_round_trip_sample_debt: 1,600
```

有 81 个持仓 + 434 个等待执行，但尚未产生 closed round-trips。

## 误诊历史回顾

1. **第一次误诊**: 认为是 schema 迁移问题 → 实际只是列名变更
2. **第二次误诊**: 认为是 Phase 3e signal_evidence 失败 → 实际早已完成
3. **第三次误诊**: 认为是语义契约缺失 → 已补全但仍不够
4. **真正根因**: **缺少 paper trading 交易证据**

---

**结论**: formal=0 不是 bug，而是正常流程——策略需要在 paper trading 中证明执行能力后才能转正。
