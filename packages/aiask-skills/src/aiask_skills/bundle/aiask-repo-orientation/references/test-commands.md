# AIASK Test Commands

Choose the smallest verification set that covers the risk surface.

## Desktop

Run from `desktop/`:

- Type/API contract changes: `npm run typecheck`
- Component/service changes: `npm test`
- Bundle/build changes: `npm run build`
- Mock UI workflows: `npm run test:e2e:mock`
- Optional live smoke: `npm run test:e2e:live` with a real Agent already configured

Current e2e mock suite covers MCP controls, Strategy Factory panel, Hermes parity tables, AI status/smoke/models/workbench response flow, responsive capabilities UI, Data & Sync intent flow, full page matrix, and safe mock controls.

## Agent

Use targeted `uv run pytest` commands from repo root unless the package requires a local working directory.

High-value targeted tests:

- `uv run pytest packages/agent/tests/test_tool_registry.py`
- `uv run pytest packages/agent/tests/test_desktop_workbench_contracts.py`
- `uv run pytest packages/agent/tests/test_financial_manager_desktop_api.py`
- `uv run pytest packages/agent/tests/test_desktop_capabilities_api.py`
- `uv run pytest packages/agent/tests/test_intents.py`
- `uv run pytest packages/agent/tests/test_gateway_daemon.py packages/agent/tests/test_gateway_daemon_phase2.py packages/agent/tests/test_gateway_daemon_phase4.py`
- `uv run pytest packages/agent/tests/test_endpoint_drift_gate.py`

## Strategy Factory

Targeted tests:

- `uv run pytest packages/strategy-factory/tests/test_cycle_pipeline.py`
- `uv run pytest packages/strategy-factory/tests/test_candidate_pipeline_observe_first.py`
- `uv run pytest packages/strategy-factory/tests/test_stock_strategy_router_p1_2.py`
- `uv run pytest packages/strategy-factory/tests/test_trade_prediction_contract_p0.py`
- `uv run pytest packages/strategy-factory/tests/test_runtime_toggles.py`
- `uv run pytest packages/strategy-factory/tests/test_readiness_service.py`

Run the full package only when risk justifies it:

- `cd packages/strategy-factory; pytest -q`

## AKShare MCP

Pick tests by domain:

- Data source/TDX/TQCenter: `test_data_source_tdx_routing.py`, `test_tdx_phase3_tools_e2e.py`, `test_tdx_storage_phase8.py`
- Data readiness/freshness: `test_p0_1_data_readiness.py`, `test_warmup_audit_scripts_contract.py`
- Factor mining/research: `test_factor_catalog_p1_3.py`, `test_qc_pipeline_p2_1.py`, `test_batch_ic_neutralize_p0_2.py`
- Incubation/hit-rate: `test_hit_rate_matrix_p3_1.py`, `test_hit_rate_reporter_matrix_p3_1.py`
- Theme/event graph: `test_theme_graph_schema.py`
- Manager/contracts: `test_strategy_mgr_capabilities_health.py`, `test_tool_argument_contract.py`

## Quant Core

Targeted tests:

- `uv run pytest packages/aiask-quant-core/tests/test_strategy_trade_prediction_p0.py`
- `uv run pytest packages/aiask-quant-core/tests/test_storage_json_caps.py`
- `uv run pytest packages/aiask-quant-core/tests/test_kline_index_validator_fix10.py`
- `uv run pytest packages/aiask-quant-core/tests/test_list_signal_forward_returns.py`

## Finance MCP Servers

Use package tests and negative guardrail checks. Do not run commands that place or cancel real orders unless the user explicitly requests live trading validation and provides the required guarded environment.
