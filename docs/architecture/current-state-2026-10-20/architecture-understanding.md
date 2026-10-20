# AIASK 当前态架构理解报告

> 生成时间：2026-10-20 08:55 +08:00  
> 范围：当前仓库（backend-only、单机、单用户）  
> 方法：`system-modeler`（当前态结构）+ `flow-visualizer`（关键流程）+ `dependency-impact-analyzer`（依赖边界）+ `c4model` / `graphviz`（视图）  
> 证据等级：**high** = 代码/配置/数据库/进程直接证实；**medium** = 多个部分信号一致；**low** = 目录名或命名推断；**unknown** = 明确未验证

---

## 0. 一页结论

**AIASK 是一个 A 股量化研究后端，不是一个面向固定用户行为的应用。** 它的交付面是 MCP servers + skills + Agent HTTP API；没有前端，没有多租户，实盘默认关闭，当前也没有常驻的 Agent 进程。

当前最重要的五个判断：

1. **代码架构是清晰的分层单体（modular monorepo），运行时却是一个单机进程簇。** 8 个 Python 包、约 38.5 万行代码，共用一份 4 GB SQLite；四工厂由 `scripts/factories/run_three_factories.py`（历史命名，实际启动四个运行体）监督，当前确实在运行。
2. **策略工厂是主生产线，但目前事实上是“观察优先”空转。** 513 轮运行中 492 轮是 `success_observe_only`；最新一轮候选 29、Gate3 输入 15、通过 0、提交 3（其中 12 条仅审计）；`promotion_reviews = 0`，`listed = 0`。
3. **Agent 边缘没有在线。** AIASK Agent 本身未启动；默认端口 8765 在 08:51–08:57 曾被另一个项目的 `python -m http.server` 占用（PID 90009，cwd 在 `/Users/mac/Desktop/FDE/...`），08:59 复查已释放。`~/.aiask/run/agent.meta.json` 还指向已移除的 Hermes tree 和已删除的 venv。
4. **SignalTracker sidecar 缺席。** 没有进程、没有 `signal_tracker.status.json`；但数据库里仍有到 2026-10-18 的信号和 27,690 条前向收益。信号来源需要单独验证，不能把“有信号”当成“sidecar 在跑”。
5. **只读控制面和真实运行账本是两套账。** `strategy_factory.mcp_server` 的 5 个只读工具查询 `factory_run_ledger`，该表 **0 行**；真实运行写入 `strategy_factory_runs`（513 行）和 `strategy_factory_dispatches`（492 行）。因此控制面可能显示 `never_run`，而生产线每 5 分钟在跑。

**最大的业务风险不是“代码跑不起来”，而是“生产线在运转、证据链却没有闭合”：** 1,642 个 paper 账户只对应 155 笔成交、1 笔平仓；118 个 open 持仓、36 个 `orphaned_exit`；晋级评审 0 次。系统在生成和观察，但还没有证明任何策略可以晋级。

---

## 0.1 修复后更新（2026-10-20 09:20）

上面的 0 节以及第 5–9 节的运行数字是 08:55 的“修复前”快照；以下记录本次会话已经落地的改动，避免把历史快照当成当前状态。第 5–9 节仍保留原始测量，用于对照。

| 修复 | 结果 | 验证 |
|---|---|---|
| rejected 策略持仓清理 | 14 个策略、55 笔卖单全部成交；rejected open 从 61 → **0**；后续 combined 清理后当前 **156 open（全部 submitted）/ 61 closed / 70 orphaned_exit** | `tests/test_rejected_strategy_position_cleanup.py`；真实库清理脚本 |
| 价格路径审计窗口 | 未平仓持仓不再把开仓时间当结束时间；57 个 open 持仓已刷新，35 个 `audited_open_position`、22 个 `missing_kline`（新开仓无后续 bar） | `tests/test_trade_position_price_path.py` |
| 控制面账本 | 四个 factory 的 status 均可读；Strategy 读 `strategy_factory_runs`，其余读 supervisor heartbeat；Strategy 最新 run 可读 | `tests/test_factory_control_strategy_runs.py`；真实库查询 |
| SignalTracker | 已作为 sidecar daemon 运行（18:30），写 `~/.aiask/run/signal_tracker.status.json`；一次运行生成 474 条信号、0 错误 | `tests/test_signal_tracker_open_positions.py`；真实运行 |
| FactorScheduler | 已作为独立 sidecar 运行（18:00），每日刷新 `factor_values` / `factor_ic_history`；当前最新因子日期仍为 2026-10-16，下一次 18:00 会刷新 | `scripts/factories/run_factor_scheduler.py`；进程与日志 |
| 持仓策略覆盖 | SignalTracker 显式并入有 open 持仓的策略（本次 22 个），不再被 200 条加载上限漏掉 | 同上；`runtime_universe.held_open_positions=22` |
| Agent HTTP | 已在 `127.0.0.1:8765` 启动，`/health` 返回 `{"status":"ok"}`，meta/pid 已修正为 conda 解释器 | `curl /health`、`curl /v1/tools` |
| Sidecar supervisor | `scripts/ops/run_sidecars.py` 统一管理 FactorScheduler / SignalTracker / Agent，退出后 30s 重启，写 `~/.aiask/run/sidecars.status.json` | 进程与 status 文件；诊断已纳入 |
| 已声明持有期解析 | 补充识别 `params.hold_days` 与 `mapping_search.hold`；156/156 个持仓策略都能解析出持有期 | `tests/test_incubation_factory_horizon.py` |
| 已声明持有期退出 | 清理 35 个超期持仓：3 个真实平仓（`combo_vmr`）、30 个历史 orphan（`lsmap_*`）；当前无超期 open | `tests/test_stale_paper_position_cleanup.py`；真实库清理脚本 |
| Execution audit acceptance | 接入 daemon Phase 7；本次对 170 个候选评估 80 个：4 个 `needs_attention`、76 个 `pending_data`、0 个 hard-gate 通过、20 个 closed round-trip | `tests/test_incubation_orchestrator_cleanup.py`；真实库运行 |
| 可选 Ray 依赖 | 缺少 Ray 时静默降级为本地回测，不再打印误导性 ImportError 告警 | `akshare_mcp.tools.backtest` 导入验证 |
| 测试 | quant-core **179 passed**；strategy-factory **1071 passed, 3 skipped**；akshare-mcp **1144 passed, 6 skipped** | 对应包根目录 `pytest -q` |

**仍未解决（需要业务裁决，不是代码缺陷）：**

- 退出策略已裁决为**仅启用已声明持有期**（2026-10-20），并已处理 35 个超期/残留持仓；当前 156 个 open 都没有超期，未来会在各自声明的持有期到达时清理。仍未平仓的 live 信号退出仍取决于均线交叉；这是策略行为，不是未决的代码问题。
- `execution audit gate` 仍 BLOCKED：本次 audit acceptance 的 80 个策略中 4 个指标失败、76 个样本不足（`real_paper_round_trip_count=20`，`bootstrap_round_trip_count=0`）。这不是代码缺陷，是策略质量与样本债务；需要更好的策略或更长的 paper 证据，不能通过放宽门禁解决。当前已有 **9 个 incubating**，说明 Gate3/submission 路径在推进。
- 健康诊断现在检查 7 个工厂/sidecar/Agent 进程（`ps` 回退，无需 psutil）、Agent `/health`、只看买入信号的订单 backlog，并正确读取 `effective_n_5d`；当前 **9 项检查：7 通过、1 警告（9 incubating 尚无 listed）、1 阻塞（audit gate）**。
- `execution audit gate` 仍 `BLOCKED`：诊断显示 7,789 个策略缺少 fresh verdict、17 个从未审计、20 个审计早于最新成交。要解除阻塞，需要 closed round-trip 证据，而不是再生成策略。
- 其余三个 factory 的只读控制面仍读空的 `factory_run_ledger`；Strategy Factory 已改为读真实表，另外三个尚未接线。
- Prometheus/OTel 只有配置，没有在线进程证据；`psutil` 未安装，诊断脚本的进程检查会跳过。

---

## 1. 项目是什么 / 不是什么

### 1.1 是什么

| 维度 | 事实 | 证据 |
|---|---|---|
| 产品形态 | MCP servers + skills + Agent HTTP API | [README.md:3-21](file:///Users/mac/Desktop/张勇/AIASK/README.md#L3-L21) |
| 代码规模 | 8 个包、984 个 Python 文件、约 385,361 行 | 本次统计（`find` + `wc`） |
| 主数据面 | A 股行情、财务、资金流、公告/新闻、因子、策略、paper 交易 | `packages/akshare-mcp/src`、`packages/aiask-quant-core/src` |
| 主控制面 | 四工厂 supervisor + 只读 MCP 控制面 + Agent 工具门面 | `scripts/factories/`、`packages/strategy-factory/src` |
| 主交付面 | 179 个 AKShare MCP tools（full）/ 144（tool-only）、5 个工厂只读 tools、Agent `agent_*` 门面 | 本次运行时枚举 + [mcp-servers-recommended.json](file:///Users/mac/Desktop/张勇/AIASK/.claude/mcp-servers-recommended.json) |
| 运行环境 | conda `aiask`（Python 3.13），单机 macOS arm64 | [CLAUDE.md:3-23](file:///Users/mac/Desktop/张勇/AIASK/CLAUDE.md#L3-L23) |

### 1.2 不是什么

- **不是前端产品。** 本树没有 `desktop/`，没有 `/v1/desktop/*` facade；客户端直接用 Agent HTTP / MCP。见 [CURRENT_PRODUCT_TRUTH.md:129-142](file:///Users/mac/Desktop/张勇/AIASK/docs/CURRENT_PRODUCT_TRUTH.md#L129-L142)。
- **不是多租户系统。** `user_id` 仍有 547 处引用，但运行库只有 2 个 app_users、1 个 users、0 个 strategy_subscriptions；默认身份是字面量 `"local"`。见 [CLAUDE.md:29-42](file:///Users/mac/Desktop/张勇/AIASK/CLAUDE.md#L29-L42)。
- **不是实盘交易产品。** live trading 默认关闭；同花顺/QMT 下单路径需要 token + 确认，参考 MCP 清单里四个券商 server 都是 `disabled`。
- **不是 24h 常驻产品。** 当前四个运行时在跑，但没有 systemd/launchd/cron 级 supervisor；四运行时 supervisor 本身也是手工启动的进程。

---

## 2. 业务能力地图（L0）

图谱：[capability-map.svg](file:///Users/mac/Desktop/张勇/AIASK/docs/architecture/current-state-2026-10-20/capability-map.svg) · 源文件：[capability-map.dot](file:///Users/mac/Desktop/张勇/AIASK/docs/architecture/current-state-2026-10-20/capability-map.dot)

| 能力域 | 具体能力 | 当前证据 |
|---|---|---|
| 市场数据与治理 | 行情/K 线、财务/估值、资金流/龙虎榜/融资融券、公告/新闻/研报、行业/板块/主题图谱、同步与完整性 | `data_source/`、`services/data_sync.py`、`docs/DATA_SOURCES.md`；DB：5,582 标的、1,738,883 根日线、119,837 行财务、933 条归一化事件 |
| 因子与研究 | 因子计算、因子挖掘（5 引擎）、walk-forward / purged K-fold / bootstrap 验证、因子池/衰减/QC | `factor_calculator/`、`services/factor_mining_factory/`、`aiask_quant_core/validation.py`；DB：44 个因子、5,561 标的、29 个 active factor |
| 策略生产 | 事件摄取、策略生成、Gate 1–3、回测、去重、提交、淘汰、观察优先 | `application/cycle_pipeline.py`、`application/services/candidate_pipeline.py`、`application/submission_gate/`；DB：8,233 条策略、513 轮运行 |
| 模拟交易与孵化 | intake、warmup/paper 账户、信号、订单、撮合、成交、持仓、NAV、指标、前向收益、执行审计、晋级评审 | `services/matching_engine.py`、`services/incubation_pipeline.py`、`services/promotion_pipeline.py`；DB：1,659 个账户、119 订单、155 成交、155 持仓、7,707 审计快照 |
| Agent 与交互 | HTTP/CLI、`agent_*` 工具、MCP 交付、Skills、IM gateway、ActionIntent/审批 | `packages/agent/src/aiask_agent/`；137 条静态 catalog 目录项；19 个仓库 skills + 20 个 bundle |
| 运维与控制面 | 四运行时 supervisor、readiness/诊断脚本、Prometheus/OTel 配置、只读工厂控制面 | `scripts/factories/`、`scripts/aiask_readiness.py`、`monitoring/`、`strategy_factory/mcp_control.py` |

**能力边界判断（Business Application Fitness）：**

- **业务模型清晰度：强。** 策略、孵化账户、paper 订单/成交/持仓、晋级评审、执行审计都是有表、有状态、有代码路径的业务对象。
- **数据/状态理解：中强。** 表结构完整，生命周期有 `IncubationStage`（diagnostic/warmup/paper/candidate/listed/failed/retired）；但运行库实际只出现 `paper` 与 `warmup`，且 `strategy_signal_evidence` 只有 2 行，证据链不完整。
- **端到端路径：部分证实。** 工厂单轮与 Agent 工具调用可以追到代码；paper 生命周期可以追到表；**晋级路径没有确认**（0 reviews）。
- **Agent 指令覆盖：规则强、流程弱。** [CLAUDE.md](file:///Users/mac/Desktop/张勇/AIASK/CLAUDE.md) 对数据诚实、单位/口径、测试写法的约束非常具体；但它没有用一段话完整描述“策略从生成到晋级”的业务流程和回归路径。
- **验证准备度：包级强、业务级弱。** 根 `pytest.ini` 覆盖 8 个包，CI 每个包都是阻塞门禁；但晋级、平仓、SignalTracker 缺失这类跨包业务路径没有对应的 E2E 门禁（本次未运行测试套件）。

---

## 3. 系统上下文（L1）

C4 源文件：[aiask-system.structurizr.dsl](file:///Users/mac/Desktop/张勇/AIASK/docs/architecture/current-state-2026-10-20/aiask-system.structurizr.dsl)（用 Qoder 的 Structurizr DSL viewer 打开）。

| 角色/外部系统 | 关系 | 证据 |
|---|---|---|
| 量化研究员 / 运维 | 启停四工厂、跑研究任务、看诊断 | 四运行时 supervisor、`scripts/factories/` |
| HTTP / CLI 客户端 | 调用 Agent HTTP API | [server.py:255-293](file:///Users/mac/Desktop/张勇/AIASK/packages/agent/src/aiask_agent/server.py#L255-L293)、[cli.py](file:///Users/mac/Desktop/张勇/AIASK/packages/agent/src/aiask_agent/cli.py) |
| MCP 客户端（Cursor/Claude/Hermes） | stdio 连接 AKShare MCP 与工厂只读控制面 | [mcp-servers-recommended.json](file:///Users/mac/Desktop/张勇/AIASK/.claude/mcp-servers-recommended.json) |
| IM 平台（可选） | Discord/Slack/Telegram/Matrix gateway | [gateway/adapters.py:810-827](file:///Users/mac/Desktop/张勇/AIASK/packages/agent/src/aiask_agent/gateway/adapters.py#L810-L827) |
| 行情/财务数据源 | Eastmoney、Sina、Tencent、SSE、TDX/TQCenter；Tushare 无 token | [DATA_SOURCES.md:1-80](file:///Users/mac/Desktop/张勇/AIASK/docs/DATA_SOURCES.md#L1-L80) |
| LLM 提供方 | 策略生成、评审、Agent 推理 | `model_client.py`、`services/strategy_llm_provider.py` |
| 券商交易端 | 同花顺/QMT；默认关闭 | `packages/finance-mcp-servers/`、MCP 清单 `disabled` |

**信任/安全边界：**

- Agent HTTP 默认只绑定 loopback；非 loopback 需要 Bearer token；控制型操作还需要 control token。见 [env.example](file:///Users/mac/Desktop/张勇/AIASK/env.example)。
- 模型可见工具只允许 `agent_*`；直接 manager 名称被 `ToolPolicyEngine` 拒绝。见 [tools/policy.py:8-16](file:///Users/mac/Desktop/张勇/AIASK/packages/agent/src/aiask_agent/tools/policy.py#L8-L16)。
- 状态型操作必须经过 `ActionIntent`/`Approval`；确认后才重放。见 [adapters/confirmed_actions.py](file:///Users/mac/Desktop/张勇/AIASK/packages/agent/src/aiask_agent/adapters/confirmed_actions.py)、`docs/architecture/tool-call-path-classification.json`。
- 实盘默认关闭；MCP server 的 HTTP transport 还要求 loopback + Origin + auth。见 [server.py:433-474](file:///Users/mac/Desktop/张勇/AIASK/packages/akshare-mcp/src/akshare_mcp/server.py#L433-L474)。

---

## 4. 容器与模块（L2/L3）

| 容器/包 | 职责 | 关键模块 | 运行形态 | 代码量 |
|---|---|---|---|---|
| `aiask-agent` | 边缘 Agent：HTTP/CLI、会话、工具、审批、gateway | `server.py`、`runtime.py`、`tool_registry.py`、`mcp_client.py`、`routes/`（27 个模块）、`gateway/` | 可选 HTTP 进程（当前未运行） | 155 py / 48,131 LOC |
| `akshare-mcp` | MCP host、数据源、managers、services、SQLite 实现 | `server.py`、`tools/`、`services/`、`data_source/`、`storage/` | stdio MCP（按需） | 507 py / 200,372 LOC |
| `strategy-factory` | 四工厂编排、门禁、契约、runtime api、只读 MCP 控制面 | `application/cycle_pipeline.py`、`application/submission_gate/`、`runtime/`、`api/`、`mcp_server.py` | 库 + 5 个只读 MCP tools | 183 py / 95,035 LOC |
| `aiask-quant-core` | 回测、因子、验证、风险模型、SQLite schema/存储 | `backtest/`、`factor_calculator/`、`validation.py`、`risk_model.py`、`storage/sqlite/` | 库 | 120 py / 39,355 LOC |
| `aiask-finance-mcp-servers` | 通达信、同花顺、东方财富、QMT | 四个 `*/server.py` | stdio MCP（默认 disabled） | 12 py / 1,892 LOC |
| `aiask-data-mcp` | 中性数据入口，动态转发到 `akshare_mcp` | `server.py`、`__init__.py` | facade + MCP 入口 | 2 py / 172 LOC |
| `aiask-skills` | 打包 Skills bundle | `bundle/` | 静态资源 | 3 py / 89 LOC |
| `aiask-agent-reference` | 最小 HostAdapter 实现 | `host.py` | 参考/契约夹具 | 2 py / 315 LOC |

### 4.1 Agent 内部（L3）

- **路由层**：27 个 route 模块，覆盖 responses/chat、finance、research、workbench、tools、mcp、gateway、approvals、jobs、learning_rl、webhooks、ws 等。入口在 [app_route_assembly.py:132-490](file:///Users/mac/Desktop/张勇/AIASK/packages/agent/src/aiask_agent/app_route_assembly.py#L132-L490)。
- **运行循环**：`AgentRuntime.run()` 做 session/run 记录、上下文压缩、planner、模型调用、工具调用、guardrail、证据抽取、learning loop。见 [runtime.py:243-999](file:///Users/mac/Desktop/张勇/AIASK/packages/agent/src/aiask_agent/runtime.py#L243-L999)。
- **工具层**：`AgentToolRegistry` 只接受 `agent_*` 名称；每个工具带 category / side_effect 元数据；`finance_safe` 只允许金融读/状态型/mcp_financial。见 [tool_registry.py:231-263](file:///Users/mac/Desktop/张勇/AIASK/packages/agent/src/aiask_agent/tool_registry.py#L231-L263)、[tools/policy.py:18-66](file:///Users/mac/Desktop/张勇/AIASK/packages/agent/src/aiask_agent/tools/policy.py#L18-L66)。
- **数据适配**：金融工具通过 `adapters/akshare.py` **在进程内 import 并调用** `akshare_mcp.tools.*`，不是走 MCP；量化工具有独立 `adapters/quant.py`；工厂视图走 `adapters/strategy_factory.py`。
- **外部 MCP**：`MCPAggregator` 支持 stdio/HTTP，读取 `~/.aiask/mcp_servers.json`；这是“通用工具”的接入面，与金融工具的进程内路径不同。见 [mcp_client.py:242](file:///Users/mac/Desktop/张勇/AIASK/packages/agent/src/aiask_agent/mcp_client.py#L242)。

### 4.2 Strategy Factory 内部（L3）

- **契约**：`FactoryCyclePipeline` 定义了 11 个阶段的顺序（warmup → collect → factor_research → readiness → spawn → autonomy → quality_gate → backtest → deduplicate → submit → elimination）。见 [cycle_pipeline.py:18-31](file:///Users/mac/Desktop/张勇/AIASK/packages/strategy-factory/src/strategy_factory/application/cycle_pipeline.py#L18-L31)。
- **运行时**：Factor Mining、Incubation、SignalTracker、Market Event Ingest 都已有 `strategy_factory.runtime.*` canonical 入口；具体实现通过 provider 注入。见 [runtime/](file:///Users/mac/Desktop/张勇/AIASK/packages/strategy-factory/src/strategy_factory/runtime/)。
- **宿主解耦**：默认 provider configurator 是 `akshare_mcp.adapters.strategy_factory_runtime:configure_strategy_factory_runtime_services`，需要 19 个 provider；standalone 是兜底。见 [default_bootstrap.py:24-68](file:///Users/mac/Desktop/张勇/AIASK/packages/strategy-factory/src/strategy_factory/runtime/default_bootstrap.py#L24-L68)。
- **只读控制面**：`strategy_factory.mcp_server` 暴露 5 个 tools + 2 个 resources；底层 `mcp_control.py` 查询 `factory_run_ledger`。见 [mcp_server.py:38-80](file:///Users/mac/Desktop/张勇/AIASK/packages/strategy-factory/src/strategy_factory/mcp_server.py#L38-L80)、[mcp_control.py:105-197](file:///Users/mac/Desktop/张勇/AIASK/packages/strategy-factory/src/strategy_factory/mcp_control.py#L105-L197)。

### 4.3 Quant Core 内部（L3）

- **回测**：`BacktestEngine`、`AdvancedBacktestEngine`、DSL 策略、内置策略、并行引擎（Ray 可选）。见 [backtest/engine.py:195](file:///Users/mac/Desktop/张勇/AIASK/packages/aiask-quant-core/src/aiask_quant_core/backtest/engine.py#L195)。
- **验证**：`WalkForwardValidator`、`PurgedKFoldCV`、`FactorValidationPipeline`。见 [validation.py:84-368](file:///Users/mac/Desktop/张勇/AIASK/packages/aiask-quant-core/src/aiask_quant_core/validation.py#L84-L368)。
- **风险**：`RiskModel` 使用收缩协方差。见 [risk_model.py:23](file:///Users/mac/Desktop/张勇/AIASK/packages/aiask-quant-core/src/aiask_quant_core/risk_model.py#L23)。
- **存储**：`storage/sqlite/` 负责市场、策略、因子、向量、审计表；factory 域 schema 已迁到 strategy-factory，但保留兼容 re-export。见 [schema_strategy.py:38-52](file:///Users/mac/Desktop/张勇/AIASK/packages/aiask-quant-core/src/aiask_quant_core/storage/sqlite/schema_strategy.py#L38-L52)。

---

## 5. 运行时与部署

图谱：[deployment-topology.svg](file:///Users/mac/Desktop/张勇/AIASK/docs/architecture/current-state-2026-10-20/deployment-topology.svg) · 源文件：[deployment-topology.dot](file:///Users/mac/Desktop/张勇/AIASK/docs/architecture/current-state-2026-10-20/deployment-topology.dot)

### 5.1 当前进程（2026-10-20 08:45–08:57 实测）

| 进程 | PID | 启动/ cadence | 作用 | 证据 |
|---|---:|---|---|---|
| 四运行时 supervisor | 1194 | 2026-10-18 16:02 启动 | 监督四个 factory 子进程 | `ps`、`supervisor_startup.json` |
| Run Strategy Factory | 4755 | 每 300s 一轮 | 策略工厂 dispatch 循环 | `ps`、DB 每 5 分钟一条 run |
| Factor Mining Factory | 1196 | 18:30 挖掘 + 06:00 维护 | 因子挖掘/池维护 | `factor_mining_factory.log` |
| Incubation Factory | 1197 | 每日 18:30 | 孵化/paper 结算 | `incubation_factory.log` |
| Market Event Ingest | 1198 | 每 3600s | 公告/事件摄取与桥接 | `market_event_ingest.log` |
| SignalTracker sidecar | — | 未运行 | 证据闭环 | 无进程、无 `signal_tracker.status.json` |
| AIASK Agent HTTP | — | 未运行 | Agent 对话/工具 API | 08:51–08:57 曾被他项目 `python -m http.server` 占用（PID 90009，cwd `/Users/mac/Desktop/FDE/...`）；08:59 已释放 |

### 5.2 持久化

- 主库：`~/.aiask/data/db/akshare_mcp.sqlite3`，4.0 GB + 230 MB WAL；市场、工厂、孵化、审计、会话表都在这里。
- 任务板：`~/.aiask/data/db/strategy_factory_task_board.sqlite3`，8.6 MB。
- 日志：`~/.aiask/logs/four_runtime_supervisor/`（strategy_factory.log、factor_mining_factory.log、incubation_factory.log、market_event_ingest.log、execution_records.jsonl）。
- Agent 运行元数据：`~/.aiask/run/agent.meta.json`，当前指向已移除的 `/Users/mac/Desktop/Hermes agent/integrations/aiask` 与已删除的 `~/.aiask/venv`。

### 5.3 接口与端口

- **Agent HTTP**：默认 `127.0.0.1:8765`；当前未由 AIASK 监听。
- **MCP**：默认 stdio，不占常驻端口；参考清单中 `aiask-akshare` 使用 `tool-only` profile，`aiask-strategy-factory` 只读；四个券商 server `disabled`。
- **监控**：`monitoring/` 有 Prometheus/Alertmanager/OTel 配置，但当前没有看到对应常驻进程；这是“配置存在”，不是“监控在线”。

---

## 6. 关键流程

### 6.1 Agent 请求与工具调用

图谱：[flow-agent-tool-call.svg](file:///Users/mac/Desktop/张勇/AIASK/docs/architecture/current-state-2026-10-20/flow-agent-tool-call.svg) · 源文件：[flow-agent-tool-call.dot](file:///Users/mac/Desktop/张勇/AIASK/docs/architecture/current-state-2026-10-20/flow-agent-tool-call.dot)

**主要路径（代码证实）：**

1. 客户端调用 `/v1/responses`、`/v1/chat/completions`、`/v1/finance`、`/v1/research` 等路由。路由装配在 [app_route_assembly.py](file:///Users/mac/Desktop/张勇/AIASK/packages/agent/src/aiask_agent/app_route_assembly.py#L132-L490)。
2. `AgentRuntime.run()` 创建/恢复 session，记录 run events，做上下文引用、压缩与 planner。见 [runtime.py:243-399](file:///Users/mac/Desktop/张勇/AIASK/packages/agent/src/aiask_agent/runtime.py#L243-L399)。
3. 模型返回 `tool_calls`；`ToolPolicyEngine` 按 category 过滤；`AgentToolRegistry` 只执行 `agent_*` 工具。见 [runtime.py:581-717](file:///Users/mac/Desktop/张勇/AIASK/packages/agent/src/aiask_agent/runtime.py#L581-L717)。
4. 金融工具经 `adapters/akshare.py` **进程内**调用 `akshare_mcp.tools.*`；量化工具经 `adapters/quant.py`；工厂视图经 `adapters/strategy_factory.py` 读取 DB facade。
5. 状态型工具（terminal/process/code/browser/plugin）不直接执行，先创建 `ActionIntent`；确认后由 `adapters/confirmed_actions.py` 重放。
6. 每次工具调用写 `tool_invocations`、抽取 sources/artifacts、写 run events；最终 `save_response` + `update_run`。见 [runtime.py:771-999](file:///Users/mac/Desktop/张勇/AIASK/packages/agent/src/aiask_agent/runtime.py#L771-L999)。

**当前状态：** 这条路径是代码证实的，不是在线流量。Agent HTTP 未运行；8765 曾在 08:51–08:57 被他项目静态 server 占用，08:59 已释放；`agent.meta.json` 过期。启动 Agent 前仍需确认端口可用并解决运行环境（venv）问题。

### 6.2 策略工厂单轮生产

图谱：[flow-strategy-factory-cycle.svg](file:///Users/mac/Desktop/张勇/AIASK/docs/architecture/current-state-2026-10-20/flow-strategy-factory-cycle.svg) · 源文件：[flow-strategy-factory-cycle.dot](file:///Users/mac/Desktop/张勇/AIASK/docs/architecture/current-state-2026-10-20/flow-strategy-factory-cycle.dot)

**阶段顺序（代码证实）：** `warmup → collect → factor_research → readiness → spawn → autonomy → quality_gate → backtest → deduplicate → submit → elimination`。见 [cycle_pipeline.py:18-31](file:///Users/mac/Desktop/张勇/AIASK/packages/strategy-factory/src/strategy_factory/application/cycle_pipeline.py#L18-L31)。

**每轮实际发生什么：**

- `run_strategy_factory.py` 每 300s 触发一次 dispatch run；默认 `--dispatch-default-universe-limit 200`。见 [run_strategy_factory.py:34](file:///Users/mac/Desktop/张勇/AIASK/scripts/factories/run_strategy_factory.py#L34)、[run_strategy_factory.py:109-131](file:///Users/mac/Desktop/张勇/AIASK/scripts/factories/run_strategy_factory.py#L109-L131)。
- `Runtime Boundary` 先校验 repository 与 runtime adapters；失败则整轮 `runtime_boundary_failed`。见 [cycle_pipeline.py:67-89](file:///Users/mac/Desktop/张勇/AIASK/packages/strategy-factory/src/strategy_factory/application/cycle_pipeline.py#L67-L89)。
- 通过后进入 11 阶段；候选生成、自主任务、Gate3、回测、去重、提交/淘汰。
- 运行结果写入 `strategy_factory_runs` / `strategy_factory_run_artifacts` / `strategy_factory_dispatches`；**不写** `factory_run_ledger`。

**最新一轮（2026-10-20 08:41，77.1s）：** 候选 29、自主生成 21、Gate3 输入 15、通过 0、失败 15、提交 3（audit-only 12）、状态 `success_observe_only`。Gate3 高频失败原因（来自 scheduler state）：`missing_parameter_perturbation_trade_stability`、`insufficient_statistical_evidence`、`missing_param_sensitivity`、`supplemental_statistical_gate_failed`。

**判断：** 这不是“工厂没跑”，而是“工厂在跑、门禁在拒绝”。在当前参数与数据条件下，继续提高生成频率不会改变结果；应先诊断 Gate3 的缺失项是数据缺口还是策略质量不足。

### 6.3 孵化与 paper 生命周期

图谱：[flow-paper-lifecycle.svg](file:///Users/mac/Desktop/张勇/AIASK/docs/architecture/current-state-2026-10-20/flow-paper-lifecycle.svg) · 源文件：[flow-paper-lifecycle.dot](file:///Users/mac/Desktop/张勇/AIASK/docs/architecture/current-state-2026-10-20/flow-paper-lifecycle.dot)

**业务路径：** 已提交策略 → incubation intake → warmup → paper → 信号 → paper 订单 → MatchingEngine → 成交 → 持仓 → 退出规则/NAV → 指标/前向收益/执行审计 → 晋级 hard gate → candidate/listed。

**物理阶段：** `diagnostic / warmup / paper / candidate / listed / failed / retired`。见 [incubation_stages.py:1-49](file:///Users/mac/Desktop/张勇/AIASK/packages/aiask-quant-core/src/aiask_quant_core/incubation_stages.py#L1-L49)。

**当前数字：**

- 1,659 个孵化账户：1,642 `paper/active` + 17 `warmup/active`。
- 7,288 条信号、122 个交易日，最新 2026-10-18。
- 119 个 paper 订单、155 笔成交、155 个持仓：118 open / 1 closed / 36 `orphaned_exit`。
- 1,397 个 NAV、1,383 条孵化指标、27,690 条前向收益、7,707 份执行审计、7,738 份闭包快照。
- `strategy_signal_evidence` 只有 2 行；`promotion_reviews = 0`；`listed = 0`。

**判断：** 系统有“账户、成交、指标”，但缺少“闭合的持仓周期 + 完整的信号证据 + 晋级评审”。`orphaned_exit` 36 个是明显的证据缺口；只有 1 笔平仓，无法支撑 hard gate 的 round-trip 要求。近期提交（`42a1965`、`dd16949`）已经把退出规则诊断为“交叉信号符号”而非价格路径，但本次没有运行对应回放/测试。

### 6.4 数据摄取

图谱：[flow-data-ingestion.svg](file:///Users/mac/Desktop/张勇/AIASK/docs/architecture/current-state-2026-10-20/flow-data-ingestion.svg) · 源文件：[flow-data-ingestion.dot](file:///Users/mac/Desktop/张勇/AIASK/docs/architecture/current-state-2026-10-20/flow-data-ingestion.dot)

**路径：** provider 客户端 → `data_source/` → `services/data_sync*` → SQLite → `data_quality_issues` / completeness / freshness → tools/Agent。

- 数据源模块：`eastmoney_datacenter.py`、`eastmoney_listing.py`、`eastmoney_margin_detail.py`、`eastmoney_mutual.py`、`sina_financials.py`、`sina_listing.py`、`sse_margin.py`、`tdx_local.py`、`tdx_tqcenter/`、`market_data.py`、`quotes.py`、`volume_units.py`。
- 数据台账：[DATA_SOURCES.md](file:///Users/mac/Desktop/张勇/AIASK/docs/DATA_SOURCES.md) 记录了本机代理对 `push2.eastmoney.com` 的阻断、`datacenter-web` 的可用性、Tushare 无 token、备用源单位/复权口径不一致等约束。
- 当前数据：5,582 标的、1,738,883 根日线（最新 2026-10-19）、119,837 行财务（最新报告期 2026-09-30）、44 个因子 × 5,561 标的（最新 2026-10-16）、`data_quality_issues = 0`。
- **注意：** 因子日期（10-16）落后于行情（10-19）；这是正常的计算滞后，但下游按交易日 join 时必须显式处理，不能把最新行情和旧因子静默拼接。

### 6.5 四运行时 supervisor

- 当前 supervisor 只负责四个运行体的进程生命周期：Strategy、Factor Mining、Incubation、Market Event Ingest；**SignalTracker 是 sidecar，不在其中**。见 [run_three_factories.py:1-28](file:///Users/mac/Desktop/张勇/AIASK/scripts/factories/run_three_factories.py#L1-L28)。
- 当前启动参数来自 `~/.aiask/logs/four_runtime_supervisor/supervisor_startup.json`；四个子进程命令与日志路径都在里面。
- 它**不**拥有业务状态机、证据表或 hard gate；这些在 strategy-factory / quant-core。
- 当前四运行时都在跑，但 SignalTracker 缺席；这意味着“四个 factory 的生产循环”与“证据闭环”不是同一个监督面。

---

## 7. 数据与状态

### 7.1 关键表分组

| 域 | 代表表 | 规模/状态 |
|---|---|---|
| 市场行情 | `stocks`、`kline_1d`、`kline_intraday`、`stock_quotes` | 5,582 标的；1,738,883 日线；最新 2026-10-19 |
| 财务/估值 | `financials`、`valuation` 相关 | 119,837 行；报告期 2026-09-30 |
| 资金流/事件 | `stock_fund_flow`、`margin_*`、`dragon_tiger`、`market_events_normalized` | 933 条归一化事件；1,033 条 event signals；274 个 event clusters |
| 因子 | `factor_values`、`factor_ic_history`、`factor_pool_active` | 386,706 行；44 个因子；5,561 标的；29 个 active |
| 策略 | `strategies`、`strategy_factory_runs`、`strategy_quality_reports`、`strategy_promotion_reviews` | 8,233 条策略；513 轮运行；7,718 份质量报告；0 份晋级评审 |
| 孵化/paper | `strategy_incubation_accounts`、`paper_accounts`、`paper_orders`、`paper_trades`、`strategy_trade_positions`、`paper_nav` | 1,659 账户；119 订单；155 成交；155 持仓；1,397 NAV |
| 证据/审计 | `strategy_signals`、`signal_forward_returns`、`strategy_signal_evidence`、`strategy_execution_audit_snapshots` | 7,288 信号；27,690 前向收益；2 条信号证据；7,707 审计快照 |
| 运行控制 | `strategy_task_runs`、`strategy_factory_dispatches`、`factory_run_ledger` | 34,828 task runs；492 dispatches；**0 ledger 行** |
| 会话/Agent | `app_sessions`、`audit_logs`、`permissions`、`roles` | 单机单用户；`app_users` 2 行 |

> `stocks` 的 5,582 行包含 5 行指数基准（如 `sh000001`、`sh000300`），它们的 `industry/sector` 为 NULL；按行业 `GROUP BY` 时不会混入公司。见 [README.md:132-135](file:///Users/mac/Desktop/张勇/AIASK/README.md#L132-L135)。

### 7.2 两个账本，两个状态词表

- **真实运行账本**：`strategy_factory_runs`（513 行，状态含 `success_observe_only`、`success`、`partial_infra`、`skipped`、`failed`）与 `strategy_factory_dispatches`（492 success）。
- **控制面账本**：`factory_run_ledger`（0 行），由 `mcp_control.py` 的只读工具读取。见 [factory_control.py:167-193](file:///Users/mac/Desktop/张勇/AIASK/packages/aiask-quant-core/src/aiask_quant_core/storage/sqlite/strategy_ai_repos/factory_control.py#L167-L193)。
- `success_observe_only` 是合法状态：`run_envelope.py` 把它映射为 `partial`，`factory_runs.py` 把它映射为 `SUCCEEDED`。见 [run_envelope.py:74](file:///Users/mac/Desktop/张勇/AIASK/packages/strategy-factory/src/strategy_factory/contracts/run_envelope.py#L74)、[factory_runs.py:89](file:///Users/mac/Desktop/张勇/AIASK/packages/strategy-factory/src/strategy_factory/api/factory_runs.py#L89)。
- 任务运行表同时存在 `completed`（27,105）和 `success`（7,707）两种词表；它们来自不同写入路径，不能简单相加当作“成功”。

### 7.3 当前数据质量判断

- 行情/财务/因子覆盖已基本补齐，`data_quality_issues = 0`；但 `factor_values` 最新只到 2026-10-16，行情到 2026-10-19。
- 36 个 `orphaned_exit` 持仓说明退出路径没有稳定写完；只有 1 个 `closed` 持仓，hard gate 的 round-trip 证据不足。
- `strategy_signal_evidence` 仅 2 行，与 7,288 条信号、7,707 份审计形成强烈不对称；这是“有审计、缺信号证据”的典型形态。
- `backtest_results` 表 0 行；回测结果可能只落在 run summary/artifacts，需验证是否属于设计（避免把“表为空”直接当作“没回测”）。

---

## 8. 依赖与边界

图谱：[dependency-map.svg](file:///Users/mac/Desktop/张勇/AIASK/docs/architecture/current-state-2026-10-20/dependency-map.svg) · 源文件：[dependency-map.dot](file:///Users/mac/Desktop/张勇/AIASK/docs/architecture/current-state-2026-10-20/dependency-map.dot)

### 8.1 规范 DAG

`runtime-boundary.json` 规定包顺序与允许边：`aiask-quant-core → strategy-factory → akshare-mcp → aiask-data-mcp → aiask-finance-mcp-servers → aiask-skills → aiask-agent-reference → aiask-agent`；并禁止 quant-core import 上层、禁止 strategy-factory 静态 import akshare-mcp。见 [runtime-boundary.json:17-42](file:///Users/mac/Desktop/张勇/AIASK/docs/architecture/runtime-boundary.json#L17-L42)。

### 8.2 实际 import 计数（本次静态扫描）

| 边 | 计数 | 性质 |
|---|---:|---|
| `akshare-mcp → strategy-factory` | 128 | 静态 import，声称只走 `api`/`runtime` 公共面 |
| `akshare-mcp → aiask-quant-core` | 36 | 静态 import |
| `strategy-factory → aiask-quant-core` | 19 | 静态 import |
| `agent → akshare-mcp` | 15 | 静态 + 动态 import |
| `agent → strategy-factory` | 19 | 静态 import |
| `agent → aiask-quant-core` | 3 | 静态 import |
| `data-mcp → akshare-mcp` | 0 静态 | 动态 `importlib` 转发（设计使然） |
| `strategy-factory → akshare-mcp` | 0 静态 | 1 个动态 configurator（文档化例外） |
| `quant-core → strategy-factory` | 1 | 兼容 re-export，**与边界规则冲突** |

> 仓库内还有一份 2026-05-29 的 Graphify 代码图谱（`reports/code-graph/`），但 `docs/factory-architecture/01-当前实际架构.md` 明确说它早于部分 6 月修改、是“架构证据而非最终真相”。本报告的依赖数字是 2026-10-20 重新扫描的 import 计数，不沿用旧 Graphify 数字。

### 8.3 边界判断

- **主要方向是健康的：** strategy-factory 不静态依赖 akshare；akshare 的 128 条 import 是“宿主依赖领域”，符合当前 direction；quant-core 基本独立。
- **两处兼容缝：**
  1. `aiask_quant_core/storage/sqlite/schema_strategy.py` 动态 import `strategy_factory.infrastructure.factory_schema_ddl`；注释说明这是“DDL 迁移后的兼容 re-export”，但它确实违反了 runtime-boundary 的“quant-core 不得 import 上层”规则。见 [schema_strategy.py:38-52](file:///Users/mac/Desktop/张勇/AIASK/packages/aiask-quant-core/src/aiask_quant_core/storage/sqlite/schema_strategy.py#L38-L52)。
  2. `strategy_factory.runtime.default_bootstrap` 动态 import `akshare_mcp.adapters.strategy_factory_runtime`；这是文档化例外，计划移除。见 [default_bootstrap.py:24-27](file:///Users/mac/Desktop/张勇/AIASK/packages/strategy-factory/src/strategy_factory/runtime/default_bootstrap.py#L24-L27)。
- **akshare-mcp 仍是巨石。** 200,372 行、507 个文件，同时承担 MCP host、provider、managers、services、SQLite 实现；`data-mcp` 只是中性 facade，没有真正拆分。它是当前变更影响面最大的节点。
- **MCP 交付面：** AKShare 179 tools / 3 resources / 14 templates / 7 prompts；工厂控制面 5 tools / 2 resources。二者都是 stdio，不是常驻 HTTP。

---

## 9. 当前实际情况：事实、偏差与风险

### 9.1 实测事实（2026-10-20）

| 事实 | 数值/状态 | 证据 |
|---|---|---|
| 市场覆盖 | 5,582 标的；1,738,883 日线；最新 2026-10-19 | SQLite 只读查询 |
| 财务覆盖 | 119,837 行；最新报告期 2026-09-30 | SQLite |
| 因子覆盖 | 386,706 行；44 个因子；5,561 标的；最新 2026-10-16 | SQLite |
| 策略账本 | 8,233 条：6,079 rejected / 1,644 submitted / 493 draft / 17 archived_draft | SQLite |
| 工厂运行 | 513 轮：492 `success_observe_only`、16 success、2 partial_infra、2 skipped、1 failed | SQLite |
| 最新一轮 | 候选 29、Gate3 15 进 0 过、提交 3、audit-only 12 | `strategy_factory_runs.summary` |
| 孵化账户 | 1,659：1,642 paper/active + 17 warmup/active | SQLite |
| paper 交易 | 119 订单、155 成交、155 持仓（118 open / 1 closed / 36 orphaned_exit） | SQLite |
| 证据链 | 7,288 信号、27,690 前向收益、7,707 审计、2 条 signal evidence | SQLite |
| 晋级 | 0 promotion_reviews、0 listed | SQLite |
| 控制面账本 | `factory_run_ledger` 0 行 | SQLite |
| Agent HTTP | 未运行；8765 于 08:59 已释放（08:51–08:57 曾被他项目占用） | `lsof`、`ps` |
| SignalTracker | 未运行；无 status.json | `ps`、文件检查 |

### 9.2 文档与现状的偏差

- [README.md:109-149](file:///Users/mac/Desktop/张勇/AIASK/README.md#L109-L149) 的“Known state”是 **2026-10-15 快照**：那里写 240 个策略、187 rejected、34 submitted；当前库是 8,233 个策略、6,079 rejected、1,644 submitted。不是同一时点。
- README 说“Nothing is scheduled. No process, no cron entry, no supervisor.”；当前四运行时 supervisor 自 2026-10-18 16:02 起在跑，策略工厂每 5 分钟一轮。这条已过期。
- [CURRENT_PRODUCT_TRUTH.md](file:///Users/mac/Desktop/张勇/AIASK/docs/CURRENT_PRODUCT_TRUTH.md) 的架构判断（backend-only、无前端、live off）仍成立；它的 2026-07 baseline 是历史，不应作为当前运行证明。
- `factory_run_ledger` 与 `strategy_factory_runs` 的偏差不是文档问题，是**控制面接线问题**：只读 MCP 控制面读的是空账本。

### 9.3 主要风险（按影响排序）

1. **证据链未闭合，生产线无法晋级。** 1,642 个 paper 账户、155 笔成交、1 笔平仓、0 次评审。继续生成策略不会解决；需要先补齐平仓/round-trip 证据与 hard gate 输入。
2. **SignalTracker 缺席，但信号仍在产生。** 无法判断 10-18 的新信号来自旧运行、工厂内部路径还是残留数据；这会让“信号—前向收益—晋级”链路的来源不可审计。
3. **Agent 运行环境过期，端口曾短暂冲突。** 8765 已释放，但 agent.meta 仍指向已删除路径与 venv；直接启动 AIASK Agent 会失败或误连。
4. **控制面与运行账本分裂。** 只读 MCP 控制面可能对运行中的工厂报 `never_run`；运营者会得到错误状态。
5. **akshare-mcp 巨石与跨包兼容缝。** 200k 行、128 条向上依赖、quant-core 的 1 条反向兼容 import；变更影响面大，边界规则未完全自动化。
6. **文档时点漂移。** README 的数字和“无调度”结论已过期；需要把“历史快照”与“当前实测”分开标注。

---

## 10. 未知项与验证任务

| 未知项 | 为什么重要 | 验证动作 | 优先级 |
|---|---|---|---|
| 10-18 的信号由谁产生 | SignalTracker 未运行，信号却在更新；无法审计证据来源 | 查 `strategy_signals` 的 `created_at`/source 字段；对比最近 SignalTracker 运行日志；在隔离库跑一次 SignalTracker `run_once` | P0 |
| 36 个 `orphaned_exit` 的成因 | 直接影响平仓/round-trip 证据 | 查 `strategy_trade_positions` 的退出路径字段；用一只标的回放 `exit_rule`；检查 `matching_engine._check_stop_loss` 与 `exit` 分支 | P0 |
| `factory_run_ledger` 是否应被写入 | 控制面状态正确性 | 查 `request_factory_run` / `transition_factory_run` 的调用点；决定是写 ledger 还是让控制面读 `strategy_factory_runs` | P0 |
| 8765 端口是否会被再次占用 | 启动 Agent 的前置条件 | 启动前 `lsof` 确认；如再被占用则换端口并同步 monitoring/Makefile | P0 |
| `backtest_results` 为 0 是否符合设计 | 回测是否真的执行、结果落在哪里 | 查 `strategy_factory_run_artifacts`、run summary 的 backtest_report；确认是否需要独立表 | P1 |
| `strategy_signal_evidence` 只有 2 行 | 晋级 hard gate 的证据输入 | 查 SignalTracker 的 evidence 写入路径；对比 `signal_forward_returns` 的 27,690 行 | P1 |
| `promotion_reviews` 为 0 是门禁未满足还是未触发 | 晋级路径是否可达 | 用一条 paper 账户手工跑 `promotion_pipeline.review()`；检查 hard gate 的 `execution_audit_gate_status` | P1 |
| 孵化账户 1,659 与 `paper_accounts` 1,661 的 2 个差额 | 账户归属/清理是否一致 | 按 account_id 左连接两表，找无孵化记录或重复账户 | P2 |
| `factor_pool_active = 29` 与因子日志 `pool_size = 11` 的口径差 | 因子池是否含 seed/quarantine/观察池 | 查 `factor_pool_active` 的 status/scope；对照 `factor_mining_factory.log` 的 pool 统计 | P2 |
| `success_observe_only` 的下游消费是否一致 | 状态词表跨 API 的语义 | 检查 Agent `factory_runs`、MCP 控制面、CLI 对同一 run 的状态映射 | P2 |
| 监控配置是否真的在跑 | 当前只有 YAML，没有进程证据 | 查 Prometheus/OTel 端口与进程；没有就标记为“配置存在、未验证” | P2 |

---

## 11. 建议的下一步（方向，不在本次实施）

1. **先让运行态自洽，再谈扩产。** 确认 8765 可用并修复 agent 运行环境；决定 SignalTracker 是否纳入 supervisor；统一 `factory_run_ledger` 与 `strategy_factory_runs` 的读写。
2. **把“平仓/round-trip”作为 P0 业务门禁。** 36 个 orphaned_exit + 1 个 closed 是比 Gate3 更根本的证据问题；先让一条 paper 策略完整走完 open → close → audit → review。
3. **诊断 Gate3 的“缺失”而不是“失败”。** 当前高频原因是 missing parameter perturbation / insufficient statistical evidence；先确认这些字段能否被数据与回测产出，而不是继续加候选。
4. **拆分 akshare-mcp 的宿主职责。** 至少把 manager/service/SQLite 实现与 MCP host 分层；先做依赖图上的“变更影响面”治理，而不是一次性大搬迁。
5. **把当前态文档变成可复算的。** README 的“Known state”应标注测量时间与命令；控制面/账本/端口这类运行事实应进入 readiness 检查，而不是靠人记。

---

## 12. 产物清单

| 文件 | 回答的问题 | 类型 |
|---|---|---|
| [architecture-understanding.md](file:///Users/mac/Desktop/张勇/AIASK/docs/architecture/current-state-2026-10-20/architecture-understanding.md) | 项目是什么、现在实际怎样、关键流程与风险 | 主报告 |
| [aiask-system.structurizr.dsl](file:///Users/mac/Desktop/张勇/AIASK/docs/architecture/current-state-2026-10-20/aiask-system.structurizr.dsl) | L1 系统上下文 + L2 容器 | C4 / Structurizr |
| [capability-map.svg](file:///Users/mac/Desktop/张勇/AIASK/docs/architecture/current-state-2026-10-20/capability-map.svg) / [.dot](file:///Users/mac/Desktop/张勇/AIASK/docs/architecture/current-state-2026-10-20/capability-map.dot) | L0 业务能力地图 | Graphviz |
| [flow-agent-tool-call.svg](file:///Users/mac/Desktop/张勇/AIASK/docs/architecture/current-state-2026-10-20/flow-agent-tool-call.svg) / [.dot](file:///Users/mac/Desktop/张勇/AIASK/docs/architecture/current-state-2026-10-20/flow-agent-tool-call.dot) | Agent 请求 → 工具 → 数据/证据 | Graphviz |
| [flow-strategy-factory-cycle.svg](file:///Users/mac/Desktop/张勇/AIASK/docs/architecture/current-state-2026-10-20/flow-strategy-factory-cycle.svg) / [.dot](file:///Users/mac/Desktop/张勇/AIASK/docs/architecture/current-state-2026-10-20/flow-strategy-factory-cycle.dot) | 策略工厂单轮 11 阶段与当前漏斗 | Graphviz |
| [flow-paper-lifecycle.svg](file:///Users/mac/Desktop/张勇/AIASK/docs/architecture/current-state-2026-10-20/flow-paper-lifecycle.svg) / [.dot](file:///Users/mac/Desktop/张勇/AIASK/docs/architecture/current-state-2026-10-20/flow-paper-lifecycle.dot) | 孵化/paper/晋级生命周期 | Graphviz |
| [flow-data-ingestion.svg](file:///Users/mac/Desktop/张勇/AIASK/docs/architecture/current-state-2026-10-20/flow-data-ingestion.svg) / [.dot](file:///Users/mac/Desktop/张勇/AIASK/docs/architecture/current-state-2026-10-20/flow-data-ingestion.dot) | 数据源 → 同步/校验 → SQLite → 消费者 | Graphviz |
| [deployment-topology.svg](file:///Users/mac/Desktop/张勇/AIASK/docs/architecture/current-state-2026-10-20/deployment-topology.svg) / [.dot](file:///Users/mac/Desktop/张勇/AIASK/docs/architecture/current-state-2026-10-20/deployment-topology.dot) | 进程、端口、存储、外部系统 | Graphviz |
| [dependency-map.svg](file:///Users/mac/Desktop/张勇/AIASK/docs/architecture/current-state-2026-10-20/dependency-map.svg) / [.dot](file:///Users/mac/Desktop/张勇/AIASK/docs/architecture/current-state-2026-10-20/dependency-map.dot) | 包依赖与边界例外 | Graphviz |
| [evidence-index.md](file:///Users/mac/Desktop/张勇/AIASK/docs/architecture/current-state-2026-10-20/evidence-index.md) | 每条关键结论的证据与复算命令 | 证据索引 |
| [artifact-summary.md](file:///Users/mac/Desktop/张勇/AIASK/docs/architecture/current-state-2026-10-20/artifact-summary.md) | 产物阅读顺序与维护说明 | 摘要 |
