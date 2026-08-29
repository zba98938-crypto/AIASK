"""Promotion gate now owned by Strategy Factory (Phase 3).

The DSR-corrected promotion decision moved out of
``akshare_mcp.services.incubation_factory.promotion_gate``. The forward-return
*fetch* stayed with the host — that is data access — while the decision came
here. The statistic itself is not reimplemented: it is injectable, and otherwise
resolved from ``aiask-quant-core``, which Strategy Factory already depends on.

The property that matters most: the gate is conservative. Thin samples, an
unavailable statistic, and exceptions must all yield ``passed=False``. A
promotion must never be granted because a computation failed.
"""

from __future__ import annotations

import pytest

from strategy_factory.api.runtime import (
    PROMOTION_DSR_ENV,
    PromotionGate,
    PromotionGateVerdict,
    promotion_gate_enabled,
)


def _dsr(dsr: float = 0.9, *, available: bool = True):
    """Injectable stand-in for the deflated Sharpe statistic."""

    def _fn(_series, **_kwargs):
        return {
            "dsr": dsr,
            "observed_sharpe": 1.2,
            "effective_trials": 3.0,
            "available": available,
        }

    return _fn


SERIES = [0.01] * 40


# --- public surface ------------------------------------------------------------


def test_gate_is_reachable_from_the_public_api() -> None:
    from strategy_factory.api import runtime

    for name in ("PromotionGate", "PromotionGateVerdict", "promotion_gate_enabled"):
        assert name in runtime.__all__


def test_defaults_are_the_behaviour_contract() -> None:
    gate = PromotionGate()

    assert gate.dsr_min == 0.60
    assert gate.min_sample_size == 30
    assert gate.periods_per_year == 252.0


# --- the toggle ----------------------------------------------------------------


def test_toggle_defaults_to_off(monkeypatch) -> None:
    """Enabling DSR correction must be an explicit operator decision."""
    monkeypatch.delenv(PROMOTION_DSR_ENV, raising=False)
    assert promotion_gate_enabled() is False


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on", " On "])
def test_toggle_accepts_truthy_spellings(monkeypatch, value: str) -> None:
    monkeypatch.setenv(PROMOTION_DSR_ENV, value)
    assert promotion_gate_enabled() is True


@pytest.mark.parametrize("value", ["0", "false", "no", "off", "", "maybe"])
def test_toggle_rejects_everything_else(monkeypatch, value: str) -> None:
    monkeypatch.setenv(PROMOTION_DSR_ENV, value)
    assert promotion_gate_enabled() is False


# --- the decision --------------------------------------------------------------


def test_high_dsr_passes() -> None:
    verdict = PromotionGate().evaluate(SERIES, n_trials=5, dsr_fn=_dsr(0.9))

    assert verdict.passed is True
    assert verdict.eligible is True
    assert verdict.dsr == 0.9
    assert any("dsr_pass" in reason for reason in verdict.reasons)


def test_low_dsr_is_evaluated_and_rejected() -> None:
    verdict = PromotionGate().evaluate(SERIES, dsr_fn=_dsr(0.10))

    assert verdict.passed is False
    # Evaluated, just not good enough — distinct from "could not evaluate".
    assert verdict.eligible is True
    assert any("dsr_below_min" in reason for reason in verdict.reasons)


def test_the_threshold_is_inclusive() -> None:
    verdict = PromotionGate(dsr_min=0.60).evaluate(SERIES, dsr_fn=_dsr(0.60))
    assert verdict.passed is True


def test_a_custom_threshold_is_honoured() -> None:
    assert PromotionGate(dsr_min=0.95).evaluate(SERIES, dsr_fn=_dsr(0.90)).passed is False


# --- conservatism --------------------------------------------------------------


def test_thin_sample_is_not_eligible() -> None:
    verdict = PromotionGate().evaluate([0.01] * 29, dsr_fn=_dsr(0.99))

    assert verdict.passed is False
    assert verdict.eligible is False, "a thin sample is 'unevaluated', not 'rejected'"
    assert verdict.sample_size == 29
    assert any("insufficient_forward_samples" in reason for reason in verdict.reasons)


def test_an_empty_series_does_not_promote() -> None:
    for empty in ([], None):
        verdict = PromotionGate().evaluate(empty, dsr_fn=_dsr(0.99))
        assert verdict.passed is False
        assert verdict.sample_size == 0


def test_a_missing_statistic_does_not_promote(monkeypatch) -> None:
    monkeypatch.setattr(PromotionGate, "_resolve_dsr_fn", staticmethod(lambda: None))

    verdict = PromotionGate().evaluate(SERIES)

    assert verdict.passed is False
    assert verdict.eligible is False
    assert "dsr_fn_unavailable" in verdict.reasons


def test_an_exception_does_not_promote() -> None:
    def _boom(_series, **_kwargs):
        raise ValueError("statistics blew up")

    verdict = PromotionGate().evaluate(SERIES, dsr_fn=_boom)

    assert verdict.passed is False
    assert verdict.eligible is False
    assert any("dsr_exception:ValueError" in reason for reason in verdict.reasons)


def test_an_unavailable_result_does_not_promote() -> None:
    verdict = PromotionGate().evaluate(SERIES, dsr_fn=_dsr(0.99, available=False))

    assert verdict.passed is False
    assert verdict.eligible is False
    assert "dsr_not_available" in verdict.reasons
    # The computed numbers are still reported for diagnosis.
    assert verdict.dsr == 0.99


def test_a_none_result_does_not_promote() -> None:
    verdict = PromotionGate().evaluate(SERIES, dsr_fn=lambda _s, **_k: None)
    assert verdict.passed is False


# --- input hardening -----------------------------------------------------------


def test_non_finite_values_are_dropped_from_the_series() -> None:
    captured: dict = {}

    def _capture(series, **_kwargs):
        captured["n"] = len(series)
        return {"dsr": 0.9, "available": True}

    dirty = [0.01] * 40 + [float("nan"), float("inf"), None, "abc"]
    PromotionGate().evaluate(dirty, dsr_fn=_capture)

    assert captured["n"] == 40, "only finite values may reach the statistic"


def test_dropping_bad_values_can_make_a_sample_too_thin() -> None:
    """Sanitisation must not be hidden: 20 good values is still 20."""
    verdict = PromotionGate().evaluate(
        [0.01] * 20 + [float("nan")] * 20, dsr_fn=_dsr(0.99)
    )

    assert verdict.sample_size == 20
    assert verdict.eligible is False


def test_n_trials_is_floored_at_one() -> None:
    captured: dict = {}

    def _capture(_series, **kwargs):
        captured.update(kwargs)
        return {"dsr": 0.9, "available": True}

    PromotionGate().evaluate(SERIES, n_trials=0, dsr_fn=_capture)
    assert captured["n_trials"] == 1


def test_periods_per_year_is_floored_at_one() -> None:
    captured: dict = {}

    def _capture(_series, **kwargs):
        captured.update(kwargs)
        return {"dsr": 0.9, "available": True}

    PromotionGate(periods_per_year=0).evaluate(SERIES, dsr_fn=_capture)
    assert captured["periods_per_year"] >= 1.0


# --- verdict serialisation -----------------------------------------------------


def test_verdict_serialises_to_a_stable_shape() -> None:
    payload = PromotionGateVerdict(
        passed=True, eligible=True, reasons=["ok"], dsr=0.7, sample_size=40
    ).to_dict()

    assert payload["passed"] is True
    assert payload["reasons"] == ["ok"]
    assert payload["dsr"] == 0.7
    assert payload["sample_size"] == 40


def test_serialisation_snapshots_reasons() -> None:
    reasons = ["one"]
    payload = PromotionGateVerdict(False, False, reasons).to_dict()
    reasons.append("two")

    assert payload["reasons"] == ["one"]


# --- statistic resolution ------------------------------------------------------


def test_the_statistic_resolves_from_quant_core() -> None:
    """Not from a host package — that would reintroduce the dependency."""
    pytest.importorskip("aiask_quant_core._validation_support")

    assert PromotionGate._resolve_dsr_fn() is not None


def test_resolution_does_not_reach_for_a_host_package() -> None:
    import inspect

    import strategy_factory.domain.promotion_gate as module

    source = inspect.getsource(module)
    assert "akshare" not in source, "the gate must not resolve statistics from a host"


def test_an_injected_statistic_wins_over_resolution() -> None:
    """Injection is what keeps this testable without any provider present."""
    verdict = PromotionGate().evaluate(SERIES, dsr_fn=_dsr(0.99))
    assert verdict.dsr == 0.99
