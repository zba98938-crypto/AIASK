# 产物摘要与维护说明

## 1. 本次回答的架构问题

> **AIASK 现在是什么、由哪些部分组成、关键业务如何流动、实际运行到什么程度、依赖边界是否成立？**

这是**当前态**问题，不是目标态设计，也不是代码评审。所有图与表都以 2026-10-20 的代码、配置、进程和 SQLite 为依据；推断与未知项在报告和证据索引中显式标注。

## 2. 产物清单

| 产物 | 视图/层级 | 回答的问题 | 主要受众 |
|---|---|---|---|
| `architecture-understanding.md` | 综合 | 项目是什么、能力/容器/流程/现状/风险 | 架构、后端、运维、产品 |
| `aiask-system.structurizr.dsl` | C4 L1/L2 | 系统边界、外部系统、容器划分 | 架构、技术负责人 |
| `capability-map.dot/.svg` | L0 | 六类业务能力及其依赖 | 产品、业务、架构 |
| `flow-strategy-factory-cycle.dot/.svg` | 业务流程 | 策略工厂单轮 11 阶段、门禁与漏斗 | 策略/工厂负责人、后端 |
| `flow-paper-lifecycle.dot/.svg` | 业务生命周期 | 孵化 → paper → 证据 → 晋级 | 策略/孵化负责人、运维 |
| `flow-data-ingestion.dot/.svg` | 数据血缘 | 数据源 → 同步/校验 → 存储 → 消费者 | 数据/后端、运维 |
| `flow-agent-tool-call.dot/.svg` | 技术流程 | Agent 请求、工具、数据、审计 | Agent/后端 |
| `deployment-topology.dot/.svg` | 运行时 | 进程、端口、存储、外部系统 | 运维、架构 |
| `dependency-map.dot/.svg` | 依赖 | 包依赖、动态装配、边界例外 | 架构、后端 |
| `evidence-index.md` | 证据 | 每条结论的代码/SQL/命令与置信度 | 复核者 |
| `README.md` | 导航 | 阅读顺序与再生成方式 | 所有人 |

## 3. 一页现状（数字为 2026-10-20 08:42–08:57）

- **代码**：8 个包、984 个 Python 文件、约 385,361 行；`akshare-mcp` 占 200,372 行，是最大的宿主/数据面。
- **运行（10:30 修复后）**：四运行时 supervisor 在跑（2026-10-18 16:02 起）；FactorScheduler sidecar（18:00）与 SignalTracker sidecar（18:30）均在跑；Agent HTTP 已在 `127.0.0.1:8765` 启动，`/health` 正常。
- **数据**：5,582 标的、1,738,883 日线、119,837 行财务、44 个因子 × 5,561 标的、0 data quality issues。
- **策略**：8,343 条策略（6,162 rejected / 1,665 submitted / 499 draft）；513+ 轮工厂运行，最新仍为 `success_observe_only`；Gate3 最新 15 进 0 过。
- **paper**：156 open（全部 submitted）/ 61 closed / 70 orphaned_exit；rejected 策略 open 已清零；已声明持有期清理处理 35 个超期持仓（3 个真实平仓、30 个历史 orphan）。
- **晋级**：0 promotion_reviews、0 listed；已有 9 个 `incubating`。execution audit gate 仍 BLOCKED：80 个策略已刷新 verdict，4 个 `needs_attention`（指标失败）、76 个 `pending_data`（样本不足），0 个 hard-gate 通过。
- **控制面**：四个 factory 的 status 均已可读（Strategy 用 `strategy_factory_runs`，其余用 supervisor heartbeat）；其他三个 factory 尚无历史 run ledger。
- **退出策略**：按用户裁决仅启用“已声明持有期”清理（`INCUBATION_FACTORY_STALE_PAPER_POSITION_CLOSURE_ENABLED=1`，batch 100、grace 0）；未声明持有期的策略仍走均线交叉信号退出。
- **测试**：quant-core 179 passed；strategy-factory 1071 passed / 3 skipped；akshare-mcp 1144 passed / 6 skipped。

## 4. 置信度与限制

| 类型 | 置信度 | 说明 |
|---|---|---|
| 静态结构（包、模块、import、契约） | high | 直接读代码/配置 |
| 运行时进程与端口 | high | `ps`、`lsof`、`curl` 实测 |
| 数据库规模与状态 | high | 只读 SQL 实测 |
| 业务流程序列 | medium–high | 代码路径 + 表结构；未做端到端 trace |
| 10-18 信号来源 | unknown | SignalTracker 未运行，来源未验证 |
| 平仓/orphaned_exit 根因 | unknown | 只读 DB，未回放退出规则 |
| 监控在线状态 | unknown | 只有 YAML，无进程证据 |
| 测试是否全绿 | unknown | 本次未运行测试套件 |

**不要把本目录当成“完成度证明”。** 它只说明当前实际状态；目标态、迁移计划和修复方案属于其他文档/后续工作。

## 5. 维护规则

1. **改代码后**：如果修改了包边界、MCP tool 数量、工厂阶段、端口、数据库表，先更新对应的 `.dot` / `.dsl` 源文件，再重新渲染 SVG；不要手改 SVG。
2. **改运行时后**：重新跑 `ps`、`lsof`、SQLite 查询，更新 `architecture-understanding.md` 的“当前事实”和 `evidence-index.md`。
3. **改文档口径后**：README 的“Known state”应标注测量时间；历史报告不要覆盖当前态文件。
4. **每次刷新**：在目录名或报告顶部写刷新日期；保持 `current-state-YYYY-MM-DD` 的命名。
5. **证据优先**：新增节点/边必须能指回代码、配置、SQL、进程或文档；推断边用虚线/点线并写验证任务。

## 6. 已知的“看起来像问题、但本次不展开”的点

- `ray` 未安装导致并行回测降级；`ParallelBacktestEngine` 导入失败。
- `backtest_results` 表 0 行，但回测阶段在跑；结果可能只在 run summary/artifacts。
- `success_observe_only` 不在 `run_envelope` 的 6 个状态里，但有显式映射（partial / SUCCEEDED）。
- `strategy_task_runs` 同时存在 `completed` 与 `success` 两种状态词表。
- 36 个 `orphaned_exit` 与近期退出规则诊断相关；本次未回放。
