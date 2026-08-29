# AIASK Map And Rules

## Source Of Truth

The repository is moving faster than historical docs and user-level skills. For every AIASK task, inspect current implementation files, package manifests, tests, and graph artifacts before making claims or edits.

Use Graphify artifacts as architecture evidence when present, but verify important conclusions against code:

- `reports/code-graph/full-2026-05-29/curated/CURATED_SUMMARY.json`
- `reports/code-graph/full-2026-05-29/curated/endpoint-map.json`
- `reports/code-graph/full-2026-05-29/curated/cross-package-edges.json`

Those graph files predate several June changes, so treat them as baseline evidence, not complete truth.

## Package Map

### `packages/agent`

FastAPI Agent runtime and system control plane. It owns:

- HTTP APIs for Desktop, model responses, runs, sessions, intents, approvals, MCP aggregation, skills, plugins, gateway, connectors, jobs, webhooks, RL, learning, and diagnostics.
- Model-visible `agent_*` tool registry and toolset policy.
- ActionIntent approval flow and control-token gated operations.
- Native/full adapters for file, terminal, browser, web, media, todo, memory, jobs, gateway, Home Assistant, Feishu, Discord, RL, MoA, security, plugins, and skills.

### `desktop`

React/Vite/Tauri workbench. It owns UI, orchestration views, mode controls, and mock/live UX. It consumes Agent HTTP only.

Current primary areas:

- Workbench chat and recent sessions/runs.
- Agent pages: Sessions, Runs/Events, Tools/Intents/Approvals, Gateway, Readiness/Health, MCP/Connectors, Plugins/Skills.
- Finance workspaces: Financial Manager, Quant Research, Strategy Factory, Factor Factory, Incubation Factory, Data, Automation, Workflows, Factory Events.
- Legacy/Advanced views remain as migration fallbacks.

### `packages/akshare-mcp`

Financial MCP server, data acquisition, tool surface, resources/prompts, manager plane, strategy/quant/research services, TDX/TQCenter routing, data readiness, vector/search, stock analysis, factor research, incubation, paper/live/execution manager support, and theme/event graph operations.

### `packages/strategy-factory`

Strategy Factory domain package. Current important areas include scheduler/cycle pipeline, observe-first and stock-first modes, admission authority, stock strategy router, trade prediction contract, budget feedback, readiness service, paper trading bridge, theme exposure/regression, quality gates, backtest filter, dedupe, submitter, elimination, and public facade contracts.

### `packages/aiask-quant-core`

Shared quant primitives without MCP or Strategy Factory ownership. It owns backtest engines, factor calculators, DSL/runtime support, SQLite schemas and storage, strategy trade prediction, strategy/factory JSON budget helpers, vector storage, TDX storage, kline validation, signal tracking, slippage, and risk primitives.

### `packages/finance-mcp-servers`

External finance software MCP servers for Tongdaxin, Tonghuashun, Eastmoney, and QMT. Read-only market/account operations and live order/cancel operations must stay separated by broker-token guardrails.

### `scripts` and root launchers

Factory and ops launchers have moved toward `scripts/factories/` and `scripts/ops/`, with compatibility root scripts still present or deleted depending on the current branch. Inspect current files before referencing a runner.

## Non-Negotiable Boundaries

- Desktop -> Agent HTTP only.
- Agent -> model-visible tools through `agent_*` facade only.
- AKShare managers are internal/provider-facing; Desktop and model tools must not expose raw manager names.
- Strategy, factor, incubation, execution, gateway delivery, plugin mutation, file/terminal/browser writes, and live trading are side-effectful and must keep visible guardrails.
- Live trading/order paths require explicit token/confirmation and rejected envelopes that surface trade-risk metadata.
- Mock e2e proves UI behavior, not live backend readiness.

## Skill Sync Policy

AIASK uses project-level skills as the long-term source and user-level skills as a local mirror.

Update order:

1. Edit `.codex/skills/aiask-*`.
2. Validate the project-level skill folders.
3. Mirror to `C:\Users\walking\.codex\skills\aiask-*`.
4. Compare file lists and hashes for all mirrored AIASK skills.

Do not manually diverge user-level copies unless the user explicitly asks for a personal override.
