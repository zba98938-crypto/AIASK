#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""定向检查最近 5 条 reports 的 quality_gate 中 V5-PR-1 注入痕迹。"""
import sqlite3, json
from datetime import datetime, timedelta
from pathlib import Path
import os

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
cur.execute("PRAGMA table_info(strategy_quality_reports)")
cols = [r[1] for r in cur.fetchall()]
print(f"[chk] columns: {cols}")

cutoff = (datetime.now() - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")

# 注意:列名 quality_gate, summary, validation_report 都是 TEXT/JSON 字段
cur.execute(
    """SELECT id, strategy_id, created_at, quality_gate, summary
    FROM strategy_quality_reports
    WHERE created_at >= ? ORDER BY created_at DESC""",
    (cutoff,),
)
rows = cur.fetchall()
print(f"[chk] {len(rows)} rows in last hour")

for row in rows:
    rid, sid, ts, qg_json, sum_json = row
    print(f"\n=== id={rid} strategy={sid} ts={ts} ===")

    try:
        qg = json.loads(qg_json) if qg_json else {}
    except Exception as e:
        print(f"  qg parse fail: {e}")
        qg = {}

    try:
        sm = json.loads(sum_json) if sum_json else {}
    except Exception as e:
        print(f"  sum parse fail: {e}")
        sm = {}

    # V5-PR-1 关键字段
    keys_to_probe = [
        "multiple_testing_mode",
        "multiple_testing_inject_status",
        "multiple_testing_inject_error",
        "deflated_sharpe_ratio",
        "deflated_sharpe_reference_sharpe",
        "deflated_sharpe_effective_trials",
        "raw_sharpe_proxy",
        "deflated_sharpe_proxy",
        "pbo_proxy",
        "reality_check_pvalue_proxy",
        "spa_pvalue_proxy",
        "run_correction_mode",
        "cohort_effective_trials",
    ]

    print(f"  --- in quality_gate ---")
    for k in keys_to_probe:
        if k in qg:
            print(f"  qg.{k}: {qg[k]!r}")

    # 嵌套 multiple_testing 字段
    mt = qg.get("multiple_testing")
    if isinstance(mt, dict):
        print(f"  qg.multiple_testing keys: {list(mt.keys())}")
        # 重点:deflated_sharpe sub-dict
        ds = mt.get("deflated_sharpe")
        if isinstance(ds, dict):
            print(f"  qg.multiple_testing.deflated_sharpe: {ds}")
        # proxy 字段
        for k in ["deflated_sharpe_proxy", "pbo_proxy", "reality_check_pvalue_proxy", "spa_pvalue_proxy", "deflated_sharpe_ratio", "pbo_value"]:
            if k in mt:
                print(f"  qg.multiple_testing.{k}: {mt[k]!r}")

    # summary 中也可能有
    print(f"  --- in summary ---")
    for k in keys_to_probe:
        if k in sm:
            print(f"  sm.{k}: {sm[k]!r}")

    # multiple_testing_registry 中的字段
    mtr = sm.get("multiple_testing_registry") or qg.get("multiple_testing_registry") or {}
    if isinstance(mtr, dict):
        print(f"  --- in multiple_testing_registry ---")
        for k in keys_to_probe:
            if k in mtr:
                print(f"  mtr.{k}: {mtr[k]!r}")

con.close()
print("\n[chk] DONE")
