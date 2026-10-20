"""提交侧参数扰动研究：对数值参数做 ±10%/±20% 单参扰动，重跑回测，
统计交易数稳定性、响应相关性与方向一致率。

设计约束（与 CLAUDE.md「诚实优先」一致）：
- 无法测量 → 三字段均为 None + 稳定 reason 码，不写 0 补位；
- 扰动预算（默认 30s）耗尽 → 只统计已完成的部分并标 ``partial=True``；
- 全部 run 失败 → 三字段 None + reason=``perturbation_runs_failed``。
"""

from __future__ import annotations

import asyncio
import math
import time
from typing import Any, Callable, Dict, List, Optional

PERTURBATION_FACTORS: tuple[float, ...] = (0.8, 0.9, 1.1, 1.2)
DEFAULT_VARIANT_CAP = 8
DEFAULT_BUDGET_SEC = 30.0
DEFAULT_CODE_TIMEOUT_SEC = 30.0
_MIN_RUNS_FOR_STATS = 3


def _is_finite_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def numeric_params(params: dict) -> Dict[str, float]:
    out: Dict[str, float] = {}
    for key, value in (params or {}).items():
        if _is_finite_number(value) and value != 0:
            out[str(key)] = float(value)
    return out


def build_perturbation_variants(params: dict, cap: int = DEFAULT_VARIANT_CAP) -> List[dict]:
    """单参数扰动：每个数值参数取 4 个缩放因子，按参数名排序确定性截断到 cap。"""
    numeric = numeric_params(params)
    variants: List[dict] = []
    for name in sorted(numeric):
        for factor in PERTURBATION_FACTORS:
            if len(variants) >= cap:
                return variants
            variants.append({
                "param": name,
                "factor": factor,
                "value": round(numeric[name] * factor, 10),
                "magnitude": abs(1.0 - factor),
            })
    return variants


def trade_count_stability(baseline_trades: float, variant_trades: List[float]) -> Optional[float]:
    """1 - mean(|T_i - T0| / max(T0, 1))，钳到 [0, 1]；无有效变体 → None。"""
    if not variant_trades:
        return None
    denominator = max(float(baseline_trades), 1.0)
    mean_dev = sum(abs(t - float(baseline_trades)) / denominator for t in variant_trades) / len(variant_trades)
    return min(1.0, max(0.0, 1.0 - mean_dev))


def pearson_correlation(xs: List[float], ys: List[float]) -> Optional[float]:
    if len(xs) != len(ys) or len(xs) < _MIN_RUNS_FOR_STATS:
        return None
    mx = sum(xs) / len(xs)
    my = sum(ys) / len(ys)
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    # 浮点误差下「零方差」不会正好等于 0，用相对阈值判「常量序列」。
    if sxx < 1e-12 * max(1.0, mx * mx) or syy < 1e-12 * max(1.0, my * my):
        return None
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    return max(-1.0, min(1.0, sxy / math.sqrt(sxx * syy)))


def trade_count_response_correlation(variants: List[dict], variant_trades: List[float], baseline_trades: float) -> Optional[float]:
    """扰动幅度 → |ΔT|/max(T0,1) 的 Pearson 相关；幅度全同（无方差）→ None。"""
    if not variants or len(variants) != len(variant_trades):
        return None
    denom = max(float(baseline_trades), 1.0)
    magnitudes = [v["magnitude"] for v in variants]
    responses = [abs(t - float(baseline_trades)) / denom for t in variant_trades]
    return pearson_correlation(magnitudes, responses)


def direction_agreement(baseline_return: float, variant_returns: List[float]) -> Optional[float]:
    """与基准同向（同号）的变体占比；基准收益为 0 → None（方向无定义）。"""
    if not variant_returns or baseline_return == 0:
        return None
    same = sum(1 for r in variant_returns if (r > 0) == (baseline_return > 0))
    return same / len(variant_returns)


async def run_parameter_perturbation_study(
    *,
    engine: Any,
    code: str,
    klines: Any,
    strategy_type: str,
    base_params: dict,
    assumptions_kwargs: Optional[dict] = None,
    baseline_result: Optional[dict] = None,
    run_backtest_async: Optional[Callable[..., Any]] = None,
    variant_cap: int = DEFAULT_VARIANT_CAP,
    budget_sec: float = DEFAULT_BUDGET_SEC,
    code_timeout_sec: float = DEFAULT_CODE_TIMEOUT_SEC,
) -> Dict[str, Any]:
    """对 code 上跑参数扰动研究。

    返回::

        {
          "parameter_perturbation_trade_stability": float | None,
          "parameter_perturbation_trade_count_correlation": float | None,
          "parameter_perturbation_direction_agreement": float | None,
          "parameter_perturbation_study": { variants_run, variants_failed, elapsed_ms,
                                           partial, reason | None },
        }

    无法测量时三字段为 None 且 reason 非空（不补 0）。
    """
    variants = build_perturbation_variants(base_params or {}, cap=variant_cap)
    output: Dict[str, Any] = {
        "parameter_perturbation_trade_stability": None,
        "parameter_perturbation_trade_count_correlation": None,
        "parameter_perturbation_direction_agreement": None,
    }
    study: Dict[str, Any] = {
        "variants_requested": len(variants),
        "variants_run": 0,
        "variants_failed": 0,
        "partial": False,
        "reason": None,
        "elapsed_ms": 0.0,
    }

    def _finish(reason: Optional[str] = None, *, partial: bool = False) -> Dict[str, Any]:
        study["reason"] = reason
        study["partial"] = partial
        study["elapsed_ms"] = round((time.perf_counter() - started_at) * 1000, 2)
        output["parameter_perturbation_study"] = study
        return output

    started_at = time.perf_counter()
    if not variants:
        return _finish("no_numeric_params")
    if not klines:
        return _finish("klines_unavailable")

    baseline_data = dict((baseline_result or {}).get("data") or {})
    t0_raw = baseline_data.get("trades_count", baseline_data.get("trade_count"))
    if t0_raw is None:
        return _finish("baseline_trades_unavailable")
    t0 = float(t0_raw)
    baseline_return_raw = baseline_data.get("total_return")
    baseline_return = float(baseline_return_raw) if _is_finite_number(baseline_return_raw) else None

    runs: List[dict] = []
    deadline = started_at + max(1.0, float(budget_sec))

    for variant in variants:
        if time.perf_counter() >= deadline:
            study["partial"] = True
            break
        try:
            if run_backtest_async is not None:
                result = await run_backtest_async(
                    engine, code, klines, strategy_type,
                    {**(base_params or {}), variant["param"]: variant["value"], **(assumptions_kwargs or {})},
                )
            else:
                result = await asyncio.wait_for(
                    asyncio.to_thread(
                        engine.run_backtest,
                        code,
                        klines,
                        strategy_type,
                        {**(base_params or {}), variant["param"]: variant["value"], **(assumptions_kwargs or {})},
                        True,
                    ),
                    timeout=code_timeout_sec,
                )
            study["variants_run"] += 1
            if not (isinstance(result, dict) and result.get("success")):
                study["variants_failed"] += 1
                continue
            data = dict(result.get("data") or {})
            tc_raw = data.get("trades_count", data.get("trade_count"))
            tr_raw = data.get("total_return")
            runs.append({
                "variant": variant,
                "trades": float(tc_raw) if _is_finite_number(tc_raw) else None,
                "return": float(tr_raw) if _is_finite_number(tr_raw) else None,
            })
        except asyncio.TimeoutError:
            study["variants_failed"] += 1
        except Exception:
            study["variants_failed"] += 1

    if study["partial"]:
        pass  # 预算耗尽仍对已完成部分出统计，但标 partial

    trade_runs = [r for r in runs if r["trades"] is not None]
    if len(trade_runs) < _MIN_RUNS_FOR_STATS:
        if not trade_runs:
            return _finish("perturbation_runs_failed" if study["variants_failed"] else "no_successful_runs")
        # 少于 3 个有效 run：只出稳定性（均值指标可测），相关性/方向标注不可测
        stability = trade_count_stability(t0, [r["trades"] for r in trade_runs])
        output["parameter_perturbation_trade_stability"] = round(stability, 4) if stability is not None else None
        return _finish("insufficient_runs_for_correlation", partial=study["partial"])

    stability = trade_count_stability(t0, [r["trades"] for r in trade_runs])
    correlation = trade_count_response_correlation(
        [r["variant"] for r in trade_runs], [r["trades"] for r in trade_runs], t0,
    )
    returns = [r["return"] for r in trade_runs if r["return"] is not None]
    agreement = (
        direction_agreement(baseline_return, returns)
        if baseline_return is not None and returns
        else None
    )
    output["parameter_perturbation_trade_stability"] = round(stability, 4) if stability is not None else None
    output["parameter_perturbation_trade_count_correlation"] = round(correlation, 4) if correlation is not None else None
    output["parameter_perturbation_direction_agreement"] = round(agreement, 4) if agreement is not None else None
    if agreement is None and baseline_return is not None and returns:
        pass
    if baseline_return is None:
        output["parameter_perturbation_direction_agreement"] = None
        study["reason"] = study.get("reason") or "baseline_return_unavailable"
    return _finish(study.get("reason"), partial=study["partial"])
