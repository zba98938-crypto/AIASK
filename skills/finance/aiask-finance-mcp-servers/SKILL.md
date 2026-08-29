---
name: aiask-finance-mcp-servers
description: Use this skill when working on AIASK finance MCP servers for Tongdaxin, Tonghuashun, Eastmoney, or QMT; market/account/order tool surfaces; stdio JSON-RPC handlers; package entrypoints; optional broker dependencies; or broker-token live trading guardrails.
version: 1.0.0
protocol_version: "1.0"
capabilities:
  - finance_mcp.develop
required_mcp: []
side_effect: read_only
---

# AIASK Finance MCP Servers

## Workflow

1. Read [references/server-surfaces.md](references/server-surfaces.md) before changing server entrypoints, JSON-RPC handlers, tool names, schemas, account/market tools, or optional dependency behavior.
2. Read [references/trade-risk-guard.md](references/trade-risk-guard.md) before touching order placement, cancellation, broker tokens, side-effect envelopes, or trade-risk metadata.
3. Keep read-only quote/account/position/order-query tools separate from live order/cancel tools.
4. Do not remove per-call broker-token requirements for live trading paths.
5. Add negative tests for missing/mismatched broker token when changing live paths.

## Key Files

- `packages/finance-mcp-servers/pyproject.toml`
- `packages/finance-mcp-servers/src/aiask_finance_mcp/tongdaxin/server.py`
- `packages/finance-mcp-servers/src/aiask_finance_mcp/tonghuashun/server.py`
- `packages/finance-mcp-servers/src/aiask_finance_mcp/eastmoney/server.py`
- `packages/finance-mcp-servers/src/aiask_finance_mcp/qmt/server.py`
- `packages/finance-mcp-servers/src/aiask_finance_mcp/_shared/trade_guard.py`
