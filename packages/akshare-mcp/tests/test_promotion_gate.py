"""INVERT-DESIGN P3 改动B：PromotionGate（前向序列 + DSR）单元测试。

验证晋升门把 DSR 作用于前向收益序列、用 n_trials 做多重检验校正，
样本不足时不评估不阻断，统计失败软降级。
"""

from __future__ import annotations

import json

from akshare_mcp.services.incubation_factory.promotion_gate import (
    PromotionGate,
    PromotionGateVerdict,
)


def _fake_dsr(dsr_value: float):
    def _fn(arr, *, n_trials=1, benchmark_sharpe=0.0, periods_per_year=252.0):
        return {
            "available": True,
            "dsr": dsr_value,
            "observed_sharpe": 1.2,
            "effective_trials": float(n_trials),
            "sample_size": int(len(arr)),
        }

    return _fn


def test_insufficient_samples_not_eligible():
    gate = PromotionGate(min_sample_size=30)
    verdict = gate.evaluate([0.01] * 10, n_trials=5, dsr_fn=_fake_dsr(0.99))
    assert isinstance(verdict, PromotionGateVerdict)
    assert verdict.eligible is False
    assert verdict.passed is False
    assert any("insufficient_forward_samples" in r for r in verdict.reasons)


def test_dsr_pass_promotes():
    gate = PromotionGate(dsr_min=0.60, min_sample_size=30)
    verdict = gate.evaluate([0.005] * 40, n_trials=10, dsr_fn=_fake_dsr(0.75))
    assert verdict.eligible is True
    assert verdict.passed is True
    assert verdict.dsr == 0.75
    assert verdict.effective_trials == 10.0


def test_dsr_below_min_holds():
    gate = PromotionGate(dsr_min=0.60, min_sample_size=30)
    verdict = gate.evaluate([0.001] * 40, n_trials=50, dsr_fn=_fake_dsr(0.40))
    assert verdict.eligible is True
    assert verdict.passed is False
    assert any("dsr_below_min" in r for r in verdict.reasons)


def test_dsr_exception_soft_degrades():
    def _boom(arr, **kw):
        raise RuntimeError("statistical failure")

    gate = PromotionGate(min_sample_size=30)
    verdict = gate.evaluate([0.01] * 40, n_trials=5, dsr_fn=_boom)
    assert verdict.passed is False
    assert verdict.eligible is False
    assert any("dsr_exception" in r for r in verdict.reasons)


def test_dsr_unavailable_holds():
    def _unavailable(arr, **kw):
        return {"available": False, "dsr": 0.0}

    gate = PromotionGate(min_sample_size=30)
    verdict = gate.evaluate([0.01] * 40, n_trials=5, dsr_fn=_unavailable)
    assert verdict.passed is False
    assert verdict.eligible is False
    assert "dsr_not_available" in verdict.reasons


def test_real_dsr_fn_runs_end_to_end():
    # 不注入 dsr_fn → 用生产实现，确认端到端可跑通且返回结构正确。
    import random

    random.seed(7)
    series = [random.gauss(0.001, 0.01) for _ in range(80)]
    gate = PromotionGate(min_sample_size=30)
    verdict = gate.evaluate(series, n_trials=20)
    assert verdict.eligible is True
    assert verdict.dsr is not None
    assert 0.0 <= verdict.dsr <= 1.0
    assert verdict.sample_size == 80


def test_non_finite_forward_returns_and_dsr_outputs_are_sanitized():
    def _bad_dsr(arr, **kw):
        return {
            "available": True,
            "dsr": float("inf"),
            "observed_sharpe": "nan",
            "effective_trials": "-inf",
            "sample_size": int(len(arr)),
        }

    gate = PromotionGate(dsr_min=0.60, min_sample_size=3)
    verdict = gate.evaluate(
        [0.01, "nan", float("inf"), 0.02, "-inf", 0.03],
        n_trials=float("inf"),
        benchmark_sharpe=float("nan"),
        dsr_fn=_bad_dsr,
    )

    assert verdict.sample_size == 3
    assert verdict.eligible is True
    assert verdict.passed is False
    assert verdict.dsr == 0.0
    assert verdict.observed_sharpe == 0.0
    assert verdict.effective_trials == 0.0
    json.dumps(verdict.to_dict(), allow_nan=False)
