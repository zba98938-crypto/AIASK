# Gateway Platforms And Webhooks

## Gateway APIs

Agent gateway routes include:

- `/v1/gateway/status`
- `/v1/gateway/daemon/status`
- `/v1/gateway/platforms`
- `/v1/gateway/platforms/{platform}/start`
- `/v1/gateway/platforms/{platform}/stop`
- `/v1/gateway/platforms/{platform}/health`
- `/v1/gateway/messages`
- `/v1/gateway/messages/{message_id}/retry`
- `/v1/gateway/directory`
- `/v1/gateway/directory/refresh`
- `/v1/gateway/send`
- `/v1/gateway/direct-deliver`
- `/v1/gateway/webhooks/{platform}`

Gateway model tools include status/platform/history/directory/send/direct-deliver variants. Message send/direct-deliver should remain controlled and auditable.

## Platforms

Integration code covers Gateway delivery and platform APIs such as Home Assistant, Feishu/Lark, Discord, and other configured gateway platforms. Platform credentials are environment-driven; never print or document actual secret values.

## Webhooks

Webhook APIs include list/create/delete/trigger. Triggering can become an external side effect and should preserve ActionIntent or explicit gated behavior where wired.

## Tests

- `packages/agent/tests/test_gateway_daemon.py`
- `packages/agent/tests/test_gateway_daemon_phase2.py`
- `packages/agent/tests/test_gateway_daemon_phase4.py`
- `packages/agent/tests/test_hermes_native_live_adapters.py`
- `desktop/src/services/aiaskApi.test.ts`
- `desktop/e2e/aiask-v1.spec.ts`
