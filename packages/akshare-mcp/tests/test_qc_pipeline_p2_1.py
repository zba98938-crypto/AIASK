"""P2-1 单测：每日因子质检流水线编排（标签派生 / shelf 决策 / 写回 / autoshelf toggle）。

关联：开发周期计划-倒置架构与因子路由-2026-06-03.md · Phase 2 · P2-1
toggle：STRATEGY_FACTORY_FACTOR_QC_PIPELINE_ENABLED / STRATEGY_FACTORY_FACTOR_QC_AUTOSHELF_ENABLED（默认 OFF）。
"""

from __future__ import annotations

import asyncio

from akshare_mcp.services.factor_mining_factory import qc_pipeline as qc


def test_toggles_default_off(monkeypatch):
    monkeypatch.delenv("STRATEGY_FACTORY_FACTOR_QC_PIPELINE_ENABLED", raising=False)
    monkeypatch.delenv("STRATEGY_FACTORY_FACTOR_QC_AUTOSHELF_ENABLED", raising=False)
    assert qc.qc_pipeline_enabled() is False
    assert qc.qc_autoshelf_enabled() is False


def test_derive_labels_from_runner_outputs():
    labels = qc.derive_qc_labels(
        oos={"passed": True, "metrics": {"rank_ic_ir": 0.5}, "bootstrap_ci": {"lower": 0.05}},
        layered={"monotonicity": 0.9, "long_short_return": 0.12},
        robustness={"window_stability": 0.8, "param_sensitivity_max": 0.1},
        multiple_testing={"deflated_sharpe": {"dsr": 0.6}, "pbo": 0.2},
    )
    assert labels["oos_pass"] is True
    assert labels["rank_ic_ir"] == 0.5
    assert labels["bootstrap_ci_lower"] == 0.05
    assert labels["dsr"] == 0.6
    assert labels["pbo"] == 0.2
    assert labels["monotonicity"] == 0.9


def test_derive_labels_from_actual_tool_shapes():
    labels = qc.derive_qc_labels(
        oos={
            "validation_report": {
                "walk_forward": {"oos_rank_ic_ir": 0.51, "oos_positive_ratio": 0.61},
                "bootstrap_ci": {"ci_lower": 0.03},
                "rating": {"grade": "B"},
                "multiple_testing": {"deflated_sharpe": {"dsr": 0.7}, "pbo": {"pbo": 0.2}},
            }
        },
        layered={
            "group_returns": [
                {"group": 1, "avg_return": -0.01},
                {"group": 2, "avg_return": 0.01},
                {"group": 3, "avg_return": 0.03},
            ],
            "period_long_short_mean": 0.04,
        },
        robustness={
            "multi_window_ic": {"stability": 0.8},
            "param_sensitivity": {"stability": 0.9},
        },
    )

    assert labels["oos_pass"] is True
    assert labels["rank_ic_ir"] == 0.51
    assert labels["bootstrap_ci_lower"] == 0.03
    assert labels["monotonicity"] == 1.0
    assert labels["param_sensitivity"] == 0.1
    assert labels["dsr"] == 0.7
    assert labels["pbo"] == 0.2


def test_decide_shelf_promote_strong_factor():
    labels = {
        "oos_pass": True, "rank_ic_ir": 0.6, "bootstrap_ci_lower": 0.05,
        "param_sensitivity": 0.1, "dsr": 0.5, "pbo": 0.2, "monotonicity": 0.9,
    }
    # research 档阈值最宽松 → 强因子应 promote
    decision = qc.decide_shelf(labels, profile="minimum")
    assert decision["decision"] == "promote"
    assert decision["reasons"] == []


def test_decide_shelf_retire_garbage_factor():
    labels = {
        "oos_pass": False, "rank_ic_ir": 0.01, "bootstrap_ci_lower": -0.2,
        "param_sensitivity": 0.9, "dsr": -0.5, "pbo": 0.95, "monotonicity": 0.1,
    }
    decision = qc.decide_shelf(labels, profile="strict")
    assert decision["decision"] == "retire"
    assert "oos_not_passed" in decision["reasons"]


def test_decide_shelf_quarantine_borderline():
    # 仅一项轻微不达标（pbo 略高），其余达标 → quarantine（非 retire）
    labels = {
        "oos_pass": True, "rank_ic_ir": 0.5, "bootstrap_ci_lower": 0.05,
        "param_sensitivity": 0.1, "dsr": 0.5, "pbo": 0.5, "monotonicity": 0.9,
    }
    decision = qc.decide_shelf(labels, profile="strict")  # live 档 pbo_max=0.35
    assert decision["decision"] == "quarantine"
    assert decision["reasons"] == ["pbo_above_max"]


def test_decide_shelf_does_not_penalize_unavailable_optional_labels():
    labels = {
        "oos_pass": True,
        "rank_ic_ir": 0.6,
        "bootstrap_ci_lower": 0.05,
        "layered_available": False,
        "robustness_available": False,
        "multiple_testing_available": False,
        "monotonicity": 0.0,
        "param_sensitivity": 0.0,
        "dsr": 0.0,
        "pbo": 0.0,
    }

    decision = qc.decide_shelf(labels, profile="strict")

    assert decision["decision"] == "promote"
    assert decision["reasons"] == []


def test_run_factor_qc_with_injected_runners():
    async def oos_runner(name):
        return {"passed": True, "metrics": {"rank_ic_ir": 0.5}, "bootstrap_ci": {"lower": 0.05}}

    async def layered_runner(name):
        return {"monotonicity": 0.9, "long_short_return": 0.12}

    async def robustness_runner(name):
        return {"window_stability": 0.8, "param_sensitivity_max": 0.1}

    async def mt_runner(name):
        return {"deflated_sharpe": {"dsr": 0.6}, "pbo": 0.2}

    result = asyncio.run(qc.run_factor_qc(
        "momentum",
        oos_runner=oos_runner,
        layered_runner=layered_runner,
        robustness_runner=robustness_runner,
        multiple_testing_runner=mt_runner,
        profile="minimum",
    ))
    assert result["factor_name"] == "momentum"
    assert result["shelf_decision"]["decision"] == "promote"


def test_run_factor_qc_runner_failure_degrades():
    async def boom(name):
        raise RuntimeError("oos failed")

    # 所有 runner 缺失/报错 → 标签取缺省，oos_pass False → 不抛异常
    result = asyncio.run(qc.run_factor_qc("x", oos_runner=boom))
    assert result["labels"]["oos_pass"] is False
    assert "shelf_decision" in result


def test_apply_qc_label_only_when_autoshelf_off(monkeypatch):
    monkeypatch.delenv("STRATEGY_FACTORY_FACTOR_QC_AUTOSHELF_ENABLED", raising=False)
    record = {"factor_id": "f1", "status": "candidate", "validation_summary": {}}
    qc_result = {
        "labels": {"rank_ic_ir": 0.5},
        "shelf_decision": {"decision": "promote", "reasons": []},
    }
    out = qc.apply_qc_to_record(record, qc_result)
    vs = out["validation_summary"]
    # 打标签但不改 status（autoshelf OFF）
    assert vs["qc_labels"] == {"rank_ic_ir": 0.5}
    assert vs["qc_shelf_decision"]["decision"] == "promote"
    assert vs["qc_autoshelf_applied"] is False
    assert out["status"] == "candidate"
    assert "quality_status" not in vs


def test_apply_qc_autoshelf_promote(monkeypatch):
    monkeypatch.setenv("STRATEGY_FACTORY_FACTOR_QC_AUTOSHELF_ENABLED", "1")
    record = {"factor_id": "f1", "status": "candidate", "validation_summary": {}}
    qc_result = {"labels": {}, "shelf_decision": {"decision": "promote", "reasons": []}}
    out = qc.apply_qc_to_record(record, qc_result)
    assert out["validation_summary"]["quality_status"] == "promoted"
    assert out["status"] == "active"
    assert out["validation_summary"]["qc_autoshelf_applied"] is True


def test_apply_qc_autoshelf_retire(monkeypatch):
    monkeypatch.setenv("STRATEGY_FACTORY_FACTOR_QC_AUTOSHELF_ENABLED", "1")
    record = {"factor_id": "f1", "status": "active", "validation_summary": {}}
    qc_result = {"labels": {}, "shelf_decision": {"decision": "retire", "reasons": ["oos_not_passed"]}}
    out = qc.apply_qc_to_record(record, qc_result)
    assert out["validation_summary"]["quality_status"] == "retired"
    assert out["status"] == "retired"


def test_factory_run_qc_pipeline_off_skips(monkeypatch):
    """_run_qc_pipeline 在 toggle OFF 时返回 skipped，不触达 DB（零变化）。"""
    monkeypatch.delenv("STRATEGY_FACTORY_FACTOR_QC_PIPELINE_ENABLED", raising=False)
    from akshare_mcp.services.factor_mining_factory.factory import FactorMiningFactory

    factory = FactorMiningFactory.__new__(FactorMiningFactory)  # 不跑 __init__/初始化

    async def _scenario():
        # db 传 None：OFF 路径不应使用它
        return await factory._run_qc_pipeline(None)

    result = asyncio.run(_scenario())
    assert result["enabled"] is False
    assert result["skipped"] is True
