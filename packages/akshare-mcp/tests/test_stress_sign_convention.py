"""``stress_test_portfolio`` must not rewrite the upstream stress result.

Background
----------
``risk_model.stress_test`` documents the contract that the admission gate
depends on: *"negative means the portfolio lost value, matching the gate
comparison ``stress_loss_percent <= -25.0``"*.

``stress_test_portfolio`` wrapped that call and then rewrote the result:

    result["loss_percent"] = abs(raw_value)          # -24.0 -> +24.0
    if abs(raw_value) <= 1: raw_value *= 100         # -0.4  -> +40.0

The first line flipped the sign, so the gate's ``<= -25.0`` comparison could
never fire on any value this tool produced. The second line fabricated
magnitude, turning a 0.4% loss into a reported 40% loss.

These tests call the registered tool and compare against the upstream function
directly, so they fail if either rewrite returns.
"""

from __future__ import annotations

import asyncio

from akshare_mcp.services.risk_model import risk_model
from akshare_mcp.tools import portfolio as portfolio_tools


class _FakeMcp:
    def __init__(self) -> None:
        self.tools: dict = {}

    def tool(self):
        def _register(fn):
            self.tools[fn.__name__] = fn
            return fn

        return _register


def _tool():
    mcp = _FakeMcp()
    portfolio_tools.register(mcp)
    return mcp.tools["stress_test_portfolio"]


def _call(holdings, **kwargs):
    return asyncio.run(_tool()(holdings=holdings, **kwargs))


def test_reported_loss_matches_the_upstream_sign_and_magnitude() -> None:
    """beta 1.2 against a -20% shock is a 24% loss, reported as negative."""

    holdings = [{"code": "600519", "value": 100_000.0, "beta": 1.2}]
    upstream = risk_model.stress_test(holdings, scenario="market_crash")
    result = _call([dict(h) for h in holdings], scenarios=["market_crash"])

    assert result["success"] is True, result.get("error")
    reported = result["data"]["stress_tests"]["market_crash"]

    assert upstream["loss_percent"] == -24.0, upstream
    assert reported["loss_percent"] == upstream["loss_percent"], (
        "the tool must pass the upstream loss through unchanged; "
        f"upstream said {upstream['loss_percent']} and the tool said "
        f"{reported['loss_percent']}"
    )
    assert reported["loss_percent"] < 0, "a loss is negative, per the gate contract"


def test_small_loss_is_not_inflated_by_a_hundred() -> None:
    """beta 0.02 against -20% is a 0.4% loss — not 40%."""

    holdings = [{"code": "600519", "value": 100_000.0, "beta": 0.02}]
    upstream = risk_model.stress_test(holdings, scenario="market_crash")
    result = _call([dict(h) for h in holdings], scenarios=["market_crash"])

    reported = result["data"]["stress_tests"]["market_crash"]

    assert upstream["loss_percent"] == -0.4, upstream
    assert reported["loss_percent"] == upstream["loss_percent"], (
        "a sub-1% loss was previously multiplied by 100 by the "
        "`abs(raw) <= 1` heuristic; reported "
        f"{reported['loss_percent']} instead of {upstream['loss_percent']}"
    )


def test_unmeasurable_scenario_stays_unmeasurable() -> None:
    """No beta means no estimable loss — ``None`` plus a reason, not 0.0."""

    holdings = [{"code": "600519", "value": 100_000.0}]
    result = _call(holdings, scenarios=["market_crash"])

    assert result["success"] is True, result.get("error")
    reported = result["data"]["stress_tests"]["market_crash"]

    assert reported["loss_percent"] is None
    assert reported["measurable"] is False
    assert reported["reason"] == "no_position_sensitivity"


def test_response_states_the_sign_convention_and_any_assumed_notional() -> None:
    holdings = [{"code": "600519", "weight": 0.5, "beta": 1.0}]
    result = _call(holdings, scenarios=["market_crash"])
    data = result["data"]

    assert data["loss_sign_convention"] == "negative means loss"
    assert data["notional_assumed"] is True
    assert data["assumed_notional"] == 1_000_000.0

    sized = _call(
        [{"code": "600519", "value": 100_000.0, "beta": 1.0}],
        scenarios=["market_crash"],
    )
    assert sized["data"]["notional_assumed"] is False
    assert sized["data"]["assumed_notional"] is None
