"""Quant factor analysis: IC, grouped backtest, OOS validation, robustness check."""

import time
from typing import Any, Dict, List, Optional

import numpy as np
from scipy import stats

from ..services.factor_analysis import FactorAnalyzer as ICFactorAnalyzer
from ..services.slippage import SlippageCalculator
from ..services.validation import FactorValidationPipeline, bootstrap_ic_ci
from ..storage import get_db
from ..utils import fail, ok

from .quant_definitions import (
    DEFAULT_FACTOR_LOOKBACK,
    SUPPORTED_FACTORS,
    _QUANT_PERF_BREAKDOWN_ENABLED,
    _normalize_factor_name,
    _to_bool,
)
from .quant_engine import (
    _SLIPPAGE_MODEL_MAP,
    _build_perf_breakdown,
    _build_tradability_mask_local,
    _calculate_factor_value,
    _compute_trade_return_with_costs,
    _extract_style_exposures,
    _get_or_build_market_panel,
    _new_perf_tracker,
    _new_run_cache,
    _perf_add,
    _prefetch_market_data,
    _minimum_factor_history,
)

# 显式声明需要通过 `from ._quant_analysis_support import *` 导出的私有 helper。
__all__ = [
    "_build_factor_return_panels",
    "run_factor_robustness_check",
]

# ---------------------------------------------------------------------------
# IC Analysis
# ---------------------------------------------------------------------------

async def _build_factor_return_panels(
    codes: List[str],
    factor_name: str,
    db,
    *,
    factor_lookback: int,
    forward_period: int,
    panel_periods: int,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    run_cache: Optional[Dict[str, Any]] = None,
    perf: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """构建 OOS 验证所需的二维面板：factor_panel / return_panel。"""
    local_run_cache = run_cache if isinstance(run_cache, dict) else _new_run_cache()
    local_perf = perf if isinstance(perf, dict) else _new_perf_tracker(False)
    per_code_factors: Dict[str, List[float]] = {}
    per_code_returns: Dict[str, List[float]] = {}
    requires_financials = SUPPORTED_FACTORS[factor_name]["requires_financials"]
    stats_info = {
        "input_codes": len(codes),
        "processed_codes": 0,
        "skipped_no_kline": 0,
        "skipped_no_financials": 0,
        "skipped_short_series": 0,
    }

    fetch_bars = max(80, panel_periods + factor_lookback + forward_period + 20)
    fetch_start = time.perf_counter()
    prefetch_resp = await _prefetch_market_data(
        db=db,
        codes=codes,
        need_financials=requires_financials,
        kline_limit=fetch_bars,
        start_date=start_date,
        end_date=end_date,
    )
    _perf_add(local_perf, "fetch", time.perf_counter() - fetch_start)
    prefetched = prefetch_resp.get("data", {})
    factor_stage_start = time.perf_counter()

    for code in codes:
        code_key = str(code or "").strip()
        code_data = prefetched.get(code_key, {})
        klines = code_data.get("klines") or []
        if not klines or len(klines) < (factor_lookback + forward_period + 5):
            stats_info["skipped_no_kline"] += 1
            continue

        panel = _get_or_build_market_panel(
            run_cache=local_run_cache,
            code=code_key,
            klines=klines,
            chronological=True,
            include_volume=False,
            include_returns=True,
        )
        closes_arr = panel.get("closes_arr")
        if not isinstance(closes_arr, np.ndarray) or closes_arr.shape[0] < (factor_lookback + forward_period + 5):
            stats_info["skipped_no_kline"] += 1
            continue

        financial = code_data.get("financial")
        if requires_financials and not financial:
            stats_info["skipped_no_financials"] += 1
            continue

        stock_info = code_data.get("stock_info")

        factors_one: List[float] = []
        returns_one: List[float] = []

        start_t = factor_lookback - 1
        end_t = int(closes_arr.shape[0]) - 1 - forward_period
        for t in range(start_t, end_t + 1):
            window = closes_arr[t - factor_lookback + 1 : t + 1]
            fv = _calculate_factor_value(
                factor_name,
                window,
                financial=financial,
                stock_info=stock_info,
                period=min(factor_lookback, int(window.shape[0])),
            )
            p0 = float(closes_arr[t])
            p1 = float(closes_arr[t + forward_period])
            if fv is None or np.isnan(fv) or p0 <= 0:
                continue
            ret = (p1 - p0) / p0
            if not np.isfinite(ret):
                continue
            factors_one.append(float(fv))
            returns_one.append(float(ret))

        if len(factors_one) < max(30, panel_periods // 2):
            stats_info["skipped_short_series"] += 1
            continue

        per_code_factors[code_key] = factors_one
        per_code_returns[code_key] = returns_one
        stats_info["processed_codes"] += 1
    _perf_add(local_perf, "factor", time.perf_counter() - factor_stage_start)

    if len(per_code_factors) < 3:
        return fail(f"Not enough valid codes for panel build, stats={stats_info}")

    common_len = min(len(v) for v in per_code_factors.values())
    common_len = min(common_len, panel_periods)
    if common_len < 30:
        return fail(f"Panel periods too short after alignment: {common_len}, stats={stats_info}")

    used_codes = sorted(per_code_factors.keys())
    factor_panel = np.array([per_code_factors[c][-common_len:] for c in used_codes], dtype=np.float64).T
    return_panel = np.array([per_code_returns[c][-common_len:] for c in used_codes], dtype=np.float64).T

    return ok(
        {
            "factor_panel": factor_panel,
            "return_panel": return_panel,
            "codes": used_codes,
            "periods": int(common_len),
            "stats": stats_info,
            "prefetch": prefetch_resp.get("meta", {}),
        }
    )

async def run_factor_robustness_check(
    codes: List[str],
    factor: str,
    windows: Optional[List[int]] = None,
    param_variations: Optional[List[int]] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    include_perf_breakdown: bool = True,
) -> Dict[str, Any]:
    """P2-2: 多窗口 IC 稳定性 + 参数敏感性 + 子样本一致性。"""
    factor_name = _normalize_factor_name(factor)
    if factor_name not in SUPPORTED_FACTORS:
        return fail(f"Unsupported factor: {factor_name}")
    if not codes:
        return fail("codes is required")

    run_cache = _new_run_cache()
    perf = _new_perf_tracker(_to_bool(include_perf_breakdown, _QUANT_PERF_BREAKDOWN_ENABLED))
    windows = windows or [5, 10, 20, 60]
    param_variations = param_variations or [10, 20, 40, 60]
    db = get_db()
    requires_financials = SUPPORTED_FACTORS[factor_name]["requires_financials"]
    factor_min_history = _minimum_factor_history(factor_name)
    max_lookback = max(
        [20, factor_min_history]
        + [max(int(w), factor_min_history) for w in windows]
        + [max(int(p), factor_min_history) for p in param_variations]
    )
    fetch_start = time.perf_counter()
    prefetch_resp = await _prefetch_market_data(
        db=db,
        codes=codes,
        need_financials=requires_financials,
        kline_limit=max_lookback + 30,
        start_date=start_date,
        end_date=end_date,
    )
    _perf_add(perf, "fetch", time.perf_counter() - fetch_start)
    prefetched = prefetch_resp.get("data", {})
    prefetch_meta = prefetch_resp.get("meta", {})

    # ── 辅助：单窗口截面 IC（复用预取数据） ──
    def _cross_section_ic(sub_codes: List[str], lookback: int) -> Dict[str, Any]:
        fv: List[float] = []
        fr: List[float] = []
        lb = max(int(lookback), factor_min_history)
        for code in sub_codes:
            code_key = str(code or "").strip()
            code_data = prefetched.get(code_key, {})
            klines = code_data.get("klines") or []
            if not klines or len(klines) < lb + 5:
                continue
            panel = _get_or_build_market_panel(
                run_cache=run_cache,
                code=code_key,
                klines=klines,
                chronological=False,
                include_volume=False,
                include_returns=True,
            )
            closes_arr = panel.get("closes_arr")
            if not isinstance(closes_arr, np.ndarray) or closes_arr.shape[0] < lb + 2:
                continue
            financial = code_data.get("financial")
            if requires_financials and not financial:
                continue
            stock_info = code_data.get("stock_info")
            val = _calculate_factor_value(
                factor_name,
                closes_arr[:lb],
                financial=financial,
                stock_info=stock_info,
                period=lb,
            )
            if val is None or np.isnan(val):
                continue
            ci = min(lb - 1, int(closes_arr.shape[0]) - 2)
            fi = min(ci + lb, int(closes_arr.shape[0]) - 1)
            p0 = float(closes_arr[ci]) if ci >= 0 else 0.0
            if fi <= ci or p0 <= 0:
                continue
            fv.append(float(val))
            fr.append(float((float(closes_arr[fi]) - p0) / p0))

        n = len(fv)
        if n < 3:
            return {"ic": 0.0, "rank_ic": 0.0, "sample_size": n, "significant": False}
        ic = float(np.corrcoef(fv, fr)[0, 1])
        rank_ic = float(stats.spearmanr(fv, fr).statistic)
        p_val = float(stats.spearmanr(fv, fr).pvalue)
        return {
            "ic": ic,
            "rank_ic": rank_ic,
            "p_value": p_val,
            "sample_size": n,
            "significant": bool(p_val < 0.05),
        }

    robust_stage_start = time.perf_counter()
    # ── 1) 多窗口 IC 稳定性 ──
    multi_window_results = {}
    for w in windows:
        multi_window_results[str(w)] = _cross_section_ic(codes, int(w))

    ic_values = [v["rank_ic"] for v in multi_window_results.values() if v["sample_size"] >= 10]
    window_stability = (
        float(1.0 - (np.std(ic_values) / (abs(np.mean(ic_values)) + 1e-9)))
        if len(ic_values) >= 2
        else 0.0
    )
    window_stability = max(0.0, min(1.0, window_stability))

    # ── 2) 参数敏感性 ──
    param_results = {}
    for p in param_variations:
        param_results[str(p)] = _cross_section_ic(codes, int(p))

    param_ics = [v["rank_ic"] for v in param_results.values() if v["sample_size"] >= 10]
    param_stability = (
        float(1.0 - (np.std(param_ics) / (abs(np.mean(param_ics)) + 1e-9)))
        if len(param_ics) >= 2
        else 0.0
    )
    param_stability = max(0.0, min(1.0, param_stability))

    # ── 3) 子样本一致性（前半 vs 后半） ──
    half = len(codes) // 2
    if half >= 5:
        codes_a, codes_b = codes[:half], codes[half:]
        sub_a = _cross_section_ic(codes_a, 20)
        sub_b = _cross_section_ic(codes_b, 20)
        same_sign = (sub_a.get("rank_ic", 0.0) * sub_b.get("rank_ic", 0.0)) > 0
        diff = abs(float(sub_a.get("rank_ic", 0.0)) - float(sub_b.get("rank_ic", 0.0)))
        subsample_consistency = 1.0 if same_sign and diff < 0.05 else (0.5 if same_sign else 0.0)
        subsample_detail = {
            "sub_a": {"rank_ic": float(sub_a.get("rank_ic", 0.0)), "sample_size": int(sub_a.get("sample_size", 0))},
            "sub_b": {"rank_ic": float(sub_b.get("rank_ic", 0.0)), "sample_size": int(sub_b.get("sample_size", 0))},
            "same_sign": bool(same_sign),
            "ic_diff": round(diff, 4),
        }
    else:
        subsample_consistency = 0.0
        subsample_detail = {"note": "insufficient codes for sub-sample split (need >= 10)"}

    robustness_score = round((window_stability * 0.4 + param_stability * 0.3 + subsample_consistency * 0.3), 4)
    grade = "strong" if robustness_score >= 0.7 else ("moderate" if robustness_score >= 0.4 else "weak")
    _perf_add(perf, "robust", time.perf_counter() - robust_stage_start)

    serialize_start = time.perf_counter()
    warnings: list[str] = []
    low_sample_windows = [k for k, v in multi_window_results.items() if int(v.get("sample_size", 0)) < 10]
    low_sample_params = [k for k, v in param_results.items() if int(v.get("sample_size", 0)) < 10]
    if low_sample_windows:
        warnings.append(f"low_sample_windows:{','.join(low_sample_windows)}")
    if low_sample_params:
        warnings.append(f"low_sample_params:{','.join(low_sample_params)}")
    if "note" in subsample_detail:
        warnings.append("insufficient_codes_for_subsample_split")

    # P2-4.3.5 fix(诊断报告 §4.3.5):跨 window IC 符号倒挂检测
    # 历史问题:windows=[10,20,60] IC=-0.999/+0.81/+0.05,因子在不同时间尺度方向相反但仅 grade=weak
    # 修复:emit warning 'factor_sign_inversion_across_horizons'
    horizon_ics = []
    for window_key, r in multi_window_results.items():
        if not isinstance(r, dict):
            continue
        ic_val = r.get("ic")
        if isinstance(ic_val, (int, float)) and r.get("sample_size", 0) >= 10:
            horizon_ics.append((r.get("window", window_key), float(ic_val)))
    if len(horizon_ics) >= 2:
        signs = {1 if ic > 0.05 else (-1 if ic < -0.05 else 0) for _, ic in horizon_ics}
        # 既有正又有负 → 倒挂
        if 1 in signs and -1 in signs:
            warnings.append(
                f"factor_sign_inversion_across_horizons: "
                f"IC signs differ across windows {horizon_ics} — "
                f"因子在不同时间尺度方向相反,可能是过拟合或周期性反向"
            )

    payload = {
        "factor": factor_name,
        "robustness_score": robustness_score,
        "grade": grade,
        "multi_window_ic": {"results": multi_window_results, "stability": round(window_stability, 4)},
        "param_sensitivity": {"results": param_results, "stability": round(param_stability, 4)},
        "subsample_consistency": {"score": subsample_consistency, "detail": subsample_detail},
        "weights": {"multi_window": 0.4, "param_sensitivity": 0.3, "subsample": 0.3},
        "prefetch": prefetch_meta,
        "date_range": {"start_date": start_date, "end_date": end_date},
        "warnings": warnings,
        "insufficient_sample": bool(warnings),
    }
    _perf_add(perf, "serialize", time.perf_counter() - serialize_start)
    perf_breakdown = _build_perf_breakdown(
        perf,
        prefetch_meta=prefetch_meta,
        run_cache=run_cache,
    )
    if perf_breakdown is not None:
        payload["perf_breakdown"] = perf_breakdown
    return ok(payload)
