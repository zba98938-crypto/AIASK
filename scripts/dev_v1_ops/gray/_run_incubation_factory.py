#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""直接触发 IncubationFactoryRunner.run_once() 验证 P1 完整链路.

发现:strategy_factory cycle (run_strategy_factory.py) 不包含 IncubationFactoryRunner!
两者是分离的服务:
  - run_strategy_factory.py → 策略生成 (Gate 0/1/2/3 → 写 quality_reports)
  - IncubationFactoryRunner → 孵化处理 (Phase 1 intake + Phase 2 加载 + Phase 3 信号)

P1 toggle ON 后,需要单独触发 IncubationFactoryRunner 才能消费 paper 账户。
"""
from __future__ import annotations
import asyncio
import json
import os
import sqlite3
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

# === 加载 .env ===
env_path = ROOT / ".env"
for line in env_path.read_text(encoding="utf-8", errors="ignore").splitlines():
    line = line.strip()
    if not line or line.startswith("#") or "=" not in line:
        continue
    key, _, val = line.partition("=")
    key, val = key.strip(), val.strip().strip('"').strip("'")
    if key:
        os.environ.setdefault(key, val)

sys.path.insert(0, str(ROOT / "packages" / "strategy-factory" / "src"))
sys.path.insert(0, str(ROOT / "packages" / "akshare-mcp" / "src"))
sys.path.insert(0, str(ROOT / "packages" / "aiask-quant-core" / "src"))


def db_paper_snapshot() -> dict:
    con = sqlite3.connect(DB)
    cur = con.cursor()
    snap = {}
    cur.execute(
        "SELECT account_id, strategy_id, stage, status, bound_at "
        "FROM strategy_incubation_accounts WHERE status='active' AND stage='paper'"
    )
    snap["paper_accounts"] = [
        {"account_id": r[0], "strategy_id": r[1], "stage": r[2], "status": r[3], "bound_at": r[4]}
        for r in cur.fetchall()
    ]
    snap["paper_count"] = len(snap["paper_accounts"])

    cur.execute(
        "SELECT event_type, COUNT(*) FROM strategy_domain_events "
        "WHERE event_type LIKE 'incubation%' GROUP BY event_type"
    )
    snap["events"] = dict(cur.fetchall())

    cur.execute("SELECT MAX(id) FROM strategy_domain_events")
    snap["max_event_id"] = cur.fetchone()[0] or 0

    con.close()
    return snap


async def main():
    print(f"=== 触发 IncubationFactoryRunner.run_once() — P1 路径完整验证 ===")
    print(f"启动时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

    # === 验证 toggle 状态 ===
    print(f"\n=== Toggle 状态 ===")
    from akshare_mcp.config._strategy_factory_toggles import (
        paper_intake_enabled,
        paper_intake_batch_limit,
    )
    print(f"  paper_intake_enabled: {paper_intake_enabled()}")
    print(f"  paper_intake_batch_limit: {paper_intake_batch_limit()}")

    # === pre snapshot ===
    print(f"\n=== Pre snapshot ===")
    pre = db_paper_snapshot()
    print(f"  paper accounts: {pre['paper_count']}")
    for acc in pre["paper_accounts"]:
        print(f"    {acc}")
    print(f"  incubation* events:")
    for k, v in pre["events"].items():
        print(f"    {k}: {v}")

    # === 直接调 IncubationFactoryRunner.run_once() ===
    print(f"\n=== 启动 IncubationFactoryRunner.run_once() ===")
    from akshare_mcp.services.incubation_factory import IncubationFactoryRunner

    runner = IncubationFactoryRunner(dry_run=False, auto_apply_review=True)

    start = time.monotonic()
    try:
        result = await asyncio.wait_for(runner.run_once(), timeout=300)
        elapsed = time.monotonic() - start
        print(f"\n=== run_once() 完成 elapsed={elapsed:.1f}s ===")

        # 顶层 status
        print(f"  status: {result.get('status')}")
        print(f"  run_id: {result.get('run_id')}")
        print(f"  phase_failures: {result.get('phase_failures', [])}")

        # === Phase 1 (intake) ===
        intake = result.get("intake") or {}
        print(f"\n  [Phase 1: Intake]")
        print(f"    scanned: {intake.get('scanned')}")
        print(f"    accepted: {intake.get('accepted')}")
        print(f"    skipped: {intake.get('skipped')}")
        print(f"    errors: {intake.get('errors')}")
        if intake.get("paper_observation_intake"):
            poi = intake["paper_observation_intake"]
            print(f"    ★ paper_observation_intake.scanned: {poi.get('scanned')}")
            print(f"    ★ paper_observation_intake.recognized: {poi.get('recognized')}")
            print(f"    ★ paper_observation_intake.strategy_ids: {poi.get('strategy_ids')}")

        # === Phase 2 + 3 (verification) ===
        verification = result.get("verification") or {}
        print(f"\n  [Phase 2+3: Verification]")
        for k in ["total", "incubating_count", "paper_count", "metrics_recorded",
                  "verification_errors", "signals_generated_total"]:
            if k in verification:
                marker = " ★" if k == "paper_count" and verification[k] > 0 else ""
                print(f"    {k}: {verification[k]}{marker}")

        # === 打印完整结果 (debug) ===
        print(f"\n  [Full result keys]")
        for k in result.keys():
            v = result[k]
            if isinstance(v, dict):
                print(f"    {k}: <dict with {len(v)} keys: {list(v.keys())[:5]}>")
            elif isinstance(v, list):
                print(f"    {k}: <list len={len(v)}>")
            else:
                print(f"    {k}: {v}")
    except asyncio.TimeoutError:
        elapsed = time.monotonic() - start
        print(f"\n  ❌ run_once() 超时 elapsed={elapsed:.1f}s")
    except Exception as exc:
        elapsed = time.monotonic() - start
        print(f"\n  ❌ run_once() 异常 elapsed={elapsed:.1f}s: {type(exc).__name__}: {exc}")
        import traceback
        traceback.print_exc()

    # === post snapshot ===
    print(f"\n=== Post snapshot ===")
    post = db_paper_snapshot()
    print(f"  paper accounts: {post['paper_count']} (Δ {post['paper_count'] - pre['paper_count']:+d})")
    print(f"  incubation* events Δ:")
    for k in set(list(pre["events"].keys()) + list(post["events"].keys())):
        pv, qv = pre["events"].get(k, 0), post["events"].get(k, 0)
        delta = qv - pv
        marker = ""
        if "paper_observation_recognized" in k and delta > 0:
            marker = " ★★★"
        if delta > 0 or "paper_observation_recognized" in k:
            print(f"    {k}: {pv} → {qv} (Δ {delta:+d}){marker}")

    # === 新事件明细 ===
    if post["max_event_id"] > pre["max_event_id"]:
        con = sqlite3.connect(DB)
        cur = con.cursor()
        cur.execute(
            "SELECT id, event_type, strategy_id, payload, created_at "
            "FROM strategy_domain_events WHERE id > ? ORDER BY id",
            (pre["max_event_id"],)
        )
        print(f"\n  新事件 ({post['max_event_id'] - pre['max_event_id']} 条):")
        for eid, etype, sid, pl_json, ts in cur.fetchall():
            marker = " ★★★" if "paper_observation_recognized" in etype else ""
            print(f"    {eid} | {etype} | sid={sid} | {ts}{marker}")
            if marker:
                # 详细 payload
                try:
                    pl = json.loads(pl_json) if pl_json else {}
                    for k, v in pl.items():
                        print(f"         payload.{k}: {v}")
                except Exception as _swallowed:
                    logger.warning("scripts/dev_v1_ops/gray/_run_incubation_factory.py:201: swallowed %s and continued: %s", type(_swallowed).__name__, _swallowed)
                    pass
        con.close()

    # === 总判定 ===
    p1_recognized_delta = post["events"].get("incubation_factory.paper_observation_recognized", 0) - \
                          pre["events"].get("incubation_factory.paper_observation_recognized", 0)

    print(f"\n=== 总判定 ===")
    if p1_recognized_delta > 0:
        print(f"  🎉🎉🎉 P1 完整链路 PASS")
        print(f"     incubation_factory.paper_observation_recognized 事件: +{p1_recognized_delta}")
        print(f"     paper 账户被孵化工厂识别并写入领域事件")
    else:
        print(f"  ⚠️ P1 路径未触发")
        print(f"     paper_observation_recognized 事件 = {p1_recognized_delta}")

    print(f"\n=== DONE ===")


if __name__ == "__main__":
    asyncio.run(main())
