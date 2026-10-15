#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""DEV-V1 一站式运维脚本 — 策略工厂到孵化工厂过渡架构.

提供 DEV-V1 灰度阶段所需的全部运维操作:
  - status        查孵化工厂当前状态(P0/P1 toggle、paper 账户、quality_reports)
  - verify        端到端验证 P0/P1 toggle 链路(不修改 DB)
  - cycle         跑一次 strategy_factory cycle (产新候选)
  - intake        跑一次 IncubationFactoryRunner (消费 paper 候选)
  - full          完整流程: cycle + intake (一站式)
  - rollback      回滚 .env 中的 DEV-V1 toggle
  - check-toggles 检查 .env 中 toggle 是否正确

用法:
    python run_dev_v1_ops.py status              # 查状态
    python run_dev_v1_ops.py verify              # 端到端逻辑验证
    python run_dev_v1_ops.py cycle               # 跑策略工厂(产 quality_reports)
    python run_dev_v1_ops.py intake              # 跑孵化工厂(消费 paper 账户)
    python run_dev_v1_ops.py full                # cycle + intake
    python run_dev_v1_ops.py rollback            # 回滚到 .env.pre_dev_v1.bak
    python run_dev_v1_ops.py check-toggles       # 检查 toggle 配置
    python run_dev_v1_ops.py --help              # 完整说明

关联文档:
  - docs/ops/DEV-V1-运维手册.md
  - 策略工厂到孵化工厂过渡-开发方案-2026-05-26.md
  - 策略工厂到孵化工厂过渡架构方案-2026-05-26.md
  - data/reports/sf_dev_v1_decision_log.md
"""

from __future__ import annotations

import argparse
import asyncio
import io
import json
import os
import sqlite3
import subprocess
import sys
import time
from collections import Counter
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from _integration_paths import default_sqlite_path, ensure_runtime_dirs, runtime_home
import logging

logger = logging.getLogger(__name__)

ensure_runtime_dirs()
DB_PATH = default_sqlite_path()


def _configure_stdio_utf8() -> None:
    """Force stdout/stderr to UTF-8 (与 run_strategy_factory.py 一致)."""
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    if sys.platform == "win32":
        try:
            import ctypes
            kernel32 = ctypes.windll.kernel32
            kernel32.SetConsoleCP(65001)
            kernel32.SetConsoleOutputCP(65001)
        except Exception as _swallowed:
            logger.warning("scripts/ops/run_dev_v1_ops.py:67: swallowed %s and continued: %s", type(_swallowed).__name__, _swallowed)
            pass
    for name in ("stdout", "stderr"):
        s = getattr(sys, name, None)
        if s is None:
            continue
        try:
            s.reconfigure(encoding="utf-8", errors="replace")
            continue
        except Exception as _swallowed:
            logger.warning("scripts/ops/run_dev_v1_ops.py:76: swallowed %s and continued: %s", type(_swallowed).__name__, _swallowed)
            pass
        buf = getattr(s, "buffer", None)
        if buf is not None:
            try:
                setattr(sys, name, io.TextIOWrapper(buf, encoding="utf-8", errors="replace", line_buffering=True))
            except Exception as _swallowed:
                logger.warning("scripts/ops/run_dev_v1_ops.py:82: swallowed %s and continued: %s", type(_swallowed).__name__, _swallowed)
                continue


_configure_stdio_utf8()


# ───────────────────────────────────────────────────────
# 共用工具函数
# ───────────────────────────────────────────────────────

def _load_dotenv() -> None:
    """从 .env 加载环境变量(setdefault 模式)."""
    env_path = ROOT / ".env"
    if not env_path.exists():
        return
    text = env_path.read_text(encoding="utf-8", errors="ignore")
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key, val = key.strip(), val.strip().strip('"').strip("'")
        if key:
            os.environ.setdefault(key, val)


def _ensure_pkg_paths() -> None:
    """把 monorepo 包加入 PYTHONPATH."""
    for sub in (
        "packages/aiask-quant-core/src",
        "packages/strategy-factory/src",
        "packages/akshare-mcp/src",
    ):
        p = str(ROOT / sub)
        if p not in sys.path:
            sys.path.insert(0, p)


def _box(title: str) -> None:
    bar = "═" * 70
    print(f"\n{bar}\n  {title}\n{bar}", flush=True)


def _kv(k: str, v) -> None:
    print(f"  {k}: {v}", flush=True)


# ───────────────────────────────────────────────────────
# 子命令: status — 查孵化工厂状态
# ───────────────────────────────────────────────────────

def cmd_status(args: argparse.Namespace) -> int:
    _box(f"DEV-V1 状态总览 — {datetime.now():%Y-%m-%d %H:%M:%S}")
    _load_dotenv()

    # 1. .env toggle 状态
    print("\n[1] .env Toggle 状态")
    print("-" * 70)
    toggles = {
        "P0 (D 级解封)": "STRATEGY_FACTORY_OBSERVE_D_GRADE_ENABLED",
        "P1 (paper intake)": "INCUBATION_FACTORY_PAPER_INTAKE_ENABLED",
        "P1 batch limit": "INCUBATION_FACTORY_PAPER_INTAKE_BATCH_LIMIT",
        "P3 (extra families)": "STRATEGY_FACTORY_TRADE_AWARE_EXTRA_FAMILIES",
        "LLM 并发": "STRATEGY_LLM_MAX_CONCURRENCY",
        "Research 并发": "STRATEGY_FACTORY_RESEARCH_TASK_CONCURRENCY",
        "每任务候选": "STRATEGY_FACTORY_CANDIDATES_PER_TASK",
        "LLM fan-out": "STRATEGY_FACTORY_LLM_FAN_OUT_COUNT",
        "Monolithic fallback": "STRATEGY_FACTORY_ALLOW_PIPELINE_EMPTY_MONOLITHIC_FALLBACK",
    }
    for label, key in toggles.items():
        val = os.environ.get(key, "<未设置>")
        marker = ""
        if key == "STRATEGY_FACTORY_OBSERVE_D_GRADE_ENABLED" and val == "1":
            marker = " ← P0 ON"
        elif key == "INCUBATION_FACTORY_PAPER_INTAKE_ENABLED" and val == "1":
            marker = " ← P1 ON"
        elif key == "STRATEGY_LLM_MAX_CONCURRENCY" and val and int(val) >= 5:
            marker = " ← 已加速"
        print(f"  {label:<30} {key} = {val!r}{marker}")

    # 2. DB 状态
    if not DB_PATH.exists():
        print(f"\n[2] DB 状态: ❌ 数据库不存在 ({DB_PATH})")
        return 1

    con = sqlite3.connect(str(DB_PATH))
    cur = con.cursor()

    # 2.1 strategies
    print("\n[2] strategies 表 status 分布")
    print("-" * 70)
    cur.execute("SELECT status, COUNT(*) FROM strategies GROUP BY status ORDER BY COUNT(*) DESC")
    for status, cnt in cur.fetchall():
        print(f"  {status!r:<25} {cnt}")

    # 2.2 strategy_incubation_accounts
    print("\n[3] 孵化账户(stage 分布)")
    print("-" * 70)
    try:
        cur.execute(
            "SELECT stage, COUNT(*) FROM strategy_incubation_accounts "
            "WHERE status='active' GROUP BY stage ORDER BY stage"
        )
        rows = cur.fetchall()
        if not rows:
            print("  (无 active 账户)")
        else:
            for stage, cnt in rows:
                marker = ""
                if stage == "paper":
                    marker = " ← P0 解封产物 ★"
                elif stage == "warmup":
                    marker = " ← 热身阶段(可能含已升级 paper)"
                elif stage == "candidate":
                    marker = " ← 正式候选"
                elif stage == "listed":
                    marker = " ← 已上线"
                print(f"  stage='{stage}'    count={cnt}{marker}")
    except Exception as exc:
        print(f"  错误: {exc}")

    # 2.3 quality_reports lane
    print("\n[4] quality_reports submission_lane 分布(P0 触发证据)")
    print("-" * 70)
    cur.execute("SELECT id, summary FROM strategy_quality_reports ORDER BY id")
    lanes: Counter = Counter()
    for rid, sj in cur.fetchall():
        try:
            sm = json.loads(sj) if sj else {}
        except Exception as _swallowed:
            logger.warning("scripts/ops/run_dev_v1_ops.py:212: swallowed %s and continued: %s", type(_swallowed).__name__, _swallowed)
            continue
        lane = str(sm.get("submission_lane") or "")
        lanes[lane] += 1
    for lane, cnt in lanes.most_common():
        marker = ""
        if lane == "observe_incubation":
            marker = " ★ P0 路径"
        elif lane == "deferred_submission":
            marker = " ← 老逻辑 D 级"
        print(f"  {lane!r:<30} {cnt}{marker}")

    # 2.4 P1 事件
    print("\n[5] strategy_domain_events 中 incubation* 事件(P1 触发证据)")
    print("-" * 70)
    try:
        cur.execute(
            "SELECT event_type, COUNT(*) FROM strategy_domain_events "
            "WHERE event_type LIKE 'incubation%' GROUP BY event_type "
            "ORDER BY COUNT(*) DESC"
        )
        for etype, cnt in cur.fetchall():
            marker = ""
            if etype == "incubation_factory.paper_observation_recognized":
                marker = " ★ P1 路径"
            print(f"  {etype:<70} {cnt}{marker}")
    except Exception as exc:
        print(f"  错误: {exc}")

    # 2.5 备份文件
    print("\n[6] 备份文件")
    print("-" * 70)
    for bak in [".env.pre_dev_v1.bak", ".env.pre_density_boost.bak"]:
        p = ROOT / bak
        if p.exists():
            size_kb = p.stat().st_size / 1024
            print(f"  ✅ {bak}  ({size_kb:.1f} KB)")
        else:
            print(f"  ⚠️ {bak}  (不存在)")

    db_bak = ROOT / "data" / "db" / "akshare_mcp.pre_dev_v1.bak"
    if db_bak.exists():
        size_gb = db_bak.stat().st_size / 1024 / 1024 / 1024
        print(f"  ✅ data/db/akshare_mcp.pre_dev_v1.bak  ({size_gb:.2f} GB)")
    else:
        print(f"  ⚠️ data/db/akshare_mcp.pre_dev_v1.bak  (不存在)")

    con.close()
    print("\n" + "═" * 70)
    return 0


# ───────────────────────────────────────────────────────
# 子命令: verify — 端到端逻辑验证(不跑 cycle,纯逻辑)
# ───────────────────────────────────────────────────────

async def _verify_async() -> int:
    _ensure_pkg_paths()
    _load_dotenv()

    # 1. toggle 解析
    print("\n[1] Toggle 解析(从 .env)")
    print("-" * 70)
    from strategy_factory.application._runtime_toggles import (
        observe_d_grade_enabled, paper_intake_enabled, paper_intake_batch_limit,
    )
    p0 = observe_d_grade_enabled()
    p1 = paper_intake_enabled()
    limit = paper_intake_batch_limit()
    print(f"  observe_d_grade_enabled():       {p0}")
    print(f"  paper_intake_enabled():          {p1}")
    print(f"  paper_intake_batch_limit():      {limit}")
    step1 = p0 and p1

    # 2. P0 路径(toggle ON)
    print("\n[2] P0 路径(D 级 + Gate-passed 候选 ON 时)")
    print("-" * 70)
    from strategy_factory.application.submitter import StrategySubmitter
    mock_strategy = {
        "id": "ops_verify_001", "strategy_type": "momentum", "strategy_type_registered": True,
        "execution_semantic_match": True, "semantic_runtime_match": True,
        "runtime_family_data_source": "tdx", "runtime_playbook": {"version": "1.0"},
        "execution_readiness_tier": "tier_a",
    }
    mock_gate = {"passed": True, "validation_grade": "D", "post_cost_sharpe": 0.55}
    ctx = StrategySubmitter._runtime_bootstrap_context(mock_gate, candidate=mock_strategy)
    eligible = ctx.get("runtime_bootstrap_eligible")
    reason = ctx.get("runtime_bootstrap_reason")
    tier = ctx.get("runtime_bootstrap_budget_tier")
    print(f"  eligible:    {eligible} (期望 True)")
    print(f"  reason:      {reason!r} (期望 'd_grade_observe_only_micro_budget')")
    print(f"  budget_tier: {tier!r} (期望 'micro')")
    step2 = (eligible is True and reason == "d_grade_observe_only_micro_budget" and tier == "micro")

    # 3. P0 路径(toggle OFF 对照)
    print("\n[3] P0 路径(toggle OFF 对照,验证老行为)")
    print("-" * 70)
    old_p0 = os.environ.get("STRATEGY_FACTORY_OBSERVE_D_GRADE_ENABLED")
    os.environ["STRATEGY_FACTORY_OBSERVE_D_GRADE_ENABLED"] = "0"
    try:
        ctx_off = StrategySubmitter._runtime_bootstrap_context(mock_gate, candidate=mock_strategy)
        eligible_off = ctx_off.get("runtime_bootstrap_eligible")
        reason_off = ctx_off.get("runtime_bootstrap_reason")
        print(f"  eligible(off):    {eligible_off} (期望 False)")
        print(f"  reason(off):      {reason_off!r} (期望 'validation_grade_d_not_allowed_for_runtime')")
        step3 = (eligible_off is False and reason_off == "validation_grade_d_not_allowed_for_runtime")
    finally:
        if old_p0 is not None:
            os.environ["STRATEGY_FACTORY_OBSERVE_D_GRADE_ENABLED"] = old_p0
        else:
            os.environ.pop("STRATEGY_FACTORY_OBSERVE_D_GRADE_ENABLED", None)

    # 4. P1 intake 路径
    print("\n[4] P1 路径(IncubationIntake._list_paper_observation_strategies)")
    print("-" * 70)
    from akshare_mcp.services.incubation_factory.intake import IncubationIntake
    intake = IncubationIntake()

    class _MockDB:
        async def list_paper_observation_strategies(self, limit=50):
            return [{"id": "fake", "strategy_id": "fake", "name": "Mock"}]

    class _MockDBNoMethod: ...

    res_off = await intake._list_paper_observation_strategies(_MockDBNoMethod())
    res_on_no_method = await intake._list_paper_observation_strategies(_MockDBNoMethod())
    res_on_with_method = await intake._list_paper_observation_strategies(_MockDB())
    print(f"  ON + 无方法:  返回 {len(res_on_no_method)} 条 (期望 0,降级)")
    print(f"  ON + 有方法:  返回 {len(res_on_with_method)} 条 (期望 1)")
    step4 = (len(res_on_no_method) == 0 and len(res_on_with_method) == 1)

    # 5. P1 runner 路径
    print("\n[5] P1 路径(IncubationFactoryRunner._list_paper_observation)")
    print("-" * 70)
    from akshare_mcp.services.incubation_factory.runner import IncubationFactoryRunner
    runner = IncubationFactoryRunner(dry_run=True)
    res_runner = await runner._list_paper_observation(_MockDB())
    print(f"  runner._list_paper_observation: 返回 {len(res_runner)} 条 (期望 1)")
    step5 = (len(res_runner) == 1)

    print("\n[总判定]")
    print("-" * 70)
    print(f"  ① toggle 解析:        {'✅' if step1 else '❌'}")
    print(f"  ② P0 路径 ON:         {'✅' if step2 else '❌'}")
    print(f"  ③ P0 路径 OFF 对照:   {'✅' if step3 else '❌'}")
    print(f"  ④ P1 intake 路径:     {'✅' if step4 else '❌'}")
    print(f"  ⑤ P1 runner 路径:     {'✅' if step5 else '❌'}")

    all_pass = all([step1, step2, step3, step4, step5])
    if all_pass:
        print("\n  🎉 端到端 5 步验证全部 PASS")
        print("     P0/P1 toggle 链路完整,只待生产 cycle 自然触发")
        return 0
    else:
        print("\n  ❌ 部分步骤失败,需排查")
        return 1


def cmd_verify(args: argparse.Namespace) -> int:
    _box("DEV-V1 端到端逻辑验证")
    return asyncio.run(_verify_async())


# ───────────────────────────────────────────────────────
# 子命令: cycle — 跑 strategy_factory cycle
# ───────────────────────────────────────────────────────

def cmd_cycle(args: argparse.Namespace) -> int:
    _box(f"运行 strategy_factory cycle (timeout {args.timeout}s)")
    log_dir = ROOT / "data" / "logs" / "dev_v1_ops"
    log_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_file = log_dir / f"cycle_{ts}.log"
    err_file = log_dir / f"cycle_{ts}.err.log"

    print(f"\n  log:  {log_file}")
    print(f"  err:  {err_file}")
    print(f"  开始: {datetime.now():%Y-%m-%d %H:%M:%S}\n")

    start = time.monotonic()
    with open(log_file, "wb") as fout, open(err_file, "wb") as ferr:
        proc = subprocess.Popen(
            [
                sys.executable,
                "-X",
                "utf8",
                "-u",
                str(ROOT / "scripts" / "factories" / "run_strategy_factory.py"),
                "--once",
            ],
            cwd=str(ROOT), stdout=fout, stderr=ferr,
        )
        print(f"  PID: {proc.pid}", flush=True)
        last_size = 0
        while True:
            elapsed = time.monotonic() - start
            rc = proc.poll()
            if rc is not None:
                print(f"\n  cycle 退出 elapsed={elapsed:.0f}s rc={rc}", flush=True)
                break
            if elapsed > args.timeout:
                print(f"\n  超时 {args.timeout}s,terminate", flush=True)
                proc.terminate()
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
                break
            try:
                sz = err_file.stat().st_size
            except FileNotFoundError:
                sz = 0
            if sz != last_size or int(elapsed) % 60 == 0:
                print(f"    [{elapsed:>4.0f}s] err.log = {sz} B (Δ {sz - last_size:+d})", flush=True)
                last_size = sz
            time.sleep(30)

    print(f"\n  完成: {datetime.now():%Y-%m-%d %H:%M:%S}")
    return 0 if proc.returncode == 0 else 1


# ───────────────────────────────────────────────────────
# 子命令: intake — 跑 IncubationFactoryRunner.run_once()
# ───────────────────────────────────────────────────────

async def _intake_async(timeout: int) -> int:
    _ensure_pkg_paths()
    _load_dotenv()

    print("\n[1] Toggle 状态")
    from akshare_mcp.config._strategy_factory_toggles import (
        paper_intake_enabled, paper_intake_batch_limit,
    )
    print(f"  paper_intake_enabled:    {paper_intake_enabled()}")
    print(f"  paper_intake_batch_limit: {paper_intake_batch_limit()}")

    # pre 快照
    con = sqlite3.connect(str(DB_PATH))
    cur = con.cursor()
    cur.execute(
        "SELECT COUNT(*) FROM strategy_incubation_accounts WHERE status='active' AND stage='paper'"
    )
    pre_paper = cur.fetchone()[0]
    cur.execute(
        "SELECT COUNT(*) FROM strategy_domain_events "
        "WHERE event_type='incubation_factory.paper_observation_recognized'"
    )
    pre_event = cur.fetchone()[0]
    cur.execute("SELECT MAX(id) FROM strategy_domain_events")
    pre_max_eid = cur.fetchone()[0] or 0
    con.close()

    print(f"\n[2] Pre snapshot")
    print(f"  paper accounts:                                    {pre_paper}")
    print(f"  incubation_factory.paper_observation_recognized:   {pre_event}")

    # 跑
    from akshare_mcp.services.incubation_factory import IncubationFactoryRunner
    runner = IncubationFactoryRunner(dry_run=False, auto_apply_review=True)
    print(f"\n[3] 启动 IncubationFactoryRunner.run_once()")
    start = time.monotonic()
    try:
        result = await asyncio.wait_for(runner.run_once(), timeout=timeout)
        elapsed = time.monotonic() - start
        print(f"\n[4] run_once() 完成 elapsed={elapsed:.2f}s")
        print(f"  status:   {result.get('status')}")
        print(f"  run_id:   {result.get('run_id')}")
        print(f"  failures: {result.get('phase_failures', [])}")

        intake = result.get("intake") or {}
        poi = intake.get("paper_observation_intake") or {}
        print(f"\n  [Phase 1: Intake]")
        print(f"    scanned:      {intake.get('scanned')}")
        print(f"    accepted:     {intake.get('accepted')}")
        print(f"    paper.scanned:    {poi.get('scanned')}")
        print(f"    paper.recognized: {poi.get('recognized')} ★")
        print(f"    paper.strategy_ids: {poi.get('strategy_ids')}")

        verification = result.get("verification") or {}
        print(f"\n  [Phase 2+3: Verification]")
        for k in ("total", "incubating_count", "paper_count", "metrics_recorded",
                  "verification_errors", "signals_generated_total"):
            if k in verification:
                print(f"    {k}: {verification[k]}")
    except asyncio.TimeoutError:
        elapsed = time.monotonic() - start
        print(f"\n  ❌ 超时 elapsed={elapsed:.0f}s")
        return 2
    except Exception as exc:
        elapsed = time.monotonic() - start
        print(f"\n  ❌ 异常 elapsed={elapsed:.0f}s: {type(exc).__name__}: {exc}")
        return 3

    # post 快照
    con = sqlite3.connect(str(DB_PATH))
    cur = con.cursor()
    cur.execute(
        "SELECT COUNT(*) FROM strategy_incubation_accounts WHERE status='active' AND stage='paper'"
    )
    post_paper = cur.fetchone()[0]
    cur.execute(
        "SELECT COUNT(*) FROM strategy_domain_events "
        "WHERE event_type='incubation_factory.paper_observation_recognized'"
    )
    post_event = cur.fetchone()[0]

    print(f"\n[5] Post snapshot Δ")
    print(f"  paper accounts:                                  {pre_paper} → {post_paper} (Δ {post_paper - pre_paper:+d})")
    print(f"  paper_observation_recognized 事件:               {pre_event} → {post_event} (Δ {post_event - pre_event:+d})")

    # 新事件明细
    if post_event > pre_event:
        cur.execute(
            "SELECT id, strategy_id, payload, created_at FROM strategy_domain_events "
            "WHERE event_type='incubation_factory.paper_observation_recognized' AND id > ? "
            "ORDER BY id",
            (pre_max_eid,)
        )
        print(f"\n  ★ 新增 paper_observation_recognized 事件:")
        for eid, sid, pl_json, ts in cur.fetchall():
            print(f"    event {eid} | strategy={sid} | {ts}")
            try:
                pl = json.loads(pl_json) if pl_json else {}
                for k, v in pl.items():
                    print(f"           {k}: {v}")
            except Exception as _swallowed:
                logger.warning("scripts/ops/run_dev_v1_ops.py:538: swallowed %s and continued: %s", type(_swallowed).__name__, _swallowed)
                pass
    con.close()

    delta = post_event - pre_event
    if delta > 0:
        print(f"\n  🎉 P1 完整链路触发 — paper_observation_recognized +{delta}")
        return 0
    elif pre_paper == 0:
        print(f"\n  🟡 当前无 paper 账户(P0 未触发或已被 IncubationFactoryRunner 推进)")
        print(f"     先跑 'cycle' 命令产生新 paper 账户,再跑 'intake'")
        return 0
    else:
        print(f"\n  ⚠️ paper 账户存在但事件未增加,需排查")
        return 4


def cmd_intake(args: argparse.Namespace) -> int:
    _box(f"运行 IncubationFactoryRunner.run_once() (timeout {args.timeout}s)")
    return asyncio.run(_intake_async(args.timeout))


# ───────────────────────────────────────────────────────
# 子命令: full — cycle + intake 一站式
# ───────────────────────────────────────────────────────

def cmd_full(args: argparse.Namespace) -> int:
    _box("DEV-V1 完整流程 (cycle + intake)")
    print("\n步骤 1/2: 运行 strategy_factory cycle")
    rc1 = cmd_cycle(args)
    if rc1 != 0:
        print(f"\n  ⚠️ cycle 退出码非 0 (rc={rc1}),仍继续跑 intake")

    print("\n步骤 2/2: 运行 IncubationFactoryRunner")
    rc2 = cmd_intake(args)
    if rc2 == 0 and rc1 == 0:
        print("\n  🎉 完整流程 SUCCESS")
        return 0
    return rc2 or rc1


# ───────────────────────────────────────────────────────
# 子命令: rollback — 回滚 .env toggle
# ───────────────────────────────────────────────────────

def cmd_rollback(args: argparse.Namespace) -> int:
    _box("DEV-V1 回滚")
    print("\n选项:")
    print("  1. .env 完整恢复(回滚 toggle 到 DEV-V1 落地前状态)")
    print("  2. DB 完整恢复(回滚 quality_reports / accounts 到 2026-05-26 12:00)")
    print()

    target = args.target

    if target in ("env", "all"):
        env_bak = ROOT / ".env.pre_dev_v1.bak"
        if not env_bak.exists():
            print(f"  ❌ {env_bak} 不存在,无法回滚 .env")
            return 1
        env_path = ROOT / ".env"
        # 二次备份当前 .env 以防误操作
        emergency = ROOT / f".env.before_rollback_{datetime.now():%Y%m%d_%H%M%S}.bak"
        env_path.replace(emergency)
        env_bak_content = env_bak.read_bytes()
        env_path.write_bytes(env_bak_content)
        print(f"  ✅ .env 已恢复(原 .env 备份到 {emergency.name})")

    if target in ("db", "all"):
        db_bak = runtime_home() / "data" / "db" / "akshare_mcp.pre_dev_v1.bak"
        if not db_bak.exists():
            print(f"  ❌ {db_bak} 不存在,无法回滚 DB")
            return 1
        db_path = default_sqlite_path()
        if not args.force:
            print(f"  ⚠️ DB 回滚是不可逆操作!当前 DB 大小 = {db_path.stat().st_size / 1024 / 1024 / 1024:.2f} GB")
            print(f"     请使用 --force 确认: python run_dev_v1_ops.py rollback --target db --force")
            return 2
        emergency_db = runtime_home() / "data" / "db" / f"akshare_mcp.before_rollback_{datetime.now():%Y%m%d_%H%M%S}.bak"
        db_path.replace(emergency_db)
        # copy
        emergency_db_size = emergency_db.stat().st_size / 1024 / 1024 / 1024
        print(f"  📁 当前 DB 备份到 {emergency_db.name} ({emergency_db_size:.2f} GB)")
        # 用 buffered copy,避免 read 整个文件到内存
        with db_bak.open("rb") as src, db_path.open("wb") as dst:
            for chunk in iter(lambda: src.read(64 * 1024 * 1024), b""):
                dst.write(chunk)
        print(f"  ✅ DB 已恢复")

    print("\n  回滚完成。下一步:")
    print("  - 重启长期运行的策略工厂/孵化工厂进程,让新 .env 生效")
    print("  - 跑 'python run_dev_v1_ops.py status' 确认状态")
    return 0


# ───────────────────────────────────────────────────────
# 子命令: check-toggles
# ───────────────────────────────────────────────────────

def cmd_check_toggles(args: argparse.Namespace) -> int:
    _box("检查 .env Toggle 配置")
    _load_dotenv()

    expected = {
        "STRATEGY_FACTORY_OBSERVE_D_GRADE_ENABLED": "1",
        "INCUBATION_FACTORY_PAPER_INTAKE_ENABLED": "1",
        "INCUBATION_FACTORY_PAPER_INTAKE_BATCH_LIMIT": "10",
        "STRATEGY_LLM_MAX_CONCURRENCY": "5",
        "STRATEGY_FACTORY_RESEARCH_TASK_CONCURRENCY": "5",
        "STRATEGY_FACTORY_CANDIDATES_PER_TASK": "6",
        "STRATEGY_FACTORY_LLM_FAN_OUT_COUNT": "3",
        "STRATEGY_FACTORY_ALLOW_PIPELINE_EMPTY_MONOLITHIC_FALLBACK": "0",
    }

    all_ok = True
    print()
    for key, expected_val in expected.items():
        actual = os.environ.get(key, "<未设置>")
        ok = (actual == expected_val)
        marker = "✅" if ok else "⚠️"
        print(f"  {marker} {key:<55} expect={expected_val:<5} actual={actual}")
        if not ok:
            all_ok = False

    if all_ok:
        print("\n  🎉 所有 toggle 配置正确")
        return 0
    else:
        print("\n  ⚠️ 部分 toggle 配置不符合 DEV-V1 推荐值")
        print("     参考: docs/ops/DEV-V1-运维手册.md 第 §3 节")
        return 1


# ───────────────────────────────────────────────────────
# CLI 入口
# ───────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(
        prog="run_dev_v1_ops.py",
        description="DEV-V1 一站式运维脚本(策略工厂到孵化工厂过渡)",
        epilog="详见: docs/ops/DEV-V1-运维手册.md",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="cmd", required=True, help="子命令")

    p_status = sub.add_parser("status", help="查 DEV-V1 状态总览")
    p_status.set_defaults(func=cmd_status)

    p_verify = sub.add_parser("verify", help="端到端逻辑验证(5 步,不跑 cycle)")
    p_verify.set_defaults(func=cmd_verify)

    p_cycle = sub.add_parser("cycle", help="跑 strategy_factory cycle(产新候选)")
    p_cycle.add_argument("--timeout", type=int, default=1500, help="cycle 超时秒数(默认 1500=25 分钟)")
    p_cycle.set_defaults(func=cmd_cycle)

    p_intake = sub.add_parser("intake", help="跑 IncubationFactoryRunner.run_once(消费 paper)")
    p_intake.add_argument("--timeout", type=int, default=300, help="intake 超时秒数(默认 300=5 分钟)")
    p_intake.set_defaults(func=cmd_intake)

    p_full = sub.add_parser("full", help="一站式: cycle + intake")
    p_full.add_argument("--timeout", type=int, default=1500, help="cycle 超时秒数(默认 1500=25 分钟)")
    p_full.set_defaults(func=cmd_full)

    p_rollback = sub.add_parser("rollback", help="回滚 .env 或 DB 到 DEV-V1 落地前状态")
    p_rollback.add_argument("--target", choices=["env", "db", "all"], default="env",
                            help="回滚目标: env(只 .env)/db(只 DB)/all(都回滚)")
    p_rollback.add_argument("--force", action="store_true", help="DB 回滚需要 --force 确认")
    p_rollback.set_defaults(func=cmd_rollback)

    p_check = sub.add_parser("check-toggles", help="检查 .env toggle 是否符合 DEV-V1 推荐值")
    p_check.set_defaults(func=cmd_check_toggles)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
