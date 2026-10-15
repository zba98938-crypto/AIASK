#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""查孵化工厂当前策略数量。

孵化工厂的"进入"有几个层次:
  1. strategies.status='incubating' — 严格"孵化中"(策略工厂 Gate-3 通过)
  2. strategies.status='submitted' — 提交后等待 (含被 P1 处理 paper observation)
  3. strategy_incubation_accounts — 实际孵化账户 (warmup/paper/candidate/listed)
  4. submission_lane='observe_incubation' 的 quality_reports — 经 P0 路由的候选
  5. strategy_domain_events: incubation_factory.* — 孵化工厂处理事件
"""
from __future__ import annotations
import os
import sqlite3
import json
from collections import Counter
from datetime import datetime
from pathlib import Path
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

DB = _aiask_db_path()
con = sqlite3.connect(DB)
cur = con.cursor()

print("=" * 70)
print(f"孵化工厂策略总览 — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
print("=" * 70)

# === 第 1 层:strategies 表 status 分布 ===
print("\n[1] strategies 表 status 分布")
print("-" * 70)
try:
    cur.execute("PRAGMA table_info(strategies)")
    has_strategies = bool(cur.fetchall())
    if has_strategies:
        cur.execute("SELECT status, COUNT(*) FROM strategies GROUP BY status ORDER BY COUNT(*) DESC")
        for status, cnt in cur.fetchall():
            marker = " ← 孵化中(严格)" if status == "incubating" else ""
            marker = " ← 提交完成,可能在 paper 通道" if status == "submitted" else marker
            print(f"  {status!r:<25} {cnt}{marker}")
    else:
        print("  表不存在")
except Exception as exc:
    print(f"  失败: {exc}")

# === 第 2 层:strategy_incubation_accounts 实际账户 ===
print("\n[2] strategy_incubation_accounts 实际孵化账户(stage 分布)")
print("-" * 70)
try:
    cur.execute("PRAGMA table_info(strategy_incubation_accounts)")
    has_acc = bool(cur.fetchall())
    if has_acc:
        cur.execute("SELECT stage, status, COUNT(*) FROM strategy_incubation_accounts GROUP BY stage, status ORDER BY stage, status")
        rows = cur.fetchall()
        if rows:
            for stage, status, cnt in rows:
                marker = ""
                if stage == "warmup":
                    marker = " ← 初始热身阶段"
                elif stage == "paper":
                    marker = " ← P0 解封 D 级 + Gate-passed 创建"
                elif stage == "candidate":
                    marker = " ← 升级为正式候选"
                elif stage == "listed":
                    marker = " ← 已上线"
                print(f"  stage={stage!r:<15} status={status!r:<12} count={cnt}{marker}")
        else:
            print("  无任何账户")

        # active 账户总数
        cur.execute("SELECT COUNT(*) FROM strategy_incubation_accounts WHERE status='active'")
        active_total = cur.fetchone()[0]
        print(f"\n  active 账户总数: {active_total}")
    else:
        print("  表不存在")
except Exception as exc:
    print(f"  失败: {exc}")

# === 第 3 层:经 P0 路由的 submission_lane='observe_incubation' ===
print("\n[3] strategy_quality_reports submission_lane 分布(看 P0 触发证据)")
print("-" * 70)
try:
    cur.execute("SELECT id, summary FROM strategy_quality_reports ORDER BY id")
    lanes = Counter()
    bootstrap_reasons = Counter()
    obs_inc_ids = []
    for rid, sj in cur.fetchall():
        try:
            sm = json.loads(sj) if sj else {}
        except Exception as _swallowed:
            logger.warning("scripts/dev_v1_ops/query/_query_incubation_status.py:107: swallowed %s and continued: %s", type(_swallowed).__name__, _swallowed)
            continue
        lane = str(sm.get("submission_lane") or "")
        lanes[lane] += 1
        reason = str(sm.get("runtime_bootstrap_reason") or "")
        if "d_grade_observe" in reason or "observe" in reason:
            bootstrap_reasons[reason] += 1
        if lane == "observe_incubation":
            obs_inc_ids.append((rid, sm.get("strategy_id"), sm.get("validation_grade")))

    for lane, cnt in lanes.most_common():
        marker = ""
        if lane == "observe_incubation":
            marker = " ← P0/Observe 路径(含 D 级解封)★"
        elif lane == "deferred_submission":
            marker = " ← 老逻辑 D 级被拒"
        elif lane == "rejected":
            marker = " ← 被拒"
        elif lane == "formal_incubation":
            marker = " ← 正式孵化"
        print(f"  {lane!r:<30} {cnt}{marker}")

    if obs_inc_ids:
        print(f"\n  observe_incubation 命中明细 (前 10 条):")
        for rid, sid, grade in obs_inc_ids[:10]:
            print(f"    report_id={rid} strategy_id={sid} grade={grade}")

    if bootstrap_reasons:
        print(f"\n  observe-related runtime_bootstrap_reason 分布:")
        for reason, cnt in bootstrap_reasons.most_common():
            print(f"    {reason!r}: {cnt}")
except Exception as exc:
    print(f"  失败: {exc}")

# === 第 4 层:incubation_factory 相关 domain events ===
print("\n[4] strategy_domain_events 中 incubation_factory.* 事件")
print("-" * 70)
try:
    cur.execute("PRAGMA table_info(strategy_domain_events)")
    has_events = bool(cur.fetchall())
    if has_events:
        cur.execute(
            """SELECT event_type, COUNT(*)
            FROM strategy_domain_events
            WHERE event_type LIKE 'incubation%'
            GROUP BY event_type ORDER BY COUNT(*) DESC"""
        )
        rows = cur.fetchall()
        if rows:
            for event_type, cnt in rows:
                marker = ""
                if "paper_observation_recognized" in event_type:
                    marker = " ← P1 toggle ON 识别 paper 候选"
                elif "strategy_accepted" in event_type:
                    marker = " ← 孵化工厂接纳新策略"
                print(f"  {event_type!r:<70} {cnt}{marker}")
        else:
            print("  无任何 incubation_factory.* 事件")
    else:
        print("  表不存在")
except Exception as exc:
    print(f"  失败: {exc}")

# === 第 5 层:经 paper_observation 通道的实际策略 ===
print("\n[5] paper observation 通道现状(P1 SQL 查询逻辑)")
print("-" * 70)
try:
    cur.execute(
        """
        SELECT s.id, s.name, s.strategy_type, s.status,
               a.stage, a.status as account_status, a.bound_at
        FROM strategies s
        JOIN strategy_incubation_accounts a ON a.strategy_id = s.id
        WHERE s.status = 'submitted'
          AND a.stage = 'paper'
          AND a.status = 'active'
          AND NOT EXISTS (
              SELECT 1 FROM strategy_incubation_accounts a2
              WHERE a2.strategy_id = s.id
                AND a2.stage IN ('candidate', 'listed')
                AND a2.status = 'active'
          )
        ORDER BY a.bound_at DESC
        """
    )
    rows = cur.fetchall()
    if rows:
        print(f"  P1 SQL 命中策略数: {len(rows)}")
        print(f"\n  明细 (前 20 条):")
        for sid, name, stype, status, stage, acc_status, bound_at in rows[:20]:
            print(f"    {sid} | {name} | {stype} | {status} | {stage} | {bound_at}")
    else:
        print("  P1 SQL 命中: 0 条")
        print("  原因: stages 中要么没有 paper 阶段账户,要么策略 status 不是 'submitted'")
except Exception as exc:
    print(f"  失败: {exc}")

# === 第 6 层:近 24 小时增量 ===
print("\n[6] 近 24 小时孵化工厂活动")
print("-" * 70)
try:
    cur.execute(
        "SELECT COUNT(*) FROM strategy_quality_reports WHERE created_at >= datetime('now', '-1 day', 'localtime')"
    )
    new_qr = cur.fetchone()[0]
    print(f"  近 24h 新增 quality_reports: {new_qr}")

    cur.execute(
        "SELECT COUNT(*) FROM strategy_incubation_accounts WHERE created_at >= datetime('now', '-1 day', 'localtime')"
    )
    new_acc = cur.fetchone()[0]
    print(f"  近 24h 新增 incubation_accounts: {new_acc}")

    cur.execute(
        "SELECT COUNT(*) FROM strategy_domain_events WHERE event_type LIKE 'incubation%' AND created_at >= datetime('now', '-1 day', 'localtime')"
    )
    new_events = cur.fetchone()[0]
    print(f"  近 24h 新增 incubation 事件: {new_events}")
except Exception as exc:
    print(f"  失败: {exc}")

con.close()

print("\n" + "=" * 70)
print("DONE")
