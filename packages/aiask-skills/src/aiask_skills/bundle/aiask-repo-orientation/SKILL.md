---
name: aiask-repo-orientation
description: Use this skill when working in the AIASK repository and you need current package boundaries, source-of-truth rules, safety invariants, command selection, skill-sync policy, or a code-first orientation before planning, reviewing, or editing AIASK code.
version: 1.0.0
protocol_version: "1.0"
capabilities:
  - repo.orient
required_mcp: []
side_effect: read_only
---

# AIASK Repo Orientation

## Workflow

1. Treat current code, manifests, tests, and graph artifacts as source of truth. Use docs and prior conversations only as supporting context.
2. Read [references/map-and-rules.md](references/map-and-rules.md) before cross-package planning, reviews, or edits.
3. Read [references/test-commands.md](references/test-commands.md) before choosing verification commands.
4. Keep changes inside package ownership boundaries unless the task explicitly crosses them.
5. When updating Codex skills, update project-level `.codex/skills` first, then mirror AIASK skills to `C:\Users\walking\.codex\skills`.

## Hard Rules

- Desktop talks to the Agent HTTP API only; it must not import Python packages, call MCP tools directly, or call managers directly.
- Model-visible Agent tools must be named `agent_*`.
- Direct manager names must not be exposed as model-visible Agent tools.
- Stateful, external-platform, strategy-lifecycle, or trade-risk actions require explicit guardrails, usually ActionIntent plus control token.
- Do not read, copy, quote, or document secret values from `.env`; document environment variable names only.
- Do not operate on live trading software, runtime databases, logs, caches, or broker state unless the user explicitly asks for that operation.

## Common Entrypoints

- Agent runtime: `packages/agent/src/aiask_agent/server.py`
- Agent tool policy/catalog/schemas: `packages/agent/src/aiask_agent/tools/`
- Desktop API client: `desktop/src/services/aiaskApi.ts`
- Desktop view registry: `desktop/src/views.ts`
- AKShare MCP server: `packages/akshare-mcp/src/akshare_mcp/server.py`
- Strategy Factory facade: `packages/strategy-factory/src/strategy_factory/api/facade.py`
- Quant Core storage/backtest primitives: `packages/aiask-quant-core/src/aiask_quant_core/`
- Finance MCP servers: `packages/finance-mcp-servers/src/aiask_finance_mcp/`
