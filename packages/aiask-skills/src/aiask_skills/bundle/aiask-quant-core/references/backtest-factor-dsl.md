# Quant Core Backtest, Factor, DSL, And Runtime

## Backtest

Primary folder: `packages/aiask-quant-core/src/aiask_quant_core/backtest`.

Important areas:

- Engines and runtime support in `engine.py`, `engine_parts/`, and `_engine_support*`.
- Built-in, single-factor, multi-factor, fundamental, macro timing, DSL, and expanded factory strategies.
- Parallel backtest helpers and artifacts/analytics.

Changing execution semantics, costs, slippage, sample windows, benchmark behavior, or returned metrics requires tests because Strategy Factory and AKShare MCP may interpret historical outputs.

## Factors And Data Pipeline

Primary folders:

- `factor_calculator/`: technical, volume, volatility, fundamental, analysis.
- `data_pipeline/`: condition stats, cross-section utilities.
- `risk_model.py` and `slippage.py`.

Keep numerical behavior stable and explicit. Do not silently swap formulas or units.

## Strategy DSL

Primary files:

- `strategy_dsl.py`
- `strategy_dsl_parts/context.py`
- `strategy_dsl_parts/runtime.py`
- `strategy_dsl_parts/specs.py`

The DSL is used by generated strategy flows. Preserve validation errors, runtime assumptions, and compatibility with Strategy Factory generated specs.

## Integration Boundaries

- AKShare MCP may adapt Quant Core features into MCP tools.
- Strategy Factory may consume backtest, storage, DSL, and trade prediction contracts.
- Agent and Desktop should reach Quant Core only through their owning packages.
