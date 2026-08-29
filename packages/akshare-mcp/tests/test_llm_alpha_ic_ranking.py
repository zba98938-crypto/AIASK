"""B2: 本地因子池真实 IC 重排 (_rank_pool_by_real_ic / _safe_formula_ic)。"""

from __future__ import annotations

import numpy as np
import pandas as pd

from akshare_mcp.services.llm_alpha import LLMAlphaMiner


def _trending_frame(n: int = 260, *, seed: int = 7) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    close = pd.Series(np.cumsum(rng.normal(0.1, 0.5, n)) + 100).abs() + 1.0
    volume = pd.Series(rng.integers(100_000, 500_000, n)).astype(float)
    return pd.DataFrame(
        {
            "close": close,
            "open": close * 0.99,
            "high": close * 1.02,
            "low": close * 0.98,
            "volume": volume,
            "amount": close * volume,
        }
    )


def test_pool_is_ordered_by_descending_real_ic() -> None:
    """重排后,带可求值公式的因子应按 |真实IC| 降序排列(机制验证,不绑定具体类别)。"""
    miner = LLMAlphaMiner()
    df = _trending_frame()
    pool = miner._build_local_candidate_pool(df)
    assert pool, "pool should not be empty"
    fr = df["close"].pct_change(5).shift(-5)
    ns = {c: df[c] for c in df.columns}
    ics = []
    for cand in pool:
        ic = miner._safe_formula_ic(str(cand.get("formula") or ""), ns, fr)
        if ic is not None:
            ics.append(ic)
    # 已评分因子的 |IC| 序列应非递增
    assert ics == sorted(ics, reverse=True)
    assert any(ic > 0 for ic in ics), "at least one factor should have real signal"


def test_safe_formula_ic_returns_float_for_simple_formula() -> None:
    miner = LLMAlphaMiner()
    df = _trending_frame()
    fr = df["close"].pct_change(5).shift(-5)
    ns = {c: df[c] for c in df.columns}
    ic = miner._safe_formula_ic("(close.pct_change(60) - close.pct_change(20))", ns, fr)
    assert ic is not None
    assert 0.0 <= ic <= 1.0


def test_safe_formula_ic_skips_assignment_formula() -> None:
    """文本信号公式含赋值/分号,无法单表达式求值 → None,不参与 IC 排序。"""
    miner = LLMAlphaMiner()
    df = _trending_frame()
    fr = df["close"].pct_change(5).shift(-5)
    ns = {c: df[c] for c in df.columns}
    assert miner._safe_formula_ic("text_signal_score=0.5; volume_confirm=1", ns, fr) is None


def test_safe_formula_ic_blocks_injection() -> None:
    """受限命名空间禁用 __builtins__,注入式公式安全返回 None。"""
    miner = LLMAlphaMiner()
    df = _trending_frame()
    fr = df["close"].pct_change(5).shift(-5)
    ns = {c: df[c] for c in df.columns}
    assert miner._safe_formula_ic('__import__("os").system("echo x")', ns, fr) is None


def test_rank_falls_back_to_heuristic_when_no_close_col() -> None:
    """无 close 列时退回启发式排序,不抛错。"""
    miner = LLMAlphaMiner()
    df = _trending_frame()
    pool = [
        {"name": "a", "category": "momentum", "formula": "volume.pct_change(5)"},
        {"name": "b", "category": "reversal", "formula": "volume.pct_change(20)"},
    ]
    ranked = miner._rank_pool_by_real_ic(
        pool,
        market_data=df,
        close_col=None,
        period_return=0.2,
        volatility=0.0,
    )
    # period_return>=0.1 → momentum 优先
    assert ranked[0]["category"] == "momentum"
    assert len(ranked) == 2
