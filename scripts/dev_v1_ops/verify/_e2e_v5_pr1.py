#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""V5-PR-1 端到端最小验证 — 直接调用 run_submission_quality_gate。

目的:不依赖完整 cycle,直接用模拟 strategy/validation_report 走完整 submission_gate 流程,
验证 V5-PR-1 注入字段是否落入最终返回结果。
"""
from __future__ import annotations

import asyncio
import json
import sys
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

ROOT = _aiask_root()
sys.path.insert(0, os.path.join(ROOT, "packages", "strategy-factory", "src"))
sys.path.insert(0, os.path.join(ROOT, "packages", "akshare-mcp", "src"))
sys.path.insert(0, os.path.join(ROOT, "packages", "aiask-quant-core", "src"))

print("[e2e] 加载 submission_gate.runner ...", flush=True)
from strategy_factory.application.submission_gate import runner as sg_runner
from pathlib import Path

# 检查 helper 是否在 globals
print(f"[e2e] _inject_run_correction_metrics 在 globals: {hasattr(sg_runner, '_inject_run_correction_metrics')}")
print(f"[e2e] run_submission_quality_gate 在 globals: {hasattr(sg_runner, 'run_submission_quality_gate')}")


class FakeDB:
    """最小化 fake DB,只覆盖 run_submission_quality_gate 可能用到的方法。"""
    async def fetchrow(self, *args, **kwargs):
        return None
    async def fetch(self, *args, **kwargs):
        return []
    async def execute(self, *args, **kwargs):
        return None


async def run():
    # 准备 strategy:必须能在 strategy_registry 找到 momentum
    strategy = {
        "id": "test_e2e_v5pr1_001",
        "strategy_id": "test_e2e_v5pr1_001",
        "name": "test_e2e_strategy",
        "strategy_type": "momentum",
        "parameters": {"window": 20, "top_n": 5},
        "entry_signals": [{"type": "momentum", "lookback": 20}],
        "exit_signals": [{"type": "momentum_break", "lookback": 5}],
    }
    # validation_report 包含 walk_forward fold 序列(让 helper 拿到 score_series)
    validation_report = {
        "walk_forward": {
            "fold_results": [
                {"oos_sharpe": 0.85, "oos_score": 0.85},
                {"oos_sharpe": 1.10, "oos_score": 1.10},
                {"oos_sharpe": 0.62, "oos_score": 0.62},
                {"oos_sharpe": 0.95, "oos_score": 0.95},
                {"oos_sharpe": 0.70, "oos_score": 0.70},
                {"oos_sharpe": 0.80, "oos_score": 0.80},
            ],
            "wf_ic_ir": 0.45,
            "robustness": {"mean_ic": 0.05},
        },
        "ic_metrics": {"ic_mean": 0.05, "ic_ir": 0.45, "ic_count": 30},
        "stats": {"sample_size": 240, "n_folds": 6},
    }
    risk_report = {"max_drawdown": -0.15, "var_95": -0.05}
    backtest_metrics = {
        "metrics_version": "test_v1",
        "annual_return": 0.18,
        "sharpe": 0.92,
        "max_drawdown": -0.15,
        "trade_count": 36,
        "post_cost_sharpe": 0.78,
    }

    print("\n[e2e] 调用 run_submission_quality_gate ...", flush=True)
    result = await sg_runner.run_submission_quality_gate(
        FakeDB(),
        strategy,
        validation_report=validation_report,
        risk_report=risk_report,
        backtest_metrics=backtest_metrics,
    )

    print(f"[e2e] 返回 {len(result)} 个字段", flush=True)

    # === 检查 V5-PR-1 注入字段 ===
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
        "passed",
        "reason",
    ]
    print(f"\n[e2e] === V5-PR-1 关键字段 ===")
    for k in keys_to_probe:
        if k in result:
            v = result[k]
            print(f"  {k}: {v!r}" if not isinstance(v, dict) else f"  {k}: <dict {len(v)} keys>")

    # mt sub-dict
    mt = result.get("multiple_testing")
    if isinstance(mt, dict):
        print(f"\n[e2e] result.multiple_testing keys: {list(mt.keys())}")
        for k, v in mt.items():
            print(f"  multiple_testing.{k}: {v if not isinstance(v, dict) else '<dict {} keys>'.format(len(v))}")

    # === 判定 ===
    inject_status = result.get("multiple_testing_inject_status")
    mt_mode = result.get("multiple_testing_mode")
    dsr = result.get("deflated_sharpe_ratio")

    print(f"\n[e2e] === VERDICT ===")
    print(f"[e2e] multiple_testing_inject_status: {inject_status!r}")
    print(f"[e2e] multiple_testing_mode: {mt_mode!r}")
    print(f"[e2e] deflated_sharpe_ratio: {dsr!r}")

    if inject_status == "ok" and mt_mode is not None:
        print("[e2e] PASS: V5-PR-1 端到端注入成功 ✅")
        return 0
    elif inject_status == "validation_runtime_unavailable":
        print("[e2e] DEGRADED: helper 软降级路径生效")
        return 1
    elif inject_status is None:
        # 可能 strategy_type 不在 registry,直接 reject 跳过 helper
        if "Strategy type not in registry" in str(result.get("reason", "")):
            print("[e2e] EARLY_REJECT: strategy_type 不在 registry,helper 没被调用")
            return 2
        else:
            print(f"[e2e] FAIL: inject_status 为 None,但 strategy 进入了流程,需排查")
            print(f"[e2e] result.reason: {result.get('reason')!r}")
            print(f"[e2e] result.passed: {result.get('passed')!r}")
            return 3
    else:
        print(f"[e2e] UNKNOWN: status={inject_status!r}")
        return 4


if __name__ == "__main__":
    code = asyncio.run(run())
    print(f"\n[e2e] DONE exit_code={code}")
    sys.exit(code)
