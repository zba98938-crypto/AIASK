---
name: aiask-run-incubation-factory
description: Run and verify one AIASK Incubation Factory cycle through the confirmed factory.run ActionIntent and persistent FactoryRunContract ledger. Use for explicit incubation runs, dry-run requests, retries, and stage-result inspection.
version: 1.0.0
protocol_version: "1.0"
capabilities:
  - factory.run.incubation
  - factory.status.read
required_mcp:
  - aiask-factories
side_effect: mutating
---

# Run Incubation Factory

1. Call `agent_factory_readiness` with `factory=incubation`.
2. Build a stable key such as `incubation:<trading-date>:<dry-or-write>`.
3. Call `agent_action_intent_create` with action `factory.run`, `factory=incubation`, `mode=once`, the idempotency key, and nested `params.dry_run` when requested.
4. Explain whether the cycle is dry-run or persistent and wait for explicit confirmation. Never invoke the incubation runner or legacy factory action directly.
5. Poll `agent_factory_runs` with `run_id` after confirmation and recover with `agent_factory_runs` plus `factory=incubation` after restart.
6. Report pipeline stages, settlement/audit summaries when present, and the contract status. Keep diagnostic, paper observation, and live readiness distinct. `partial` is not promotion success.
