# AIASK Graphify Curated Graph Report

## Outputs

The multi-megabyte raw and per-package graph dumps are generated locally and
excluded from Git. The reviewable repository evidence retained beside this
report is:

- `CURATED_SUMMARY.json` — aggregate counts and graph metadata.
- `cross-package-edges.json` — cross-package dependency evidence.
- `endpoint-map.json` — Agent/Desktop endpoint classification used by the drift gate.

Run `make -C integrations/aiask code-graph` to regenerate the omitted graph
dumps under `graphify-out/` and `curated/*.graph.json`.

## Counts
- Original: 18439 nodes, 46715 edges
- Core curated: 8115 nodes, 23615 edges
- Tests-only: 2104 nodes, 2756 edges
- Docs-only: 7 nodes, 6 edges
- Endpoint map: 162 endpoints, 0 Desktop+Agent matches

## Package Subgraphs
- `agent`: 901 nodes, 3299 edges
- `akshare-mcp`: 4517 nodes, 13237 edges
- `strategy-factory`: 1987 nodes, 5367 edges
- `aiask-quant-core`: 551 nodes, 1341 edges
- `desktop`: 0 nodes, 0 edges
- `root-runners`: 0 nodes, 0 edges

## Core Hub Nodes
- `ok()` (222): `packages/akshare-mcp/src/akshare_mcp/utils.py:L223`
- `fail()` (198): `packages/akshare-mcp/src/akshare_mcp/utils.py:L234`
- `server.py` (157): `packages/agent/src/aiask_agent/server.py:L1`
- `utils.py` (157): `packages/akshare-mcp/src/akshare_mcp/utils.py:L1`
- `normalize_code()` (146): `packages/akshare-mcp/src/akshare_mcp/utils.py:L353`
- `strategy_manager.py` (107): `packages/akshare-mcp/src/akshare_mcp/tools/managers/strategy_manager.py:L1`
- `AgentRuntime` (91): `packages/agent/src/aiask_agent/runtime.py:L140`
- `runner.py` (90): `packages/strategy-factory/src/strategy_factory/application/_submitter_actions/runner.py:L1`
- `app_route_assembly.py` (87): `packages/agent/src/aiask_agent/app_route_assembly.py:L1`
- `_factory_scheduler_loop.py` (86): `packages/strategy-factory/src/strategy_factory/application/_factory_scheduler_loop.py:L1`
- `session_store.py` (82): `packages/agent/src/aiask_agent/session_store.py:L1`
- `app_route_callbacks.py` (82): `packages/agent/src/aiask_agent/app_route_callbacks.py:L1`
- `ok_with_meta()` (80): `packages/akshare-mcp/src/akshare_mcp/tools/manager_protocol.py:L308`
- `fail_with_meta()` (78): `packages/akshare-mcp/src/akshare_mcp/tools/manager_protocol.py:L395`
- `runtime.py` (76): `packages/agent/src/aiask_agent/runtime.py:L1`
- `manager_protocol.py` (72): `packages/akshare-mcp/src/akshare_mcp/tools/manager_protocol.py:L1`
- `__init__.py` (71): `packages/akshare-mcp/src/akshare_mcp/services/__init__.py:L1`
- `screen_conditions.py` (70): `packages/akshare-mcp/src/akshare_mcp/services/screen_conditions.py:L1`
- `AgentSessionStore` (67): `packages/agent/src/aiask_agent/session_store.py:L49`
- `native_capabilities.py` (66): `packages/agent/src/aiask_agent/native_capabilities.py:L1`
- `handlers_personal.py` (65): `packages/akshare-mcp/src/akshare_mcp/tools/managers/strategy_mgr_crud/handlers_personal.py:L1`
- `get_artifact_async()` (64): `packages/akshare-mcp/src/akshare_mcp/services/artifact_registry.py:L127`
- `server.py` (64): `packages/akshare-mcp/src/akshare_mcp/server.py:L1`
- `__init__.py` (64): `packages/akshare-mcp/src/akshare_mcp/services/stock_radar/__init__.py:L1`
- `_runner_helpers.py` (64): `packages/strategy-factory/src/strategy_factory/application/_submitter_actions/_runner_helpers.py:L1`

## Cross-Package Edge Samples
- `agent` -> `akshare-mcp`: `governance_check()` --indirect_call--> `governance_check_workflow()` (INFERRED, `packages/agent/src/aiask_agent/adapters/akshare.py:L89`)
- `agent` -> `akshare-mcp`: `_execute_data_sync()` --calls--> `register_data_sync_manager()` (INFERRED, `packages/agent/src/aiask_agent/adapters/desktop_ops.py:L136`)
- `agent` -> `strategy-factory`: `_execute_factor_factory()` --calls--> `ensure_default_runtime_services()` (INFERRED, `packages/agent/src/aiask_agent/adapters/desktop_ops.py:L172`)
- `agent` -> `strategy-factory`: `_execute_factor_factory()` --calls--> `get_factor_mining_runtime()` (INFERRED, `packages/agent/src/aiask_agent/adapters/desktop_ops.py:L173`)
- `agent` -> `strategy-factory`: `_execute_incubation_factory()` --calls--> `ensure_default_runtime_services()` (INFERRED, `packages/agent/src/aiask_agent/adapters/desktop_ops.py:L204`)
- `agent` -> `strategy-factory`: `_execute_incubation_factory()` --calls--> `build_incubation_runtime()` (INFERRED, `packages/agent/src/aiask_agent/adapters/desktop_ops.py:L207`)
- `agent` -> `akshare-mcp`: `_execute_stock_radar()` --calls--> `push_stock_radar_digest()` (INFERRED, `packages/agent/src/aiask_agent/adapters/desktop_ops.py:L537`)
- `agent` -> `akshare-mcp`: `_execute_stock_radar()` --calls--> `run_stock_radar()` (INFERRED, `packages/agent/src/aiask_agent/adapters/desktop_ops.py:L520`)
- `agent` -> `akshare-mcp`: `_load_status_handler()` --indirect_call--> `stock_radar_status()` (INFERRED, `packages/agent/src/aiask_agent/adapters/stock_radar.py:L102`)
- `agent` -> `akshare-mcp`: `_load_candidates_handler()` --indirect_call--> `stock_radar_candidates()` (INFERRED, `packages/agent/src/aiask_agent/adapters/stock_radar.py:L108`)
- `agent` -> `akshare-mcp`: `_load_digest_handler()` --indirect_call--> `stock_radar_digest()` (INFERRED, `packages/agent/src/aiask_agent/adapters/stock_radar.py:L114`)
- `agent` -> `akshare-mcp`: `_load_factory_status_handler()` --indirect_call--> `handle_factory_status()` (INFERRED, `packages/agent/src/aiask_agent/adapters/strategy_factory.py:L165`)
- `agent` -> `akshare-mcp`: `_load_factory_runs_handler()` --indirect_call--> `handle_factory_runs()` (INFERRED, `packages/agent/src/aiask_agent/adapters/strategy_factory.py:L171`)
- `agent` -> `akshare-mcp`: `_load_promotion_reviews_handler()` --indirect_call--> `handle_promotion_reviews()` (INFERRED, `packages/agent/src/aiask_agent/adapters/strategy_factory.py:L177`)
- `agent` -> `akshare-mcp`: `_load_domain_events_handler()` --indirect_call--> `handle_domain_events()` (INFERRED, `packages/agent/src/aiask_agent/adapters/strategy_factory.py:L183`)
- `agent` -> `akshare-mcp`: `_load_factory_dispatch_handler()` --indirect_call--> `handle_factory_dispatch_run()` (INFERRED, `packages/agent/src/aiask_agent/adapters/strategy_factory.py:L189`)
- `agent` -> `strategy-factory`: `incubation_factory_status()` --calls--> `ensure_default_runtime_services()` (INFERRED, `packages/agent/src/aiask_agent/adapters/strategy_factory.py:L368`)
- `agent` -> `strategy-factory`: `incubation_factory_status()` --calls--> `build_incubation_runtime()` (INFERRED, `packages/agent/src/aiask_agent/adapters/strategy_factory.py:L369`)
- `akshare-mcp` -> `aiask-quant-core`: `configure_akshare_storage_runtime_hooks()` --calls--> `configure_storage_runtime_hooks()` (INFERRED, `packages/akshare-mcp/src/akshare_mcp/adapters/strategy_factory_runtime.py:L91`)
- `akshare-mcp` -> `strategy-factory`: `configure_strategy_factory_runtime_services()` --calls--> `configure_runtime_services()` (INFERRED, `packages/akshare-mcp/src/akshare_mcp/adapters/strategy_factory_runtime.py:L152`)
- `akshare-mcp` -> `strategy-factory`: `build_strategy_factory_runtime_adapters()` --calls--> `build_default_runtime_adapters()` (INFERRED, `packages/akshare-mcp/src/akshare_mcp/adapters/strategy_factory_runtime.py:L203`)
- `akshare-mcp` -> `strategy-factory`: `build_strategy_factory_scheduler_kwargs()` --calls--> `build_default_scheduler_kwargs()` (INFERRED, `packages/akshare-mcp/src/akshare_mcp/adapters/strategy_factory_runtime.py:L211`)
- `akshare-mcp` -> `agent`: `market_data.py` --imports--> `akshare.py` (EXTRACTED, `packages/akshare-mcp/src/akshare_mcp/data_source/market_data.py:L17`)
- `akshare-mcp` -> `strategy-factory`: `configure_local_strategy_factory_runtime()` --calls--> `ensure_default_runtime_services()` (INFERRED, `packages/akshare-mcp/src/akshare_mcp/runtime/strategy_factory_bootstrap.py:L11`)
- `akshare-mcp` -> `strategy-factory`: `build_local_strategy_factory_runtime_adapters()` --calls--> `build_default_runtime_adapters()` (INFERRED, `packages/akshare-mcp/src/akshare_mcp/runtime/strategy_factory_bootstrap.py:L17`)
- `akshare-mcp` -> `strategy-factory`: `build_local_strategy_factory_scheduler_kwargs()` --calls--> `build_default_scheduler_kwargs()` (INFERRED, `packages/akshare-mcp/src/akshare_mcp/runtime/strategy_factory_bootstrap.py:L23`)
- `akshare-mcp` -> `aiask-quant-core`: `_shutdown_services_async()` --calls--> `close_db()` (INFERRED, `packages/akshare-mcp/src/akshare_mcp/server.py:L278`)
- `akshare-mcp` -> `aiask-quant-core`: `_shutdown_services_async()` --calls--> `drain_cleanup_callbacks()` (INFERRED, `packages/akshare-mcp/src/akshare_mcp/server.py:L284`)
- `akshare-mcp` -> `aiask-quant-core`: `_build_oos_validation_report()` --calls--> `FactorValidationPipeline` (INFERRED, `packages/akshare-mcp/src/akshare_mcp/services/_factor_validation_pipeline_support.py:L214`)
- `akshare-mcp` -> `strategy-factory`: `StrategyExecutionUniverse` --inherits--> `ExecutionUniverseContract` (EXTRACTED, `packages/akshare-mcp/src/akshare_mcp/services/_strategy_execution_universe.py:L55`)
- `akshare-mcp` -> `strategy-factory`: `_get_strategy_factory_imports()` --indirect_call--> `preferred_strategy_types_for_factor()` (INFERRED, `packages/akshare-mcp/src/akshare_mcp/services/_strategy_generators_context.py:L35`)
- `akshare-mcp` -> `strategy-factory`: `_get_strategy_factory_imports()` --indirect_call--> `preferred_strategy_types_for_factor()` (INFERRED, `packages/akshare-mcp/src/akshare_mcp/services/_strategy_generators_external.py:L37`)
- `akshare-mcp` -> `strategy-factory`: `_get_strategy_factory_imports()` --indirect_call--> `resolve_candidate_validation_profile()` (INFERRED, `packages/akshare-mcp/src/akshare_mcp/services/_strategy_generators_generate.py:L42`)
- `akshare-mcp` -> `strategy-factory`: `_get_strategy_factory_imports()` --indirect_call--> `validate_precompile_candidate_contract()` (INFERRED, `packages/akshare-mcp/src/akshare_mcp/services/_strategy_generators_generate.py:L43`)
- `akshare-mcp` -> `strategy-factory`: `_get_strategy_factory_imports()` --indirect_call--> `preferred_strategy_types_for_factor()` (INFERRED, `packages/akshare-mcp/src/akshare_mcp/services/_strategy_generators_generate.py:L39`)
- `akshare-mcp` -> `strategy-factory`: `_pipeline_candidate_to_spec()` --calls--> `resolve_candidate_validation_profile()` (INFERRED, `packages/akshare-mcp/src/akshare_mcp/services/_strategy_generators_generate_parts/specs.py:L39`)
- `akshare-mcp` -> `strategy-factory`: `_pipeline_candidate_to_spec()` --calls--> `validate_precompile_candidate_contract()` (INFERRED, `packages/akshare-mcp/src/akshare_mcp/services/_strategy_generators_generate_parts/specs.py:L52`)
- `akshare-mcp` -> `strategy-factory`: `_get_strategy_factory_imports()` --indirect_call--> `HypothesisLoweringCompiler` (INFERRED, `packages/akshare-mcp/src/akshare_mcp/services/_strategy_generators_specs_parts/context.py:L31`)
- `akshare-mcp` -> `strategy-factory`: `_get_strategy_factory_imports()` --indirect_call--> `validate_precompile_candidate_contract()` (INFERRED, `packages/akshare-mcp/src/akshare_mcp/services/_strategy_generators_specs_parts/context.py:L41`)
- `akshare-mcp` -> `strategy-factory`: `_get_strategy_factory_imports()` --indirect_call--> `synthesize_confidence_contract()` (INFERRED, `packages/akshare-mcp/src/akshare_mcp/services/_strategy_generators_specs_parts/context.py:L40`)

## Endpoint Samples
- `/health`: server `GET:health`, desktop `-`
- `/health/detailed`: server `GET:health_detailed`, desktop `-`
- `/intents`: server `GET:intent_list, POST:intent_create`, desktop `-`
- `/intents/{intent_id}`: server `GET:intent_get`, desktop `-`
- `/intents/{intent_id}/confirm`: server `POST:intent_confirm`, desktop `-`
- `/intents/{intent_id}/deny`: server `POST:intent_deny`, desktop `-`
- `/v1/ai/config`: server `GET:ai_config, PATCH:ai_config_save`, desktop `-`
- `/v1/ai/models`: server `GET:ai_models`, desktop `-`
- `/v1/ai/smoke`: server `POST:ai_smoke`, desktop `-`
- `/v1/ai/status`: server `GET:ai_status`, desktop `-`
- `/v1/approvals`: server `GET:approvals`, desktop `-`
- `/v1/approvals/{approval_id}/{decision}`: server `POST:approval_decide`, desktop `-`
- `/v1/artifacts/{artifact_id}`: server `GET:artifact_get`, desktop `-`
- `/v1/artifacts/{artifact_id}/content`: server `GET:artifact_content`, desktop `-`
- `/v1/browser/sessions`: server `GET:browser_sessions`, desktop `-`
- `/v1/capabilities/parity`: server `GET:capabilities_parity`, desktop `-`
- `/v1/chat/completions`: server `POST:chat_completions`, desktop `-`
- `/v1/connectors`: server `GET:connectors_list_api`, desktop `-`
- `/v1/connectors/summary`: server `GET:connectors_summary_api`, desktop `-`
- `/v1/connectors/{connector_type}/{name}`: server `GET:connector_detail_api`, desktop `-`
- `/v1/connectors/{connector_type}/{name}/test`: server `POST:connector_test_api`, desktop `-`
- `/v1/desktop/analytics/summary`: server `GET:desktop_analytics_summary`, desktop `-`
- `/v1/desktop/broker-readiness`: server `GET:desktop_broker_readiness`, desktop `-`
- `/v1/desktop/broker/accounts`: server `GET:desktop_broker_accounts`, desktop `-`
- `/v1/desktop/broker/analytics/latest`: server `GET:desktop_broker_analytics_latest`, desktop `-`
- `/v1/desktop/broker/analytics/run`: server `POST:desktop_broker_analytics_run`, desktop `-`
- `/v1/desktop/broker/orders`: server `GET:desktop_broker_orders`, desktop `-`
- `/v1/desktop/broker/positions`: server `GET:desktop_broker_positions`, desktop `-`
- `/v1/desktop/broker/sync`: server `POST:desktop_broker_sync`, desktop `-`
- `/v1/desktop/capabilities`: server `GET:desktop_capabilities`, desktop `-`

## Notes
- Core graph excludes tests, docs, rationale nodes, and obvious Python/TypeScript builtin type hubs.
- Subgraph `community` values are weak connected component ids after filtering, not Leiden communities.
- Keep using the original full graph for forensic detail; use curated outputs for Agent/tool integration experiments.
