---
name: aiask-run-four-factory-cycle
description: Orchestrate one confirmed AIASK cycle across Market Event Ingest, Factor Mining, Strategy Factory, and Incubation using persistent FactoryRunContract records. Use when the user requests all four factories or an end-to-end factory cycle.
version: 1.0.0
protocol_version: "1.0"
capabilities:
  - factory.run.cycle
  - factory.status.read
required_mcp:
  - aiask-factories
side_effect: mutating
---

# Run Four Factory Cycle

Use this order unless the user provides a justified dependency override:

1. `market_event_ingest`
2. `factor_mining`
3. `strategy_factory`
4. `incubation`

## Workflow

1. Call `agent_factory_readiness` without a factory filter. Missing SignalTracker is an explicit sidecar warning, not a fifth factory and not automatically a hard blocker.
2. Choose one cycle ID and derive four stable idempotency keys: `<factory>:<cycle-id>`.
3. Create four separate `factory.run` ActionIntents. Show all four scopes and obtain explicit confirmation for each stateful run; never collapse confirmation into a raw script or MCP call.
4. Submit the next intent only after the preceding run reaches a terminal state. Stop downstream submission on `failed` or `blocked`. On `partial`, summarize the impact and ask before continuing when the missing evidence affects downstream validity.
5. Poll with `agent_factory_runs` and `run_id`. On restart, reconstruct state from `agent_factory_runs` using factory filters and the idempotency keys.
6. Produce a final matrix with factory, run ID, status, raw status, attempts, blockers, and quality flags. Include the SignalTracker sidecar status separately.

Duplicate submissions with the same key must resolve to the original run. Never invent a new key merely to bypass a failed or active run.
