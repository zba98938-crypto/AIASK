"""Economic-sense dimensions must not pay a failing check for being untestable.

Background
----------
Fourteen "unavailable" paths in ``EconomicSenseValidator`` returned a hardcoded
``score: 0.5`` — for every dimension, and across ``insufficient_data``,
``singular_matrix``, ``degenerate`` and ``except Exception`` alike. With the
observed scoring tiers that produced an inverted incentive:

    measured, economically meaningful (p < 0.05)  -> 1.0
    tested and NOT significant     (p >= 0.10)    -> 0.3
    could not be tested at all                    -> 0.5   <-- better than the
                                                              measurement above

and an exception additionally gifted 0.5. ``no_amount_data`` scored 0.5 while
its sibling ``zero_amount`` scored 0.3 — two flavours of "no data" on different
marks, which shows the 0.5 had no consistent basis.
"""

from __future__ import annotations

import asyncio

import numpy as np
import pandas as pd

from akshare_mcp.services.factor_mining_factory.validation.economic_sense import (
    EconomicSenseValidator,
)


def _short_series(n: int = 10) -> pd.Series:
    return pd.Series(np.linspace(1.0, 2.0, n))


def test_granger_insufficient_data_is_unmeasurable() -> None:
    validator = EconomicSenseValidator()
    result = validator._granger_causality(_short_series(), _short_series())

    assert result["available"] is False
    assert result["score"] is None, (
        "an untestable dimension must not collect half marks; "
        f"got {result['score']}"
    )
    assert result["reason"]


def test_capacity_without_amount_data_is_unmeasurable() -> None:
    validator = EconomicSenseValidator()
    result = validator._estimate_capacity(_short_series(), None, None)

    assert result["score"] is None
    assert result["reason"] == "no_amount_data"


def test_no_unavailable_path_returns_a_score() -> None:
    """The general form: nothing that says ``available: False`` may carry a score."""

    validator = EconomicSenseValidator()
    probes = [
        validator._granger_causality(_short_series(), _short_series()),
        validator._estimate_capacity(_short_series(), None, None),
        validator._forecast_decay(_short_series(), _short_series()),
        validator._assess_tradability(pd.Series([], dtype=float)),
    ]

    for payload in probes:
        if payload.get("available") is False:
            assert payload.get("score") is None, payload


def test_rule_based_judgment_discloses_that_it_is_a_prior() -> None:
    """The 'LLM judge' dimension is a heuristic that never consults an LLM."""

    # Worst case by construction: no hypothesis (+0 not awarded), no known
    # family (+0), expression longer than 100 chars (-0.1), no temporal
    # operator (-0.05). Floor = 0.5 - 0.1 - 0.05.
    payload = EconomicSenseValidator._rule_based_judgment(
        "custom_factor", "", "a" * 120
    )

    assert payload["mode"] == "rule_based"
    assert payload["basis"] == "heuristic_prior"
    assert payload["prior"] == 0.5
    assert payload["score"] == 0.35, (
        "worst-case input still scores the 0.35 floor — documented here so the "
        "number is not mistaken for a measurement"
    )


def test_a_factor_with_no_evidence_at_all_still_scores_above_zero() -> None:
    """The prior is the point: 0.35 is awarded for absence of evidence."""

    payload = EconomicSenseValidator._rule_based_judgment("x", "", "a" * 120)
    assert payload["score"] > 0.0
    assert "overly_complex_expression" in payload["concerns"]
    assert "no_temporal_smoothing" in payload["concerns"]


def test_llm_judgment_states_that_no_llm_was_consulted() -> None:
    validator = EconomicSenseValidator()
    payload = asyncio.run(
        validator._llm_economic_judgment("custom_factor", "some hypothesis", "a + b")
    )

    assert payload["llm_consulted"] is False
    assert "llm_provider_enabled" in payload


def test_partial_evidence_is_disclosed_and_cannot_pass() -> None:
    """Only tradability is measurable here, so evidence is incomplete."""

    validator = EconomicSenseValidator()
    report = asyncio.run(
        validator.validate(
            _short_series(),
            _short_series(),
            factor_name="custom_factor",
            factor_hypothesis="",
            factor_expression="",
        )
    )

    assert report.partial is True
    assert set(report.unmeasured_dimensions) >= {
        "causality",
        "capacity",
        "decay_forecast",
        "llm_judgment",
    }
    assert report.passed is False, (
        "a pass on partial evidence is the same defect as a pass on a "
        "fabricated 0.5"
    )
    assert report.overall_score is not None

    payload = report.to_dict()
    assert payload["partial"] is True
    assert payload["unmeasured_dimensions"] == report.unmeasured_dimensions
