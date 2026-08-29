---
name: aiask-agent-memory-model-learning-rl
description: Use this skill when working on AIASK Agent model providers, OpenAI-compatible provider pools, model fallback/error classification, AI status/smoke/models APIs, financial memory, session/run/event storage, search, local user data and learning datasets, learning proposals, skill reflection, mixture-of-agents, RL Atropos environments/runs/config/logs/results, or voice/model-adjacent configuration.
version: 1.0.0
protocol_version: "1.0"
capabilities:
  - agent_memory.develop
required_mcp: []
side_effect: read_only
---

# AIASK Agent Memory Model Learning RL

## Workflow

1. Read [references/model-memory-session.md](references/model-memory-session.md) before changing providers, model clients, AI status/smoke, memory, sessions, runs, events, or search.
2. Read [references/learning-moa-rl-voice.md](references/learning-moa-rl-voice.md) before changing learning proposals, skill reflection, MoA, RL environments/runs/config, or voice provider configuration.
3. Do not log or write secret values from provider env vars.
4. Keep status/readiness explicit for missing providers, mock clients, unavailable RL backends, and placeholder voice branches.
5. Update Desktop and Agent tests when route shapes or run/session metadata change.

## Key Files

- `packages/agent/src/aiask_agent/model_providers.py`
- `packages/agent/src/aiask_agent/model_client.py`
- `packages/agent/src/aiask_agent/memory.py`
- `packages/agent/src/aiask_agent/memory_providers.py`
- `packages/agent/src/aiask_agent/session_store.py`
- `packages/agent/src/aiask_agent/session_store_user_data.py`
- `packages/agent/src/aiask_agent/learning_loop.py`
- `packages/agent/src/aiask_agent/moa.py`
- `packages/agent/src/aiask_agent/rl_atropos.py`
- `packages/agent/src/aiask_agent/voice.py`
