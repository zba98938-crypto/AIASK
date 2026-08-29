#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""V5-PR-1 隔离验证 — 不依赖完整 cycle,直接调用 helper 看真实输出。

目的:
  - 证明 _inject_run_correction_metrics 在真实 validation_runtime + 真实 strategy 下能跑通
  - 输出 multiple_testing_mode / deflated_sharpe_ratio / inject_status 等关键字段
  - 与单元测试不同:这里用 mcp_services.get_validation_runtime() 的真实运行时,不是 mock

如果这一步 ok 出非空字段,说明 V5-PR-1 helper 工程上完全接活;
未来 cycle 一旦产生候选,新写的 quality_reports 就会带上这些字段。
"""
from __future__ import annotations

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

# 调整 PYTHONPATH 让 strategy_factory 包可 import
ROOT = _aiask_root()
sys.path.insert(0, os.path.join(ROOT, "packages", "strategy-factory", "src"))
sys.path.insert(0, os.path.join(ROOT, "packages", "akshare-mcp", "src"))
sys.path.insert(0, os.path.join(ROOT, "packages", "aiask-quant-core", "src"))

print(f"[verify] PYTHONPATH 前 3:", flush=True)
for p in sys.path[:3]:
    print(f"  {p}", flush=True)

# 触发 fragment loader 让 _inject_run_correction_metrics 装入 globals
print("[verify] 加载 submission_gate.runner ...", flush=True)
from strategy_factory.application.submission_gate import runner as sg_runner
from pathlib import Path
print(f"[verify] runner 已加载,有 _inject_run_correction_metrics? {hasattr(sg_runner, '_inject_run_correction_metrics')}", flush=True)
print(f"[verify] runner 有 _estimate_run_correction_metrics? {hasattr(sg_runner, '_estimate_run_correction_metrics')}", flush=True)

# 准备一组真实风格的输入
strategy = {
    "id": "test_v5_pr1_001",
    "strategy_id": "test_v5_pr1_001",
    "name": "test_strategy_for_v5_pr1",
    "strategy_type": "momentum",
    "parameters": {"window": 20},
}
profile = {
    "profile": "trade_validation",
    "validation_focus": "target_plus_representative",
}
# normalized 模拟从 quality_gate 出来的真实结构
normalized = {
    "passed": True,
    "post_cost_sharpe": 0.85,  # 触发 observed_score=0.85
    "wf_ic_ir": 0.42,
    "trade_count": 12,
    "max_drawdown": -0.18,
    "attempt_adjustment": {
        "attempt_count": 5,
        "penalty": 0.02,
        "cohort_effective_trials": 4.5,
    },
}
# 模拟 walk_forward 序列:5 个 fold 的 oos_sharpe
validation_report = {
    "walk_forward": {
        "fold_results": [
            {"oos_sharpe": 0.7},
            {"oos_sharpe": 0.9},
            {"oos_sharpe": 1.1},
            {"oos_sharpe": 0.4},
            {"oos_sharpe": 0.8},
        ]
    }
}
# backtest_metrics 不带 family_returns(模拟生产中常见情况)
backtest_metrics = {
    "metrics_version": "test_v1",
    "annual_return": 0.15,
    "sharpe": 0.85,
}

print("\n[verify] 调用 _inject_run_correction_metrics ...", flush=True)
result = sg_runner._inject_run_correction_metrics(
    strategy,
    profile,
    normalized,
    validation_report=validation_report,
    backtest_metrics=backtest_metrics,
)
print("[verify] 返回字段:", flush=True)
print(json.dumps(result, indent=2, default=str, ensure_ascii=False), flush=True)

# === 判定 ===
status = result.get("multiple_testing_inject_status")
mt_mode = result.get("multiple_testing_mode")
dsr = result.get("deflated_sharpe_ratio")
print("\n[verify] === KEY METRICS ===", flush=True)
print(f"[verify] multiple_testing_inject_status: {status!r}", flush=True)
print(f"[verify] multiple_testing_mode: {mt_mode!r}", flush=True)
print(f"[verify] deflated_sharpe_ratio: {dsr!r}", flush=True)
print(f"[verify] pbo: {result.get('pbo')!r}", flush=True)
print(f"[verify] white_reality_check_pvalue: {result.get('white_reality_check_pvalue')!r}", flush=True)
print(f"[verify] hansen_spa_pvalue: {result.get('hansen_spa_pvalue')!r}", flush=True)
err = result.get("multiple_testing_inject_error")
if err:
    print(f"[verify] inject_error: {err!r}", flush=True)

print("\n[verify] === VERDICT ===", flush=True)
if status == "ok":
    print("[verify] PASS: V5-PR-1 helper 在真实 validation_runtime 下成功执行,返回非空字段", flush=True)
elif status == "validation_runtime_unavailable":
    print("[verify] PARTIAL: helper 软降级路径生效,validation_runtime 不可用(预期可能,生产端有 import)", flush=True)
elif status == "exception":
    print(f"[verify] FAIL: helper 抛异常,err={err}", flush=True)
else:
    print(f"[verify] UNKNOWN: status={status!r}, 需排查", flush=True)

# 模拟 normalize 二次合并(模拟 entry.py 第 196 行后的合并逻辑)
print("\n[verify] 模拟 entry.py 中的 normalize_quality_gate_result 合并 ...", flush=True)
try:
    merged = sg_runner.normalize_quality_gate_result({**normalized, **result})
    print(f"[verify] merged.multiple_testing_mode: {merged.get('multiple_testing_mode')!r}", flush=True)
    print(f"[verify] merged.deflated_sharpe_ratio: {merged.get('deflated_sharpe_ratio')!r}", flush=True)
except Exception as e:
    print(f"[verify] normalize_quality_gate_result 失败: {e}", flush=True)

print("\n[verify] DONE", flush=True)
