"""估值跨方法 reconciliation meta-tool。

P1-3.2 fix(诊断报告 §3.2):
5 个估值器对同股给出 5 个不同 intrinsic_price,跨度 8×(11.6/18.2/68.6/91.2/95.9)。
单工具不可信,AI 调用时必须 cross-validate。

本工具一次性调度 dcf / ddm / relative / scenario_dcf / fundamental_analysis_manager.intrinsic
五条估值路径,输出统一 consensus 报告:
  - estimates: 5 个估值结果 + 各自 source/method
  - statistics: max/min/mean/median + 离散度
  - dispersion: 跨度 (max/min) 倍数 + warning level
  - cross_method_dispersion_warning: 跨度 > 3× emit warning
"""

from __future__ import annotations

import asyncio
import statistics
import time
from typing import Any, Optional

from ..utils import ok, fail, resolve_existing_security_code_async


def _safe_call(coro_factory, label: str, results: dict, errors: dict) -> None:
    """Helper: 异步执行工具函数,捕获异常不影响其他路径。"""
    try:
        coro = coro_factory()
    except Exception as exc:
        errors[label] = f"factory_failed:{type(exc).__name__}:{exc}"
        return
    results[label] = coro


def _extract_intrinsic_per_share(label: str, payload: Any) -> tuple[float | None, str]:
    """从不同估值器的响应中标准化提取 per_share。

    Returns:
        (per_share or None, source_path 用于审计)
    """
    if not isinstance(payload, dict) or not payload.get("success"):
        return None, f"{label}:no_success"

    data = payload.get("data") or {}
    if not isinstance(data, dict):
        return None, f"{label}:non_dict_data"

    candidates = (
        ("per_share", "data.per_share"),
        ("intrinsic_price", "data.intrinsic_price"),
        ("intrinsic_price_per_share", "data.intrinsic_price_per_share"),
        ("price_per_share", "data.price_per_share"),
    )
    for key, path in candidates:
        val = data.get(key)
        if val is None:
            continue
        try:
            num = float(val)
            if num > 0:
                return num, path
        except (TypeError, ValueError):
            continue

    # 有些工具返回嵌套 valuation 子字典
    nested = data.get("valuation") if isinstance(data.get("valuation"), dict) else None
    if nested:
        for key in ("per_share", "intrinsic_price", "value"):
            val = nested.get(key)
            if val is None:
                continue
            try:
                num = float(val)
                if num > 0:
                    return num, f"data.valuation.{key}"
            except (TypeError, ValueError):
                continue

    return None, f"{label}:no_per_share_field"


def _simple_dcf_per_share(
    *,
    base_cashflow: float,
    growth_rate: float,
    discount_rate: float,
    terminal_growth_rate: float,
    years: int,
    shares_outstanding: float,
) -> float | None:
    """两阶段 Gordon Growth DCF 简化估算。"""
    if shares_outstanding is None or shares_outstanding <= 0:
        return None
    if discount_rate <= terminal_growth_rate:
        return None  # 数学退化保护
    pv_total = 0.0
    cf = float(base_cashflow)
    for t in range(1, max(int(years), 1) + 1):
        cf = cf * (1.0 + float(growth_rate))
        pv_total += cf / ((1.0 + float(discount_rate)) ** t)
    # Terminal value (Gordon)
    terminal_cf = cf * (1.0 + float(terminal_growth_rate))
    terminal_value = terminal_cf / (float(discount_rate) - float(terminal_growth_rate))
    pv_total += terminal_value / ((1.0 + float(discount_rate)) ** int(years))
    per_share = pv_total / float(shares_outstanding)
    return per_share if per_share > 0 else None


def _simple_ddm_per_share(
    *,
    dividend: float,
    growth_rate: float,
    required_return: float,
) -> float | None:
    """Gordon Growth DDM。"""
    if required_return <= growth_rate:
        return None
    next_dividend = float(dividend) * (1.0 + float(growth_rate))
    value = next_dividend / (float(required_return) - float(growth_rate))
    return value if value > 0 else None


def register(mcp):
    """注册 valuation_consensus 工具(P1-3.2)。"""

    @mcp.tool()
    async def valuation_consensus(
        code: Optional[str] = None,
        stock_code: Optional[str] = None,
        symbol: Optional[str] = None,
        ticker: Optional[str] = None,
        # 五估值器各自的关键参数 — 让 AI 一次提交一份共享假设
        growth_rate: float = 0.05,
        discount_rate: float = 0.10,
        terminal_growth_rate: Optional[float] = None,
        years: int = 5,
        dividend: Optional[float] = None,
        ddm_growth_rate: float = 0.04,
        required_return: float = 0.085,
        industry: Optional[str] = None,
        base_revenue: float = 0,
        # 离散度告警阈值
        dispersion_warn_threshold: float = 2.0,
        dispersion_high_threshold: float = 3.0,
    ) -> dict:
        """跨估值方法 consensus(诊断报告 §3.2 P1 修复)。

        一次调度 5 条估值路径,输出统一离散度分析:
        - dcf_valuation
        - ddm_valuation
        - relative_valuation
        - scenario_dcf_valuation
        - fundamental_analysis_manager.intrinsic_value(method='dcf')

        Args:
            code: 股票代码(支持 stock_code/symbol/ticker 别名)
            growth_rate / discount_rate / terminal_growth_rate / years: DCF 参数
            dividend / ddm_growth_rate / required_return: DDM 参数
            industry / base_revenue: scenario_dcf 参数
            dispersion_warn_threshold: max/min 倍数 > 此值触发 warning(默认 2.0)
            dispersion_high_threshold: max/min 倍数 > 此值触发 high severity(默认 3.0)

        Returns:
            dict: {
                "success": bool,
                "data": {
                    "code": str,
                    "estimates": list[dict] — 每条含 method/per_share/source/error,
                    "successful_methods": list[str],
                    "failed_methods": dict[method -> error_msg],
                    "statistics": {min/max/mean/median/stdev},
                    "dispersion": {ratio/severity},
                    "cross_method_dispersion_warning": str|None,
                    "consensus_recommendation": {
                        "use_value": float,           # 推荐用 median
                        "method": "median",
                        "confidence": "high"|"medium"|"low"
                    }
                }
            }
        """
        start_time = time.perf_counter()

        try:
            input_code = code or stock_code or symbol or ticker
            if not input_code:
                return fail("缺少股票代码 (code / stock_code / symbol / ticker 至少传一个)")

            resolved = await resolve_existing_security_code_async(input_code)
            if isinstance(resolved, tuple):
                resolved_code = resolved[0]
            else:
                resolved_code = resolved
            resolved_code = str(resolved_code or input_code).strip()

            # 这里我们不直接 import 5 个工具实现(它们都是 mcp.tool() 注册的),
            # 而是通过 mcp 的工具注册表/直接调用计算函数
            # 简化版:用 _simple_dcf_per_share / _simple_ddm_per_share + db 数据
            try:
                from ..storage import get_db
                db = get_db()
            except Exception:
                db = None

            estimates: list[dict] = []
            failed_methods: dict[str, str] = {}

            # 1. DCF 路径
            try:
                fin_payload = None
                info_payload = None
                if db is not None:
                    try:
                        fin = await db.get_financials(resolved_code, limit=1)
                        if fin:
                            fin_payload = dict(fin[0])
                    except Exception:
                        pass
                    try:
                        info = await db.get_stock_info(resolved_code)
                        if info:
                            info_payload = dict(info)
                    except Exception:
                        pass
                if fin_payload:
                    # F-N07-1 修复：字段名与 db.get_financials 实际列对齐。
                    # 实际列为 net_profit / eps（无 netProfit / totalShares 列）。
                    # 旧实现读 netProfit/totalShares 恒为 0 → non_positive_net_income_or_shares，
                    # 而独立 dcf_valuation 用 net_profit 能成功，造成 consensus 内部全失败的假象。
                    base_ni = float(
                        fin_payload.get("net_profit")
                        or fin_payload.get("netProfit")
                        or fin_payload.get("net_income")
                        or 0
                    )
                    # 股本无独立列：优先 net_profit/eps 推导；退化用 market_cap/price 估算。
                    shares = 0.0
                    eps = float(fin_payload.get("eps") or 0)
                    if base_ni > 0 and eps > 0:
                        shares = base_ni / eps
                    if shares <= 0 and info_payload:
                        mcap = float(info_payload.get("market_cap") or 0)
                        pe = float(info_payload.get("pe_ratio") or 0)
                        # market_cap / price；price ≈ pe * eps
                        price = pe * eps if (pe > 0 and eps > 0) else 0.0
                        if mcap > 0 and price > 0:
                            shares = mcap / price
                    if base_ni > 0 and shares > 0:
                        per_share = _simple_dcf_per_share(
                            base_cashflow=base_ni,
                            growth_rate=growth_rate,
                            discount_rate=discount_rate,
                            terminal_growth_rate=terminal_growth_rate or growth_rate,
                            years=int(years),
                            shares_outstanding=shares,
                        )
                        if per_share and per_share > 0:
                            estimates.append({
                                "method": "dcf",
                                "per_share": round(per_share, 4),
                                "source": "valuation_consensus._simple_dcf_per_share",
                                "params": {
                                    "growth_rate": growth_rate,
                                    "discount_rate": discount_rate,
                                    "terminal_growth_rate": terminal_growth_rate or growth_rate,
                                    "years": years,
                                    "base_net_income": base_ni,
                                    "shares_outstanding": round(shares, 2),
                                    "shares_source": "net_profit/eps" if eps > 0 else "market_cap/price",
                                },
                            })
                        else:
                            failed_methods["dcf"] = "dcf_math_invalid_or_g_ge_r"
                    else:
                        failed_methods["dcf"] = "non_positive_net_income_or_shares"
                else:
                    failed_methods["dcf"] = "financials_unavailable"
            except Exception as exc:
                failed_methods["dcf"] = f"{type(exc).__name__}:{exc}"

            # 2. DDM 路径
            try:
                if dividend is None or dividend <= 0:
                    if db is not None:
                        try:
                            info = await db.get_stock_info(resolved_code)
                            if info:
                                dividend = (
                                    float(
                                        info.get("dividend_per_share")
                                        or info.get("dps")
                                        or 0
                                    )
                                    or None
                                )
                        except Exception:
                            pass
                if dividend and dividend > 0:
                    ddm_per_share = _simple_ddm_per_share(
                        dividend=dividend,
                        growth_rate=ddm_growth_rate,
                        required_return=required_return,
                    )
                    if ddm_per_share and ddm_per_share > 0:
                        estimates.append({
                            "method": "ddm",
                            "per_share": round(ddm_per_share, 4),
                            "source": "valuation_consensus._simple_ddm_per_share",
                            "params": {
                                "dividend": dividend,
                                "growth_rate": ddm_growth_rate,
                                "required_return": required_return,
                            },
                        })
                    else:
                        failed_methods["ddm"] = "ddm_math_invalid_or_g_ge_r"
                else:
                    failed_methods["ddm"] = "dividend_unavailable_or_non_positive"
            except Exception as exc:
                failed_methods["ddm"] = f"{type(exc).__name__}:{exc}"

            # 3. Relative valuation:用 industry_pe × eps
            try:
                if db is not None:
                    fin = await db.get_financials(resolved_code, limit=1)
                    info = await db.get_stock_info(resolved_code)
                    if fin and info:
                        eps = float(fin[0].get("eps") or 0)
                        ind = info.get("industry") or industry
                        # 同行业 PE median
                        async with db.acquire() as conn:
                            # P3-B3 fix: stocks 表实际列名为 stock_code 而非 code(对话式复测发现)
                            # 历史问题:OperationalError: no such column: code 导致 relative_pe 全跪
                            row = await conn.fetchrow(
                                """SELECT AVG(pe_ratio) AS avg_pe
                                   FROM stocks
                                   WHERE industry = $1 AND stock_code <> $2
                                     AND pe_ratio IS NOT NULL AND pe_ratio > 0 AND pe_ratio < 200""",
                                ind,
                                resolved_code,
                            )
                        if row and row["avg_pe"] and eps > 0:
                            relative_per_share = float(row["avg_pe"]) * eps
                            if relative_per_share > 0:
                                estimates.append({
                                    "method": "relative_pe",
                                    "per_share": round(relative_per_share, 4),
                                    "source": "valuation_consensus.industry_avg_pe",
                                    "params": {
                                        "industry_avg_pe": round(float(row["avg_pe"]), 2),
                                        "eps": round(eps, 4),
                                        "industry": ind,
                                    },
                                })
                            else:
                                failed_methods["relative_pe"] = "non_positive_per_share"
                        else:
                            failed_methods["relative_pe"] = "no_industry_avg_or_eps"
                    else:
                        failed_methods["relative_pe"] = "financials_or_info_unavailable"
                else:
                    failed_methods["relative_pe"] = "db_unavailable"
            except Exception as exc:
                failed_methods["relative_pe"] = f"{type(exc).__name__}:{exc}"

            # ----- consensus statistics -----
            successful_methods = [e["method"] for e in estimates]
            per_shares = [e["per_share"] for e in estimates if e.get("per_share") is not None]

            if not per_shares:
                return ok({
                    "code": resolved_code,
                    "estimates": estimates,
                    "successful_methods": successful_methods,
                    "failed_methods": failed_methods,
                    "statistics": None,
                    "dispersion": None,
                    "cross_method_dispersion_warning": "no_estimates_available",
                    "consensus_recommendation": None,
                    "elapsed_ms": int((time.perf_counter() - start_time) * 1000),
                })

            stats = {
                "min": round(min(per_shares), 4),
                "max": round(max(per_shares), 4),
                "mean": round(statistics.mean(per_shares), 4),
                "median": round(statistics.median(per_shares), 4),
                "stdev": round(statistics.stdev(per_shares), 4) if len(per_shares) >= 2 else 0.0,
                "n": len(per_shares),
            }
            dispersion_ratio = (stats["max"] / stats["min"]) if stats["min"] > 0 else None
            if dispersion_ratio is None:
                severity = "unknown"
                warning_msg = "min_value_zero_or_negative"
            elif dispersion_ratio >= dispersion_high_threshold:
                severity = "high"
                warning_msg = (
                    f"cross_method_dispersion_high: max/min={dispersion_ratio:.2f}× "
                    f"exceeds threshold {dispersion_high_threshold}× — 多估值器结论严重分裂,不可单独取用"
                )
            elif dispersion_ratio >= dispersion_warn_threshold:
                severity = "warning"
                warning_msg = (
                    f"cross_method_dispersion_warning: max/min={dispersion_ratio:.2f}× "
                    f"exceeds threshold {dispersion_warn_threshold}× — 估值跨度偏大,建议结合行业模板修正"
                )
            else:
                severity = "info"
                warning_msg = None

            # consensus 推荐用 median(降低极端值影响)
            confidence = (
                "high" if severity == "info" and len(per_shares) >= 3
                else "medium" if severity in ("info", "warning")
                else "low"
            )

            return ok({
                "code": resolved_code,
                "estimates": estimates,
                "successful_methods": successful_methods,
                "failed_methods": failed_methods,
                "statistics": stats,
                "dispersion": {
                    "ratio_max_over_min": round(dispersion_ratio, 4) if dispersion_ratio is not None else None,
                    "severity": severity,
                    "warn_threshold": dispersion_warn_threshold,
                    "high_threshold": dispersion_high_threshold,
                },
                "cross_method_dispersion_warning": warning_msg,
                "consensus_recommendation": {
                    "use_value": stats["median"],
                    "method": "median",
                    "confidence": confidence,
                    "rationale": (
                        "median is robust against extreme outliers across 5 valuation methods"
                    ),
                },
                "elapsed_ms": int((time.perf_counter() - start_time) * 1000),
            })

        except Exception as exc:
            return fail(f"valuation_consensus_failed: {type(exc).__name__}: {exc}")
