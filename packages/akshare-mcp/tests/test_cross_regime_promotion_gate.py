"""INVERT-DESIGN P3 改动B：跨 regime 晋升门单元测试。

验证 evaluate_cross_regime_skill 的判定逻辑：
- 全部达标 regime 标签 skill_lcb>0 → passed
- 任一达标标签 skill_lcb<=0 → 不 passed，记录 negative_labels
- 样本不足（n<min_n）→ 不评估、不阻断（交全局 skill_lcb 把关）
"""

from __future__ import annotations


def _eval(hit_rate_by_regime, **kw):
    from akshare_mcp.services.strategy_lifecycle_shared.common import (
        evaluate_cross_regime_skill,
    )

    return evaluate_cross_regime_skill(hit_rate_by_regime, **kw)


def test_cross_regime_all_positive_passes():
    payload = {
        "trend_regime": {
            "uptrend": {"skill_lcb": 0.04, "n": 40},
            "range": {"skill_lcb": 0.02, "n": 30},
        }
    }
    result = _eval(payload, min_n=20)
    assert result["evaluated"] is True
    assert result["passed"] is True
    assert result["negative_labels"] == []


def test_cross_regime_one_negative_fails():
    payload = {
        "trend_regime": {
            "uptrend": {"skill_lcb": 0.05, "n": 40},
            "range": {"skill_lcb": -0.01, "n": 35},
        }
    }
    result = _eval(payload, min_n=20)
    assert result["evaluated"] is True
    assert result["passed"] is False
    assert "trend_regime:range" in result["negative_labels"]


def test_cross_regime_low_sample_not_blocking():
    # 两个标签都样本不足 → 不评估、不阻断
    payload = {
        "trend_regime": {
            "uptrend": {"skill_lcb": -0.10, "n": 5},
            "range": {"skill_lcb": -0.20, "n": 8},
        }
    }
    result = _eval(payload, min_n=20)
    assert result["evaluated"] is False
    assert result["passed"] is True
    assert result["negative_labels"] == []


def test_cross_regime_empty_input_not_blocking():
    result = _eval({}, min_n=20)
    assert result["evaluated"] is False
    assert result["passed"] is True


def test_cross_regime_mixed_sample_only_evaluates_qualified():
    # uptrend 达标且正；range 样本不足（即使负）应被忽略 → passed
    payload = {
        "trend_regime": {
            "uptrend": {"skill_lcb": 0.03, "n": 50},
            "range": {"skill_lcb": -0.30, "n": 3},
        }
    }
    result = _eval(payload, min_n=20)
    assert result["evaluated"] is True
    assert result["passed"] is True
    assert result["evaluated_labels"] == ["trend_regime:uptrend"]
