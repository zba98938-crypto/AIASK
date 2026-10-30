# AIASK

A-share quantitative research backend, delivered as **MCP servers + skills**.
There is no frontend and no user-facing application in this tree: clients talk to
the Agent HTTP API or connect to the MCP servers over stdio.

## What is here

| Package | What it is |
|---|---|
| `packages/akshare-mcp/` | The main MCP server: **179** registered tools in the default `full` profile, **144** in `tool-only` (which the reference manifest uses), plus 3 resources and 7 prompts. Market/finance data, valuation, research, quant and strategy services, data sync, freshness/readiness checks. |
| `packages/strategy-factory/` | Strategy / factor / incubation factory domain package, plus its own MCP server (`strategy_factory.mcp_server`) and the factory control plane (`strategy_factory.mcp_control`). |
| `packages/aiask-quant-core/` | Quant primitives: backtest engines, factor calculators, validation (walk-forward, purged k-fold, bootstrap), risk model, SQLite storage. |
| `packages/agent/` | FastAPI Agent runtime and the `agent_*` tool facades. |
| `packages/finance-mcp-servers/` | Tongdaxin, Tonghuashun, Eastmoney and QMT MCP servers. |
| `packages/data-mcp/` | Neutral data-provider facade. |
| `packages/aiask-skills/` | Packaged skill bundle. |
| `packages/reference-host/` | Minimal reference `HostAdapter` implementation. |
| `skills/finance/` | 19 in-repo skills. |
| `scripts/` | Data sync, backfill, readiness, factory runners, monitoring and ops. Diagnostic checks are named `check_*.py`. |

The operational environment is Python 3.13. Package manifests currently target
`>=3.12,<3.14`, except `finance-mcp-servers`, whose manifest remains compatible
with Python `>=3.10`.

## Setup

Development uses one conda environment, `aiask` (Python 3.13). See `CLAUDE.md`
for the rule and the reason. Install the local packages editable in dependency
order:

```bash
/opt/miniconda3/envs/aiask/bin/python -m pip install \
  -e packages/aiask-quant-core \
  -e packages/strategy-factory \
  -e packages/akshare-mcp \
  -e packages/data-mcp \
  -e packages/finance-mcp-servers \
  -e packages/aiask-skills \
  -e packages/reference-host \
  -e packages/agent
```

For a standalone deployment, install in the order above (`aiask-quant-core`
first, it is the base dependency) into any Python 3.12/3.13 environment — a
conda env is the supported form — and optionally set:

```bash
export AIASK_CONDA_ENV=aiask                       # conda env name to look for
# export AIASK_VENV_PYTHON=/path/to/python         # or name it outright
export AIASK_PROJECT_ROOT="$PWD"
export AIASK_SQLITE_PATH="$HOME/.aiask/data/db/akshare_mcp.sqlite3"
export AKSHARE_MCP_SQLITE_PATH="$HOME/.aiask/data/db/akshare_mcp.sqlite3"
```

`scripts/run_mcp_stdio.py` resolves the AIASK interpreter in this order:
`AIASK_VENV_PYTHON`, then an isolated venv at `$AIASK_RUNTIME_HOME/venv`, then
the conda env named by `AIASK_CONDA_ENV`. If none is found it fails closed
instead of running under an arbitrary Python. The `.claude/mcp-servers-recommended.json`
manifest launches every shipped server this way.

All configuration variables are documented in `env.example`. Do not put real
secrets in this repository.

## Data sources

What the project reads, from where, and what was measured as reachable on this
host, is in **`docs/DATA_SOURCES.md`**. Read it before adding a provider: this
machine's proxy answers `datacenter-web.eastmoney.com` while blocking
`push2.eastmoney.com`, which is the opposite of the usual assumption, and Tushare
is a declared dependency with no token configured.

## Tests

Per-package, from that package's directory (each `pytest.ini` / `pyproject.toml`
uses relative `pythonpath`):

```bash
cd packages/akshare-mcp && /opt/miniconda3/envs/aiask/bin/python -m pytest -q
```

Or the whole repository from the root — `pytest.ini` lists every package's suite:

```bash
/opt/miniconda3/envs/aiask/bin/python -m pytest -q
```

CI (`.github/workflows/tests.yml`) gates on every package. The exact local test
count is intentionally not recorded here because the suites evolve; run the
root command above for the current result.

## Safety boundaries

- External clients use the Agent HTTP API or MCP; nothing else is a public surface.
- Model-visible Agent tools stay behind `agent_*` facades.
- AKShare managers are internal/provider-facing. Do not expose raw manager names
  as model tools.
- `akshare_mcp.services` may import `strategy_factory` only through
  `strategy_factory.api` and `strategy_factory.runtime`. A test enforces this
  (`packages/akshare-mcp/tests/test_services_runtime_public_api_only.py`).
- Live trading is off by default. Tonghuashun/QMT order paths stay token- and
  confirmation-guarded. Strategy, factor, incubation, execution, gateway
  delivery, plugin mutation and file/terminal/browser writes require explicit
  guardrails.
- Formula execution goes through `pandas_formula_guard` / the factor compiler AST
  whitelist; `evaluate_compiled_factor` refuses candidates marked `valid=False`
  at execution time rather than trusting call sites.

## Known state, honestly

- **Runtime state (measured 2026-10-29):** no supervisor or sidecar process was
  alive during this check. Historical status files and old architecture notes
  are not evidence that a service is currently running. Use the pid and fresh
  heartbeat, not the last reported `running` label.
- **Daily bars are behind (read-only measurement 2026-10-29):**
  `kline_1d` has 1,739,152 rows and its latest date is 2026-10-23. Coverage is
  53 codes on each of 10-21..10-23, 57 on 10-19..10-20, versus 5,570 on
  10-16. This is not full-market coverage and the table is not yet registered
  as a complete `kline_1d` sync. Reproduce with
  `sqlite3.connect('file:<AIASK_RUNTIME_HOME>/data/db/akshare_mcp.sqlite3?mode=ro', uri=True)`
  and `SELECT substr(time,1,10), COUNT(*) FROM kline_1d GROUP BY 1 ORDER BY 1 DESC LIMIT 10`.
- **Quotes are not execution-grade:** read-only measurement found `stock_quotes`
  latest at 2026-10-18 (171 rows). A DB snapshot older than the execution
  session is not a valid fill, stop-loss, or risk-reduction price. This machine
  has no verified real-time quote source; stale-quote refusal is expected, not
  a service failure.
- **Paper evidence needs repair before it is a current performance baseline.**
  The database contains paper fills dated 10-19..10-21 outside their session
  bar ranges, plus weekend-dated evidence. P7/P8 repair and sync have not been
  authorized/applied, so historical aggregate scores should not be interpreted
  as clean evidence.
- **Admission is observation, not rejection:** after commit `4534777`, gate-A
  failures route candidates to the zero-capital observation lane. The previous
  statement that the gate was rejecting them was stale.
- **Promotion review table:** the authoritative table is
  `strategy_promotion_reviews` (76 rows at the 2026-10-20 measurement: 24
  `rejected`, 52 `watch`), not `promotion_reviews`. Reproduce with
  `SELECT status, COUNT(*) FROM strategy_promotion_reviews GROUP BY status`.
- **No CI run is claimed:** this checkout has no git remote. GitHub Actions have
  not run for this local branch; `scripts/ci_local.sh` is the intended local
  pre-commit gate, not a substitute for a hosted CI result.
- **Industry comes from East money's F10 report, not Sina's board list.** Sina
  classified 48% of the market and counted `次新股` — a listing-age bucket — as
  an industry. `stocks.industry` is East money `EM2016` level 1 (28 values) and
  `stocks.sector` is level 2 (104 values), both at 100% coverage, so the two
  columns are one taxonomy at two depths rather than two taxonomies that cannot
  be compared. `scripts/sync_market_universe.py --with-industry` refreshes them.
- **`stocks` still holds 5 index rows** (`sh000001`, `sh000300`, `sz399001`,
  `sz399006`, `sz399300`) alongside the companies. They are benchmarks, not
  companies, and their industry/sector are left NULL so a `GROUP BY industry`
  over this table returns companies only.
- **Historical task-run repair (measured 2026-10-20):** 15 `signal_tracker`
  task runs had been reclaimed. This describes a past repair, not a claim that
  the sidecar is alive now. Their processes
  died before the finaliser ran, and the finaliser had been calling the INSERT
  helper with an `id`, so it could never close its own row. The finaliser now
  updates, cycles reclaim stale rows as `timed_out`, and the historical 15 have
  been reclaimed by `scripts/reclaim_stale_task_runs.py`; this historical count
  was not re-measured on 2026-10-29. Paper-position counts and holding-period
  closure configuration in older reports likewise are historical and are not
  treated as a valid post-audit performance baseline.
- Single-machine, single-user. See the user-system ruling in `CLAUDE.md` before
  touching anything `user_id`-shaped.
- `docs/` contains historical reports whose "100% complete" claims predate this
  tree. `docs/CURRENT_PRODUCT_TRUTH.md` is the current-state pointer; prefer the
  tree over both.
- `docs/archive/frontend/` is historical only. It is not executable and not a
  readiness claim.
