# AGENT.md — AIASK 开发与协作规范


> **Hermes 集成副本勘误（2026-07-18）**  
> 本文是历史/上游阶段材料，不是当前 Hermes 树完成证明。  
> 在当前 `integrations/aiask/` 副本中：`packages/desktop-api` 不存在；Hermes Desktop 不是完整 AIASK workbench；live trading 默认关闭。  
> 权威状态：`AIASK_FINANCE_AGENT_COMPLETION_AUDIT.md`、`integrations/aiask/docs/CURRENT_PRODUCT_TRUTH.md`、`docs/GAP_INVENTORY_STATUS.md`。

> 本文件是 AIASK 仓库的长期开发约定。所有人类开发者与 AI 编码代理在改动本仓库前必须先读它。
>
> 当前阶段：AIASK 的 Desktop、Agent、金融 MCP、Strategy Factory、Quant Core、外部金融连接器、技能/插件/网关等主体已经形成。后续开发重点不是继续堆功能，而是收敛边界、稳定契约、提高可验证性、补齐风控和运维能力。

---

## 1. 文档定位

AGENT.md 只写长期有效的开发规范、工程边界和协作原则。它不承担实现百科、路由表、文件索引、历史方案清单或模块细节说明的职责。

具体实现以当前代码、manifest、测试和 Graphify/code graph 事实为准；专题方案、诊断、评审和执行计划放入 `docs/`。如果本文与当前代码冲突，以代码为准，并在同次改动中修正文档。

本文件的目标是回答三件事：

1. AIASK 现在按什么边界继续开发。
2. 哪些安全、风控、测试和协作规则不可破坏。
3. AI 编码代理进入仓库后应该如何判断、修改和验证。

---

## 2. 项目定位

AIASK 是面向中国金融市场的本地优先金融系统级 Agent 平台。它不是单一聊天机器人，也不是单纯 AKShare 封装，而是一个把桌面工作台、Agent 控制面、金融数据/交易连接、策略生产、量化研究、技能插件和自动化任务组合起来的可操作系统。

当前默认可用形态是：

- 本地或内网优先。
- 只读研究、数据分析、策略观察优先。
- dry-run、paper、sandbox 优先。
- 状态变更必须显式确认。
- 真实交易默认关闭，必须额外满足确认、权限、环境和审计条件。

任何“让模型直接操作资金、账户、交易软件、持久状态或外部平台”的能力，都必须被视为高风险能力，而不是普通功能。

---

## 3. 当前工程事实

开发判断必须基于当前仓库事实，而不是历史对话或旧文档记忆。

当前技术栈的长期事实包括：

- 后端主体是 Python 包体系，核心运行时围绕 FastAPI、Pydantic、MCP、金融数据工具和量化/策略模块组织。
- Desktop 是 React、Vite、Tauri 工作台，职责是展示、交互、审批和能力编排，不承接后端风控实现。
- Agent 是系统控制面，负责 HTTP API、模型工具注册、工具策略、ActionIntent、MCP 聚合、技能/插件和运行时能力治理。
- 金融 MCP 与 manager plane 承担金融数据、研究分析、策略管理、组合/风控、paper/live/execution 等能力，但不能直接裸露给模型和 Desktop。
- Strategy Factory、Factor Mining Factory、Incubation Factory 和 Quant Core 已经是独立能力域，后续改动必须保护它们的契约、数据假设和可追踪性。
- 外部金融软件连接器属于高风险边界，读能力和交易能力必须严格区分。
- 本地持久化、会话、intent、任务、策略状态和量化数据以本地文件或 SQLite 形态为主，不能被当成可随意删除或重建的临时缓存。
- root runners、后台任务、gateway、webhook 和自动化链路属于运行态入口，开发时必须同时考虑停止、重试、降级和人工接管。
- Graphify/code graph、endpoint map 和 cross-package edges 是架构审计证据，不能替代代码阅读，但必须用于跨模块影响判断。

这些事实允许被概括进规范，但不应在 AGENT.md 中展开成具体文件表、接口表或实现说明。

---

## 4. 非协商硬规则

1. **代码先于文档**：先读当前代码、manifest、测试和图谱事实，再引用历史文档。
2. **Desktop 只走 Agent**：Desktop 只能通过 Agent HTTP API 消费后端能力，不直接调用 MCP、manager 或 Python 包。
3. **模型工具必须 `agent_*`**：所有模型可见工具必须由 Agent 包装，并使用 `agent_*` 命名。
4. **禁止直曝 manager**：不得把底层 manager 名称或内部工具名作为模型可见工具暴露。
5. **状态变更走 ActionIntent**：高风险、持久化、外部发送、策略推广、交易相关动作必须走可审批、可拒绝、可审计的 intent 流程。
6. **实盘交易默认关闭**：没有明确确认、权限 guard、隔离环境验收和审计证据，不得开放真实下单、撤单或账户写入。
7. **不读取密钥值**：不得读取、复制、输出或引用 `.env` 中的真实值；文档只能写环境变量名称和配置语义。
8. **不操作运行态数据**：除非用户明确要求并说明目的，不操作数据库、日志、缓存、broker runtime 或真实客户端。
9. **失败必须显性化**：禁止宽泛吞错、静默降级和“看起来成功”的 fallback；失败必须带可诊断原因。
10. **测试匹配风险**：越靠近跨包边界、数据质量、策略生产、外部平台和交易路径，验证越必须完整。

---

## 5. 外部基线映射

AIASK 的开发规范参考以下官方或行业基线，但落地时以本仓库边界为准：

| 外部基线 | 在 AIASK 中的开发含义 |
| --- | --- |
| [NIST SSDF SP 800-218](https://csrc.nist.gov/pubs/sp/800/218/final) | 安全需求、代码保护、依赖验证、漏洞响应要进入日常开发，而不是上线前补救。 |
| [CISA Secure by Design](https://www.cisa.gov/securebydesign) | 默认安全、最小权限、可度量安全结果；不要把安全责任转嫁给用户手动加固。 |
| [OWASP Top 10 for LLM Applications](https://owasp.org/www-project-top-10-for-large-language-model-applications) | 防 prompt injection、敏感信息泄露、过度代理、工具误用、输出未校验和无界资源消耗。 |
| [MCP Security Best Practices](https://modelcontextprotocol.io/docs/tutorials/security/security_best_practices) | MCP 工具和 OAuth/consent 必须最小权限、逐步授权、服务端校验、可审计。 |
| [OWASP API Security Top 10 2023](https://owasp.org/API-Security/editions/2023/en/0x00-header/) | Agent HTTP API 必须关注鉴权、授权、对象级权限、资源消耗、SSRF 和外部 API 输入。 |
| [Tauri Capabilities](https://v2.tauri.app/security/capabilities/) | Desktop 必须保持明确 IPC/HTTP 边界、最小 capabilities、收紧 CSP 和系统权限。 |
| [SLSA Provenance](https://slsa.dev/provenance) / [OpenSSF Scorecard](https://github.com/ossf/scorecard) | 依赖、构建、CI、token 权限、锁文件和供应链风险必须可检查、可解释。 |
| [NIST AI RMF](https://www.nist.gov/itl/ai-risk-management-framework) | AI 能力要有治理、风险映射、度量、处置和人类监督，不把模型输出等同事实。 |

这些基线不是文档装饰。新增能力如果削弱默认安全、最小权限、人工确认、依赖可追踪或审计能力，必须在设计中说明理由和补偿控制。

---

## 6. 架构协作边界

### 6.1 Desktop

Desktop 负责产品体验、状态呈现、审批入口、工作流操作和能力可视化。它可以展示能力状态、触发 intent、显示降级和错误，但不能承接后端风控、权限判断或交易安全实现。

Desktop 改动必须同时考虑：

- mock/live 语义一致。
- loading、error、degraded、unauthorized、forbidden 状态完整。
- 用户能看懂审批状态、数据状态和风险状态。
- 不扩大 Tauri capabilities、CSP 或系统权限，除非有清楚安全理由。

### 6.2 Agent

Agent 是系统控制面和模型工具边界。Agent 改动必须保持工具命名、toolset、side-effect、envelope、trace、intent、权限和 Desktop API 契约一致。

新增 Agent 能力必须先回答：

- 这是不是模型可见工具。
- 是否必须属于 `finance_safe` 或更高权限 toolset。
- 是否会写状态、调用外部平台、执行代码、移动资金或改变策略生命周期。
- 是否需要 ActionIntent、控制 token、人工确认或额外审计。
- Desktop 如何发现、展示和处理失败。

### 6.3 MCP 与 manager plane

MCP 和 manager 可以复杂，但对 Agent 和 Desktop 暴露必须稳定、可控、可审计。不要把 manager 内部复杂度泄漏到模型工具名、Desktop 文案或用户操作路径里。

金融 MCP 改动必须明确：

- 数据来源和质量信号。
- 工具副作用等级。
- 失败和降级语义。
- 是否允许直接执行，还是必须转为 intent。
- 是否会触发真实账户、真实外部平台或持久状态变化。

### 6.4 Strategy Factory 与三大工厂

策略生产链路必须保持“可追踪、可回放、可淘汰、可解释”。候选生成、数据收集、回测、质量门禁、去重、提交、淘汰、孵化和反馈都不能被 LLM 叙述替代。

默认不得把新策略链路直接推向实盘。策略进入更高阶段前必须有数据证据、验证口径、风险说明和人工可理解的状态。

### 6.5 Quant Core

量化核心改动必须保护计算语义、回测假设、指标定义、交易规则和存储语义。任何会改变历史结果解释、指标口径或样本范围的改动，都必须同步更新验证和说明。

### 6.6 外部金融连接器

外部金融软件连接器必须把行情/账户读取与下单/撤单等交易动作分开。交易动作必须保留显式确认、权限 guard、拒绝 envelope 和审计信息。

连接器异常不得被解释成交易成功、数据成功或策略成功。

---

## 7. 安全开发规范

安全不是最后的扫描动作，而是每次改动的设计约束。

1. 默认选择只读、最小权限、最小暴露面。
2. 新增权限、token、外部连接、动态执行、插件、webhook、browser、terminal、文件写入能力时，必须说明权限边界和失败模式。
3. 不把用户输入、模型输出、MCP 返回、外部网页、插件声明或历史记忆当成可信输入。
4. 所有跨边界输入必须经过 schema、allowlist、权限、资源限制或等价校验。
5. 所有敏感输出必须避免泄露 token、key、cookie、私钥、账户凭据、个人信息和内部路径细节。
6. 任何安全例外都必须是显式、临时、可审计、可回退的。
7. 新增功能不得要求用户靠“记住不要点错”来保证安全。

---

## 8. AI / Agent / MCP 规范

AIASK 的模型能力必须被当成受控执行系统，而不是普通文本生成器。

1. 模型不能直接获得底层 manager、broker、filesystem、terminal、browser、webhook 或外部平台的无约束控制权。
2. prompt、网页、文档、MCP resource、工具描述、插件声明和历史消息都可能包含恶意指令；工具执行必须服从系统策略，而不是服从内容本身。
3. MCP server、插件、skill、gateway 平台和外部工具必须有来源、权限、范围、失败审计和禁用路径。
4. 高风险工具必须具备用户可理解的确认信息：动作、对象、参数、影响、可撤销性和失败后果。
5. 模型输出进入金融结论、策略晋级、订单、外部消息或代码执行前，必须有结构化校验或人工确认。
6. general/full 能力必须被视为高权限模式，默认不能替代 finance-safe 模式。
7. 动态工具注册必须防止名称混淆、权限膨胀、tool poisoning 和 manager 泄漏。

---

## 9. 金融、数据与策略规范

金融系统的核心风险不是“没有答案”，而是“用错误数据给出看似确定的答案”。

1. 金融判断必须显式处理数据新鲜度、缺失、stale、fallback、placeholder、PIT、一致性和可解释性。
2. 数据不可得、字段不可得、上游限流、付费限制、延迟、降级和异常必须对调用方可见。
3. 回测必须有成本、基准、样本范围、交易规则、关键指标和风险口径。
4. 策略晋级必须有门禁，不得只凭 LLM 叙述、单次回测或单一指标。
5. 因子入池、衰减、淘汰和复活必须可追踪。
6. 孵化阶段必须记录前向表现、命中率、反馈和淘汰原因。
7. 任何生产策略路径都必须防止未来函数、样本泄漏、幸存者偏差和静默降级。
8. 真实交易动作必须和研究、回测、paper、sandbox 明确隔离。

---

## 10. API 与契约规范

跨包、跨进程、Desktop/Agent、Agent/MCP、Agent/外部平台之间的改动，优先稳定契约，再修改实现。

1. 新增或修改 API 必须定义请求、响应、错误、权限、幂等性和降级语义。
2. 不要让前端通过猜测错误字符串判断业务状态；状态应结构化。
3. 不要把后端内部异常、堆栈、凭据、真实路径或第三方原始错误直接暴露给用户。
4. 读接口和写接口要分开；写接口必须清楚标记副作用和确认要求。
5. endpoint drift 不是自动等于缺陷，但必须分类、解释和纳入契约治理。
6. 新字段必须考虑兼容、默认值、缺省语义和旧客户端行为。

---

## 11. Desktop / Tauri 规范

Desktop 的安全边界由 Agent HTTP 契约、Tauri capabilities、CSP、token 使用和 UI 审批共同构成。

1. 不为方便开发扩大系统权限、文件系统权限、shell 权限或网络范围。
2. CSP 放开必须有明确原因，并保持最小连接域和最小资源类型。
3. Tauri capabilities 必须按窗口和能力收敛，不使用宽泛权限兜底。
4. Desktop 不保存、展示或记录真实敏感凭据。
5. UI 必须清楚区分 mock、degraded、live、dry-run、paper、sandbox 和 trade-risk。
6. Desktop 上的“执行”按钮不能绕过后端权限和 ActionIntent。
7. 前端 mock 只能服务开发体验，不能成为真实能力已完成的证据。

---

## 12. 供应链与依赖规范

AIASK 同时包含 Python、Node、Rust/Tauri 和金融外部依赖，供应链风险必须前移处理。

1. 依赖新增要说明用途、权限面、运行时影响和替代方案。
2. 不引入无人维护、来源不明、权限过宽或会隐式执行外部代码的依赖。
3. 锁文件、构建配置、CI 权限和发布产物必须可追踪。
4. 自动化 workflow 默认最小 token 权限，不把 secret 暴露给不可信脚本、fork 或动态命令。
5. 依赖漏洞、已知 advisory 和安全扫描结果要有处理路径，不用“暂时忽略”长期沉淀。
6. 生成代码、二进制、模型文件、数据快照和外部包进入仓库前必须说明来源和更新策略。

---

## 13. 测试与验证规范

每次改动都要选择覆盖风险面的最小验证集合。无法执行验证时，必须在最终说明中写明原因和剩余风险。

验证选择按风险分级：

- 文档-only：检查路径、标题、引用、敏感信息和 Markdown 基本质量。
- Desktop：至少覆盖类型检查、组件行为或构建中的一种；涉及关键工作流时补 UI 测试。
- Agent：覆盖工具注册、toolset、权限、envelope、ActionIntent 和 HTTP 契约。
- MCP/manager：覆盖副作用分类、失败 envelope、数据质量和 Agent 包装。
- 策略/量化：覆盖数据门禁、回测口径、策略生命周期和结果可解释性。
- 交易/外部平台：必须覆盖拒绝路径、确认路径、token guard、审计字段和 negative tests。
- 跨包改动：组合验证相关包，并说明未覆盖边界。

“测试通过”必须具体到执行了什么类型的验证；“未跑测试”必须具体到为什么没有跑。

---

## 14. 运维与可观测性规范

上线能力必须能被观察、诊断和恢复。

1. 关键链路必须暴露健康、状态、降级、错误原因和最近运行证据。
2. trace、run、intent、job、strategy、data gate 等状态必须能关联到用户可理解的事件。
3. 后台任务、runner、gateway、webhook 和外部平台集成必须有停止、重试、失败审计和人工接管路径。
4. 生产或准生产问题不能只靠日志排查；需要最小 runbook 和验证命令。
5. readiness 报告必须区分静态图谱结论、测试结论、运行态结论和真实交易验收。
6. 指标或告警缺失本身就是上线风险，不能用“功能可用”覆盖。

---

## 15. 文档与报告规范

1. 文档必须服务当前开发阶段，不写过期实现细节。
2. AGENT.md 写长期规范；具体方案、审计、诊断、报告写入 `docs/`。
3. 架构报告必须先讲实际架构、已实现能力、业务闭环和价值，再讲风险。
4. 问题报告必须包含证据、影响、建议动作、验证方式和风险等级。
5. 上线评审必须明确评审口径，不等同于生产压测、红队测试或真实交易验收。
6. 文档更新不能替代代码验证；新增能力必须同步更新用户可见契约或使用说明。
7. 不在文档、测试、mock、截图或报告中写真实密钥值。

---

## 16. Graphify / Code Graph 使用规范

本仓库已沉淀 `aiask-graphify-architecture` 技能，用于把 Graphify/code graph 产物转成可复用的架构理解、能力盘点、影响分析和上线评审。

使用口径：

1. 需要整体架构、能力地图、跨包影响、endpoint 审计或上线评审时，应优先使用该技能。
2. Graph 事实是证据，不是最终真相；关键结论必须回到代码、manifest 或测试确认。
3. 报告不能默认写成问题清单；必须先说明项目实际架构、已实现功能、业务闭环和价值。
4. endpoint drift 要先分类，再判断是否缺陷。
5. cross-package edges 用于判断影响范围和契约测试，不直接等同坏味道。
6. 分析过程中不读取 `.env`，不引用密钥值，不操作运行态数据。

推荐报告顺序：

1. 执行摘要
2. 项目实际定位
3. 实际架构
4. 已实现功能地图
5. 能支撑的业务闭环
6. 对我们项目的帮助
7. 系统架构评审
8. 上线就绪度矩阵
9. 关键上线约束与风险
10. 测试与验证缺口
11. 行动计划

---

## 17. AI 编码代理工作规则

AI 编码代理在本仓库工作时必须遵守：

1. 先读代码、manifest、测试和当前规范，再提出或执行改动。
2. 不凭历史对话记忆判断当前实现。
3. 优先使用 `rg` 搜索，避免盲猜路径。
4. 修改前明确影响范围；修改后说明验证结果。
5. 不回滚用户或他人改动，除非用户明确要求。
6. 不把无关格式化、重命名、迁移混入功能改动。
7. 不用宽泛异常吞错，不用 silent fallback 掩盖失败。
8. 不把实现细节塞回 AGENT.md；长期规范写这里，细节写专题文档。
9. 对用户要求的报告，先确认目标属于项目理解、架构说明、上线评审、问题诊断还是实施计划。
10. 发现同类失败连续出现时，停止重复试错，重新分析根因。

---

## 18. 当前开发优先级

当前阶段优先级从高到低：

1. 关闭上线阻断风险和高风险交易路径。
2. 收敛 Desktop 与 Agent 的接口契约。
3. 强化数据 freshness、质量门禁和降级可见性。
4. 稳定 Strategy Factory、Factor Mining Factory、Incubation Factory 的 dry-run/paper 闭环。
5. 完善测试、可观测性、runbook、readiness 和 endpoint drift 治理。
6. 收敛技能、插件、MCP、gateway、general/full 能力的权限与审计边界。
7. 在边界稳定后再扩展新功能。

---

## 19. 修订规则

AGENT.md 只保留长期稳定规范。任何人或 AI 如果发现本文与当前代码、实际流程或开发阶段不一致，必须在同次改动中修正。

修订 AGENT.md 前必须至少复核：

1. 当前 package manifests 与 Desktop/Tauri 配置。
2. Agent 工具策略、ActionIntent、MCP 聚合和交易 guard 相关代码。
3. Graphify/code graph 的 curated summary、endpoint map 和跨包依赖事实。
4. 最近的诊断、readiness、runbook 和用户明确选择的项目方向。
5. 相关外部安全、Agent、API、供应链和 AI 风险基线是否有重大更新。

文档准确性优先于完整性；规范清晰优先于细节堆叠；安全边界优先于功能速度。
