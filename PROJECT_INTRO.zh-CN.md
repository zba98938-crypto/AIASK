# Hermes + AIASK 金融智能体项目介绍

## 一句话定位

这是一个以 Hermes Agent 为通用智能体底座、以 AIASK 为隔离式金融/量化边缘能力层的研究型系统。它的目标不是让大模型直接“荐股或自动下单”，而是把行情数据、因子研究、策略生成、纸面交易和审计证据串成一条可追溯的策略生产线。

## 项目由什么组成

### Hermes Agent（通用底座）

Hermes 是 Nous Research 的自我改进型 AI Agent，负责：

- 对话循环、模型适配、上下文/提示缓存、记忆和跨会话检索；
- CLI/TUI、Hermes Desktop、Telegram、Discord、Slack、WhatsApp、Signal 等入口；
- 终端、文件、浏览器、搜索、代码执行、任务委派和定时任务；
- MCP 客户端、插件发现和 skills 系统。

Hermes 核心不直接导入金融包。该边界保护核心的提示缓存和稳定工具面，也使金融能力可以独立升级。

### AIASK 集成层（`integrations/aiask`）

这是从独立 AIASK 项目复制而来的、隔离的金融运行时，包含：

| 包 | 作用 |
|---|---|
| `packages/agent` | FastAPI Agent HTTP、`agent_*` 安全工具 facade、会话/审批和金融工作流入口 |
| `packages/akshare-mcp` | AKShare MCP 主服务器、行情/财务/新闻/技术面/研究/量化工具、SQLite 存储和数据新鲜度检查 |
| `packages/aiask-quant-core` | 因子计算、回测、策略 DSL、信号/风险/滑点、交易审计和量化存储原语 |
| `packages/strategy-factory` | 策略工厂领域模型、生命周期、调度、候选路由、准入、质量门和提交 |
| `packages/finance-mcp-servers` | 通达信、同花顺、东方财富、MiniQMT 的可选 MCP 服务器 |
| `desktop` | 仅面向金融运维的五视图工作台，不是 Hermes Desktop 的聊天重写 |

AIASK 使用 Python 3.12–3.13 独立虚拟环境；不要把它的依赖安装到 Hermes 核心环境。

## 要达到的目标

最终目标是“可审计的策略生产线”：

```text
市场事件 → 因子原料 → 策略候选 → 准入/回测 → observe/paper 孵化
        → 信号 → paper order/trade → forward returns → execution audit
        → 风险/治理门 → 晋级讨论 → 反馈回策略和因子工厂
```

这里的“晋级”必须建立在真实 paper closed round-trip、前向收益、执行审计和风险/治理证据上。bootstrap、回测或一次 quality session 只能证明代码路径或提供诊断，不能替代生产 hard gate。

## 四工厂与 SignalTracker

当前标准口径的四个 supervisor 运行体是：

1. **Market Event Ingest**：抓取官方/可信市场事件，统一 source tier、可靠性和降级原因，并生成事件锚点/策略任务。
2. **Factor Mining Factory**：搜索和组合因子，执行 sandbox、IC/中性化、样本外验证、QC，并维护有来源和新鲜度元数据的 active factor pool。
3. **Strategy Factory**：融合事件、因子、股票画像和 LLM 研究结果，生成结构化候选，做去重、准入、质量门、提交和生命周期编排。
4. **Incubation Factory**：接收 observe/diagnostic/paper 策略，生成信号，推进 paper order、成交、持仓、前向验证、命中率、反馈和 execution audit。

**SignalTracker 是 sidecar，不是第四工厂**。它负责跨策略、信号、paper execution、forward returns 和 evidence 表推进证据闭环；缺失时应被报告为闭环不完整。`run_three_factories.py` 是历史文件名，实际 supervisor 管理四个运行体，且不自动包含 SignalTracker。

策略工厂有两种重要入口：

- **事件/主题驱动**：从市场事件、主题图谱、因子和研究任务生成候选；
- **stock-first（个股优先）**：先读取 `StockRegimeProfile`，按趋势、均值回归、价值/质量、成长、多因子、事件轮动等 family 路由，并给出持有周期、置信度、排除项和理由。路由器本身是无 IO 的纯函数，低流动性、下跌/震荡等状态会显式排除不适合的策略族。

策略生命周期采用“物理状态 + 证据派生覆盖层”：数据库主状态保持有限且稳定，`generated → signal → order → fill → forward → round-trip → audit → promotion_ready` 是诊断视图，不可伪造写入主状态。系统必须区分 `missing`（链路断裂）和 `insufficient_samples`（样本尚未成熟）。

## 股票与金融 MCP 能力

### AKShare MCP（研究和数据主入口）

`akshare-mcp` 通过 FastMCP 注册市场、财务、资金流、宏观、新闻、期权、技术指标、回测、组合、估值、决策、研究、告警、交易计划、股票雷达和市场温度等工具；另有资源、prompts 和运行时 skills。

典型能力包括：

- 个股基本资料、K 线、实时行情、技术指标、估值/财务和资金流；
- 新闻/公告/研究检索、语义股票搜索、行业/概念板块和成分股；
- 股票深度分析工作流、交易计划、关键价位/止损位、市场温度与股票雷达；
- 因子库、因子计算、IC、因子回测、样本外验证、鲁棒性和风险分析；
- 本地 SQLite 数据同步、缓存、warmup、freshness/readiness 和 stale/degraded/fallback 信号。

数据源可按 TDX、TQCenter、Tushare、AKShare 等路由；Tushare/在线源不是无条件启动依赖，`TDX_LOCAL_ONLY`、`AKSHARE_MCP_SQLITE_PATH` 等边界必须保留。空数据、过期数据和降级不能静默伪装成成功。

### 金融软件 MCP（可选，带副作用）

| MCP | 行情 | 交易 | 现实约束 |
|---|---:|---:|---|
| 通达信 | ✓ | ✓ | `pytdx`，需行情服务器 |
| 同花顺 | — | ✓ | `easytrader`，主要需 Windows 客户端 |
| 东方财富 | ✓ | — | `efinance`，以公开行情为主 |
| MiniQMT | ✓ | ✓ | 需安装 QMT/XtQuant 客户端 |

查询工具与下单/撤单工具分离。交易工具带 `stateful/trade_risk` 元数据，要求 ActionIntent/确认、broker token 和显式启用；`finance_safe` 默认不可交易，项目当前明确不宣称 live trading 产品或券商生产认证。

## Skills 是什么、能做什么

Skills 不是新的核心模型工具，而是面向 Agent 的可复用操作规范、参数约束、诊断流程和安全边界。当前同步到 Hermes 的 `skills/finance/` 有 14 个 AIASK skill，覆盖：Agent runtime/native tools/integrations、AKShare 数据与 manager、研究分析、Quant Core、Strategy Factory、Factor Mining、Incubation、金融 MCP、记忆/模型学习和架构审查。

其中最关键的使用关系是：

- `aiask-strategy-factory`：策略 facade、cycle、stock-first、准入、生命周期、质量门和 paper bridge；
- `aiask-akshare-mcp-data`：MCP 启动 profile、工具注册、数据源路由、SQLite、同步与 freshness；
- `aiask-factor-mining-factory`：因子搜索、验证、QC、active pool 和反馈；
- `aiask-incubation-factory`：孵化阶段、信号、forward、命中率、反馈和晋级门；
- `aiask-finance-mcp-servers`：券商/行情 MCP 的工具面、可选依赖和交易风险保护。

Skills 的作用是让 Hermes 知道“何时调用哪个 facade/MCP、哪些数据必须核验、哪些动作必须确认”，而不是绕过边界直接暴露 `strategy_manager` 或直接下单。

## 当前能实现什么

在依赖、数据和凭证就绪的前提下，项目可以：

- 通过 Hermes 对话或 AIASK Agent HTTP 查询股票和市场信息；
- 执行股票分析、研究检索、因子研究、回测和策略审查；
- 运行四工厂的一次 cycle，查看 status、runs、readiness 和 evidence；
- 运行/监控 paper 孵化、信号追踪、forward returns 和执行审计；
- 通过可选 MCP 连接本地金融软件读取行情/账户，或在满足 token/确认条件后执行受保护的交易动作；
- 使用 Hermes 的 CLI、Desktop、TUI、消息平台和 cron 分发研究结果。

## 当前不能据此宣称的事情

- 不是保证收益的荐股系统，也不是自动盈利机器人；
- 不是已完成的全量 AIASK V1 前端，`packages/desktop-api` 在此副本不存在；
- Hermes Desktop 不是完整 AIASK 金融工作台；金融 workbench 是独立的 focused 五视图；
- 不是默认开启的实盘交易系统，不代表券商生产认证；
- `factory once/cycle` 不等于 24 小时 supervisor 或长期健康，`ops-start` 也不等于多日 soak；
- 当前本地 evidence 表可能为空，`hard_gate_passed=0` 在没有真实 paper round-trip 时是正确且诚实的结果。

## 适合的使用者与下一步

它适合量化研究者、金融 Agent 工程师和希望把“想法—数据—策略—纸面证据”自动化的人。推荐顺序是：先建立独立 AIASK venv，运行 readiness 和本地只读 AKShare MCP；再同步数据并执行单次 factory cycle；确认 evidence/diagnostics 可解释后，再启用长期 supervisor 或任何受保护的 broker action。生产化重点应放在真实 paper round-trip、数据质量、执行审计和风险治理，而不是追求一次运行返回“成功”。

## 运行架构：一次请求是怎样完成的

一次典型的“分析某只股票”请求大致经过以下路径：

1. 用户从 Hermes CLI、Desktop、TUI 或消息平台发出自然语言问题。
2. Hermes Agent 根据当前会话、已启用 toolset 和 finance skill 判断是否需要调用金融能力。
3. 请求进入 AIASK Agent HTTP 或 MCP 客户端；Hermes 不直接导入 AIASK 的内部 manager。
4. Agent facade 对参数、工具权限、数据质量和副作用类型进行归一化。
5. AKShare MCP 从本地 SQLite、缓存或配置的数据源读取股票资料、行情、财务、新闻、技术面和因子数据。
6. 研究/量化服务计算指标、风险、因子 IC、回测或结构化分析结果。
7. 返回结果时保留 source、freshness、degraded、fallback、evidence 等元数据。
8. Hermes 将结果组织成面向用户的解释，同时保留必要的限制和数据缺口。

策略工厂运行时通常不是通过模型逐个调用 MCP 工具，而是通过 Strategy Factory 的公开 facade、runtime bootstrap 和注入的 provider/adapter 直接编排。MCP/Agent facade 主要负责外围状态查询、触发、诊断以及受控工作流入口。

## 三层能力模型

### 第一层：数据层

数据层解决“市场发生了什么、股票现在是什么状态、历史数据是否可信”的问题。它包括：

- 行情和 K 线：指数、个股、板块、成分股和历史序列；
- 基本面：公司资料、财务指标、估值和股东/资金相关信息；
- 市场信息：新闻、公告、研究文本、宏观与行业事件；
- 衍生数据：技术指标、资金流、市场温度、情绪、关键价位和风险信号；
- 本地存储：SQLite 表、缓存、向量/语义检索和数据同步任务。

数据层的关键设计不是“尽可能多抓数据”，而是记录数据来自哪里、何时更新、是否过期、是否降级、是否通过本地缓存，以及当前数据是否足以支持研究或策略准入。

### 第二层：研究与量化层

Quant Core 与 AKShare research services 将原始数据转换成可比较、可回测、可审计的研究对象：

- 因子表达式和因子库；
- 截面/时序因子计算；
- IC、分组收益、样本外验证和鲁棒性分析；
- 回测、交易成本、滑点、风险和压力测试；
- 策略 DSL、交易预测 contract、信号和执行审计；
- 股票画像、趋势/波动/情绪/估值/质量/成长维度。

该层的输出不是简单的“买入/卖出”，而是包含输入数据、参数、时间窗口、指标、缺失字段和质量标签的研究证据。

### 第三层：Agent 与产品层

Agent 层把研究能力变成可使用的产品：

- 自然语言股票分析和研究问答；
- 策略工厂状态、运行记录、事件 lineage 和 evidence 查询；
- 工厂单次运行、研究 cycle、readiness 和诊断；
- Desktop finance focused workbench 的状态展示；
- Hermes 的对话记忆、skills、定时任务和跨平台投递；
- 对交易、插件变更、文件/终端写入等动作进行确认和授权。

## 策略工厂详细说明

### 1. 候选从哪里来

候选策略可以来自四类输入：

- **市场事件**：公告、交易所披露、官方事件和可信主题变化；
- **因子原料**：Factor Mining Factory 验证后的 active factor pool；
- **股票画像**：个股状态、行业、流动性、波动和估值/质量画像；
- **研究反馈**：历史策略表现、淘汰原因、孵化证据和预算反馈。

LLM 可以参与假设生成、策略草案和研究解释，但它不是证据本身。策略必须落成结构化 contract，至少说明 hypothesis、universe、entry、exit、risk、holding period、data dependencies、版本和 lineage。

### 2. 个股优先路由

stock-first 模式先构造 `StockRegimeProfile`，再由纯函数路由器选择策略 family。例如：

- 上升趋势 + 动量较强 + 放量：趋势跟随、均线交叉、突破类；
- RSI 超卖或反转分较高、且不是明显下跌趋势：均值回归、RSI、缺口回补；
- 估值吸引力和质量分同时较高：价值、质量、成长类；
- 多因子 IC 共振：多因子类；
- 事件催化和情绪发酵：事件突破、行业轮动类。

路由器还会输出 holding-period bucket、confidence、exclusions 和 rationale。例如低流动性股票会排除短线高频族，震荡或下跌状态会排除趋势族。这样做的目的，是减少对所有股票套用同一策略模板。

### 3. 准入与质量门

候选生成后会经过结构完整性、去重、数据依赖、回测/验证、风险和治理检查。每个 gate 都应记录通过、拒绝、跳过或降级原因。

系统区分：

- `rejected`：候选没有通过准入或质量门；
- `blocked`：链路断裂、服务未运行、关联缺失或工具失败；
- `pending_evidence`：链路方向正确，但真实样本尚未成熟；
- `bootstrap_pending/bootstrap_ready`：诊断或样本债状态，不是生产通过；
- `passed`：真实 paper realized trades 达到 production floor 且审计指标通过。

### 4. 孵化和证据闭环

被接纳的策略进入 observe、diagnostic 或 paper 轨道。SignalTracker 和 Incubation Factory 继续推进：

```text
策略候选
  ↓
观察账户/孵化账户
  ↓
策略信号
  ↓
paper order
  ↓
paper trade / open position
  ↓
forward return 到期
  ↓
退出并形成 closed round-trip
  ↓
execution audit
  ↓
风险与治理检查
```

只有形成真实 closed round-trip，才能逐步获得生产级执行证据。单独有信号、订单、买入成交或回测收益，都不能说明策略已经成熟。

## 股票 MCP 详细说明

### AKShare MCP 的只读研究面

最适合作为默认启用的股票 MCP。它可以支持以下自然语言任务：

- “查询某股票最近的行情、财务和估值”；
- “总结某股票近 30 天公告和新闻，并区分来源可信度”；
- “比较同一行业股票的技术面、资金流和市场温度”；
- “计算一个因子过去一年的 IC，并做样本外验证”；
- “对某股票做深度分析，给出数据缺口和风险提示”；
- “运行股票雷达，输出候选和可投递摘要”。

其核心安全原则是：默认读本地数据和公开数据；数据源切换显式可见；缓存过期、空表和 provider 失败不能被包装成正常结果。

### 金融软件 MCP 的连接面

金融软件 MCP 更接近本地终端和券商环境：

- 通达信适合行情读取，也具备交易接口；
- 东方财富适合公开行情读取，不提供该包中的下单能力；
- 同花顺主要用于 Windows 下的交易客户端自动化；
- MiniQMT 连接 QMT/XtQuant，适合已有 QMT 环境的用户。

它们不是 AKShare MCP 的替代品，而是可选 provider。缺少本地客户端、账号、token 或平台依赖时，相关服务器应不可用或降级，不应影响只读研究功能。

## Skills 的使用方式

Skill 通常包含名称、适用场景、必须先读的参考文档、关键文件、硬性规则和测试要求。它解决的是“如何正确使用能力”，而不是“增加一个永远发送给模型的核心 tool”。

一个金融 skill 的典型工作方式是：

1. 根据用户任务选择 skill；
2. 读取该 skill 的完整说明和必要 references；
3. 使用公开 facade、Agent `agent_*` 工具或 MCP workflow；
4. 检查 readiness、freshness、权限和副作用；
5. 返回结果和证据、缺口、降级原因；
6. 对写入、交易和长期任务保留确认与审计记录。

因此，`aiask-strategy-factory` 会阻止直接暴露 raw `strategy_manager`；`aiask-akshare-mcp-data` 要求保留 TDX 本地模式和数据 freshness；`aiask-finance-mcp-servers` 要求每次交易路径校验 broker token；`aiask-incubation-factory` 要求保持 diagnostic/paper/live 边界清晰。

## 典型用户流程

### 研究股票

用户在 Hermes 中提出问题 → Agent 选择 finance skill → 调用 AKShare MCP → 返回行情、财务、新闻、技术面和数据质量 → Hermes 生成带引用范围和风险提示的分析。

### 研究和筛选策略

用户指定市场、股票池或主题 → Market Event Ingest/Factor Mining 提供事件和因子 → Strategy Factory 生成结构化候选 → 通过准入、回测和风险 gate → 进入 observe/diagnostic/paper。

### 验证策略

启动 SignalTracker 和 Incubation Factory → 产生信号并转 paper order → 记录成交、持仓、退出和 forward returns → 执行 audit → 查看 blocker、样本债或晋级条件。

### 受保护的交易动作

只有在用户明确启用、broker token 正确、ActionIntent/确认流程通过且 provider 可用时，才允许进入下单/撤单路径。默认 finance-safe 配置只开放研究和只读能力。

## 部署与运维要点

- Hermes 和 AIASK 使用不同虚拟环境；AIASK 要求 Python 3.12–3.13；
- 优先运行 `aiask_readiness.py`，再执行只读数据 warmup 和单次 factory cycle；
- MCP 可通过 stdio 注册，也有受保护的 loopback HTTP/Agent HTTP surface；
- 生产运行要观察 factory status、run history、evidence summary、数据 freshness、skip reason 和 orphan process；
- `factory once` 只证明一次运行结束；`factory cycle` 只证明选定研究工厂完成一轮；`ops-start` 只证明 supervisor 启动并管理进程，不证明 24 小时稳定；
- 所有 live、broker、外部写入和插件变更都应显式授权并可审计。

## 现阶段最重要的产品判断

该项目已经具备“研究自动化和策略证据生产线”的大量代码与接口，但仍应以研究/paper 为主产品定位。最有价值的下一步不是扩大模型工具数量，而是：

1. 持续同步可信市场数据；
2. 让四工厂产生可复核的运行和 evidence；
3. 形成真实 paper closed round-trip；
4. 减少 missing/blocked 状态，明确区分链路故障与样本不足；
5. 在完成风险治理和人工确认后，再评估受限的 live 能力。
