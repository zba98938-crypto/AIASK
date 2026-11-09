#!/usr/bin/env bash
# ONE-OFF runbook (2026-10-26). Run OUTSIDE the Claude sandbox, from the repo root:
#
#     bash scripts/ops/runbook_catch_up_2026_10_26.sh 2>&1 | tee /tmp/aiask-catchup.log
#
# Each step checks its own result and stops on the first failure.
# Send /tmp/aiask-catchup.log back for review. Delete this file afterwards.
set -euo pipefail
cd "$(dirname "$0")/../.."
PY=/opt/miniconda3/envs/aiask/bin/python

# The sandbox bootstrap redirects to the .aiask-local snapshot DB; never here.
unset PYTHONPATH AIASK_SQLITE_PATH AKSHARE_MCP_SQLITE_PATH STRATEGY_FACTORY_TASK_BOARD_PATH AIASK_RUNTIME_HOME
step() { echo; echo "===== $(date '+%F %T') $*"; }
fail() { echo "STOP: $*"; exit 1; }

step "0. preflight"
DB=$($PY -c 'import sys; sys.path.insert(0,"packages/aiask-quant-core/src"); from aiask_quant_core.runtime_paths import resolve_sqlite_path_with_source as r; print(r()[0])')
echo "db=$DB"
[ "$DB" = "$HOME/.aiask/data/db/akshare_mcp.sqlite3" ] || fail "DB is not the authoritative one"
touch "$HOME/.aiask/data/db/.probe" && rm "$HOME/.aiask/data/db/.probe" || fail "DB dir not writable"
for f in /tmp/aiask-supervisor.pid "$HOME/.aiask/run/sidecars.pid"; do
  if [ -f "$f" ] && kill -0 "$(cat "$f")" 2>/dev/null; then fail "already running: $f pid=$(cat "$f")"; fi
done
BEFORE=$($PY scripts/check_score85_progress.py); echo "$BEFORE" | sed -n 1,3p

step "1. daily sync (full market, Sina rate-safe concurrency 1; ~1-2 h)"
$PY scripts/free_data_daily_sync.py --json > /tmp/aiask-daily-sync.json || fail "daily sync exited non-zero; see /tmp/aiask-daily-sync.json"
tail -c 2000 /tmp/aiask-daily-sync.json; echo
$PY - <<'EOF' || exit 1
import json, subprocess, sys
r = json.loads(subprocess.check_output([sys.executable, "scripts/check_score85_progress.py", "--json"]))
md = r["market_data"]
print("latest_bar_date", md["latest_bar_date"], "synced_session", md["synced_session"])
if not md["synced_session"] or md["synced_session"] < "2026-10-23" or md["synced_session"] != md["latest_bar_date"]:
    sys.exit("STOP: kline_1d not caught up to a full session >= 2026-10-23")
EOF

step "2. purge weekend paper_nav / incubation metrics (backup first)"
$PY scripts/ops/purge_weekend_snapshot_rows.py --apply
$PY - <<'EOF' || exit 1
import json, subprocess, sys
r = json.loads(subprocess.check_output([sys.executable, "scripts/ops/purge_weekend_snapshot_rows.py"]))
left = {t: v for t, v in r["to_delete"].items() if v}
print("remaining weekend rows:", left or "none")
if left:
    sys.exit("STOP: weekend rows remain after purge")
EOF

step "3. start supervisor and sidecars"
# Local time: strategy_factory_runs.started_at is written as local +08:00.
START_TS=$(date '+%Y-%m-%dT%H:%M:%S')
$PY scripts/start_factories_daemon.py || fail "supervisor did not start; see /tmp/aiask-supervisor.log"
nohup $PY scripts/ops/run_sidecars.py > /tmp/aiask-sidecars.log 2>&1 &
sleep 10
kill -0 "$(cat "$HOME/.aiask/run/sidecars.pid")" 2>/dev/null || fail "sidecars not running; see /tmp/aiask-sidecars.log"
echo "supervisor pid=$(cat /tmp/aiask-supervisor.pid) sidecars pid=$(cat "$HOME/.aiask/run/sidecars.pid")"

step "4a. LLM gateway reachable with .env config (key not printed)"
$PY - <<'EOF' || exit 1
import json, sys, urllib.request
env = {}
for line in open(".env", encoding="utf-8"):
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip().strip('"').strip("'")
base, key, model = env.get("STRATEGY_LLM_BASE_URL", ""), env.get("STRATEGY_LLM_API_KEY", ""), env.get("STRATEGY_LLM_MODEL", "")
if not (base and key and model):
    sys.exit("STOP: STRATEGY_LLM_BASE_URL / API_KEY / MODEL not all set in .env")
body = json.dumps({"model": model, "messages": [{"role": "user", "content": "reply with: ok"}], "max_tokens": 8}).encode()
req = urllib.request.Request(base.rstrip("/") + "/chat/completions", data=body,
                             headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
try:
    resp = json.loads(urllib.request.urlopen(req, timeout=60).read())
except Exception as exc:
    sys.exit(f"STOP: gateway call failed: {type(exc).__name__}: {str(exc)[:200]}")
print("gateway ok, model:", resp.get("model"), "choices:", len(resp.get("choices") or []))
EOF

step "4b. wait for a strategy_factory run started after $START_TS (up to 12 min)"
$PY - "$START_TS" <<'EOF' || exit 1
import sqlite3, sys, time
from pathlib import Path
start = sys.argv[1]
db = Path.home() / ".aiask/data/db/akshare_mcp.sqlite3"
deadline = time.time() + 720
while time.time() < deadline:
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    rows = con.execute(
        "SELECT run_id, status, started_at, substr(coalesce(error,''),1,200) FROM strategy_factory_runs "
        "WHERE started_at >= ? AND completed_at IS NOT NULL ORDER BY started_at", (start,)).fetchall()
    con.close()
    if rows:
        for r in rows:
            print(r)
        statuses = {r[1] for r in rows}
        if statuses <= {"partial_llm", "skipped"}:
            print("NOTE: no run left partial_llm/skipped yet; LLM may still be gated by the cohort readiness gate")
        sys.exit(0)
    time.sleep(30)
sys.exit("STOP: no completed strategy_factory run within 12 min; see /tmp/aiask-supervisor.log")
EOF

step "5. agent test suite (needs local socket bind)"
(cd packages/agent && $PY -m pytest -q -p no:cacheprovider 2>&1 | tail -25) || true

step "6. loop counters after catch-up"
$PY scripts/check_score85_progress.py
echo; echo "===== done $(date '+%F %T')"
