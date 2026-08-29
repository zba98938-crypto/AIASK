# AKShare Manager Catalog

## Manager Areas

Manager modules currently cover:

- Strategy manager lifecycle, CRUD, review, closure review, domain events, factory status/runs, factory events, theme exposure, event outbox, vector health, runtime controls.
- Quant manager classic and model/factor registry actions, factor candidate registry, factor research memory, scheduler status/run, batch compute, IC and neutralization flows.
- Risk manager VaR, stress, exposure, concentration, liquidity, runtime risk events.
- Portfolio, watchlist, user, data sync, paper trading, live trading, and execution managers.
- Execution audit, paper observation, diagnostic observation, promotion/closure/incubation surfaces.

Inspect current files under `tools/managers/` before assuming action names; many handlers are split into support modules.

## Contracts

Important contracts and compatibility layers:

- `contracts/strategy_manager_contract.py`
- `tools/manager_protocol.py`
- `services/_quant_core_compat.py`
- Agent adapters under `packages/agent/src/aiask_agent/adapters/`

Manager actions consumed by Agent or Desktop should have stable envelopes and explicit side-effect metadata.

## Tests

Useful tests:

- `test_strategy_mgr_capabilities_health.py`
- `test_strategy_factory_ownership.py`
- `test_factory_db_only_boundary.py`
- `test_paper_order_bridge_idempotency.py`
- `test_paper_trading_reconcile.py`
- `test_execution_audit_*`
- Agent `test_strategy_factory_adapter_ownership.py`
