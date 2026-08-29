# Finance MCP Trade-Risk Guard

## Guard Source

Primary file:

- `packages/finance-mcp-servers/src/aiask_finance_mcp/_shared/trade_guard.py`

Important concepts:

- `TradeGuardError`
- `require_broker_token`
- `trade_risk_envelope`

## Guard Policy

Every live place/cancel order tool must require a per-call `broker_token`. Missing or mismatched token rejects the operation and returns a trade-risk envelope.

Never cache a token so future calls can skip explicit confirmation.

## Env Var Names

Document names only, not values:

- `AIASK_FINANCE_THS_BROKER_TOKEN`
- `AIASK_FINANCE_QMT_BROKER_TOKEN`

## Guarded Tools

Tonghuashun:

- `ths_place_order`
- `ths_cancel_order`

QMT:

- `qmt_place_order`
- `qmt_cancel_order`

Preserve validation for direction, code/order id, price, amount/volume, and token.

## Safety Rules

- Keep `side_effect.level = trade_risk` or equivalent metadata.
- Keep `explicit_token_required = True`.
- Do not treat paper/sandbox/account read tests as live order acceptance.
- Do not route live order calls around MCP guardrails or Agent ActionIntent policy.
