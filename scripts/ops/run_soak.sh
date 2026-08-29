#!/usr/bin/env bash
# Four-runtime supervisor soak launcher (operator-run).
#
# This is NOT the 24h quality session (that is
# run_strategy_factory_quality_session.py --hours N) and NOT `factory cycle`.
# It supervises four runtimes: strategy, factor_mining, incubation,
# market_event_ingest. SignalTracker is a sidecar and is not started here.
#
# Usage:
#   scripts/ops/run_soak.sh probe      # ~90 min shakeout, then auto-stop
#   scripts/ops/run_soak.sh 24h        # full soak, no auto-stop
#   scripts/ops/run_soak.sh status     # health check while running
#   scripts/ops/run_soak.sh stop       # stop the supervisor + its process group
#
# Writes are forced off: the supervisor hard-assigns LIVE_TRADING_ENABLED=0,
# BROKER_ALLOW_WRITE=0, LIVE_TRADING_READ_ONLY=1 for every child. Verify that in
# the header of the log before trusting a run.

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
AIASK_ROOT="$(cd "$HERE/../.." && pwd)"
# AIASK_ROOT is <repo>/integrations/aiask, so the repo root is two levels up.
REPO_ROOT="$(cd "$AIASK_ROOT/../.." && pwd)"

export AIASK_PROJECT_ROOT="$AIASK_ROOT"
: "${AIASK_VENV_PYTHON:=$AIASK_ROOT/packages/agent/.venv/bin/python}"
export AIASK_VENV_PYTHON
: "${AIASK_RUNTIME_HOME:=$HOME/.aiask}"
export AIASK_RUNTIME_HOME

# The plugin needs psutil, which lives in the Hermes venv, not the AIASK one.
HERMES_PY="$REPO_ROOT/.venv/bin/python"
PLUGIN="$AIASK_ROOT/hermes-plugin/aiask-hermes-plugin/readiness.py"
SOAK_LOG_DIR="$AIASK_RUNTIME_HOME/logs/soak"
mkdir -p "$SOAK_LOG_DIR"

_plugin_call() {
  # $1 = python expression against the loaded module `m`
  "$HERMES_PY" - "$PLUGIN" "$1" <<'PY'
import importlib.util, json, sys
spec = importlib.util.spec_from_file_location("aiask_ops", sys.argv[1])
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
print(json.dumps(eval(sys.argv[2], {"m": m}), indent=2, default=str))
PY
}

case "${1:-}" in
  probe|24h)
    MODE="$1"
    STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
    LOG="$SOAK_LOG_DIR/soak-$MODE-$STAMP.log"

    echo "== preflight =="
    _plugin_call 'm.factory_ops_start(dry_run=True)'
    echo
    echo "== readiness (research track) =="
    _plugin_call 'm.triage_readiness()' | head -40 || true
    echo
    read -r -p "Start the four-runtime supervisor now? [y/N] " reply
    [[ "$reply" == "y" || "$reply" == "Y" ]] || { echo "aborted"; exit 1; }

    # caffeinate is load-bearing: without it macOS sleeps and the soak dies
    # partway through, which looks identical to a crash in the logs.
    echo "== starting (log: $LOG) =="
    nohup caffeinate -is "$HERMES_PY" - "$PLUGIN" >>"$LOG" 2>&1 <<'PY' &
import importlib.util, json, sys
spec = importlib.util.spec_from_file_location("aiask_ops", sys.argv[1])
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
print(json.dumps(m.factory_ops_start(confirm=True), indent=2, default=str), flush=True)
PY
    echo "launcher pid: $!"
    sleep 20
    echo
    echo "== status after 20s =="
    _plugin_call 'm.factory_ops_status()'

    if [[ "$MODE" == "probe" ]]; then
      echo
      echo "Probe mode: will auto-stop in 90 minutes."
      echo "Watch with:  tail -f $LOG"
      echo "Or:          $0 status"
      ( sleep 5400; "$0" stop >>"$LOG" 2>&1 ) &
    else
      echo
      echo "24h mode: no auto-stop. Stop with: $0 stop"
    fi
    echo
    echo "Success criteria (all four must hold — 'it did not crash' is NOT enough):"
    echo "  1. ops-status stays alive with a heartbeat newer than ~2x --heartbeat-interval (default 60s)"
    echo "  2. evidence row counts GROW: compare 'factory evidence' at start vs end"
    echo "  3. restart count stays low; a child restarting every cycle is a crashloop, not resilience"
    echo "  4. after stop, NO orphans: pgrep -f run_.*_factory returns nothing"
    echo
    echo "Failure signatures to grep for in $LOG:"
    echo "  Traceback | SystemExit | OOM | Killed | exited | ModuleNotFoundError | restart_count"
    ;;

  status)
    echo "== ops status =="
    _plugin_call 'm.factory_ops_status()'
    echo
    echo "== evidence counts =="
    _plugin_call 'm.evidence_summary()' | head -30 || true
    echo
    echo "== live children =="
    pgrep -fl 'run_strategy_factory|run_factor_mining|run_incubation|run_market_event' || echo "(none)"
    echo
    echo "== write-guard check (must all be 0/1) =="
    grep -m1 'live_trading_writes' "$SOAK_LOG_DIR"/soak-*.log 2>/dev/null | tail -1 || echo "(no log yet)"
    ;;

  stop)
    echo "== stopping =="
    _plugin_call 'm.factory_ops_stop()'
    sleep 3
    echo
    echo "== orphan check (must be empty — this is the P0-6 regression test) =="
    if pgrep -fl 'run_strategy_factory|run_factor_mining|run_incubation|run_market_event'; then
      echo "ORPHANS PRESENT — process-group teardown did not work; report this."
      exit 1
    fi
    echo "(clean: no orphaned children)"
    ;;

  *)
    sed -n '2,18p' "$0"
    exit 2
    ;;
esac
