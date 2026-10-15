"""The provisional-incubation risk gate must fail closed on a missing measurement.

Context
-------
``maybe_grant_provisional_incubation`` reads three risk ceilings:

    var_percent > 4.5   or   cvar_percent > 6.5   or   stress_loss_percent <= -25.0

These are read through ``_required_metric``, which returns ``None`` for an
absent value so the caller denies. That is the correct shape, and this test
exists because the shape is easy to lose: akshare-mcp carries a second copy of
this function that reads the same ceilings through ``safe_metric_value``
(missing -> 0.0), under which all three comparisons are False and a strategy
with **no risk data at all** passes every risk ceiling. The control group below
is what makes this test non-vacuous: without it, a denial could come from any
earlier precondition rather than from the risk check itself.
"""

from __future__ import annotations

import pytest

from strategy_factory.application.quality_reporting import (
    maybe_grant_provisional_incubation,
)

# These four literals are the minimum that reaches the risk branch. They were
# derived from the real preconditions, not guessed:
#   is_factory_provisional_candidate  -> tags must contain "factory"
#   is_provisional_technical_strategy -> "ma_cross" is in the technical set
#   has_only_statistical_gate_failures -> every reason_code must carry a
#                                         statistical prefix
#   _count_statistical_checks_passed  -> needs >= 2 passing checks
_STRATEGY = {"strategy_type": "ma_cross", "tags": ["factory"]}
_GATE = {
    "passed": False,
    "reasons": ["walk_forward_ic_ir below min"],
    "wf_ic_ir": 1.0,
    "pkf_ic": 1.0,
}
_BACKTEST = {"sharpe_ratio": 1.2, "max_drawdown": 0.10, "trades_count": 40}
_HEALTHY_RISK = {
    "var_percent": 1.0,
    "cvar_percent": 1.5,
    "stress_loss_percent": -8.0,
}


def _gate(risk_report):
    return maybe_grant_provisional_incubation(
        _STRATEGY,
        _GATE,
        validation_report={},
        risk_report=risk_report,
        backtest_metrics=_BACKTEST,
    )


def test_control_group_reaches_the_risk_branch() -> None:
    """Without this passing, every denial below could be about something else."""

    result = _gate(dict(_HEALTHY_RISK))
    assert result.get("passed") is True, (
        "the control group must be granted so that the denials in the other "
        "tests are attributable to the risk check rather than to an earlier "
        f"precondition; got {result.get('reasons')}"
    )


@pytest.mark.parametrize(
    "key",
    ["var_percent", "cvar_percent", "stress_loss_percent"],
)
def test_explicit_none_is_denied(key: str) -> None:
    risk = {**_HEALTHY_RISK, key: None}
    assert _gate(risk).get("passed") is False


@pytest.mark.parametrize(
    "risk",
    [
        {"stress_loss_percent": -8.0},  # var and cvar keys entirely absent
        {"var_percent": 1.0},  # cvar and stress absent
        {},  # nothing measured at all
    ],
)
def test_absent_keys_are_denied(risk: dict) -> None:
    assert _gate(risk).get("passed") is False


@pytest.mark.parametrize(
    "override",
    [
        {"var_percent": 9.9},
        {"cvar_percent": 9.9},
        {"stress_loss_percent": -30.0},
    ],
)
def test_breaching_values_are_denied(override: dict) -> None:
    assert _gate({**_HEALTHY_RISK, **override}).get("passed") is False


def test_measured_zero_still_passes() -> None:
    """A genuine measurement of 0.0 is not the same as an absent one.

    This is also why the code fix alone does not repair history: the eight
    stored reports that read ``var_percent = 0.0`` were fabricated by the old
    ``.get(..., 0.0)``, and they still pass this ceiling today. Marking them
    (the provenance migration) is a separate, necessary step.
    """

    assert _gate({**_HEALTHY_RISK, "var_percent": 0.0}).get("passed") is True
