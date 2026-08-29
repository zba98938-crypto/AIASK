---
name: aiask-run-factor-mining
description: Run and verify one AIASK Factor Mining cycle through the confirmed factory.run ActionIntent and persistent FactoryRunContract ledger. Use for user-requested factor mining runs, bounded engine/candidate options, retries, and result inspection.
version: 1.0.0
protocol_version: "1.0"
capabilities:
  - factory.run.factor_mining
  - factory.status.read
required_mcp:
  - aiask-factories
side_effect: mutating
---

# Run Factor Mining

1. Call `agent_factory_readiness` with `factory=factor_mining`.
2. Create a stable idempotency key such as `factor_mining:<trading-date>:<scope>`.
3. Call `agent_action_intent_create` with action `factory.run`. Set `factory=factor_mining`, `mode=once`, the idempotency key, and nested params only from `candidate_count`, `evolution_generations`, `engines`, and `codes`.
4. Show the exact scope and wait for explicit confirmation. Do not call `factor_factory.run_once`, a raw MCP write tool, or the Python runner.
5. After confirmation, poll `agent_factory_runs` with `run_id`; recover via `agent_factory_runs` with `factory=factor_mining` if needed.
6. Report admitted/validated factor summary when present, plus status, raw status, quality flags, blockers, and attempt count. A `partial` run must not be represented as a clean factor admission.
