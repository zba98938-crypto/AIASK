"""DEV-V1 P3: D→C 升级 family 集合 toggle-aware 行为测试。"""

from __future__ import annotations

import pytest


def _import_proxy_and_constants():
    """父模块命名空间下的间接引用。

    `_TRADE_AWARE_VALIDATION_GRADE_FAMILIES` 实际定义在
    `strategy_factory.application.submission_gate.runner` 模块里
    `strategy_factory.application.submission_gate.runner` 的 globals 中。

    历史 fragment 路径已合并进父模块,
    必须从被注入的父模块读取。
    """
    import importlib
    parent = importlib.import_module(
        "strategy_factory.application.submission_gate.runner"
    )
    return (
        parent._TRADE_AWARE_VALIDATION_GRADE_FAMILIES,
        parent._BASE_TRADE_AWARE_FAMILIES,
        parent._CANDIDATE_EXTRA_FAMILIES,
    )


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch):
    monkeypatch.delenv("STRATEGY_FACTORY_TRADE_AWARE_EXTRA_FAMILIES", raising=False)
    yield


def test_default_only_base_three_families():
    """默认 toggle 空: 只允许基础 3 个 family。"""
    proxy, base, _ = _import_proxy_and_constants()
    assert "momentum" in proxy
    assert "ma_cross" in proxy
    assert "quality_factor" in proxy
    assert "volatility_breakout" not in proxy
    assert len(proxy) == len(base) == 3


def test_extra_single_family(monkeypatch):
    monkeypatch.setenv("STRATEGY_FACTORY_TRADE_AWARE_EXTRA_FAMILIES", "volatility_breakout")
    proxy, _, _ = _import_proxy_and_constants()
    assert "volatility_breakout" in proxy
    assert "value_factor" not in proxy
    assert len(proxy) == 4


def test_extra_multiple_families(monkeypatch):
    monkeypatch.setenv(
        "STRATEGY_FACTORY_TRADE_AWARE_EXTRA_FAMILIES",
        "volatility_breakout,value_factor,sector_rotation",
    )
    proxy, _, _ = _import_proxy_and_constants()
    assert "volatility_breakout" in proxy
    assert "value_factor" in proxy
    assert "sector_rotation" in proxy
    assert "macro_timing" not in proxy


def test_invalid_family_silently_dropped(monkeypatch):
    """白名单外的 family 静默丢弃,防止误配置 reverse-only 类型。"""
    monkeypatch.setenv(
        "STRATEGY_FACTORY_TRADE_AWARE_EXTRA_FAMILIES",
        "rsi,mean_reversion_short,volatility_breakout,gap_fill",
    )
    proxy, _, _ = _import_proxy_and_constants()
    assert "rsi" not in proxy
    assert "mean_reversion_short" not in proxy
    assert "gap_fill" not in proxy
    assert "volatility_breakout" in proxy
    # base 3 + 1 extra = 4
    assert len(proxy) == 4


def test_runtime_env_change_takes_effect(monkeypatch):
    """toggle 改变后立即生效,无需重启进程或重新导入模块。"""
    proxy, _, _ = _import_proxy_and_constants()
    assert "volatility_breakout" not in proxy
    monkeypatch.setenv("STRATEGY_FACTORY_TRADE_AWARE_EXTRA_FAMILIES", "volatility_breakout")
    assert "volatility_breakout" in proxy
    monkeypatch.delenv("STRATEGY_FACTORY_TRADE_AWARE_EXTRA_FAMILIES")
    assert "volatility_breakout" not in proxy


def test_iteration_returns_all_resolved_families(monkeypatch):
    monkeypatch.setenv("STRATEGY_FACTORY_TRADE_AWARE_EXTRA_FAMILIES", "volatility_breakout")
    proxy, base, _ = _import_proxy_and_constants()
    families = set(proxy)
    assert families == set(base) | {"volatility_breakout"}


def test_full_extras_whitelist(monkeypatch):
    """灰度终态:全部 7 个候选扩展 family 都在白名单内,可以一起开启。"""
    monkeypatch.setenv(
        "STRATEGY_FACTORY_TRADE_AWARE_EXTRA_FAMILIES",
        ",".join([
            "volatility_breakout", "value_factor", "sector_rotation",
            "macro_timing", "growth_factor", "north_capital_track",
            "event_structure_breakout",
        ]),
    )
    proxy, _, candidate_extras = _import_proxy_and_constants()
    for fam in candidate_extras:
        assert fam in proxy
    # base 3 + 7 extras = 10
    assert len(proxy) == 10


def test_eq_operator_works():
    """__eq__ 让 == 检查也工作(虽然实际代码不太用)。"""
    proxy, base, _ = _import_proxy_and_constants()
    assert proxy == base


def test_existing_in_check_pattern_is_preserved(monkeypatch):
    """验证现有调用点 `if x in _TRADE_AWARE_VALIDATION_GRADE_FAMILIES` 仍然正常。"""
    monkeypatch.setenv("STRATEGY_FACTORY_TRADE_AWARE_EXTRA_FAMILIES", "volatility_breakout")
    proxy, _, _ = _import_proxy_and_constants()

    # 模拟 trade_profile.py 第 27 行的检查模式
    strategy_type = "volatility_breakout"
    if strategy_type not in proxy:
        pytest.fail("toggle ON 后 volatility_breakout 应该在集合内")

    # 模拟 attempt_adjustment.py 第 469 行
    family = "ma_cross"
    if family in proxy:
        pass  # 应该匹配
    else:
        pytest.fail("基础 family ma_cross 应该永远在集合内")
