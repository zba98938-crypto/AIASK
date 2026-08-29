# 研究档（lite）成功标准


> **Hermes 集成副本勘误（2026-07-18）**  
> 本文是历史/上游阶段材料，不是当前 Hermes 树完成证明。  
> 在当前 `integrations/aiask/` 副本中：`packages/desktop-api` 不存在；Hermes Desktop 不是完整 AIASK workbench；live trading 默认关闭。  
> 权威状态：`AIASK_FINANCE_AGENT_COMPLETION_AUDIT.md`、`integrations/aiask/docs/CURRENT_PRODUCT_TRUTH.md`、`docs/GAP_INVENTORY_STATUS.md`。

> 目的：避免把 `AKSHARE_QUALITY_PROFILE=lite` 误读成“可以 formal / 可以上线”。

## 1. 两档语义

| 档位 | 环境 | 目标 | 非目标 |
|------|------|------|--------|
| **研究档 lite** | `AKSHARE_QUALITY_PROFILE=lite` | 打通因子→策略→证据闭环，寻找 **skill 转正** 的策略家族 | 不等于生产可上线 |
| **生产档 strict** | `AKSHARE_QUALITY_PROFILE=strict` | 严格 IC/多重检验/晋级门槛，服务 formal / 实盘候选 | 不以“能跑通”为成功 |

当前默认（本地）：**lite 研究档**。

## 2. 研究档成功定义（必须同时满足）

研究档“阶段性成功”定义为出现可复现的策略家族，满足：

1. **h10 `skill_lcb` > 0**（主）
2. **h10 `forward_sharpe` > 0**
3. **h5 `skill_lcb` ≥ -0.02**（允许略弱，但不能显著为负）
4. 有效样本：`h5 effective_n ≥ 40` 且 `h10 effective_n ≥ 25`
5. 同一构造在 **≥2 个不重叠再平衡窗口** 上同向
6. 因子侧截面 IC 与策略侧 skill **同向**（见第 3 节）

> 注意：`formal_incubation > 0` **不是** 研究档成功条件。  
> formal 仍走生产硬门槛（primary/secondary/recent skill_lcb 全 > 0 等）。

## 3. 因子 IC → 策略 skill 一致性

每个进入 observe 的候选，必须同时记录：

| 字段 | 含义 |
|------|------|
| `factor_id` / expression | 来源因子 |
| `factor_rank_ic_mean` / `factor_rank_ic_ir` | 截面研究指标 |
| `strategy_id` | 映射出的策略 |
| `h5/h10 skill_lcb, hit_rate, forward_sharpe` | 可交易表现 |
| `mapping_type` | 如 topn / combo_neutral_weekly |

**一致性规则（研究档）：**

- 若 `abs(factor_rank_ic_mean) ≥ 0.015` 且符号稳定，则策略主窗口 skill 应 **同号**；
- 若 IC 为正而策略 skill 显著为负（h10 skill_lcb < -0.05），记为 **mapping_failure**，不得因因子 promoted 而继续加仓同类映射；
- 因子池 `quality_status=promoted` 仅表示研究消费资格，**不**等于 formal 资格。

## 4. 明确禁止的误读

1. readiness=ready ⇒ 可上线  
2. factor promoted ⇒ 策略可 formal  
3. hit_rate>0.5 ⇒ 有 edge（必须看 skill_lcb）  
4. 为 formal=0 而放宽 `promotion_ready` 硬门槛  

## 5. 生产档 formal 门槛（摘要，不放宽）

代码 `promotion_ready` 需同时：

- primary_effective_n ≥ 60
- secondary_effective_n ≥ 30
- primary / secondary / recent skill_lcb 全 > 0
- coverage_ratio ≥ 0.75
- stability_gap ≤ 0.05
- 无 blockers / execution hard gate 通过

## 6. 相关开关

- `AKSHARE_QUALITY_PROFILE=lite|strict|minimum`
- `STRATEGY_FACTORY_FACTOR_POOL_ADMIT_ACTIVE_WITHOUT_PROMOTION`：仅影响因子是否可被策略工厂消费，不改变 formal 门槛
- `FACTOR_MINING_ALLOW_ATR_INVERSE_VARIANTS`：是否允许短窗 ATR inverse 近失败族

## 7. 当前基线（审查时点）

- 最好策略：`combo_vmr_8cf25c8e`（组合+中性+周频）
- h5 skill_lcb ≈ -0.087，h10 sharpe ≈ +0.11  
- 结论：有改善但仍未达到研究档“skill 转正”成功线，更未达到 formal。
