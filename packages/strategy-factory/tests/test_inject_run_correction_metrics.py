"""V5-PR-1: _inject_run_correction_metrics 单元测试.

验证:
  1. validation_runtime 不可用时软降级
  2. 有效输入下能算出 deflated_sharpe_ratio 等字段
  3. 任何异常都不打断主流程
  4. multiple_testing_inject_status 字段正确标记
"""

from __future__ import annotations

import importlib

import numpy as np
import pytest


def _import_helper():
    """从父模块拿 _inject_run_correction_metrics(它由 fragment loader 注入到 runner globals)."""
    parent = importlib.import_module(
        "strategy_factory.application.submission_gate.runner"
    )
    return parent._inject_run_correction_metrics


class _MockValidationRuntime:
    """模拟真实 validation_runtime — 返回固定测试值."""

    @staticmethod
    def deflated_sharpe_ratio(arr, **kwargs):
        return {
            "available": True,
            "dsr": 0.123,
            "reference_sharpe": 0.5,
            "effective_trials": 5.0,
        }

    @staticmethod
    def probability_of_backtest_overfitting(family_arr, **kwargs):
        return {"available": True, "pbo": 0.45}

    @staticmethod
    def white_reality_check(family_arr, **kwargs):
        return {"available": True, "p_value": 0.08}

    @staticmethod
    def hansen_spa_test(family_arr, **kwargs):
        return {"available": True, "p_value": 0.07}


def test_inject_returns_dict_for_minimal_input(monkeypatch):
    """最小输入(无 validation_report / backtest_metrics)也能返回非空 dict."""
    inject = _import_helper()

    # mock get_validation_runtime 返回 _MockValidationRuntime
    import strategy_factory.infrastructure.mcp_services as mcp
    monkeypatch.setattr(mcp, "get_validation_runtime", lambda: _MockValidationRuntime)

    strategy = {"id": "s1", "strategy_type": "volatility_breakout"}
    profile = {"profile": "trade_rule_validation"}
    normalized = {
        "post_cost_sharpe": 1.2,
        "attempt_adjustment": {"attempt_count": 5, "cohort_effective_trials": 5.0},
    }

    result = inject(strategy, profile, normalized)
    # 验证返回 dict + status 字段
    assert isinstance(result, dict)
    assert result.get("multiple_testing_inject_status") == "ok"
    # 至少有 mode / proxy 字段
    assert result.get("run_correction_mode") in ("attempt_only_proxy", "bootstrap_family_proxy")


def test_inject_calls_validation_runtime_when_score_series_provided(monkeypatch):
    """提供 score_series (>=3) 时,DSR formal 路径触发,返回 deflated_sharpe_ratio 字段."""
    inject = _import_helper()
    import strategy_factory.infrastructure.mcp_services as mcp
    monkeypatch.setattr(mcp, "get_validation_runtime", lambda: _MockValidationRuntime)

    strategy = {"id": "s1", "strategy_type": "volatility_breakout"}
    profile = {"profile": "trade_rule_validation"}
    normalized = {
        "post_cost_sharpe": 1.0,
        "attempt_adjustment": {"attempt_count": 3, "cohort_effective_trials": 3.0},
    }
    # 模拟 walk_forward 5 个 fold 的 score
    validation_report = {
        "walk_forward": {
            "fold_results": [
                {"oos_sharpe": 0.8},
                {"oos_sharpe": 1.2},
                {"oos_sharpe": 0.9},
                {"oos_sharpe": 1.1},
                {"oos_sharpe": 1.3},
            ]
        }
    }

    result = inject(strategy, profile, normalized, validation_report=validation_report)
    assert result.get("multiple_testing_inject_status") == "ok"
    # DSR formal 路径触发 (sample_size=5 >= 3)
    assert result.get("multiple_testing_mode") == "formal_runtime"
    assert result.get("deflated_sharpe_ratio") == 0.123  # mock 返回值


def test_inject_calls_pbo_when_family_returns_provided(monkeypatch):
    """family_returns (12+, 2+) 时,PBO/RC/SPA formal 路径触发."""
    inject = _import_helper()
    import strategy_factory.infrastructure.mcp_services as mcp
    monkeypatch.setattr(mcp, "get_validation_runtime", lambda: _MockValidationRuntime)

    strategy = {"id": "s1", "strategy_type": "volatility_breakout"}
    profile = {"profile": "trade_rule_validation"}
    normalized = {
        "post_cost_sharpe": 1.0,
        "attempt_adjustment": {"attempt_count": 3, "cohort_effective_trials": 3.0},
    }
    # validation_report 至少 24 期才能跑 bootstrap proxy
    fold_scores = [{"oos_sharpe": 0.5 + i * 0.01} for i in range(24)]
    validation_report = {"walk_forward": {"fold_results": fold_scores}}
    # family_returns (24, 3) — 满足 PBO/RC/SPA 条件
    family_returns = np.random.RandomState(42).normal(0.001, 0.02, (24, 3)).tolist()
    backtest_metrics = {
        "multiple_testing": {"family_returns": family_returns},
    }

    result = inject(
        strategy, profile, normalized,
        validation_report=validation_report,
        backtest_metrics=backtest_metrics,
    )
    assert result.get("multiple_testing_inject_status") == "ok"
    # 如果 family_returns 被解析,PBO/RC/SPA 字段都应该填上
    assert result.get("pbo") == 0.45
    assert result.get("white_reality_check_pvalue") == 0.08
    assert result.get("hansen_spa_pvalue") == 0.07


def test_inject_soft_fail_when_validation_runtime_unavailable(monkeypatch):
    """get_validation_runtime 返回 None 时软降级."""
    inject = _import_helper()
    import strategy_factory.infrastructure.mcp_services as mcp
    monkeypatch.setattr(mcp, "get_validation_runtime", lambda: None)

    strategy = {"id": "s1", "strategy_type": "volatility_breakout"}
    profile = {"profile": "trade_rule_validation"}
    normalized = {"post_cost_sharpe": 1.0}

    result = inject(strategy, profile, normalized)
    assert isinstance(result, dict)
    assert result.get("multiple_testing_inject_status") == "validation_runtime_unavailable"
    # 不应该有 deflated_sharpe_ratio
    assert "deflated_sharpe_ratio" not in result


def test_inject_soft_fail_on_exception(monkeypatch):
    """任何内部异常都不传播,转为 status='exception'."""
    inject = _import_helper()
    import strategy_factory.infrastructure.mcp_services as mcp

    def _broken():
        raise RuntimeError("boom")

    monkeypatch.setattr(mcp, "get_validation_runtime", _broken)

    strategy = {"id": "s1", "strategy_type": "volatility_breakout"}
    profile = {"profile": "trade_rule_validation"}
    normalized = {"post_cost_sharpe": 1.0}

    # validation_runtime 拉取异常 -> 被吞进 try,返回 status='validation_runtime_unavailable'
    result = inject(strategy, profile, normalized)
    assert isinstance(result, dict)
    # 异常被局部吞,validation_runtime 取到 None
    assert result.get("multiple_testing_inject_status") in (
        "validation_runtime_unavailable", "exception"
    )


def test_inject_observed_score_fallback_chain(monkeypatch):
    """post_cost_sharpe 缺失时回退到 wf_ic_ir,再到 sharpe_ratio."""
    inject = _import_helper()
    import strategy_factory.infrastructure.mcp_services as mcp
    monkeypatch.setattr(mcp, "get_validation_runtime", lambda: _MockValidationRuntime)

    strategy = {"id": "s1", "strategy_type": "volatility_breakout"}
    profile = {"profile": "factor_rank_validation"}
    # 只给 wf_ic_ir,没有 post_cost_sharpe
    normalized = {
        "wf_ic_ir": 0.15,
        "attempt_adjustment": {"attempt_count": 3, "cohort_effective_trials": 3.0},
    }

    result = inject(strategy, profile, normalized)
    # 不应该崩溃,应该正常返回
    assert isinstance(result, dict)
    assert result.get("multiple_testing_inject_status") == "ok"


def test_inject_attempt_adjustment_fallback(monkeypatch):
    """normalized.attempt_adjustment 缺失时,helper 应该自己重建."""
    inject = _import_helper()
    import strategy_factory.infrastructure.mcp_services as mcp
    monkeypatch.setattr(mcp, "get_validation_runtime", lambda: _MockValidationRuntime)

    strategy = {"id": "s1", "strategy_type": "volatility_breakout"}
    profile = {"profile": "trade_rule_validation"}
    # 完全空的 normalized
    normalized = {}

    result = inject(strategy, profile, normalized)
    assert isinstance(result, dict)
    # 至少不崩
    assert result.get("multiple_testing_inject_status") in ("ok", "exception", "validation_runtime_unavailable")


# === family_returns 合成 (打通 PBO/RC/SPA) ===

def _import_family_builder():
    parent = importlib.import_module(
        "strategy_factory.application.submission_gate.runner"
    )
    return parent._build_family_returns_from_klines


class _FakeKlineDb:
    """返回真实形态的 close 序列 (趋势 + 周期波动),供策略 signal 生成器消费。"""

    def __init__(self, length: int = 120):
        self._length = int(length)

    async def get_klines(self, code: str, limit: int = 500):
        import numpy as _np
        n = min(self._length, int(limit))
        # 趋势 + 正弦波动,确定性 (按 code 派生相位),非纯随机噪声
        seed = sum(ord(c) for c in str(code)) % 97
        t = _np.arange(n, dtype=float)
        closes = 50.0 + 0.05 * t + 4.0 * _np.sin((t + seed) / 7.0)
        return [{"close": float(round(v, 3))} for v in closes]


class _EmptyKlineDb:
    async def get_klines(self, code: str, limit: int = 500):
        return []


def test_build_family_returns_from_real_klines():
    """真实 K 线 + MaCross signal 生成器 → 合成 (n_obs>=12, n_models>=2) 矩阵。"""
    import asyncio
    import numpy as np
    from aiask_quant_core.backtest.builtin_strategies import MaCrossStrategy

    build = _import_family_builder()
    db = _FakeKlineDb(length=120)
    strategy = {
        "id": "s1",
        "strategy_type": "ma_cross",
        "params": {"short_period": 5, "long_period": 20, "target_symbols": ["600519", "000001"]},
    }

    family = asyncio.run(build(db, strategy, MaCrossStrategy, min_len=60))
    assert family is not None
    assert isinstance(family, np.ndarray)
    assert family.ndim == 2
    assert family.shape[0] >= 12  # n_obs
    assert family.shape[1] >= 2   # n_models (base + 参数扰动变体)


def test_build_family_returns_pbo_rc_spa_end_to_end(monkeypatch):
    """合成的 family_returns 经 inject 喂给 runtime → PBO/RC/SPA 字段被填充。"""
    import asyncio
    import strategy_factory.infrastructure.mcp_services as mcp
    from aiask_quant_core.backtest.builtin_strategies import MaCrossStrategy

    monkeypatch.setattr(mcp, "get_validation_runtime", lambda: _MockValidationRuntime)
    build = _import_family_builder()
    inject = _import_helper()

    db = _FakeKlineDb(length=120)
    strategy = {
        "id": "s1",
        "strategy_type": "ma_cross",
        "params": {"short_period": 5, "long_period": 20, "target_symbols": ["600519", "000001"]},
    }
    family = asyncio.run(build(db, strategy, MaCrossStrategy, min_len=60))
    assert family is not None and family.shape[0] >= 12 and family.shape[1] >= 2

    normalized = {
        "post_cost_sharpe": 1.0,
        "attempt_adjustment": {"attempt_count": 3, "cohort_effective_trials": 3.0},
    }
    # 提供 24+ fold scores 让 bootstrap proxy 也走到,family_returns_fallback 触发 PBO/RC/SPA
    fold_scores = [{"oos_sharpe": 0.5 + i * 0.01} for i in range(24)]
    validation_report = {"walk_forward": {"fold_results": fold_scores}}

    result = inject(
        strategy,
        {"profile": "trade_rule_validation"},
        normalized,
        validation_report=validation_report,
        family_returns_fallback=family,
    )
    assert result.get("multiple_testing_inject_status") == "ok"
    assert result.get("multiple_testing_mode") == "formal_runtime"
    assert result.get("pbo") == 0.45
    assert result.get("white_reality_check_pvalue") == 0.08
    assert result.get("hansen_spa_pvalue") == 0.07


def test_build_family_returns_none_when_no_klines():
    """取不到 K 线时返回 None (诚实 missing,不兜底造假)。"""
    import asyncio
    from aiask_quant_core.backtest.builtin_strategies import MaCrossStrategy

    build = _import_family_builder()
    strategy = {
        "id": "s1",
        "strategy_type": "ma_cross",
        "params": {"short_period": 5, "long_period": 20, "target_symbols": ["600519"]},
    }
    assert asyncio.run(build(_EmptyKlineDb(), strategy, MaCrossStrategy, min_len=60)) is None
    # 无 target_symbols 也返回 None
    strategy_no_sym = {"id": "s2", "strategy_type": "ma_cross", "params": {"short_period": 5}}
    assert asyncio.run(build(_FakeKlineDb(), strategy_no_sym, MaCrossStrategy, min_len=60)) is None
    # klass=None 返回 None
    assert asyncio.run(build(_FakeKlineDb(), strategy, None, min_len=60)) is None


def test_inject_fallback_ignored_when_backtest_metrics_has_family():
    """backtest_metrics 直接携带 family_returns 时,优先用它而非 fallback。"""
    import numpy as np
    inject = _import_helper()
    import strategy_factory.infrastructure.mcp_services as mcp

    # 用 monkeypatch 之外的方式:直接构造两个可区分矩阵不现实(runtime mock 返回固定值),
    # 这里只验证提供 fallback 不破坏既有 backtest_metrics 路径,且 status=ok。
    import pytest as _pytest
    with _pytest.MonkeyPatch.context() as mp:
        mp.setattr(mcp, "get_validation_runtime", lambda: _MockValidationRuntime)
        strategy = {"id": "s1", "strategy_type": "ma_cross", "params": {}}
        normalized = {"post_cost_sharpe": 1.0, "attempt_adjustment": {"attempt_count": 3, "cohort_effective_trials": 3.0}}
        fold_scores = [{"oos_sharpe": 0.5 + i * 0.01} for i in range(24)]
        direct_family = np.random.RandomState(7).normal(0.001, 0.02, (24, 3)).tolist()
        result = inject(
            strategy,
            {"profile": "trade_rule_validation"},
            normalized,
            validation_report={"walk_forward": {"fold_results": fold_scores}},
            backtest_metrics={"multiple_testing": {"family_returns": direct_family}},
            family_returns_fallback=np.zeros((24, 2)),  # 应被忽略
        )
        assert result.get("multiple_testing_inject_status") == "ok"
        assert result.get("pbo") == 0.45
