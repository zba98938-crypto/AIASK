# Model Providers, Memory, Sessions, And Runs

## Model Providers

Implementation:

- `model_providers.py`
- `model_client.py`

Current concepts include provider credentials/specs, usage accounting, OpenAI-compatible clients, mock client fallback, provider pools, fallback order, and error classification for auth, rate limit, timeout, network, and provider failures.

HTTP routes:

- `/v1/ai/status`
- `/v1/ai/smoke`
- `/v1/ai/models`

Do not expose raw API keys. Status output should describe configured/missing/provider IDs without secrets.

## Memory

Implementation:

- `memory.py`
- `memory_providers.py`

Tools:

- `agent_memory_save`
- `agent_memory_search`
- `agent_memory`
- `agent_memory_manage`

Memory stores financial/user/research content with user, symbol, strategy, topic, and content metadata. Keep durable backend behavior explicit and do not silently degrade semantic/external provider failures.

## Sessions, Runs, Events, And Search

Implementation:

- `session_store.py`

HTTP routes:

- `/v1/desktop/workbench/summary`
- `/v1/desktop/runs`
- `/v1/hermes/sessions`
- `/v1/sessions/{session_id}/messages`
- `/v1/runs/{run_id}`
- `/v1/runs/{run_id}/events`
- `/v1/runs/{run_id}/events/stream`
- `/v1/search`

Desktop expects recent sessions, recent runs, normalized run events, approval markers, local profile/data-policy state, and resume-session behavior. Keep run event payloads stable enough for the current workbench and sessions/runs surfaces in `desktop/src/App.tsx` and `desktop/src/pages/AgentPages.tsx`.

## Tests

- `packages/agent/tests/test_ai_status_and_smoke.py`
- `packages/agent/tests/test_desktop_workbench_contracts.py`
- `packages/agent/tests/test_session_memory_todo.py`
- `desktop/src/services/aiaskApi.test.ts`
- `desktop/e2e/aiask-v1.spec.ts`
