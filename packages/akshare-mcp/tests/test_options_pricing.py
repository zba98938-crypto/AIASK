from __future__ import annotations

from datetime import datetime, timedelta
import math

import pytest

from akshare_mcp.services.options_pricing import OptionsPricing


def test_black_scholes_known_call_and_put_prices_are_finite() -> None:
    call_price = OptionsPricing.black_scholes(
        spot=100,
        strike=100,
        time_to_maturity=1,
        risk_free_rate=0.05,
        volatility=0.2,
        option_type="call",
    )
    put_price = OptionsPricing.black_scholes(
        spot=100,
        strike=100,
        time_to_maturity=1,
        risk_free_rate=0.05,
        volatility=0.2,
        option_type="put",
    )

    assert call_price == pytest.approx(10.4506, rel=1e-4)
    assert put_price == pytest.approx(5.5735, rel=1e-4)
    assert math.isfinite(call_price)
    assert math.isfinite(put_price)


def test_black_scholes_expired_options_return_intrinsic_value() -> None:
    assert OptionsPricing.black_scholes(120, 100, 0, 0.03, 0.0, "call") == 20
    assert OptionsPricing.black_scholes(80, 100, -0.01, 0.03, 0.0, "put") == 20


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"spot": 0}, "spot"),
        ({"strike": -1}, "strike"),
        ({"volatility": 0}, "volatility"),
        ({"volatility": float("nan")}, "volatility"),
        ({"option_type": "straddle"}, "option_type"),
    ],
)
def test_black_scholes_rejects_invalid_inputs(kwargs: dict[str, object], message: str) -> None:
    params = {
        "spot": 100,
        "strike": 100,
        "time_to_maturity": 0.25,
        "risk_free_rate": 0.03,
        "volatility": 0.2,
        "option_type": "call",
    }
    params.update(kwargs)

    with pytest.raises(ValueError, match=message):
        OptionsPricing.black_scholes(**params)


def test_calculate_greeks_rejects_invalid_non_expired_inputs() -> None:
    with pytest.raises(ValueError, match="volatility"):
        OptionsPricing.calculate_greeks(
            spot=100,
            strike=100,
            time_to_maturity=0.25,
            risk_free_rate=0.03,
            volatility=0,
            option_type="call",
        )


def test_implied_volatility_rejects_invalid_inputs() -> None:
    with pytest.raises(ValueError, match="option_price"):
        OptionsPricing.implied_volatility(
            option_price=0,
            spot=100,
            strike=100,
            time_to_maturity=0.25,
            risk_free_rate=0.03,
        )


def test_calculate_time_to_maturity_handles_valid_invalid_and_expired_dates() -> None:
    future = (datetime.now() + timedelta(days=30)).strftime("%Y-%m-%d")
    expired = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")

    assert OptionsPricing.calculate_time_to_maturity(future) == pytest.approx(30 / 365, abs=1 / 365)
    assert OptionsPricing.calculate_time_to_maturity(expired) == 0.0
    assert OptionsPricing.calculate_time_to_maturity("not-a-date") == 0.0
