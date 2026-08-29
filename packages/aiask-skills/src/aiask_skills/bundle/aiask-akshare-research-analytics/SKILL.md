---
name: aiask-akshare-research-analytics
description: Use this skill when working on AIASK AKShare MCP research and analytics tools, stock deep analysis, strategy review workflows, LLM provider runtime, staged strategy generation, factor research/catalog, regime/profile analysis, vector/search/governance, sentiment/news, valuation, decision, alerts, portfolio/backtest, conformal/data validation, or analytics tests.
version: 1.0.0
protocol_version: "1.0"
capabilities:
  - research.analytics.develop
required_mcp:
  - aiask-akshare
side_effect: read_only
---

# AIASK AKShare Research Analytics

## Workflow

1. Read [references/financial-tool-surface.md](references/financial-tool-surface.md) before changing MCP financial tools, stock analysis workflows, backtest/portfolio/valuation/decision tools, alerts, or argument contracts.
2. Read [references/research-decision-vector-sentiment.md](references/research-decision-vector-sentiment.md) before changing LLM strategy generation, factor research, vector/search, sentiment/news, stock deep analysis, or research workflow contracts.
3. Preserve structured tool outputs and explicit degraded/fallback states.
4. Keep stateful strategy or manager actions behind manager protocol and Agent facades.
5. Add focused tests for statistical, data-quality, LLM parsing, or vector contract changes.

## Key Files

- `packages/akshare-mcp/src/akshare_mcp/tools/`
- `packages/akshare-mcp/src/akshare_mcp/services/`
- `packages/akshare-mcp/src/akshare_mcp/services/strategy_spec/`
- `packages/akshare-mcp/src/akshare_mcp/services/factor_mining_factory/`
- `packages/akshare-mcp/tests/`
