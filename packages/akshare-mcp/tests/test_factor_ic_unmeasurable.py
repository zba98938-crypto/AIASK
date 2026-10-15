"""An IC that was never computed must not read as a computed zero.

`pearsonr`/`spearmanr` return NaN when an input array is constant — a factor with
no cross-sectional variation has no correlation to measure. Both IC helpers
converted that NaN to ``0.0`` (with ``p_value = 1.0``) and still reported the full
``sample_size``, so the record was indistinguishable from a real measurement of
zero information. Nine such warnings appeared in the first factor-mining cycle
over the expanded universe.

These are behaviour assertions on the returned contract, not on the source.
"""

from __future__ import annotations

import pytest

from akshare_mcp.services.factor_analysis import FactorAnalyzer
from aiask_quant_core.factor_calculator.analysis import AnalysisFactorsMixin


def _varying(n: int = 60):
    return [float(i) for i in range(n)], [float((i * 7) % 13) for i in range(n)]


def _constant(n: int = 60):
    return [3.0] * n, [float(i % 11) for i in range(n)]


@pytest.mark.parametrize(
    ("call", "expected_reason"),
    [
        (lambda f, r: FactorAnalyzer.calculate_ic(f, r), "undefined_correlation"),
        (
            lambda f, r: FactorAnalyzer.calculate_ic_dual(f, r, enable_neutralization=False),
            "undefined_correlation",
        ),
        (
            lambda f, r: AnalysisFactorsMixin.calculate_factor_ic(f, r),
            "undefined_correlation",
        ),
    ],
)
def test_a_constant_factor_has_no_measurable_ic(call, expected_reason) -> None:
    factor, returns = _constant()
    result = call(factor, returns)

    assert result["measurable"] is False
    assert result["reason"] == expected_reason
    ic_keys = [key for key in ("ic", "normal_ic") if key in result]
    assert ic_keys, "the contract must expose an IC field"
    for key in ic_keys:
        assert result[key] is None, (
            f"{key} is {result[key]!r}; an undefined correlation must be None, "
            "not a number that reads as a measurement"
        )
    # The sample was examined, so its size is still reported — that is data, not a
    # verdict, and dropping it would hide an input-quality problem.
    assert result["sample_size"] == len(factor)


@pytest.mark.parametrize(
    "call",
    [
        lambda f, r: FactorAnalyzer.calculate_ic(f, r),
        lambda f, r: FactorAnalyzer.calculate_ic_dual(f, r, enable_neutralization=False),
        lambda f, r: AnalysisFactorsMixin.calculate_factor_ic(f, r),
    ],
)
def test_a_varying_factor_still_produces_a_number(call) -> None:
    factor, returns = _varying()
    result = call(factor, returns)

    assert result["measurable"] is True
    assert result["reason"] is None
    value = result.get("ic") if "ic" in result else result.get("normal_ic")
    assert isinstance(value, float)


@pytest.mark.parametrize(
    ("call", "floor"),
    [
        # Each helper documents its own minimum sample: 2, 3 and 10 points
        # respectively. The test uses one fewer than each floor rather than a
        # single arbitrary size, so it cannot pass by accident on the wrong one.
        (lambda f, r: FactorAnalyzer.calculate_ic(f, r), 2),
        (lambda f, r: FactorAnalyzer.calculate_ic_dual(f, r, enable_neutralization=False), 3),
        (lambda f, r: AnalysisFactorsMixin.calculate_factor_ic(f, r), 10),
    ],
)
def test_too_few_samples_is_reported_as_unmeasurable(call, floor: int) -> None:
    n = floor - 1
    result = call([float(i) for i in range(n)], [float(i) * 0.1 for i in range(n)])

    assert result["measurable"] is False
    assert result["reason"] == "insufficient_samples"
    assert result.get("ic", result.get("normal_ic")) is None


def test_a_length_mismatch_is_still_an_error_for_dual_ic() -> None:
    """Only the unmeasurable cases changed; a caller bug must still raise."""

    with pytest.raises(ValueError):
        FactorAnalyzer.calculate_ic_dual([1.0, 2.0, 3.0], [0.1, 0.2])


def test_a_length_mismatch_is_an_unmeasurable_result_for_the_mixin() -> None:
    """The mixin never raised; it reported. It now reports the reason too."""

    result = AnalysisFactorsMixin.calculate_factor_ic([1.0, 2.0, 3.0], [0.1, 0.2])
    assert result["measurable"] is False
    assert result["reason"] == "length_mismatch"
    assert result["ic"] is None
