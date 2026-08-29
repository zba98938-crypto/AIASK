---
name: aiask-run-strategy-factory
description: Run and verify one AIASK Strategy Factory cycle through the confirmed factory.run ActionIntent and persistent FactoryRunContract ledger. Use when the user asks to start, retry, or inspect a Strategy Factory run; do not use for daemon management or direct script execution.
version: 1.0.0
protocol_version: "1.0"
capabilities:
  - factory.run.strategy_factory
  - factory.status.read
required_mcp:
  - aiask-factories
side_effect: mutating
---

# Run Strategy Factory

1. Call `agent_factory_readiness` with `factory=strategy_factory`. Stop on blockers; report sidecar warnings separately.
2. Build a stable idempotency key from the requested business operation, such as `strategy_factory:<trading-date>:<mode>`. Reuse it for retries.
3. Call `agent_action_intent_create` with action `factory.run` and params containing `factory=strategy_factory`, the idempotency key, `mode=once`, and an optional nested `params` object.
4. Present the intent and wait for explicit user confirmation. Never call a runner script, raw MCP mutation tool, or dispatcher directly.
5. After confirmation, poll `agent_factory_runs` with the returned `run_id`. Use `agent_factory_runs` with `factory=strategy_factory` to recover the ID after a client restart.
6. Report the terminal state, raw status, blockers, quality flags, evidence references, and attempt count. Treat `partial` and `blocked` as completed but not successful.

The read-only `aiask-factories` MCP may be used for status and Resources. It cannot execute a run.
