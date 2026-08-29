"""Regression tests for portfolio trade-date alignment and metric sentinels."""

from __future__ import annotations


def _bars(start_day: int, n: int, close0: float = 10.0, *, fmt: str = "yyyymmdd") -> list[dict]:
    rows = []
    for i in range(n):
        day = start_day + i
        if fmt == "iso":
            date = f"2024-01-{day:02d}"
        else:
            date = f"202401{day:02d}"
        rows.append(
            {
                "trade_date": date,
                "close": close0 + i * 0.1,
                "volume": 1_000_000 + i,
            }
        )
    return rows


def test_portfolio_aligns_on_shared_trade_dates_not_trailing_length():
    from aiask_quant_core.backtest.engine import BacktestEngine

    series_a = _bars(1, 10, 10.0)
    series_b = _bars(6, 10, 20.0)

    result = BacktestEngine.run_portfolio_backtest(
        {"AAA": series_a, "BBB": series_b},
        strategy="ma_cross",
        params={
            "initial_capital": 100000,
            "short_period": 2,
            "long_period": 3,
            "weights": {"AAA": 0.5, "BBB": 0.5},
        },
    )
    assert result.get("success") is True, result
    data = result.get("data") or {}
    equity = data.get("equity_curve") or []
    assert len(equity) == 5, f"expected 5 aligned bars, got {len(equity)}"


def test_portfolio_normalizes_mixed_date_formats():
    from aiask_quant_core.backtest.engine import BacktestEngine

    series_a = _bars(1, 8, 10.0, fmt="yyyymmdd")
    series_b = _bars(3, 8, 20.0, fmt="iso")  # overlap days 3-8 = 6

    result = BacktestEngine.run_portfolio_backtest(
        {"AAA": series_a, "BBB": series_b},
        strategy="ma_cross",
        params={
            "initial_capital": 100000,
            "short_period": 2,
            "long_period": 3,
            "weights": {"AAA": 0.5, "BBB": 0.5},
        },
    )
    assert result.get("success") is True, result
    equity = (result.get("data") or {}).get("equity_curve") or []
    assert len(equity) == 6, f"expected 6 mixed-format bars, got {len(equity)}"


def test_normalize_trade_date_helper():
    from aiask_quant_core.backtest.utils import normalize_trade_date, align_klines_by_trade_date

    assert normalize_trade_date("2024-01-02") == "20240102"
    assert normalize_trade_date("20240102") == "20240102"
    assert normalize_trade_date("2024-01-02T15:00:00+08:00") == "20240102"
    common, aligned = align_klines_by_trade_date(
        {
            "A": [{"trade_date": "2024-01-01", "close": 1}, {"trade_date": "2024-01-02", "close": 2}],
            "B": [{"date": "20240102", "close": 3}, {"date": "20240103", "close": 4}],
        }
    )
    assert common == ["20240102"]
    assert len(aligned["A"]) == 1 and len(aligned["B"]) == 1


def test_profit_factor_undefined_is_none_not_999():
    from aiask_quant_core.backtest._engine_support import _compute_profit_factor

    value = _compute_profit_factor(
        {
            "round_trip_positions": [
                {"realized_pnl": 10.0},
                {"realized_pnl": 5.0},
            ]
        }
    )
    assert value is None
