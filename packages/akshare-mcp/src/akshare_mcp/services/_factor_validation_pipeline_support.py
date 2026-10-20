"""候选因子验证流水线：编译、横截面、OOS、稳健性、相似度、成本容量。"""

from __future__ import annotations

from collections import defaultdict
import re
from typing import Any, Optional

import numpy as np
import pandas as pd

from .cost_model import build_cost_model
from .factor_analysis import FactorAnalyzer
from .factor_candidate_compiler import compile_factor_candidate, evaluate_compiled_factor
from .validation import (
    FactorValidationPipeline,
    deflated_sharpe_ratio,
    hansen_spa_test,
    probability_of_backtest_overfitting,
    white_reality_check,
)
import logging

logger = logging.getLogger(__name__)

_SIMILARITY_BASIS_DEFINITIONS = [
    {"name": "basis_momentum_20d", "family": "momentum", "inputs": ["close"], "expression_dsl": "momentum_20d"},
    {"name": "basis_momentum_60d", "family": "momentum", "inputs": ["close"], "expression_dsl": "momentum_60d"},
    {"name": "basis_reversal_5d", "family": "reversal", "inputs": ["close"], "expression_dsl": "-return_5d"},
    {"name": "basis_volatility_20d", "family": "volatility", "inputs": ["close"], "expression_dsl": "volatility_20d"},
    {"name": "basis_volume_ratio", "family": "liquidity", "inputs": ["volume"], "expression_dsl": "volume_ratio_5_20"},
]

_SIMILARITY_BASIS_CACHE: dict[str, dict[str, Any]] = {}

def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except Exception:
        return default

def _round_float(value: Any, digits: int = 6, default: float = 0.0) -> float:
    return round(float(_safe_float(value, default)), digits)

def _clip01(value: Any, default: float = 0.0) -> float:
    return max(0.0, min(1.0, float(_safe_float(value, default))))

def _sort_frame(rows: list[dict[str, Any]]) -> pd.DataFrame:
    frame = pd.DataFrame(list(rows or []))
    if frame.empty:
        return pd.DataFrame(columns=["date", "open", "high", "low", "close", "volume", "amount", "turnover"])
    if "date" not in frame.columns:
        frame["date"] = ""
    frame = frame.copy()
    frame["_date_key"] = pd.to_datetime(frame["date"], errors="coerce")
    frame = frame.sort_values(by=["_date_key", "date"], ascending=[True, True]).drop(columns=["_date_key"])
    for column in ("open", "high", "low", "close", "volume", "amount", "turnover"):
        if column not in frame.columns:
            frame[column] = np.nan
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    frame["date"] = frame["date"].astype(str)
    return frame.reset_index(drop=True)

async def _load_validation_frame(db, code: str, lookback_bars: int) -> tuple[pd.DataFrame, list[str], Optional[str]]:
    source_chain = []
    reason = None
    rows = []
    try:
        rows = await db.get_klines(code, limit=max(160, int(lookback_bars)))
        if rows:
            source_chain.append("db.get_klines")
    except Exception as exc:
        reason = f"db.get_klines failed: {exc}"
        rows = []
    if not rows and reason is None:
        reason = "db.get_klines returned no rows"
    return _sort_frame(rows), source_chain, reason

def _dedupe(seq: list[str]) -> list[str]:
    return list(dict.fromkeys([str(item) for item in seq if str(item).strip()]))

def _build_series(frame: pd.DataFrame, column: str) -> pd.Series:
    if frame.empty or column not in frame.columns:
        return pd.Series(dtype=float)
    work = frame.copy()
    if "date" not in work.columns:
        return pd.Series(dtype=float)
    work["date"] = work["date"].astype(str)
    # Keep last bar for a trade date so panel reindex never sees duplicate labels.
    work = work.drop_duplicates(subset=["date"], keep="last")
    series = pd.to_numeric(work[column], errors="coerce").astype(float)
    out = pd.Series(series.values, index=work["date"].astype(str), dtype=float)
    if out.index.has_duplicates:
        out = out[~out.index.duplicated(keep="last")]
    return out

def _build_panel(series_map: dict[str, pd.Series]) -> pd.DataFrame:
    if not series_map:
        return pd.DataFrame()
    cleaned: dict[str, pd.Series] = {}
    for code, series in series_map.items():
        if series is None:
            continue
        item = pd.Series(series, dtype=float)
        item.index = item.index.astype(str)
        if item.index.has_duplicates:
            item = item[~item.index.duplicated(keep="last")]
        cleaned[str(code)] = item
    if not cleaned:
        return pd.DataFrame()
    panel = pd.concat(cleaned, axis=1, join="outer")
    panel.index = panel.index.astype(str)
    if panel.index.has_duplicates:
        panel = panel[~panel.index.duplicated(keep="last")]
    try:
        panel = panel.sort_index(key=lambda idx: pd.to_datetime(idx, errors="coerce"))
    except Exception:
        panel = panel.sort_index()
    return panel

def _cross_section_summary(cs_rows: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    analyzer = FactorAnalyzer()
    date_metrics = []
    diagnostic_counts: dict[str, int] = {}
    for date_key, rows in cs_rows.items():
        factor_values = [_safe_float(item.get("factor_value"), np.nan) for item in rows]
        future_returns = [_safe_float(item.get("future_return"), np.nan) for item in rows]
        if len(rows) < 3:
            continue
        factor_arr = np.asarray(factor_values, dtype=float)
        return_arr = np.asarray(future_returns, dtype=float)
        finite_mask = np.isfinite(factor_arr) & np.isfinite(return_arr)
        if int(finite_mask.sum()) < 3:
            continue
        if (
            float(np.nanstd(factor_arr[finite_mask])) < 1e-12
            or float(np.nanstd(return_arr[finite_mask])) < 1e-12
        ):
            diagnostic_counts["constant_input_warning"] = (
                diagnostic_counts.get("constant_input_warning", 0) + 1
            )
            date_metrics.append(
                {
                    "date": date_key,
                    "sample_size": int(finite_mask.sum()),
                    "normal_ic": 0.0,
                    "rank_ic": 0.0,
                    "normal_p_value": 1.0,
                    "rank_p_value": 1.0,
                }
            )
            continue
        dual = analyzer.calculate_ic_dual(
            factor_values=factor_values,
            forward_returns=future_returns,
            enable_neutralization=False,
        )
        date_metrics.append(
            {
                "date": date_key,
                "sample_size": len(rows),
                "normal_ic": float(dual.get("normal_ic", 0.0)),
                "rank_ic": float(dual.get("rank_ic", 0.0)),
                "normal_p_value": float(dual.get("normal_p_value", 1.0)),
                "rank_p_value": float(dual.get("rank_p_value", 1.0)),
            }
        )

    if not date_metrics:
        return {
            "dates": [],
            "summary": {
                "sample_dates": 0,
                "rank_ic_mean": 0.0,
                "rank_ic_std": 0.0,
                "rank_ic_ir": 0.0,
                "normal_ic_mean": 0.0,
                "positive_ratio": 0.0,
                "significant_ratio": 0.0,
            },
            "diagnostic_counts": diagnostic_counts,
        }

    rank_ics = np.array([item["rank_ic"] for item in date_metrics], dtype=float)
    normal_ics = np.array([item["normal_ic"] for item in date_metrics], dtype=float)
    rank_std = float(np.std(rank_ics)) if len(rank_ics) > 1 else 0.0
    rank_ir = float(np.mean(rank_ics) / rank_std) if rank_std > 1e-12 else float(np.mean(rank_ics) * np.sqrt(max(len(rank_ics), 1)))
    significant_ratio = float(np.mean(np.array([item["rank_p_value"] < 0.05 for item in date_metrics], dtype=float)))
    return {
        "dates": date_metrics,
        "summary": {
            "sample_dates": len(date_metrics),
            "rank_ic_mean": round(float(np.mean(rank_ics)), 6),
            "rank_ic_std": round(rank_std, 6),
            "rank_ic_ir": round(rank_ir, 6),
            "normal_ic_mean": round(float(np.mean(normal_ics)), 6),
            "positive_ratio": round(float(np.mean(rank_ics > 0)), 6),
            "significant_ratio": round(significant_ratio, 6),
        },
        "diagnostic_counts": diagnostic_counts,
    }

def _extract_latest_snapshot(cross_section_rows: dict[str, list[dict[str, Any]]], cross_section: dict[str, Any]) -> dict[str, Any]:
    if not cross_section.get("dates"):
        return {}
    latest_date = str(cross_section["dates"][-1]["date"])
    latest_rows = list(cross_section_rows.get(latest_date) or [])
    return {
        "date": latest_date,
        "sample_size": len(latest_rows),
        "top_codes": sorted(latest_rows, key=lambda item: item["factor_value"], reverse=True)[:5],
        "bottom_codes": sorted(latest_rows, key=lambda item: item["factor_value"])[:5],
    }

def _build_oos_validation_report(
    factor_df: pd.DataFrame,
    return_df: pd.DataFrame,
    *,
    factor_name: str,
) -> dict[str, Any]:
    if factor_df.empty or return_df.empty:
        return {"available": False, "reason": "empty_factor_or_return_panel"}

    common_index = factor_df.index.intersection(return_df.index)
    common_columns = factor_df.columns.intersection(return_df.columns)
    if len(common_index) < 30 or len(common_columns) < 3:
        return {
            "available": False,
            "reason": "insufficient_panel_shape",
            "n_periods": int(len(common_index)),
            "n_stocks": int(len(common_columns)),
        }

    factor_panel = factor_df.loc[common_index, common_columns].to_numpy(dtype=float)
    return_panel = return_df.loc[common_index, common_columns].to_numpy(dtype=float)
    min_samples = max(3, min(10, int(len(common_columns))))
    pipeline = FactorValidationPipeline(
        wf_train_window=max(20, min(60, int(len(common_index) * 0.45))),
        wf_test_window=max(5, min(20, int(len(common_index) * 0.2))),
        wf_step=max(5, min(20, int(len(common_index) * 0.2))),
        kfold_n_folds=max(3, min(5, int(len(common_index) // 20) or 3)),
        kfold_purge_gap=max(1, min(5, int(len(common_index) * 0.05) or 1)),
        min_samples=min_samples,
        validation_parallel=False,
        bootstrap_mode="fast",
    )
    report = pipeline.run(
        factor_panel=factor_panel,
        return_panel=return_panel,
        factor_name=factor_name,
        validation_parallel=False,
        max_workers=1,
        bootstrap_mode="fast",
    )
    return {"available": True, **report}

def _build_horizon_return_panel(close_df: pd.DataFrame, horizon: int) -> pd.DataFrame:
    horizon = max(1, int(horizon))
    shifted = close_df.shift(-horizon)
    denom = close_df.replace(0.0, np.nan)
    return (shifted - close_df) / denom

def _date_index_health(values: Any) -> dict[str, Any]:
    index = pd.Index(list(values or []), dtype="object")
    if len(index) == 0:
        return {
            "count": 0,
            "invalid_dates": 0,
            "duplicate_dates": 0,
            "monotonic_increasing": True,
        }
    dt_index = pd.to_datetime(index.astype(str), errors="coerce")
    invalid_dates = int(dt_index.isna().sum())
    valid = dt_index[~dt_index.isna()]
    return {
        "count": int(len(index)),
        "invalid_dates": invalid_dates,
        "duplicate_dates": int(valid.duplicated().sum()),
        "monotonic_increasing": bool(valid.is_monotonic_increasing) if len(valid) else True,
    }

def _detect_suspicious_expression_tokens(expression_dsl: str) -> list[str]:
    expr = str(expression_dsl or "").strip().lower()
    if not expr:
        return []
    tokens: list[str] = []
    if re.search(r"\b(?:delay|delta)\s*\([^)]*,\s*-\s*\d+", expr):
        tokens.append("negative_delay_or_delta_literal")
    if "shift(-" in expr or "shift (-" in expr:
        tokens.append("negative_shift_literal")
    for needle, label in (
        ("lead(", "lead_function_literal"),
        ("future", "future_keyword_literal"),
        ("lookahead", "lookahead_keyword_literal"),
        ("next_return", "next_return_keyword_literal"),
        ("next_close", "next_close_keyword_literal"),
    ):
        if needle in expr:
            tokens.append(label)
    return list(dict.fromkeys(tokens))

def _build_lookahead_audit(
    compiled: dict[str, Any],
    frame_map: dict[str, pd.DataFrame],
    factor_df: pd.DataFrame,
    return_df: pd.DataFrame,
    *,
    horizon_days: int,
) -> dict[str, Any]:
    if factor_df.empty or return_df.empty:
        return {"available": False, "reason": "empty_factor_or_return_panel"}

    common_index = factor_df.index.intersection(return_df.index)
    common_columns = factor_df.columns.intersection(return_df.columns)
    if len(common_index) == 0 or len(common_columns) == 0:
        return {
            "available": False,
            "reason": "no_common_factor_return_panel",
            "n_periods": int(len(common_index)),
            "n_stocks": int(len(common_columns)),
        }

    factor_panel = factor_df.loc[common_index, common_columns].to_numpy(dtype=float)
    return_panel = return_df.loc[common_index, common_columns].to_numpy(dtype=float)
    finite_pairs = int((np.isfinite(factor_panel) & np.isfinite(return_panel)).sum())
    total_cells = int(factor_panel.size)
    finite_overlap_ratio = float(finite_pairs / total_cells) if total_cells > 0 else 0.0
    truncated_return_cells = int((~np.isfinite(return_panel)).sum())

    panel_date_health = _date_index_health(common_index.tolist())
    issue_codes: list[str] = []
    invalid_dates_total = 0
    duplicate_dates_total = 0
    non_monotonic_codes: list[str] = []
    for code, frame in frame_map.items():
        check = _date_index_health((frame.get("date") if "date" in frame.columns else pd.Series(dtype=str)).tolist())
        invalid_dates_total += int(check.get("invalid_dates", 0))
        duplicate_dates_total += int(check.get("duplicate_dates", 0))
        if not bool(check.get("monotonic_increasing", True)):
            non_monotonic_codes.append(str(code))
        if (
            int(check.get("invalid_dates", 0)) > 0
            or int(check.get("duplicate_dates", 0)) > 0
            or not bool(check.get("monotonic_increasing", True))
        ):
            issue_codes.append(str(code))

    tail_rows = min(max(1, int(horizon_days)), int(len(common_index)))
    tail_index = common_index[-tail_rows:]
    tail_values = return_df.loc[tail_index, common_columns].to_numpy(dtype=float)
    non_null_future_cells = int(np.isfinite(tail_values).sum()) if tail_rows > 0 else 0

    expression_dsl = str((compiled.get("candidate") or {}).get("expression_dsl") or "")
    suspicious_tokens = _detect_suspicious_expression_tokens(expression_dsl)

    warnings: list[str] = []
    if suspicious_tokens:
        warnings.append("candidate_expression_contains_suspicious_future_token")
    if non_null_future_cells > 0:
        warnings.append("return_panel_tail_contains_non_null_future_cells")
    if not bool(panel_date_health.get("monotonic_increasing", True)):
        warnings.append("panel_dates_not_monotonic")
    if duplicate_dates_total > 0:
        warnings.append("duplicate_trade_dates_detected")
    if invalid_dates_total > 0:
        warnings.append("invalid_trade_dates_detected")
    if finite_overlap_ratio < 0.60:
        warnings.append("factor_return_overlap_ratio_low")

    risk_level = "low"
    if suspicious_tokens or non_null_future_cells > 0:
        risk_level = "high"
    elif warnings:
        risk_level = "medium"

    status = "pass"
    if risk_level == "high":
        status = "fail"
    elif risk_level == "medium":
        status = "warn"

    return {
        "available": True,
        "status": status,
        "risk_level": risk_level,
        "horizon_days": int(horizon_days),
        "warnings": warnings,
        "candidate_expression": {
            "expression_dsl": expression_dsl,
            "suspicious_tokens": suspicious_tokens,
            "referenced_fields": list(compiled.get("referenced_fields") or []),
            "function_calls": list(compiled.get("function_calls") or []),
        },
        "date_integrity": {
            "panel": panel_date_health,
            "codes_checked": int(len(frame_map)),
            "invalid_dates_total": int(invalid_dates_total),
            "duplicate_dates_total": int(duplicate_dates_total),
            "non_monotonic_codes": non_monotonic_codes[:10],
            "issue_codes": issue_codes[:10],
        },
        "panel_shape": {
            "n_periods": int(len(common_index)),
            "n_stocks": int(len(common_columns)),
            "finite_overlap_pairs": finite_pairs,
            "finite_overlap_ratio": _round_float(finite_overlap_ratio, 6),
            "truncated_return_cells": truncated_return_cells,
        },
        "tail_check": {
            "expected_tail_rows": int(tail_rows),
            "checked_stock_count": int(len(common_columns)),
            "non_null_future_cells": int(non_null_future_cells),
            "passed": bool(non_null_future_cells == 0),
        },
        "compiler_guard": {
            "safe_function_whitelist_active": True,
            "delay_functions_present": any(
                str(fn) in {"delay", "delta"} for fn in list(compiled.get("function_calls") or [])
            ),
        },
    }

def _build_long_short_return_series(
    factor_df: pd.DataFrame,
    return_df: pd.DataFrame,
    *,
    reference_index: pd.Index | None = None,
    reference_columns: pd.Index | None = None,
) -> np.ndarray:
    if factor_df.empty or return_df.empty:
        return np.zeros(0, dtype=float)

    common_index = pd.Index(reference_index) if reference_index is not None else factor_df.index.intersection(return_df.index)
    common_columns = pd.Index(reference_columns) if reference_columns is not None else factor_df.columns.intersection(return_df.columns)
    if len(common_index) == 0 or len(common_columns) < 4:
        return np.zeros(0, dtype=float)

    series: list[float] = []
    for date_key in common_index:
        if date_key not in factor_df.index or date_key not in return_df.index:
            series.append(np.nan)
            continue

        candidate_columns = factor_df.columns.intersection(return_df.columns).intersection(common_columns)
        if len(candidate_columns) < 4:
            series.append(np.nan)
            continue

        factor_row = pd.to_numeric(factor_df.loc[date_key, candidate_columns], errors="coerce")
        return_row = pd.to_numeric(return_df.loc[date_key, candidate_columns], errors="coerce")
        mask = factor_row.notna() & return_row.notna()
        if int(mask.sum()) < 4:
            series.append(np.nan)
            continue

        ranked = factor_row.loc[mask].sort_values(ascending=False)
        basket_size = max(1, min(int(len(ranked) // 3), 5))
        if len(ranked) < basket_size * 2:
            series.append(np.nan)
            continue

        top_codes = ranked.head(basket_size).index
        bottom_codes = ranked.tail(basket_size).index
        top_mean = pd.to_numeric(return_row.loc[top_codes], errors="coerce").replace([np.inf, -np.inf], np.nan).dropna().mean()
        bottom_mean = pd.to_numeric(return_row.loc[bottom_codes], errors="coerce").replace([np.inf, -np.inf], np.nan).dropna().mean()
        if not np.isfinite(top_mean) or not np.isfinite(bottom_mean):
            series.append(np.nan)
            continue
        series.append(float(top_mean - bottom_mean))

    return np.asarray(series, dtype=float)

def _build_multiple_testing_report(
    compiled: dict[str, Any],
    frame_map: dict[str, pd.DataFrame],
    factor_df: pd.DataFrame,
    return_df: pd.DataFrame,
    recorded_trial_count: int | None = None,
) -> dict[str, Any]:
    if not frame_map or factor_df.empty or return_df.empty:
        return {"available": False, "reason": "empty_frame_map_or_factor_return_panel"}

    common_index = factor_df.index.intersection(return_df.index)
    common_columns = factor_df.columns.intersection(return_df.columns)
    if len(common_index) < 20 or len(common_columns) < 4:
        return {
            "available": False,
            "reason": "insufficient_panel_shape",
            "n_periods": int(len(common_index)),
            "n_stocks": int(len(common_columns)),
        }

    candidate_returns = _build_long_short_return_series(
        factor_df,
        return_df,
        reference_index=common_index,
        reference_columns=common_columns,
    )
    if int(np.isfinite(candidate_returns).sum()) < 20:
        return {"available": False, "reason": "insufficient_candidate_long_short_samples"}

    family_members = [
        {
            "name": str((compiled.get("candidate") or {}).get("name") or "candidate_factor"),
            "kind": "candidate",
            "sample_size": int(np.isfinite(candidate_returns).sum()),
        }
    ]
    family_series = [candidate_returns]

    for basis_name, basis_compiled in _get_similarity_basis().items():
        basis_series_map: dict[str, pd.Series] = {}
        for code, frame in frame_map.items():
            try:
                basis_series = evaluate_compiled_factor(basis_compiled, frame)
                basis_series_map[code] = pd.Series(basis_series.values, index=frame["date"].astype(str), dtype=float)
            except Exception as _swallowed:
                logger.warning("packages/akshare-mcp/src/akshare_mcp/services/_factor_validation_pipeline_support.py:516: swallowed %s and continued: %s", type(_swallowed).__name__, _swallowed)
                continue

        basis_df = _build_panel(basis_series_map)
        basis_returns = _build_long_short_return_series(
            basis_df,
            return_df,
            reference_index=common_index,
            reference_columns=common_columns,
        )
        finite_count = int(np.isfinite(basis_returns).sum())
        if finite_count < 20:
            continue
        family_series.append(basis_returns)
        family_members.append(
            {
                "name": basis_name,
                "kind": "basis_factor",
                "sample_size": finite_count,
            }
        )

    if len(family_series) < 2:
        return {
            "available": False,
            "reason": "insufficient_family_members",
            "family_member_count": int(len(family_series)),
        }

    family_matrix = np.column_stack(family_series)
    family_matrix = family_matrix[np.all(np.isfinite(family_matrix), axis=1)]
    if family_matrix.shape[0] < 20 or family_matrix.shape[1] < 2:
        return {
            "available": False,
            "reason": "insufficient_family_return_matrix",
            "sample_size": int(family_matrix.shape[0]),
            "family_member_count": int(family_matrix.shape[1]),
        }

    candidate_series = family_matrix[:, 0]
    split_count = max(4, min(8, int(family_matrix.shape[0] // 20) or 4))
    # The family column count is the number of compared members, not the number
    # of trials that were actually run. When a real trial count is recorded,
    # DSR must not use a smaller denominator (a tautologically optimistic one).
    family_column_count = int(family_matrix.shape[1])
    recorded = int(recorded_trial_count) if recorded_trial_count is not None else None
    n_trials = max(family_column_count, recorded) if recorded is not None else family_column_count
    n_trials_source = "recorded_trial_count" if recorded is not None and recorded > family_column_count else "family_columns"
    dsr = deflated_sharpe_ratio(candidate_series, n_trials=n_trials, periods_per_year=252.0)
    pbo = probability_of_backtest_overfitting(family_matrix, n_splits=split_count, seed=42)
    white_rc = white_reality_check(family_matrix, n_bootstrap=200, seed=42)
    hansen_spa = hansen_spa_test(family_matrix, n_bootstrap=200, seed=42, center="consistent")

    dsr_value = _safe_float((dsr or {}).get("dsr"), np.nan)
    pbo_value = _safe_float((pbo or {}).get("pbo"), np.nan)
    rc_p_value = _safe_float((white_rc or {}).get("p_value"), np.nan)
    spa_p_value = _safe_float((hansen_spa or {}).get("p_value"), np.nan)

    # Research vs production multi-testing track.
    # - strict/production: sensitive (default)
    # - lite/minimum research: only mark high risk when BOTH dsr and pbo fail badly
    try:
        from .factor_mining_factory.quality import QUALITY_PROFILE_ACTIVE as _mt_profile
    except Exception:
        _mt_profile = "strict"
    research_track = str(_mt_profile or "strict").lower() in {"lite", "minimum"}

    warnings: list[str] = []
    dsr_cut = 0.05 if research_track else 0.10
    pbo_cut = 0.65 if research_track else 0.50
    rc_cut = 0.30 if research_track else 0.20
    spa_cut = 0.30 if research_track else 0.20
    if np.isfinite(dsr_value) and float(dsr_value) < dsr_cut:
        warnings.append("deflated_sharpe_low")
    if np.isfinite(pbo_value) and float(pbo_value) > pbo_cut:
        warnings.append("pbo_high")
    if np.isfinite(rc_p_value) and float(rc_p_value) > rc_cut:
        warnings.append("white_reality_check_weak")
    if np.isfinite(spa_p_value) and float(spa_p_value) > spa_cut:
        warnings.append("hansen_spa_weak")

    risk_level = "low"
    if research_track:
        severe = {"deflated_sharpe_low", "pbo_high"}
        severe_hits = [w for w in warnings if w in severe]
        if len(severe_hits) >= 2:
            risk_level = "high"
        elif warnings:
            risk_level = "medium"
    else:
        if "pbo_high" in warnings or "deflated_sharpe_low" in warnings:
            risk_level = "high"
        elif warnings:
            risk_level = "medium"

    return {
        "available": True,
        "risk_level": risk_level,
        "sample_size": int(family_matrix.shape[0]),
        "family_member_count": int(family_matrix.shape[1]),
        "n_trials": int(n_trials),
        "n_trials_source": n_trials_source,
        "family_members": family_members,
        "warnings": warnings,
        "deflated_sharpe": dsr,
        "pbo": pbo,
        "white_reality_check": white_rc,
        "hansen_spa": hansen_spa,
    }

def _aggregate_rank_ic(factor_df: pd.DataFrame, return_df: pd.DataFrame) -> dict[str, Any]:
    analyzer = FactorAnalyzer()
    rows = []
    common_index = factor_df.index.intersection(return_df.index)
    common_columns = factor_df.columns.intersection(return_df.columns)
    if len(common_index) == 0 or len(common_columns) < 3:
        return {"sample_dates": 0, "rank_ic_mean": 0.0, "positive_ratio": 0.0, "significant_ratio": 0.0}

    for date_key in common_index:
        factor_values = factor_df.loc[date_key, common_columns].to_numpy(dtype=float)
        future_returns = return_df.loc[date_key, common_columns].to_numpy(dtype=float)
        dual = analyzer.calculate_ic_dual(
            factor_values=factor_values.tolist(),
            forward_returns=future_returns.tolist(),
            enable_neutralization=False,
        )
        if int(dual.get("sample_size", 0)) < 3:
            continue
        rows.append(
            {
                "rank_ic": float(dual.get("rank_ic", 0.0)),
                "rank_p_value": float(dual.get("rank_p_value", 1.0)),
            }
        )
    if not rows:
        return {"sample_dates": 0, "rank_ic_mean": 0.0, "positive_ratio": 0.0, "significant_ratio": 0.0}

    rank_ics = np.array([item["rank_ic"] for item in rows], dtype=float)
    return {
        "sample_dates": len(rows),
        "rank_ic_mean": _round_float(np.mean(rank_ics)),
        "rank_ic_std": _round_float(np.std(rank_ics)),
        "positive_ratio": _round_float(np.mean(rank_ics > 0)),
        "significant_ratio": _round_float(np.mean(np.array([item["rank_p_value"] < 0.05 for item in rows], dtype=float))),
    }

def _build_robustness_report(
    factor_df: pd.DataFrame,
    close_df: pd.DataFrame,
    *,
    base_horizon: int,
) -> dict[str, Any]:
    if factor_df.empty or close_df.empty:
        return {"available": False, "reason": "empty_factor_or_close_panel"}

    common_index = factor_df.index.intersection(close_df.index)
    common_columns = factor_df.columns.intersection(close_df.columns)
    if len(common_index) < 20 or len(common_columns) < 3:
        return {
            "available": False,
            "reason": "insufficient_panel_shape",
            "n_periods": int(len(common_index)),
            "n_stocks": int(len(common_columns)),
        }

    horizons = sorted({max(1, base_horizon // 2), max(1, int(base_horizon)), min(20, max(2, base_horizon * 2))})
    horizon_results = {}
    for horizon in horizons:
        one_return_df = _build_horizon_return_panel(close_df.loc[common_index, common_columns], horizon)
        horizon_results[str(horizon)] = _aggregate_rank_ic(
            factor_df.loc[common_index, common_columns],
            one_return_df,
        )

    eligible = [value["rank_ic_mean"] for value in horizon_results.values() if int(value.get("sample_dates", 0)) >= 5]
    horizon_stability = 0.0
    if len(eligible) >= 2:
        mean_val = float(np.mean(eligible))
        std_val = float(np.std(eligible))
        horizon_stability = max(0.0, min(1.0, 1.0 - (std_val / (abs(mean_val) + 1e-9))))

    base_return_df = _build_horizon_return_panel(close_df.loc[common_index, common_columns], base_horizon)
    base_dates = list(factor_df.loc[common_index, common_columns].index.intersection(base_return_df.index))
    split_detail: dict[str, Any]
    split_consistency = 0.0
    if len(base_dates) >= 10:
        half = len(base_dates) // 2
        first_dates = base_dates[:half]
        second_dates = base_dates[half:]
        first_res = _aggregate_rank_ic(
            factor_df.loc[first_dates, common_columns],
            base_return_df.loc[first_dates, common_columns],
        )
        second_res = _aggregate_rank_ic(
            factor_df.loc[second_dates, common_columns],
            base_return_df.loc[second_dates, common_columns],
        )
        same_sign = float(first_res.get("rank_ic_mean", 0.0)) * float(second_res.get("rank_ic_mean", 0.0)) >= 0
        diff = abs(float(first_res.get("rank_ic_mean", 0.0)) - float(second_res.get("rank_ic_mean", 0.0)))
        split_consistency = 1.0 if same_sign and diff < 0.05 else (0.5 if same_sign else 0.0)
        split_detail = {
            "first_half": first_res,
            "second_half": second_res,
            "same_sign": bool(same_sign),
            "rank_ic_diff": _round_float(diff, 4),
        }
    else:
        split_detail = {"note": "insufficient_dates_for_time_split"}

    base_stats = horizon_results.get(str(base_horizon), {})
    robustness_score = round(
        horizon_stability * 0.45
        + float(base_stats.get("positive_ratio", 0.0)) * 0.20
        + float(base_stats.get("significant_ratio", 0.0)) * 0.15
        + split_consistency * 0.20,
        4,
    )
    grade = "strong" if robustness_score >= 0.7 else ("moderate" if robustness_score >= 0.4 else "weak")
    return {
        "available": True,
        "base_horizon": int(base_horizon),
        "horizon_grid": horizons,
        "horizon_results": horizon_results,
        "time_split_consistency": {
            "score": split_consistency,
            "detail": split_detail,
        },
        "robustness_score": robustness_score,
        "grade": grade,
    }

def _basis_candidate(defn: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": defn["name"],
        "hypothesis": f"basis factor {defn['name']}",
        "family": defn["family"],
        "inputs": list(defn["inputs"]),
        "expression_dsl": defn["expression_dsl"],
        "expected_holding_period": 10,
        "expected_regime": [],
        "complexity_hint": "low",
        "novelty_rationale": "internal similarity basis",
    }

def _get_similarity_basis() -> dict[str, dict[str, Any]]:
    if _SIMILARITY_BASIS_CACHE:
        return _SIMILARITY_BASIS_CACHE
    for item in _SIMILARITY_BASIS_DEFINITIONS:
        compiled = compile_factor_candidate(_basis_candidate(item))
        _SIMILARITY_BASIS_CACHE[item["name"]] = compiled
    return _SIMILARITY_BASIS_CACHE

def _safe_corr(left: np.ndarray, right: np.ndarray) -> float:
    mask = np.isfinite(left) & np.isfinite(right)
    if int(mask.sum()) < 20:
        return 0.0
    lv = left[mask]
    rv = right[mask]
    if np.std(lv) < 1e-12 or np.std(rv) < 1e-12:
        return 0.0
    corr = np.corrcoef(lv, rv)[0, 1]
    if not np.isfinite(corr):
        return 0.0
    return float(corr)

def _build_similarity_report(
    compiled: dict[str, Any],
    frame_map: dict[str, pd.DataFrame],
    factor_df: pd.DataFrame,
) -> dict[str, Any]:
    if not frame_map or factor_df.empty:
        return {"available": False, "reason": "empty_frame_map_or_factor_panel"}

    basis_rows = []
    for basis_name, basis_compiled in _get_similarity_basis().items():
        basis_series_map: dict[str, pd.Series] = {}
        for code, frame in frame_map.items():
            try:
                basis_series = evaluate_compiled_factor(basis_compiled, frame)
                basis_series_map[code] = pd.Series(basis_series.values, index=frame["date"].astype(str), dtype=float)
            except Exception as _swallowed:
                logger.warning("packages/akshare-mcp/src/akshare_mcp/services/_factor_validation_pipeline_support.py:787: swallowed %s and continued: %s", type(_swallowed).__name__, _swallowed)
                continue

        basis_df = _build_panel(basis_series_map)
        common_index = factor_df.index.intersection(basis_df.index)
        common_columns = factor_df.columns.intersection(basis_df.columns)
        if len(common_index) == 0 or len(common_columns) == 0:
            corr = 0.0
            sample_size = 0
        else:
            left = factor_df.loc[common_index, common_columns].to_numpy(dtype=float).reshape(-1)
            right = basis_df.loc[common_index, common_columns].to_numpy(dtype=float).reshape(-1)
            mask = np.isfinite(left) & np.isfinite(right)
            sample_size = int(mask.sum())
            corr = _safe_corr(left, right)

        basis_rows.append(
            {
                "basis_factor": basis_name,
                "expression_dsl": basis_compiled.get("candidate", {}).get("expression_dsl"),
                "correlation": _round_float(corr, 6),
                "abs_correlation": _round_float(abs(corr), 6),
                "sample_size": sample_size,
            }
        )

    basis_rows.sort(key=lambda item: item.get("abs_correlation", 0.0), reverse=True)
    top_match = basis_rows[0] if basis_rows else None
    token_overlap = []
    candidate_fields = set(compiled.get("referenced_fields") or [])
    for basis_name, basis_compiled in _get_similarity_basis().items():
        basis_fields = set(basis_compiled.get("referenced_fields") or [])
        union = candidate_fields | basis_fields
        jaccard = (len(candidate_fields & basis_fields) / len(union)) if union else 0.0
        token_overlap.append(
            {
                "basis_factor": basis_name,
                "field_jaccard": _round_float(jaccard, 6),
            }
        )
    token_overlap.sort(key=lambda item: item["field_jaccard"], reverse=True)

    redundancy_flag = bool(top_match and abs(float(top_match.get("correlation", 0.0))) >= 0.95)
    return {
        "available": True,
        "top_similar_basis": basis_rows[:5],
        "field_overlap": token_overlap[:5],
        "redundancy_flag": redundancy_flag,
    }

def _build_turnover_report(factor_df: pd.DataFrame) -> dict[str, Any]:
    if factor_df.empty or len(factor_df.index) < 2 or len(factor_df.columns) < 2:
        return {"available": False, "reason": "insufficient_factor_panel_for_turnover"}
    top_n = max(1, min(len(factor_df.columns), max(3, len(factor_df.columns) // 3)))
    analyzer = FactorAnalyzer()
    turnover = analyzer.factor_turnover(factor_df, top_n=top_n)
    return {
        "available": True,
        "selection_size": int(top_n),
        "stats": {key: _round_float(value, 6) for key, value in turnover.items()},
    }

def _build_cost_capacity_report(
    factor_df: pd.DataFrame,
    amount_df: pd.DataFrame,
    *,
    turnover_report: dict[str, Any],
    hypothetical_notional: float = 1_000_000.0,
    max_participation_rate: float = 0.10,
) -> dict[str, Any]:
    if factor_df.empty or amount_df.empty or len(factor_df.columns) == 0:
        return {"available": False, "reason": "empty_factor_or_amount_panel"}

    common_index = factor_df.index.intersection(amount_df.index)
    common_columns = factor_df.columns.intersection(amount_df.columns)
    if len(common_index) == 0 or len(common_columns) == 0:
        return {"available": False, "reason": "no_common_factor_amount_panel"}

    top_n = max(1, min(len(common_columns), max(3, len(common_columns) // 3)))
    basket_amounts = []
    for date_key in common_index:
        factor_row = factor_df.loc[date_key, common_columns]
        amount_row = amount_df.loc[date_key, common_columns]
        ranked_codes = factor_row.dropna().sort_values(ascending=False).head(top_n).index
        if len(ranked_codes) == 0:
            continue
        basket_amount = pd.to_numeric(amount_row.loc[ranked_codes], errors="coerce").replace([np.inf, -np.inf], np.nan).dropna().sum()
        if np.isfinite(basket_amount) and basket_amount > 0:
            basket_amounts.append(float(basket_amount))

    avg_basket_amount = float(np.mean(basket_amounts)) if basket_amounts else 0.0
    deployable_notional_per_day = avg_basket_amount * max_participation_rate
    days_to_deploy = (
        float(hypothetical_notional / deployable_notional_per_day)
        if deployable_notional_per_day > 0
        else None
    )
    cost_model = build_cost_model(
        {
            "commission_rate": 0.0003,
            "slippage_bps": 5.0,
            "market_impact_bps": 3.0,
            "rebalance_frequency": "factor_rotation",
        },
        notional=float(hypothetical_notional),
        default_mode="execution",
        reference_price_fallback=1.0,
    )
    estimated_total = float((cost_model.get("estimated") or {}).get("total", 0.0))
    estimated_cost_rate = estimated_total / hypothetical_notional if hypothetical_notional > 0 else 0.0
    turnover_penalty = float((turnover_report.get("stats") or {}).get("mean_turnover", 0.0)) if turnover_report.get("available") else 0.0

    liquidity_score = 1.0
    if days_to_deploy is not None:
        liquidity_score = max(0.0, min(1.0, 1.0 / max(days_to_deploy, 1.0)))
    execution_score = round(
        max(0.0, 1.0 - estimated_cost_rate * 40.0) * 0.6
        + max(0.0, 1.0 - turnover_penalty) * 0.2
        + liquidity_score * 0.2,
        4,
    )
    return {
        "available": True,
        "selection_size": int(top_n),
        "hypothetical_notional": float(hypothetical_notional),
        "max_participation_rate": float(max_participation_rate),
        "avg_basket_amount": _round_float(avg_basket_amount, 2),
        "deployable_notional_per_day": _round_float(deployable_notional_per_day, 2),
        "days_to_deploy": _round_float(days_to_deploy, 4, 0.0) if days_to_deploy is not None else None,
        "estimated_cost_rate": _round_float(estimated_cost_rate, 6),
        "cost_model": cost_model,
        "execution_score": execution_score,
    }
