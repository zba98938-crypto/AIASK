"""Gate-3 statistical evidence tests (P1 / R5 / Property 3 / Property 4).

Verifies:
1. Missing vs weak vs pass classification (R5.1).
2. Reason codes ``missing_<metric>`` and ``weak_<metric>`` are emitted
   (R5.2, R5.3).
3. Placeholder 0.0 is treated as ``missing`` (audit P1-prep finding /
   Property 4): a value of 0.0 from an empty walk-forward bootstrap run
   should NOT be evaluated as "weak" against the threshold.
4. Threshold semantics unchanged for the existing pass path (Property 3
   regression guard).
5. ``derived=true`` is propagated when a metric was supplied via the
   stop-gap inverse-of-trade-stability path (Property 9).
"""

from __future__ import annotations

import asyncio

import pytest


# Use the runtime-evaluation entry point from the same module that Gate-3
# uses internally. The fragment loader rolls these up under
# ``submission_gate.runner``; the easiest way to import the helpers is via
# the application module that exposes them.
from strategy_factory.application.submission_gate.runner import (  # type: ignore[attr-defined]
    _classify_gate3_metric_value,
    _build_gate3_evaluation,
    _evaluate_statistical_admission,
    _run_statistical_gate,
)


# ---------------------------------------------------------------------------
# Classifier (Property 4)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "value, expected",
    [
        (None, "missing"),
        (float("nan"), "missing"),
        (float("inf"), "missing"),
        (float("-inf"), "missing"),
        (0.0, "missing"),
        (1e-13, "missing"),  # zero up to noise floor
        (-1e-13, "missing"),
        (0.05, "present_real"),
        (-0.5, "present_real"),
        (1.0, "present_real"),
    ],
)
def test_classify_gate3_metric_value(value, expected) -> None:
    assert _classify_gate3_metric_value(value) == expected


# ---------------------------------------------------------------------------
# evaluate_statistical_admission — missing path (R5.2)
# ---------------------------------------------------------------------------

def test_evaluate_statistical_admission_missing_emits_per_metric_codes() -> None:
    strategy = {"id": "s_missing"}
    profile = {"profile": "factor_rank_validation"}
    # Empty payload — every metric is missing
    result = _evaluate_statistical_admission(strategy, profile, gate_payload={})

    reasons = result.get("reasons") or []
    # Backward-compatible aggregate line
    aggregate = [r for r in reasons if r.startswith("missing_statistical_metrics")]
    assert len(aggregate) == 1, f"expected aggregate line, got {reasons}"
    assert "insufficient_statistical_evidence" in reasons
    # Per-metric structured codes
    for m in ("wf_ic_ir", "pkf_ic", "bootstrap_ci_lower", "param_sensitivity"):
        assert f"missing_{m}" in reasons, f"missing_{m} not in {reasons}"
    assert result.get("passed") is False
    # gate_3_evaluation array gives the per-metric breakdown
    eval_rows = result.get("gate_3_evaluation") or []
    assert {row["metric"] for row in eval_rows} == {
        "wf_ic_ir", "pkf_ic", "bootstrap_ci_lower", "param_sensitivity",
    }
    for row in eval_rows:
        assert row["status"] == "missing"
        assert row["value"] is None
        assert row["reason_code"] == f"missing_{row['metric']}"


# ---------------------------------------------------------------------------
# Placeholder 0.0 → missing (audit P1-prep finding)
# ---------------------------------------------------------------------------

def test_evaluate_statistical_admission_placeholder_zero_is_missing() -> None:
    """Audit P1-prep observed that factor_validation_bootstrap writes 0.0
    when an empty fold/bootstrap run happens. Gate-3 must NOT treat that
    as 'weak below threshold' — it's an upstream pipeline failure."""
    strategy = {"id": "s_zero"}
    profile = {"profile": "factor_rank_validation"}
    result = _evaluate_statistical_admission(
        strategy, profile,
        gate_payload={
            "wf_ic_ir": 0.0,
            "pkf_ic": 0.0,
            "bootstrap_ci_lower": 0.0,
            "param_sensitivity": 0.0,
        },
    )
    reasons = result.get("reasons") or []
    # All four metrics should be classified as missing despite being 0.0.
    for m in ("wf_ic_ir", "pkf_ic", "bootstrap_ci_lower", "param_sensitivity"):
        assert f"missing_{m}" in reasons, f"missing_{m} not in {reasons}"
        assert f"weak_{m}" not in reasons, (
            f"weak_{m} should not appear when the value is the placeholder 0.0"
        )
    eval_rows = result.get("gate_3_evaluation") or []
    for row in eval_rows:
        assert row["status"] == "missing"


# ---------------------------------------------------------------------------
# Weak path (R5.3)
# ---------------------------------------------------------------------------

def test_evaluate_statistical_admission_weak_emits_weak_reason_codes() -> None:
    strategy = {"id": "s_weak"}
    profile = {"profile": "factor_rank_validation"}
    # Pick values that are present_real but below their incubation thresholds
    # (default thresholds: wf_ic_ir_min=0.20, pkf_ic_min=0.01,
    # bootstrap_ci_lower_min=-0.01, param_sensitivity_max=0.35).
    result = _evaluate_statistical_admission(
        strategy, profile,
        gate_payload={
            "wf_ic_ir": 0.05,             # below 0.20 → weak
            "pkf_ic": 0.005,              # below 0.01 → weak
            "bootstrap_ci_lower": -0.05,  # below -0.01 → weak
            "param_sensitivity": 0.50,    # above 0.35 → weak (max)
        },
    )
    reasons = result.get("reasons") or []
    for m in ("wf_ic_ir", "pkf_ic", "bootstrap_ci_lower", "param_sensitivity"):
        assert f"weak_{m}" in reasons, f"weak_{m} not in {reasons}"
        assert f"missing_{m}" not in reasons
    eval_rows = result.get("gate_3_evaluation") or []
    for row in eval_rows:
        assert row["status"] == "weak"
        assert row["reason_code"] == f"weak_{row['metric']}"
    # Gate is not passed (any single weak metric prevents passing)
    assert result.get("passed") is False


# ---------------------------------------------------------------------------
# Pass path — regression guard (Property 3)
# ---------------------------------------------------------------------------

def test_evaluate_statistical_admission_pass_remains_unchanged() -> None:
    """Same input that used to pass before P1 must still pass after P1."""
    strategy = {"id": "s_pass"}
    profile = {"profile": "factor_rank_validation"}
    result = _evaluate_statistical_admission(
        strategy, profile,
        gate_payload={
            "wf_ic_ir": 0.40,
            "pkf_ic": 0.05,
            "bootstrap_ci_lower": 0.02,
            "param_sensitivity": 0.10,
        },
    )
    assert result.get("passed") is True
    # No weak / missing structured codes
    reasons = result.get("reasons") or []
    for r in reasons:
        assert not r.startswith("missing_"), r
        assert not r.startswith("weak_"), r
    eval_rows = result.get("gate_3_evaluation") or []
    for row in eval_rows:
        assert row["status"] == "pass"


# ---------------------------------------------------------------------------
# Mixed path: one missing + one pass + one weak
# ---------------------------------------------------------------------------

def test_evaluate_statistical_admission_mixed_status() -> None:
    strategy = {"id": "s_mixed"}
    profile = {"profile": "factor_rank_validation"}
    result = _evaluate_statistical_admission(
        strategy, profile,
        gate_payload={
            "wf_ic_ir": 0.40,            # pass
            # pkf_ic missing
            "bootstrap_ci_lower": -0.03, # weak
            "param_sensitivity": 0.10,   # pass
        },
    )
    reasons = result.get("reasons") or []
    assert "missing_pkf_ic" in reasons
    assert "weak_bootstrap_ci_lower" in reasons
    # The pass metrics should not appear in structured codes
    assert "missing_wf_ic_ir" not in reasons
    assert "weak_wf_ic_ir" not in reasons
    assert "missing_param_sensitivity" not in reasons
    assert "weak_param_sensitivity" not in reasons
    eval_rows = {row["metric"]: row for row in result.get("gate_3_evaluation") or []}
    assert eval_rows["wf_ic_ir"]["status"] == "pass"
    assert eval_rows["pkf_ic"]["status"] == "missing"
    assert eval_rows["bootstrap_ci_lower"]["status"] == "weak"
    assert eval_rows["param_sensitivity"]["status"] == "pass"


# ---------------------------------------------------------------------------
# derived=true propagation (Property 9, R5.4)
# ---------------------------------------------------------------------------

def test_gate3_evaluation_marks_derived_metric() -> None:
    """When a metric is supplied via the stop-gap inverse path
    (audit_source contains 'inverse'), gate_3_evaluation must mark
    derived=true so dashboards know it's a P1 stop-gap."""
    rows = _build_gate3_evaluation(
        wf_ic_ir=0.40,
        pkf_ic=0.05,
        bootstrap_ci_lower=0.02,
        param_sensitivity=0.10,
        thresholds={
            "walk_forward_ic_ir_min": 0.20,
            "purged_kfold_ic_min": 0.01,
            "bootstrap_ci_lower_min": -0.01,
            "param_sensitivity_max": 0.35,
        },
        payload={
            "metric_source_audit": {
                "param_sensitivity":
                    "backtest_metrics.parameter_perturbation_trade_stability_inverse",
                "wf_ic_ir": "validation_report.walk_forward",
            },
        },
    )
    by_metric = {r["metric"]: r for r in rows}
    assert by_metric["param_sensitivity"]["derived"] is True
    assert by_metric["wf_ic_ir"]["derived"] is False
    # Other metrics with no audit entry are non-derived by default.
    assert by_metric["pkf_ic"]["derived"] is False
    assert by_metric["bootstrap_ci_lower"]["derived"] is False


def test_statistical_gate_reads_validation_statistical_metrics_with_audit() -> None:
    result = asyncio.run(
        _run_statistical_gate(
            None,
            {"id": "s_statistical_metrics"},
            profile={"profile": "factor_rank_validation"},
            validation_report={
                "statistical_metrics": {
                    "wf_ic_ir": {
                        "value": 0.40,
                        "derived": True,
                        "source": "derived_from_walk_forward_mean_std",
                    },
                    "pkf_ic": {
                        "value": 0.05,
                        "derived": False,
                        "source": "real_purged_kfold",
                    },
                    "bootstrap_ci_lower": {
                        "value": 0.02,
                        "derived": True,
                        "source": "derived_from_bootstrap_mean_se",
                    },
                    "param_sensitivity": {
                        "value": 0.10,
                        "derived": True,
                        "source": "derived_from_parameter_perturbation_stability",
                    },
                },
            },
        )
    )

    assert result["passed"] is True
    assert result["metric_source_audit"]["wf_ic_ir"].startswith(
        "validation_report.statistical_metrics.wf_ic_ir"
    )
    by_metric = {row["metric"]: row for row in result["gate_3_evaluation"]}
    assert by_metric["wf_ic_ir"]["derived"] is True
    assert by_metric["bootstrap_ci_lower"]["derived"] is True
    assert by_metric["param_sensitivity"]["derived"] is True
    assert by_metric["pkf_ic"]["derived"] is False
