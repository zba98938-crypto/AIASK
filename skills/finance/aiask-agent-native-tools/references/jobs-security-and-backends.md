# Jobs, Security, And Backends

## Jobs And Cron

Agent exposes job routes and native job tools:

- `/v1/jobs`
- `/v1/jobs/{job_id}/runs`
- `/v1/jobs/{job_id}`
- `/v1/jobs/{job_id}/run`
- `agent_job_create`, `agent_job_list`, `agent_job_run`, `agent_cronjob`

Jobs may execute prompts/tools on schedules and should preserve toolset, user, control, and audit semantics.

## Terminal Backends And Process Registry

Terminal/process functionality covers local and optional remote/container backends through `terminal_backends.py` and `process_registry.py`.

Do not assume optional dependencies such as Docker, SSH, Modal, or Daytona are installed. Surface unavailable backend state explicitly.

## Security

Security scanning and advisory data live under `security.py` and `data/known_advisories.json`.

Rules:

- Do not scan or print secrets by default.
- Keep redaction and path handling explicit.
- Treat dependency/advisory output as diagnostic data, not automatic remediation.

## Tests

- `packages/agent/tests/test_db_retention_tool.py`
- `packages/agent/tests/test_terminal_cross_platform.py`
- `packages/agent/tests/test_native_full_parity.py`
- Desktop settings/security tests where UI exposes these controls.
