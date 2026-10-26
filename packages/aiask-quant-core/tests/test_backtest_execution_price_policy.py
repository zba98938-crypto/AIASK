from __future__ import annotations

import numpy as np

from aiask_quant_core.backtest._engine_support import _simulate_trades_from_masks


def _bars(*, omit_open_index: int | None = None) -> list[dict[str, object]]:
    rows = []
    for index, (open_price, close_price) in enumerate(
        [(10.0, 10.5), (11.5, 11.0), (12.5, 12.0), (14.5, 13.0)]
    ):
        row: dict[str, object] = {
            "date": f"2026-01-0{index + 1}",
            "close": close_price,
            "volume": 100_000_000,
        }
        if index != omit_open_index:
            row["open"] = open_price
        rows.append(row)
    return rows


def _simulate(policy: str, bars: list[dict[str, object]]) -> dict[str, object]:
    return _simulate_trades_from_masks(
        closes=np.asarray([float(row["close"]) for row in bars]),
        volumes=np.asarray([float(row["volume"]) for row in bars]),
        entry_mask=np.asarray([True, False, False, False]),
        exit_mask=np.asarray([False, False, True, False]),
        initial_capital=10_000.0,
        commission_rate=0.0,
        return_trades=True,
        klines=bars,
        params={
            "execution_price_policy": policy,
            "arrival_price_policy": "next_open",
            "market_ruleset": "cn_equity",
            "min_trade_lot": 100,
            "t_plus_one": True,
            "sell_tax_rate": 0.0,
            "slippage": 0.0,
            "market_impact_bps": 0.0,
            "initial_capital": 10_000.0,
        },
    )


def test_next_open_policy_uses_next_bar_open_for_entry_and_exit():
    result = _simulate("next_open", _bars())

    fills = result["fills"]
    assert isinstance(fills, list)
    assert len(fills) == 2
    assert fills[0]["price_source"] == "next_session_open"
    assert fills[0]["signal_time"] == "2026-01-01"
    assert fills[0]["time"] == "2026-01-02"
    # The entry open (11.5) is distinct from the same bar's close (11.0).
    assert float(fills[0]["price"]) > 11.5
    assert float(fills[0]["price"]) < 11.51
    assert fills[1]["price_source"] == "next_session_open"
    assert fills[1]["time"] == "2026-01-04"
    # Exit uses the final next-session open (14.5), not its close (13.0).
    assert float(fills[1]["price"]) > 14.4


def test_next_open_policy_does_not_fallback_when_open_is_missing():
    result = _simulate("next_open", _bars(omit_open_index=1))

    assert result["trades"] == []
    assert result["closed_round_trip_count"] == 0
    assert result["execution_summary"]["failed_order_count"] == 1
    assert result["execution_summary"]["blocked_reason_counts"] == {
        "missing_next_session_open": 1
    }


def test_legacy_policy_remains_explicitly_close_based():
    result = _simulate("next_close_legacy", _bars())

    fills = result["fills"]
    assert isinstance(fills, list)
    assert fills[0]["price_source"] == "next_bar_close_legacy"
    # Legacy behavior is close-based and therefore materially differs from
    # the certified next-session-open policy.
    assert float(fills[0]["price"]) < 11.01
