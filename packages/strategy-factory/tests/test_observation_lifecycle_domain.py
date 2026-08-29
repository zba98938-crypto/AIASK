"""Observation lifecycle policy now owned by Strategy Factory (Phase 3).

The daily observe / promote / halt decision moved out of
``akshare_mcp.services.incubation_factory`` because it is lifecycle *policy*, not
data access. It moved unchanged — stdlib only, no database, pure ``decide()``.

The host keeps a re-export shim so existing imports still work; these tests own
the behavioural contract on this side of the boundary.
"""

from __future__ import annotations

import pytest

from strategy_factory.api.runtime import (
    OBSERVATION_DECISIONS,
    ObservationLifecycleDecision,
    ObservationLifecyclePolicy,
)


def _inputs(**overrides):
    """Baseline inputs that produce ``promote`` unless overridden."""
    base = {
        "primary_skill_lcb": 0.05,
        "recent_primary_skill_lcb": 0.03,
        "stability_gap": 0.02,
        "coverage_ratio": 0.70,
        "primary_n": 50,
    }
    base.update(overrides)
    return base


# --- public surface ------------------------------------------------------------


def test_policy_is_reachable_from_the_public_api() -> None:
    from strategy_factory.api import runtime

    for name in ("ObservationLifecyclePolicy", "ObservationLifecycleDecision"):
        assert name in runtime.__all__


def test_decision_vocabulary_is_closed() -> None:
    assert OBSERVATION_DECISIONS == ("observe", "promote", "halt")


def test_defaults_match_the_documented_thresholds() -> None:
    """These defaults are the behaviour contract; changing one changes outcomes."""
    policy = ObservationLifecyclePolicy()

    assert policy.min_primary_n == 10
    assert policy.halt_recent_skill_lcb == -0.03
    assert policy.halt_stability_gap == 0.10
    assert policy.observe_min_coverage == 0.25
    assert policy.promote_skill_lcb == 0.02
    assert policy.promote_min_coverage == 0.60
    assert policy.promote_max_stability_gap == 0.05
    assert policy.regime_min_n == 20
    assert policy.regime_enabled is False


# --- decision precedence -------------------------------------------------------


def test_healthy_metrics_promote() -> None:
    decision = ObservationLifecyclePolicy().decide(**_inputs())

    assert decision.decision == "promote"
    assert any("promote:skill_lcb" in reason for reason in decision.reasons)


def test_insufficient_sample_observes_regardless_of_metrics() -> None:
    """Sample size is checked first: neither kill nor promote on thin data."""
    decision = ObservationLifecyclePolicy().decide(**_inputs(primary_n=9))

    assert decision.decision == "observe"
    assert decision.reasons == ["insufficient_samples:9<10"]


def test_negative_recent_skill_halts() -> None:
    decision = ObservationLifecyclePolicy().decide(
        **_inputs(recent_primary_skill_lcb=-0.05)
    )

    assert decision.decision == "halt"
    assert any("recent_skill_lcb_negative" in reason for reason in decision.reasons)


def test_broken_stability_halts() -> None:
    decision = ObservationLifecyclePolicy().decide(**_inputs(stability_gap=0.20))

    assert decision.decision == "halt"
    assert any("stability_gap_high" in reason for reason in decision.reasons)


def test_low_coverage_observes() -> None:
    decision = ObservationLifecyclePolicy().decide(**_inputs(coverage_ratio=0.10))

    assert decision.decision == "observe"
    assert any("coverage_low" in reason for reason in decision.reasons)


def test_halt_takes_precedence_over_low_coverage() -> None:
    """A negative signal outranks 'not enough data yet'."""
    decision = ObservationLifecyclePolicy().decide(
        **_inputs(recent_primary_skill_lcb=-0.05, coverage_ratio=0.10)
    )
    assert decision.decision == "halt"


def test_mediocre_metrics_fall_through_to_observe() -> None:
    decision = ObservationLifecyclePolicy().decide(
        **_inputs(primary_skill_lcb=0.01, recent_primary_skill_lcb=0.001)
    )

    assert decision.decision == "observe"
    assert "observe_default" in decision.reasons


@pytest.mark.parametrize(
    "overrides",
    [
        {},
        {"primary_n": 1},
        {"recent_primary_skill_lcb": -1.0},
        {"stability_gap": 1.0},
        {"coverage_ratio": 0.0},
        {"primary_skill_lcb": 0.0},
    ],
)
def test_every_decision_is_in_the_vocabulary(overrides) -> None:
    decision = ObservationLifecyclePolicy().decide(**_inputs(**overrides))
    assert decision.decision in OBSERVATION_DECISIONS


# --- promotion requires all four conditions ------------------------------------


@pytest.mark.parametrize(
    "overrides",
    [
        {"primary_skill_lcb": 0.02},          # not strictly above the threshold
        {"recent_primary_skill_lcb": 0.0},    # must be strictly positive
        {"stability_gap": 0.06},              # above promote_max_stability_gap
        {"coverage_ratio": 0.59},             # below promote_min_coverage
    ],
)
def test_promotion_needs_every_condition(overrides) -> None:
    decision = ObservationLifecyclePolicy().decide(**_inputs(**overrides))
    assert decision.decision != "promote", f"{overrides} must not promote"


# --- regime gate ---------------------------------------------------------------


_NEGATIVE_REGIME = {"trend": {"up": {"n": 30, "recent_skill_lcb": -0.02}}}


def test_regime_gate_is_off_by_default() -> None:
    decision = ObservationLifecyclePolicy().decide(
        **_inputs(), hit_rate_by_regime=_NEGATIVE_REGIME
    )

    assert decision.decision == "promote", "the gate must not apply unless enabled"
    # The regime data was still evaluated, and reported as such.
    assert decision.regime_evaluated is True


def test_enabled_regime_gate_halts_on_a_negative_label() -> None:
    decision = ObservationLifecyclePolicy(regime_enabled=True).decide(
        **_inputs(), hit_rate_by_regime=_NEGATIVE_REGIME
    )

    assert decision.decision == "halt"
    assert decision.regime_negative_labels == ("trend:up",)
    assert any("regime_skill_negative" in reason for reason in decision.reasons)


def test_labels_below_the_sample_floor_are_ignored() -> None:
    thin = {"trend": {"up": {"n": 19, "recent_skill_lcb": -0.5}}}

    decision = ObservationLifecyclePolicy(regime_enabled=True).decide(
        **_inputs(), hit_rate_by_regime=thin
    )

    assert decision.decision == "promote"
    assert decision.regime_evaluated is False


def test_regime_falls_back_to_overall_skill_when_recent_is_absent() -> None:
    without_recent = {"trend": {"up": {"n": 30, "skill_lcb": -0.02}}}

    decision = ObservationLifecyclePolicy(regime_enabled=True).decide(
        **_inputs(), hit_rate_by_regime=without_recent
    )

    assert decision.decision == "halt"


def test_positive_regime_records_cross_regime_evidence() -> None:
    positive = {"trend": {"up": {"n": 30, "recent_skill_lcb": 0.05}}}

    decision = ObservationLifecyclePolicy(regime_enabled=True).decide(
        **_inputs(), hit_rate_by_regime=positive
    )

    assert decision.decision == "promote"
    assert "regime_cross_positive" in decision.reasons


def test_effective_n_is_accepted_as_the_sample_field() -> None:
    decision = ObservationLifecyclePolicy(regime_enabled=True).decide(
        **_inputs(),
        hit_rate_by_regime={"trend": {"up": {"effective_n": 30, "recent_skill_lcb": -0.1}}},
    )
    assert decision.decision == "halt"


# --- input hardening -----------------------------------------------------------


@pytest.mark.parametrize("bad", [None, "abc", float("nan"), float("inf")])
def test_non_numeric_metrics_do_not_raise(bad) -> None:
    """Sanitisation matters: a NaN must not silently satisfy a threshold."""
    decision = ObservationLifecyclePolicy().decide(**_inputs(primary_skill_lcb=bad))
    assert decision.decision in OBSERVATION_DECISIONS


def test_nan_skill_does_not_promote() -> None:
    decision = ObservationLifecyclePolicy().decide(
        **_inputs(primary_skill_lcb=float("nan"))
    )
    assert decision.decision != "promote"


def test_empty_regime_payload_is_tolerated() -> None:
    for value in (None, {}, {"trend": None}, {"trend": {}}):
        decision = ObservationLifecyclePolicy(regime_enabled=True).decide(
            **_inputs(), hit_rate_by_regime=value
        )
        assert decision.decision == "promote"


# --- decision serialisation ----------------------------------------------------


def test_decision_serialises_to_a_stable_shape() -> None:
    payload = ObservationLifecycleDecision("halt", ["why"], True, ("a:b",)).to_dict()

    assert payload == {
        "decision": "halt",
        "reasons": ["why"],
        "regime_evaluated": True,
        "regime_negative_labels": ["a:b"],
    }


def test_serialisation_copies_rather_than_aliases() -> None:
    reasons = ["one"]
    decision = ObservationLifecycleDecision("observe", reasons)
    payload = decision.to_dict()
    reasons.append("two")

    assert payload["reasons"] == ["one"], "to_dict must snapshot, not alias"


# --- purity --------------------------------------------------------------------


def test_the_module_stays_pure() -> None:
    import inspect

    import strategy_factory.domain.observation_lifecycle as module

    source = inspect.getsource(module)
    for forbidden in ("import sqlite3", "await ", "async def", "akshare", "os.getenv"):
        assert forbidden not in source, (
            f"observation_lifecycle must stay pure; found {forbidden!r}"
        )
