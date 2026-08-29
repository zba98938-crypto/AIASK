"""P0-C 可观测性回归:候选→spec 静默清零现可归因。

诊断报告病灶 C(收窄版):_normalize_snapshot_pipeline_candidate 返回 None 时原本完全静默
(无日志/reason/计数),是"staged 空 specs 查不出原因"的真盲区。修复后被清零的候选会带
_generator_normalize_reject_reason,并由 _pipeline_candidate_to_spec / _generate_via_pipeline
冒泡到 last_report 的 pipeline_normalize_rejections + pipeline_candidate_funnel。
"""

from __future__ import annotations

import copy

from akshare_mcp.services._strategy_generators_generate import (
    _normalize_snapshot_pipeline_candidate,
)


_CONSERVATIVE_RT = {
    "task_source": "snapshot",
    "validation_focus": "candidate_target_only",
    "allowed_strategy_types": ["ma_cross"],
}


def _run(cand: dict):
    payload = copy.deepcopy(cand)
    out = _normalize_snapshot_pipeline_candidate(payload)
    return out, payload.get("_generator_normalize_reject_reason")


def test_momentum_or_disallowed_type_records_reject_reason():
    # conservative snapshot task 下不在白名单的类型被清零,且记录可归因 reason
    out, reason = _run(
        {"strategy_type": "rsi", "name": "r", "research_task": dict(_CONSERVATIVE_RT), "target_symbols": ["600000"]}
    )
    assert out is None
    assert reason and reason.startswith("strategy_type_not_in_conservative_allowlist")


def test_momentum_unconditional_drop_has_reason():
    # 白名单含 momentum 时走无条件 momentum 分支,仍记录 reason(非静默)
    rt = {**_CONSERVATIVE_RT, "allowed_strategy_types": ["momentum", "ma_cross"]}
    out, reason = _run(
        {"strategy_type": "momentum", "name": "m", "research_task": rt, "target_symbols": ["600000"]}
    )
    assert out is None
    assert reason == "momentum_dropped_in_conservative_snapshot_task"


def test_snapshot_pool_contract_empty_has_reason():
    # high_vol_growth + ma_cross → _apply_snapshot_pool_contract 返回 {} → 清零并记 reason
    out, reason = _run(
        {
            "strategy_type": "ma_cross",
            "name": "c",
            "research_task": {"pool_profile": "high_vol_growth"},
            "pool_profile": "high_vol_growth",
            "target_symbols": ["600000"],
        }
    )
    assert out is None
    assert reason == "snapshot_pool_contract_empty"


def test_allowed_candidate_kept_without_reject_reason():
    # 合法候选保留,不打 reject reason
    out, reason = _run(
        {"strategy_type": "ma_cross", "name": "ok", "research_task": dict(_CONSERVATIVE_RT),
         "target_symbols": ["600000"], "params": {}}
    )
    assert out is not None
    assert reason is None


def test_non_conservative_task_passthrough_no_reason():
    # 非 conservative snapshot task 直接放行,不清零不打 reason
    out, reason = _run(
        {"strategy_type": "momentum", "name": "m", "research_task": {"task_source": "manual"},
         "target_symbols": ["600000"]}
    )
    assert out is not None
    assert reason is None
