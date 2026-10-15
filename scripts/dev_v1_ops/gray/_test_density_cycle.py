#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""跑 1 个 cycle 验证产出密度提升后的实际效果."""
from __future__ import annotations
import os
import subprocess
import sys
import sqlite3
import json
import time
from datetime import datetime
from pathlib import Path
from collections import Counter

SCRIPTS_DIR = Path(__file__).resolve().parents[2]
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from _integration_paths import default_sqlite_path, ensure_runtime_dirs, runtime_home
import logging

logger = logging.getLogger(__name__)

def _aiask_root() -> Path:
    override = os.getenv("AIASK_PROJECT_ROOT")
    if override:
        return Path(override).expanduser().resolve()
    current = Path(__file__).resolve()
    for parent in (current.parent, *current.parents):
        if (parent / "packages").is_dir() and (parent / "scripts").is_dir():
            return parent
    return Path.cwd()


def _aiask_db_path() -> str:
    override = os.getenv("AIASK_SQLITE_PATH") or os.getenv("AKSHARE_MCP_SQLITE_PATH")
    if override:
        return str(Path(override).expanduser().resolve())
    return str(default_sqlite_path())

ROOT = _aiask_root()
DB = _aiask_db_path()
ensure_runtime_dirs()
LOG_DIR = runtime_home() / "logs" / "dev_v1_ops"
LOG_DIR.mkdir(parents=True, exist_ok=True)
LOG_FILE = LOG_DIR / "density_cycle.log"
ERR_FILE = LOG_DIR / "density_cycle.err.log"

CYCLE_TIMEOUT_SEC = 25 * 60  # 25 分钟超时(参数翻倍后预计延长)


def db_snapshot() -> dict:
    con = sqlite3.connect(DB)
    cur = con.cursor()
    snap = {}
    cur.execute("SELECT COUNT(*) FROM strategy_quality_reports")
    snap["qr_total"] = cur.fetchone()[0]
    cur.execute("SELECT MAX(id) FROM strategy_quality_reports")
    snap["max_qr_id"] = cur.fetchone()[0] or 0
    cur.execute("SELECT COUNT(*) FROM strategy_generation_experiments")
    snap["exp_total"] = cur.fetchone()[0]
    cur.execute("SELECT MAX(id) FROM strategy_generation_experiments")
    snap["max_exp_id"] = cur.fetchone()[0] or 0
    cur.execute("SELECT COUNT(*) FROM strategies")
    snap["strategies_total"] = cur.fetchone()[0]
    try:
        cur.execute("SELECT COUNT(*) FROM strategy_incubation_accounts WHERE status='active' AND stage='paper'")
        snap["paper_accounts"] = cur.fetchone()[0]
    except Exception:
        snap["paper_accounts"] = -1
    cur.execute("SELECT MAX(id) FROM strategy_factory_runs")
    snap["max_run_id"] = cur.fetchone()[0] or 0
    con.close()
    return snap


def analyze_new_data(prev: dict) -> dict:
    """分析 cycle 跑完后新增的数据."""
    con = sqlite3.connect(DB)
    cur = con.cursor()

    # 新增 quality_reports
    cur.execute(
        "SELECT id, summary FROM strategy_quality_reports WHERE id > ? ORDER BY id",
        (prev["max_qr_id"],)
    )
    qr_rows = cur.fetchall()

    qr_stats = {
        "new_count": len(qr_rows),
        "lanes": Counter(),
        "grades": Counter(),
        "gate_b": Counter(),
        "observe_incubation": 0,
        "d_grade_observe_count": 0,
    }
    for rid, sj in qr_rows:
        try:
            sm = json.loads(sj) if sj else {}
        except Exception as _swallowed:
            logger.warning("scripts/dev_v1_ops/gray/_test_density_cycle.py:98: swallowed %s and continued: %s", type(_swallowed).__name__, _swallowed)
            continue
        lane = str(sm.get("submission_lane") or "")
        grade = str(sm.get("validation_grade") or "")
        gb = str((sm.get("gate_b") or {}).get("decision") or "")
        reason = str(sm.get("runtime_bootstrap_reason") or "")
        qr_stats["lanes"][lane] += 1
        qr_stats["grades"][grade] += 1
        qr_stats["gate_b"][gb] += 1
        if lane == "observe_incubation":
            qr_stats["observe_incubation"] += 1
        if "d_grade_observe" in reason:
            qr_stats["d_grade_observe_count"] += 1

    # 新增 experiments
    cur.execute(
        "SELECT id, generator_type, status FROM strategy_generation_experiments WHERE id > ?",
        (prev["max_exp_id"],)
    )
    exp_rows = cur.fetchall()
    exp_stats = {
        "new_count": len(exp_rows),
        "generators": Counter(),
        "statuses": Counter(),
    }
    for rid, gt, st in exp_rows:
        exp_stats["generators"][gt or "?"] += 1
        exp_stats["statuses"][st or "?"] += 1

    # cycle summary
    cur.execute(
        "SELECT id, status, summary, elapsed_seconds FROM strategy_factory_runs WHERE id > ? ORDER BY id DESC LIMIT 1",
        (prev["max_run_id"],)
    )
    run_row = cur.fetchone()
    run_summary = {}
    if run_row:
        rid, status, sj, elapsed = run_row
        run_summary["run_id"] = rid
        run_summary["status"] = status
        run_summary["elapsed_sec"] = elapsed
        try:
            sm = json.loads(sj) if sj else {}
            for k in ["llm_status_counts", "pipeline_fallback_counts",
                      "candidates_spawned", "autonomy_generated",
                      "autonomy_task_count", "autonomy_completed_task_count",
                      "autonomy_failed_task_count",
                      "bulk_stock_matrix_family_counts",
                      "bulk_stock_matrix_planned_family_counts"]:
                if k in sm:
                    run_summary[k] = sm[k]
        except Exception as _swallowed:
            logger.warning("scripts/dev_v1_ops/gray/_test_density_cycle.py:149: swallowed %s and continued: %s", type(_swallowed).__name__, _swallowed)
            pass

    con.close()
    return {"qr": qr_stats, "exp": exp_stats, "run": run_summary}


print(f"=== 产出密度提升 — cycle 测试 ===")
print(f"启动时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

print(f"\n=== pre snapshot ===")
pre = db_snapshot()
for k, v in pre.items():
    print(f"  {k}: {v}")

print(f"\n=== 启动 cycle (timeout {CYCLE_TIMEOUT_SEC//60} min) ===")
start = time.monotonic()
with open(LOG_FILE, "wb") as fout, open(ERR_FILE, "wb") as ferr:
    proc = subprocess.Popen(
        [sys.executable, "-X", "utf8", "-u", str(ROOT / "scripts" / "factories" / "run_strategy_factory.py"), "--once"],
        cwd=str(ROOT),
        stdout=fout,
        stderr=ferr,
    )
    print(f"PID: {proc.pid}", flush=True)

    last_size = 0
    while True:
        elapsed = time.monotonic() - start
        rc = proc.poll()
        if rc is not None:
            print(f"cycle 退出 elapsed={elapsed:.0f}s rc={rc}", flush=True)
            break
        if elapsed > CYCLE_TIMEOUT_SEC:
            print(f"超时,terminate", flush=True)
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
            break
        # 每 30s 打印 err.log 大小
        try:
            sz = ERR_FILE.stat().st_size
        except FileNotFoundError:
            sz = 0
        if sz != last_size or int(elapsed) % 60 == 0:
            print(f"  [{elapsed:.0f}s] err.log={sz}B (Δ{sz - last_size:+d})", flush=True)
            last_size = sz
        time.sleep(30)


print(f"\n=== 分析新数据 ===")
result = analyze_new_data(pre)

print(f"\n[Run summary]")
for k, v in result["run"].items():
    if isinstance(v, dict):
        print(f"  {k}: {v}")
    else:
        print(f"  {k}: {v}")

print(f"\n[Strategy Generation Experiments 新增]")
exp = result["exp"]
print(f"  total new: {exp['new_count']} (vs 之前 cycle 通常 ~100)")
print(f"  generators: {dict(exp['generators'])}")
print(f"  statuses: {dict(exp['statuses'])}")

print(f"\n[Quality Reports 新增 ★]")
qr = result["qr"]
print(f"  total new: {qr['new_count']} (vs 之前 1/cycle)")
print(f"  lanes: {dict(qr['lanes'])}")
print(f"  grades: {dict(qr['grades'])}")
print(f"  gate_b: {dict(qr['gate_b'])}")
print(f"  observe_incubation 命中: {qr['observe_incubation']} ★★★")
print(f"  d_grade_observe 路由: {qr['d_grade_observe_count']}")

post = db_snapshot()
print(f"\n[Δ vs pre]")
for k in pre:
    if isinstance(pre[k], (int, float)):
        delta = post[k] - pre[k]
        if delta != 0:
            print(f"  {k}: {pre[k]} → {post[k]} (Δ {delta:+d})")

# 最终判定
print(f"\n=== 总判定 ===")
qr_growth = qr["new_count"]
exp_growth = exp["new_count"]
prev_qr_per_cycle = 1
prev_exp_per_cycle = 30  # 估算

print(f"  quality_reports: {qr_growth} (基线 ~{prev_qr_per_cycle},倍数 {qr_growth / max(prev_qr_per_cycle, 1):.1f}×)")
print(f"  experiments:     {exp_growth} (基线 ~{prev_exp_per_cycle},倍数 {exp_growth / max(prev_exp_per_cycle, 1):.1f}×)")
print(f"  observe_incubation hits: {qr['observe_incubation']}")

if qr["observe_incubation"] > 0:
    print(f"\n  🎉 P0 路径触发! ({qr['observe_incubation']} 条候选走 observe_incubation lane)")
elif qr_growth > prev_qr_per_cycle:
    print(f"\n  ✅ 产出密度提升,quality_reports 新增 {qr_growth} > 基线 {prev_qr_per_cycle}")
elif qr_growth == prev_qr_per_cycle:
    print(f"\n  🟡 产出未变化(可能 LLM 阶段问题)")
else:
    print(f"\n  ⚠️ 产出反而下降")

print(f"\n=== DONE ===")
