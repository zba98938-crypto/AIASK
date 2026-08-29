"""Tests for instrument-profile realized-metric measurement (P0-b)."""

from __future__ import annotations

import math

from akshare_mcp.services.instrument_profile_measurement import (
    measure_instrument_profile,
)


def _synth_trend_klines(n: int = 200, *, start: float = 10.0, drift: float = 0.004) -> list[dict]:
    """合成一段带温和上行趋势 + 日内波动的日线。"""
    rows: list[dict] = []
    close = start
    prev_close = start
    for i in range(n):
        # 温和趋势 + 周期波动
        ret = drift + 0.02 * math.sin(i / 7.0)
        close = max(0.5, prev_close * (1.0 + ret))
        high = close * (1.0 + 0.015 + 0.005 * abs(math.sin(i / 3.0)))
        low = close * (1.0 - 0.015 - 0.005 * abs(math.cos(i / 3.0)))
        open_ = prev_close * (1.0 + 0.003 * math.sin(i / 5.0))
        volume = 1_000_000 * (1.0 + 0.4 * abs(math.sin(i / 4.0)))
        rows.append(
            {
                "time": f"2025-{(i % 12) + 1:02d}-{(i % 27) + 1:02d}",
                "code": "600000",
                "open": round(open_, 4),
                "high": round(max(high, open_, close), 4),
                "low": round(min(low, open_, close), 4),
                "close": round(close, 4),
                "volume": round(volume, 2),
                "turnover": round(volume * close, 2),
            }
        )
        prev_close = close
    return rows


def test_measure_returns_measured_profile_for_sufficient_history() -> None:
    rows = _synth_trend_klines(200)
    summary = measure_instrument_profile(rows)
    assert summary["measured"] is True
    assert summary["bars"] == 200
    for key in (
        "annual_volatility_realized_252d",
        "atr14_pct_realized",
        "gap_p95_realized",
        "intraday_range_p90",
        "trend_efficiency_60d_realized",
        "volume_ratio_p80",
        "volume_ratio_p90",
        "turnover_rate_p80",
        "turnover_rate_p90",
    ):
        assert key in summary
        assert summary[key] > 0.0


def test_measure_rejects_insufficient_history() -> None:
    rows = _synth_trend_klines(30)
    summary = measure_instrument_profile(rows)
    assert summary["measured"] is False
    assert "insufficient_bars" in summary["reason"]


def test_measure_ignores_invalid_rows() -> None:
    rows = _synth_trend_klines(120)
    # 注入若干非法行,应被跳过而不报错
    rows.insert(10, {"open": None, "high": None, "low": None, "close": None})
    rows.insert(50, {"open": 1, "high": 1, "low": 1, "close": 0})
    summary = measure_instrument_profile(rows)
    assert summary["measured"] is True
    assert summary["bars"] >= 118


def test_measured_metrics_feed_normalizer_as_measured() -> None:
    """测量输出喂给 _normalize_instrument_profile,应判定 measured_profile_complete。"""
    from akshare_mcp.services.strategy_spec.normalizers import _normalize_instrument_profile

    rows = _synth_trend_klines(200)
    summary = measure_instrument_profile(rows)
    profile = _normalize_instrument_profile(
        None,
        target_symbols=["600000"],
        source_symbol_summary=summary,
    )
    assert profile["measured_profile_complete"] is True
    assert profile["measurement_source"] == "measured"
