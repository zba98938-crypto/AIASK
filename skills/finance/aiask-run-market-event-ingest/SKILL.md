---
name: aiask-run-market-event-ingest
description: Run and verify one AIASK Market Event Ingest cycle through the confirmed factory.run ActionIntent and persistent FactoryRunContract ledger. Use for explicit bounded event-ingest requests, offline/network modes, retries, and provenance result inspection.
version: 1.0.0
protocol_version: "1.0"
capabilities:
  - factory.run.market_event_ingest
  - factory.status.read
required_mcp:
  - aiask-factories
side_effect: mutating
---

# Run Market Event Ingest

1. Call `agent_factory_readiness` with `factory=market_event_ingest`.
2. Build a stable key such as `market_event_ingest:<trading-date>:<scope>`.
3. Call `agent_action_intent_create` with action `factory.run`, `factory=market_event_ingest`, `mode=once`, the idempotency key, and nested params limited to `allow_network`, `codes`, and `lookback_days`.
4. State whether network access is enabled and wait for explicit confirmation. Do not call the ingest script or a raw AKShare write tool.
5. Poll `agent_factory_runs` with `run_id`; use `agent_factory_runs` with `factory=market_event_ingest` for restart recovery.
6. Report fetched/saved/normalized counts, source/provenance quality, bridge outcome, raw status, quality flags, and blockers. Preserve degraded or partial source coverage in the result.
