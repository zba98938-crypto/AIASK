# Quant Core Storage And Contracts

## Package Role

`packages/aiask-quant-core` owns shared quant primitives and storage contracts. It should not own Agent HTTP, AKShare MCP tool registration, Strategy Factory orchestration, or broker integration.

## SQLite Areas

Primary folder: `packages/aiask-quant-core/src/aiask_quant_core/storage/sqlite`.

Important areas:

- Market schemas split across `_schema_market_phase_*`.
- Strategy schemas in `schema_strategy.py` and `schema_strategy_parts/`.
- Strategy AI, strategy runtime, strategy incubation, strategy vector, and signal tracking storage.
- TDX storage in `tdx_storage.py`.
- Vector unified storage/index/doc helpers.
- Strategy factory JSON budget helpers in `strategy_factory_json_budget.py`.
- Strategy trade prediction in `strategy_trade_prediction.py`.

## Current Contract-Sensitive Tests

- `test_strategy_trade_prediction_p0.py`: schema initialization, JSON round-trip, hash mutation rejection.
- `test_storage_json_caps.py`: caps large strategy/factory JSON fields and backup behavior.
- `test_kline_index_validator_fix10.py`: index/stock code validation.
- `test_list_signal_forward_returns.py`: signal forward return listing.

## Rules

- Schema changes need focused tests and compatibility review for AKShare MCP and Strategy Factory.
- JSON caps should preserve latest useful summaries while preventing table bloat.
- Store provenance and integrity metadata where existing storage contracts expect it.
- Do not rewrite stored strategy/prediction semantics without explicit migration and tests.
