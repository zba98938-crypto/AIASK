from __future__ import annotations

import asyncio
import copy
import json
from datetime import date, timedelta

import pytest

from strategy_factory.application.research.paper_trading_bridge import PaperTradingBridge


IDENTITY = {"candidate_contract_hash": "candidate-v1", "tested_object_hash": "tested-v1"}
STRATEGY = {"id": "strategy-1", "params": {**IDENTITY, "backtest_win_rate": 0.5}}


def _dates(count=60):
    result = []
    day = date(2025, 1, 2)
    while len(result) < count:
        if day.weekday() < 5:
            result.append(day.isoformat())
        day += timedelta(days=1)
    return result


def _fill(position_id, side, day, price, *, quantity=100, commission=1.0, suffix=""):
    return {
        "id": f"{position_id}-{side}{suffix}", "position_id": position_id,
        "account_id": "paper-1", "stock_code": "600519", "trade_type": side,
        "quantity": quantity, "price": price, "commission": commission,
        "amount": 9_999_999.0,  # Deliberately unrelated to PnL; not a winning-trade proxy.
        "trade_time": f"{day}T14:00:00+08:00", "order_metadata": json.dumps(IDENTITY),
    }


class EvidenceDb:
    def __init__(self, *, count=30, nav_count=60):
        days = _dates(max(60, nav_count))
        self.account = {"id": "paper-1", "strategy_id": "strategy-1", "initial_capital": 100_000, "risk_rules": {}}
        self.nav = [{"nav_date": day, "total_value": 100_000 + index * 20} for index, day in enumerate(days[:nav_count])]
        self.trades = []
        for index in range(count):
            self.trades.extend([
                _fill(f"p{index}", "buy", days[index * 2], 10),
                _fill(f"p{index}", "sell", days[index * 2 + 1], 9.5 if index % 5 == 0 else 12),
            ])
        self.writes = []

    async def fetchrow(self, _query, *_args):
        return self.account

    async def fetch(self, query, *_args):
        if "FROM paper_nav" in query:
            return copy.deepcopy(self.nav)
        return copy.deepcopy(self.trades)

    async def execute(self, *args):
        self.writes.append(args)
        raise AssertionError("Promotion assessment must never execute trades or change status")


def _evaluate(db, strategy=None):
    return asyncio.run(PaperTradingBridge(db).evaluate_promotion(strategy or STRATEGY))


def test_positive_closed_net_evidence_is_eligible_but_does_not_trade():
    db = EvidenceDb()
    result = _evaluate(db)
    assert result["eligible"] is True
    assert result["reasons"] == []
    assert result["days_active"] == 60
    assert result["trade_count"] == result["closed_round_trip_count"] == 30
    assert result["actual_win_rate"] == pytest.approx(0.8)
    # Winners +198, losers -52; both entry and exit commission are included.
    assert result["net_pnl"] == pytest.approx(24 * 198 - 6 * 52)
    assert result["total_return"] == pytest.approx((24 * 198 - 6 * 52) / 100_000)
    assert result["profit_factor"] == pytest.approx(24 * 198 / (6 * 52))
    assert result["sharpe"] > 0.5
    assert result["net_return_ci_lower"] > 0
    assert db.writes == []


def test_positive_and_negative_paired_fills_not_amount_or_claimed_profit():
    db = EvidenceDb(count=0)
    days = _dates()
    db.trades = [
        _fill("winner", "buy", days[0], 10, commission=0),
        _fill("winner", "sell", days[1], 11, commission=0),
        _fill("loser", "buy", days[2], 10, commission=0),
        _fill("loser", "sell", days[3], 8, commission=0),
    ]
    for trade in db.trades:
        trade["profit"] = 1_000_000  # Even a claimed profit cannot override actual paired fills.
    result = _evaluate(db)
    assert result["actual_win_rate"] == 0.5
    assert result["profit_factor"] == 0.5
    assert result["net_pnl"] == -100
    assert result["total_return"] == -0.001
    assert result["net_return_ci_lower"] < 0
    assert "weak_profit_factor" in result["reasons"]


def test_price_gain_becomes_a_loss_after_both_fill_fees():
    db = EvidenceDb(count=0)
    days = _dates()
    db.trades = [
        _fill("fees", "buy", days[0], 10, commission=2),
        _fill("fees", "sell", days[1], 10.01, commission=2),
    ]
    result = _evaluate(db)
    assert result["net_pnl"] == pytest.approx(-3)
    assert result["actual_win_rate"] == 0.0
    assert result["profit_factor"] == 0.0
    assert result["net_return_ci_lower"] is None
    assert result["metric_reasons"]["net_return_ci_lower"] == "insufficient_return_samples"


def test_missing_profit_without_entry_fills_never_uses_positive_sell_amount():
    db = EvidenceDb(count=0)
    db.trades = [_fill("orphan", "sell", _dates()[1], 12)]
    result = _evaluate(db)
    assert result["eligible"] is False
    assert result["actual_win_rate"] is None
    assert result["profit_factor"] is None
    assert result["net_pnl"] is None
    assert result["invalid_trade_counts"] == {"unpaired_exit": 1}
    assert result["metric_reasons"]["actual_win_rate"] == "incomplete_trade_data"


@pytest.mark.parametrize("field", ["price", "quantity", "commission", "position_id", "trade_time"])
def test_incomplete_fills_fail_closed_even_with_other_profitable_positions(field):
    db = EvidenceDb()
    db.trades[-1][field] = None
    result = _evaluate(db)
    assert result["eligible"] is False
    assert result["actual_win_rate"] is None
    assert result["profit_factor"] is None
    assert result["total_return"] is None
    assert result["metric_reasons"]["actual_win_rate"] == "incomplete_trade_data"
    assert result["invalid_trade_counts"]


def test_partial_fills_count_once_only_after_position_is_flat():
    db = EvidenceDb(count=0)
    days = _dates()
    db.trades = [
        _fill("closed", "buy", days[0], 10),
        _fill("closed", "buy", days[1], 12, suffix="2"),
        _fill("closed", "sell", days[2], 12, quantity=50),
        _fill("closed", "sell", days[3], 12, quantity=150, suffix="2"),
        _fill("open", "buy", days[0], 10, quantity=200),
        _fill("open", "sell", days[1], 11, quantity=100),
    ]
    result = _evaluate(db)
    assert result["closed_round_trip_count"] == 1
    assert result["open_position_count"] == 1
    assert result["net_pnl"] == 196  # Four fills, four fees; the open position contributes nothing.


def test_same_symbol_with_different_position_ids_is_not_a_round_trip():
    db = EvidenceDb(count=0)
    db.trades = [
        _fill("entry", "buy", _dates()[0], 10),
        _fill("unrelated-exit", "sell", _dates()[1], 12),
    ]
    result = _evaluate(db)
    assert result["trade_count"] == 0
    assert result["actual_win_rate"] is None
    assert "incomplete_trade_data" in result["reasons"]


@pytest.mark.parametrize("count,nav_count,expected_reason", [
    (29, 60, "insufficient_closed_round_trips"),
    (30, 59, "insufficient_valid_trading_days"),
])
def test_minimum_sample_is_60_unique_nav_dates_and_30_closed_round_trips(count, nav_count, expected_reason):
    db = EvidenceDb(count=count, nav_count=nav_count)
    db.nav = db.nav * 3  # Repeated rows must not manufacture observation days.
    result = _evaluate(db)
    assert result["eligible"] is False
    assert result["days_active"] == nav_count
    assert result["trade_count"] == count
    assert expected_reason in result["reasons"]


def test_nav_validation_ignores_weekends_future_dates_and_nonfinite_values():
    db = EvidenceDb(nav_count=59)
    db.nav.extend([
        {"nav_date": "2025-01-04", "total_value": 100_000},  # Saturday
        {"nav_date": "2099-01-05", "total_value": 100_000},
        {"nav_date": "2025-12-31", "total_value": float("nan")},
        {"nav_date": "not-a-date", "total_value": 100_000},
        {"nav_date": "2025-12-30", "total_value": 0},
    ])
    result = _evaluate(db)
    assert result["days_active"] == 59
    assert result["invalid_nav_count"] == 3
    assert result["eligible"] is False


def test_positive_point_return_is_not_enough_without_positive_95_percent_ci():
    db = EvidenceDb()
    for trade in db.trades:
        trade["commission"] = 0
        if trade["trade_type"] == "sell":
            index = int(trade["position_id"][1:])
            trade["price"] = 9 if index % 5 == 0 else 10.5
    result = _evaluate(db)
    assert result["total_return"] > 0
    assert result["profit_factor"] == 2
    assert result["sharpe"] > 0.5
    assert result["net_return_ci_lower"] < 0
    assert result["eligible"] is False
    assert result["reasons"] == ["nonpositive_net_return_ci_lower"]


@pytest.mark.parametrize("threshold,metric", [("min_sharpe", "sharpe"), ("min_profit_factor", "profit_factor")])
def test_sharpe_and_profit_factor_thresholds_are_actually_evaluated(monkeypatch, threshold, metric):
    import strategy_factory.application.research.paper_trading_bridge as module

    db = EvidenceDb()
    baseline = _evaluate(db)
    monkeypatch.setitem(module._PROMOTION_THRESHOLDS, threshold, baseline[metric] + 1)
    result = _evaluate(db)
    assert result["eligible"] is False
    assert f"weak_{metric}" in result["reasons"]


def test_drawdown_evaluates_the_stricter_account_risk_limit():
    db = EvidenceDb()
    db.account["risk_rules"] = json.dumps({"max_drawdown_pct": 10})
    db.nav[-1]["total_value"] = 85_000
    result = _evaluate(db)
    assert 0.1 < result["max_drawdown"] < 0.25
    assert result["thresholds"]["max_drawdown"] == 0.1
    assert "weak_max_drawdown" in result["reasons"]


@pytest.mark.parametrize("missing", ["nav", "trades", "initial_capital", "backtest_win_rate"])
def test_missing_promotion_evidence_is_not_replaced_with_plausible_defaults(missing):
    db = EvidenceDb()
    strategy = copy.deepcopy(STRATEGY)
    if missing == "initial_capital":
        db.account.pop(missing)
    elif missing == "backtest_win_rate":
        strategy["params"].pop(missing)
    else:
        setattr(db, missing, [])
    result = _evaluate(db, strategy)
    assert result["eligible"] is False
    metric = {"nav": "max_drawdown", "trades": "actual_win_rate", "initial_capital": "total_return", "backtest_win_rate": "backtest_win_rate"}[missing]
    assert result[metric] is None
    assert result["metric_reasons"][metric]


def test_trade_evidence_cannot_be_relabelled_as_a_different_tested_object():
    db = EvidenceDb()
    strategy = copy.deepcopy(STRATEGY)
    strategy["params"]["tested_object_hash"] = "different-tested-object"
    result = _evaluate(db, strategy)
    assert result["eligible"] is False
    assert result["actual_win_rate"] is None
    assert result["unbound_or_mismatched_trade_count"] == 60
    assert result["metric_reasons"]["actual_win_rate"] == "unbound_or_mismatched_trade_identity"


def test_unrealized_nav_gains_never_replace_closed_round_trip_return():
    db = EvidenceDb(count=0)
    db.nav[-1]["total_value"] = 1_000_000
    result = _evaluate(db)
    assert result["total_return"] is None
    assert result["sharpe"] is None
    assert result["metric_reasons"]["total_return"] == "no_closed_round_trips"
    assert result["eligible"] is False


def test_zero_net_profit_is_not_a_win_despite_positive_sell_notional():
    db = EvidenceDb(count=0)
    db.trades = [
        _fill("flat", "buy", _dates()[0], 10, commission=0),
        _fill("flat", "sell", _dates()[1], 10, commission=0),
    ]
    db.trades[-1]["profit"] = 0
    result = _evaluate(db)
    assert result["net_pnl"] == 0
    assert result["actual_win_rate"] == 0
    assert result["profit_factor"] is None
    assert result["metric_reasons"]["profit_factor"] == "no_losing_round_trips"
    assert result["eligible"] is False


def test_missing_account_has_explicitly_unmeasurable_metrics():
    db = EvidenceDb()
    db.account = None
    result = _evaluate(db)
    assert result["eligible"] is False
    assert result["reason"] == "no_paper_account"
    assert result["actual_win_rate"] is None
    assert result["metric_reasons"]["actual_win_rate"] == "no_paper_account"


def test_query_failure_is_distinguished_from_measured_zero():
    class UnavailableDb(EvidenceDb):
        async def fetch(self, query, *_args):
            raise RuntimeError("database temporarily unavailable")

    result = _evaluate(UnavailableDb())
    assert result["eligible"] is False
    assert "nav_history_unavailable" in result["reasons"]
    assert "trade_history_unavailable" in result["reasons"]
    assert result["total_return"] is None
    assert result["max_drawdown"] is None
