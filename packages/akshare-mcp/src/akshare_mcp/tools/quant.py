"""Quant factor tools — thin registration layer.

Heavy logic lives in sub-modules:
  quant_definitions  — factor registry, constants, helpers
  quant_engine       — data helpers, prefetch, factor calculation
  quant_analysis     — IC, grouped backtest, OOS, robustness
"""

import json
import re
from typing import Any, Dict, Optional

import numpy as np

from ..services.conditional_returns import (
    calculate_conditional_returns,
    validate_conditions as _validate_conditional_fields,
)
from ..services.data_pipeline import compute_signal_hit_rate, normalize_klines
from ..storage import get_db
from ..utils import (
    enrich_response_meta,
    fail,
    ok,
    ok_degraded_empty,
    parse_date_input,
    resolve_existing_security_code_async,
    validate_int_range,
)

from .quant_definitions import (
    DEFAULT_FACTOR_LOOKBACK,
    MARKET_CAP_KEYS,
    PROFIT_GROWTH_KEYS,
    REVENUE_GROWTH_KEYS,
    SUPPORTED_FACTORS,
    _normalize_factor_name,
)
from .quant_engine import (
    _calculate_factor_value,
    _latest_financial_row,
)
from .quant_analysis import (
    run_factor_group_backtest,
    run_factor_ic_analysis,
    run_factor_oos_validation,
    run_factor_robustness_check,
)


# ---------------------------------------------------------------------------
# Local helpers (not worth a sub-module)
# ---------------------------------------------------------------------------

def _factor_library_payload(category: str = "all") -> dict:
    category_key = str(category or "all").strip().lower()
    categories = sorted({meta["category"] for meta in SUPPORTED_FACTORS.values()})
    if category_key not in {"all", *categories}:
        raise ValueError(f"Unsupported category: {category_key}. Supported: all, {', '.join(categories)}")
    # P2-4.2.1 fix: 暴露完整的 alias-canonical 映射(诊断报告 §4.2.1)
    # AI Agent 一次调用即可看到所有别名,避免 'momentum_20d' 在不同 action 接受/拒绝
    from .factor_naming import list_factor_aliases

    alias_map = list_factor_aliases()

    factors = [
        {
            "name": name,
            "category": meta["category"],
            "description": meta["description"],
            "requires_financials": meta["requires_financials"],
            "default_period": int(meta.get("default_period", 20)),
            "data_dependency": meta.get(
                "data_dependency",
                ["kline", "financials"] if meta.get("requires_financials") else ["kline"],
            ),
            "sub_factors": meta.get("sub_factors", []),
            "aliases": list(set((meta.get("aliases", []) or []) + alias_map.get(name, []))),
            "status": "supported",
            # P2-4.2.2 fix(诊断报告 §4.2.2):标 availability_hint
            # list 与 calculate 数据脱节问题 — 提示具体股票上是否可用需要传 code 参数
            "availability_hint": (
                "always_available" if not meta.get("requires_financials")
                else "requires_financials_in_db"
            ),
        }
        for name, meta in SUPPORTED_FACTORS.items()
        if category_key in ("all", meta["category"])
    ]
    return {
        "factors": factors,
        "count": len(factors),
        "categories": categories,
        "supported_factors": sorted(SUPPORTED_FACTORS.keys()),
        "total_categories": len(categories),
        # P2-4.2.1 fix: 顶层暴露完整 alias map,AI 可一次性看到所有别名
        "alias_canonical_map": alias_map,
        "alias_count": sum(len(aliases) for aliases in alias_map.values()),
        "naming_note": (
            "All 4 quant_manager actions (factor_ic / batch_compute_factors / "
            "calculate_factor / list_factors) accept any name in 'aliases' or 'name'. "
            "Resolver: factor_naming.resolve_factor_name()"
        ),
        "note": f"Factor library includes {len(SUPPORTED_FACTORS)} factors.",
    }


def _build_similar_pattern_report(klines: list[dict], window_days: int, top_n: int, forward_days: list[int]) -> dict:
    ordered = normalize_klines(klines)
    closes = np.array([float(k.get("close", 0) or 0) for k in ordered], dtype=np.float64)
    max_forward = max(forward_days) if forward_days else 10
    if len(closes) < window_days * 3 + max_forward:
        return {"matches": [], "aggregate_prediction": {}, "pattern_window": window_days}
    latest_window = closes[-window_days:]
    latest_returns = np.diff(latest_window) / np.maximum(latest_window[:-1], 1e-12)
    latest_start = len(closes) - window_days
    matches = []
    for start in range(20, latest_start - max_forward):
        end = start + window_days
        if end >= latest_start:
            break
        hist_window = closes[start:end]
        hist_returns = np.diff(hist_window) / np.maximum(hist_window[:-1], 1e-12)
        if np.std(hist_returns) < 1e-12 or np.std(latest_returns) < 1e-12:
            similarity = 0.0
        else:
            similarity = float(np.corrcoef(latest_returns, hist_returns)[0, 1])
        if not np.isfinite(similarity):
            continue
        end_idx = end - 1
        future = {}
        for fd in forward_days:
            future_idx = end_idx + int(fd)
            if future_idx >= len(closes) or closes[end_idx] <= 0:
                continue
            future[f"{fd}d"] = round(float((closes[future_idx] - closes[end_idx]) / closes[end_idx]), 4)
        regime_ret = (closes[end_idx] - closes[start]) / closes[start] if closes[start] > 0 else 0.0
        regime = "bullish" if regime_ret >= 0.05 else ("bearish" if regime_ret <= -0.05 else "neutral")
        matches.append({
            "pattern_end_date": ordered[end_idx].get("date") or ordered[end_idx].get("time"),
            "similarity": round(similarity, 4),
            "market_regime": regime,
            "forward_returns": future,
        })
    matches = sorted(matches, key=lambda item: item["similarity"], reverse=True)[:top_n]
    aggregate = {}
    for fd in forward_days:
        values = [item["forward_returns"].get(f"{fd}d") for item in matches if f"{fd}d" in item["forward_returns"]]
        values = [float(v) for v in values if v is not None]
        aggregate[f"{fd}d"] = {
            "samples": len(values),
            "avg_return": round(float(np.mean(values)), 4) if values else None,
            "hit_rate": round(float(np.mean(np.array(values) > 0)), 4) if values else None,
        }
    return {"matches": matches, "aggregate_prediction": aggregate, "pattern_window": window_days}


async def _load_factor_klines(
    db,
    code: str,
    *,
    start_date: Optional[str],
    end_date: Optional[str],
    limit: int,
):
    """Support both new and legacy db.get_klines signatures."""
    try:
        return await db.get_klines(code, start_date=start_date, end_date=end_date, limit=limit)
    except TypeError:
        return await db.get_klines(code, limit=limit)


def _parse_lookback_days(value: Any, *, field_name: str) -> tuple[Optional[int], Optional[str]]:
    """Accept integer lookbacks and common day strings like "60d"."""
    raw = value
    if isinstance(raw, str):
        text = raw.strip().lower()
        match = re.fullmatch(r"(\d+)\s*(?:d|day|days)?", text)
        if not match:
            return None, f"{field_name} must be a lookback integer, e.g. 20 or '20d'"
        raw = match.group(1)
    return validate_int_range(raw, field_name=field_name, minimum=1)


def _coerce_bool_flag(value: Any, *, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return default


# ---------------------------------------------------------------------------
# MCP tool registration
# ---------------------------------------------------------------------------

def register(mcp):
    @mcp.tool()
    def get_factor_library(category: str = "all"):
        """获取可用因子库与分类信息。

        Args:
            category: 因子分类，默认 ``all`` 返回全部分类。

        Returns:
            dict: 标准 ``ok(...)`` 响应，包含因子分类、说明和支持列表。
        """
        try:
            return ok(_factor_library_payload(category))
        except Exception as e:
            return fail(str(e))

    @mcp.tool()
    def list_factors(category: str = "all"):
        """列出指定分类下的支持因子。

        Args:
            category: 因子分类，默认 ``all``。

        Returns:
            dict: 标准 ``ok(...)`` 响应，包含因子列表和来源标记。
        """
        try:
            payload = _factor_library_payload(category)
            payload["source"] = "SUPPORTED_FACTORS"
            return ok(payload)
        except Exception as e:
            return fail(str(e))

    @mcp.tool()
    async def get_factor_catalog(
        category: str = "all",
        factor_name: Optional[str] = None,
        include_dsl_metadata: bool = True,
        only_with_ic: bool = False,
        limit: int = 0,
    ):
        """Return the unified read-only factor catalog."""
        try:
            from ..services.factor_catalog import build_factor_catalog, get_dsl_summary

            include_dsl = _coerce_bool_flag(include_dsl_metadata, default=True)
            only_ic = _coerce_bool_flag(only_with_ic, default=False)
            catalog = await build_factor_catalog(include_dsl_metadata=include_dsl)

            category_key = str(category or "all").strip().lower()
            if category_key != "all":
                catalog = {
                    name: meta
                    for name, meta in catalog.items()
                    if str(meta.get("category") or "").lower() == category_key
                }

            normalized_factor = None
            if factor_name:
                normalized_factor = _normalize_factor_name(str(factor_name))
                if normalized_factor not in catalog:
                    return fail(f"Unsupported or filtered factor: {factor_name}")
                catalog = {normalized_factor: catalog[normalized_factor]}

            if only_ic:
                catalog = {
                    name: meta
                    for name, meta in catalog.items()
                    if bool(meta.get("ic_history_available"))
                }

            sorted_items = sorted(catalog.items(), key=lambda item: item[0])
            resolved_limit = max(0, int(limit or 0))
            if resolved_limit:
                sorted_items = sorted_items[:resolved_limit]
            catalog = dict(sorted_items)
            categories = sorted({str(meta.get("category", "unknown")) for meta in catalog.values()})

            return ok({
                "catalog": catalog,
                "count": len(catalog),
                "category": category_key,
                "factor_name": normalized_factor,
                "only_with_ic": only_ic,
                "include_dsl_metadata": include_dsl,
                "dsl": get_dsl_summary() if include_dsl else None,
                "stats": {
                    "ic_history_available_count": sum(
                        1 for meta in catalog.values() if bool(meta.get("ic_history_available"))
                    ),
                    "category_count": len(categories),
                    "categories": categories,
                },
                "source_chain": [
                    "tools.quant.get_factor_catalog",
                    "services.factor_catalog.build_factor_catalog",
                    "tools.quant_definitions.SUPPORTED_FACTORS",
                    "services.factor_candidate_compiler.DSL_WHITELIST",
                    "storage.factor_ic_history",
                ],
            })
        except Exception as e:
            return fail(str(e))

    @mcp.tool()
    async def calculate_factor(
        code: str,
        factor: str,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
    ):
        """计算单只股票的单个因子值。

        Args:
            code: 股票代码。
            factor: 因子名称，支持 ``SUPPORTED_FACTORS`` 中的标准名称或别名。

        Returns:
            dict: 标准 ``ok(...)`` 响应，包含因子值、样本量和是否依赖财务数据。
        """
        try:
            code, _, error = await resolve_existing_security_code_async(code=code)
            if error:
                return fail(error)
            normalized_start_date = parse_date_input(start_date).isoformat() if start_date else None
            normalized_end_date = parse_date_input(end_date).isoformat() if end_date else None
            if start_date and normalized_start_date is None:
                return fail(f"Invalid start_date: {start_date}")
            if end_date and normalized_end_date is None:
                return fail(f"Invalid end_date: {end_date}")

            factor_name = _normalize_factor_name(factor)
            if factor_name not in SUPPORTED_FACTORS:
                return fail(f"Unsupported factor: {factor_name}. Supported: {', '.join(sorted(SUPPORTED_FACTORS.keys()))}")

            db = get_db()
            klines = await _load_factor_klines(
                db,
                code,
                start_date=normalized_start_date,
                end_date=normalized_end_date,
                limit=100,
            )
            if not klines:
                return fail("No kline data")

            ordered_klines = normalize_klines(klines)
            closes = [k["close"] for k in ordered_klines if isinstance(k, dict) and k.get("close") is not None]
            if len(closes) < 2:
                return fail("Not enough close data")

            financial = None
            if SUPPORTED_FACTORS[factor_name]["requires_financials"]:
                financial = _latest_financial_row(await db.get_financials(code, limit=1))
                if not financial:
                    return fail(f"No financial data for factor: {factor_name}")

            stock_info = None
            try:
                stock_info = await db.get_stock_info(code)
            except Exception:
                stock_info = None

            value = _calculate_factor_value(
                factor_name,
                closes,
                financial=financial,
                stock_info=stock_info,
                period=DEFAULT_FACTOR_LOOKBACK,
            )
            if value is None or np.isnan(value):
                if factor_name == "growth":
                    return fail(
                        "Failed to calculate factor: growth (missing growth fields, expected one of "
                        f"{', '.join(REVENUE_GROWTH_KEYS)} or {', '.join(PROFIT_GROWTH_KEYS)})"
                    )
                if factor_name == "size":
                    return fail(
                        "Failed to calculate factor: size (missing market cap in stock_info/financials, expected one of "
                        f"{', '.join(MARKET_CAP_KEYS)})"
                    )
                if factor_name == "momentum":
                    return fail(
                        f"Failed to calculate factor: momentum (need >= 2 close prices, got {len(closes)})"
                    )
                if factor_name == "trend":
                    return fail(
                        f"Failed to calculate factor: trend (need >= 3 close prices, got {len(closes)})"
                    )
                if factor_name == "reversal":
                    return fail(
                        f"Failed to calculate factor: reversal (need >= 2 close prices, got {len(closes)})"
                    )
                if factor_name == "volatility":
                    return fail(
                        f"Failed to calculate factor: volatility (need >= 4 close prices with valid returns, got {len(closes)})"
                    )
                if factor_name == "value":
                    return fail(
                        "Failed to calculate factor: value (need positive pe_ratio, pb_ratio, or ps_ratio in financials)"
                    )
                if factor_name == "quality":
                    return fail(
                        "Failed to calculate factor: quality (need roe/debt_ratio in financials)"
                    )
                return fail(f"Failed to calculate factor: {factor_name}")

            return ok(
                {
                    "code": code,
                    "factor": factor_name,
                    "value": float(value),
                    "requires_financials": SUPPORTED_FACTORS[factor_name]["requires_financials"],
                    "sample_size": len(closes),
                    "start_date": normalized_start_date or start_date,
                    "end_date": normalized_end_date or end_date,
                }
            )
        except Exception as e:
            return fail(str(e))

    @mcp.tool()
    async def calculate_factor_ic(
        codes: list,
        factor: str,
        period: int = 20,
        enable_neutralization: bool = True,
        bootstrap_n: int = 1000,
        bootstrap_confidence: float = 0.95,
        include_perf_breakdown: bool = True,
    ):
        """Calculate dual information coefficient (Normal IC + Rank IC) by cross-section."""
        try:
            return await run_factor_ic_analysis(
                codes=codes,
                factor=factor,
                period=period,
                enable_neutralization=enable_neutralization,
                bootstrap_n=bootstrap_n,
                bootstrap_confidence=bootstrap_confidence,
                include_perf_breakdown=include_perf_breakdown,
            )
        except Exception as e:
            return fail(str(e))

    @mcp.tool()
    async def backtest_factor(
        codes: list,
        factor: str,
        groups: int = 5,
        holding_days: int = 20,
        commission: float = 0.0003,
        slippage: float = 0.0,
        slippage_model: str = "",
        tradability_filter: bool = False,
        is_st: bool = False,
        rebalance_step: int = 0,
        max_periods: int = 0,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        include_perf_breakdown: bool = True,
    ):
        """Run grouped factor backtest on a stock universe."""
        try:
            return await run_factor_group_backtest(
                codes=codes,
                factor=factor,
                groups=groups,
                holding_days=holding_days,
                factor_lookback=DEFAULT_FACTOR_LOOKBACK,
                commission=commission,
                slippage=slippage,
                slippage_model=slippage_model,
                tradability_filter=tradability_filter,
                is_st=is_st,
                rebalance_step=rebalance_step,
                max_periods=max_periods,
                start_date=start_date,
                end_date=end_date,
                include_perf_breakdown=include_perf_breakdown,
            )
        except Exception as e:
            return fail(str(e))

    @mcp.tool()
    async def validate_factor_oos(
        codes: list,
        factor: str,
        factor_lookback: int = 20,
        forward_period: int = 20,
        panel_periods: int = 180,
        wf_train_window: int = 60,
        wf_test_window: int = 20,
        wf_step: int = 0,
        kfold_n_folds: int = 5,
        kfold_purge_gap: int = 5,
        bootstrap_n: int = 1000,
        bootstrap_confidence: float = 0.95,
        validation_parallel: bool = True,
        max_workers: int = 0,
        bootstrap_mode: str = "",
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        include_perf_breakdown: bool = True,
    ):
        """P0-A: Unified OOS validation (Walk-Forward + Purged KFold + Bootstrap CI)."""
        try:
            result = await run_factor_oos_validation(
                codes=codes,
                factor=factor,
                factor_lookback=factor_lookback,
                forward_period=forward_period,
                panel_periods=panel_periods,
                wf_train_window=wf_train_window,
                wf_test_window=wf_test_window,
                wf_step=(None if int(wf_step or 0) == 0 else int(wf_step)),
                kfold_n_folds=kfold_n_folds,
                kfold_purge_gap=kfold_purge_gap,
                bootstrap_n=bootstrap_n,
                bootstrap_confidence=bootstrap_confidence,
                validation_parallel=validation_parallel,
                max_workers=(None if int(max_workers or 0) <= 0 else int(max_workers)),
                bootstrap_mode=bootstrap_mode,
                start_date=start_date,
                end_date=end_date,
                include_perf_breakdown=include_perf_breakdown,
            )
            if not result.get("success") and "Not enough valid codes for panel build" in str(result.get("error") or ""):
                return ok_degraded_empty(
                    {
                        "factor": _normalize_factor_name(factor),
                        "validation_report": None,
                        "panel_info": {
                            "n_periods": 0,
                            "n_stocks": 0,
                            "codes": list(codes or []),
                            "factor_lookback": int(factor_lookback),
                            "forward_period": int(forward_period),
                        },
                        "stats": {"reason": str(result.get("error") or "")},
                        "date_range": {"start_date": start_date, "end_date": end_date},
                        "warnings": ["insufficient_panel_sample"],
                        "insufficient_sample": True,
                    },
                    fallback_reason="insufficient_panel_sample",
                    source_chain=["quant.prefetch_market_data", "validation.FactorValidationPipeline.run"],
                    quality_flags=["empty", "insufficient_sample"],
                )
            return result
        except Exception as e:
            return fail(str(e))

    @mcp.tool()
    async def get_conditional_returns(
        code: str,
        conditions: Any = None,
        forward_days: list = None,
        logic: str = "AND",
        lookback_days: int = 250,
    ):
        """按历史条件统计未来收益分布，向 AI 提供条件概率证据。

        Args:
            code (str): 6位股票代码，如 "600519"
            conditions (list[dict]): 条件列表，每项格式 {"field": str, "op": str, "value": number}
                - field: 指标名，如 "rsi_14", "volume_ratio", "pct_change", "close", "ma_5"
                - op: 比较运算符，如 "<", ">", "<=", ">=", "==", "!="
                - value: 阈值数值
                示例: [{"field": "rsi_14", "op": "<", "value": 30}, {"field": "volume_ratio", "op": ">", "value": 2.0}]
            forward_days (list[int], optional): 向前看天数列表，默认 [5, 10, 20]
            logic (str, optional): 多条件逻辑 "AND" 或 "OR"，默认 "AND"
            lookback_days (int, optional): 回溯K线天数，默认 250，最小 30
        """
        try:
            code, _, error = await resolve_existing_security_code_async(code=code)
            if error:
                return fail(error)
            if conditions in (None, "", [], {}):
                return fail("conditions is required")
            if isinstance(conditions, str):
                try:
                    conditions = json.loads(conditions)
                except Exception:
                    return fail("conditions 必须是有效的 JSON")
            if not isinstance(conditions, (list, dict)):
                return fail("conditions 必须是对象或对象数组")
            if str(logic or "AND").strip().upper() not in {"AND", "OR"}:
                return fail("logic 必须为 AND 或 OR")
            # FIX-6: 校验 field/op 合法性，未识别字段/非法运算符显性报错而非静默 0 匹配
            valid, validation_error = _validate_conditional_fields(conditions, logic)
            if not valid:
                return fail(validation_error)
            lookback = max(30, int(lookback_days))
            db = get_db()
            klines = await db.get_klines(code, limit=lookback)
            if not klines or len(klines) < 30:
                return fail(f"K 线数据不足（需要至少 30 条，实际 {len(klines) if klines else 0} 条）")
            report = calculate_conditional_returns(
                klines=klines,
                conditions=conditions,
                forward_days=forward_days or [5, 10, 20],
                logic=logic,
            )
            return ok({
                "code": code,
                "kline_count": len(klines),
                "lookback_days": lookback,
                **report,
            })
        except Exception as e:
            return fail(str(e))

    @mcp.tool()
    async def find_similar_patterns(
        code: str,
        window_days: int = 20,
        top_n: int = 10,
        forward_days: list = None,
        lookback_days: int = 360,
    ):
        """基于历史 K 线窗口搜索相似形态并统计后续收益。

        Args:
            code: 股票代码。
            window_days: 目标形态窗口长度。
            top_n: 返回最相似样本数量。
            forward_days: 统计未来收益的天数列表，默认 ``[5, 10, 20]``。
            lookback_days: 回看 K 线样本长度。

        Returns:
            dict: 标准 ``ok(...)`` 响应，包含相似样本、聚合收益和窗口信息。
        """
        try:
            code, _, error = await resolve_existing_security_code_async(code=code)
            if error:
                return fail(error)
            forward = [int(day) for day in (forward_days or [5, 10, 20])]
            db = get_db()
            klines = await db.get_klines(code, limit=max(int(lookback_days), int(window_days) * 4))
            if not klines or len(klines) < max(90, int(window_days) * 3):
                return fail("K 线数据不足，无法进行历史形态匹配")
            report = _build_similar_pattern_report(klines, int(window_days), int(top_n), forward)
            return ok({
                "code": code,
                "lookback_days": max(int(lookback_days), int(window_days) * 4),
                **report,
            })
        except Exception as e:
            return fail(str(e))

    @mcp.tool()
    async def get_signal_hit_rate(
        code: str,
        signal: str = "rsi_oversold",
        forward_days: list = None,
        lookback_days: int = 250,
        signal_params: Optional[Dict[str, Any]] = None,
    ):
        """统计历史信号命中率与未来收益表现。

        Args:
            code: 股票代码。
            signal: 信号名称，默认 ``rsi_oversold``。
            forward_days: 未来收益统计窗口列表，默认 ``[5, 10, 20]``。
            lookback_days: 回看样本长度。
            signal_params: 信号参数字典。

        Returns:
            dict: 标准 ``ok(...)`` 响应，包含命中次数、收益分布和窗口配置。
        """
        try:
            code, _, error = await resolve_existing_security_code_async(code=code)
            if error:
                return fail(error)
            forward = [int(day) for day in (forward_days or [5, 10, 20])]
            db = get_db()
            klines = await db.get_klines(code, limit=max(60, int(lookback_days)))
            if not klines or len(klines) < 30:
                return fail("K 线数据不足，无法统计命中率")
            report = compute_signal_hit_rate(klines, signal=signal, forward_days=forward, signal_params=signal_params)
            payload = {
                "code": code,
                "lookback_days": max(60, int(lookback_days)),
                **report,
            }
            result = ok(payload)
            if payload.get("reliability_warning") or int(payload.get("sample_count") or 0) < 10:
                result["fallback_reason"] = payload.get("reliability_warning") or "signal_sample_below_10"
                return enrich_response_meta(
                    result,
                    source_chain=["quant.get_signal_hit_rate", "db.get_klines", "services.data_pipeline"],
                    quality_flags=["partial", "insufficient_sample", "degraded"],
                    degraded=True,
                    fallback_used=False,
                )
            return result
        except Exception as e:
            return fail(str(e))

    @mcp.tool()
    async def factor_robustness_check(
        codes: list,
        factor: str,
        windows: list = None,
        param_variations: list = None,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        include_perf_breakdown: bool = True,
    ):
        """P2-2: Factor robustness check — multi-window IC stability, parameter sensitivity, sub-sample consistency."""
        try:
            if windows is not None:
                if not isinstance(windows, list):
                    return fail("windows 必须是整数列表")
                parsed_windows = []
                for item in windows:
                    value, value_error = _parse_lookback_days(item, field_name="window")
                    if value_error:
                        return fail(value_error)
                    parsed_windows.append(value)
                windows = parsed_windows
            if param_variations is not None:
                if not isinstance(param_variations, list):
                    return fail("param_variations must be a list of lookback integers")
                parsed_variations = []
                for item in param_variations:
                    value, value_error = _parse_lookback_days(item, field_name="param_variation")
                    if value_error:
                        return fail(
                            "param_variations must be lookback integers, e.g. [10, 20, 40] "
                            f"or ['10d', '20d']; got {item!r}"
                        )
                    parsed_variations.append(value)
                param_variations = parsed_variations
            return await run_factor_robustness_check(
                codes=codes,
                factor=factor,
                windows=windows,
                param_variations=param_variations,
                start_date=start_date,
                end_date=end_date,
                include_perf_breakdown=include_perf_breakdown,
            )
        except Exception as e:
            return fail(str(e))
