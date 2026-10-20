"""Unmeasurable inputs must not be reported as measured zeros.

These call the real functions and assert on their return values.
"""

from __future__ import annotations

import asyncio

import pandas as pd
import pytest

from akshare_mcp.services.llm_alpha import LLMAlphaMiner
from akshare_mcp.tools.managers._risk_manager_var import (
    _handle_calculate_var,
    _returns_by_date,
)


def _ok(payload, **kwargs):
    return {"success": True, "data": payload, **kwargs}


def _fail(message, **kwargs):
    return {"success": False, "error": message, **kwargs}


class _FakeDb:
    """Serves klines per code; a code mapped to [] has no usable history."""

    def __init__(self, series: dict[str, list[dict]]) -> None:
        self._series = series

    async def get_klines(self, code, start_date=None, end_date=None, limit=None):
        return list(self._series.get(code, []))


def _bars(dates: list[str], start: float = 10.0) -> list[dict]:
    return [
        {"date": day, "close": start + index, "open": start, "high": start, "low": start}
        for index, day in enumerate(dates)
    ]


def test_returns_are_keyed_by_trade_date() -> None:
    series = _returns_by_date(_bars(["2026-01-02", "2026-01-05", "2026-01-06"]))
    assert list(series.keys()) == ["2026-01-05", "2026-01-06"]


def test_var_refuses_when_a_constituent_has_no_history() -> None:
    """A missing holding must not be dropped and the rest renormalised to 100%."""
    db = _FakeDb(
        {
            "AAA": _bars(["2026-01-02", "2026-01-05", "2026-01-06", "2026-01-07"]),
            "BBB": [],
        }
    )
    kwargs = {"codes": ["AAA", "BBB"], "weights": [0.9, 0.1], "portfolio_value": 1_000_000}
    result = asyncio.run(_handle_calculate_var(db=db, kwargs=kwargs, ok=_ok, fail=_fail))

    assert result["success"] is False
    assert result["error"] == "missing_constituents"
    assert result["missing_codes"] == ["BBB"]


def test_var_aligns_on_common_trading_dates() -> None:
    db = _FakeDb(
        {
            "AAA": _bars(["2026-01-02", "2026-01-05", "2026-01-06", "2026-01-07"]),
            "BBB": _bars(["2026-01-06", "2026-01-07"], start=20.0),
        }
    )
    kwargs = {"codes": ["AAA", "BBB"], "weights": [0.5, 0.5], "portfolio_value": 1_000_000}
    result = asyncio.run(_handle_calculate_var(db=db, kwargs=kwargs, ok=_ok, fail=_fail))

    assert result["success"] is True
    payload = result["data"]
    # Only 2026-01-07 is a return date shared by both series.
    assert payload["observations"] == 1
    assert payload["aligned_from"] == "2026-01-07"
    assert payload["aligned_to"] == "2026-01-07"


def test_insufficient_samples_are_not_a_measured_zero_ic() -> None:
    miner = LLMAlphaMiner()
    factor = pd.Series([1.0, 2.0, 3.0, 4.0, 5.0])
    returns = pd.Series([0.01, 0.02, 0.03, 0.04, 0.05])

    evaluation = miner.evaluate_factor(factor, returns)

    assert evaluation["ic"] is None
    assert evaluation["unmeasurable_reason"] == "insufficient_samples"
    assert evaluation["sample_size"] == 5


def test_formula_scoring_treats_insufficient_samples_as_unscored() -> None:
    """A short namespace must yield None, not 0.0, so it is not ranked."""
    miner = LLMAlphaMiner()
    namespace = pd.DataFrame({"close": [1.0, 2.0, 3.0, 4.0, 5.0]})
    forward_returns = pd.Series([0.01, 0.02, 0.03, 0.04, 0.05])

    assert miner._safe_formula_ic("close.pct_change(1)", namespace, forward_returns) is None


def test_a_negative_ic_factor_gets_a_negative_combination_weight() -> None:
    """abs(IC) weighting gave a reverse factor the same weight as a forward one."""
    miner = LLMAlphaMiner()
    returns = pd.Series([0.01, -0.01] * 60)
    forward_factor = pd.Series(list(returns))
    reverse_factor = -forward_factor

    weights = miner.optimize_factor_combination(
        {"forward": forward_factor, "reverse": reverse_factor},
        returns,
        method="ic_weight",
    )

    assert weights["forward"] > 0
    assert weights["reverse"] < 0


def test_get_top_factors_does_not_rank_unmeasurable_factors_as_zero() -> None:
    miner = LLMAlphaMiner()
    returns = pd.Series([0.01, -0.01] * 60)
    good = pd.Series(list(returns))
    too_short = pd.Series([1.0, 2.0, 3.0])

    ranked = miner.get_top_factors({"good": good, "too_short": too_short}, returns, top_n=2)

    scored = {item["name"]: item for item in ranked}
    assert scored["good"]["score"] is not None
    assert scored["too_short"]["score"] is None
    assert scored["too_short"]["unmeasurable_reason"] == "insufficient_samples"
    assert ranked[0]["name"] == "good"
