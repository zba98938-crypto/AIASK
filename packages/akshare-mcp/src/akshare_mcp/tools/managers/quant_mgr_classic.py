"""Classic factor action handlers for quant_manager."""

from __future__ import annotations

import logging
import os
from datetime import datetime
from typing import Any, Callable

import numpy as np

from ...storage import get_db
from ..quant import SUPPORTED_FACTORS, run_factor_group_backtest, run_factor_ic_analysis
from ..quant_definitions import _normalize_factor_name
from ..quant_engine import _extract_style_exposures
from .quant_mgr_helpers import (
    _as_code_list,
    _compute_alternative_factors_for_code,
    _compute_scalar_factor_bundle,
    _load_valuation_snapshot,
    _parse_date_value,
    _safe_float,
    _select_financial_snapshot,
    _sort_klines_ascending,
)

logger = logging.getLogger(__name__)


def _batch_ic_neutralize_enabled() -> bool:
    """P0-2: 每日 batch IC 是否走行业/市值/beta 中性化的 dual-IC 路径。

    默认 OFF：保持历史行为（AnalysisFactorsMixin.calculate_factor_ic，无中性化）。
    ON：改用 FactorAnalyzer.calculate_ic_dual，对齐 factor_vals 的 per-stock 风格暴露做截面残差中性化。
    """
    raw = os.getenv("STRATEGY_FACTORY_BATCH_IC_NEUTRALIZE_ENABLED")
    if raw is None:
        return False
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}

_CALCULATE_FACTORS_SUPPORTED = {
    "momentum",
    "value",
    "quality",
    "growth",
    "volatility",
    "liquidity",
    "sentiment",
    "event",
    "capital_flow",
    "alternative_composite",
}
_BATCH_FACTORS_SUPPORTED = {"momentum", "value", "quality", "growth", "volatility", "reversal"}
_FACTOR_CATEGORY_ALIAS_MAP = {
    "mom_1d": "momentum",
    "mom_5d": "momentum",
    "mom_10d": "momentum",
    "mom_60d": "momentum",
    "momentum": "momentum",
    "atr_14": "volatility",
    "atr_20": "volatility",
    "volatility": "volatility",
    "rsi_14": "momentum",
    "macd": "momentum",
}


async def _load_klines_with_fallback(
    *,
    db: Any,
    code: str,
    limit: int = 252,
) -> tuple[list[dict], list[str]]:
    klines = await db.get_klines(code, limit=limit)
    source_chain = ["db.get_klines"]
    if klines:
        return list(klines), source_chain

    # PR-F4: 因子计算场景下，本地 DB 已有 5 年历史数据，不需要网络 fallback
    # 避免网络超时导致大量股票计算失败
    return [], source_chain


async def handle_calculate_factors(
    *,
    kw: dict[str, Any],
    code: str | None,
    db: Any,
    ok: Callable[..., dict],
    fail: Callable[..., dict],
) -> dict:
    if not code:
        return fail("需要提供股票代码（code）")

    raw_factors = kw.get("factors", ["momentum", "value", "quality", "growth"])
    if isinstance(raw_factors, str):
        raw_factors = [item.strip() for item in raw_factors.split(",") if item.strip()]

    factors: list[str] = []
    unknown_factors: list[str] = []
    for factor in list(raw_factors or []):
        normalized_factor = _normalize_factor_name(str(factor))
        alias_category = _FACTOR_CATEGORY_ALIAS_MAP.get(normalized_factor)
        if alias_category in _CALCULATE_FACTORS_SUPPORTED:
            factors.append(alias_category)
            continue
        if normalized_factor in _CALCULATE_FACTORS_SUPPORTED:
            factors.append(normalized_factor)
            continue
        meta = SUPPORTED_FACTORS.get(normalized_factor) or {}
        category = str(meta.get("category") or "").strip().lower()
        if category in _CALCULATE_FACTORS_SUPPORTED:
            factors.append(category)
            continue
        unknown_factors.append(str(factor))

    factors = list(dict.fromkeys(factors)) or ["momentum", "value", "quality", "growth"]
    if unknown_factors:
        return fail(
            f"Unsupported factors: {unknown_factors}. "
            f"Supported: {sorted(_CALCULATE_FACTORS_SUPPORTED)}"
        )

    klines, source_chain = await _load_klines_with_fallback(db=db, code=code, limit=252)
    if not klines:
        return fail(
            f"未找到 {code} 的K线数据。\n\n"
            f"请先运行数据预热: data_warmup(action='warmup', stocks=['{code}'], lookback_days=252)",
            source_chain=source_chain,
        )

    ordered_klines = _sort_klines_ascending(klines)
    closes = [item.get("close") for item in ordered_klines if isinstance(item, dict) and item.get("close") is not None]
    if len(closes) < 2:
        return fail("K线数据不足，无法计算因子", source_chain=source_chain)

    financials = await db.get_financials(code, limit=4)
    latest_financial = _select_financial_snapshot(financials)
    valuation_snapshot = await _load_valuation_snapshot(db, code)
    factor_values: dict[str, dict[str, Any]] = {}

    if "momentum" in factors:
        momentum_20 = (closes[-1] - closes[-20]) / closes[-20] if len(closes) >= 20 and closes[-20] else 0.0
        momentum_60 = (closes[-1] - closes[-60]) / closes[-60] if len(closes) >= 60 and closes[-60] else 0.0
        momentum_120 = (closes[-1] - closes[-120]) / closes[-120] if len(closes) >= 120 and closes[-120] else 0.0
        factor_values["momentum"] = {
            "momentum_20d": float(momentum_20),
            "momentum_60d": float(momentum_60),
            "momentum_120d": float(momentum_120),
            "score": float((momentum_20 + momentum_60 + momentum_120) / 3.0),
            "level": "strong" if momentum_60 > 0.1 else ("weak" if momentum_60 < -0.1 else "neutral"),
        }

    if "value" in factors:
        pe_ratio = _safe_float(valuation_snapshot.get("pe_ratio"), 0.0)
        pb_ratio = _safe_float(valuation_snapshot.get("pb_ratio"), 0.0)
        ps_ratio = _safe_float(latest_financial.get("ps_ratio"), 0.0)
        pe_score = 1.0 / pe_ratio if pe_ratio > 0 else 0.0
        pb_score = 1.0 / pb_ratio if pb_ratio > 0 else 0.0
        value_components = [score for score in (pe_score, pb_score) if score > 0]
        if ps_ratio > 0:
            value_components.append(1.0 / ps_ratio)
        value_score = sum(value_components) / len(value_components) if value_components else 0.0
        factor_values["value"] = {
            "pe_ratio": pe_ratio,
            "pb_ratio": pb_ratio,
            "ps_ratio": ps_ratio,
            "score": float(value_score),
            "level": "undervalued" if pe_ratio > 0 and pe_ratio < 15 and pb_ratio > 0 and pb_ratio < 2 else (
                "overvalued" if pe_ratio > 30 else "fair"
            ),
        }

    if "quality" in factors:
        roe = _safe_float(latest_financial.get("roe"), 0.0)
        roa = _safe_float(latest_financial.get("roa"), 0.0)
        gross_margin = _safe_float(latest_financial.get("gross_margin"), 0.0)
        debt_ratio = _safe_float(latest_financial.get("debt_ratio"), 1.0)
        quality_score = (
            (roe / 30 if roe > 0 else 0) * 0.4
            + (roa / 15 if roa > 0 else 0) * 0.3
            + (gross_margin / 50 if gross_margin > 0 else 0) * 0.2
            + ((1 - debt_ratio) if debt_ratio < 1 else 0) * 0.1
        )
        factor_values["quality"] = {
            "roe": roe,
            "roa": roa,
            "gross_margin": gross_margin,
            "debt_ratio": debt_ratio,
            "score": float(quality_score),
            "level": "high" if roe > 15 and debt_ratio < 0.5 else ("low" if roe < 5 else "medium"),
        }

    if "growth" in factors:
        revenue_growth = _safe_float(latest_financial.get("revenue_growth"), 0.0)
        profit_growth = _safe_float(latest_financial.get("profit_growth"), 0.0)
        growth_score = float(max(min((revenue_growth + profit_growth) / 200.0, 1.0), -1.0))
        factor_values["growth"] = {
            "revenue_growth": revenue_growth,
            "profit_growth": profit_growth,
            "score": growth_score,
            "level": "high" if revenue_growth > 15 and profit_growth > 15 else (
                "low" if revenue_growth < 0 and profit_growth < 0 else "medium"
            ),
        }

    if "volatility" in factors:
        prices = np.array(closes, dtype=float)
        returns = np.diff(prices) / prices[:-1]
        volatility = float(np.std(returns) * np.sqrt(252)) if len(returns) > 1 else 0.0
        factor_values["volatility"] = {
            "annual_volatility": volatility,
            "score": float(1.0 / volatility if volatility > 0 else 0.0),
            "level": "high" if volatility > 0.4 else ("low" if volatility < 0.2 else "medium"),
        }

    if "liquidity" in factors:
        volumes = [float(item.get("volume", 0) or 0) for item in klines[-20:]]
        amounts = [float(item.get("amount", 0) or 0) for item in klines[-20:]]
        avg_volume = float(np.mean(volumes)) if volumes else 0.0
        avg_amount = float(np.mean(amounts)) if amounts else 0.0
        factor_values["liquidity"] = {
            "avg_volume_20d": avg_volume,
            "avg_amount_20d": avg_amount,
            "score": float(avg_amount / 1e8),
            "level": "high" if avg_amount > 1e8 else ("low" if avg_amount < 1e7 else "medium"),
        }

    requested_alt = any(factor in factors for factor in ("sentiment", "event", "capital_flow", "alternative_composite"))
    if requested_alt:
        alt_days = int(kw.get("alt_lookback_days", 30) or 30)
        alt_limit = int(kw.get("alt_limit", 20) or 20)
        alt_factors, alt_sources = await _compute_alternative_factors_for_code(
            db=db,
            code=code,
            lookback_days=alt_days,
            limit=alt_limit,
        )
        for key in ("sentiment", "event", "capital_flow", "alternative_composite"):
            if key in factors and key in alt_factors:
                factor_values[key] = alt_factors[key]
        source_chain = source_chain + alt_sources

    composite_score = float(np.mean([item.get("score", 0) for item in factor_values.values()])) if factor_values else 0.0
    return ok(
        {
            "code": code,
            "factors": factor_values,
            "composite_score": composite_score,
            "data_window": {
                "kline_bars": len(closes),
                "financial_records": len(financials) if isinstance(financials, list) else (1 if isinstance(financials, dict) else 0),
            },
        },
        source_chain=source_chain + ["db.get_financials"],
    )


async def handle_factor_ic(
    *,
    kw: dict[str, Any],
    fail: Callable[..., dict],
    with_meta: Callable[..., dict],
) -> dict:
    factor_name = str(kw.get("factor_name", kw.get("factor", "momentum")) or "").strip().lower()
    period = kw.get("period", 20)
    codes = kw.get("codes", [])
    enable_neutralization = bool(kw.get("enable_neutralization", True))
    bootstrap_n = int(kw.get("bootstrap_n", 1000) or 1000)
    bootstrap_confidence = float(kw.get("bootstrap_confidence", 0.95) or 0.95)

    if factor_name not in SUPPORTED_FACTORS:
        return fail(
            f"Unsupported factor: {factor_name}. "
            f"Supported: {sorted(SUPPORTED_FACTORS.keys())}"
        )
    if not isinstance(codes, list) or not codes:
        return fail("需要提供股票列表（codes）")

    result = await run_factor_ic_analysis(
        codes=codes,
        factor=factor_name,
        period=period,
        enable_neutralization=enable_neutralization,
        bootstrap_n=bootstrap_n,
        bootstrap_confidence=bootstrap_confidence,
    )
    if result.get("success") and isinstance(result.get("data"), dict):
        result["data"]["factor_name"] = result["data"].get("factor", factor_name)
        result["data"]["description"] = "IC>0 表示因子与未来收益同向关联，IC<0 则反向"

    return with_meta(
        result,
        source_chain=["db.get_klines", "db.get_financials(optional)", "factor_analysis_dual_ic", "bootstrap_ci"],
    )


async def handle_backtest_factor(
    *,
    kw: dict[str, Any],
    fail: Callable[..., dict],
    with_meta: Callable[..., dict],
) -> dict:
    factor_name = str(kw.get("factor_name", kw.get("factor", "momentum")) or "").strip().lower()
    start_date = kw.get("start_date")
    end_date = kw.get("end_date")
    groups = kw.get("groups", 5)
    holding_days = kw.get("holding_days", 20)
    factor_lookback = kw.get("factor_lookback", 20)
    codes = kw.get("codes", [])

    if factor_name not in SUPPORTED_FACTORS:
        return fail(
            f"Unsupported factor: {factor_name}. "
            f"Supported: {sorted(SUPPORTED_FACTORS.keys())}"
        )
    if not isinstance(codes, list) or not codes:
        return fail("需要提供股票列表（codes）")

    result = await run_factor_group_backtest(
        codes=codes,
        factor=factor_name,
        groups=groups,
        holding_days=holding_days,
        factor_lookback=factor_lookback,
    )
    if result.get("success") and isinstance(result.get("data"), dict):
        result["data"]["factor_name"] = result["data"].get("factor", factor_name)
        result["data"]["start_date"] = start_date
        result["data"]["end_date"] = end_date

    return with_meta(
        result,
        source_chain=["db.get_klines", "db.get_financials(optional)", "numpy-grouping"],
    )


async def handle_multi_factor_score(
    *,
    kw: dict[str, Any],
    code: str | None,
    ok: Callable[..., dict],
    fail: Callable[..., dict],
    quant_manager_call: Callable[..., Any],
) -> dict:
    if not code:
        return fail("需要提供股票代码（code）")

    weights = kw.get(
        "weights",
        {
            "momentum": 0.3,
            "value": 0.3,
            "quality": 0.2,
            "volatility": 0.1,
            "liquidity": 0.1,
        },
    )

    result = await quant_manager_call(
        action="calculate_factors",
        code=code,
        kwargs={"factors": list(weights.keys())},
    )
    if not result.get("success"):
        return result

    factors_data = result["data"]["factors"]
    total_score = 0.0
    factor_scores = {}

    for factor_name, weight in weights.items():
        if factor_name in factors_data:
            score = float(factors_data[factor_name].get("score", 0.0))
            weighted_score = score * float(weight)
            total_score += weighted_score
            factor_scores[factor_name] = {
                "score": score,
                "weight": float(weight),
                "weighted_score": float(weighted_score),
            }

    if total_score > 0.7:
        rating, recommendation = "A", "strong_buy"
    elif total_score > 0.5:
        rating, recommendation = "B", "buy"
    elif total_score > 0.3:
        rating, recommendation = "C", "hold"
    else:
        rating, recommendation = "D", "sell"

    # P2-4.3.6 fix(诊断报告 §4.3.6):single_factor_dominance 检测
    # 历史问题:weights volatility=0.51/momentum=0.18/value=0.15/.../growth=0.06,
    # 单因子贡献 84% — emit warning 提示 AI 该评分不可靠
    warnings: list[str] = []
    if total_score > 0:
        max_contribution = 0.0
        max_factor = None
        for factor_name, item in factor_scores.items():
            ws = abs(float(item.get("weighted_score") or 0.0))
            if ws > max_contribution:
                max_contribution = ws
                max_factor = factor_name
        if total_score > 1e-6:
            ratio = max_contribution / abs(total_score)
            if ratio > 0.5:
                warnings.append(
                    f"single_factor_dominance>50%: {max_factor} contributes "
                    f"{ratio*100:.1f}% of total_score. 建议 z-score 归一化或调整 weights."
                )

    return ok(
        {
            "code": code,
            "total_score": float(total_score),
            "rating": rating,
            "recommendation": recommendation,
            "factor_scores": factor_scores,
            "warnings": warnings,
            "weights": dict(weights),
        },
        source_chain=["quant_manager.calculate_factors"],
    )


async def handle_alternative_factors(
    *,
    kw: dict[str, Any],
    code: str | None,
    db: Any,
    ok: Callable[..., dict],
    fail: Callable[..., dict],
) -> dict:
    codes = _as_code_list(kw.get("codes"))
    if not codes and code:
        codes = [code]
    if not codes:
        return fail("需要提供 code 或 codes")

    lookback_days = int(kw.get("lookback_days", 30) or 30)
    limit = int(kw.get("limit", 20) or 20)
    lookback_days = max(7, min(180, lookback_days))
    limit = max(5, min(100, limit))

    result_rows = []
    source_chain = []
    for one_code in codes:
        factors, one_source_chain = await _compute_alternative_factors_for_code(
            db=db,
            code=one_code,
            lookback_days=lookback_days,
            limit=limit,
        )
        source_chain.extend(one_source_chain)
        result_rows.append({"code": one_code, "factors": factors})

    return ok(
        {
            "codes": codes,
            "count": len(result_rows),
            "data_window": {"lookback_days": lookback_days, "limit_per_source": limit},
            "rows": result_rows,
        },
        source_chain=source_chain or ["quant_manager.alternative_factors"],
    )


async def handle_batch_compute_factors(
    *,
    kw: dict[str, Any],
    ok: Callable[..., dict],
    fail: Callable[..., dict],
    get_db_fn: Callable[[], Any] = get_db,
) -> dict:
    codes = kw.get("codes", [])
    if not isinstance(codes, list) or not codes:
        return fail("codes (list of stock codes) is required")
    factors = kw.get("factors", ["momentum", "value", "quality", "growth"])
    persist = bool(kw.get("persist", kw.get("store", True)))
    compute_ic = bool(kw.get("compute_ic", True))
    period = int(kw.get("period", 20) or 20)

    unknown = [factor for factor in factors if factor not in _BATCH_FACTORS_SUPPORTED]
    if unknown:
        return fail(f"Unsupported factors for batch: {unknown}. Supported: {sorted(_BATCH_FACTORS_SUPPORTED)}")

    db = get_db_fn()
    results = {}
    errors = []
    persist_buffer = []
    today = datetime.now().date()

    for stock_code in codes[:int(kw.get("max_codes", 1000) or 1000)]:
        try:
            klines, _source_chain = await _load_klines_with_fallback(db=db, code=stock_code, limit=252)
            if not klines:
                errors.append({"code": stock_code, "error": "no kline data"})
                continue

            ordered_klines = _sort_klines_ascending(klines)
            closes = [item.get("close") for item in ordered_klines if isinstance(item, dict) and item.get("close") is not None]
            if len(closes) < 20:
                errors.append({"code": stock_code, "error": "insufficient data"})
                continue

            financials = await db.get_financials(stock_code, limit=8)
            latest_fin = _select_financial_snapshot(financials)
            valuation_snapshot = await _load_valuation_snapshot(db, stock_code)
            factor_values = _compute_scalar_factor_bundle(
                closes,
                financial_snapshot=latest_fin,
                valuation_snapshot=valuation_snapshot,
                factors=factors,
            )

            results[stock_code] = factor_values
            if persist and factor_values:
                persist_buffer.append(
                    {
                        "stock_code": stock_code,
                        "factor_date": today,
                        "values": factor_values,
                    }
                )
        except Exception as exc:
            errors.append({"code": stock_code, "error": str(exc)})

    if persist and persist_buffer:
        try:
            if hasattr(db, "save_factor_values_batch"):
                await db.save_factor_values_batch(persist_buffer)
            else:
                for item in persist_buffer:
                    await db.save_factor_values(item["stock_code"], item["factor_date"], item["values"])
        except Exception as exc:
            logger.warning("batch_compute_factors persist batch failed, retrying row-wise: %s", exc)
            for item in persist_buffer:
                try:
                    await db.save_factor_values(item["stock_code"], item["factor_date"], item["values"])
                except Exception as row_exc:
                    errors.append({"code": item["stock_code"], "error": f"persist failed: {row_exc}"})

    ic_results = {}
    if compute_ic and len(results) >= 10:
        # PR-F5: 多周期 IC 计算（5/10/20/60 日）
        # 正确的 IC 计算：用 T-horizon 天的因子值 vs T-horizon 到 T 的实际收益率
        # 避免 look-ahead bias：因子值必须在收益率观测期之前计算
        ic_horizons = [int(h) for h in (kw.get("ic_horizons") or [5, 10, 20, 60])]
        neutralize = _batch_ic_neutralize_enabled()
        for horizon in ic_horizons:
            horizon_ic = {}
            for factor_name in factors:
                factor_vals = []
                forward_rets = []
                # P0-2: 与 factor_vals 对齐的 per-stock 风格暴露（仅中性化开启时收集）
                style_industries: list[Any] = []
                style_market_caps: list[Any] = []
                style_betas: list[Any] = []
                for stock_code, factor_values_map in results.items():
                    if factor_name not in factor_values_map:
                        continue
                    try:
                        klines = await db.get_klines(stock_code, limit=horizon + 252)
                        if not klines or len(klines) < horizon + 60:
                            continue
                        ordered_klines = _sort_klines_ascending(klines)
                        closes = [float(item.get("close", 0) or 0) for item in ordered_klines]
                        if len(closes) < horizon + 60:
                            continue

                        # 正确逻辑：
                        # 1. 取 T-horizon 时刻的 K 线数据计算因子值（滞后因子）
                        # 2. forward_return = closes[T] / closes[T-horizon] - 1
                        lagged_idx = len(closes) - horizon - 1  # T-horizon 的位置
                        if lagged_idx < 60:
                            continue
                        lagged_closes = closes[:lagged_idx + 1]
                        if len(lagged_closes) < 30:
                            continue

                        # 用滞后数据重新计算因子值
                        lagged_date = _parse_date_value(ordered_klines[lagged_idx].get("date"))
                        financials = await db.get_financials(stock_code, limit=8)
                        lagged_fin = _select_financial_snapshot(financials, as_of_date=lagged_date)
                        lagged_valuation = await _load_valuation_snapshot(db, stock_code, as_of_date=lagged_date)
                        lagged_bundle = _compute_scalar_factor_bundle(
                            lagged_closes,
                            financial_snapshot=lagged_fin,
                            valuation_snapshot=lagged_valuation,
                            factors=[factor_name],
                        )
                        if factor_name not in lagged_bundle:
                            continue
                        lagged_fv = float(lagged_bundle[factor_name])

                        # 基本面因子需要有 financials 数据
                        if factor_name in {"value", "quality", "growth"} and not lagged_fin:
                            continue

                        # forward return = 从 T-horizon 到 T 的实际收益率
                        close_at_signal = closes[lagged_idx]
                        close_at_end = closes[-1]
                        if close_at_signal > 0 and close_at_end > 0:
                            factor_vals.append(lagged_fv)
                            forward_rets.append((close_at_end - close_at_signal) / close_at_signal)
                            # P0-2: 仅在中性化开启时取风格暴露，保持 OFF 路径零额外开销
                            if neutralize:
                                try:
                                    stock_info = await db.get_stock_info(stock_code)
                                except Exception:
                                    stock_info = None
                                styles = _extract_style_exposures(stock_info, lagged_fin)
                                style_industries.append(styles.get("industry"))
                                style_market_caps.append(styles.get("market_cap"))
                                style_betas.append(styles.get("beta"))
                    except Exception:
                        continue

                if len(factor_vals) >= 10:
                    if neutralize:
                        # P0-2: 行业/市值/beta 截面残差中性化（dual-IC）。
                        # 风格暴露与 factor_vals 一一对齐；缺失时 calculate_ic_dual 内部自动降级回原始值。
                        from ...services.factor_analysis import FactorAnalyzer

                        dual = FactorAnalyzer.calculate_ic_dual(
                            factor_vals,
                            forward_rets,
                            industry=style_industries or None,
                            market_cap=style_market_caps or None,
                            beta=style_betas or None,
                            enable_neutralization=True,
                        )
                        ic_val = float(dual.get("normal_ic", 0.0))
                        rank_ic = float(dual.get("rank_ic", 0.0))
                    else:
                        from ...services.factor_calculator.analysis import AnalysisFactorsMixin

                        ic_data = AnalysisFactorsMixin.calculate_factor_ic(factor_vals, forward_rets)
                        ic_val = ic_data.get("ic", 0.0)
                        rank_ic = ic_data.get("rank_ic", 0.0)
                    horizon_ic[factor_name] = {"ic": ic_val, "rank_ic": rank_ic, "sample_size": len(factor_vals)}
                    if persist:
                        await db.save_factor_ic(factor_name, str(horizon), today, ic_val, rank_ic, len(factor_vals))

            if horizon_ic:
                ic_results[f"horizon_{horizon}"] = horizon_ic
        # 兼容旧格式：取 period=20 的结果作为顶层 ic
        if f"horizon_{period}" in ic_results:
            ic_results.update(ic_results[f"horizon_{period}"])

    return ok(
        {
            "computed_count": len(results),
            "error_count": len(errors),
            "errors": errors[:10],
            "factors": factors,
            "ic": ic_results,
            "persisted": persist,
            "store": persist,
        },
        source_chain=(
            ["db.get_klines", "db.save_factor_values", "db.save_factor_ic"]
            if persist
            else ["db.get_klines"]
        ),
    )


__all__ = [
    "handle_alternative_factors",
    "handle_backtest_factor",
    "handle_batch_compute_factors",
    "handle_calculate_factors",
    "handle_factor_ic",
    "handle_multi_factor_score",
]
