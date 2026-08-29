# MCP, Plugins, Skills, And Connectors

## MCP Aggregation

Agent MCP APIs expose:

- `/v1/mcp/servers`
- `/v1/mcp/tools`
- `/v1/mcp/resources`
- `/v1/mcp/prompts`
- `/v1/mcp/oauth_status`
- `/v1/mcp/register-local`
- `/v1/mcp/discover`
- `/v1/mcp/oauth/start`
- `/v1/mcp/oauth/callback`
- `/v1/mcp/resources/read`
- `/v1/mcp/prompts/get`

Keep discovery, resource read, prompt get, and OAuth flows gated as currently designed. Dynamic MCP tools must not bypass `agent_*` policy or side-effect classification.

## Plugins And Runtime Skills

Agent plugin and skill APIs include:

- `/v1/skills`, `/v1/skills/{name}`
- `/v1/plugins`, `/v1/plugins/{name}`
- `/v1/plugins/{name}/tools/{tool}/test`
- `/v1/plugins/{name}/commands`
- `/v1/plugins/{name}/commands/{command}/test`

Important files:

- `plugin_runtime.py`
- `skill_packs.py`
- `native_skill_store.py`
- `financial_skill_templates.py`

Codex skills under `.codex/skills` are separate from Agent runtime skills and AKShare MCP runtime skill contracts.

## Connectors

Agent connector APIs include:

- `/v1/connectors`
- `/v1/connectors/summary`
- `/v1/connectors/{connector_type}/{name}`
- `/v1/connectors/{connector_type}/{name}/test`

Desktop uses these through MCP/Connectors and settings/integrations pages. Keep connector failures structured and do not expose secret values.

## Tests

- `packages/agent/tests/test_mcp_client.py`
- `packages/agent/tests/test_native_full_parity.py`
- `packages/agent/tests/test_hermes_full_expanded_capabilities.py`
- `desktop/src/services/aiaskApi.test.ts`
- `desktop/e2e/aiask-v1.spec.ts`
