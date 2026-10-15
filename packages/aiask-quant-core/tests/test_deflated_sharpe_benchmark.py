"""An absent benchmark must not become a zero benchmark.

``deflated_sharpe_ratio`` typed ``benchmark_sharpe: float = 0.0`` and read it
back as ``float(benchmark_sharpe or 0.0)``. Both spellings collapse "nobody
supplied a benchmark" into "the benchmark Sharpe is 0.0", so
``psr_vs_benchmark`` always reported a probability of beating zero — a
comparison against a reference that was never chosen, presented as a
measurement. ``validation.py`` did the same with
``mt_cfg.get("benchmark_sharpe", 0.0) or 0.0``.
"""

from __future__ import annotations

import numpy as np

from aiask_quant_core._validation_support import deflated_sharpe_ratio


def _returns(n: int = 250, seed: int = 5) -> np.ndarray:
    return np.random.default_rng(seed).normal(0.0008, 0.012, size=n)


def test_absent_benchmark_is_reported_as_absent() -> None:
    result = deflated_sharpe_ratio(_returns())

    assert result["benchmark_supplied"] is False
    assert result["benchmark_sharpe"] is None, (
        "an unsupplied benchmark must not surface as 0.0; "
        f"got {result['benchmark_sharpe']!r}"
    )
    assert result["psr_vs_benchmark"] is None
    assert result["psr_vs_benchmark_reason"] == "benchmark_not_supplied"
    # The DSR itself does not depend on a benchmark, so it is still produced.
    assert result["dsr"] is not None


def test_explicit_zero_benchmark_is_a_supplied_value() -> None:
    result = deflated_sharpe_ratio(_returns(), benchmark_sharpe=0.0)

    assert result["benchmark_supplied"] is True
    assert result["benchmark_sharpe"] == 0.0
    assert result["psr_vs_benchmark"] is not None
    assert result["psr_vs_benchmark_reason"] is None


def test_benchmark_value_changes_the_comparison() -> None:
    low = deflated_sharpe_ratio(_returns(), benchmark_sharpe=0.0)
    high = deflated_sharpe_ratio(_returns(), benchmark_sharpe=2.0)

    assert high["psr_vs_benchmark"] < low["psr_vs_benchmark"], (
        "a demanding benchmark must lower the probability of beating it"
    )


def test_none_and_zero_do_not_produce_the_same_payload() -> None:
    """The regression, stated directly."""

    unsupplied = deflated_sharpe_ratio(_returns())
    zero = deflated_sharpe_ratio(_returns(), benchmark_sharpe=0.0)

    assert unsupplied["psr_vs_benchmark"] != zero["psr_vs_benchmark"]
    assert unsupplied["benchmark_sharpe"] != zero["benchmark_sharpe"]
