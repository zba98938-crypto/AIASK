"""Stress losses use actual portfolio NAV, including uninvested cash."""

from datetime import date, timedelta

import numpy as np
import pytest

from aiask_quant_core.risk_model import RiskModel
from strategy_factory.application import _panels_evaluation as panels


def test_stress_cash_remains_unshocked_in_portfolio_denominator():
    holding = {"code": "000001", "value": 10_000.0, "beta": 1.0}
    invested = RiskModel.stress_test([holding])
    with_cash = RiskModel.stress_test([holding], portfolio_value=100_000.0)
    assert invested["loss_percent"] == pytest.approx(-20.0)
    assert with_cash["loss_percent"] == pytest.approx(-2.0)
    assert with_cash["shocked_value"] == pytest.approx(98_000.0)
    assert with_cash["cash_value"] == pytest.approx(90_000.0)
    assert with_cash["coverage"] == 1.0


@pytest.mark.parametrize("holdings, reason", [
    ([], "no_positions"),
    ([{"code": "000001", "value": 10_000.0}], "no_position_sensitivity"),
])
def test_cash_does_not_turn_missing_exposure_evidence_into_measured_zero(holdings, reason):
    result = RiskModel.stress_test(holdings, portfolio_value=100_000.0)
    assert result["loss_percent"] is None
    assert not result["measurable"]
    assert result["reason"] == reason


@pytest.mark.asyncio
@pytest.mark.parametrize("position_limit", [0.1, 1.0])
async def test_real_panel_risk_loss_scales_by_actual_invested_weight(monkeypatch, position_limit):
    codes = ["000001", "000002", "000003"]
    monkeypatch.setattr(panels, "_resolve_strategy_sample_selection", lambda *a, **kw: {"sample_codes": codes})
    rows = []
    for i in range(220):
        price = float(10 * np.exp(.001 * i + .02 * np.sin(i / 5)))
        rows.append({
            "date": str(date(2025, 1, 1) + timedelta(days=i)),
            "open": price, "close": price, "high": price, "low": price,
            "volume": 10_000_000,
        })

    class Db:
        async def get_klines(self, code, limit):
            return rows

    from test_target_risk_binding import contract
    params = {"max_position_pct": position_limit, "target_risk_contract": contract(codes, {code:1/3 for code in codes})}
    params["target_risk_contract"]["execution_kwargs"].update(commission=.0003, slippage=0.)
    panel = await panels._build_strategy_panels("buy_and_hold", params, Db())
    report = await panels._run_risk_report("buy_and_hold", params, Db())
    stock_value = sum(holding["value"] for holding in panel["holdings"])
    assert panel["portfolio_value"] == pytest.approx(stock_value + panel["cash_value"])
    expected = sum(-20 * holding["beta"] * holding["weight"] for holding in panel["holdings"])
    assert report["stress_loss_percent"] == pytest.approx(expected, abs=5e-5)
    assert report["stress_measurable"]
    if position_limit == .1:
        assert -3 < report["stress_loss_percent"] < -1
        assert panel["cash_value"] / panel["portfolio_value"] > .8
