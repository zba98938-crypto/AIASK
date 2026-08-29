# AKShare MCP Runtime And Tool Surface

## Entrypoint And Profiles

Primary file: `packages/akshare-mcp/src/akshare_mcp/server.py`.

The server creates FastMCP app "AKShare Stock Data Server v2" and supports startup profiles:

- `full`: default.
- `worker`: worker-oriented.
- `tool-only`, `tool_only`, `lite`: disables background schedulers/startup validators and excludes manager-heavy modules where configured.

Transports are controlled by `MCP_TRANSPORT` and include stdio, SSE, and streamable HTTP. HTTP transports must keep origin/auth/token and loopback safety.

## Tool Surface

Core/lazy modules include market, finance, fund flow, macro, news, options, technical, backtest, portfolio, valuation, decision, search, semantic, research, data warmup, alerts, AI workflows, governance workflow, adapter tools, basic data, key levels, stop levels, trade plan, db freshness, managers, resources, prompts, market blocks, vector, skills, quant, sentiment, data sync, and factor profile.

When changing registration:

- Keep startup costs controlled by the existing eager/lazy split.
- Update tool contract tests when argument names or structured outputs change.
- Preserve resource/prompt registration and runtime skill scan behavior.

## Runtime Skill Tools

Files:

- `packages/akshare-mcp/src/akshare_mcp/tools/skills_registry.py`
- `packages/akshare-mcp/src/akshare_mcp/tools/skills.py`

These expose AKShare MCP runtime skill contracts and may scan repo/user skill directories. They are separate from Codex skills used by this assistant.

## Tests

Useful tests:

- `test_tool_argument_contract.py`
- `test_tool_catalog_vector_contracts.py`
- `test_mcp_full_tool_regression_fixes.py`
- `test_skill_capability_audit.py`
- `test_provider_contracts.py`
