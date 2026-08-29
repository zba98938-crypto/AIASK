"""P2-2 单测：前向收益采集窗口可配（默认零变化 / 可纳入 40 horizon）。

关联：开发周期计划-倒置架构与因子路由-2026-06-03.md · Phase 2 · P2-2
env：STRATEGY_FACTORY_FORWARD_DAYS / STRATEGY_FACTORY_FORWARD_HORIZONS。
"""

from __future__ import annotations

import math

from akshare_mcp.services import signal_tracker as st_ctx
from akshare_mcp.services.incubation_factory import forward_verifier as fv


def test_forward_days_default_unchanged(monkeypatch):
    monkeypatch.delenv("STRATEGY_FACTORY_FORWARD_DAYS", raising=False)
    assert st_ctx._resolve_forward_days() == [1, 5, 10, 20]


def test_forward_days_includes_40_when_set(monkeypatch):
    monkeypatch.setenv("STRATEGY_FACTORY_FORWARD_DAYS", "1,5,10,20,40")
    assert st_ctx._resolve_forward_days() == [1, 5, 10, 20, 40]


def test_forward_days_dedup_and_invalid_ignored(monkeypatch):
    monkeypatch.setenv("STRATEGY_FACTORY_FORWARD_DAYS", "5,5,abc,-3,10")
    assert st_ctx._resolve_forward_days() == [5, 10]


def test_forward_days_empty_falls_back(monkeypatch):
    monkeypatch.setenv("STRATEGY_FACTORY_FORWARD_DAYS", "  ")
    assert st_ctx._resolve_forward_days() == [1, 5, 10, 20]


def test_forward_horizons_default_unchanged(monkeypatch):
    monkeypatch.delenv("STRATEGY_FACTORY_FORWARD_HORIZONS", raising=False)
    assert fv._resolve_forward_horizons() == [5, 10, 20]


def test_forward_horizons_includes_40_when_set(monkeypatch):
    monkeypatch.setenv("STRATEGY_FACTORY_FORWARD_HORIZONS", "5,10,20,40")
    assert fv._resolve_forward_horizons() == [5, 10, 20, 40]


def test_forward_verifier_ignores_non_finite_returns():
    verifier = fv.ForwardVerifier()

    assert verifier._extract_forward_return(
        {"forward_5d": "nan", "return_5d": float("inf"), "fwd_5": "-inf"},
        5,
    ) is None
    sharpe = verifier._compute_forward_sharpe(
        [0.01, float("nan"), 0.02, float("inf"), 0.03, 0.04, 0.05],
        horizon_days=5,
    )
    ic = verifier._compute_forward_ic(
        [1, 1, -1, -1, 1, float("nan")],
        [0.01, 0.02, -0.01, -0.02, 0.03, float("inf")],
    )

    assert math.isfinite(sharpe)
    assert math.isfinite(ic)
