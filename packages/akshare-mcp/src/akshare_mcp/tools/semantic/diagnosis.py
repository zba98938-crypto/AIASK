"""智能股票诊断。"""

from datetime import datetime

from ...utils import (
    attach_argument_contract_meta,
    fail,
    ok,
    resolve_canonical_arg,
    resolve_existing_security_code_async,
)
from ..investment_analysis import get_investment_analysis


def _maybe_float(value):
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _build_evidence(context: dict) -> tuple[list[dict], list[str], list[str], str, str]:
    valuation = context.get("valuation", {}) if isinstance(context, dict) else {}
    fundamentals = context.get("fundamentals", {}) if isinstance(context, dict) else {}
    technical = context.get("technical", {}) if isinstance(context, dict) else {}
    momentum = context.get("momentum", {}) if isinstance(context, dict) else {}
    risk = context.get("risk", {}) if isinstance(context, dict) else {}
    evidence, highlights, risks = [], [], []

    def add(category: str, signal: str, value, interpretation: str, positive: bool | None = None):
        evidence.append({"category": category, "signal": signal, "value": value, "interpretation": interpretation})
        if positive is True:
            highlights.append(interpretation)
        elif positive is False:
            risks.append(interpretation)

    pe = _maybe_float(valuation.get("pe"))
    relative_pe = _maybe_float(valuation.get("industry_relative_pe"))
    if pe is not None:
        # 三态：低估(<20)利好 / 高估(>50)风险 / 20~50 中性
        pe_pos = True if 0 < pe < 20 else (False if pe > 50 else None)
        add("valuation", "pe", round(pe, 2), f"PE为{pe:.2f}", positive=pe_pos)
    if relative_pe is not None:
        rel_pos = True if relative_pe < 0.9 else (False if relative_pe > 1.2 else None)
        rel_text = "相对行业估值偏低" if relative_pe < 0.9 else ("相对行业估值偏高" if relative_pe > 1.2 else "相对行业估值中性")
        add("valuation", "industry_relative_pe", round(relative_pe, 4), rel_text, positive=rel_pos)

    roe = _maybe_float(fundamentals.get("roe"))
    debt_ratio = _maybe_float(fundamentals.get("debt_ratio"))
    revenue_yoy = _maybe_float(fundamentals.get("revenue_yoy"))
    if roe is not None:
        # 三态：高ROE(>=15)利好 / 低ROE(<8)风险 / 8~15 中性
        roe_pos = True if roe >= 15 else (False if roe < 8 else None)
        add("fundamental", "roe", round(roe, 2), f"ROE为{roe:.2f}", positive=roe_pos)
    if revenue_yoy is not None:
        rev_pos = True if revenue_yoy >= 10 else (False if revenue_yoy < 0 else None)
        add("fundamental", "revenue_yoy", round(revenue_yoy, 2), f"营收增速为{revenue_yoy:.2f}%", positive=rev_pos)
    if debt_ratio is not None:
        # 三态：低负债(<40)利好 / 高负债(>70)风险 / 40~70 中性
        debt_pos = True if debt_ratio < 40 else (False if debt_ratio > 70 else None)
        add("fundamental", "debt_ratio", round(debt_ratio, 2), f"负债率为{debt_ratio:.2f}%", positive=debt_pos)

    rsi = _maybe_float(technical.get("rsi_14"))
    ma_alignment = technical.get("ma_alignment")
    if rsi is not None:
        # 三态：超卖(<30)反转机会 / 超买(>70)风险 / 30~70 中性（不再把中性 RSI 当风险）
        rsi_pos = True if rsi < 30 else (False if rsi > 70 else None)
        rsi_text = f"RSI为{rsi:.2f}（超卖）" if rsi < 30 else (f"RSI为{rsi:.2f}（超买）" if rsi > 70 else f"RSI为{rsi:.2f}（中性）")
        add("technical", "rsi_14", round(rsi, 2), rsi_text, positive=rsi_pos)
    if ma_alignment:
        # 三态：多头利好 / 空头风险 / mixed 等中性
        ma_pos = True if ma_alignment == "bullish" else (False if ma_alignment == "bearish" else None)
        add("technical", "ma_alignment", ma_alignment, f"均线结构为{ma_alignment}", positive=ma_pos)

    mom_20d = _maybe_float(momentum.get("mom_20d"))
    market_regime = momentum.get("market_regime")
    if mom_20d is not None:
        # 三态：明显正动量(>2%)利好 / 明显负动量(<-2%)风险 / ±2% 内中性
        mom_pos = True if mom_20d > 0.02 else (False if mom_20d < -0.02 else None)
        add("momentum", "mom_20d", round(mom_20d, 4), f"20日动量为{mom_20d:.4f}", positive=mom_pos)
    if market_regime:
        # 三态：bullish 利好 / bearish 风险 / neutral 中性（不再把 neutral 当利好）
        regime_pos = True if market_regime == "bullish" else (False if market_regime == "bearish" else None)
        add("momentum", "market_regime", market_regime, f"市场环境判定为{market_regime}", positive=regime_pos)

    vol20 = _maybe_float(risk.get("volatility_20d"))
    max_dd = _maybe_float(risk.get("max_drawdown_250d"))
    if vol20 is not None:
        # 三态：低波动(<4%)利好 / 高波动(>8%)风险 / 中间中性
        vol_pos = True if vol20 < 0.04 else (False if vol20 > 0.08 else None)
        add("risk", "volatility_20d", round(vol20, 4), f"20日波动率为{vol20:.4f}", positive=vol_pos)
    if max_dd is not None:
        # 三态：小回撤(<25%)利好 / 大回撤(>40%)风险 / 中间中性
        dd_pos = True if max_dd < 0.25 else (False if max_dd > 0.40 else None)
        add("risk", "max_drawdown_250d", round(max_dd, 4), f"250日最大回撤为{max_dd:.4f}", positive=dd_pos)

    # F-N40-1 修复：基于净证据（利好 vs 风险）相对判定，而非"risks 绝对计数>=4 一刀切 sell"。
    # 旧逻辑把中性指标全塞 risk 桶 + 绝对阈值 → 对几乎所有标的输出 sell。
    pos_n, risk_n = len(highlights), len(risks)
    net = pos_n - risk_n
    if pos_n >= 4 and risk_n <= 1:
        recommendation, recommendation_text = "buy", "偏积极，可重点跟踪或分批布局"
    elif net >= 2:
        recommendation, recommendation_text = "buy", "利好证据明显多于风险，可分批布局"
    elif net <= -3:
        recommendation, recommendation_text = "sell", "风险证据显著多于利好，建议回避或降低仓位"
    elif net <= -1:
        recommendation, recommendation_text = "wait", "风险略多于利好，建议等待更明确信号"
    else:
        recommendation, recommendation_text = "hold", "多空因素交织，适合继续持有观察"
    return evidence, highlights, risks, recommendation, recommendation_text


async def smart_stock_diagnosis(
    code: str = "",
    stock_code: str = "",
    symbol: str = "",
    ticker: str = "",
):
    """结构化股票诊断，输出证据而非硬编码总分。"""
    try:
        raw_code, alias_hits, _ = resolve_canonical_arg(
            "code",
            code,
            stock_code=stock_code,
            symbol=symbol,
            ticker=ticker,
        )
        code, _, error = await resolve_existing_security_code_async(code=raw_code)
        canonical_args = {"code": code or raw_code}
        if error:
            return attach_argument_contract_meta(
                fail(error),
                canonical_tool="smart_stock_diagnosis",
                canonical_args=canonical_args,
                alias_hits=alias_hits,
            )
        analysis = await get_investment_analysis(code)
        if not analysis.get("success"):
            return attach_argument_contract_meta(
                fail(analysis.get("error", "diagnosis_failed")),
                canonical_tool="smart_stock_diagnosis",
                canonical_args=canonical_args,
                alias_hits=alias_hits,
            )
        context = analysis.get("data", {}) or {}
        evidence, highlights, risks, recommendation, recommendation_text = _build_evidence(context)
        basic_info = context.get("basic_info", {}) if isinstance(context.get("basic_info"), dict) else {}
        price_context = context.get("price_context", {}) if isinstance(context.get("price_context"), dict) else {}
        return attach_argument_contract_meta(
            ok({
                "code": code,
                "name": basic_info.get("name", ""),
                "recommendation": recommendation,
                "recommendation_text": recommendation_text,
                "decision_mode": "context_aggregator",
                "analysis_context": context,
                "evidence": evidence,
                "highlights": highlights,
                "risks": risks,
                "summary": {
                    "positive_evidence_count": len(highlights),
                    "risk_count": len(risks),
                    "market_regime": context.get("momentum", {}).get("market_regime"),
                },
                "current_price": price_context.get("current_price"),
                "analysis_date": price_context.get("analysis_date") or datetime.now().strftime("%Y-%m-%d"),
            }),
            canonical_tool="smart_stock_diagnosis",
            canonical_args=canonical_args,
            alias_hits=alias_hits,
        )
    except Exception as e:
        return attach_argument_contract_meta(
            fail(str(e)),
            canonical_tool="smart_stock_diagnosis",
            canonical_args={"code": code or ""},
            alias_hits=[],
        )
