"""INVERT-DESIGN P2 改动C：ObservationLifecyclePolicy 单元测试。

验证抽取后的决策逻辑与历史 _derive_decision 完全一致（零行为变化），
以及 regime 维度可选纳入的行为。
"""

from __future__ import annotations

from akshare_mcp.services.incubation_factory.observation_lifecycle_policy import (
    ObservationLifecyclePolicy,
)


def _decide(**kw):
    policy = ObservationLifecyclePolicy(regime_enabled=kw.pop("regime_enabled", False))
    return policy.decide(**kw).decision


def _base(**over):
    args = dict(
        primary_skill_lcb=0.05,
        recent_primary_skill_lcb=0.03,
        stability_gap=0.02,
        coverage_ratio=0.8,
        primary_n=60,
    )
    args.update(over)
    return args


def test_insufficient_samples_observe():
    assert _decide(**_base(primary_n=5)) == "observe"


def test_recent_skill_negative_halt():
    assert _decide(**_base(recent_primary_skill_lcb=-0.05)) == "halt"


def test_stability_gap_high_halt():
    assert _decide(**_base(stability_gap=0.15)) == "halt"


def test_low_coverage_observe():
    assert _decide(**_base(coverage_ratio=0.20)) == "observe"


def test_strong_skill_promote():
    assert _decide(**_base()) == "promote"


def test_marginal_skill_observe():
    # skill_lcb 正但不足 promote 阈值，覆盖率达标 → observe（默认分支）
    assert _decide(**_base(primary_skill_lcb=0.01)) == "observe"


def test_regime_disabled_ignores_regime_negatives():
    # regime 有负，但 regime_enabled=False → 不影响（仍 promote）
    hr = {"trend_regime": {"range": {"recent_skill_lcb": -0.05, "n": 40}}}
    assert _decide(**_base(regime_enabled=False, hit_rate_by_regime=hr)) == "promote"


def test_regime_enabled_negative_halts():
    # regime_enabled=True + 达标 regime 近期转负 → halt（即使全局指标好）
    hr = {"trend_regime": {"range": {"recent_skill_lcb": -0.05, "n": 40}}}
    assert _decide(**_base(regime_enabled=True, hit_rate_by_regime=hr)) == "halt"


def test_regime_enabled_low_sample_not_halt():
    # regime 负但样本不足（n<min_n）→ 不触发 halt，仍 promote
    hr = {"trend_regime": {"range": {"recent_skill_lcb": -0.50, "n": 3}}}
    assert _decide(**_base(regime_enabled=True, hit_rate_by_regime=hr)) == "promote"


def test_non_finite_metrics_fall_back_to_observe():
    policy = ObservationLifecyclePolicy(regime_enabled=True)
    decision = policy.decide(
        primary_skill_lcb=float("inf"),
        recent_primary_skill_lcb="inf",
        stability_gap=float("nan"),
        coverage_ratio="-inf",
        primary_n=float("inf"),
        hit_rate_by_regime={"trend_regime": {"range": {"recent_skill_lcb": "-inf", "n": "inf"}}},
    )

    assert decision.decision == "observe"
    assert decision.reasons == ["insufficient_samples:0<10"]
