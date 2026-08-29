"""Tests for the P4 statistical robustness post-processor.

Covers:
1. ``enrich_validation_report_with_robustness_derivations`` adds the
   derived metrics block when the upstream walk-forward / bootstrap_ci
   / parameter_perturbation_trade_stability fields contain real data
   (R8.1, R8.2, Property 9 derived=True).
2. The post-processor leaves real upstream values alone (i.e., never
   overrides ``walk_forward.oos_rank_ic_ir`` or
   ``bootstrap_ci.ci_lower`` when they are already populated).
3. Sampling policy switches to ``degraded`` after the configured
   ``full_sample_size`` (R8.5).
4. Per-candidate budget env var override (R8.5).
5. Idempotency: calling the post-processor twice yields the same shape.
"""

from __future__ import annotations

from strategy_factory.application.research.statistical_robustness import (
    RobustnessSample,
    RobustnessSamplingPolicy,
    enrich_validation_report_with_robustness_derivations,
)


# ---------------------------------------------------------------------------
# Sampling policy (R8.5)
# ---------------------------------------------------------------------------

def test_sampling_policy_full_for_low_index() -> None:
    policy = RobustnessSamplingPolicy(full_sample_size=4, budget_sec=30.0)
    for i in (0, 1, 2, 3):
        decision = policy.decision_for(i)
        assert isinstance(decision, RobustnessSample)
        assert decision.sampling == "full"
        assert decision.candidate_index == i
        assert decision.full_sample_size == 4
        assert decision.budget_sec == 30.0


def test_sampling_policy_degraded_after_full_size() -> None:
    policy = RobustnessSamplingPolicy(full_sample_size=4)
    for i in (4, 5, 99):
        decision = policy.decision_for(i)
        assert decision.sampling == "degraded"


def test_sampling_policy_env_override_full_size(monkeypatch) -> None:
    monkeypatch.setenv("STRATEGY_FACTORY_BACKTEST_FULL_SAMPLE_SIZE", "2")
    policy = RobustnessSamplingPolicy()
    assert policy.full_sample_size == 2
    assert policy.decision_for(1).sampling == "full"
    assert policy.decision_for(2).sampling == "degraded"


def test_sampling_policy_env_override_budget_sec(monkeypatch) -> None:
    monkeypatch.setenv("STRATEGY_FACTORY_BACKTEST_PER_CANDIDATE_BUDGET_SEC", "12.5")
    policy = RobustnessSamplingPolicy()
    assert policy.budget_sec == 12.5


def test_sampling_policy_env_bad_value_falls_back() -> None:
    import os
    os.environ["STRATEGY_FACTORY_BACKTEST_PER_CANDIDATE_BUDGET_SEC"] = "garbage"
    try:
        policy = RobustnessSamplingPolicy()
        assert policy.budget_sec == 30.0  # default
    finally:
        del os.environ["STRATEGY_FACTORY_BACKTEST_PER_CANDIDATE_BUDGET_SEC"]


# ---------------------------------------------------------------------------
# enrich_validation_report — derivations (R8.1 / R8.2 / Property 9)
# ---------------------------------------------------------------------------

def test_enrich_derives_wf_ic_ir_when_mean_std_present() -> None:
    report = {
        "walk_forward": {
            "oos_rank_ic_mean": 0.05,
            "oos_rank_ic_std": 0.10,
            # oos_rank_ic_ir intentionally absent
        },
    }
    out = enrich_validation_report_with_robustness_derivations(
        report, backtest_metrics={}, candidate_index=0,
    )
    metrics = out["statistical_metrics"]
    assert "wf_ic_ir" in metrics
    assert metrics["wf_ic_ir"]["derived"] is True
    assert abs(metrics["wf_ic_ir"]["value"] - 0.5) < 1e-9
    assert metrics["wf_ic_ir"]["source"] == "derived_from_walk_forward_mean_std"


def test_enrich_skips_wf_ic_ir_when_already_real() -> None:
    """Don't override real upstream IR values."""
    report = {
        "walk_forward": {
            "oos_rank_ic_mean": 0.05,
            "oos_rank_ic_std": 0.10,
            "oos_rank_ic_ir": 0.42,  # real upstream value
        },
    }
    out = enrich_validation_report_with_robustness_derivations(
        report, backtest_metrics={}, candidate_index=0,
    )
    # When upstream is real, the post-processor doesn't add a derived field.
    assert "wf_ic_ir" not in out.get("statistical_metrics", {})
    # Original upstream value is untouched
    assert out["walk_forward"]["oos_rank_ic_ir"] == 0.42


def test_enrich_does_not_derive_wf_ic_ir_from_zero_inputs() -> None:
    """If mean and std are 0.0 (placeholders), don't derive — that would
    yield NaN or a meaningless number."""
    report = {
        "walk_forward": {
            "oos_rank_ic_mean": 0.0,
            "oos_rank_ic_std": 0.0,
        },
    }
    out = enrich_validation_report_with_robustness_derivations(
        report, backtest_metrics={}, candidate_index=0,
    )
    assert "wf_ic_ir" not in out.get("statistical_metrics", {})


def test_enrich_derives_bootstrap_ci_lower() -> None:
    report = {
        "bootstrap_ci": {
            "ic_mean": 0.10,
            "se": 0.04,
        },
    }
    out = enrich_validation_report_with_robustness_derivations(
        report, backtest_metrics={}, candidate_index=0,
    )
    metrics = out["statistical_metrics"]
    assert "bootstrap_ci_lower" in metrics
    assert metrics["bootstrap_ci_lower"]["derived"] is True
    expected = 0.10 - 1.96 * 0.04
    assert abs(metrics["bootstrap_ci_lower"]["value"] - expected) < 1e-9


def test_enrich_skips_bootstrap_ci_lower_when_real() -> None:
    report = {
        "bootstrap_ci": {
            "ic_mean": 0.10,
            "se": 0.04,
            "ci_lower": 0.025,  # real upstream
        },
    }
    out = enrich_validation_report_with_robustness_derivations(
        report, backtest_metrics={}, candidate_index=0,
    )
    assert "bootstrap_ci_lower" not in out.get("statistical_metrics", {})


def test_enrich_derives_period_robustness_from_walk_forward() -> None:
    report = {
        "walk_forward": {
            "is_ic_mean": 0.04,
            "oos_ic_mean": 0.025,
            "oos_rank_ic_mean": 0.025,
        },
    }
    out = enrich_validation_report_with_robustness_derivations(
        report, backtest_metrics={}, candidate_index=0,
    )
    pr = out["statistical_metrics"].get("period_robustness")
    assert pr is not None
    assert pr["derived"] is True
    assert pr["value"]["first_half_ic"] == 0.04
    assert pr["value"]["second_half_ic"] == 0.025


def test_enrich_derives_param_sensitivity_from_trade_stability() -> None:
    report = {}
    bt = {"parameter_perturbation_trade_stability": 0.85}
    out = enrich_validation_report_with_robustness_derivations(
        report, backtest_metrics=bt, candidate_index=0,
    )
    ps = out["statistical_metrics"].get("param_sensitivity")
    assert ps is not None
    assert ps["derived"] is True
    assert abs(ps["value"] - 0.15) < 1e-9


# ---------------------------------------------------------------------------
# Sampling tag (R8.5)
# ---------------------------------------------------------------------------

def test_enrich_writes_sampling_full_for_low_index() -> None:
    out = enrich_validation_report_with_robustness_derivations(
        {}, backtest_metrics={}, candidate_index=0,
        policy=RobustnessSamplingPolicy(full_sample_size=4),
    )
    assert out["statistical_metrics"]["sampling"] == "full"
    assert out["statistical_metrics"]["sampling_policy"]["candidate_index"] == 0


def test_enrich_writes_sampling_degraded_for_high_index() -> None:
    out = enrich_validation_report_with_robustness_derivations(
        {}, backtest_metrics={}, candidate_index=10,
        policy=RobustnessSamplingPolicy(full_sample_size=4),
    )
    assert out["statistical_metrics"]["sampling"] == "degraded"


# ---------------------------------------------------------------------------
# Resilience
# ---------------------------------------------------------------------------

def test_enrich_handles_none_validation_report() -> None:
    out = enrich_validation_report_with_robustness_derivations(
        None, backtest_metrics=None, candidate_index=0,
    )
    assert out["statistical_metrics"]["sampling"] == "full"


def test_enrich_is_idempotent() -> None:
    report = {
        "walk_forward": {"oos_rank_ic_mean": 0.05, "oos_rank_ic_std": 0.10},
        "bootstrap_ci": {"ic_mean": 0.10, "se": 0.04},
    }
    bt = {"parameter_perturbation_trade_stability": 0.85}
    once = enrich_validation_report_with_robustness_derivations(
        report, backtest_metrics=bt, candidate_index=0,
    )
    twice = enrich_validation_report_with_robustness_derivations(
        once, backtest_metrics=bt, candidate_index=0,
    )
    # Same set of derived metrics
    assert set(once["statistical_metrics"].keys()) == set(twice["statistical_metrics"].keys())
    # Values unchanged
    assert once["statistical_metrics"]["wf_ic_ir"]["value"] == \
           twice["statistical_metrics"]["wf_ic_ir"]["value"]


def test_enrich_does_not_overwrite_existing_statistical_metrics_block() -> None:
    """If a real (non-derived) metric is already present in
    ``statistical_metrics``, the post-processor must not stomp it."""
    report = {
        "walk_forward": {"oos_rank_ic_mean": 0.05, "oos_rank_ic_std": 0.10},
        "statistical_metrics": {
            "wf_ic_ir": {"value": 0.99, "derived": False, "source": "real_perturbation"},
        },
    }
    out = enrich_validation_report_with_robustness_derivations(
        report, backtest_metrics={}, candidate_index=0,
    )
    # Real value stays
    assert out["statistical_metrics"]["wf_ic_ir"]["value"] == 0.99
    assert out["statistical_metrics"]["wf_ic_ir"]["derived"] is False
