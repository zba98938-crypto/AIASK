"""P3-1 单测：命中率矩阵（strategy_type × regime × holding_bucket）。

关联：开发周期计划-倒置架构与因子路由-2026-06-03.md · Phase 3 · P3-1
验收：交叉聚合正确；空/低样本单元诚实标注 insufficient_samples；skill_lcb 同口径；
      build_hit_rate_matrix 注入 lister/verifier 可跑；verify 失败跳过不阻断。
"""

from __future__ import annotations

import asyncio
import json

from akshare_mcp.services.incubation_factory import hit_rate_matrix as hrm


def _verify_result(sid, stype, *, trend_label, n, hit_rate):
    return {
        "strategy_id": sid,
        "strategy_type": stype,
        "hit_rate_by_regime": {
            "trend_regime": {trend_label: {"hit_rate": hit_rate, "skill_lcb": 0.0, "n": n}},
            "vol_regime": {},
            "sentiment_regime": {},
        },
    }


def test_aggregate_basic_crosstab():
    strategies = {
        "s1": {"id": "s1", "strategy_type": "momentum", "holding_period_bucket": "medium"},
        "s2": {"id": "s2", "strategy_type": "momentum", "holding_period_bucket": "medium"},
    }
    results = [
        _verify_result("s1", "momentum", trend_label="trend_up", n=10, hit_rate=0.6),
        _verify_result("s2", "momentum", trend_label="trend_up", n=10, hit_rate=0.8),
    ]
    out = hrm.aggregate_hit_rate_matrix(results, strategies, min_cell_n=5)
    cell = out["matrix"]["momentum"]["medium"]["trend_regime"]["trend_up"]
    assert cell["status"] == "ok"
    # 加权合并：(0.6*10 + 0.8*10)/20 = 0.7
    assert cell["hit_rate"] == 0.7
    assert cell["n"] == 20
    assert out["totals"]["cells_ok"] == 1


def test_insufficient_samples_flagged():
    strategies = {"s1": {"id": "s1", "strategy_type": "rsi", "holding_period_bucket": "short"}}
    results = [_verify_result("s1", "rsi", trend_label="range", n=3, hit_rate=0.5)]
    out = hrm.aggregate_hit_rate_matrix(results, strategies, min_cell_n=5)
    cell = out["matrix"]["rsi"]["short"]["trend_regime"]["range"]
    assert cell["status"] == "insufficient_samples"
    assert cell["n"] == 3
    assert "hit_rate" not in cell
    assert out["totals"]["cells_insufficient"] == 1


def test_separate_types_and_buckets_not_merged():
    strategies = {
        "s1": {"id": "s1", "strategy_type": "momentum", "holding_period_bucket": "medium"},
        "s2": {"id": "s2", "strategy_type": "value_factor", "holding_period_bucket": "long"},
    }
    results = [
        _verify_result("s1", "momentum", trend_label="trend_up", n=10, hit_rate=0.7),
        _verify_result("s2", "value_factor", trend_label="trend_up", n=10, hit_rate=0.4),
    ]
    out = hrm.aggregate_hit_rate_matrix(results, strategies, min_cell_n=5)
    assert "momentum" in out["matrix"]
    assert "value_factor" in out["matrix"]
    assert out["matrix"]["momentum"]["medium"]["trend_regime"]["trend_up"]["hit_rate"] == 0.7
    assert out["matrix"]["value_factor"]["long"]["trend_regime"]["trend_up"]["hit_rate"] == 0.4


def test_skill_lcb_matches_wilson():
    # Wilson 下界 - 0.5 随机基线：小样本 CI 宽，n=20/hit=0.7 实际为负（诚实反映不确定性）。
    small = hrm._wilson_skill_lcb(0.7, 20)
    assert small < 0.0  # 小样本不该被判为正 skill
    # 大样本下同样 hit_rate 才转正
    large = hrm._wilson_skill_lcb(0.7, 500)
    assert large > 0.0
    assert large < 0.2
    # n<5 直接返回 0
    assert hrm._wilson_skill_lcb(0.7, 3) == 0.0


def test_build_matrix_with_injected_lister_and_verifier():
    rows = {
        "incubating": [
            {"id": "s1", "strategy_type": "momentum", "holding_period_bucket": "medium"},
            {"id": "s2", "strategy_type": "rsi", "holding_period_bucket": "short"},
        ],
        "listed": [],
    }

    async def lister(status, limit):
        return rows.get(status, [])

    async def verifier(strategy):
        sid = strategy["id"]
        stype = strategy["strategy_type"]
        label = "trend_up" if stype == "momentum" else "range"
        return _verify_result(sid, stype, trend_label=label, n=8, hit_rate=0.65)

    out = asyncio.run(hrm.build_hit_rate_matrix(strategy_lister=lister, verifier=verifier, min_cell_n=5))
    assert out["totals"]["strategies"] == 2
    assert "momentum" in out["matrix"]
    assert "rsi" in out["matrix"]


def test_build_matrix_verify_failure_skips():
    async def lister(status, limit):
        if status == "incubating":
            return [{"id": "bad", "strategy_type": "momentum", "holding_period_bucket": "medium"},
                    {"id": "good", "strategy_type": "momentum", "holding_period_bucket": "medium"}]
        return []

    async def verifier(strategy):
        if strategy["id"] == "bad":
            raise RuntimeError("verify boom")
        return _verify_result("good", "momentum", trend_label="trend_up", n=10, hit_rate=0.7)

    out = asyncio.run(hrm.build_hit_rate_matrix(strategy_lister=lister, verifier=verifier, min_cell_n=5))
    # bad 被跳过，只剩 good
    assert out["totals"]["strategies"] == 1
    assert out["matrix"]["momentum"]["medium"]["trend_regime"]["trend_up"]["n"] == 10


def test_empty_input_returns_empty_matrix():
    out = hrm.aggregate_hit_rate_matrix([], {})
    assert out["matrix"] == {}
    assert out["totals"]["strategies"] == 0


def test_aggregate_skips_non_finite_cells():
    strategies = {
        "bad": {"id": "bad", "strategy_type": "momentum", "holding_period_bucket": "medium"},
        "good": {"id": "good", "strategy_type": "momentum", "holding_period_bucket": "medium"},
    }
    results = [
        _verify_result("bad", "momentum", trend_label="trend_up", n=10, hit_rate=float("inf")),
        _verify_result("good", "momentum", trend_label="trend_up", n=10, hit_rate=0.6),
    ]

    out = hrm.aggregate_hit_rate_matrix(results, strategies, min_cell_n=5)
    cell = out["matrix"]["momentum"]["medium"]["trend_regime"]["trend_up"]

    assert cell["status"] == "ok"
    assert cell["hit_rate"] == 0.6
    assert cell["n"] == 10
    json.dumps(out, allow_nan=False)
