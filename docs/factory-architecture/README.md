# 策略工厂四工厂架构与技术规范


> **Hermes 集成副本勘误（2026-07-18）**  
> 本文是历史/上游阶段材料，不是当前 Hermes 树完成证明。  
> 在当前 `integrations/aiask/` 副本中：`packages/desktop-api` 不存在；Hermes Desktop 不是完整 AIASK workbench；live trading 默认关闭。  
> 权威状态：`AIASK_FINANCE_AGENT_COMPLETION_AUDIT.md`、`integrations/aiask/docs/CURRENT_PRODUCT_TRUTH.md`、`docs/GAP_INVENTORY_STATUS.md`。

## 规范地位与强制性

**本文档集是策略工厂开发的强制性技术规范，不是参考建议。**

所有策略工厂相关的代码开发、架构修改、功能添加、问题修复，**必须**先检查是否符合本规范。任何违反规范的代码修改（包括但不限于：新增补偿逻辑、添加 Phase 3x 阶段、绕过统一状态机、在 Quality Session 中启用生产补偿开关）都**不得**合并到主分支。

### 规范目标

把策略工厂从补丁式修复拉回统一架构：
- 四工厂各自负责什么、为什么存在、要达成什么目标、产生什么证据
- 失败时如何定位责任组件
- 如何通过统一生命周期账本裁决策略当前状态
- 所有判断标准必须可执行、可验证

### 规范更新原则

本文档以当前代码、启动脚本、Graphify 代码图谱、AKShare MCP 资源目录、测试和最近运行事实为准；历史报告和根目录 Markdown 只作为问题背景。

**规范与代码冲突时的处理**：
1. 若规范描述的是”目标设计”，代码尚未实现 → 代码必须按规范实现，或在规范中标注”当前未实现”
2. 若规范描述与实际代码行为不符 → 优先修正文档，但必须评估代码是否违反架构原则
3. 若代码通过打补丁方式绕过规范 → 必须回退补丁，按规范重新设计

### 违反规范的典型行为

**禁止**以下修复方式：
- ❌ 在 Quality Session 中通过环境变量启用 backlog/backfill/stale close 等补偿逻辑
- ❌ 添加新的 Phase 3c/3d/3e/... 来打补丁，而不是修复根因
- ❌ 用 `success` 状态掩盖 `partial_infra` 或 `failed` 真相
- ❌ 绕过 `ExecutionUniverseContract` 统一查询接口，使用局部查询路径
- ❌ 把 `bootstrap_pending/bootstrap_ready` 当作 production hard gate 通过条件
- ❌ 在没有统一生命周期状态机的情况下，从多个局部真相拼出健康报告
- ❌ 修改单个 timeout 参数来掩盖调度预算失配问题

## 当前结论

- 正式”四工厂”运行主管口径为：`Strategy Factory`、`Factor Mining Factory`、`Incubation Factory`、`Market Event Ingest`。
- `SignalTracker` 是策略孵化闭环必需的 sidecar，不再混称为 supervisor 管理的第四工厂。
- `scripts/factories/run_three_factories.py` 是当前四运行体 supervisor（**注意：文件名是历史遗留命名，实际启动四个运行体**）；`scripts/factories/run_all_factories.py` 是兼容入口。
- `scripts/factories/run_strategy_factory_quality_session.py` 是验证会话（脚本级验证工具，不是包内领域类），不是生产 supervisor，也不应该承担生产补偿逻辑。
- 生命周期必须分成物理状态和业务覆盖层：`strategies.status`、`strategy_incubation_accounts.stage/status` 是实际存储（实际数据库中 stage 只有 `warmup`、`diagnostic`、`failed`，无 `paper`/`observe`/`candidate` 等值）；`generated -> ... -> promotion_ready` 是证据派生诊断状态。
- production hard gate 只认 `execution_audit_gate_status='passed'`；`bootstrap_pending/bootstrap_ready` 只能作为诊断或样本债状态。
- `healthy/pending_evidence/degraded/blocked/unsafe` 是规范级健康模型，当前代码尚未统一实现为同一个 enum/class。
- canonical `ExecutionUniverseContract` 已位于 `packages/strategy-factory/src/strategy_factory/contracts/execution_universe.py`；剩余工作是继续压缩 SignalTracker/Incubation 的 legacy fallback 与 compat 消费面，而不是继续回迁 contract owner。
- canonical bootstrap 已位于 `packages/strategy-factory/src/strategy_factory/runtime/default_bootstrap.py`；`akshare_mcp.runtime.strategy_factory_bootstrap` 仅保留 compat shim 角色。
- 四个 runtime 的 canonical 入口已落在 `strategy_factory.runtime.*`；`packages/akshare-mcp/src/akshare_mcp/server.py` 当前仅承担 MCP host 与非工厂后台服务，不再拥有 Strategy Factory、SignalTracker、MatchingEngine、NavEngine、FactorScheduler 的 embedded lifecycle。
- 当前会话未暴露可调用的 `thinking` MCP；本轮深度审查使用了可见的 `akshare-mcp` resources、Graphify、当前源码和公开联网资料。若后续环境暴露 `thinking` MCP，应把其审查结论追加到 [09-深度架构审查报告](09-深度架构审查报告.md)。

## 当前 source-of-truth 入口

### 文档归类口径

- 仓库级文档导航见 `../README.md`。
- 本目录主要承接四工厂 current-state 文档、运行规范、架构审查和能力台账。
- `strategy-factory-spec/` 目录只承接 target-state 规格与标准化目标，不作为现状证明。
- 历史运行报告、阶段性附录和一次性观测记录统一放在 `appendix/` 下；其中运行报告现集中在 `appendix/reports/`。
- 根目录 `../../四工厂独立化与MCP瘦身拆分开发方案-2026-06-23.md` 继续是本轮唯一 target-state 主方案，不下沉到本目录以免和 current-state 文档混写。

本目录当前已经拆成“现状文档”与“目标规范”两套口径。要看真实运行情况，请优先读：

1. [01-当前实际架构](01-当前实际架构.md)
2. [03-四工厂运行规范](03-四工厂运行规范.md)
3. [09-深度架构审查报告](09-深度架构审查报告.md)
4. [12-四工厂真实流程与MCP依赖审计](12-四工厂真实流程与MCP依赖审计.md)
5. [13-四工厂-MCP能力占用台账](13-四工厂-MCP能力占用台账.md)

要看目标态、标准化规格或未来迁移目标，请读 `strategy-factory-spec/README.md` 及其子文档。该目录不是现状证明。

本轮正式 target-state 主方案见根目录：

- [四工厂独立化与MCP瘦身拆分开发方案-2026-06-23](../../四工厂独立化与MCP瘦身拆分开发方案-2026-06-23.md)

## 强制性要求

**所有代码提交前必须通过**：
1. [CODE_REVIEW_CHECKLIST.md](CODE_REVIEW_CHECKLIST.md) 所有检查项
2. 规范符合性验证（手工或自动化）
3. 健康诊断工具确认修复效果

## 目标读者与强制要求

- **产品和业务负责人**：看 [07-四工厂目标与业务价值](07-四工厂目标与业务价值.md)，理解四工厂最终要形成什么策略生产线。所有需求必须先评估是否符合四工厂职责边界。
- **架构和后端工程**：看 `00` 到 `05`，**必须**按契约改代码，而不是追单个指标修补。所有代码修改必须通过 [10-规范符合性清单](10-规范符合性清单.md) 验证。
- **运维和验证人员**：看 [06-运行与诊断手册](06-运行与诊断手册.md) 和 [10-规范符合性清单](10-规范符合性清单.md)，判断系统是健康、待证据成熟、退化还是阻塞。禁止通过修改验证脚本参数来掩盖生产问题。
- **后续审查者**：看 [appendix/source-index](appendix/source-index.md)，从源码、Graphify、MCP 和公开资料复核本文档。发现规范与代码不一致时，必须提交规范符合性报告。

## 阅读顺序

### 核心规范（必读）

1. [00-术语与四工厂口径裁决](00-术语与四工厂口径裁决.md) - 四工厂定义和术语统一
2. [01-当前实际架构](01-当前实际架构.md) - 当前代码实际状态
3. [02-策略工厂全链路生命周期规范](02-策略工厂全链路生命周期规范.md) - **强制性**生命周期规范
4. [03-四工厂运行规范](03-四工厂运行规范.md) - 四工厂职责边界
5. [04-SignalTracker与证据闭环规范](04-SignalTracker与证据闭环规范.md) - SignalTracker 定位与证据链路
6. [05-技术治理与修复路线图](05-技术治理与修复路线图.md) - 当前治理约束、修复优先级与路线

### 运维与验收（必读）

7. [06-运行与诊断手册](06-运行与诊断手册.md) - **强制性**运维规范和诊断流程
8. [10-规范符合性清单](10-规范符合性清单.md) - 规范符合性验收清单
9. [11-禁止的修复方式与正确路径](11-禁止的修复方式与正确路径.md) - **强制性**禁止打补丁，正确修复根因
10. [12-四工厂真实流程与MCP依赖审计](12-四工厂真实流程与MCP依赖审计.md) - 当前真实流程链与跨包依赖口径
11. [13-四工厂-MCP能力占用台账](13-四工厂-MCP能力占用台账.md) - 当前能力占用清单和迁移参照

### 参考文档

12. [07-四工厂目标与业务价值](07-四工厂目标与业务价值.md) - 业务目标和价值定位
13. [08-外部原则与参考映射](08-外部原则与参考映射.md) - 外部原则、参考口径与映射边界
14. [09-深度架构审查报告](09-深度架构审查报告.md) - 架构审查结论
15. [appendix/source-index](appendix/source-index.md) - 源码索引和验证路径
16. [appendix/reports](appendix/reports/) - 历史运行报告与阶段性观测记录

### 快速导航

**我是新开发人员**：
1. 先读 [11-禁止的修复方式与正确路径](11-禁止的修复方式与正确路径.md)，了解哪些做法是被明确禁止的
2. 再读 [02-策略工厂全链路生命周期规范](02-策略工厂全链路生命周期规范.md)，理解统一状态机和证据链路
3. 最后读 [10-规范符合性清单](10-规范符合性清单.md)，确认代码修改符合规范

**我要修复问题**：
1. 先运行 `uv run python scripts/factories/diagnose_factory_health.py`，诊断根因
2. 查阅 [11-禁止的修复方式与正确路径](11-禁止的修复方式与正确路径.md)，确认修复方向
3. 按照 [02-策略工厂全链路生命周期规范](02-策略工厂全链路生命周期规范.md) 的验收标准实现修复
4. 用诊断工具验证修复效果

**我要进行运维操作**：
1. 直接查阅 [06-运行与诊断手册](06-运行与诊断手册.md)
2. 遵守手册中的强制性规范和禁止行为
3. 使用自动化诊断工具，不手工拼接 SQL
4. 补读 [03-四工厂运行规范](03-四工厂运行规范.md) 和 [04-SignalTracker与证据闭环规范](04-SignalTracker与证据闭环规范.md)，确认当前运行体和 sidecar 口径
5. 若需要看真实依赖链，再读 [12-四工厂真实流程与MCP依赖审计](12-四工厂真实流程与MCP依赖审计.md) 与 [13-四工厂-MCP能力占用台账](13-四工厂-MCP能力占用台账.md)

## 如何使用

遇到策略工厂问题时，不再从某个表层指标开始修。先把问题落到以下四层：

1. 控制面：哪个 supervisor、sidecar、quality session 或 Agent 入口触发了运行。
2. 生命周期：先看 `strategies.status`、`strategy_incubation_accounts.stage/status`、`pipeline_stage` 的物理状态，再派生策略停在 `generated`、`admitted_observe`、`paper_signalled`、`paper_ordered`、`paper_filled_open`、`round_trip_closed`、`audit_ready` 的哪一段。
3. 证据链：`strategy_signals`、`paper_orders`、`paper_trades`、`strategy_trade_positions`、`signal_forward_returns`、`strategy_signal_evidence`、execution audit 是否可串联。
4. 责任组件：应由 Strategy、Factor Mining、Incubation、Market Event Ingest、SignalTracker、Quant Core storage 还是 quality session 暴露失败。

只有当这四层都解释清楚后，才进入代码修改或数据修复。任何只让 `healthy=true`、只加 timeout、只在质量会话里补偿、只把 bootstrap 当晋级证据的修法，都不符合本规范。

## 文档边界

本文档说明架构、契约、运行规范和诊断方法，不直接修改策略工厂代码。策略生成、因子挖掘、孵化、paper 交易、execution audit 等行为仍由各自源码实现。

本文档不保存 `.env`、凭证值、数据库 dump、broker 状态或 live 交易凭证。任何运行命令都必须遵守 live trading guardrail：默认只允许诊断、paper、read-only 或明确受控的工厂流程。

## 当前未完成事项

- 统一生命周期账本仍需产品化为只读诊断入口，避免人工拼 Strategy run、SignalTracker、Incubation 和 SQLite 表。
- SignalTracker sidecar 虽然已有 phase-level timeout 和 partial result 结构，但仍需在生产控制面中成为显式依赖，而不是靠运维记忆单独启动。
- execution audit 与 hard gate 已坚持真实 paper closed round-trip，但样本债需要通过持续 paper execution 和 stale close policy 自然消化。
- `quality session` 仍应继续瘦身：它可以验证、采样和报告，不应成为生产证据补偿路径。
- `thinking` MCP 当前不可见；后续若可用，需要把独立审查结论纳入 `09`，并标注与代码事实是否一致。

## 维护规则

- 修改四工厂启动方式时，必须同步更新 `00`、`03` 和 `06`。
- 修改策略生命周期或证据表语义时，必须同步更新 `02`、`04` 和 `10`。
- 修改 Strategy Factory 与 AKShare MCP 的 provider 边界时，必须同步更新 `01` 和 `appendix/source-index`。
- 修改 quality session 行为时，必须说明它是验证工具还是生产控制面；默认只能是验证工具。
- 修改 hard gate、execution audit 或 bootstrap/backtest 行为时，必须明确 production hard gate 只认真实 paper closed round-trip。
