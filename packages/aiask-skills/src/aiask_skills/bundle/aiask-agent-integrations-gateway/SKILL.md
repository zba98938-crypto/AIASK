---
name: aiask-agent-integrations-gateway
description: Use this skill when working on AIASK Agent integrations including MCP aggregation, ACP-provided MCP servers, native plugins, runtime skills/skill packs, unified connectors, Gateway daemon/platforms/messages/directory, webhooks, Home Assistant, Feishu/Lark, Discord, and cross-platform delivery through Agent HTTP or `agent_*` tools.
version: 1.0.0
protocol_version: "1.0"
capabilities:
  - agent_gateway.develop
required_mcp: []
side_effect: read_only
---

# AIASK Agent Integrations Gateway

## Workflow

1. Read [references/mcp-plugins-skills-connectors.md](references/mcp-plugins-skills-connectors.md) before changing MCP aggregation, connector discovery, plugin lifecycle, runtime skills, or skill packs.
2. Read [references/gateway-platforms-webhooks.md](references/gateway-platforms-webhooks.md) before changing gateway daemon behavior, platform APIs, message delivery, retries, directories, webhooks, Home Assistant, Feishu/Lark, or Discord.
3. Keep integration capabilities behind Agent HTTP APIs or `agent_*` facades.
4. Preserve control-token gates for management and mutation.
5. Treat external delivery and platform writes as side-effectful; use ActionIntent or explicit gated routes where existing behavior requires it.

## Key Files

- `packages/agent/src/aiask_agent/mcp_client.py`
- `packages/agent/src/aiask_agent/acp.py`
- `packages/agent/src/aiask_agent/plugin_runtime.py`
- `packages/agent/src/aiask_agent/skill_packs.py`
- `packages/agent/src/aiask_agent/native_skill_store.py`
- `packages/agent/src/aiask_agent/connectors.py`
- `packages/agent/src/aiask_agent/connector_manager.py`
- `packages/agent/src/aiask_agent/routes/plugins_skills.py`
- `packages/agent/src/aiask_agent/routes/connectors.py`
- `packages/agent/src/aiask_agent/routes/gateway.py`
- `packages/agent/src/aiask_agent/gateway_daemon.py`
- `packages/agent/src/aiask_agent/gateway_route_factories.py`
- `packages/agent/src/aiask_agent/platform_apis.py`
- `packages/agent/src/aiask_agent/webhooks.py`
- `packages/agent/src/aiask_agent/homeassistant.py`
