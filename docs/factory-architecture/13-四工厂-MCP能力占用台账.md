# 四工厂 MCP 能力占用台账


> **Hermes 集成副本勘误（2026-07-18）**  
> 本文是历史/上游阶段材料，不是当前 Hermes 树完成证明。  
> 在当前 `integrations/aiask/` 副本中：`packages/desktop-api` 不存在；Hermes Desktop 不是完整 AIASK workbench；live trading 默认关闭。  
> 权威状态：`AIASK_FINANCE_AGENT_COMPLETION_AUDIT.md`、`integrations/aiask/docs/CURRENT_PRODUCT_TRUTH.md`、`docs/GAP_INVENTORY_STATUS.md`。

## 文档定位

本文档是四工厂与 `akshare-mcp` 真实边界的台账版证明，目标是把“依赖了哪些能力”写成可复核清单，而不是继续停留在“感觉很重”。

本台账只描述当前实际位置，不等于目标迁移位置。

本轮正式 target-state 主方案见根目录：

- `../../四工厂独立化与MCP瘦身拆分开发方案-2026-06-23.md`

## 文档归类说明

- 仓库级文档导航见 `../README.md`。
- 本文档属于 current-state 能力台账，负责冻结四工厂当前实际吃掉的 runtime surface。
- 运行报告和阶段性观测材料统一放在 `appendix/reports/`，避免把一次性观测结果混入长期台账结构。

## 统一 schema

后续任何补充都按以下字段维护：

```yaml
factory: 工厂名称
entrypoint: 当前运行入口
owner_package: 当前真正拥有实现的包
runtime_providers: 通过 configure_runtime_services 注入的 provider 编号
runtime_adapters: 通过 MCPRuntimeAdapters 注入的 adapter 编号
direct_services: 直接调用或直接承载运行逻辑的 service/runtime
required_db_methods: 当前真实依赖的 DB/repository 方法
background_dependencies: sidecar、daemon、warmup 或其它后台依赖
mcp_host_surface: 与 server/tool/resource/prompt/manager facade 的关系
migration_bucket: 保留在 akshare-mcp / 迁回 strategy-factory / 暂不动
evidence_paths: 代码证据路径
```

## 一、Strategy Factory host 依赖编号

### Runtime provider surface

当前 `configure_strategy_factory_runtime_services()` 注册 35 项 provider：

| 编号 | provider key | 当前实现来源 |
| --- | --- | --- |
| `P01` | `db_provider` | `get_strategy_factory_db_provider()` |
| `P02` | `factor_scheduler` | `get_factor_scheduler` |
| `P03` | `factor_mining_factory` | `get_factor_mining_factory` |
| `P04` | `factor_mining_support_factory` | `get_factor_mining_factory` |
| `P05` | `factor_pool_gateway` | `get_factor_pool_gateway` |
| `P06` | `incubation_runtime_factory` | `IncubationFactoryRunner` |
| `P07` | `incubation_runtime_support_factory` | `IncubationFactoryRunner` |
| `P08` | `market_event_ingest_runner` | `run_market_text_source_ingest` |
| `P09` | `market_event_ingest_support_factory` | `get_market_event_ingest_support` |
| `P10` | `quant_manager_callable` | `quant_manager` |
| `P11` | `runtime_warmup_runner` | `run_runtime_data_warmup` |
| `P12` | `signal_tracker_runtime_factory` | `get_signal_tracker` |
| `P13` | `signal_tracker_runtime_support_factory` | `get_signal_tracker` |
| `P14` | `strategy_promotion_pipeline_service` | `get_strategy_promotion_pipeline_service` |
| `P15` | `strategy_runtime_control_service` | `get_strategy_runtime_control_service` |
| `P16` | `strategy_runtime_risk_service_factory` | `get_strategy_runtime_risk_service` |
| `P17` | `strategy_lifecycle_shared_runtime` | `_strategy_lifecycle_shared_runtime()` |
| `P18` | `event_context_builder` | `build_event_context` |
| `P19` | `sentiment_analyzer` | `sentiment_analyzer` |
| `P20` | `financial_semantic_service_factory` | `get_financial_semantic_service` |
| `P21` | `index_kline_provider` | `_get_index_kline_from_db` |
| `P22` | `strategy_dsl_compiler` | `compile_strategy_blueprint` |
| `P23` | `strategy_vector_platform_factory` | `get_strategy_vector_platform` |
| `P24` | `strategy_vector_search_engine_builder` | `VectorSearchEngine` |
| `P25` | `strategy_autonomy_service_factory` | `_strategy_autonomy_service_runtime()` |
| `P26` | `strategy_incubation_service_factory` | `get_strategy_incubation_service` |
| `P27` | `strategy_incubation_pipeline_service_factory` | `get_strategy_incubation_pipeline_service` |
| `P28` | `autonomy_lifecycle_runtime` | `_autonomy_lifecycle_runtime()` |
| `P29` | `strategy_vector_profile_builder` | `_build_strategy_vector_profile` |
| `P30` | `strategy_vector_governance_service_factory` | `get_strategy_vector_governance_service` |
| `P31` | `strategy_domain_projection_service_factory` | `get_strategy_domain_projection_service` |
| `P32` | `strategy_lifecycle_scan_runner` | `_lifecycle_scan` |
| `P33` | `execution_audit_snapshot_builder` | `build_execution_audit_snapshot_payload` |
| `P34` | `closure_review_builder` | `build_closure_review` |
| `P35` | `strategy_llm_provider_loader` | `get_strategy_llm_provider` |

### Runtime adapter surface

当前 `MCPRuntimeAdapters` 暴露 7 条通道：

| 编号 | adapter key | 类型 |
| --- | --- | --- |
| `A01` | `repository` | storage/repository |
| `A02` | `vector_search` | gateway |
| `A03` | `autonomy` | gateway |
| `A04` | `factor_research` | gateway |
| `A05` | `incubation` | gateway |
| `A06` | `validation` | gateway |
| `A07` | `risk` | gateway |

说明：

- 若只统计 gateway 通道，当前是 6 条。
- 若把 repository 也计入 runtime adapter surface，则当前是 7 条。

### Shared storage runtime hooks

除 provider 与 adapter 外，`configure_akshare_storage_runtime_hooks()` 还向共享存储层注入额外 hook：

| 编号 | hook |
| --- | --- |
| `H01` | `signal_evidence_builder` |
| `H02` | `execution_audit_snapshot_builder` |
| `H03` | `execution_audit_snapshot_metadata` |
| `H04` | `event_extractor` |
| `H05` | `headline_sentiment_classifier` |
| `H06` | `text_embedding_service_factory` |
| `H07` | `rejected_kline_recorder` |
| `H08` | `execution_audit_gate_evaluator` |
| `H09` | `cleanup_callbacks` |

## 二、按工厂的能力占用台账

### Strategy Factory

| field | value |
| --- | --- |
| `factory` | `Strategy Factory` |
| `entrypoint` | `scripts/factories/run_strategy_factory.py` |
| `owner_package` | 编排在 `packages/strategy-factory`；默认 runtime bridge 在 `packages/akshare-mcp` |
| `runtime_providers` | `P01-P20` |
| `runtime_adapters` | `A01-A07` |
| `direct_services` | `akshare_mcp.adapters.strategy_factory_runtime`、`decision_event_builder`、`factor_mining_factory`、`factor_scheduler`、`financial_semantic_service`、`promotion_pipeline`、`runtime_control`、`strategy_lifecycle_shared`、`strategy_llm_provider`、`vector_platform`、`strategy_autonomy`、`incubation`、`incubation_pipeline`、`vector_search` |
| `required_db_methods` | `save_strategy_factory_run`、`save_strategy_factory_run_artifact`、`list_strategies`、`get_strategy_metrics`、`get_signal_stats`、`save_strategy_task_run`、`update_strategy_task_run`、`save_strategy_signal_evidence`、`get_strategy_incubation_account`、`save_strategy_incubation_account`、`list_factory_event_clusters`、`save_factory_event_cluster`、`save_factory_event_signal` |
| `background_dependencies` | runtime warmup、factor scheduler、external LLM provider、vector index backend |
| `mcp_host_surface` | 运行路径主要靠 in-process runtime bridge，不靠 FastMCP tool 调用；manager/tool facade 与 `services/` 边界层主要服务于外围状态查询、兼容入口与触发，且当前已收敛到 `strategy_factory.api.*` / `runtime.*` 公开面 |
| `migration_bucket` | 领域编排继续留在 `strategy-factory`；provider 装配、DB、vector、semantic、外部平台和默认 runtime 继续留在 `akshare-mcp` |
| `evidence_paths` | `scripts/factories/run_strategy_factory.py`、`packages/akshare-mcp/src/akshare_mcp/adapters/strategy_factory_runtime.py`、`packages/strategy-factory/src/strategy_factory/infrastructure/runtime_services.py`、`packages/strategy-factory/src/strategy_factory/infrastructure/runtime_adapters.py`、`packages/strategy-factory/src/strategy_factory/infrastructure/mcp_services.py`、`packages/strategy-factory/src/strategy_factory/infrastructure/mcp_adapters.py` |

### Factor Mining Factory

| field | value |
| --- | --- |
| `factory` | `Factor Mining Factory` |
| `entrypoint` | `scripts/factories/run_factor_mining_factory.py` |
| `owner_package` | canonical orchestration 在 `packages/strategy-factory`；support / engine / QC / pool / provider 在 `packages/akshare-mcp` |
| `runtime_providers` | 通过 `P03`、`P04` 间接暴露给 Strategy Factory；工厂自身运行不经 `strategy-factory` adapter |
| `runtime_adapters` | 无直接 adapter 契约；当前直接运行 service/runtime |
| `direct_services` | `factor_mining_factory.factory`、`engines/`、`evolution/`、`validation/`、`qc_pipeline.py`、`pool/`、`feedback/` |
| `required_db_methods` | `initialize`、`acquire` / `connection` / `execute_raw`、`get_factor_ic_history`、因子池专用持久化表写入、domain event 写入 |
| `background_dependencies` | 可选 factor scheduler 单例、LLM factor engine、validation/QC runtime |
| `mcp_host_surface` | manager/tool facade 可触发状态与动作，但正式 runner 走直接 service 调用 |
| `migration_bucket` | canonical runtime 已迁回 `strategy-factory`；因子池存储、QC、验证、engine、外部数据与 LLM provider 继续留在 `akshare-mcp` |
| `evidence_paths` | `scripts/factories/run_factor_mining_factory.py`、`packages/akshare-mcp/src/akshare_mcp/services/factor_mining_factory/`、`packages/akshare-mcp/src/akshare_mcp/services/factor_scheduler.py` |

### Incubation Factory

| field | value |
| --- | --- |
| `factory` | `Incubation Factory` |
| `entrypoint` | `scripts/factories/run_incubation_factory.py` -> `strategy_factory.runtime.default_bootstrap.ensure_default_runtime_services()` -> `strategy_factory.runtime.incubation.build_incubation_runtime(...)` |
| `owner_package` | canonical orchestration 在 `packages/strategy-factory`；paper runtime / support / provider glue / compat 在 `packages/akshare-mcp` |
| `runtime_providers` | root runner 会先通过 canonical bootstrap 注入 host provider；runtime 自身还会消费 `P14`、`P15`、`P17` 等共享服务 |
| `runtime_adapters` | 对 Strategy Factory 来说主要通过 `A05` 暴露 incubation gateway；工厂自身运行不依赖 adapter |
| `direct_services` | `runner.py`、`intake.py`、`signal_generator.py`、`forward_verifier.py`、`metrics_recorder.py`、`hit_rate_reporter.py`、`feedback_writer.py`、`trade_prediction_verifier.py`、`accelerator.py`、`alert_monitor.py` |
| `required_db_methods` | `list_strategies`、`list_active_paper_observation_strategies`、`list_paper_observation_strategies`、`list_diagnostic_observation_strategies`、`get_strategy_incubation_account`、`save_strategy_incubation_account`、`save_strategy_incubation_metric`、`save_strategy_domain_event`、`list_strategy_signal_evidence`、`list_strategy_paper_orders`、`list_strategy_paper_trades`、`list_strategy_trade_positions`、`get_signals`、`save_strategy_runtime_control`、`save_strategy_closure_snapshot` |
| `background_dependencies` | `MatchingEngine`、`NavEngine`、paper runtime account state、execution audit shared logic |
| `mcp_host_surface` | manager/runtime facade 提供状态与动作；正式工厂 runner 现已直接进入 `strategy_factory.runtime.incubation`，AKShare package runner 退为 compat 层 |
| `migration_bucket` | canonical runtime 已迁回 `strategy-factory`；paper runtime、SQLite、audit snapshot、shared runtime、execution provider 留在 `akshare-mcp` |
| `evidence_paths` | `scripts/factories/run_incubation_factory.py`、`packages/akshare-mcp/scripts/run_incubation_factory.py`、`packages/akshare-mcp/src/akshare_mcp/services/incubation_factory/runner.py`、`packages/akshare-mcp/src/akshare_mcp/services/incubation_factory/` |

### Market Event Ingest

| field | value |
| --- | --- |
| `factory` | `Market Event Ingest` |
| `entrypoint` | `scripts/factories/run_market_event_ingest.py` |
| `owner_package` | canonical orchestration 在 `packages/strategy-factory`；source adapter / fetch / persistence / compat 在 `packages/akshare-mcp` |
| `runtime_providers` | 无 `strategy-factory` provider registry依赖；bridge 输出进入 Strategy Factory event tables |
| `runtime_adapters` | 无直接 adapter 通道 |
| `direct_services` | `market_text_source_ingest.py`、`market_event_sources.py`、`_market_event_parts/event_constants.py`、`event_mappers.py`、`event_validation.py` |
| `required_db_methods` | `upsert_market_event_normalized`、`list_market_events_normalized`、`save_factory_event_cluster`、`save_factory_event_signal` |
| `background_dependencies` | official source fetch、network availability、source adapter config |
| `mcp_host_surface` | stock radar 等能力可复用同一 service；正式工厂 runner 仍是 direct service 模式 |
| `migration_bucket` | canonical runtime 已迁回 `strategy-factory`；source adapter、network fetch、SQLite persistence 继续留在 `akshare-mcp` |
| `evidence_paths` | `scripts/factories/run_market_event_ingest.py`、`packages/akshare-mcp/src/akshare_mcp/services/market_text_source_ingest.py`、`packages/akshare-mcp/src/akshare_mcp/services/market_event_sources.py` |

### SignalTracker sidecar

| field | value |
| --- | --- |
| `factory` | `SignalTracker sidecar` |
| `entrypoint` | `scripts/factories/run_signal_tracker.py` -> `strategy_factory.runtime.default_bootstrap.ensure_default_runtime_services()` -> `strategy_factory.runtime.signal_tracker.get_signal_tracker_runtime()` |
| `owner_package` | canonical orchestration 在 `packages/strategy-factory`；sidecar runtime / IO / persistence / compat 在 `packages/akshare-mcp` |
| `runtime_providers` | root runner / manager facade 会先通过 canonical bootstrap 注入 host provider；运行拓扑上仍是独立 sidecar |
| `runtime_adapters` | 无直接 adapter 通道 |
| `direct_services` | `signal_tracker.py`、`signal_tracker_parts/specs.py`、`signal_tracker_parts/runtime.py` |
| `required_db_methods` | `list_strategies`、`get_strategy_quality_report`、`list_active_paper_observation_strategies`、`list_paper_observation_strategies`、`save_strategy_task_run`、`update_strategy_task_run`、`save_strategy_signal_event_snapshot`、`save_signals`、`get_pending_forward_returns`、`save_forward_returns_batch` / `save_forward_returns`、`get_klines`、`save_klines`、`save_strategy_domain_event` |
| `background_dependencies` | sidecar process、自身 phase timeout、前向收益回填、kline fallback |
| `mcp_host_surface` | manager/runtime facade 可做 status/run-once；生产口径仍应继续视为 sidecar，`packages/akshare-mcp/scripts/run_signal_tracker.py` 保留 compat 入口 |
| `migration_bucket` | canonical runtime 已迁回 `strategy-factory`；signal persistence、forward-return IO、sidecar runtime 留在 `akshare-mcp` |
| `evidence_paths` | `scripts/factories/run_signal_tracker.py`、`packages/akshare-mcp/scripts/run_signal_tracker.py`、`packages/akshare-mcp/src/akshare_mcp/services/signal_tracker_parts/specs.py` |

## 三、总量结论

### 当前至少已确认的共享宿主依赖面

- Strategy Factory provider 注册项：35
- Strategy Factory adapter 通道：7
- Strategy Factory shared storage hook：9

### 当前主要运行实现面

按当前代码组织，四工厂重载 `akshare-mcp` 的重点不在 tool catalog，而在以下实现面：

- `factor_mining_factory/`：搜索、进化、验证、QC、active pool、feedback
- `incubation_factory/`：intake、信号、前向验证、指标、命中率、反馈、audit、paper runtime
- `signal_tracker_parts/`：信号生成、运行宇宙加载、前向收益回填、生命周期事件
- `market_text_source_ingest.py` + `market_event_sources.py`：official source fetch、normalized event、bridge
- `strategy_lifecycle_shared/`：overview、confidence、execution audit snapshot、closure review
- `matching_engine.py` / `nav_engine.py`：Incubation 持有的 paper-trading background runtime

### 冻结结论

- 若只看 FastMCP tools，会低估四工厂对 `akshare-mcp` 的真实占用。
- 若只看 `strategy-factory` 目录，又会高估它已经完成了多少运行迁移。
- 当前最准确的结论是：`strategy-factory` 已掌握 canonical orchestration 与公共 runtime 入口，`akshare-mcp` 仍掌握大量 support/provider/IO/compat 实现和宿主能力。

## 四、维护要求

- 今后新增四工厂运行逻辑，文档必须先判断它属于：
  - host facade
  - provider surface
  - adapter surface
  - factory implementation surface
- `akshare-mcp/tools/managers/` 若新增对 `strategy_factory` 的依赖，应继续限制在 `strategy_factory.api.*` 与 `strategy_factory.runtime.*`，不得回退到根包 facade 或私有层。
- 今后若新增 provider / adapter / sidecar / direct service 依赖，必须先更新本台账，再谈迁移路线。
- 若文档与代码冲突，以当前代码为准，并在 `12-四工厂真实流程与MCP依赖审计.md` 中补证据。
