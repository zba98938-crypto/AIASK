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

- **Nothing is scheduled.** No process, no cron entry, no supervisor. Each
  factory has a `--once` mode and was last run as one manual four-factory cycle
  on 2026-10-15.
- **The universe is the whole market, and no longer a curated residue.** 5,582
  rows in `stocks`, 5,577 listed, 1,733,871 daily bars covering 100% of them,
  latest bar 2026-10-15. `resolve_stock_universe()` still returns the source it
  used and warns on stderr when it degrades to the 30-code seed pool, so the old
  failure mode is reported rather than fixed by having been fixed once.
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
- **15 `signal_tracker` task runs sat in `running` from 2026-07-16.** Their
  processes died before the finaliser ran, and the finaliser had been calling
  the INSERT helper with an `id`, so it could never close its own row. The
  finaliser now updates, cycles reclaim stale rows as `timed_out`, and the
  historical 15 have been reclaimed by `scripts/reclaim_stale_task_runs.py`.
- **No strategy has been promoted.** Of 240 strategies, 187 are `rejected`, 34
  `submitted`, 0 live. The gates reject; that is the system working, not a bug.
- Single-machine, single-user. See the user-system ruling in `CLAUDE.md` before
  touching anything `user_id`-shaped.
- `docs/` contains historical reports whose "100% complete" claims predate this
  tree. `docs/CURRENT_PRODUCT_TRUTH.md` is the current-state pointer; prefer the
  tree over both.
- `docs/archive/frontend/` is historical only. It is not executable and not a
  readiness claim.
