# Incubation Runner Phases

## Entrypoints

Inspect current launchers before invoking. Historical root runners may be deleted or moved; current worktree includes factory/ops scripts.

Important files:

- `services/incubation_factory/runner.py`
- `services/incubation_factory/forward_verifier.py`
- `services/incubation_factory/hit_rate_reporter.py`
- `services/incubation_factory/hit_rate_matrix.py`
- `services/incubation.py`
- `services/incubation_pipeline.py`

## Phase Concerns

Current incubation work includes:

- Intake of strategies into observation/incubation lanes.
- Diagnostic observation and paper observation handling.
- Signal generation and forward verification.
- Metrics recording and hit-rate matrix/report generation.
- Stage transitions and promotion gate decisions.
- Feedback writing for Strategy Factory budget/lifecycle loops.
- Alert/health/heartbeat behavior.

Keep phase-level errors visible so operators can distinguish partial degradation from total failure.

## Dry Run

Dry-run behavior matters. Preserve skipped write paths and clear status reporting when dry-run is active.

Prefer dry-run or status paths for validation unless the task explicitly requires persisted events or feedback.
