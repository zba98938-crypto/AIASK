"""Shared statistics for direction hit-rate scorecards.

Kept in one place because every gate below must agree on the same definitions:
Wilson lower bounds, block bootstrap, and profit factor. A second, slightly
different implementation of any of these has already produced contradictory
numbers in this project.
"""

from __future__ import annotations

import math
from typing import Any, Iterable, Optional, Sequence

Z_95 = 1.959963984540054


def wilson_interval(hits: int, n: int, *, z: float = Z_95) -> tuple[Optional[float], Optional[float]]:
    """Two-sided Wilson score interval; ``(None, None)`` when n is empty."""
    if n <= 0:
        return None, None
    phat = hits / n
    denominator = 1.0 + z * z / n
    centre = phat + z * z / (2 * n)
    margin = z * math.sqrt((phat * (1 - phat) + z * z / (4 * n)) / n)
    return (centre - margin) / denominator, (centre + margin) / denominator


def wilson_lcb(hits: int, n: int, *, z: float = Z_95) -> Optional[float]:
    return wilson_interval(hits, n, z=z)[0]


def mean(values: Sequence[float]) -> Optional[float]:
    return sum(values) / len(values) if values else None


def stdev(values: Sequence[float]) -> Optional[float]:
    if len(values) < 2:
        return None
    centre = mean(values)
    assert centre is not None
    variance = sum((value - centre) ** 2 for value in values) / (len(values) - 1)
    return math.sqrt(variance)


def block_bootstrap_lcb(
    values: Sequence[float],
    *,
    block_size: int = 20,
    iterations: int = 2000,
    seed: int = 1729,
    z: float = Z_95,
) -> tuple[Optional[float], Optional[float]]:
    """Moving-block bootstrap lower bound of the mean.

    ``values`` must already be one observation per independent unit (trading
    day), in time order. Overlapping horizon windows are handled by making the
    block at least as long as the horizon.
    """
    n = len(values)
    if n == 0:
        return None, None
    block = max(1, min(int(block_size or 1), n))
    if block >= n:
        # One block covers everything: the bootstrap collapses to the sample
        # mean, which is honest but has no spread to report.
        point = mean(values)
        return point, point
    # Deterministic LCG: no dependence on the global RNG, reproducible in tests.
    state = seed & 0xFFFFFFFF

    def _next() -> float:
        nonlocal state
        state = (1103515245 * state + 12345) & 0x7FFFFFFF
        return state / 0x7FFFFFFF

    blocks = n - block + 1
    means: list[float] = []
    for _ in range(max(1, iterations)):
        total = 0.0
        taken = 0
        while taken < n:
            start = int(_next() * blocks) % blocks
            chunk = values[start:start + block]
            total += sum(chunk)
            taken += len(chunk)
        means.append(total / taken if taken else 0.0)
    means.sort()
    index = max(0, min(len(means) - 1, int((0.025) * len(means))))
    point = mean(values)
    spread = stdev(means)
    if spread is None:
        return point, None
    return means[index], point


def profit_factor(net_returns: Iterable[float]) -> Optional[float]:
    """Gross profit / gross loss on net returns. ``None`` when there is no loss side.

    Returning a large number for "no losing trade" would be a fabricated value;
    callers must treat ``None`` as unmeasurable and say so.
    """
    gross_profit = 0.0
    gross_loss = 0.0
    seen = False
    for value in net_returns:
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        if not math.isfinite(number):
            continue
        seen = True
        if number > 0:
            gross_profit += number
        else:
            gross_loss += -number
    if not seen or gross_loss == 0:
        return None
    return gross_profit / gross_loss


def required_hits_for_lcb(n: int, threshold: float, *, z: float = Z_95) -> Optional[int]:
    """Smallest hit count whose Wilson LCB clears ``threshold`` at sample size n."""
    if n <= 0:
        return None
    for hits in range(n + 1):
        lcb = wilson_lcb(hits, n, z=z)
        if lcb is not None and lcb > threshold:
            return hits
    return None


def binomial_pass_probability(true_rate: float, n: int, threshold: float, *, z: float = Z_95) -> Optional[float]:
    """P(Wilson LCB > threshold | true rate), used for expectation management."""
    hits_needed = required_hits_for_lcb(n, threshold, z=z)
    if hits_needed is None:
        return None
    if true_rate <= 0:
        return 0.0 if hits_needed > 0 else 1.0
    if true_rate >= 1:
        return 1.0
    # Exact-ish upper tail via the incomplete beta identity, computed by summing
    # the tail terms (n is small here: hundreds).
    def _log_comb(a: int, b: int) -> float:
        return math.lgamma(a + 1) - math.lgamma(b + 1) - math.lgamma(a - b + 1)

    log_p = math.log(true_rate)
    log_q = math.log(1 - true_rate)
    total = 0.0
    for hits in range(hits_needed, n + 1):
        log_term = _log_comb(n, hits) + hits * log_p + (n - hits) * log_q
        total += math.exp(log_term)
    return min(1.0, max(0.0, total))


__all__ = [
    "Z_95",
    "binomial_pass_probability",
    "block_bootstrap_lcb",
    "mean",
    "profit_factor",
    "required_hits_for_lcb",
    "stdev",
    "wilson_interval",
    "wilson_lcb",
]
