# AKShare Financial Tool Surface

## Tool Areas

AKShare MCP tools cover:

- Market, finance, fund flow, macro, news, options, technical, basic data, market blocks.
- Backtest and portfolio optimization/risk/stress.
- Valuation, decision, key levels, stop levels, trade plan.
- Alerts and data warmup.
- AI workflows and governance workflow.
- Stock research/deep analysis and argument-contract compatibility.

Keep argument aliases stable where tests assert them, especially stock code/symbol aliases.

## Data And Failure Semantics

Financial outputs should expose:

- Data source and freshness where available.
- Missing optional dependency, optional source unavailable, and degraded/fallback status.
- Parameter and argument errors as structured failures.
- No silent local fallback after bad LLM/provider output where tests require suppression.

## Tests

Useful tests:

- `test_stock_deep_analysis.py`
- `test_tool_argument_contract.py`
- `test_fix_valuation_consensus.py`
- `test_failed_tool_regressions.py`
- `test_full_chain_regression_repairs.py`
- `test_data_validation_adapter_contract.py`
- `test_conformal_adapter_runtime.py`
- `test_baostock_optional_dependency.py`
