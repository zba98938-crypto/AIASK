#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""P0 + P1 端到端强制验证(兜底方案 v2).

目标:
  1. 验证 .env 中的 toggle 真的被解析(已 PASS)
  2. 验证 P0 路径:D 级 + Gate-passed → runtime_bootstrap_eligible=True
     (通过 StrategySubmitter._runtime_bootstrap_context 验证)
  3. 验证 P1 路径:IncubationIntake._list_paper_observation_strategies 在 toggle ON
     时会真的去 db 查询(toggle OFF 时直接返回空)
"""
from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime
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

# 加载 .env
env_path = ROOT / ".env"
for line in env_path.read_text(encoding="utf-8", errors="ignore").splitlines():
    line = line.strip()
    if not line or line.startswith("#") or "=" not in line:
        continue
    key, _, val = line.partition("=")
    key = key.strip()
    val = val.strip().strip('"').strip("'")
    if key:
        os.environ.setdefault(key, val)

sys.path.insert(0, str(ROOT / "packages" / "strategy-factory" / "src"))
sys.path.insert(0, str(ROOT / "packages" / "akshare-mcp" / "src"))
sys.path.insert(0, str(ROOT / "packages" / "aiask-quant-core" / "src"))

print(f"[e2e] === P0 + P1 强制端到端验证 v2 ===", flush=True)
print(f"[e2e] 时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}", flush=True)

# === 步骤 1:toggle 解析 ===
print(f"\n[e2e] 步骤 1: toggle 解析", flush=True)
try:
    from strategy_factory.application._runtime_toggles import (
        observe_d_grade_enabled,
        paper_intake_enabled,
        paper_intake_batch_limit,
    )
    p0_on = observe_d_grade_enabled()
    p1_on = paper_intake_enabled()
    p1_limit = paper_intake_batch_limit()
    print(f"  P0 (observe_d_grade_enabled): {p0_on}", flush=True)
    print(f"  P1 (paper_intake_enabled): {p1_on}", flush=True)
    print(f"  P1 batch limit: {p1_limit}", flush=True)
    step1_pass = p0_on and p1_on and p1_limit == 10
    print(f"  步骤 1: {'✅ PASS' if step1_pass else '❌ FAIL'}", flush=True)
except Exception as exc:
    print(f"  ❌ 步骤 1 失败: {exc}", flush=True)
    sys.exit(2)

# === 步骤 2:P0 路径(StrategySubmitter._runtime_bootstrap_context)===
print(f"\n[e2e] 步骤 2: P0 路径验证(StrategySubmitter._runtime_bootstrap_context)", flush=True)
try:
    from strategy_factory.application.submitter import StrategySubmitter

    mock_strategy = {
        "id": "e2e_test_d_gate_passed",
        "strategy_id": "e2e_test_d_gate_passed",
        "strategy_type": "momentum",
        "name": "E2E Test D+Pass",
        "params": {"window": 20},
        "candidate_contract_hash": "test_hash_001",
        "execution_contract_hash": "test_hash_001",
        "tested_object_hash": "test_hash_001_tested",
        "candidate_identity_signature": "test_sig_001",
        "strategy_type_registered": True,
        "execution_semantic_match": True,
        "execution_semantic_gap": False,
        "semantic_runtime_match": True,
        "runtime_family_data_source": "tdx",
        "runtime_playbook": {"version": "1.0"},
        "execution_readiness_tier": "tier_a",
    }

    mock_gate_d_passed = {
        "passed": True,
        "validation_grade": "D",
        "post_cost_sharpe": 0.55,
    }

    ctx = StrategySubmitter._runtime_bootstrap_context(
        mock_gate_d_passed, candidate=mock_strategy
    )
    print(f"  context 返回字段:", flush=True)
    keys = ["runtime_bootstrap_eligible", "runtime_bootstrap_reason",
            "runtime_bootstrap_budget_tier", "strategy_type_registered"]
    for k in keys:
        print(f"    {k} = {ctx.get(k)!r}", flush=True)

    eligible = ctx.get("runtime_bootstrap_eligible")
    reason = ctx.get("runtime_bootstrap_reason")
    budget_tier = ctx.get("runtime_bootstrap_budget_tier")

    p0_path_pass = (
        eligible is True
        and reason == "d_grade_observe_only_micro_budget"
        and budget_tier == "micro"
    )
    print(f"  步骤 2 (P0 路径): {'✅ PASS' if p0_path_pass else '❌ FAIL'}", flush=True)
    if not p0_path_pass:
        print(f"    期望: eligible=True, reason='d_grade_observe_only_micro_budget', budget='micro'", flush=True)
except Exception as exc:
    print(f"  ❌ 步骤 2 失败: {exc}", flush=True)
    import traceback
    traceback.print_exc()
    p0_path_pass = False

# === 步骤 3:对照 toggle OFF 时 D 级行为 ===
print(f"\n[e2e] 步骤 3: 对照测试(临时关 P0 toggle 看老行为)", flush=True)
old_p0 = os.environ.get("STRATEGY_FACTORY_OBSERVE_D_GRADE_ENABLED")
os.environ["STRATEGY_FACTORY_OBSERVE_D_GRADE_ENABLED"] = "0"
try:
    ctx_off = StrategySubmitter._runtime_bootstrap_context(
        mock_gate_d_passed, candidate=mock_strategy
    )
    eligible_off = ctx_off.get("runtime_bootstrap_eligible")
    reason_off = ctx_off.get("runtime_bootstrap_reason")
    print(f"  toggle OFF: eligible={eligible_off}, reason={reason_off!r}", flush=True)
    contrast_pass = (
        eligible_off is False
        and reason_off == "validation_grade_d_not_allowed_for_runtime"
    )
    print(f"  步骤 3 (对照): {'✅ PASS' if contrast_pass else '❌ FAIL'}", flush=True)
finally:
    if old_p0 is not None:
        os.environ["STRATEGY_FACTORY_OBSERVE_D_GRADE_ENABLED"] = old_p0
    else:
        os.environ.pop("STRATEGY_FACTORY_OBSERVE_D_GRADE_ENABLED", None)

# === 步骤 4:P1 toggle ON 时 IncubationIntake._list_paper_observation_strategies 行为 ===
print(f"\n[e2e] 步骤 4: P1 路径验证(IncubationIntake._list_paper_observation_strategies)", flush=True)
try:
    from akshare_mcp.services.incubation_factory.intake import IncubationIntake

    intake = IncubationIntake()

    class MockDBNoMethod:
        """模拟一个不实现 list_paper_observation_strategies 的 db"""
        pass

    class MockDBWithMethod:
        """模拟一个实现了 list_paper_observation_strategies 的 db"""
        async def list_paper_observation_strategies(self, limit=50):
            # 返回一个伪 paper observation 候选
            return [{
                "id": "fake_strategy_001",
                "strategy_id": "fake_strategy_001",
                "name": "Fake Paper Observation Test",
                "strategy_type": "momentum",
                "status": "submitted",
                "paper_account_id": "fake_account_001",
                "paper_bound_at": "2026-05-26 12:00:00",
            }]

    # P1 toggle ON + db 不实现该方法 → 应返回空(降级)
    result_no_method = asyncio.run(intake._list_paper_observation_strategies(MockDBNoMethod()))
    print(f"  toggle ON + db无该方法: 返回 {len(result_no_method)} 条 (期望 0,降级)", flush=True)

    # P1 toggle ON + db 实现该方法 → 应返回 1 条
    result_with_method = asyncio.run(intake._list_paper_observation_strategies(MockDBWithMethod()))
    print(f"  toggle ON + db有该方法: 返回 {len(result_with_method)} 条 (期望 1)", flush=True)

    # P1 toggle OFF → 应返回空
    old_p1 = os.environ.get("INCUBATION_FACTORY_PAPER_INTAKE_ENABLED")
    os.environ["INCUBATION_FACTORY_PAPER_INTAKE_ENABLED"] = "0"
    try:
        result_off = asyncio.run(intake._list_paper_observation_strategies(MockDBWithMethod()))
        print(f"  toggle OFF + db有该方法: 返回 {len(result_off)} 条 (期望 0)", flush=True)
    finally:
        if old_p1 is not None:
            os.environ["INCUBATION_FACTORY_PAPER_INTAKE_ENABLED"] = old_p1
        else:
            os.environ.pop("INCUBATION_FACTORY_PAPER_INTAKE_ENABLED", None)

    p1_path_pass = (
        len(result_no_method) == 0
        and len(result_with_method) == 1
        and len(result_off) == 0
    )
    print(f"  步骤 4 (P1 路径): {'✅ PASS' if p1_path_pass else '❌ FAIL'}", flush=True)
except Exception as exc:
    print(f"  ❌ 步骤 4 失败: {exc}", flush=True)
    import traceback
    traceback.print_exc()
    p1_path_pass = False


# === 步骤 5:对应 IncubationFactoryRunner._list_paper_observation 行为 ===
print(f"\n[e2e] 步骤 5: P1 runner 端验证(IncubationFactoryRunner._list_paper_observation)", flush=True)
try:
    from akshare_mcp.services.incubation_factory.runner import IncubationFactoryRunner

    runner = IncubationFactoryRunner(dry_run=True)

    # P1 toggle ON + db 实现该方法 → 应返回 1 条
    result = asyncio.run(runner._list_paper_observation(MockDBWithMethod()))
    print(f"  runner._list_paper_observation: 返回 {len(result)} 条 (期望 1)", flush=True)

    p1_runner_pass = len(result) == 1
    print(f"  步骤 5 (P1 runner): {'✅ PASS' if p1_runner_pass else '❌ FAIL'}", flush=True)
except Exception as exc:
    print(f"  ❌ 步骤 5 失败: {exc}", flush=True)
    import traceback
    traceback.print_exc()
    p1_runner_pass = False


# === 总判定 ===
print(f"\n[e2e] === 总判定 ===", flush=True)
print(f"  步骤 1 (toggle 解析): {'✅' if step1_pass else '❌'}", flush=True)
print(f"  步骤 2 (P0 路径 ON):  {'✅' if p0_path_pass else '❌'}", flush=True)
print(f"  步骤 3 (P0 路径 OFF 对照): {'✅' if contrast_pass else '❌'}", flush=True)
print(f"  步骤 4 (P1 intake): {'✅' if p1_path_pass else '❌'}", flush=True)
print(f"  步骤 5 (P1 runner): {'✅' if p1_runner_pass else '❌'}", flush=True)

all_pass = step1_pass and p0_path_pass and contrast_pass and p1_path_pass and p1_runner_pass

if all_pass:
    print(f"\n[e2e] 🎉 完整端到端验证 PASS", flush=True)
    print(f"     P0 toggle ON 时:D + Gate-passed 候选 → eligible=True → observe lane → paper 账户", flush=True)
    print(f"     P1 toggle ON 时:IncubationIntake/Runner 真的会调 db.list_paper_observation_strategies", flush=True)
    print(f"     生产 cycle 一旦产 D + Gate-passed 候选,链路就会自然触发", flush=True)
    sys.exit(0)
else:
    print(f"\n[e2e] ❌ 部分验证失败", flush=True)
    sys.exit(1)
