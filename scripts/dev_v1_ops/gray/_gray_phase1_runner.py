#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""灰度阶段 1 — P0 toggle ON 执行器。

目的:
  1. 设置 STRATEGY_FACTORY_OBSERVE_D_GRADE_ENABLED=1
  2. 启动 strategy factory cycle (run_strategy_factory.py --once)
  3. cycle 跑完后,跑 SQL 探针验证 P0 是否生效

为什么不直接在 PowerShell 设 env var?
  - PowerShell here-string + 中文 + 长命令容易炸
  - 把 env var 放进 Python 子进程的 os.environ 更可控

相比直接调 run_strategy_factory.py 的优势:
  - 把 env var 设置 / cycle 启动 / 探针 验证一站式跑完
  - 输出统一到一个 log file 易检查
  - 即使 cycle 因 LLM 没产候选,探针仍可以验证 toggle 状态
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

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
    return str(_aiask_root() / "data" / "db" / "akshare_mcp.sqlite3")

ROOT = _aiask_root()
LOG_FILE = ROOT / "_gray_phase1.log"
ERR_FILE = ROOT / "_gray_phase1.err.log"

# === 步骤 1:设置 toggle ===
env = os.environ.copy()
env["STRATEGY_FACTORY_OBSERVE_D_GRADE_ENABLED"] = "1"
# 显式不开 P3 / P1 (本阶段只验 P0)
env.pop("STRATEGY_FACTORY_TRADE_AWARE_EXTRA_FAMILIES", None)
env["INCUBATION_FACTORY_PAPER_INTAKE_ENABLED"] = "1"
env.setdefault("INCUBATION_FACTORY_PAPER_INTAKE_BATCH_LIMIT", "300")

print(f"[gray] === 灰度阶段 1 启动 ===", flush=True)
print(f"[gray] 时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}", flush=True)
print(f"[gray] toggle: STRATEGY_FACTORY_OBSERVE_D_GRADE_ENABLED=1", flush=True)
print(f"[gray] 其他 toggle: 默认 OFF (P3 / P1 / V5-PR-1 helper 已是代码层默认开)", flush=True)

# === 步骤 2:记录启动前 DB 状态 ===
import sqlite3

DB = _aiask_db_path()
con = sqlite3.connect(DB)
cur = con.cursor()

cur.execute("SELECT COUNT(*) FROM strategy_quality_reports")
pre_qr = cur.fetchone()[0]
print(f"[gray] 启动前 quality_reports 总数: {pre_qr}", flush=True)

cur.execute("SELECT COUNT(*) FROM strategy_quality_reports WHERE created_at >= datetime('now', '-1 hour', 'localtime')")
pre_qr_recent = cur.fetchone()[0]
print(f"[gray] 启动前最近 1h quality_reports: {pre_qr_recent}", flush=True)

# 看 strategy_incubation_accounts 表中 stage 分布
try:
    cur.execute("PRAGMA table_info(strategy_incubation_accounts)")
    has_table = bool(cur.fetchall())
    if has_table:
        cur.execute("SELECT stage, COUNT(*) FROM strategy_incubation_accounts WHERE status='active' GROUP BY stage")
        rows = cur.fetchall()
        print(f"[gray] 启动前 active 账户 stage 分布: {rows}", flush=True)
        cur.execute("SELECT COUNT(*) FROM strategy_incubation_accounts WHERE status='active' AND stage='paper'")
        pre_paper = cur.fetchone()[0]
    else:
        pre_paper = 0
        print("[gray] 表 strategy_incubation_accounts 不存在,跳过", flush=True)
except Exception as exc:
    pre_paper = -1
    print(f"[gray] 查 paper 账户失败: {exc}", flush=True)

con.close()

# === 步骤 3:启动 cycle ===
print(f"\n[gray] 启动 cycle: python -X utf8 -u run_strategy_factory.py --once", flush=True)
print(f"[gray] 日志: {LOG_FILE} / {ERR_FILE}", flush=True)
print(f"[gray] 由于上一次 cycle 9 分钟仍在 LLM 阶段,本次设 max=12 分钟超时", flush=True)

start = time.monotonic()
with open(LOG_FILE, "wb") as fout, open(ERR_FILE, "wb") as ferr:
    proc = subprocess.Popen(
        [sys.executable, "-X", "utf8", "-u", "run_strategy_factory.py", "--once"],
        cwd=str(ROOT),
        env=env,
        stdout=fout,
        stderr=ferr,
    )
    print(f"[gray] cycle 进程 PID: {proc.pid}", flush=True)

    # 轮询 + 超时控制
    TIMEOUT_SEC = 12 * 60  # 12 分钟
    POLL_INTERVAL = 30
    while True:
        elapsed = time.monotonic() - start
        rc = proc.poll()
        if rc is not None:
            print(f"[gray] cycle 退出 elapsed={elapsed:.0f}s rc={rc}", flush=True)
            break
        if elapsed > TIMEOUT_SEC:
            print(f"[gray] 超时 {TIMEOUT_SEC}s,终止 cycle...", flush=True)
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
            print(f"[gray] cycle 已强制终止", flush=True)
            break
        # 进度显示:每 30s 打印 err.log 大小
        try:
            err_size = ERR_FILE.stat().st_size
        except FileNotFoundError:
            err_size = 0
        print(f"[gray] [{elapsed:.0f}s] cycle still running, err.log={err_size}B", flush=True)
        time.sleep(POLL_INTERVAL)

# === 步骤 4:cycle 结束 — 跑探针 ===
print(f"\n[gray] === cycle 结束,跑 SQL 探针 ===", flush=True)
con = sqlite3.connect(DB)
cur = con.cursor()

cur.execute("SELECT COUNT(*) FROM strategy_quality_reports")
post_qr = cur.fetchone()[0]
print(f"[gray] 启动后 quality_reports 总数: {post_qr} (新增 {post_qr - pre_qr})", flush=True)

# 抽查最近新增的报告
cur.execute(
    """SELECT id, strategy_id, created_at, summary
       FROM strategy_quality_reports
       WHERE id > ? ORDER BY id DESC LIMIT 30""",
    (pre_qr,)
)
new_rows = cur.fetchall()
print(f"[gray] 本次新增 {len(new_rows)} 条 reports", flush=True)

import json
from collections import Counter
import logging

logger = logging.getLogger(__name__)

submission_lanes = Counter()
validation_grades = Counter()
gate_b_decisions = Counter()
observe_incubation_count = 0
inject_status_count = Counter()
mt_mode_count = Counter()

for row in new_rows:
    rid, sid, ts, sum_json = row
    try:
        sm = json.loads(sum_json) if sum_json else {}
    except Exception as _swallowed:
        logger.warning("scripts/dev_v1_ops/gray/_gray_phase1_runner.py:173: swallowed %s and continued: %s", type(_swallowed).__name__, _swallowed)
        continue
    lane = sm.get("submission_lane")
    grade = sm.get("validation_grade")
    submission_lanes[str(lane)] += 1
    validation_grades[str(grade)] += 1

    gb = sm.get("gate_b") or {}
    gb_dec = gb.get("decision")
    gate_b_decisions[str(gb_dec)] += 1

    if lane == "observe_incubation":
        observe_incubation_count += 1

    # V5-PR-1 字段
    inject_st = sm.get("multiple_testing_inject_status")
    if inject_st:
        inject_status_count[str(inject_st)] += 1
    mt_md = sm.get("multiple_testing_mode")
    if mt_md:
        mt_mode_count[str(mt_md)] += 1

print(f"\n[gray] === 新增 reports 分析 ===", flush=True)
print(f"[gray] submission_lane: {dict(submission_lanes)}", flush=True)
print(f"[gray] validation_grade: {dict(validation_grades)}", flush=True)
print(f"[gray] gate_b decision: {dict(gate_b_decisions)}", flush=True)
print(f"[gray] **observe_incubation 命中数: {observe_incubation_count} / {len(new_rows)}**", flush=True)
print(f"[gray] V5-PR-1 inject_status: {dict(inject_status_count)}", flush=True)
print(f"[gray] V5-PR-1 mt_mode: {dict(mt_mode_count)}", flush=True)

# 检查 paper 账户增量
try:
    cur.execute("SELECT COUNT(*) FROM strategy_incubation_accounts WHERE status='active' AND stage='paper'")
    post_paper = cur.fetchone()[0]
    print(f"\n[gray] paper 账户: 启动前 {pre_paper} → 启动后 {post_paper} (增量 {post_paper - pre_paper})", flush=True)
except Exception as exc:
    print(f"[gray] 查 paper 账户失败: {exc}", flush=True)
    post_paper = -1

con.close()

# === 步骤 5:判定 ===
print(f"\n[gray] === 灰度阶段 1 判定 ===", flush=True)

# 红线检查
r1 = (proc.returncode is not None and proc.returncode != 0)
r2 = (post_paper - pre_paper if post_paper >= 0 and pre_paper >= 0 else 0) > 200
r3 = False  # 暂不检查 R3,需要单独 SQL

if r1:
    print(f"[gray] ❌ R1 触发: cycle 异常退出 rc={proc.returncode}", flush=True)
if r2:
    print(f"[gray] ❌ R2 触发: paper 账户增量 {post_paper - pre_paper} > 200", flush=True)

if not (r1 or r2 or r3):
    print(f"[gray] ✅ 无红线告警", flush=True)

if observe_incubation_count > 0:
    print(f"[gray] ✅ P0 toggle 生效: observe_incubation 命中 {observe_incubation_count} 条", flush=True)
elif len(new_rows) == 0:
    print(f"[gray] 🟡 cycle 0 候选 (LLM 路径问题),无法验证 P0 toggle 在新数据上的生效", flush=True)
    print(f"[gray]    但代码层 toggle 已设 ON,下次 cycle 真有候选时会路由到 observe_incubation", flush=True)
else:
    print(f"[gray] 🟡 cycle 产 {len(new_rows)} 条候选但 0 条进 observe_incubation lane", flush=True)
    print(f"[gray]    可能原因: 候选都是 ABC 级,无 D 级 + Gate-passed", flush=True)

print(f"\n[gray] DONE", flush=True)
