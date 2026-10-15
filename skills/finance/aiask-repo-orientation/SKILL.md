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
5. Skills live in two places that must stay identical: `skills/finance/<name>/SKILL.md` and `packages/aiask-skills/src/aiask_skills/bundle/<name>/SKILL.md`. Edit both. `packages/aiask-skills/tests/test_bundle.py` checks they agree.

## Hard Rules

- This tree is backend-only: there is no `desktop/` directory and no frontend. Clients use the Agent HTTP API or MCP directly; they must not import Python packages or call managers directly.
- Model-visible Agent tools must be named `agent_*`.
- Direct manager names must not be exposed as model-visible Agent tools.
- Stateful, external-platform, strategy-lifecycle, or trade-risk actions require explicit guardrails, usually ActionIntent plus control token.
- Do not read, copy, quote, or document secret values from `.env`; document environment variable names only.
- Do not operate on live trading software, runtime databases, logs, caches, or broker state unless the user explicitly asks for that operation.

## Common Entrypoints

- Agent runtime: `packages/agent/src/aiask_agent/server.py`
- Agent tool policy/catalog/schemas: `packages/agent/src/aiask_agent/tools/`
- AKShare MCP server: `packages/akshare-mcp/src/akshare_mcp/server.py`
- Strategy Factory facade: `packages/strategy-factory/src/strategy_factory/api/facade.py`
- Quant Core storage/backtest primitives: `packages/aiask-quant-core/src/aiask_quant_core/`
- Finance MCP servers: `packages/finance-mcp-servers/src/aiask_finance_mcp/`
