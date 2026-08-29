# AIASK 宿主无关化与独立发布：全量开发方案

状态：**方案（已按代码实测校准）** · 日期：2026-08-28 · 基线提交：`bc24eef`

本文档是可执行的开发方案，不是愿景陈述。每条结论都标注了代码证据；与原始计划书冲突处以本文档为准，冲突原因见 §1。

---

## 1. 与原计划书的五处偏差（实测校准）

原计划书的阶段划分基于若干与代码不符的前提。照原顺序执行会做无用功或得到半迁移状态。

| # | 原计划书假设 | 实测结论 | 影响 |
|---|---|---|---|
| 1 | Phase 3「MCP host 瘦身」待做 | **已完成**。`akshare_mcp/server.py:545-554` 已移除四工厂/SignalTracker/MatchingEngine/NavEngine 自动启动，env toggle 只打日志 | Phase 3 降级为验证，不写代码 |
| 2 | `aiask` CLI 是四工厂权威控制面 | **原本没有 `factory` 子命令**。控制面全在 Hermes 插件（`hermes-plugin/aiask-hermes-plugin/cli.py:151` + `readiness.py` 2969 行） | Phase 4「hermes 变 shim」在反转不存在的依赖；建 CLI 属 Phase 1 |
| 3 | 工厂域 schema 在 `akshare-mcp` | **在 `aiask-quant-core`**：`storage/sqlite/schema_strategy.py` 1641 行 / 57 表定义（实测建 56 张，51 张属工厂域） | 搬迁源头不同；quant-core storage 占 26.5k/39.8k 行，是三分之二重写 |
| 4 | 需打破 `akshare-mcp ↔ strategy-factory` 循环依赖 | **不存在循环**。实测 DAG 无环：`quant-core ← strategy-factory ← akshare-mcp ← agent`，且 `akshare_mcp` 对 sf 的 121 处引用**全部**走 `.api.*`/`.runtime.*` 公共面，零 `application`/`infrastructure`/`domain` 泄漏 | 大幅降风险：不需要解环，只需搬实现 |
| 5 | `schema_strategy` 缺 migration ledger | **已有 checkpoint**（`schema_base.py:678-683`，namespace `strategy_runtime`）。真实缺口是它只 sha256 `(namespace, key, source_module)` 字符串、漂移时主动刷新 checksum，**检测不到表被改名/删除/挪走** | 需要的是结构指纹，不是新建 ledger |

### 1.1 一条方法论结论

**解耦程度必须跑，不能只 grep。** 现有静态守卫全绿，但 `runtime/default_bootstrap.py:21-24` 用**字符串**动态 import `akshare_mcp.adapters.strategy_factory_runtime` 解析全部 19 个必需 provider——正则看不见。同一个盲点让 Hermes 侧边界守卫漏掉 `importlib.import_module()`。任何"已解耦"声明都必须有可执行证据。

---

## 2. 目标形态与依赖 DAG

```text
aiask-quant-core        计算/回测/指标/风险 primitive + 通用 storage primitive
        ↑
strategy-factory        四工厂编排 + phase 状态机 + 运行 DTO + 工厂域 schema/账本/证据
        ↑
data provider           AKShare/券商连接器/行情表/外部数据缓存（现 akshare-mcp）
        ↑
MCP servers + aiask CLI 查询与受控触发 + 权威控制面
        ↑
Host Adapter            Hermes / 标准 MCP 宿主 / 其他 Agent
```

四工厂固定为 Strategy Factory、Factor Mining、Incubation、Market Event Ingest。**SignalTracker 是 sidecar，不计入四工厂**——已由 `strategy-factory/tests/test_factory_runner_topology.py` 和 `agent/tests/test_factory_cli_contract.py` 双向锁定。

### 2.1 当前依赖实测（基线）

| 包 | → quant-core | → strategy-factory | → akshare-mcp |
|---|---|---|---|
| `aiask_quant_core` | — | 0 | 0 |
| `strategy_factory` | 14 | — | **1**（仅 `default_bootstrap` 的动态字符串） |
| `akshare_mcp` | 23 | 121（全走公共面） | — |
| `aiask_agent` | 3 | 10 | 20 |

---

## 3. 阶段总览与状态

| 阶段 | 内容 | 状态 | 剩余体量 |
|---|---|---|---|
| Phase 0 | 契约冻结、可执行边界、迁移安全网 | **已完成** | 0 |
| Phase 1 | JSON envelope + CLI + **独立运行退出条件** | **已完成**（xfail 已转通过） | 0 |
| Phase 2 | 工厂域 schema 与 Repository 搬迁 | Protocol 拆分 + 指纹落库**已完成**；表搬迁待做 | 51 表 / ~4.1k 行 repo |
| Phase 3 | 四工厂编排搬迁 | 前置协议**已补齐**、私有穿透**已清零**、stage 状态机/生命周期/晋升门**已搬**；余下 A 类待做 | ~5.4k 行（A 类） |
| Phase 4 | MCP host 瘦身 | **基本已完成**，需验证 | 0 |
| Phase 5 | Hermes adapter 收薄 | 部分完成（样板已建） | ~3 处 |
| Phase 6 | 独立仓库 + 第二宿主 | `aiask-skills`、`aiask-agent-reference`、中性 factory MCP、clean-isolation fixture **已完成**；独立仓库创建和真实第三方宿主仍待执行 | 仓库层操作 |

---

## 4. Phase 0（已完成）：契约冻结与安全网

### 4.1 已交付

| 交付物 | 位置 | 作用 |
|---|---|---|
| 可执行边界测试 | `strategy-factory/tests/test_runtime_executable_boundary.py` | 子进程 `sys.meta_path` 阻断 `akshare_mcp`，真跑契约而非 grep |
| Phase 1 退出条件哨兵 | 同上 `test_phase1_exit_condition_real_cycle_without_the_host` | `xfail(strict=True)`——provider 搬完后**转为失败**，强制更新阶段状态 |
| 动态 import 守卫 | `tests/test_aiask_integration.py` | AST 扫 `importlib.import_module()`/`__import__()`，堵住正则盲点；并补 `cron/`、`acp_adapter/` 覆盖断言 |
| 结构指纹 | `aiask-quant-core/.../factory_schema_fingerprint.py` | 表/列级指纹 + diff + `assert_migration_preserved_structure`；对真实 56 表验证过 |

### 4.2 两个已修的阻塞 bug

1. **`--daemon` 崩溃**：`IncubationRuntime` 缺 `run_daemon`，而两个 supervisor 都用该模式启动孵化。修时发现 legacy `incubation_factory/runner.py:648` 委托回 `runtime.run_daemon()`——补方法本会变成**无限递归**。已把 daemon 循环下沉进包内（按日 `run_time` 调度、300s 退避沿用 legacy 常量、`try/finally` 保证 paper-trading engine 关闭），并修掉 `build_incubation_runtime(run_time=...)` 参数被丢弃。
2. **`--notice-days` 空操作**：modern 分支只认 `lookback_days`，`notice_days` 被 `**kwargs` 吞掉，窗口永远默认 7 天。已在 runtime 边界做别名折叠，两条分支语义统一。

### 4.3.1 已达成：Phase 1 退出条件（严格 xfail 已转为通过）

`runtime/standalone_providers.py` 提供**仅用 Strategy Factory + quant-core** 实现的完整 19 provider 集合。实测（子进程 `sys.meta_path` 阻断 `akshare_mcp`）：

```text
缺失 required: []
factor_mining / incubation / signal_tracker / market_event_ingest
  → preflight/status/run_once 全部完成，零 error
akshare_mcp 已加载? False
```

那条 `xfail(strict=True)` 哨兵**已删除并替换为真实断言**（`test_phase1_exit_condition_real_cycle_without_the_host`），strategy-factory 现为 **937 passed、0 xfailed**。

设计原则是**最小而非伪造**：`db_provider` 是真实 quant-core SQLite adapter（schema/ledger 真写入）；引擎类 provider 返回空结果而非编造数据；每处降级都带 `standalone_no_data_provider` 标记，空 cycle 不可能被误读为真实 cycle。

它**不是** AKShare 宿主的替代品——不能挖因子、取行情、跑 paper trade。作用是让契约可执行/可测，并给第二宿主一个可逐 provider 覆盖的基线。

搬迁中顺带修好两处：`MarketEventIngestOrchestrator._get_db` 与 incubation 同款私有穿透（现优先公共 `get_db`）；`build_mining_context` 的参数名与 orchestrator 的关键字调用不匹配。

### 4.3 Phase 1 退出条件的正确表述（历史记录）

原计划书的退出条件「strategy-factory 可在无 Hermes、无 `akshare-mcp.server` 下完成空数据 preflight/status/run_once」**不可用注入 stub 来证明**：

- 完全无 provider 时，`get_*_runtime()` 抛 `RuntimeError: missing_provider=...`
- 一旦注入返回 `None` 的 stub，契约返回结构化 `support_missing` 而**不抛错**

所以"契约不崩"与"能独立跑真实 cycle"是两件事。已分开固化：`test_four_runtimes_degrade_structurally_without_the_akshare_host` 断言前者，`test_phase1_exit_condition_real_cycle_without_the_host`（xfail strict）锁定后者。

---

## 5. Phase 1（已完成）：JSON envelope 与权威控制面

### 5.1 治理 envelope

`strategy-factory/src/strategy_factory/contracts/run_envelope.py`。字段全部必填，缺一即协议破坏：

```text
protocol_version  runtime  run_id  status  started_at  finished_at
counts  degraded_reasons  errors  evidence_refs  data
```

设计要点：

- `status` 为闭合词表：`success|partial|degraded|failed|blocked|skipped`。`normalize_status()` **fail closed**——未知值降为 `failed`，不乐观判成功。
- `degraded_reasons` 非空 = 跑了但不可当干净成功；与 `errors`（真失败）分开。
- 退出码：**0** 干净 / **1** 失败 / **2** 完成但不干净。调度器可区分三态。
- `ok` 只对 `success` 为真。`partial`/`degraded` 一律为假。

写测试时抓出并修掉三处 fail-open：
1. 非 dict 结果（runtime 崩了返回 `None`）曾判 `success` → 现 `failed` 并记 `runtime_returned_non_mapping`
2. 空 dict `{}` 曾判 `success` → 现 `failed` 并记 `runtime_returned_empty_payload`
3. `finish("success")` 会覆盖已记录的 degradation → 现显式成功声明也不能盖掉已记录问题

### 5.2 `aiask factory` 控制面

`agent/src/aiask_agent/factory_cli.py`，挂在 `aiask` 入口下：

| 子命令 | 作用 |
|---|---|
| `once` | 每个选定 runtime 跑一遍 |
| `cycle` | 顺序过一遍，支持 `--stop-on-failure` |
| `status` | 报告 runner 存在性与拓扑，不启动任何 cycle |
| `preflight` | **`preflight()` 此前零调用点**，现由此暴露 |
| `evidence` | 只读汇总证据账本 |
| `schedule-template` | 输出全部注释掉的 cron 样本，不安装 |

安全性质：子进程强制 `LIVE_TRADING_ENABLED=0`；`--only` 拒绝未知 runtime；sidecar 需 `--with-sidecar` 显式加入。

### 5.3 Hermes 插件委托样板

`readiness.py` 新增 `run_aiask_cli()`，复用插件既有 venv 解析/研究安全 env/脱敏，命令形态为 `python -m aiask_agent.cli factory ...`。已实测打通 `hermes aiask factory preflight` → `aiask factory preflight` → 治理 envelope。degraded/partial 不会被洗成 `ok`。

---

## 6. Phase 2（待做）：工厂域 schema 与 Repository 搬迁

### 6.1 表归属分界

`schema_strategy.py` 实测建 56 张表，**51 张属工厂域**。未覆盖的 5 张正确排除：

- `vector_index_registry`、`strategy_vector_index_items`、`strategy_vector_index_snapshots`、`strategy_vector_profiles` → **vector 域**，有独立 `schema_vector.py` 与 `vector_runtime` namespace
- `sqlite_sequence` → sqlite 内部表

行情表（`stock_daily_bars`、`index_daily_bars`、`north_fund_flows` 等）**不随工厂域走**，已由 `test_market_data_tables_are_not_claimed_by_the_factory_domain` 锁定。

### 6.2 Repository Protocol 必须先拆

`aiask_quant_core/storage/contracts.py` 的 `StrategyFactoryRepository` 混了两个域，**36 个方法中 4 个属行情域**：

| 应留在 data provider | 应随 strategy-factory 走 |
|---|---|
| `get_klines`、`get_limit_up_stats`、`get_factor_ic_history`、`list_stock_universe` | 其余 32 个 |

**不拆就搬会让 strategy-factory 连带拥有行情表读取**，违背 §2 的 DAG。拆法：

```text
StrategyFactoryRepository (32)   → strategy_factory.contracts.repository
MarketDataRepository (4)         → 留 quant-core / provider 侧
```

### 6.3 搬迁步骤

**2.1 已完成**。`contracts.py` 现声明三组：

```text
MARKET_DATA_REPOSITORY_METHODS      (4)   行情读，留 data provider
FACTORY_DOMAIN_REPOSITORY_METHODS   (32)  派生 = 联合 − 行情，随工厂域走
REQUIRED_REPOSITORY_METHODS         (36)  联合，保持向后兼容
```

工厂域清单是**派生**的（不是手工维护第二份），所以两份不可能漂移。新增 `MarketDataRepository` Protocol，`runtime_checkable`，并经 `strategy_factory.api.contracts` 导出（含 quant-core 缺失时的 fallback 定义）。

关键澄清：`strategy-factory` **确实在消费**这 4 个行情方法（`application/collect.py:1168,1180`、`application/event_engine.py:552`）。所以拆分不是剥离调用，而是分离**所有权**——消费合法，拥有不合法。由 `test_repository_protocol_split.py` 13 项锁定。

| 步 | 动作 | 验收 |
|---|---|---|
| ~~2.1~~ | ~~拆 Protocol~~ | **已完成** |
| ~~2.4~~ | ~~结构指纹落库~~ | **已完成**（见 §6.3.1） |
| 2.2 | `strategy_ai_repos/` 12 文件 4070 行按域搬迁 | 指纹前后一致 |
| 2.3 | `schema_strategy.py` 51 张工厂表移入 strategy-factory | `assert_migration_preserved_structure` 通过 |

### 6.3.1 已完成：结构指纹落库 + vector 边界厘清

`record_factory_schema_fingerprint()` 把结构指纹写入 `mcp_schema_migrations`（namespace `factory_domain_structure`）。与既有字符串 checkpoint 的关键差别：**漂移时报告而非静默刷新** —— 返回 `previous_fingerprint` / `changed`，由调用方判断是合法迁移还是回归。首次写入幂等，重复调用不改记录。

**vector 表边界已厘清**（实测修正）：`schema_strategy.py` 建的 6 张 `strategy_vector_*` / `vector_index_registry` 表属**宿主 vector 平台**，不属工厂域。判据是引用计数——`strategy-factory` 对它们**零引用**，`akshare-mcp` 全部在用。注意这 6 张与 `schema_vector.py` 建的 `vector_*` 表是**两套不同的表**，仅命名相似。现由 `VECTOR_PLATFORM_TABLES` 显式排除（优先级高于 `strategy_` 前缀匹配），而非依赖"不在清单里"的隐式排除。

### 6.3.3 已完成：工厂域持久化门面（渐进式搬迁第一步）

`strategy_factory/infrastructure/factory_repository.py`，经 `strategy_factory.api.runtime` 导出。Strategy Factory 由此**拥有工厂域持久化契约**，但不拥有数据库驱动。

为何用门面而非物理搬 mixin：那些 mixin 建立在宿主基础设施上（`self.acquire()`、`self._decode_json_field`、连接池），搬走会连带搬走连接池；且宿主经鸭子类型跨 121 处调用，一次性搬移难回滚。门面只**转发**不复制 SQL，因此不存在两份实现漂移的可能。

三条性质由测试锁定（26 项）：

1. **拒绝行情读**——即便注入的连接提供 `get_klines`，门面也拒绝，不能成为 provider 私有表的后门
2. **缺失能力结构化上报**——`MissingCapability` 带方法名，而非编排路径深处抛 `AttributeError`
3. **真实适配器 32/32 满足**——`SQLiteAdapter` 实测完全覆盖工厂域契约，门面可直接替换裸连接，无需改宿主

已接入第一个消费者：`aiask factory preflight` 现输出 `data.storage`（`declared/available/missing`），缺失能力降级为 `storage_capability_missing:<name>`，使存储问题在 cycle 启动**前**暴露。

### 6.3.5 已完成：schema DDL 实体所有权迁移

`schema_strategy.py` 1641 行**已物理搬入** `strategy_factory/infrastructure/factory_schema_ddl.py`。搬迁前核实它完全自包含（只依赖 `logging` + `conn.execute`，零其他 import），所以搬迁是机械的。

验证：搬迁前后**结构指纹完全一致**（`sha256:6cb58106...`），建表数 56 不变，工厂域表 51 不变。

quant-core 侧留再导出薄壳（`FactorySchemaUnavailable` 给出可操作的错误信息），并**惰性**引用——实测导入 quant-core 时不触发 strategy-factory，无循环依赖。同时保证 quant-core 在 strategy-factory 缺失时仍可独立导入（它是 DAG 底层，只要行情数据的调用方不该被迫装工厂包）。

DAG 守卫抓到这个 shim 违反"quant-core 是叶子"——这是搬迁的真实代价，已作为**具名受控例外**处理（与 `default_bootstrap.py` 同一模式），并补两条约束测试：shim 必须惰性引用（否则成环）、quant-core 必须能在无 strategy-factory 时导入。

### 6.3.4 已完成：schema 所有权注册转移

`schema_base._init_tables` 原先硬编码 `from .schema_strategy import init_strategy_tables`，使 quant-core 成为 51 张工厂域表的结构所有者。

已改为**注册表查找**（`schema_providers.py`）：域可注册自己的 initializer，quant-core 在无注册时回退到自带副本。`strategy_factory.infrastructure.factory_schema` 现为注册的所有者，且 `ensure_default_runtime_services()` 在每条 bootstrap 路径自动注册。

实测：owner 从 `None` → `strategy_factory.infrastructure.factory_schema`，注册的 initializer 真能建 56 张表，migration ledger 记录的 `source_module` 随之变化。

**DDL 文本仍在 quant-core，这是有意的**：1641 行定义 51 张在用表，复制会产生两份互相漂移的定义——最坏的中间态。所以先转移**所有权**（本模块已是结构权威），DDL 正文之后作为机械搬迁，由结构指纹验证。

由 `test_schema_providers.py` 13 项 + `test_factory_schema_ownership.py` 12 项锁定。

### 6.3.2 2.2/2.3 的实施风险（实测后修正评估）

`strategy_ai_repos/` 是 **mixin 组合**：`SQLiteAdapter` → `strategy.py` → `StrategyAIMixin` → 11 个 repo mixin。宿主 121 处引用依赖 `db.save_strategy_factory_run()` 这类**鸭子类型**调用直接可用。物理搬走 mixin 会同时改变 quant-core `SQLiteAdapter` 的能力面，属高风险、难回滚操作。

建议实施方式：**先在 strategy-factory 建 repository 实现并让宿主 adapter 转发**，两侧并存跑通指纹比对后再摘除旧 mixin，而不是一次性搬移。

**安全网用法**（Phase 0 已就位）：

```python
before = await collect_factory_schema(conn)
# ... 执行迁移 ...
after = await collect_factory_schema(conn)
assert_migration_preserved_structure(before, after)   # 掉表/掉列/改类型即失败
```

### 6.4 退出条件（可执行）

`test_phase1_exit_condition_real_cycle_without_the_host` 的 xfail **转为通过**（因 strict=True 会报失败，此时移除 xfail 标记）。即：`ensure_default_runtime_services()` 不再需要 `akshare_mcp` 即可完成真实空数据 cycle。

---

## 7. Phase 3（待做）：四工厂编排搬迁

### 7.1 唯一注册点——本阶段最大的风险缓解

全部 provider 在**一处**注册：`akshare-mcp/src/akshare_mcp/adapters/strategy_factory_runtime.py:152` 的 `configure_runtime_services(...)`（共 38 个 provider，其中 19 个为必需）。

**搬迁 = 替换这一个注册点，不是逐个改调用方。** 这是 Phase 3 可控的根本原因。

### 7.2 Factor Mining 拆分（9375 + 1167 行）

| 归属 | 文件 | 行数 |
|---|---|---|
| **A 搬走**（编排/准入/门禁） | `factory.py` 1074、`api.py` 389、`qc_pipeline.py` 384、`quality.py` 261、`blueprints.py` 245、`quick_evidence.py` 225、`scheduler.py` 172、`reappraise.py` 168、`pool/active_pool.py` 615 | ~3533 |
| **A 搬走**（混合文件，需方法级切） | `factor_scheduler.py`（stage 状态机 `:176-300`、门禁 `:419-484` 搬；`DEFAULT_UNIVERSE`、`_load_dynamic_universe`、`get_factor_scheduler` 留） | ~700/1167 |
| **B 留下**（计算/引擎） | `engines/` 2940、`sandbox/` 519、`evolution/` 274、`validation/` 407、`feedback/knowledge_graph.py`+`meta_learner.py`、`pool/{decay_tracker,orthogonalizer,portfolio_optimizer}.py` | ~4600 |
| **C 留下**（数据访问） | `engines/context.py` 315、`pool/storage.py` 351、`feedback/decay_monitor.py` 294、`feedback/performance_writer.py` 127 | ~1087 |
| **D 留下**（宿主胶水） | 各 `__init__.py` | ~65 |

### 7.3 需新增的 provider 协议方法（否则 A 搬走会成环）

A 类代码通过宿主相对 import 调 B/C，搬走前必须补协议：

| 缺口 | 现状调用点 | 需新增协议方法 |
|---|---|---|
| QC 三件套 | `factory.py:133-137` 直调 `...tools.quant_analysis` | `run_factor_oos_validation`、`run_factor_group_backtest`、`run_factor_robustness_check` |
| 强校验流水线 | `factory.py:484`、`:638` 调 `..factor_validation_pipeline` | `validate_factor_candidate(db, candidate, **kw)` |
| 因子池存储/优化 | `api.py:106`、`:150`、`:175`、`:345` | `load_factor_pool`、`optimize_factor_weights`、`write_factor_performance` |
| 私有状态穿透 | `api.py:158` 读 `factory._active_pool._factors` | `get_factor_metrics(factor_ids)` |
| DSL 编译门 | `pool/active_pool.py:113`、`:208` | `compile_factor_candidate(candidate)` |
| LLM provider | `factor_scheduler.py:305` | `get_factor_llm_provider()` |

### 7.3.1 已完成：协议补齐与私有穿透消除

上表全部 6 组已落地，共新增 **15 个协议方法**（`application/factor_mining/provider.py`），并在 `runtime/provider_adapters.py::LegacyFactorMiningProvider` 实现桥接——旧宿主无需改动即可继续工作（adapter 用 `getattr` 读宿主私有方法，缺失时结构化降级为 `skipped` 而非崩溃）。

**`run_maintenance` 的零协议穿透已消除**：原先穿透 7 个私有属性（`_decay_monitor`、`_persist_decay_report`、`_persist_decay_updates`、`_promote_quarantine_factors`、`_run_qc_pipeline`、`_active_pool`、`_get_db`），现在只调公共协议。`factory._active_pool._factors` 这处两级私有访问也收敛进 adapter 的 `get_factor_metrics()`。

由 `test_factor_mining_provider_protocol.py` 39 项锁定，含一条**源码级**守卫：断言 `run_maintenance` 源码里不出现 `support._x` / `provider._x` 形式的穿透（匹配持有者前缀而非裸名，否则 `get_active_pool_size` 会误报）。该守卫经植入回归验证有效。

### 7.4 Incubation + Sidecar 拆分（6914 + 2059 行）

`runner.py` 2363 行是最大单块，需方法级切分：`run_once`/`run_daemon` 已委托 runtime（保留薄壳），phase 协调与 promotion/blocker 判定搬走，`_get_db`/`_persist_*`/`_settle_strategy_orders` 留下。

`matching_engine.py` 405 + `nav_engine.py` 187 为 **B 类**（撮合/净值计算），保持注入。其生命周期已由 `IncubationRuntime.start()/stop()` 通过 `_start_paper_trading_daemons` 控制（Phase 0 的 `run_daemon` 已保证 `try/finally` 关闭）。

`signal_tracker.py` 1467 行：phase A-H 协调器搬走，运行拓扑保持 sidecar。

### 7.3.2 已完成：stage 状态机搬迁（A 类第一批）

`strategy_factory/domain/stage_status.py`，经 `api.runtime` 导出。搬走 6 个函数、约 220 行决策逻辑：

```text
normalize_stage_status   build_stage_result      summarize_stage_results
resolve_run_status       quality_status          build_run_summary
```

选它作为第一批的依据：**依赖闭包完全纯净**——无 db、无宿主服务、无 I/O，只互相调用。搬迁前逐个核实过。

宿主侧 `factor_scheduler.py` **净减 82 行**（−111/+29），6 个方法全部变成 2-9 行薄壳，保持 `cls._x()` 调用形态，因此 12 处内部调用点**一行未改**。

验证方式（三层）：
1. 宿主侧 70 项测试（含此前的 44 项 fail-closed）全过 → 委托后行为一致
2. strategy-factory 侧新增 55 项 → 契约在新家被独立锁定
3. 子进程阻断 `akshare_mcp` 后逻辑仍可用 → 真正独立，不只是移动了文件

`test_stage_status_domain.py` 末尾有一条纯度守卫：断言模块源码不含 `await`/`async def`/`sqlite3`/`akshare`/`requests`——防止未来往回加 I/O 依赖，否则它就不再可搬。

### 7.3.3 已完成：A 类第二、三批

**观察生命周期策略**（`domain/observation_lifecycle.py`，199 行）——observe/promote/halt 日决策。搬前核实：零 db、零 async、零 env，唯一调用点 `metrics_recorder.py`。宿主侧 199 → 23 行再导出薄壳。

**晋升门禁**（`domain/promotion_gate.py`）——DSR 多重检验校正决策。这是首个**拆分式**搬迁：决策搬走，取数留下。

拆分依据：`promotion_gate.py` 是「纯决策 + 尾部 async 取数」混合。`PromotionGate.evaluate` 的 DSR 已是可注入参数（`dsr_fn`），只有 fallback 走宿主 import；而 `deflated_sharpe_ratio` 在 **quant-core 也有一份且签名完全一致**，所以搬迁后直接从 quant-core 解析，依赖方向合法、无需宿主注入。`fetch_forward_return_series`（读 `signal_forward_returns`）属数据访问，留在宿主。

其 env 名本就是 `STRATEGY_FACTORY_PROMOTION_DSR_ENABLED` 前缀，语义上早已属 strategy-factory。

保守性由测试专门锁定：样本不足、统计不可用、抛异常三种情况都必须 `passed=False`——晋升绝不能因为计算失败而被授予。

**暂缓 `quality.py`（261 行）**：它有 6+ 处外部调用方，且 env 名是 `AKSHARE_QUALITY_PROFILE`。搬到中立包会让宿主专属 env 名进入 strategy-factory。需先决定是否改名（破坏性变更），故不在本批。

### 7.4.1 已完成：Incubation 编排的最后一处私有穿透

实测修正：`IncubationOrchestrator` 的私有穿透**只剩 1 处**（`_get_db`），其源码注释自己标注为「Phase 3 应改为显式注入」。说明 Incubation 的编排搬迁完成度远高于原方案估计。

已处理：`get_db()` 加入 `IncubationRuntimeSupport` 协议并优先使用，下划线名保留为旧宿主 fallback；`LegacyIncubationProvider` 同时暴露两个名字。由 `test_incubation_provider_get_db.py` 7 项锁定，含一条「除该 fallback 外不得有其他私有穿透」的源码守卫。

### 7.4.2 已完成：root runner 提供方中立化

五个 runner 原先硬编码 `packages/akshare-mcp/src` 路径注入 + 直接 `import akshare_mcp.env_loader`——这是"root runner 不再经过 AKShare"这一条最后未闭合的地方：它们驱动的 runtime 契约是中立的，但脚本本身不是。

新增 `scripts/factories/_runner_bootstrap.py`：按**存在性**注入包、按**可用性**选 provider 加载 env。实测两种情况：

```text
有 provider：注入 4 包，provider=akshare-mcp，env loaded=True
无 provider：provider=None，reason=no_data_provider_installed，仍正常启动
```

五个 runner 全部 `--help` 通过，残余硬编码计数为 0。由 `test_runner_provider_neutrality.py` 25 项锁定。

搬迁中修掉一处自己引入的缺陷：批量替换吃掉了 `run_market_event_ingest.py` 的 `_load_dotenv` 定义，已用中性版本补回。

### 7.5 建议执行顺序（按依赖）

1. ~~补 §7.3 的 6 组协议方法~~ — **已完成**（15 个方法）
2. ~~消除 `run_maintenance` 私有穿透~~ — **已完成**
3. ~~消除 Incubation `_get_db` 穿透~~ — **已完成**
4. 搬 Factor Mining A 类（~3533 行）
5. 搬 Incubation A 类（`runner.py` 2363 行，方法级）
6. 搬 SignalTracker phase 协调器
7. 替换 §7.1 单一注册点

### 7.6 退出条件

所有 root runner 直接调 `strategy_factory.runtime.*`，不经 AKShare runner；`test_runtime_executable_boundary` 全绿且 xfail 已移除。

---

## 8. Phase 4（基本已完成）：MCP host 瘦身

`server.py:545-554` 已移除四工厂自动启动。**本阶段只需验证，不写代码**：

- 删除 MCP server 后四工厂仍可经 CLI 独立运行
- 启动 MCP 不隐式启动工厂
- 已知残留：`akshare_mcp/storage/__init__.py:17-21` 在 import 时调 `ensure_default_runtime_services()`——这是"导入 akshare 即装配 provider"的来源，Phase 3 完成后应移除

---

## 9. Phase 5（部分完成）：Hermes adapter 收薄

### 9.1 硬阻塞：零

四个可安装包对 Hermes **import 干净**，已由 `agent/tests/test_no_hermes_dependency.py` 从 AIASK 侧守卫（字面 token `hermes_cli` 出现即失败）。所有 `parents[N]` 路径回溯均落在 `integrations/aiask`，从不越界。

包内 `hermes` 字样均为非耦合：`capabilities.py` 的静态 parity 台账、`"hermes-aiask"` 运行目录名、`AIASK_` 前缀 env 名。

### 9.2 真实耦合三处（均在包外）

| 位置 | 性质 | 处置 |
|---|---|---|
| `hermes-plugin/.../readiness.py:794,795,841,1155-1157,1310` | 7 处惰性 `hermes_cli`/`hermes_constants` import，全为 Hermes profile 安装 | 独立构建时按定义不发布 |
| `scripts/install_hermes_plugin.py:19-31` | 唯一 `sys.path` 越界到 Hermes repo root | 仅插件 bootstrap 用 |
| `scripts/aiask_readiness.py:97-103` | `_hermes_root()` 回溯读 `optional-mcps` | 优雅降级，`HERMES_ROOT` 可覆盖 |

另有一处测试夹具假设 `<hermes>/integrations/aiask` 路径：`agent/tests/test_factory_control_e2e.py:23-24`。

### 9.3 已完成：全部查询/运行动词已委托

`preflight`/`status`/`once`/`cycle`/`evidence`/`schedule-template` **六个动词全部**经 `run_aiask_cli` 委托到中性 CLI。实测三个动词端到端打通，且 `schedule-template` 的 `degraded` 未被洗成 `ok`。

`ops-start`/`ops-stop`/`ops-status` **有意保留在宿主**——那是进程监管（pid 文件、信号、supervisor 生命周期），属宿主职责而非工厂编排。

由 `test_aiask_factory_delegation.py` 18 项锁定，含一条源码守卫：断言这六个函数体内不出现 `_run_aiask_script` 或 `scripts/factories`——重新自实现会立即失败。经植入验证有效。

---

## 10. Phase 6（包面已完成，建仓与真实宿主待执行）：独立仓库与第二宿主

**两个原以为的「决策点」实测后不存在：**

1. **`desktop/` 归属不是体量问题**。101MB 里 **100MB 是 `node_modules`**，早已被 `.gitignore` 覆盖（`.gitignore:44` 与 `:150`），实际入库仅 **13 个文件**、源码 92KB。此前把磁盘占用误当成了仓库体量。
2. **建仓不需要改任何包配置**。包间依赖用 `[tool.uv.sources]` **相对路径**（`../akshare-mcp`），`packages/` 内部相对关系在独立仓库中不变。已由 `test_standalone_extraction_readiness.py` 断言"相对、不越出 `packages/`、非绝对路径"。

**唯一仍需你决策的是历史策略**：`--history=none`（单提交，简单）或 `preserve`（保留触及子树的 13 个提交，需装 `git-filter-repo`）。子树历史很浅（13 提交，基线 `307d56b`），两种选择代价都不高。

### 10.1 已就绪的条件

- 依赖 DAG 无环（§2.1）
- 公共面零内部泄漏（`akshare_mcp`/`aiask_agent` 均不碰 `strategy_factory.application/infrastructure/domain`）
- MCP manifest 已宿主中立：`optional-mcps/aiask-factories/manifest.yaml` 为 stdio + `AIASK_PROJECT_ROOT`，零 Hermes 引用

### 10.1.1 已修：launcher 硬编码嵌入路径

原方案说"第二宿主只需拷贝 manifest"——这个判断**不完全成立**。全部 6 个 `optional-mcps/aiask-*/run_aiask_mcp_stdio.py` 的回退逻辑硬编码 `integrations/aiask` 后缀，独立仓库后回退永不命中，每个用户都被迫手设 `AIASK_PROJECT_ROOT`。

已修：回退同时接受两种布局，且**按内容检测**（`pyproject.toml` + `scripts/run_mcp_stdio.py` 同时存在）而非按目录名，所以两种布局都不被偏袒：

```text
embedded    <host>/integrations/aiask
standalone  仓库根自身
```

由 `tests/test_aiask_mcp_launcher_layouts.py` 39 项锁定（6 launcher × 6 场景 + 3 项 manifest 检查），含"仅目录名叫 aiask 的空目录不得被接受"和"只读 MCP 不得暴露执行类工具"。经回滚验证：未修补时 standalone 场景 6 项精确失败。

### 10.2 包与测试分布

| 包 | 测试数 | 独立发布 |
|---|---|---|
| `agent` | 45 | ✓ |
| `aiask-quant-core` | 12 | ✓ |
| `akshare-mcp` | 115 | ✓ |
| `finance-mcp-servers` | 2 | ✓ |
| `strategy-factory` | 74 | ✓ |

`desktop/` 101MB 与 `scripts/` 1.8MB 需决定归属。

### 10.3 已完成：Skills 元数据补全

19 个 skill 原本只有 `name` + `description`。现全部声明 7 个字段：

```yaml
version: 1.0.0            # SemVer
protocol_version: "1.0"   # 必须加引号，否则 YAML 会解析成 float
capabilities: [...]        # 点分、宿主中立的能力 ID
required_mcp: [...]        # 实际存在的 MCP 名，或 []
side_effect: read_only | mutating
```

分布：**5 mutating**（4 个 `aiask-run-*` + strategy-factory 试点，走 ActionIntent 确认流）/ **14 read_only**（开发指引类）。

安全性前置确认：本地 skill 加载器用 `yaml.safe_load` 且**无 schema 校验**，额外字段安全；`scripts/build_skills_index.py` 爬的是**外部**源、不扫 `skills/finance/`，故不影响 index freshness。先用单个 skill 试点验证加载正常，再批量。

由 `tests/test_aiask_skill_metadata.py` **251 项**锁定，含四条语义守卫：
- 声明 `factory.run*` 能力者必须标 `mutating`（不能把启动工厂的 skill 标成只读）
- `read_only` 者不得声明 `factory.run*`
- `required_mcp` 里的名字必须是真实存在的 MCP
- 正文提到某 MCP 就必须在 `required_mcp` 声明它（否则第二宿主不会安装）

### 10.3.2 更正（外部审查发现）：Skills 此前根本不会随仓库发布

我此前声称「Skills 元数据 19/19 补齐」，**这个完成判断是错的**。

`skills/finance` 位于 **Hermes 根目录**（供宿主发现），而提取器只复制
`integrations/aiask` 下的已跟踪文件。所以补的元数据对独立仓库**完全无效**——
提取出的仓库一个 Skill 都没有。测试 `test_aiask_skill_metadata.py` 验证的是宿主镜像。

同时确认 `integrations/aiask/.codex/skills` 有 22 个目录但 **git 跟踪数为 0**，
且 frontmatter 仍只有 `name`/`description`（是补元数据之前的旧副本）。实测两处
差异仅为我新增的元数据 + CRLF 行尾，内容一致——所以 `skills/finance` 是唯一权威来源。

已修（保留单一来源）：提取器新增 `EXTERNAL_SOURCES` 映射，把 `skills/finance`
搬入独立仓库。实测 **66 文件 / 19 skills**，元数据 **19/19 完整**。

三条守卫防止回归：
1. `test_the_extraction_planner_carries_this_skills_tree` —— 元数据测试与「会被搬走」绑定
2. `test_extracted_repository_ships_usable_skills` —— **端到端**：真跑提取、检查产出仓库里的 frontmatter 完整（此前完全缺失这条）
3. `test_the_stale_codex_copy_is_not_the_source` —— `.codex/skills` 一旦被跟踪即失败，强制显式决定权威来源

`--history=preserve` 分支同步处理：`git-filter-repo --subdirectory-filter` 只保留子树，
故外部来源以一个后续提交补入，并在返回值里说明它们没有历史。

其中第 2 条我最初写成了 `pytest.importorskip("yaml")`——该 venv 无 yaml，
测试会静默 skip，在最需要它的环境里等于不存在。已改为 stdlib 正则解析顶层键。

### 10.3.1 更正：Skills 正文无需改写

我此前记录「11 个 skill 引用宿主专属概念」——**这个结论是错的**，来自早期把 `HERMES_` 等宽匹配当成耦合。实测：`SKILL.md` 正文里 `hermes` 提及数为 **0**。

7 个 `references/` 子文件确有提及，但全部无害——是 AIASK **自己拥有**的产物：
- `packages/agent/tests/test_hermes_*.py` — AIASK 自己的 parity 测试
- `/v1/hermes/status`、`/v1/hermes/toolsets` — 由 `aiask_agent/routes/hermes.py` 提供的 AIASK 自身接口

所以这一项本就不存在。改为加守卫防止未来真的引入：`test_skill_body_does_not_instruct_a_host_only_command` 与 `test_reference_files_do_not_instruct_a_host_only_command` 禁止 `hermes aiask` / `hermes profile` / `HERMES_HOME` / `hermes_cli` 这类**调用形式**（而非裸词，否则会误伤 AIASK 自身产物）。经植入验证有效。

### 10.6 已完成：clean-install E2E 与中性 facade

**clean-install E2E** 在纯净 venv 里只装 quant-core + strategy-factory（无 `akshare_mcp`），跑通四 runtime 真实 cycle：

```text
akshare_mcp 可导入? False
缺失 provider: []
factor_mining / incubation / signal_tracker / market_event_ingest → error=None
CLEAN-INSTALL E2E 通过: True
```

**它立刻抓到一个真实打包缺陷**：`strategy_factory/contracts/` 缺 `__init__.py`，是隐式命名空间包——开发时靠测试 `pythonpath` 侥幸可用，安装后 `ModuleNotFoundError: strategy_factory.contracts.host_adapter`。这正是所有 `sys.path` 类边界测试都看不见的一类问题。已补齐，并加结构守卫（`test_no_subpackage_is_left_implicit`）防止新子包再犯，无需每次跑重型安装。

重型 E2E 需 `AIASK_RUN_CLEAN_INSTALL_E2E=1` 显式开启（要建 venv 装依赖）；9 项结构检查默认always跑。

**中性 facade**：新包 `packages/data-mcp`（`aiask-data-mcp`）。`akshare-mcp` 这个名字把一个上游数据源的名称，用在了一个已经拥有 MCP host、SQLite provider、manager facade 和券商连接器的包上——名不符实，且独立产品里 AKShare 只是数据源之一。

facade **再导出而非分叉**（一份实现、两个名字），所以采用中性名零行为风险。两条边界：
- 只暴露**数据 provider** 关注点（`storage`/`env_loader`/`server`/`factory_server`），工厂编排必须走 `strategy_factory.api`——数据 provider 不该成为工厂契约的旁路
- 非无界透传：未声明的名字（`services`/`adapters`/`tools` 等）解析即拒

兼容策略：`akshare_mcp` 保留一个主版本，新代码用 `aiask_data_mcp`；窗口关闭后实现搬到 facade、旧名变薄壳。

开发中修掉一个自己引入的缺陷：`__getattr__` 代理遮蔽了同名子模块，导致 `from aiask_data_mcp import server` 解析到后端 server 而非 facade 入口。已用 `_OWN_SUBMODULES` 区分自有/代理，并加测试锁定。

由 `test_clean_install_e2e.py` 10 项 + `test_facade.py` 20 项锁定。

### 10.5 已完成：Host Adapter Contract 与最小 reference host

**契约**：`strategy_factory/contracts/host_adapter.py`，经 `api.runtime` 导出。六个方法（`install_skill_bundle`/`register_mcp`/`discover_capabilities`/`invoke_aiask_cli`/`map_side_effect_policy`/`render_structured_error`）+ 三个数据形状（`SkillBundle`/`McpManifest`/`CapabilityReport`）。

两条被编码进契约的规则：
- **副作用是声明的、非推断的**。`default_side_effect()` fail-closed——未知动词判为 `mutating`，新动词不能因未分类而绕过宿主确认门。`assert_policy_not_loosened()` 允许宿主收紧但禁止放宽。
- **降级必须可见**。`render_structured_error` 收治理 envelope，`degraded` 不可能被渲染成 clean。

**最小 reference host**：`aiask_agent/reference_host.py`。实测契约校验 `conforms=True`、`isinstance(HostAdapter)=True`，并端到端跑通：

```text
factory status            ok=True  status=success
factory preflight         ok=True  status=success
factory schedule-template ok=False status=degraded   ← 未被洗白
side effects: status=read_only once=mutating live_trading=destructive unknown=mutating
```

它**有意不提供**对话循环、gateway、平台矩阵、桌面、模型路由、Hermes parity 面——契约不要求这些。作用是 conformance fixture 与第二宿主起点。

由 `test_host_adapter_contract.py` 28 项 + `test_reference_host.py` 26 项锁定。

**关于 `aiask-agent` 47.8k 行的澄清**：实测 28 个文件含 hermes 字样、共 12679 行，但**零真实 import**——全部是 AIASK 自己的 parity 台账（`HERMES_BASELINE_VERSION` 等常量）与自有路由名（`/v1/hermes/*` 由 `routes/hermes.py` 提供）。所以它不是"耦合待清除"，而是"完整宿主 vs 最小 reference host"的定位区别。新建 reference host 而非改造它，是因为那些 parity 面对 Hermes 集成仍有用。

### 10.4 已完成：提取规划器

`scripts/extract_standalone_repo.py` —— **默认 dry-run**，`--execute` 且必须显式 `--target`。两种历史策略：`none`（单提交，无需额外工具）/ `preserve`（需 `git-filter-repo`，保留触及子树的 13 个提交）。

实测执行结果（临时目录，已清理）：

```text
1727 tracked → 1718 copied   （排除 hermes-plugin 9 文件）
baseline bc24eef, 单提交
提取树内 strategy-factory 测试 617 passed
包相对依赖 ../akshare-mcp 等全部有效，无需改配置
```

617 vs 原树 748 的差额全部是**本轮新增未提交**的 8 个测试文件——脚本只复制 git 已跟踪文件，行为正确，且规划器已就"工作树不干净"发出警告。这反证了该警告的价值。

由 `test_extraction_planner.py` 11 项锁定：dry-run 不创建任何东西、`--execute` 必须带 `--target`、拒绝非空目标（不覆盖既有文件）、缺 `git-filter-repo` 时给出可行替代。

---

## 11. 已知残留（与阶段独立）

### 11.1 已修：stage status fail-open（本轮发现的真实缺陷）

`FactorScheduler._normalize_stage_status` 对未知 token 返回 `"completed"`。实测后果：`timeout` / `cancelled` / `crashed` / `blocked` 全被判为 completed，`_resolve_run_status` 进而把整个 run 报成 `success`——**一个超时的 stage 会当成成功**。

已改为 fail closed（未知 → `failed`，并显式识别上述失败 token）。安全性已核验：4 个调用点传入的都是已知 token（`batch_stage_status` 等），或已经过该函数（幂等），所以不会把正常 stage 误判为失败。

由 `test_factor_scheduler_stage_status.py` 44 项锁定。经回退验证：fail-open 状态下 18 项精确失败。

### 11.2 已修：两个「公共面」守卫定义不一致

`akshare-mcp/tests/test_services_runtime_public_api_only.py` 只允许 `strategy_factory.api` / `.runtime`，而我新建的 DAG 守卫一度也放行 `contracts`。两个守卫对"公共面"定义不同，比任一条规则本身更糟。

已统一到更严的定义：`contracts` 视为 package-private，需要的符号经 `api.runtime` / `api.contracts` 再导出。相应修好一处**既有**违规（`execution_universe_adapter.py:26` 直接 import `strategy_factory.contracts.execution_universe`），并把 `factory_cli.py` 的 envelope 导入改走 `api.runtime`。

### 11.3 其余残留

| 问题 | 位置 | 性质 |
|---|---|---|
| `run_all_factories.py` 是死代码 shim | `scripts/factories/run_all_factories.py` | `main()` 委托给 `run_three_factories.py`，自身 `FactorySpec`/`_build_specs`/`_tee_child_output` 永不执行且与 live 版不兼容 |
| SignalTracker 被所有 supervisor 遗漏 | `run_three_factories.py` | 对 `run_signal_tracker.py` **零引用**；`run_all_factories.py:123` 的 `getattr(args,"no_signal_tracker",True)` 默认 `True` 致永不运行 |
| `run_id` 恒打印 None | `run_strategy_factory.py:432` | `_normalize_cycle_result` 从不把 `run_id` 提出 `data` |
| `preflight()` 曾零调用点 | 七个 runner | Phase 1 已由 `aiask factory preflight` 暴露；runner 自身仍未调用 |
| 五套重复调度实现 | 各 runner | 三套 HH:MM daemon + 两套 interval loop，退避常量各自独立 |

---

## 12. 验收矩阵

| 类别 | 测试 | 状态 |
|---|---|---|
| 静态边界 | `test_package_decoupling_boundary.py`、`test_no_hermes_dependency.py` | ✓ |
| **可执行**边界 | `test_runtime_executable_boundary.py` | ✓（含 xfail 哨兵） |
| 动态 import 守卫 | `test_aiask_integration.py` AST 扫描 | ✓ |
| envelope 契约 | `test_run_envelope_contract.py` | ✓ 27 项 |
| CLI 契约 | `test_factory_cli_contract.py` | ✓ 21 项 |
| 委托层 | `test_aiask_factory_delegation.py` | ✓ 8 项 |
| schema 指纹 | `test_factory_schema_fingerprint.py` | ✓ 30 项（含真实 schema） |
| 包依赖 DAG | `test_package_dag_boundary.py` | ✓ 13 项（AST，含 BOM 加固） |
| provider 协议 | `test_factor_mining_provider_protocol.py` | ✓ 39 项（含源码级穿透守卫） |
| Repository 拆分 | `test_repository_protocol_split.py` | ✓ 13 项 |
| MCP launcher 布局 | `test_aiask_mcp_launcher_layouts.py` | ✓ 39 项（embedded + standalone） |
| Incubation get_db | `test_incubation_provider_get_db.py` | ✓ 7 项 |
| Skills 元数据 | `test_aiask_skill_metadata.py` | ✓ 271 项 |
| 提取前置条件 | `test_standalone_extraction_readiness.py` | ✓ 17 项 |
| 提取规划器 | `test_extraction_planner.py` | ✓ 11 项 |
| 工厂域持久化门面 | `test_factory_repository.py` | ✓ 26 项（含真实适配器 32/32） |
| stage status fail-closed（宿主侧） | `test_factor_scheduler_stage_status.py` | ✓ 44 项 |
| stage 状态机（搬迁后） | `test_stage_status_domain.py` | ✓ 55 项（含纯度守卫） |
| 观察生命周期策略 | `test_observation_lifecycle_domain.py` | ✓ 35 项 |
| 晋升门禁 | `test_promotion_gate_domain.py` | ✓ 34 项 |
| 指纹落库/漂移报告 | `test_factory_schema_fingerprint.py` | ✓ 40 项 |
| 拓扑不漂移 | `test_factory_runner_topology.py` | ✓ |
| daemon 契约 | `test_incubation_runtime_daemon.py` | ✓ 7 项 |
| 参数不丢弃 | `test_market_event_ingest_lookback_alias.py` | ✓ 6 项 |
| schema 迁移 | 旧库升级 / 幂等 / 回滚 | **待 Phase 2** |
| clean-install E2E | 无 Hermes 源码环境 | **已完成：Strategy Factory + Quant Core fresh venv；reference-host clean isolation fixture** |
| 第二宿主 | Claude Desktop 等 | **reference-host 包已完成；真实第三方宿主 adapter 待执行** |

当前测试数：strategy-factory **900 + 1 xfailed**、quant-core **96**、akshare-mcp **862**（10 项既有失败，与本工作无关）、agent **436 + 13 skipped**、Hermes 侧 **433**。

---

## 13. 环境与 CI 陷阱

- 系统 `python3` 为 3.9，缺 `tomllib`。跑 strategy-factory/quant-core 用 `integrations/aiask/packages/agent/.venv`(3.13)，跑 Hermes 测试用 `.venv`(3.11，含 yaml)。
- **`aiask-quant-core` 的 test extra 只有 `pytest`，没有 `pytest-asyncio`**。在该包写 async 测试会本地过、CI 挂。用 `asyncio.run()` 包同步入口。已在纯净 3.13 + 仅 pytest 环境验证 30 项通过。
- CI 用 Python 3.12 + `uv sync`；本地验证为 3.13/3.11 + pip。纯 stdlib 模块风险低，但 3.12 未实测。
- CI 已接入本轮全部新测试：`.github/workflows/aiask-packages.yml` 的 `strategy-factory` job 增 4 个文件，新增 `quant-core-schema` job。

---

## 14. 发布与兼容策略

- 全部包 SemVer；MCP/CLI/Skills/Host Adapter 各自维护 `v1` 协议版本
- 破坏性变更只进主版本；envelope 字段变更需 bump `PROTOCOL_VERSION`
- `akshare-mcp` 兼容包名保留一个主版本，中性 facade 为长期入口
- `MCPRuntimeAdapters`、旧 bootstrap、旧 AKShare import 路径各保留一个主版本的 deprecated alias
- `integrations/aiask` 过渡期只作锁定版本镜像，不再作独立业务源
- **默认产品路径保持研究/诊断/paper**。不因拆分开放实盘：`factory_cli._child_env()` 强制 `LIVE_TRADING_ENABLED=0`，已由测试锁定
