"""MCP_FIX_PLAN 第四批回归测试（FIX-35 ROOT-3/F-N42-2 payload bloat + lineage）。

- FIX-35a: closure_review factory.runs 仅保留标量摘要（不内嵌全 stages）
- FIX-35b: factory_run_id 仅在与策略真实关联时归属（手工 draft 不再被赋全局 run_id）
"""

from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src" / "akshare_mcp"


def _strategy_mgr_crud_source() -> str:
    """Read strategy_mgr_crud source, tolerating either a single module file or a package dir."""
    managers = _SRC / "tools" / "managers"
    single = managers / "strategy_mgr_crud.py"
    if single.exists():
        return single.read_text(encoding="utf-8")
    pkg = managers / "strategy_mgr_crud"
    return "\n".join(p.read_text(encoding="utf-8") for p in sorted(pkg.glob("*.py")))


# ── FIX-35a: factory run 摘要化 ──────────────────────────────────────

def test_fix35_summarize_factory_run_drops_stages():
    from akshare_mcp.services.strategy_lifecycle_shared.closure_review import _summarize_factory_run

    heavy_run = {
        "run_id": "factory_run_123",
        "status": "completed",
        "completed_at": "2026-05-30T10:00:00Z",
        "submitted_count": 3,
        # 重字段：完整 stages（110KB 级），必须被剔除
        "stages": {
            "warmup": {"x": "y" * 10000},
            "collect": {"data": list(range(10000))},
            "backtest": {"trades": [{"i": i} for i in range(5000)]},
        },
        "summary": {
            "submitted": 3,
            "raw_b_or_above_rate": 0.5,
            # 嵌套 dict 不应进摘要
            "nested_heavy": {"a": list(range(1000))},
        },
    }
    out = _summarize_factory_run(heavy_run)
    assert out["run_id"] == "factory_run_123"
    assert out["status"] == "completed"
    assert "stages" not in out
    # summary 仅保留标量
    assert out["summary"]["submitted"] == 3
    assert out["summary"]["raw_b_or_above_rate"] == 0.5
    assert "nested_heavy" not in out["summary"]


def test_fix35_summarize_handles_empty():
    from akshare_mcp.services.strategy_lifecycle_shared.closure_review import _summarize_factory_run

    assert _summarize_factory_run({}) == {} or _summarize_factory_run({}).get("run_id") is None
    assert _summarize_factory_run(None) == {}


# ── 静态扫描：确保 closure_review 不再内嵌全量 runs + 血缘门控 ─────────

def test_fix35_closure_review_runs_summarized_static():
    src = (_SRC / "services" / "strategy_lifecycle_shared" / "closure_review.py").read_text(encoding="utf-8")
    # 不应再出现把全量 factory_runs 直接塞进 runs 的写法
    assert '"runs": list(factory_runs or [])' not in src
    # 应使用摘要化
    assert "_summarize_factory_run(run) for run in list(factory_runs or [])" in src
    assert "runs_truncated" in src


def test_fix35_closure_review_lineage_gated_static():
    src = (_SRC / "services" / "strategy_lifecycle_shared" / "closure_review.py").read_text(encoding="utf-8")
    # factory_run_id 不再回退到全局 latest_factory_run.run_id
    assert 'or _string((latest_factory_run or {}).get("run_id"))\n        or None\n    )\n    resolved_as_of' not in src
    # 应有血缘归属基准标注
    assert "factory_run_lineage_basis" in src
    assert "_strategy_linked_run" in src


# ── FIX-36: incubation_overview 无 id 返回列表 (F-N42-4) ─────────────

def test_fix36_incubation_overview_no_id_not_required():
    from akshare_mcp.contracts.strategy_manager_contract import STRATEGY_MANAGER_REQUIRED_PARAMS

    # incubation_overview 不应再强制 id（handler 支持无 id 列表分支）
    assert "incubation_overview" not in STRATEGY_MANAGER_REQUIRED_PARAMS


# ── FIX-37: rank sort_by 枚举校验 (F-N22-3) ──────────────────────────

def test_fix37_rank_sort_by_validation_static():
    src = _strategy_mgr_crud_source()
    rank_idx = src.find("async def handle_rank")
    assert rank_idx != -1
    rank_src = src[rank_idx:rank_idx + 2000]
    assert "sort_by_warning" in rank_src
    assert "_valid_sort_keys" in rank_src


# ── FIX-38: publish 不可逆提示 (F-N42-3) ─────────────────────────────

def test_fix38_publish_irreversible_note_static():
    src = _strategy_mgr_crud_source()
    assert "irreversible_note" in src


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
