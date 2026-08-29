# Manager Side Effects And Safety

## Side-Effect Levels

`manager_protocol.py` normalizes manager payloads and side-effect metadata. Preserve:

- Read-only actions for status, list, detail, readiness, reports, and health.
- Stateful actions for persisted strategy/factory/data/portfolio/watchlist/user/paper changes.
- Trade-risk actions for live orders, live cancels, broker/account writes, or execution paths that can affect real accounts.

## Agent Boundary

Agent should wrap safe manager behavior as `agent_*` tools and route stateful behavior through ActionIntent or explicitly gated HTTP routes. Do not add `agent_strategy_manager`, `agent_quant_manager`, or similar raw-manager facades.

## Live Trading Guardrails

Live order/cancel paths need:

- Explicit token or broker confirmation.
- Rejection envelope when token is missing or invalid.
- `side_effect.level = trade_risk` or equivalent metadata.
- Audit fields sufficient to explain action, target, parameters, and decision.

Never treat paper/sandbox tests as live trading acceptance.
