"""An empty portfolio has no risk measurement — not a measured risk of zero.

Both helpers used to return a *numeric* zero for every risk field:

    _empty_var_payload     -> var.percentage = 0.0, volatility = 0.0,
                              max_drawdown = 0.0
    _empty_stress_payload  -> loss_percentage = "0.00%", severity = "low",
                              summary.worst_loss = 0.0

Read downstream, those are indistinguishable from "the scenario was evaluated
and produced no loss", which is why the stress-loss admission check
(``<= -25.0``) can never fire on them and why a 0% VaR passes a 4.5% ceiling.

``total_value`` / ``current_value`` are deliberately still 0.0: for an empty
portfolio that value is a fact, not a placeholder.
"""

from __future__ import annotations

from akshare_mcp.tools.managers.risk_mgr_helpers import (
    _empty_stress_payload,
    _empty_var_payload,
)

# Every field a consumer could read as a computed risk number.
_VAR_RISK_PATHS = (
    ("var", "percentage"),
    ("var", "amount"),
    ("cvar", "percentage"),
    ("cvar", "amount"),
)


def test_empty_var_payload_reports_no_measurement() -> None:
    payload = _empty_var_payload("pf-1", "portfolio_id", 0.95, "historical")

    assert payload["empty_portfolio"] is True
    assert payload["measurable"] is False
    assert payload["reason"] == "empty_portfolio"

    for path in _VAR_RISK_PATHS:
        assert payload[path[0]][path[1]] is None, f"{'.'.join(path)} must be None"
    assert payload["volatility"] is None
    assert payload["max_drawdown"] is None

    # A fact, not a placeholder.
    assert payload["total_value"] == 0.0


def test_empty_stress_payload_reports_no_measurement() -> None:
    payload = _empty_stress_payload(
        "pf-1", "portfolio_id", "market_crash", "market down 20%"
    )

    assert payload["empty_portfolio"] is True
    assert payload["measurable"] is False
    assert payload["reason"] == "empty_portfolio"

    assert payload["loss"] is None
    assert payload["loss_percentage"] is None
    assert payload["stressed_value"] is None
    # "low" was the worst offender: it graded a scenario that was never run.
    assert payload["severity"] is None
    assert payload["summary"]["worst_loss"] is None
    assert payload["summary"]["worst_loss_pct"] is None

    # A fact, not a placeholder.
    assert payload["current_value"] == 0.0
