"""FIX-17 (F-N40-1) 回归：smart_stock_diagnosis 证据三态归类，中性不再误判为风险。"""

import pytest

from akshare_mcp.tools.semantic.diagnosis import _build_evidence


def _ctx(**over):
    base = {
        "valuation": {"pe": 19.91},
        "fundamentals": {"roe": 10.06, "debt_ratio": 15.32},
        "technical": {"rsi_14": 45.22, "ma_alignment": "bearish"},
        "momentum": {"mom_20d": -0.0536, "market_regime": "bearish"},
        "risk": {"max_drawdown_250d": 0.1811},
    }
    base.update(over)
    return base


def test_neutral_rsi_not_counted_as_risk():
    evidence, highlights, risks, rec, _ = _build_evidence(_ctx())
    # RSI 45.22 属中性区间(30~70)，不应进 risks
    assert not any("RSI" in r for r in risks), f"中性RSI被误判为风险: {risks}"


def test_neutral_regime_not_risk_when_neutral():
    evidence, highlights, risks, rec, _ = _build_evidence(
        _ctx(momentum={"mom_20d": 0.001, "market_regime": "neutral"})
    )
    # market_regime=neutral 不应进 risks，也不应进 highlights
    assert not any("市场环境" in r for r in risks)
    assert not any("市场环境" in h for h in highlights)
    # mom_20d=0.001 在 ±2% 内属中性
    assert not any("20日动量" in r for r in risks)


def test_strong_stock_not_auto_sell():
    """强多头标的（低估值+高ROE+多头+正动量+低波动）应给 buy/hold，不应 sell。"""
    ctx = {
        "valuation": {"pe": 15.0},
        "fundamentals": {"roe": 22.0, "debt_ratio": 25.0, "revenue_yoy": 18.0},
        "technical": {"rsi_14": 55.0, "ma_alignment": "bullish"},
        "momentum": {"mom_20d": 0.06, "market_regime": "bullish"},
        "risk": {"volatility_20d": 0.02, "max_drawdown_250d": 0.12},
    }
    _, highlights, risks, rec, _ = _build_evidence(ctx)
    assert rec in ("buy", "hold"), f"强多头标的被判 {rec}; highlights={highlights} risks={risks}"
    assert rec != "sell"


def test_genuine_weak_stock_still_flagged():
    """真正弱势标的（高估值+低ROE+空头+大跌+高负债）应给 sell/wait。"""
    ctx = {
        "valuation": {"pe": 80.0},
        "fundamentals": {"roe": 3.0, "debt_ratio": 85.0, "revenue_yoy": -10.0},
        "technical": {"rsi_14": 75.0, "ma_alignment": "bearish"},
        "momentum": {"mom_20d": -0.08, "market_regime": "bearish"},
        "risk": {"volatility_20d": 0.12, "max_drawdown_250d": 0.55},
    }
    _, highlights, risks, rec, _ = _build_evidence(ctx)
    assert rec in ("sell", "wait"), f"弱势标的被判 {rec}; risks={risks}"
    assert len(risks) >= 4


def test_diagnosis_not_universally_sell():
    """混合中性标的（原 bug 会判 sell）现在应为 hold/wait，而非 sell。"""
    _, highlights, risks, rec, _ = _build_evidence(_ctx())
    # 茅台样本：PE低(利好)+负债低(利好)+回撤小(利好) vs 均线空头+市场空头(风险)
    # 净证据不应到 -3，故不应 sell
    assert rec != "sell", f"中性混合标的仍被判 sell; highlights={highlights} risks={risks}"
