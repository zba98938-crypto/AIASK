#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""DEV-V1 灰度合并执行器(阶段 1+2+3).

设计:
  1. 持续跑 N 个 cycle (默认 5,可调),累积 D + Gate-passed 候选
  2. P0 + P1 同时 ON (从 .env 读)
  3. 每个 cycle 后检查:
     - cycle rc 是否 0
     - quality_reports 增量
     - submission_lane='observe_incubation' 命中数
     - paper 账户增量
     - paper_observation_recognized 事件数(P1 触发证据)
  4. 红线触发立即停止
  5. 最后输出汇总报告

优势:
  - 一次性把 3 个运维步骤合并执行
  - 自动监控,无需手工跑探针
  - 任何红线触发自动停止
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from collections import Counter
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
    return str(_aiask_root() / "data" / "db" / "akshare_mcp.sqlite3")

ROOT = _aiask_root()
DB = _aiask_db_path()

MAX_CYCLES = 5  # 最多跑 5 个 cycle
CYCLE_TIMEOUT_SEC = 15 * 60  # 单 cycle 超时 15 分钟
RED_LINE_R2 = 200  # paper 账户增量上限

LOG_DIR = ROOT / "_gray_full_logs"
LOG_DIR.mkdir(exist_ok=True)


def db_snapshot() -> dict:
    """对 DB 当前状态做快照."""
    con = sqlite3.connect(DB)
    cur = con.cursor()
    snap = {}
    cur.execute("SELECT COUNT(*) FROM strategy_quality_reports")
    snap["quality_reports_total"] = cur.fetchone()[0]
    cur.execute("SELECT MAX(id) FROM strategy_quality_reports")
    snap["max_qr_id"] = cur.fetchone()[0] or 0

    cur.execute("PRAGMA table_info(strategy_incubation_accounts)")
    has_acc = bool(cur.fetchall())
    if has_acc:
        cur.execute("SELECT stage, COUNT(*) FROM strategy_incubation_accounts WHERE status='active' GROUP BY stage")
        snap["account_stages"] = dict(cur.fetchall())
        cur.execute("SELECT COUNT(*) FROM strategy_incubation_accounts WHERE status='active' AND stage='paper'")
        snap["paper_accounts"] = cur.fetchone()[0]
    else:
        snap["account_stages"] = {}
        snap["paper_accounts"] = 0

    # P1 事件:incubation_factory.paper_observation_recognized
    try:
        cur.execute("PRAGMA table_info(strategy_domain_events)")
        if cur.fetchall():
            cur.execute(
                "SELECT COUNT(*) FROM strategy_domain_events WHERE event_type='incubation_factory.paper_observation_recognized'"
            )
            snap["p1_recognized_events"] = cur.fetchone()[0]
        else:
            snap["p1_recognized_events"] = 0
    except Exception:
        snap["p1_recognized_events"] = 0

    con.close()
    return snap


def analyze_new_reports(prev_max_id: int) -> dict:
    """分析本 cycle 新增的 quality_reports."""
    con = sqlite3.connect(DB)
    cur = con.cursor()
    cur.execute(
        "SELECT id, strategy_id, summary FROM strategy_quality_reports WHERE id > ? ORDER BY id",
        (prev_max_id,)
    )
    rows = cur.fetchall()

    stats = {
        "new_count": len(rows),
        "lanes": Counter(),
        "grades": Counter(),
        "gate_b": Counter(),
        "observe_incubation_count": 0,
        "d_grade_observe_count": 0,
        "v5_pr1_inject_status": Counter(),
        "v5_pr1_dsr_nonnull": 0,
    }

    for rid, sid, sj in rows:
        try:
            sm = json.loads(sj) if sj else {}
        except Exception as _swallowed:
            logger.warning("scripts/dev_v1_ops/gray/_gray_phase_full.py:125: swallowed %s and continued: %s", type(_swallowed).__name__, _swallowed)
            continue
        lane = str(sm.get("submission_lane") or "")
        grade = str(sm.get("validation_grade") or "")
        gb_dec = str((sm.get("gate_b") or {}).get("decision") or "")
        bootstrap_reason = str(sm.get("runtime_bootstrap_reason") or "")

        stats["lanes"][lane] += 1
        stats["grades"][grade] += 1
        stats["gate_b"][gb_dec] += 1

        if lane == "observe_incubation":
            stats["observe_incubation_count"] += 1
        if "d_grade_observe" in bootstrap_reason:
            stats["d_grade_observe_count"] += 1

        # V5-PR-1 字段
        inj_st = sm.get("multiple_testing_inject_status")
        if inj_st:
            stats["v5_pr1_inject_status"][str(inj_st)] += 1
        if sm.get("deflated_sharpe_ratio") is not None:
            stats["v5_pr1_dsr_nonnull"] += 1

    con.close()
    return stats


def run_one_cycle(idx: int) -> dict:
    """执行 1 个 cycle,返回结果."""
    log_file = LOG_DIR / f"cycle_{idx}.log"
    err_file = LOG_DIR / f"cycle_{idx}.err.log"

    print(f"\n[cycle-{idx}] === 启动 ===", flush=True)
    print(f"[cycle-{idx}] 时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}", flush=True)
    print(f"[cycle-{idx}] log: {log_file}", flush=True)

    # 关键: 不传 env override,让子进程从 .env 读 toggle
    # (run_strategy_factory.py 内部应该会调 dotenv.load_dotenv 或类似)
    pre_snap = db_snapshot()
    print(f"[cycle-{idx}] pre: qr={pre_snap['quality_reports_total']} paper={pre_snap['paper_accounts']} p1_events={pre_snap['p1_recognized_events']}", flush=True)

    start = time.monotonic()
    with open(log_file, "wb") as fout, open(err_file, "wb") as ferr:
        proc = subprocess.Popen(
            [sys.executable, "-X", "utf8", "-u", "run_strategy_factory.py", "--once"],
            cwd=str(ROOT),
            stdout=fout,
            stderr=ferr,
        )
        print(f"[cycle-{idx}] PID: {proc.pid}", flush=True)

        last_log_size = 0
        last_progress = start
        while True:
            elapsed = time.monotonic() - start
            rc = proc.poll()
            if rc is not None:
                print(f"[cycle-{idx}] 退出 elapsed={elapsed:.0f}s rc={rc}", flush=True)
                break
            if elapsed > CYCLE_TIMEOUT_SEC:
                print(f"[cycle-{idx}] 超时 {CYCLE_TIMEOUT_SEC}s, terminate", flush=True)
                proc.terminate()
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
                break
            # 进度
            try:
                cur_size = err_file.stat().st_size
            except FileNotFoundError:
                cur_size = 0
            if cur_size != last_log_size or (time.monotonic() - last_progress) >= 60:
                print(f"[cycle-{idx}] [{elapsed:.0f}s] err_log={cur_size}B (Δ{cur_size - last_log_size:+d})", flush=True)
                last_log_size = cur_size
                last_progress = time.monotonic()
            time.sleep(15)

    rc = proc.returncode
    post_snap = db_snapshot()
    new_stats = analyze_new_reports(pre_snap["max_qr_id"])

    cycle_result = {
        "idx": idx,
        "rc": rc,
        "elapsed_sec": time.monotonic() - start,
        "pre": pre_snap,
        "post": post_snap,
        "new_reports": new_stats,
        "delta_paper_accounts": post_snap["paper_accounts"] - pre_snap["paper_accounts"],
        "delta_p1_events": post_snap["p1_recognized_events"] - pre_snap["p1_recognized_events"],
    }

    print(f"[cycle-{idx}] === 结果 ===", flush=True)
    print(f"[cycle-{idx}] rc={rc} elapsed={cycle_result['elapsed_sec']:.0f}s", flush=True)
    print(f"[cycle-{idx}] new_reports={new_stats['new_count']} lanes={dict(new_stats['lanes'])} grades={dict(new_stats['grades'])}", flush=True)
    print(f"[cycle-{idx}] **observe_incubation={new_stats['observe_incubation_count']}** d_grade_observe={new_stats['d_grade_observe_count']}", flush=True)
    print(f"[cycle-{idx}] **paper Δ={cycle_result['delta_paper_accounts']}** p1_events Δ={cycle_result['delta_p1_events']}", flush=True)
    print(f"[cycle-{idx}] V5-PR-1 inject={dict(new_stats['v5_pr1_inject_status'])} dsr_nonnull={new_stats['v5_pr1_dsr_nonnull']}", flush=True)

    # 红线检查
    red = []
    if rc != 0:
        red.append(f"R1: rc={rc}")
    if cycle_result["delta_paper_accounts"] > RED_LINE_R2:
        red.append(f"R2: paper Δ={cycle_result['delta_paper_accounts']} > {RED_LINE_R2}")
    cycle_result["red_lines"] = red
    if red:
        print(f"[cycle-{idx}] ⚠️ RED LINES: {red}", flush=True)
    else:
        print(f"[cycle-{idx}] ✅ no red lines", flush=True)

    return cycle_result


def main():
    print(f"=== DEV-V1 灰度合并执行器(阶段 1+2+3)===", flush=True)
    print(f"启动时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}", flush=True)

    # 验证 .env toggle
    print(f"\n=== 检查 .env toggle ===", flush=True)
    env_path = ROOT / ".env"
    env_text = env_path.read_text(encoding="utf-8", errors="ignore")
    p0_on = "STRATEGY_FACTORY_OBSERVE_D_GRADE_ENABLED=1" in env_text
    p1_on = "INCUBATION_FACTORY_PAPER_INTAKE_ENABLED=1" in env_text
    p1_limit_set = "INCUBATION_FACTORY_PAPER_INTAKE_BATCH_LIMIT" in env_text
    print(f"  P0 toggle (STRATEGY_FACTORY_OBSERVE_D_GRADE_ENABLED=1): {p0_on}", flush=True)
    print(f"  P1 toggle (INCUBATION_FACTORY_PAPER_INTAKE_ENABLED=1): {p1_on}", flush=True)
    print(f"  P1 batch limit set: {p1_limit_set}", flush=True)
    if not (p0_on and p1_on):
        print(f"  ❌ 缺少 toggle, 退出", flush=True)
        return 1

    # 初始 DB 快照
    init_snap = db_snapshot()
    print(f"\n=== 初始 DB 状态 ===", flush=True)
    for k, v in init_snap.items():
        print(f"  {k}: {v}", flush=True)

    # 跑多个 cycle
    cycles = []
    for i in range(1, MAX_CYCLES + 1):
        result = run_one_cycle(i)
        cycles.append(result)

        # 红线触发立即停止
        if result["red_lines"]:
            print(f"\n!!! 红线触发, 停止后续 cycle !!!", flush=True)
            break

        # 提前成功条件:已经产生 paper 账户增量,P1 事件也开始记录
        if result["delta_paper_accounts"] > 0 and result["delta_p1_events"] > 0:
            print(f"\n✅ 阶段成功标志达成: paper 账户增长 + P1 事件记录", flush=True)
            print(f"   再多跑 1 个 cycle 验证稳定性后退出", flush=True)
            if i < MAX_CYCLES:
                # 再跑 1 个 cycle 验证稳定
                result2 = run_one_cycle(i + 1)
                cycles.append(result2)
            break

    # === 汇总 ===
    print(f"\n=== 汇总报告 ===", flush=True)
    final_snap = db_snapshot()
    print(f"\n初始 → 最终:", flush=True)
    for k in init_snap:
        if isinstance(init_snap[k], (int, float)):
            print(f"  {k}: {init_snap[k]} → {final_snap.get(k, '?')}", flush=True)
    print(f"  account_stages: {init_snap.get('account_stages')} → {final_snap.get('account_stages')}", flush=True)

    print(f"\n各 cycle 概要:", flush=True)
    print(f"  {'idx':<5} {'rc':<5} {'sec':<6} {'new':<5} {'observe':<8} {'paper Δ':<8} {'p1 Δ':<6} {'red':<10}", flush=True)
    print(f"  {'-'*60}", flush=True)
    for c in cycles:
        rl = ','.join(c['red_lines']) if c['red_lines'] else 'ok'
        print(f"  {c['idx']:<5} {c['rc']!s:<5} {c['elapsed_sec']:<6.0f} {c['new_reports']['new_count']:<5} {c['new_reports']['observe_incubation_count']:<8} {c['delta_paper_accounts']:+<8} {c['delta_p1_events']:+<6} {rl:<10}", flush=True)

    # 总判定
    total_new = sum(c["new_reports"]["new_count"] for c in cycles)
    total_observe = sum(c["new_reports"]["observe_incubation_count"] for c in cycles)
    total_paper_delta = final_snap["paper_accounts"] - init_snap["paper_accounts"]
    total_p1_events_delta = final_snap["p1_recognized_events"] - init_snap["p1_recognized_events"]
    any_red = any(c["red_lines"] for c in cycles)

    print(f"\n=== 总判定 ===", flush=True)
    print(f"  cycles 跑了: {len(cycles)}", flush=True)
    print(f"  总新增 quality_reports: {total_new}", flush=True)
    print(f"  总 observe_incubation 命中: {total_observe}", flush=True)
    print(f"  paper 账户净增量: {total_paper_delta}", flush=True)
    print(f"  P1 paper_observation_recognized 事件净增量: {total_p1_events_delta}", flush=True)
    print(f"  任何红线触发: {any_red}", flush=True)

    if any_red:
        print(f"\n❌ FAIL: 红线触发,需排查后再启动", flush=True)
        return 2
    elif total_observe > 0 and total_paper_delta > 0:
        print(f"\n✅ FULL SUCCESS: P0 + P1 全链路验证通过", flush=True)
        print(f"   - P0: D 级 + Gate-passed 候选成功路由到 observe_incubation lane", flush=True)
        print(f"   - lifecycle_coordinator: 创建了 stage='paper' 账户", flush=True)
        if total_p1_events_delta > 0:
            print(f"   - P1: incubation_factory 识别了 paper observation 候选", flush=True)
        return 0
    elif total_observe > 0:
        print(f"\n🟡 PARTIAL: P0 路由生效但 paper 账户未增长", flush=True)
        print(f"   可能 lifecycle_coordinator 路径有边界条件没满足", flush=True)
        return 3
    else:
        print(f"\n🟡 NO_TRIGGER: {len(cycles)} 个 cycle 都没产生 D + Gate-passed 候选", flush=True)
        print(f"   不是红线问题,数据巧合;可继续跑更多 cycle", flush=True)
        return 4


if __name__ == "__main__":
    code = main()
    print(f"\nDONE exit_code={code}", flush=True)
    sys.exit(code)
