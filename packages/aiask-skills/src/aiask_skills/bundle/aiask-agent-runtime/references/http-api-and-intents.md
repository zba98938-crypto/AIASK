# Agent HTTP API And Intents

## Current Route Families

Primary file: `packages/agent/src/aiask_agent/server.py`.

The server exposes FastAPI routes and a fallback/simple ASGI path in the same file. When editing route behavior, keep both paths aligned if the fallback branch handles that endpoint.

Important route groups:

- Health and capability inventory: `/health`, `/health/detailed`, `/v1/tools`, `/v1/desktop/capabilities`, `/v1/capabilities/parity`, `/v1/hermes/status`, `/v1/hermes/readiness`, `/v1/financial-system/readiness`.
- Desktop settings/data/profile: `/v1/desktop/settings/status`, `/v1/desktop/data/status`, `/v1/desktop/data/sync-plan`, `/v1/desktop/users/local-profile`.
- Workbench history and run control: `/v1/desktop/workbench/summary`, `/v1/desktop/runs`, `/v1/hermes/sessions`, `/v1/sessions/{session_id}/messages`, `/v1/runs/{run_id}`, `/v1/runs/{run_id}/events`, `/v1/runs/{run_id}/events/stream`, `/v1/runs/{run_id}/cancel`, `/v1/runs/{run_id}/stop`, `/v1/runs/{run_id}/steer`.
- Model and response APIs: `/v1/ai/status`, `/v1/ai/smoke`, `/v1/ai/models`, `/v1/responses`, `/v1/responses/{response_id}`, `/v1/chat/completions`, `/v1/search`.
- Desktop quant and financial manager: `/v1/desktop/quant/presets`, `/v1/desktop/quant/research-runs`, `/v1/desktop/quant/research-runs/{id}/report`, `/v1/desktop/financial-manager/catalog`, `/v1/desktop/financial-manager/status`, `/v1/desktop/financial-manager/query`, `/v1/desktop/financial-manager/intent`.
- Tool invocation: `/v1/tools/{tool_name}`, `/v1/hermes/admin/tools/{tool_name}`, `/v1/hermes/toolsets`, `/v1/hermes/tools`, `/v1/hermes/config`.
- ActionIntent and approvals: `/intents`, `/intents/{intent_id}`, `/intents/{intent_id}/confirm`, `/intents/{intent_id}/deny`, `/v1/approvals`, `/v1/approvals/{approval_id}/{decision}`.
- Full/native controls: `/v1/processes`, `/v1/terminal/backends`, `/v1/terminal/sessions`, `/v1/browser/sessions`.
- Skills/plugins/MCP: `/v1/skills`, `/v1/plugins`, `/v1/plugins/{name}/commands`, `/v1/plugins/{name}/tools/{tool}/test`, `/v1/mcp/servers`, `/v1/mcp/tools`, `/v1/mcp/resources`, `/v1/mcp/prompts`, `/v1/mcp/oauth_status`, `/v1/mcp/register-local`, `/v1/mcp/discover`, `/v1/mcp/oauth/start`, `/v1/mcp/oauth/callback`, `/v1/mcp/resources/read`, `/v1/mcp/prompts/get`.
- Gateway/connectors/webhooks/jobs/RL/learning: `/v1/gateway/*`, `/v1/connectors/*`, `/v1/webhooks/*`, `/v1/jobs/*`, `/v1/rl/*`, `/v1/learning/*`.

## ActionIntent Rules

Agent tools include `agent_action_intent_create` and `agent_action_intent_get`; HTTP routes expose list/get/create/confirm/deny.

Use ActionIntent for:

- Strategy Factory mutation and factory event mutation.
- Data sync plans that write or schedule work.
- Financial Manager stateful actions.
- Gateway send/direct-deliver actions.
- Webhook trigger actions.
- Plugin/skill/job operations that cross control boundaries.

Do not perform a stateful operation immediately just because a Desktop button exists. Desktop should create or confirm the intent through Agent routes, and the Agent should enforce control-token and side-effect policy.

## Desktop Contract Expectations

Desktop expects structured JSON, not stringly typed hidden state. Keep:

- `object` or `aiask_envelope` style fields where already used.
- `success`, `data`, `error`, `error_code`, `meta`, `side_effect`, and `secrets_redacted` semantics where present.
- Gated responses explicit: control token missing, full mode disabled, unavailable backend, unsupported connector, or degraded data should be visible.

When route fields change, update:

- `desktop/src/services/aiaskApi.ts`
- `desktop/src/mock/mockData.ts`
- Relevant Desktop tests and Agent contract tests.

## Tests To Consider

- `packages/agent/tests/test_desktop_workbench_contracts.py`
- `packages/agent/tests/test_financial_manager_desktop_api.py`
- `packages/agent/tests/test_desktop_capabilities_api.py`
- `packages/agent/tests/test_intents.py`
- `packages/agent/tests/test_gateway_daemon*.py`
- `packages/agent/tests/test_endpoint_drift_gate.py`
- `desktop/src/services/aiaskApi.test.ts`
