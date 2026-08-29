"""P0-3 回归测试：逐标的 trend/vol regime 推断（默认 OFF 零变化 / ON 推断 / 缺数据降级）。

关联：开发周期计划-倒置架构与因子路由-2026-06-03.md · Phase 0 · P0-3
toggle：STRATEGY_FACTORY_PER_SYMBOL_REGIME_ENABLED（默认 OFF）。
"""

from __future__ import annotations

import asyncio

from akshare_mcp.services import incubation


def test_toggle_default_off(monkeypatch):
    monkeypatch.delenv("STRATEGY_FACTORY_PER_SYMBOL_REGIME_ENABLED", raising=False)
    assert incubation._per_symbol_regime_enabled() is False
    monkeypatch.setenv("STRATEGY_FACTORY_PER_SYMBOL_REGIME_ENABLED", "1")
    assert incubation._per_symbol_regime_enabled() is True


def test_infer_trend_up():
    closes = [10.0 + i * 0.3 for i in range(40)]  # 稳定上行
    labels = incubation._infer_symbol_regime(closes)
    assert labels["trend_regime"] == "trend_up"


def test_infer_trend_down():
    closes = [50.0 - i * 0.4 for i in range(40)]  # 稳定下行
    labels = incubation._infer_symbol_regime(closes)
    assert labels["trend_regime"] == "trend_down"


def test_infer_range():
    # 在窄幅内来回震荡，20 日动量接近 0
    closes = [20.0 + (1.0 if i % 2 == 0 else -1.0) * 0.1 for i in range(40)]
    labels = incubation._infer_symbol_regime(closes)
    assert labels["trend_regime"] == "range"


def test_infer_high_vol():
    # 大幅交替跳动 → 高已实现波动率
    closes = []
    price = 30.0
    for i in range(40):
        price *= 1.08 if i % 2 == 0 else 0.93
        closes.append(price)
    labels = incubation._infer_symbol_regime(closes)
    assert labels["vol_regime"] == "high_vol"


def test_infer_low_vol():
    closes = [25.0 + i * 0.001 for i in range(40)]  # 近乎平直 → 低波动
    labels = incubation._infer_symbol_regime(closes)
    assert labels["vol_regime"] == "low_vol"


def test_infer_insufficient_data_unknown():
    labels = incubation._infer_symbol_regime([10.0, 11.0, 12.0])
    assert labels["trend_regime"] == "unknown"
    assert labels["vol_regime"] == "unknown"


class _Db:
    def __init__(self, closes):
        self._closes = closes

    async def get_klines(self, code, limit=60, **_kw):
        return [{"date": f"2026-01-{(i % 28) + 1:02d}", "close": c} for i, c in enumerate(self._closes)]


def test_infer_from_db_trend_up():
    db = _Db([10.0 + i * 0.3 for i in range(40)])
    labels = asyncio.run(incubation._infer_symbol_regime_from_db(db, "600519"))
    assert labels["trend_regime"] == "trend_up"


def test_infer_from_db_no_klines_degrades():
    class _Empty:
        async def get_klines(self, *_a, **_k):
            return []

    labels = asyncio.run(incubation._infer_symbol_regime_from_db(_Empty(), "600519"))
    assert labels == {"trend_regime": "unknown", "vol_regime": "unknown"}


def test_resolve_signal_regime_merges_passed_regime():
    # _resolve_signal_regime 应采用显式传入的 trend/vol（模拟 P0-3 调用方合并后的结果）
    labels = incubation._resolve_signal_regime(
        {"params": {}},
        {"trend_regime": "trend_up", "vol_regime": "high_vol"},
    )
    assert labels["trend_regime"] == "trend_up"
    assert labels["vol_regime"] == "high_vol"
