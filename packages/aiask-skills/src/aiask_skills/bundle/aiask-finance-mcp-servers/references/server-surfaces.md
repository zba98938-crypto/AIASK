# Finance MCP Server Surfaces

## Package

Primary manifest: `packages/finance-mcp-servers/pyproject.toml`.

Scripts:

- `aiask-finance-tdx`
- `aiask-finance-ths`
- `aiask-finance-em`
- `aiask-finance-qmt`

Optional groups cover Tongdaxin, Tonghuashun, Eastmoney, QMT, all, and dev dependencies.

## Servers

Tongdaxin:

- Market data, realtime quote, kline history, minute/tick/block/finance/snapshot-style data depending on implementation and optional dependency availability.

Eastmoney:

- Realtime quote, kline history, funds, NAV, bonds, futures, news flow.

Tonghuashun:

- Position/order/deal queries.
- Live place/cancel order guarded by broker token.

QMT:

- Account/position/order queries.
- Live place/cancel order guarded by broker token and configured account settings.

## Integration Boundary

These are separate MCP servers. Agent should aggregate them through MCP configuration and Agent policy, not import broker server internals directly.

Read-only market/account functions are not proof that order placement is safe or enabled.
