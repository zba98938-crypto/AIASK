"""P0-2 回归测试：每日 batch IC 中性化接线（默认 OFF 零变化 / ON 走 dual-IC / 缺风格降级）。

关联：开发周期计划-倒置架构与因子路由-2026-06-03.md · Phase 0 · P0-2
toggle：STRATEGY_FACTORY_BATCH_IC_NEUTRALIZE_ENABLED（默认 OFF）。
"""

from __future__ import annotations

import asyncio

import numpy as np

from akshare_mcp.tools.managers import quant_mgr_classic
from akshare_mcp.tools.managers.quant_mgr_classic import handle_batch_compute_factors


def _ok(data, **kwargs):
    payload = {"success": True, "data": data, "error": None}
    payload.update(kwargs)
    return payload


def _fail(message, **kwargs):
    return {"success": False, "error": message, **kwargs}


def _make_klines(n: int, base: float, slope: float):
    # 单调上行的收盘价序列，保证 momentum 因子可计算且各股不同。
    return [
        {"date": f"2026-{((idx // 28) % 12) + 1:02d}-{(idx % 28) + 1:02d}", "close": base + idx * slope}
        for idx in range(n)
    ]


class _Db:
    """12 只股票、每只 ~340 根 K 线，足够 horizon=20 时产出 >=10 个截面样本。"""

    def __init__(self, *, with_style: bool):
        self._with_style = with_style
        self._codes = [f"60000{idx:02d}" for idx in range(12)]
        self.saved_ic: list[tuple] = []

    async def get_klines(self, code, limit=252, **_kwargs):
        seed = sum(ord(ch) for ch in str(code)) % 7
        return _make_klines(340, base=10.0 + seed, slope=0.05 + 0.01 * seed)

    async def get_financials(self, *_args, **_kwargs):
        return []

    async def get_stock_info(self, code):
        if not self._with_style:
            return None
        # 行业 + 市值 风格暴露，跨股票有差异以驱动中性化回归。
        seed = sum(ord(ch) for ch in str(code)) % 4
        return {
            "industry": ["银行", "白酒", "半导体", "医药"][seed],
            "market_cap": 1.0e10 * (seed + 1),
        }

    async def save_factor_values_batch(self, *_args):
        return None

    async def save_factor_values(self, *_args):
        return None

    async def save_factor_ic(self, *args):
        self.saved_ic.append(args)


def _run(with_style: bool):
    db = _Db(with_style=with_style)

    async def scenario():
        return await handle_batch_compute_factors(
            kw={
                "codes": db._codes,
                "factors": ["momentum"],
                "store": False,
                "compute_ic": True,
                "ic_horizons": [20],
            },
            ok=_ok,
            fail=_fail,
            get_db_fn=lambda: db,
        )

    return asyncio.run(scenario())


def test_off_uses_plain_calculate_factor_ic(monkeypatch):
    """OFF（默认）：走 AnalysisFactorsMixin.calculate_factor_ic，不触碰 dual-IC。"""
    monkeypatch.delenv("STRATEGY_FACTORY_BATCH_IC_NEUTRALIZE_ENABLED", raising=False)
    assert quant_mgr_classic._batch_ic_neutralize_enabled() is False

    from akshare_mcp.services.factor_analysis import FactorAnalyzer

    def _boom(*_a, **_k):
        raise AssertionError("OFF 路径不应调用 calculate_ic_dual")

    monkeypatch.setattr(FactorAnalyzer, "calculate_ic_dual", staticmethod(_boom))

    result = _run(with_style=True)
    assert result["success"] is True
    assert "horizon_20" in result["data"]["ic"]
    assert "momentum" in result["data"]["ic"]["horizon_20"]


def test_on_uses_dual_ic_with_styles(monkeypatch):
    """ON + 有风格数据：走 calculate_ic_dual 且 neutralization 真正启用。"""
    monkeypatch.setenv("STRATEGY_FACTORY_BATCH_IC_NEUTRALIZE_ENABLED", "1")
    assert quant_mgr_classic._batch_ic_neutralize_enabled() is True

    from akshare_mcp.services.factor_analysis import FactorAnalyzer

    calls: dict = {}
    original = FactorAnalyzer.calculate_ic_dual

    def _spy(factor_values, forward_returns, **kwargs):
        calls["industry"] = kwargs.get("industry")
        calls["market_cap"] = kwargs.get("market_cap")
        calls["enable_neutralization"] = kwargs.get("enable_neutralization")
        calls["n"] = len(factor_values)
        return original(factor_values, forward_returns, **kwargs)

    monkeypatch.setattr(FactorAnalyzer, "calculate_ic_dual", staticmethod(_spy))

    result = _run(with_style=True)
    assert result["success"] is True
    assert calls.get("enable_neutralization") is True
    assert calls.get("industry") and len(calls["industry"]) == calls["n"]
    assert calls.get("market_cap") and len(calls["market_cap"]) == calls["n"]
    assert "horizon_20" in result["data"]["ic"]


def test_on_missing_styles_degrades_gracefully(monkeypatch):
    """ON + 无风格数据（get_stock_info=None）：不报错，dual-IC 内部降级回原始值。"""
    monkeypatch.setenv("STRATEGY_FACTORY_BATCH_IC_NEUTRALIZE_ENABLED", "1")
    result = _run(with_style=False)
    assert result["success"] is True
    # 仍应产出 IC 结构（中性化在无风格时回退原始值，不阻断）。
    assert "horizon_20" in result["data"]["ic"]
    assert "momentum" in result["data"]["ic"]["horizon_20"]


def test_neutralize_dof_guard_small_cross_section():
    """灰度测试发现：小截面 + 多行业哑变量 → 自由度不足时跳过中性化（防翻号污染）。"""
    from akshare_mcp.services.factor_analysis import FactorAnalyzer

    # 10 样本，10 个不同行业 → 哑变量列数≈9，加截距≈10 列，dof≈0 → 应跳过
    fv = [0.1 * i for i in range(10)]
    industries = [f"ind_{i}" for i in range(10)]  # 全不同行业
    neutralized, info = FactorAnalyzer._neutralize_factor_values(
        np.array(fv, dtype=float),
        industry=industries,
        enable_neutralization=True,
    )
    assert info["reason"] == "insufficient_degrees_of_freedom"
    assert info.get("dof") is not None and info["dof"] < 5
    # 跳过后应返回原始值（未残差化）
    assert np.allclose(neutralized, np.array(fv, dtype=float))


def test_neutralize_proceeds_with_adequate_dof():
    """大截面 + 少数行业 → 自由度充足，正常残差化（不触发守卫）。"""
    from akshare_mcp.services.factor_analysis import FactorAnalyzer

    n = 60
    rng = np.random.default_rng(7)
    fv = rng.normal(size=n)
    industries = [["A", "B", "C"][i % 3] for i in range(n)]  # 仅 3 行业
    neutralized, info = FactorAnalyzer._neutralize_factor_values(
        np.array(fv, dtype=float),
        industry=industries,
        enable_neutralization=True,
    )
    assert info.get("reason") != "insufficient_degrees_of_freedom"
    assert info["industry"] is True
    assert "residual_std" in info
