---
name: aiask-agent-runtime
description: Use this skill when working on the AIASK Agent runtime, FastAPI routes, HTTP contracts, model-visible `agent_*` tools, toolset policy, ActionIntent and approval flows, run/session storage, financial manager APIs, or Agent safety envelopes.
version: 1.0.0
protocol_version: "1.0"
capabilities:
  - agent_runtime.develop
required_mcp: []
side_effect: read_only
---

# AIASK Agent Runtime

## Workflow

1. Read [references/toolsets-and-tools.md](references/toolsets-and-tools.md) before changing tool names, schemas, categories, toolset gates, MCP wrappers, or model-visible capabilities.
2. Read [references/http-api-and-intents.md](references/http-api-and-intents.md) before changing `server.py`, HTTP API contracts, runs/events/sessions, approvals, or ActionIntent behavior.
3. Keep all model-visible tools behind the `agent_*` facade.
4. Keep FastAPI and fallback route implementations aligned and cover contract changes with backend tests.
5. Preserve structured failure, side-effect metadata, trace data, and control-token gates.

## Hard Rules

- `packages/agent/src/aiask_agent/tools/policy.py` is the naming and toolset authority.
- Default toolset is `finance_safe`; `general_full` requires explicit env enablement.
- Raw manager names and raw MCP stateful actions must not become model-visible tools.
- Durable or external state changes must use ActionIntent, approval routes, or equivalent explicit guardrails.
- Do not leak secrets, raw `.env` values, stack traces, or third-party credential details through HTTP responses.

## Key Files

- `packages/agent/src/aiask_agent/server.py`
- `packages/agent/src/aiask_agent/tool_registry.py`
- `packages/agent/src/aiask_agent/tools/catalog.py`
- `packages/agent/src/aiask_agent/tools/schemas.py`
- `packages/agent/src/aiask_agent/intents.py`
- `packages/agent/src/aiask_agent/session_store.py`
- `packages/agent/src/aiask_agent/adapters/desktop_ops.py`
