# AIASK integration — current product truth (2026-07-15)

This file is the **current-state** pointer for the Hermes + AIASK finance
upgrade. Prefer it over June historical “100% complete” reports when those
conflict with the tree.

## Hermes finance edition (2026-07-23)

Edge productization work targets a Hermes “AIASK finance edition” skin without
core tool expansion. Phase 1 gates, non-goals, and operator quickstart live in:

- `docs/AIASK_HERMES_FINANCE_EDITION.md`

Architecture remains edge-only (`runtime-boundary.json`). Live trading stays
off by default. Desktop finance is the focused 5-view workbench embedded under
Hermes Desktop `/finance` when present — not a claim of full V1 workbench.

### Phase 1 implementation note (2026-07-23, later)

Landed in the edge plugin + Desktop shell (not Hermes core tools):

- `hermes aiask setup --finance-profile aiask-finance` — create profile, install plugin/skills, write read-mostly MCP defaults (`aiask-akshare` enabled; broker MCPs disabled). **Use `--finance-profile`**, not Hermes top-level `--profile` (that flag switches `HERMES_HOME` before plugin CLI discovery).
- `hermes aiask agent start|stop|status` — isolated venv Agent HTTP process (pid under `AIASK_RUNTIME_HOME`); venv python path is not symlink-collapsed so `aiask_agent` stays importable.
- `hermes aiask factory status|once|evidence` — free_data + strategy_factory once + evidence path/counts
- Dashboard plugin API: `GET /factories/status`, `GET /evidence/summary`, `POST /factories/once` (token), `POST /setup` (token unless dry_run)
- Hermes Desktop route `/finance` embeds focused workbench via iframe + setup panel (default Vite `127.0.0.1:1420`)

### Phase 1 gate evidence (2026-07-23)

| Gate | Status | Notes |
|------|--------|-------|
| G1 Setup | **pass (code; host-dependent)** | Setup now fails closed when MCP manifests or readiness are unavailable; a successful response is not a substitute for a host run |
| G2 Runtime | **pass (code; host-dependent)** | Agent start requires a healthy HTTP response and fails/cleans up on timeout; host health still requires the isolated venv and installed packages |
| G3 Desktop | **pass (code)** | `/finance` route + FinanceView embed present; live Vite UI optional |
| G4 Dashboard | **pass (code)** | factories/evidence/setup API + dist UI present |
| G5 Dialogue | **pass (defaults)** | Profile MCP: akshare enabled, brokers disabled; 14 finance skills; no core finance tools |
| G6 Factories | **pass (code; host-dependent)** | Factory commands require the canonical isolated venv and force research subprocesses read-only; a local smoke is not 24h ops evidence |
| G7 Boundary | **pass** | Core import scan clean; footprint test allows Desktop finance skin only |

Still **not** claimed: live trading product, full V1 workbench, 24h four-factory always-on, multi-platform live soak.

### Phase 2 research ops (2026-07-23)

Research/paper ops on the same edge plugin (not Hermes core tools):

- `hermes aiask readiness --triage` — classifies expected optional secrets vs actionable warnings; `research_ready` when critical failures are clear (never auto-claims production/live)
- `hermes aiask factory cycle [--only …] [--skip-free-data]` — multi-factory once cycle (strategy / factor_mining / incubation / signal_tracker); not 24h supervisor
- `hermes aiask factory schedule-template` — disabled cron samples only
- `hermes aiask research-board` — SQLite lite metrics + heuristic gates (not formal promotion)
- Dashboard: `GET /readiness/triage`, `GET /research-board`, `GET /factories/schedule-template`, `POST /factories/cycle` (control-token)
- Desktop `/finance` setup lists Phase 2 CLI hints

| Gate | Status | Notes |
|------|--------|-------|
| P2-G1 Triage | **pass** | CLI + unit + dashboard route |
| P2-G2 Cycle | **pass (code+select)** | `--only` selection unit-tested; full cycle optional long-run |
| P2-G3 Schedule | **pass** | `enabled=false` samples |
| P2-G4 Board | **pass** | lite board with missing-SQLite ok path |
| P2-G5 Surfaces | **pass (code)** | dist panels + Desktop setup hints |
| P2-G6 Boundary | **pass** | same edge boundary; no live/24h claims |

Still **not** claimed: live trading product, full V1 workbench, formal track promotion automation, 24h four-factory always-on.

Research readiness without Tushare/broker credentials is warning-clean when all
required runtime checks pass. Credential presence is informational, and a
passing check is not counted as a warning merely because its failure severity
would be `warning`.

### Phase 3 residual productization (2026-07-23)

Edge plugin surfaces for residuals **7–9** (still default-off / honesty-first):

- `hermes aiask live status|plan` — live arming inventory + opt-in checklist; never mutates env; place/cancel still need broker_token (`trade_risk`)
- `hermes aiask workbench` — surface map only; there is no frontend in this tree
- `hermes aiask factory ops-status|ops-start|ops-stop` — productized supervisor around `run_three_factories.py` (confirm required; not auto-started by setup)
- Dashboard: `GET /live/status`, `GET /live/plan`, `GET /workbench`, `GET/POST /factories/ops/*` (mutations token-gated)
- Desktop `/finance` setup lists Phase 3 CLI

| Gate | Status | Notes |
|------|--------|-------|
| P3-G1 Live status | **pass** | default `live_armed=false` |
| P3-G2 Live plan | **pass** | mutates=false checklist |
| P3-G3 Workbench | **n/a (backend-only)** | frontend removed; `/v1/workbench/summary` serves API clients |
| P3-G4 Ops status | **pass** | not auto-started |
| P3-G5 Ops start/stop | **pass (code)** | confirm + token; dry-run unit-tested |
| P3-G6 Surfaces | **pass (code)** | dist + Desktop hints |
| P3-G7 Boundary | **pass** | edge/MCP only |

Still **not** claimed: broker production certification, full historical V1 frontend, multi-day 24h soak proof, formal promotion automation.

### Factory command semantics

| Surface | Execution model | Success proves | Does not prove |
|---|---|---|---|
| `factory once` | free-data/strategy one-shot | selected one-shot subprocesses exited successfully | other factories, scheduling, or continuous health |
| `factory cycle` | selected research factories run once in sequence | that single research cycle completed | supervisor health or 24h operation |
| `factory ops-start` | starts the four-runtime process supervisor | supervisor survived startup and owns a managed process group | multi-day soak, evidence quality, or formal promotion |
| `run_strategy_factory_quality_session.py` | explicitly bounded quality session | only the emitted session evidence and duration | general production certification |

The historical filename `run_three_factories.py` supervises four runtimes. It
is retained as a compatibility entrypoint; product-facing commands and status
use the term **four-runtime supervisor**. `factory cycle` and `ops-start` must
never be described as the 24h quality-session path.

## No frontend in this tree

This tree is backend-only. The AIASK focused workbench (`desktop/`) was removed
when the tree was extracted, and with it the `/v1/desktop/*` HTTP facade that
existed to serve it.

| Surface | Status |
|---|---|
| **AIASK focused workbench** | **Removed.** No `desktop/` directory. Clients use the Agent HTTP/MCP interfaces directly. |
| **`/v1/desktop/*` facade** | **Removed.** Answers a structured 404 whose `meta.replacement` names the neutral path. Capabilities live under `/v1/settings`, `/v1/data`, `/v1/users`, `/v1/finance`, `/v1/research`, `/v1/workbench`, `/v1/capabilities`. |
| **Desktop asset proxy** | **Removed with no replacement.** `/v1/desktop/files` and the strategies / stock-pools routes proxied a separate service (`AIASK_DESKTOP_API_BASE_URL`, `127.0.0.1:8001`) that is not part of a backend-only tree. |
| **Legacy V1 frontend docs** | Archived under `docs/archive/frontend/` — historical only, not executable, and not a readiness claim. |

Readiness no longer treats a missing frontend as a product blocker.

## Authoritative gates

1. `python integrations/aiask/scripts/aiask_readiness.py --venv-python "$AIASK_VENV_PYTHON" --json`
2. `AIASK_FINANCE_AGENT_COMPLETION_AUDIT.md` (repo root)
3. `integrations/aiask/docs/architecture/runtime-boundary.json`

## Verified local baseline (2026-07-15)

- AIASK venv: `${AIASK_RUNTIME_HOME:-~/.aiask}/venv` (CPython 3.12); all bootstrap, readiness, MCP, and factory paths use this same default
- Readiness: `summary.ready=true`, `product_ready=true`
- Agent HTTP: `127.0.0.1:8765` health + finance_safe tool catalog (115 tools)
- Live trading: default **off**
- Factory evidence tables: empty until market data sync + factory cycles run
- Fragment debt consumers: 0 (budget target met; historical June reports remain non-authoritative)

## Stale claims to ignore

- “AIASK V1 frontend P0–P3 100%” for **this** Hermes tree
- “desktop-api package complete” (`packages/desktop-api` is missing here)
- “Hermes Desktop hosts full AIASK workbench”
- “All finance platforms fully integrated and verified”


## Fragment debt

- consumer modules calling exec_fragments/exec_block: **0**
- `*_parts` directories: **deleted**
- `_fragment_loader.py`: **deleted**
- package tests after cleanup: strategy-factory/quant-core/finance-mcp green (755+ passed in combined run)


## Hermes engineering note (2026-07-18, later same day)

- AIASK historical docs under `integrations/aiask/docs/**` now carry Hermes-copy disclaimers; scan gate: `tests/gateway/test_aiask_docs_disclaimer_scan.py`.
- Three-surface unit parity advanced: approval choices / `approval.respond` / session method tokens locked by `tests/gateway/test_surface_approval_session_parity.py`; Windows dashboard PTY unavailable messaging locked by `tests/hermes_cli/test_windows_dashboard_pty_contract.py`.
- This still does **not** close Relay Class-1 live validation, live multi-platform E2E, full AIASK workbench claims, live trading, or host-level 24h four-factory ops.

## Hermes engineering note (2026-07-18, tui session lifecycle extract)

- `tui_gateway/session_lifecycle_ops.py` extracted from `tui_gateway/server.py` with re-export + gates.
- Surface parity gate now includes behavioral `approval.respond` resolve-path coverage in addition to source contracts.
- This still does **not** close Relay Class-1 live validation, live multi-platform E2E, full AIASK workbench claims, live trading, or host-level 24h four-factory ops.

## Hermes engineering note (2026-07-18, tui pet/billing extract)

- `tui_gateway/pet_ops.py` and `tui_gateway/billing_ops.py` extracted from `tui_gateway/server.py` with re-export + deferred method registration + gates.
- `tui_gateway/server.py` now ~12225 LOC (down from ~13405 after lifecycle extract).
- This still does **not** close Relay Class-1 live validation, live multi-platform E2E, full AIASK workbench claims, live trading, or host-level 24h four-factory ops.

## Hermes engineering note (2026-07-18, surface interrupt parity)

- Surface parity gate now also covers behavioral `session.interrupt` deny-all approval clearing and Desktop/TUI shared `session.interrupt` RPC token.
- This still does **not** close Relay Class-1 live validation, live multi-platform E2E, full AIASK workbench claims, live trading, or host-level 24h four-factory ops.

## Hermes engineering note (2026-07-18, tui handoff/delegation extract)

- `tui_gateway/handoff_ops.py` and `tui_gateway/delegation_ops.py` extracted from `tui_gateway/server.py` with re-export + deferred method registration + gates.
- `tui_gateway/server.py` now ~11810 LOC (down from ~12225 after pet/billing extract).
- This still does **not** close Relay Class-1 live validation, live multi-platform E2E, full AIASK workbench claims, live trading, or host-level 24h four-factory ops.

## Hermes engineering note (2026-07-18, tui session-control extract)

- `tui_gateway/session_control_ops.py` extracted from `tui_gateway/server.py` with re-export + deferred method registration + gates.
- Surface-parity static method checks now accept deferred `register_*` extraction modules.
- `tui_gateway/server.py` now ~11443 LOC (down from ~11810 after handoff/delegation extract).
- This still does **not** close Relay Class-1 live validation, live multi-platform E2E, full AIASK workbench claims, live trading, or host-level 24h four-factory ops.

## Hermes engineering note (2026-07-18, tui respond extract)

- `tui_gateway/respond_ops.py` extracted from `tui_gateway/server.py` with re-export + deferred method registration + gates.
- `tui_gateway/server.py` now ~11401 LOC.
- This still does **not** close Relay Class-1 live validation, live multi-platform E2E, full AIASK workbench claims, live trading, or host-level 24h four-factory ops.

## Hermes engineering note (2026-07-18, tui session-crud extract)

- `tui_gateway/session_crud_ops.py` extracted from `tui_gateway/server.py` with re-export + deferred method registration + gates.
- Surface-parity ops list now includes `session_crud_ops` so `session.list` / `session.resume` deferred registration is accepted.
- `tui_gateway/server.py` now ~10275 LOC (down from ~11401 after respond extract).
- This still does **not** close Relay Class-1 live validation, live multi-platform E2E, full AIASK workbench claims, live trading, or host-level 24h four-factory ops.

## Hermes engineering note (2026-07-18, tui prompt/media extract)

- `tui_gateway/prompt_ops.py` extracted from `tui_gateway/server.py` with re-export + deferred method registration + gates.
- Includes `prompt.submit`, attachment RPCs, background/preview, and `_run_prompt_submit` notification helpers.
- Surface-parity ops list now includes `prompt_ops`.
- `tui_gateway/server.py` now ~8719 LOC (down from ~10275 after session-crud extract).
- This still does **not** close Relay Class-1 live validation, live multi-platform E2E, full AIASK workbench claims, live trading, or host-level 24h four-factory ops.

## Hermes engineering note (2026-07-18, tui config/setup/projects extract)

- `tui_gateway/config_ops.py` extracted from `tui_gateway/server.py` with re-export + deferred method registration + dual-signature projects wrappers + gates.
- Includes `config.set/get`, `setup.status/runtime_check`, and full `projects.*` surface.
- Surface-parity ops list now includes `config_ops`.
- `tui_gateway/server.py` now ~7568 LOC (down from ~8719 after prompt extract).
- This still does **not** close Relay Class-1 live validation, live multi-platform E2E, full AIASK workbench claims, live trading, or host-level 24h four-factory ops.

## Hermes engineering note (2026-07-18, tui residual ops extract + import repair)

- Residual TUI method surface extracted to `tools_ops` / `complete_ops` / `paste_ops` / `slash_ops` / `voice_ops` / `insights_ops` / `rollback_ops` / `surface_ops` with deferred registration.
- Fixed import fallout that made `import tui_gateway.server` fail (`complete_ops` missing `threading`/`time`, plus sibling missing stdlib imports).
- `tui_gateway/server.py` now ~4855 LOC; residual ops + surface-parity gates green.
- This still does **not** close Relay Class-1 live validation, live multi-platform E2E, full AIASK workbench claims, live trading, or host-level 24h four-factory ops.

## Hermes engineering note (2026-07-18, tui runtime model helper extract)

- Extracted model resolve/switch/persist + toolset enablement helpers into `tui_gateway/runtime_model_ops.py`.
- `tui_gateway/server.py` now ~2753 LOC after residual method-surface + runtime helper extraction.
- This still does **not** close Relay Class-1 live validation, live multi-platform E2E, full AIASK workbench claims, live trading, or host-level 24h four-factory ops.

## Hermes engineering note (2026-07-18, tui agent/session construction helper extract)

- Extracted `_make_agent` / `_start_agent_build` / `_init_session` and related construction helpers into `tui_gateway/agent_session_ops.py`.
- `tui_gateway/server.py` now ~2753 LOC.
- This still does **not** close Relay Class-1 live validation, live multi-platform E2E, full AIASK workbench claims, live trading, or host-level 24h four-factory ops.

## Hermes engineering note (2026-07-18, tui session-info/tool-progress helper extract)

- Extracted `_session_info` / tool progress callbacks / usage helpers into `tui_gateway/session_info_ops.py`.
- `tui_gateway/server.py` now ~2753 LOC.
- This still does **not** close Relay Class-1 live validation, live multi-platform E2E, full AIASK workbench claims, live trading, or host-level 24h four-factory ops.

## Engineering note (Hermes residual TUI helper extract, later 2026-07-18)

- Extracted residual `tui_gateway` session workspace helpers (cwd/db/git) into `session_workspace_ops.py` and history/compress/busy-queue helpers into `history_queue_ops.py`.
- `tui_gateway/server.py` now ~**1721** LOC with **128** registered methods; re-exports preserve call sites.
- Focused gates: `tests/tui_gateway/test_session_workspace_ops.py`, `tests/tui_gateway/test_history_queue_ops.py`, residual ops / surface parity / session-crud/prompt suites (**71 passed** in the verification set).
- This still does **not** close Relay Class-1 live validation, live multi-platform E2E, full AIASK workbench claims, live trading, or host-level 24h four-factory ops.

## Engineering note (Hermes residual TUI config/personality extract, later 2026-07-18)

- Extracted residual `tui_gateway` config/display helpers into `config_display_ops.py` and personality/checkpoint helpers into `personality_checkpoint_ops.py`.
- Closed residual import fallout (`_PENDING_INPUT_COMMANDS` re-export, slash/agent bare-name resolution, Path-typed review-summary hermes home fixture).
- `tui_gateway/server.py` now ~**1341** LOC with **128** registered methods; focused residual/surface suite **170 passed**.
- This still does **not** close Relay Class-1 live validation, live multi-platform E2E, full AIASK workbench claims, live trading, or host-level 24h four-factory ops.

## Engineering note (Hermes residual TUI slash-worker/child-run extract, later 2026-07-18)

- Extracted residual `tui_gateway` slash-worker runtime into `slash_worker_ops.py` and child-run registry into `child_run_ops.py`.
- Closed latent bare-name fallout in `session_info_ops` / `slash_ops`, re-exported `_fuzzy_cache`, and kept Windows subprocess monkeypatch compatibility.
- `tui_gateway/server.py` now ~**1200** LOC with **128** registered methods; focused residual/surface suite **200 passed**.
- This still does **not** close Relay Class-1 live validation, live multi-platform E2E, full AIASK workbench claims, live trading, or host-level 24h four-factory ops.

## Engineering note (Hermes residual TUI panic/plumbing extract, later 2026-07-18)

- Extracted residual `tui_gateway` panic/crash hooks into `panic_ops.py` and db/profile/emit/image helpers into `session_plumbing_ops.py`.
- Closed latent re-export fallout for `is_truthy_value`, hermes-home overrides, and `sanitize_replay_history` required by extracted ops modules.
- `tui_gateway/server.py` now ~**982** LOC with **128** registered methods; focused residual/surface suite green.
- This still does **not** close Relay Class-1 live validation, live multi-platform E2E, full AIASK workbench claims, live trading, or host-level 24h four-factory ops.

## Engineering note (Hermes gateway start_gateway extract, later 2026-07-18)

- Extracted residual `gateway/run.py` entry points `start_gateway` / `main` into `gateway/start_gateway_ops.py` with re-export and late-bound `gateway.run` attribute resolution for monkeypatch parity.
- Closed extract fallout: `_weakref` re-export for runner weakref stamp; `lifecycle_ops_mixin.Platform` free-name import.
- `gateway/run.py` now ~**765** LOC; focused startup suite green.
- This still does **not** close Relay Class-1 live validation, live multi-platform E2E, full AIASK workbench claims, live trading, or host-level 24h four-factory ops.

## Engineering note (Hermes gateway runner init/exit extract, later 2026-07-18)

- Extracted residual `GatewayRunner.__init__` body into `gateway/runner_init_ops.py` and exit helpers into `gateway/runner_exit_ops.GatewayRunnerExitMixin`.
- Combined with `start_gateway_ops`, `gateway/run.py` is now ~**444** LOC thin composition root.
- Focused startup/init suite green (**28 passed**).
- This still does **not** close Relay Class-1 live validation, live multi-platform E2E, full AIASK workbench claims, live trading, or host-level 24h four-factory ops.

## Engineering note (Hermes CLI terminal-input extract, later 2026-07-18)

- Extracted CLI terminal paste/CPR/mouse hygiene helpers into `hermes_cli/cli_terminal_input_ops.py` after prior worktree + file-drop extracts.
- Added re-export identity gates for `cli_worktree_ops` / `cli_file_drop_ops` / `cli_terminal_input_ops`.
- `cli.py` now ~**14858** LOC; focused CLI extract suite **337 passed**.
- This still does **not** close Relay Class-1 live validation, live multi-platform E2E, full AIASK workbench claims, live trading, or host-level 24h four-factory ops.
