"""B1: 本地候选排序优先真实 IC,category 偏好作为 tiebreak。

由于 generate() 路径需要 db/frame/miner 重度 mock,这里直接对真实
_local_category_rank 复现 runtime.py 的排序键语义做回归,锁定:
(1) 高 IC 候选优先于低 IC(无论 category 偏好);
(2) IC 相同时退回 category 偏好排序。
"""

from __future__ import annotations

import akshare_mcp.services._strategy_generators_specs as specs_mod


def _rank_host():
    for name in dir(specs_mod):
        obj = getattr(specs_mod, name)
        if isinstance(obj, type) and hasattr(obj, "_local_category_rank"):
            return obj
    raise AssertionError("no class with _local_category_rank found")


def _sort_key(host, item):
    category = str((item or {}).get("category") or "custom").strip().lower()
    try:
        ic = float((item or {}).get("real_ic_abs") or 0.0)
    except (TypeError, ValueError):
        ic = 0.0
    return (-ic, host._local_category_rank(category, research_task=None))


def test_high_ic_candidate_beats_low_ic_regardless_of_category() -> None:
    host = _rank_host()
    pool = [
        {"category": "momentum", "real_ic_abs": 0.05, "name": "mom"},
        {"category": "reversal", "real_ic_abs": 0.30, "name": "rev"},
    ]
    ordered = sorted(pool, key=lambda it: _sort_key(host, it))
    # reversal 在默认任务下 category 偏好更差,但真实 IC 更高 → 应排第一
    assert ordered[0]["name"] == "rev"


def test_equal_ic_falls_back_to_category_preference() -> None:
    host = _rank_host()
    pool = [
        {"category": "reversal", "real_ic_abs": 0.1, "name": "rev"},
        {"category": "momentum", "real_ic_abs": 0.1, "name": "mom"},
    ]
    ordered = sorted(pool, key=lambda it: _sort_key(host, it))
    # IC 相同 → 默认任务下 momentum category 偏好优于 reversal
    assert ordered[0]["name"] == "mom"


def test_missing_ic_treated_as_zero() -> None:
    host = _rank_host()
    pool = [
        {"category": "momentum", "name": "mom_no_ic"},  # 无 real_ic_abs
        {"category": "reversal", "real_ic_abs": 0.2, "name": "rev_ic"},
    ]
    ordered = sorted(pool, key=lambda it: _sort_key(host, it))
    # 缺 IC 视为 0,有正 IC 的 reversal 应排前
    assert ordered[0]["name"] == "rev_ic"
