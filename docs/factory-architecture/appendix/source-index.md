# Source Index


> **Hermes 集成副本勘误（2026-07-18）**  
> 本文是历史/上游阶段材料，不是当前 Hermes 树完成证明。  
> 在当前 `integrations/aiask/` 副本中：`packages/desktop-api` 不存在；Hermes Desktop 不是完整 AIASK workbench；live trading 默认关闭。  
> 权威状态：`AIASK_FINANCE_AGENT_COMPLETION_AUDIT.md`、`integrations/aiask/docs/CURRENT_PRODUCT_TRUTH.md`、`docs/GAP_INVENTORY_STATUS.md`。

本索引列出本文档使用的事实来源。源码路径按仓库根目录相对路径书写；MCP resources 和外部 URL 单独列出，不能当作本地路径。

## 审查状态

| 来源 | 本轮状态 |
| --- | --- |
| 当前源码 | 已只读复核关键入口 |
| Graphify | 已读取 `CURATED_SUMMARY.json` 和 `cross-package-edges.json` |
| AKShare MCP resources | 已读取 server capabilities；tool catalog 可读但体积大；governance system report 读取失败 |
| thinking MCP | 当前会话不可见，未调用 |
| 联网公开资料 | MLOps 与 NIST 页面可读；SR 11-7 legacy URL 当前返回 404 |

## Supervisor 与脚本

| 路径 | 作用 |
| --- | --- |
| `scripts/factories/run_three_factories.py` | 当前四运行体 supervisor，启动 Strategy、Factor Mining、Incubation、Market Event Ingest |
| `scripts/factories/run_all_factories.py` | 兼容入口，委托 `run_three_factories.py`，不再混合启动 SignalTracker |
| `scripts/factories/run_strategy_factory.py` | Strategy Factory 独立运行入口 |
| `scripts/factories/run_factor_mining_factory.py` | Factor Mining Factory 独立运行入口 |
| `scripts/factories/run_incubation_factory.py` | Incubation Factory 独立运行入口 |
| `scripts/factories/run_market_event_ingest.py` | Market Event Ingest 独立运行入口 |
| `scripts/factories/run_signal_tracker.py` | SignalTracker sidecar wrapper |
| `packages/akshare-mcp/scripts/run_signal_tracker.py` | SignalTracker 实际运行器 |
| `scripts/factories/run_strategy_factory_quality_session.py` | quality session 验证会话 |
| `scripts/factories/_quality_session_common.py` | quality session 共享 helper |
| `scripts/factories/_quality_session_collectors.py` | quality session 数据采集与 runtime snapshot |
| `scripts/factories/_quality_session_modes.py` | quality session mode/env 配置 |
| `scripts/factories/_quality_session_report.py` | quality session issue flags 和报告摘要 |
| `scripts/factories/_quality_session_render.py` | quality session report render |
| `start_four_factories_24h.sh` | 历史包装脚本，存在旧口径风险，不作为规范来源 |

## Strategy Factory

| 路径 | 作用 |
| --- | --- |
| `packages/strategy-factory/src/strategy_factory/api/facade.py` | Strategy Factory public facade |
| `packages/strategy-factory/src/strategy_factory/api/contracts.py` | Typed contracts 和边界类型 |
| `packages/strategy-factory/src/strategy_factory/infrastructure/mcp_services.py` | runtime service registry，声明 Strategy Factory 拥有编排与契约 |
| `packages/strategy-factory/src/strategy_factory/application/factory_scheduler.py` | 调度器主入口 |
| `packages/strategy-factory/src/strategy_factory/application/cycle_pipeline.py` | cycle pipeline |
| `packages/strategy-factory/src/strategy_factory/application/cycle_runner.py` | cycle runner |
| `packages/strategy-factory/src/strategy_factory/application/_factory_scheduler_loop_parts/models.py` | dispatch run status 和 outer timeout 相关逻辑 |
| `packages/strategy-factory/src/strategy_factory/application/_factory_scheduler_loop_parts/policy_execution.py` | run_once timeout、paper trading cycle 等 policy |
| `packages/strategy-factory/src/strategy_factory/application/services/admission_authority.py` | formal/provisional/observe/reject admission |
| `packages/strategy-factory/src/strategy_factory/application/services/lifecycle_coordinator.py` | lifecycle handoff 和 execution audit snapshot 协调 |
| `packages/strategy-factory/src/strategy_factory/application/stock_strategy_matrix.py` | stock-first/matrix 生成路径 |
| `packages/strategy-factory/src/strategy_factory/application/trade_prediction_contract.py` | trade prediction contract |

## AKShare Runtime Providers

| 路径 | 作用 |
| --- | --- |
| `packages/akshare-mcp/src/akshare_mcp/adapters/strategy_factory_runtime.py` | 注入 Strategy Factory runtime providers |
| `packages/akshare-mcp/src/akshare_mcp/services/strategy_autonomy.py` | 策略自治生成服务 |
| `packages/akshare-mcp/src/akshare_mcp/services/strategy_pipeline.py` | staged LLM pipeline |
| `packages/akshare-mcp/src/akshare_mcp/services/_strategy_llm_provider_runtime.py` | LLM provider runtime |
| `packages/akshare-mcp/src/akshare_mcp/services/signal_tracker_parts/specs.py` | SignalTracker phase 和执行闭环逻辑 |
| `packages/akshare-mcp/src/akshare_mcp/services/strategy_lifecycle_shared/common.py` | `strategies.status` transition map 和 status alias |
| `packages/akshare-mcp/src/akshare_mcp/services/strategy_lifecycle_shared/confidence.py` | confidence contract 与 execution audit gate evaluator |
| `packages/akshare-mcp/src/akshare_mcp/services/strategy_lifecycle_shared/incubation.py` | incubation pipeline stage resolution |
| `packages/akshare-mcp/src/akshare_mcp/services/strategy_lifecycle_shared/overview.py` | lifecycle overview、promotion/hard gate 汇总 |
| `packages/akshare-mcp/src/akshare_mcp/services/strategy_lifecycle_shared/execution_quality_parts/execution_metrics.py` | execution evidence status 与 audit gate 汇总 |
| `packages/akshare-mcp/src/akshare_mcp/services/market_event_sources.py` | 市场事件源归一化和 Strategy Factory bridge |
| `packages/akshare-mcp/src/akshare_mcp/services/event_constants.py` | source tier、reliability 默认值和事件源常量 |
| `packages/akshare-mcp/src/akshare_mcp/services/event_mappers.py` | CNINFO/SSE mapper、source tier normalization |
| `packages/akshare-mcp/src/akshare_mcp/services/event_validation.py` | event validation、source tier 和 reliability 聚合 |

## Factor Mining Factory

| 路径 | 作用 |
| --- | --- |
| `packages/akshare-mcp/src/akshare_mcp/services/factor_mining_factory/factory.py` | 因子挖掘主 cycle |
| `packages/akshare-mcp/src/akshare_mcp/services/factor_mining_factory/engines/engine_scheduler.py` | engine 调度与 timeout/degradation |
| `packages/akshare-mcp/src/akshare_mcp/services/factor_mining_factory/qc_pipeline.py` | QC pipeline |
| `packages/akshare-mcp/src/akshare_mcp/services/factor_mining_factory/pool/active_pool.py` | active factor pool |
| `packages/akshare-mcp/src/akshare_mcp/services/factor_catalog.py` | factor catalog |

## Incubation Factory 与 Paper Evidence

| 路径 | 作用 |
| --- | --- |
| `packages/akshare-mcp/src/akshare_mcp/services/incubation_factory/runner.py` | 孵化工厂 phase runner |
| `packages/akshare-mcp/src/akshare_mcp/services/incubation_factory/intake.py` | observe/paper/incubating intake |
| `packages/akshare-mcp/src/akshare_mcp/services/incubation_factory/forward_verifier.py` | forward verification |
| `packages/akshare-mcp/src/akshare_mcp/services/incubation_factory/hit_rate_matrix.py` | hit-rate matrix |
| `packages/akshare-mcp/src/akshare_mcp/services/incubation_factory/hit_rate_reporter.py` | hit-rate report |
| `packages/akshare-mcp/src/akshare_mcp/services/incubation_factory/feedback_writer.py` | feedback 写回 |
| `packages/akshare-mcp/src/akshare_mcp/services/incubation_pipeline.py` | pipeline stage、candidate/graduation/promoted 逻辑 |
| `packages/akshare-mcp/src/akshare_mcp/services/incubation.py` | incubation service facade |
| `packages/akshare-mcp/src/akshare_mcp/services/incubation_parts/runtime.py` | `process_strategies`、settlement、metrics |
| `packages/akshare-mcp/src/akshare_mcp/services/incubation_parts/context.py` | order sync、settlement context |

## Quant Core Evidence 与 Audit

| 路径 | 作用 |
| --- | --- |
| `packages/aiask-quant-core/src/aiask_quant_core/storage/sqlite/signal_tracking.py` | signal tracking storage |
| `packages/aiask-quant-core/src/aiask_quant_core/storage/sqlite/_strategy_crud_core.py` | strategy status 查询、paper/diagnostic observation 查询 helper |
| `packages/aiask-quant-core/src/aiask_quant_core/storage/sqlite/strategy_incubation.py` | incubation storage aggregate |
| `packages/aiask-quant-core/src/aiask_quant_core/storage/sqlite/strategy_incubation_repos/signal_evidence.py` | signal evidence |
| `packages/aiask-quant-core/src/aiask_quant_core/storage/sqlite/strategy_incubation_repos/trade_positions.py` | trade positions |
| `packages/aiask-quant-core/src/aiask_quant_core/storage/sqlite/strategy_incubation_repos/trade_audit.py` | trade audit summary |
| `packages/aiask-quant-core/src/aiask_quant_core/storage/sqlite/strategy_incubation_repos/execution_acceptance.py` | execution audit acceptance |
| `packages/aiask-quant-core/src/aiask_quant_core/storage/sqlite/strategy_incubation_repos/closure_snapshots.py` | execution audit snapshots |
| `packages/aiask-quant-core/src/aiask_quant_core/storage/sqlite/schema_strategy_parts/mappers.py` | incubation accounts/metrics/pipeline schema |
| `packages/aiask-quant-core/src/aiask_quant_core/storage/sqlite/schema_strategy_parts/schema_definitions.py` | strategy/evidence schema definitions |

## Graphify Evidence

| 路径 | 作用 |
| --- | --- |
| `reports/code-graph/full-2026-05-29/curated/CURATED_SUMMARY.json` | 代码图谱摘要；core 7638 nodes / 19921 edges |
| `reports/code-graph/full-2026-05-29/curated/cross-package-edges.json` | 跨包依赖证据 |
| `reports/code-graph/full-2026-05-29/curated/endpoint-map.json` | Agent/Desktop endpoint map |

Graphify package 规模：`akshare-mcp` 4190/11216，`strategy-factory` 1754/4032，`aiask-quant-core` 619/1253，`agent` 540/1897，`desktop` 429/1111，`root-runners` 63/137。

Graphify 跨包边计数：`akshare-mcp -> strategy-factory` 47，`strategy-factory -> aiask-quant-core` 21，`akshare-mcp -> aiask-quant-core` 16，`agent -> akshare-mcp` 4，`root-runners -> akshare-mcp` 3，`strategy-factory -> akshare-mcp` 3。

## MCP Resources

| Resource | 本轮状态 | 说明 |
| --- | --- | --- |
| `resource://server/capabilities` | 可读 | 报告 179 tools、14 resource templates、7 prompts |
| `resource://server/tool-catalog` | 可读 | 体积较大；用于确认工具目录和 side-effect contract 存在 |
| `resource://governance/system/report` | 读取失败 | server error：resource handler 异常，需纳入治理可观测性 |

当前可见 MCP server 只有 `akshare-mcp`。未暴露 `thinking` MCP。

## 外部公开参考

| URL | 本轮状态 | 采用方式 |
| --- | --- | --- |
| `https://ml-ops.org/content/mlops-principles` | 可读 | 映射版本化、测试、可复现、监控、自动化 |
| `https://www.nist.gov/itl/ai-risk-management-framework` | 可读 | 映射 Govern/Map/Measure/Manage 风险治理思想 |
| `https://nvlpubs.nist.gov/nistpubs/ai/NIST.AI.100-1.pdf` | 可下载 | 作为 NIST AI RMF PDF 参考地址 |
| `https://www.federalreserve.gov/supervisionreg/srletters/sr1107.htm` | 当前返回 404 | 仅保留为 legacy reference，不支撑具体正文断言 |

## 关键测试

| 路径 | 覆盖 |
| --- | --- |
| `packages/strategy-factory/tests/test_cycle_pipeline.py` | cycle pipeline |
| `packages/strategy-factory/tests/test_cycle_status_resolution.py` | run status resolution |
| `packages/strategy-factory/tests/test_strategy_factory_quality_fixes.py` | quality fixes regression |
| `packages/strategy-factory/tests/test_runtime_provider_boundary.py` | runtime provider boundary |
| `packages/strategy-factory/tests/test_package_decoupling_boundary.py` | package boundary |
| `packages/strategy-factory/tests/test_paper_trading_bridge.py` | paper bridge |
| `packages/strategy-factory/tests/test_no_live_trading_boundary.py` | live trading boundary |
| `packages/akshare-mcp/tests/test_factor_catalog_p1_3.py` | factor catalog |
| `packages/akshare-mcp/tests/test_qc_pipeline_p2_1.py` | factor QC pipeline |
| `packages/akshare-mcp/tests/test_batch_ic_neutralize_p0_2.py` | IC neutralization |
| `packages/akshare-mcp/tests/test_forward_horizons_p2_2.py` | forward horizons |
| `packages/akshare-mcp/tests/test_hit_rate_matrix_p3_1.py` | incubation hit-rate matrix |
| `packages/akshare-mcp/tests/test_hit_rate_reporter_matrix_p3_1.py` | hit-rate reporter |
| `packages/akshare-mcp/tests/test_strategy_factory_ownership.py` | Strategy Factory ownership |
| `packages/akshare-mcp/tests/test_tool_argument_contract.py` | MCP tool contracts |
| `packages/akshare-mcp/tests/test_provider_contracts.py` | provider contracts |
| `packages/akshare-mcp/tests/test_mcp_full_tool_regression_fixes.py` | MCP full tool regression |
| `packages/akshare-mcp/tests/test_data_source_tdx_routing.py` | data source routing |
| `packages/akshare-mcp/tests/test_tdx_storage_phase8.py` | TDX storage |
| `packages/akshare-mcp/tests/test_p0_1_data_readiness.py` | data readiness |
| `packages/akshare-mcp/tests/test_warmup_audit_scripts_contract.py` | warmup/audit scripts |
| `packages/akshare-mcp/tests/test_strategy_factory_quality_session_report.py` | quality session report/health flags |
| `packages/akshare-mcp/tests/test_signal_tracker_forward_returns.py` | SignalTracker forward returns |
| `packages/akshare-mcp/tests/test_execution_audit_acceptance_backfill.py` | execution audit acceptance/backfill |
| `packages/akshare-mcp/tests/test_execution_audit_replay_contract.py` | execution audit replay contract |
| `packages/aiask-quant-core/tests/test_strategy_trade_prediction_p0.py` | strategy trade prediction |
| `packages/aiask-quant-core/tests/test_storage_json_caps.py` | storage JSON caps |
| `packages/aiask-quant-core/tests/test_list_signal_forward_returns.py` | signal forward returns |
