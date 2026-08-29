# Incubation Pipeline, Hit Rate, And Feedback

## Pipeline Stages And Gates

Pipeline behavior covers stages such as candidate/observe/review/graduation/promoted/listed/blocked depending on current data. Promotion should consider:

- Execution audit gate.
- Risk hard gate.
- Governance status.
- Runtime control blocking.
- Observed days and trade days.
- Promote streak and forward evidence.
- Paper account status when available.

Do not shortcut promotion based only on a single metric or LLM narrative.

## Hit-Rate Matrix

Current hit-rate implementation includes matrix/reporting across families, stages, horizons, and forward verification coverage. Preserve enough structure for Desktop and Strategy Factory feedback to understand:

- Hit-rate confidence and lower-bound behavior.
- Family/stage health.
- Missing forward windows.
- Promotion-ready or evidence-debt signals.

Useful tests:

- `test_hit_rate_matrix_p3_1.py`
- `test_hit_rate_reporter_matrix_p3_1.py`
- `test_forward_horizons_p2_2.py`

## Observation And Execution Audit

Related tests cover:

- Diagnostic observation runner and intake.
- Paper observation intake.
- Execution audit snapshot/replay/acceptance.
- Strategy lifecycle shared runtime.
- Incubation surface stage resolution.

Keep diagnostic and paper paths clearly separate from live trading.

## Desktop/Agent

Agent tools:

- `agent_incubation_factory_status`
- `agent_strategy_domain_events`

Desktop panel:

- There is no direct first-class incubation product page in the current Desktop V1 navigation.
- The visible Desktop shell keeps incubation behind safer higher-level surfaces and backend/Agent control points.

Keep event payloads stable enough to render lifecycle state, hit-rate health, recent failures, feedback, and promotion activity.
