# Strategy Factory Quality Records

`CURRENT.md` is the latest Markdown review produced by
`scripts/factories/monitor_factory_quality.py`. Daily files use the
`YYYY-MM-DD.md` naming scheme. The observer reads the four-runtime supervisor
heartbeat and the SQLite `strategy_factory_runs` / `strategy_quality_reports`
records, then appends one review entry for each completed strategy-factory run.

The observer is read-only with respect to the factory database. A factory run
with `status=success` means that execution completed; the review still reports
quality-gate pass rate, negative Sharpe samples, drawdown, validation grade, and
blockers before classifying the round.

The detached observer can be started with:

```bash
/opt/miniconda3/envs/aiask/bin/python scripts/factories/monitor_factory_quality.py --daemon --interval 60
```

Its runtime state and PID are stored under `$AIASK_RUNTIME_HOME/run/`, while
the Markdown documents are kept here for review and handoff.
