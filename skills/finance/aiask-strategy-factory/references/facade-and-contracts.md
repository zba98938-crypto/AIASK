# Strategy Factory Facade And Contracts

## Public Facade

Primary files:

- `packages/strategy-factory/src/strategy_factory/__init__.py`
- `packages/strategy-factory/src/strategy_factory/api/facade.py`
- `packages/strategy-factory/src/strategy_factory/api/dto_parts/runtime.py`

Use the facade or established application service boundaries for public behavior. Avoid importing deep internals from AKShare MCP or Agent.

## Current Contract Areas

Important current application modules include:

- `cycle_pipeline.py` and `cycle_runner.py`.
- `factory_execution.py`, `factory_scheduler.py`, and scheduler loop/runtime/analysis parts.
- `services/admission_authority.py`, `candidate_pipeline.py`, `readiness_service.py`, `submission_coordinator.py`, `lifecycle_coordinator.py`.
- `stock_strategy_router.py`, `stock_strategy_matrix.py`, and matrix/router policy parts.
- `trade_prediction_contract.py`.
- `research/`: paper trading bridge, theme graph, theme exposure builder, theme response regression, walk-forward, statistical robustness, factor effectiveness, target baskets.
- `_budget_feedback*`, `_combined_scan_report.py`, `_cycle_success_summary.py`.
- `submission_gate/` and `_submitter_actions/`.

## Agent And Manager Boundary

AKShare MCP owns `strategy_manager` integration and manager protocol. Agent owns model-visible `agent_*` facades and ActionIntent routes.

Agent-facing safe tools include read/status operations such as factory status/runs, review snapshot, domain events, incubation status, and factory event read facades. Mutations should use ActionIntent.

## Tests

Contract-sensitive tests include:

- `test_public_contracts.py`
- `test_package_decoupling_boundary.py`
- `test_runtime_provider_boundary.py`
- `test_trade_prediction_contract_p0.py`
- `test_no_live_trading_boundary.py`
- `test_strategy_factory_quality_fixes.py`
