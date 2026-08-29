# Agent Toolsets And Tool Registry

## Policy Authority

Primary file: `packages/agent/src/aiask_agent/tools/policy.py`.

Important constants and behavior:

- Agent tool names must start with `agent_`.
- `finance_safe` is the default toolset.
- `general_full` is available only when environment policy enables full/general tools.
- Direct manager tokens such as `strategy_manager`, `live_trading_manager`, `paper_trading_manager`, `execution_manager`, `available_tools`, and `get_tool_contract` are forbidden in model-visible Agent tool names.

## Finance-Safe Tool Surface

Finance-safe tools include read-oriented and guarded financial workflows such as:

- Tool/catalog and stock analysis: `agent_tool_catalog`, `agent_analyze_stock`.
- Governance/data/quant gates: `agent_governance_check`, `agent_data_validation`, `agent_quant_data_gate`, `agent_factor_validation`, `agent_backtest_suite`, `agent_portfolio_risk`, `agent_quant_research_run`.
- Strategy Factory read facades: `agent_factory_status`, `agent_factory_runs`, `agent_strategy_review_snapshot`, `agent_strategy_domain_events`.
- Factory event read facades: `agent_factory_event_list`, `agent_factory_event_preview_tasks`, `agent_factory_event_lineage`, `agent_factory_theme_exposure_status`, `agent_factory_event_outbox_status`.
- Incubation read facade: `agent_incubation_factory_status`.
- ActionIntent: `agent_action_intent_create`, `agent_action_intent_get`.

Stateful financial or strategy actions should be represented as intents rather than raw manager calls.

## General/Full Tool Surface

General/full tools cover high-permission native capabilities:

- Files, terminal/processes, code execution, terminal backends, TUI status.
- Browser automation, CDP, web search/extract, X search.
- Vision, image, video, text-to-speech, transcription.
- Todo/subgoal/delegation/jobs/cron.
- Skills, skill packs, plugins, MCP manage, ACP manage.
- Model and memory management, session search/handoff, memory save/search.
- Security scan and advisory checks.
- Gateway/platform delivery, Home Assistant, Feishu/Lark, Discord.
- Learning loop, MoA, RL environments/config/runs/results/logs.

These capabilities are not a replacement for `finance_safe`; they require explicit full-mode policy and control-token treatment in Desktop.

## Schema And Catalog Files

- `packages/agent/src/aiask_agent/tools/catalog.py` lists model-visible tool descriptors.
- `packages/agent/src/aiask_agent/tools/schemas.py` defines JSON schemas.
- `packages/agent/src/aiask_agent/tool_registry.py` registers tools and adapts MCP/manager behavior.
- `packages/agent/src/aiask_agent/capabilities.py` maps Hermes/reference capabilities to AIASK `agent_*` capabilities.

When adding a tool, update catalog, schema, registry behavior, tests, and Desktop capability inventory if user-visible.

## Safety Invariants

- MCP financial tools may be wrapped by Agent facades, but Desktop and model tools should not call raw MCP stateful actions directly.
- Tool responses should keep `aiask_envelope` style shape and side-effect metadata.
- Read-only tools must remain read-only; mutation should be explicit and auditable.
- Plugin and dynamic MCP tools require poisoning/name-confusion review before becoming model-visible.
