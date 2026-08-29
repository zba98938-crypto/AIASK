#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""查 P0 修改路径在历史 1961 条 quality_reports 中的反事实基线。

核心问题:历史中有多少条候选满足 P0 解封条件?
解封条件 (DEV-V1 文档 §P0.2):
  - validation_grade == 'D' (硬否决前提)
  - gate_b decision != 'block' / 'reject' (Gate-passed,不是 quality_gate 自己拒)
  - submission_lane != 'observe_incubation' (老逻辑下应该是 'rejected')
"""
from __future__ import annotations
import os
import sqlite3
import json
from collections import Counter
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

DB = _aiask_db_path()
con = sqlite3.connect(DB)
cur = con.cursor()

cur.execute("SELECT COUNT(*) FROM strategy_quality_reports")
total = cur.fetchone()[0]
print(f"[base] 总 reports: {total}")

# 全表扫描提取 validation_grade + gate_b decision + submission_lane
cur.execute("SELECT id, summary FROM strategy_quality_reports ORDER BY id")
rows = cur.fetchall()
print(f"[base] 扫描 {len(rows)} 行...")

stats = {
    "grade_dist": Counter(),
    "lane_dist": Counter(),
    "gate_b_dist": Counter(),
    "d_grade_total": 0,
    "d_grade_gate_b_pass": 0,  # ★ P0 解封候选数
    "d_grade_gate_b_pass_rejected_lane": 0,  # ★★ 真实"被 D 级硬否决"的候选数
    "d_grade_gate_b_pass_observe_lane": 0,  # 已在 observe lane
    "parse_fails": 0,
}

for rid, sj in rows:
    try:
        sm = json.loads(sj) if sj else {}
    except Exception:
        stats["parse_fails"] += 1
        continue

    grade = str(sm.get("validation_grade") or "")
    lane = str(sm.get("submission_lane") or "")
    gb = sm.get("gate_b") or {}
    gb_dec = str(gb.get("decision") or "")

    stats["grade_dist"][grade] += 1
    stats["lane_dist"][lane] += 1
    stats["gate_b_dist"][gb_dec] += 1

    if grade == "D":
        stats["d_grade_total"] += 1
        # gate_b 不是 block / reject = Gate-passed
        if gb_dec not in ("block", "reject", "rejected"):
            stats["d_grade_gate_b_pass"] += 1
            if lane == "rejected":
                stats["d_grade_gate_b_pass_rejected_lane"] += 1
            elif lane == "observe_incubation":
                stats["d_grade_gate_b_pass_observe_lane"] += 1

print(f"\n[base] === Grade 分布 ===")
for g, c in stats["grade_dist"].most_common():
    print(f"  {g!r}: {c}")

print(f"\n[base] === submission_lane 分布 ===")
for l, c in stats["lane_dist"].most_common():
    print(f"  {l!r}: {c}")

print(f"\n[base] === gate_b decision 分布 ===")
for d, c in stats["gate_b_dist"].most_common():
    print(f"  {d!r}: {c}")

print(f"\n[base] === P0 解封基线 ===")
print(f"  D 级总数: {stats['d_grade_total']}")
print(f"  D 级 + Gate-B passed (P0 解封候选): {stats['d_grade_gate_b_pass']}")
print(f"    └── 老逻辑下被 'rejected' (P0 ON 后会改为 observe_incubation): {stats['d_grade_gate_b_pass_rejected_lane']}")
print(f"    └── 已在 'observe_incubation' lane: {stats['d_grade_gate_b_pass_observe_lane']}")

if stats["parse_fails"] > 0:
    print(f"\n[base] payload parse 失败: {stats['parse_fails']}")

con.close()
print(f"\n[base] DONE")
