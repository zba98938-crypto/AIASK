"""
样本外验证协议模块 (P0-A)

提供因子级别的样本外验证能力：
- Walk-Forward 滚动验证：滑动窗口训练/测试，评估因子泛化能力
- Purged K-Fold CV：带清洗间隔的时间分层交叉验证，防止前视偏差
- Bootstrap IC 置信区间：非参数 Bootstrap 估计 IC 的置信区间

Author: AKShare MCP Server
Version: 2.0
"""

from __future__ import annotations

import math
import os
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field
from itertools import combinations
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
from scipy import stats

def _env_flag(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return bool(default)
    return str(raw).strip().lower() not in {"0", "false", "off", "no", "n"}

def _env_int(name: str, default: int, *, min_value: int, max_value: int) -> int:
    raw = os.getenv(name)
    try:
        val = int(str(raw).strip()) if raw is not None else int(default)
    except Exception:
        val = int(default)
    return max(min_value, min(max_value, val))

_CPU_COUNT = max(int(os.cpu_count() or 1), 1)
_VALIDATION_PARALLEL_ENABLED = _env_flag("AKSHARE_VALIDATION_PARALLEL_ENABLED", True)
_VALIDATION_MAX_WORKERS_DEFAULT = _env_int(
    "AKSHARE_VALIDATION_MAX_WORKERS",
    min(4, _CPU_COUNT),
    min_value=1,
    max_value=max(1, min(_CPU_COUNT, 32)),
)
_VALIDATION_PARALLEL_MIN_WORKLOAD = _env_int(
    "AKSHARE_VALIDATION_PARALLEL_MIN_WORKLOAD",
    50_000,
    min_value=1,
    max_value=10_000_000,
)
_BOOTSTRAP_MODE_DEFAULT = str(os.getenv("AKSHARE_VALIDATION_BOOTSTRAP_MODE") or "full").strip().lower() or "full"
_BOOTSTRAP_WARN_THRESHOLD = _env_int(
    "AKSHARE_VALIDATION_BOOTSTRAP_WARN_THRESHOLD",
    20_000,
    min_value=1_000,
    max_value=1_000_000,
)

def _normalize_bootstrap_mode(mode: Optional[str]) -> str:
    val = str(mode or "").strip().lower()
    if val in {"fast", "full"}:
        return val
    return "full"

def _resolve_bootstrap_iterations(
    n_bootstrap: Optional[int],
    bootstrap_mode: Optional[str],
) -> int:
    mode = _normalize_bootstrap_mode(bootstrap_mode)
    default_n = 300 if mode == "fast" else 1000
    if n_bootstrap is None:
        n = default_n
    else:
        n = int(n_bootstrap)
    return max(50, min(20_000, n))

def _rowwise_pearson_corr(x: np.ndarray, y: np.ndarray) -> np.ndarray:
    """Compute Pearson correlation row-by-row for two 2D arrays."""
    if x.ndim != 2 or y.ndim != 2:
        raise ValueError("x and y must be 2D arrays")
    if x.shape != y.shape:
        raise ValueError("x and y must have the same shape")

    x = x.astype(np.float64, copy=False)
    y = y.astype(np.float64, copy=False)
    x_center = x - x.mean(axis=1, keepdims=True)
    y_center = y - y.mean(axis=1, keepdims=True)

    numerator = np.sum(x_center * y_center, axis=1)
    denom_x = np.sum(x_center * x_center, axis=1)
    denom_y = np.sum(y_center * y_center, axis=1)
    denominator = np.sqrt(denom_x * denom_y)

    corr = np.divide(
        numerator,
        denominator,
        out=np.zeros_like(numerator, dtype=np.float64),
        where=denominator > 0,
    )
    corr[~np.isfinite(corr)] = 0.0
    return corr

def _has_variation(arr: np.ndarray, *, tol: float = 1e-12) -> bool:
    """判断数组是否存在有效波动，避免常量输入触发相关性告警。"""
    if arr.size < 2:
        return False
    arr = arr.astype(np.float64, copy=False)
    spread = float(np.max(arr) - np.min(arr))
    return bool(np.isfinite(spread) and spread > tol)

def _safe_pearson_corr(x: np.ndarray, y: np.ndarray) -> float:
    """对常量输入安全的 Pearson 相关系数。"""
    if x.size < 3 or y.size < 3:
        return 0.0
    if not _has_variation(x) or not _has_variation(y):
        return 0.0
    corr = _rowwise_pearson_corr(
        x.astype(np.float64, copy=False).reshape(1, -1),
        y.astype(np.float64, copy=False).reshape(1, -1),
    )[0]
    return float(corr) if np.isfinite(corr) else 0.0

def _safe_spearman_corr(x: np.ndarray, y: np.ndarray) -> float:
    """对常量输入安全的 Spearman 相关系数。"""
    if x.size < 3 or y.size < 3:
        return 0.0
    if not _has_variation(x) or not _has_variation(y):
        return 0.0
    rank_x = stats.rankdata(x.astype(np.float64, copy=False), method="average")
    rank_y = stats.rankdata(y.astype(np.float64, copy=False), method="average")
    if not _has_variation(rank_x) or not _has_variation(rank_y):
        return 0.0
    corr = _rowwise_pearson_corr(rank_x.reshape(1, -1), rank_y.reshape(1, -1))[0]
    return float(corr) if np.isfinite(corr) else 0.0

def _bootstrap_chunk_size(n_sample: int, n_bootstrap: int) -> int:
    # Keep each chunk to a bounded memory footprint.
    target_elements = 3_000_000
    per_row = max(1, int(n_sample))
    chunk = max(1, target_elements // per_row)
    return max(1, min(int(n_bootstrap), chunk))

def _bootstrap_ic_vectorized(
    fv: np.ndarray,
    rv: np.ndarray,
    *,
    method: str,
    n_bootstrap: int,
    rng: np.random.RandomState,
) -> np.ndarray:
    n = len(fv)
    chunk_size = _bootstrap_chunk_size(n, n_bootstrap)
    out = np.empty(n_bootstrap, dtype=np.float64)
    m = str(method or "spearman").strip().lower()

    pos = 0
    while pos < n_bootstrap:
        size = min(chunk_size, n_bootstrap - pos)
        idx = rng.randint(0, n, size=(size, n))
        sample_fv = fv[idx]
        sample_rv = rv[idx]
        if m == "spearman":
            # Spearman = Pearson on ranks; keep tie handling with scipy rankdata.
            rank_fv = stats.rankdata(sample_fv, method="average", axis=1)
            rank_rv = stats.rankdata(sample_rv, method="average", axis=1)
            out[pos : pos + size] = _rowwise_pearson_corr(rank_fv, rank_rv)
        else:
            out[pos : pos + size] = _rowwise_pearson_corr(sample_fv, sample_rv)
        pos += size

    return out

def _bootstrap_mean_vectorized(
    values: np.ndarray,
    *,
    n_bootstrap: int,
    rng: np.random.RandomState,
) -> np.ndarray:
    n = len(values)
    chunk_size = _bootstrap_chunk_size(n, n_bootstrap)
    out = np.empty(n_bootstrap, dtype=np.float64)

    pos = 0
    while pos < n_bootstrap:
        size = min(chunk_size, n_bootstrap - pos)
        idx = rng.randint(0, n, size=(size, n))
        out[pos : pos + size] = np.mean(values[idx], axis=1)
        pos += size

    return out

def _coerce_return_series(values: Any) -> np.ndarray:
    arr = np.asarray(values if values is not None else [], dtype=np.float64).reshape(-1)
    arr = arr[np.isfinite(arr)]
    return arr

def _coerce_return_matrix(values: Any) -> np.ndarray:
    arr = np.asarray(values if values is not None else [], dtype=np.float64)
    if arr.ndim == 1:
        arr = arr.reshape(-1, 1)
    if arr.ndim != 2:
        raise ValueError("returns must be 1D or 2D")
    mask = np.all(np.isfinite(arr), axis=1)
    arr = arr[mask]
    return arr

def _safe_sharpe_ratio(returns: np.ndarray, *, periods_per_year: float = 252.0) -> float:
    arr = _coerce_return_series(returns)
    if arr.size < 3:
        return 0.0
    std = float(np.std(arr, ddof=1))
    if std <= 1e-12:
        return 0.0
    annualizer = math.sqrt(max(float(periods_per_year or 1.0), 1.0))
    return float(np.mean(arr) / std * annualizer)

def _sample_moments(returns: np.ndarray) -> tuple[float, float]:
    arr = _coerce_return_series(returns)
    if arr.size < 8:
        return 0.0, 3.0
    skewness = float(stats.skew(arr, bias=False))
    kurtosis = float(stats.kurtosis(arr, fisher=False, bias=False))
    if not np.isfinite(skewness):
        skewness = 0.0
    if not np.isfinite(kurtosis) or kurtosis <= 0:
        kurtosis = 3.0
    return skewness, kurtosis

def _estimated_sharpe_std(
    observed_sharpe: float,
    sample_size: int,
    *,
    skewness: float,
    kurtosis: float,
) -> float:
    if sample_size <= 1:
        return 0.0
    variance = (
        1.0
        - float(skewness) * float(observed_sharpe)
        + ((float(kurtosis) - 1.0) / 4.0) * float(observed_sharpe) ** 2
    ) / max(sample_size - 1, 1)
    return float(math.sqrt(max(variance, 1e-12)))

def _average_off_diagonal_correlation(correlation_matrix: np.ndarray) -> Optional[float]:
    arr = np.asarray(correlation_matrix if correlation_matrix is not None else [], dtype=np.float64)
    if arr.ndim != 2 or arr.shape[0] != arr.shape[1] or arr.shape[0] < 2:
        return None
    mask = ~np.eye(arr.shape[0], dtype=bool)
    values = arr[mask]
    values = values[np.isfinite(values)]
    if values.size == 0:
        return None
    return float(np.mean(values))

def _effective_independent_trials(
    n_trials: Optional[int],
    *,
    sharpe_trials: Optional[np.ndarray] = None,
    correlation_matrix: Optional[np.ndarray] = None,
    average_correlation: Optional[float] = None,
) -> float:
    trial_count = int(n_trials or 0)
    if trial_count <= 0 and sharpe_trials is not None:
        trial_count = int(np.asarray(sharpe_trials).size)
    trial_count = max(1, trial_count)

    rho = average_correlation
    if rho is None and correlation_matrix is not None:
        rho = _average_off_diagonal_correlation(correlation_matrix)
    if rho is None or not np.isfinite(rho):
        return float(trial_count)

    rho = float(min(1.0, max(-0.99, rho)))
    # Bailey & Lopez de Prado (2014): \hat N = \hat\rho + (1-\hat\rho) M
    effective = rho + (1.0 - rho) * float(trial_count)
    return float(max(1.0, min(float(trial_count), effective)))

def _expected_max_sharpe(mu: float, sigma: float, num_trials: float) -> float:
    sigma = max(float(sigma or 0.0), 0.0)
    num_trials = max(float(num_trials or 1.0), 1.0)
    if sigma <= 0.0 or num_trials <= 1.0:
        return float(mu)
    emc = 0.5772156649  # Euler-Mascheroni constant
    z1 = stats.norm.ppf(1.0 - 1.0 / num_trials)
    z2 = stats.norm.ppf(1.0 - 1.0 / (num_trials * math.e))
    return float(mu + sigma * ((1.0 - emc) * z1 + emc * z2))

def _stationary_bootstrap_indices(
    n_obs: int,
    *,
    restart_probability: float,
    rng: np.random.Generator,
) -> np.ndarray:
    if n_obs <= 0:
        return np.zeros(0, dtype=int)
    q = float(min(1.0, max(1e-6, restart_probability)))
    indices = np.empty(n_obs, dtype=int)
    indices[0] = int(rng.integers(0, n_obs))
    for i in range(1, n_obs):
        if float(rng.random()) < q:
            indices[i] = int(rng.integers(0, n_obs))
        else:
            indices[i] = (indices[i - 1] + 1) % n_obs
    return indices

def _autocovariance(series: np.ndarray, lag: int) -> float:
    arr = _coerce_return_series(series)
    n = arr.size
    if lag < 0 or lag >= n:
        return 0.0
    centered = arr - float(np.mean(arr))
    if lag == 0:
        return float(np.dot(centered, centered) / n)
    return float(np.dot(centered[lag:], centered[:-lag]) / n)

def _hac_long_run_variance(series: np.ndarray, max_lag: Optional[int] = None) -> float:
    arr = _coerce_return_series(series)
    n = arr.size
    if n < 3:
        return max(float(np.var(arr)) if n else 0.0, 1e-12)
    lag = int(max_lag) if max_lag is not None else int(math.floor(1.5 * n ** (1.0 / 3.0)))
    lag = max(1, min(lag, n - 1))
    gamma0 = _autocovariance(arr, 0)
    variance = gamma0
    for k in range(1, lag + 1):
        weight = 1.0 - k / float(lag + 1)
        gamma = _autocovariance(arr, k)
        variance += 2.0 * weight * gamma
    return float(max(variance, 1e-12))

def _performance_metric(
    returns: np.ndarray,
    *,
    metric: str,
    periods_per_year: float,
) -> float:
    arr = _coerce_return_series(returns)
    if arr.size < 3:
        return 0.0
    metric_name = str(metric or "sharpe").strip().lower()
    if metric_name == "mean":
        return float(np.mean(arr))
    if metric_name in {"sum", "cumulative"}:
        return float(np.sum(arr))
    return _safe_sharpe_ratio(arr, periods_per_year=periods_per_year)

def deflated_sharpe_ratio(
    returns: Optional[np.ndarray] = None,
    *,
    observed_sharpe: Optional[float] = None,
    n_trials: Optional[int] = None,
    sharpe_trials: Optional[np.ndarray] = None,
    correlation_matrix: Optional[np.ndarray] = None,
    average_correlation: Optional[float] = None,
    benchmark_sharpe: float = 0.0,
    sample_size: Optional[int] = None,
    skewness: Optional[float] = None,
    kurtosis: Optional[float] = None,
    periods_per_year: float = 252.0,
) -> Dict[str, Any]:
    """Compute Bailey & Lopez de Prado's Deflated Sharpe Ratio."""
    arr = _coerce_return_series(returns)
    n_obs = int(sample_size or arr.size)
    if observed_sharpe is None:
        observed_sharpe = _safe_sharpe_ratio(arr, periods_per_year=periods_per_year)
    observed_sharpe = float(observed_sharpe or 0.0)
    if skewness is None or kurtosis is None:
        est_skew, est_kurt = _sample_moments(arr)
        skewness = est_skew if skewness is None else float(skewness)
        kurtosis = est_kurt if kurtosis is None else float(kurtosis)
    else:
        skewness = float(skewness)
        kurtosis = float(kurtosis)

    sharpe_arr = np.asarray(sharpe_trials if sharpe_trials is not None else [], dtype=np.float64).reshape(-1)
    sharpe_arr = sharpe_arr[np.isfinite(sharpe_arr)]
    effective_trials = _effective_independent_trials(
        n_trials,
        sharpe_trials=sharpe_arr if sharpe_arr.size else None,
        correlation_matrix=correlation_matrix,
        average_correlation=average_correlation,
    )
    if sharpe_arr.size >= 2:
        mu_trials = float(np.mean(sharpe_arr))
        sigma_trials = float(np.std(sharpe_arr, ddof=1))
        reference_mode = "family_sharpes"
    else:
        mu_trials = float(benchmark_sharpe or 0.0)
        sigma_trials = _estimated_sharpe_std(
            observed_sharpe,
            n_obs,
            skewness=float(skewness),
            kurtosis=float(kurtosis),
        )
        reference_mode = "estimated_standard_error"

    sr_ref = _expected_max_sharpe(mu_trials, sigma_trials, effective_trials)
    denominator = math.sqrt(
        max(
            1e-12,
            1.0
            - float(skewness) * observed_sharpe
            + ((float(kurtosis) - 1.0) / 4.0) * observed_sharpe ** 2,
        )
    )
    z_score = ((observed_sharpe - sr_ref) * math.sqrt(max(n_obs - 1, 1))) / denominator
    dsr = float(stats.norm.cdf(z_score))
    return {
        "available": bool(n_obs >= 3),
        "sample_size": int(n_obs),
        "observed_sharpe": float(observed_sharpe),
        "benchmark_sharpe": float(benchmark_sharpe or 0.0),
        "reference_sharpe": float(sr_ref),
        "reference_mean": float(mu_trials),
        "reference_std": float(sigma_trials),
        "effective_trials": float(effective_trials),
        "skewness": float(skewness),
        "kurtosis": float(kurtosis),
        "z_score": float(z_score),
        "dsr": dsr,
        "psr_vs_benchmark": float(
            stats.norm.cdf(((observed_sharpe - float(benchmark_sharpe or 0.0)) * math.sqrt(max(n_obs - 1, 1))) / denominator)
        ),
        "reference_mode": reference_mode,
    }

def probability_of_backtest_overfitting(
    family_returns: np.ndarray,
    *,
    n_splits: int = 8,
    metric: str = "sharpe",
    periods_per_year: float = 252.0,
    max_combinations: int = 4096,
    seed: Optional[int] = 42,
) -> Dict[str, Any]:
    """Estimate PBO using CSCV (Bailey et al., 2014)."""
    matrix = _coerce_return_matrix(family_returns)
    n_obs, n_models = matrix.shape
    if n_obs < 12 or n_models < 2:
        return {
            "available": False,
            "reason": "insufficient_family_returns",
            "sample_size": int(n_obs),
            "model_count": int(n_models),
        }

    splits = int(n_splits)
    if splits % 2 != 0:
        splits -= 1
    splits = max(2, min(splits, n_obs))
    while splits > 2 and (splits % 2 != 0 or n_obs // splits < 2):
        splits -= 2
    if splits < 2:
        return {
            "available": False,
            "reason": "insufficient_split_blocks",
            "sample_size": int(n_obs),
            "model_count": int(n_models),
        }

    block_indices = [np.asarray(idx, dtype=int) for idx in np.array_split(np.arange(n_obs), splits) if len(idx) > 0]
    if len(block_indices) % 2 != 0:
        block_indices = block_indices[:-1]
    splits = len(block_indices)
    if splits < 2:
        return {
            "available": False,
            "reason": "insufficient_nonempty_blocks",
            "sample_size": int(n_obs),
            "model_count": int(n_models),
        }

    half = splits // 2
    combos = list(combinations(range(splits), half))
    sampled = False
    if len(combos) > max_combinations:
        rng = np.random.default_rng(seed)
        selected = rng.choice(len(combos), size=int(max_combinations), replace=False)
        combos = [combos[i] for i in sorted(selected.tolist())]
        sampled = True

    lambda_values: list[float] = []
    winner_indices: list[int] = []
    relative_ranks: list[float] = []

    all_blocks = set(range(splits))
    for train_combo in combos:
        train_set = set(train_combo)
        test_combo = sorted(all_blocks - train_set)
        if not test_combo:
            continue
        train_idx = np.concatenate([block_indices[i] for i in sorted(train_set)])
        test_idx = np.concatenate([block_indices[i] for i in test_combo])
        if train_idx.size < 3 or test_idx.size < 3:
            continue
        is_scores = np.asarray(
            [
                _performance_metric(matrix[train_idx, j], metric=metric, periods_per_year=periods_per_year)
                for j in range(n_models)
            ],
            dtype=np.float64,
        )
        oos_scores = np.asarray(
            [
                _performance_metric(matrix[test_idx, j], metric=metric, periods_per_year=periods_per_year)
                for j in range(n_models)
            ],
            dtype=np.float64,
        )
        if not np.any(np.isfinite(is_scores)) or not np.any(np.isfinite(oos_scores)):
            continue
        winner = int(np.nanargmax(is_scores))
        ranks = stats.rankdata(oos_scores, method="average")
        relative_rank = float(ranks[winner] / (n_models + 1.0))
        relative_rank = float(min(1.0 - 1e-8, max(1e-8, relative_rank)))
        lambda_values.append(float(math.log(relative_rank / (1.0 - relative_rank))))
        winner_indices.append(winner)
        relative_ranks.append(relative_rank)

    lambda_arr = np.asarray(lambda_values, dtype=np.float64)
    if lambda_arr.size == 0:
        return {
            "available": False,
            "reason": "no_valid_cscv_partitions",
            "sample_size": int(n_obs),
            "model_count": int(n_models),
        }

    return {
        "available": True,
        "sample_size": int(n_obs),
        "model_count": int(n_models),
        "n_splits": int(splits),
        "partition_count": int(lambda_arr.size),
        "sampled_partitions": bool(sampled),
        "metric": str(metric),
        "pbo": float(np.mean(lambda_arr <= 0.0)),
        "lambda_mean": float(np.mean(lambda_arr)),
        "lambda_median": float(np.median(lambda_arr)),
        "relative_rank_mean": float(np.mean(relative_ranks)),
        "relative_rank_median": float(np.median(relative_ranks)),
        "winner_index_mode": int(np.bincount(np.asarray(winner_indices, dtype=int)).argmax()) if winner_indices else None,
    }

def _prepare_relative_performance_matrix(
    family_returns: np.ndarray,
    benchmark_returns: Optional[np.ndarray] = None,
) -> np.ndarray:
    matrix = _coerce_return_matrix(family_returns)
    if benchmark_returns is None:
        return matrix
    benchmark = _coerce_return_series(benchmark_returns)
    if benchmark.size != matrix.shape[0]:
        raise ValueError("benchmark_returns must have the same number of periods as family_returns")
    return matrix - benchmark.reshape(-1, 1)

def white_reality_check(
    family_returns: np.ndarray,
    *,
    benchmark_returns: Optional[np.ndarray] = None,
    n_bootstrap: int = 1000,
    stationary_bootstrap_p: float = 0.1,
    seed: Optional[int] = 42,
) -> Dict[str, Any]:
    """White's Reality Check with stationary bootstrap."""
    differential = _prepare_relative_performance_matrix(family_returns, benchmark_returns)
    n_obs, n_models = differential.shape
    if n_obs < 12 or n_models < 1:
        return {
            "available": False,
            "reason": "insufficient_family_returns",
            "sample_size": int(n_obs),
            "model_count": int(n_models),
        }

    observed_means = np.mean(differential, axis=0)
    observed_stat = float(np.max(np.sqrt(n_obs) * observed_means))
    centered = differential - observed_means.reshape(1, -1)

    rng = np.random.default_rng(seed)
    draws = max(100, int(n_bootstrap or 0))
    bootstrap_stats = np.empty(draws, dtype=np.float64)
    for i in range(draws):
        idx = _stationary_bootstrap_indices(
            n_obs,
            restart_probability=stationary_bootstrap_p,
            rng=rng,
        )
        sample = centered[idx, :]
        bootstrap_stats[i] = float(np.max(np.sqrt(n_obs) * np.mean(sample, axis=0)))

    best_index = int(np.argmax(observed_means))
    return {
        "available": True,
        "sample_size": int(n_obs),
        "model_count": int(n_models),
        "observed_stat": observed_stat,
        "p_value": float(np.mean(bootstrap_stats >= observed_stat)),
        "critical_value_95": float(np.percentile(bootstrap_stats, 95)),
        "best_model_index": best_index,
        "best_model_mean": float(observed_means[best_index]),
        "bootstrap_mean": float(np.mean(bootstrap_stats)),
        "bootstrap_std": float(np.std(bootstrap_stats, ddof=1)),
        "bootstrap_draws": int(draws),
        "stationary_bootstrap_p": float(stationary_bootstrap_p),
    }

def hansen_spa_test(
    family_returns: np.ndarray,
    *,
    benchmark_returns: Optional[np.ndarray] = None,
    n_bootstrap: int = 1000,
    stationary_bootstrap_p: float = 0.1,
    seed: Optional[int] = 42,
    center: str = "consistent",
    hac_lags: Optional[int] = None,
) -> Dict[str, Any]:
    """Hansen's Superior Predictive Ability test with stationary bootstrap."""
    differential = _prepare_relative_performance_matrix(family_returns, benchmark_returns)
    n_obs, n_models = differential.shape
    if n_obs < 12 or n_models < 1:
        return {
            "available": False,
            "reason": "insufficient_family_returns",
            "sample_size": int(n_obs),
            "model_count": int(n_models),
        }

    center_mode = str(center or "consistent").strip().lower()
    if center_mode not in {"lower", "consistent", "upper"}:
        center_mode = "consistent"

    means = np.mean(differential, axis=0)
    omega = np.asarray([math.sqrt(_hac_long_run_variance(differential[:, j], max_lag=hac_lags)) for j in range(n_models)])
    omega = np.where(np.isfinite(omega) & (omega > 1e-9), omega, 1e-9)
    observed_z = np.sqrt(n_obs) * means / omega
    observed_stat = float(max(0.0, np.max(observed_z)))

    if center_mode == "upper":
        mu_hat = means
    elif center_mode == "lower":
        mu_hat = np.maximum(means, 0.0)
    else:
        loglog_n = math.log(max(math.log(max(n_obs, 3)), 1.0000001))
        threshold = -np.sqrt((omega ** 2 / max(n_obs, 1)) * 2.0 * loglog_n)
        mu_hat = means * (means >= threshold)

    centered = differential - mu_hat.reshape(1, -1)
    rng = np.random.default_rng(seed)
    draws = max(100, int(n_bootstrap or 0))
    bootstrap_stats = np.empty(draws, dtype=np.float64)
    for i in range(draws):
        idx = _stationary_bootstrap_indices(
            n_obs,
            restart_probability=stationary_bootstrap_p,
            rng=rng,
        )
        sample = centered[idx, :]
        sample_means = np.mean(sample, axis=0)
        bootstrap_stats[i] = float(max(0.0, np.max(np.sqrt(n_obs) * sample_means / omega)))

    best_index = int(np.argmax(means))
    return {
        "available": True,
        "sample_size": int(n_obs),
        "model_count": int(n_models),
        "center": center_mode,
        "observed_stat": observed_stat,
        "p_value": float(np.mean(bootstrap_stats >= observed_stat)),
        "critical_value_95": float(np.percentile(bootstrap_stats, 95)),
        "best_model_index": best_index,
        "best_model_mean": float(means[best_index]),
        "best_model_zscore": float(observed_z[best_index]),
        "bootstrap_mean": float(np.mean(bootstrap_stats)),
        "bootstrap_std": float(np.std(bootstrap_stats, ddof=1)),
        "bootstrap_draws": int(draws),
        "stationary_bootstrap_p": float(stationary_bootstrap_p),
        "hac_lags": int(hac_lags) if hac_lags is not None else None,
    }

def _calc_ic_pair(
    factor_values: np.ndarray,
    returns: np.ndarray,
) -> Tuple[float, float]:
    """计算 Pearson IC 和 Spearman Rank IC"""
    mask = np.isfinite(factor_values) & np.isfinite(returns)
    if mask.sum() < 3:
        return 0.0, 0.0
    fv = factor_values[mask]
    rv = returns[mask]
    p_ic = _safe_pearson_corr(fv, rv)
    r_ic = _safe_spearman_corr(fv, rv)
    return p_ic, r_ic

def _group_return(
    factor_values: np.ndarray,
    returns: np.ndarray,
    n_groups: int = 5,
) -> Tuple[List[float], float]:
    """计算分组收益和多空收益"""
    mask = np.isfinite(factor_values) & np.isfinite(returns)
    fv = factor_values[mask]
    rv = returns[mask]
    if len(fv) < n_groups:
        return [], 0.0
    sorted_idx = np.argsort(fv)
    group_size = len(sorted_idx) // n_groups
    group_rets = []
    for i in range(n_groups):
        start = i * group_size
        end = (i + 1) * group_size if i < n_groups - 1 else len(sorted_idx)
        group_rets.append(float(np.mean(rv[sorted_idx[start:end]])))
    ls_ret = group_rets[-1] - group_rets[0]
    return group_rets, float(ls_ret)

def bootstrap_ic_ci(
    factor_values: np.ndarray,
    returns: np.ndarray,
    method: str = "spearman",
    n_bootstrap: Optional[int] = None,
    confidence: float = 0.95,
    seed: Optional[int] = None,
    bootstrap_mode: Optional[str] = None,
    large_sample_warn_threshold: Optional[int] = None,
) -> Dict[str, Any]:
    """
    Bootstrap 非参数法估计 IC 置信区间。

    Args:
        factor_values: 因子值
        returns: 收益率
        method: 'pearson' 或 'spearman'
        n_bootstrap: 重采样次数
        confidence: 置信水平
        seed: 随机种子

    Returns:
        包含 ic, ci_lower, ci_upper, se, bootstrap_ics 的字典
    """
    mode = _normalize_bootstrap_mode(bootstrap_mode or _BOOTSTRAP_MODE_DEFAULT)
    n_bootstrap_eff = _resolve_bootstrap_iterations(n_bootstrap, mode)
    warn_threshold = (
        int(large_sample_warn_threshold)
        if large_sample_warn_threshold is not None
        else _BOOTSTRAP_WARN_THRESHOLD
    )

    mask = np.isfinite(factor_values) & np.isfinite(returns)
    fv = factor_values[mask]
    rv = returns[mask]
    n = len(fv)

    if n < 5:
        return {
            "ic": 0.0,
            "ci_lower": 0.0,
            "ci_upper": 0.0,
            "se": 0.0,
            "n_bootstrap": 0,
            "sample_size": n,
            "bootstrap_mode": mode,
        }

    rng = np.random.RandomState(seed)
    boot_ics = _bootstrap_ic_vectorized(
        fv=fv,
        rv=rv,
        method=method,
        n_bootstrap=n_bootstrap_eff,
        rng=rng,
    )

    alpha = 1.0 - confidence
    ci_lower = float(np.percentile(boot_ics, 100 * alpha / 2))
    ci_upper = float(np.percentile(boot_ics, 100 * (1 - alpha / 2)))

    # 原始 IC
    if method == "spearman":
        ic_orig = _safe_spearman_corr(fv, rv)
    else:
        ic_orig = _safe_pearson_corr(fv, rv)

    return {
        "ic": ic_orig,
        "ci_lower": ci_lower,
        "ci_upper": ci_upper,
        "se": float(np.std(boot_ics)),
        "n_bootstrap": int(n_bootstrap_eff),
        "sample_size": n,
        "confidence": confidence,
        "bootstrap_mode": mode,
        "performance_hint": (
            "Large sample with full bootstrap; consider bootstrap_mode='fast' (n_bootstrap=300)."
            if n >= warn_threshold and mode == "full"
            else None
        ),
    }

def _run_walk_forward_validate_task(
    factor_panel: np.ndarray,
    return_panel: np.ndarray,
    cfg: Dict[str, Any],
) -> ValidationSummary:
    from .validation import WalkForwardValidator

    validator = WalkForwardValidator(
        train_window=int(cfg.get("train_window", 60)),
        test_window=int(cfg.get("test_window", 20)),
        step=cfg.get("step"),
        n_groups=int(cfg.get("n_groups", 5)),
        min_samples_per_period=int(cfg.get("min_samples", 10)),
    )
    return validator.validate(factor_panel, return_panel)

def _run_purged_kfold_validate_task(
    factor_panel: np.ndarray,
    return_panel: np.ndarray,
    cfg: Dict[str, Any],
) -> ValidationSummary:
    from .validation import PurgedKFoldCV

    validator = PurgedKFoldCV(
        n_folds=int(cfg.get("n_folds", 5)),
        purge_gap=int(cfg.get("purge_gap", 5)),
        n_groups=int(cfg.get("n_groups", 5)),
        min_samples_per_period=int(cfg.get("min_samples", 10)),
    )
    return validator.validate(factor_panel, return_panel)

def _run_bootstrap_ic_series_task(
    ic_series: np.ndarray,
    n_bootstrap: Optional[int],
    confidence: float,
    bootstrap_mode: Optional[str],
    seed: int = 42,
) -> Dict[str, Any]:
    from .validation import FactorValidationPipeline

    return FactorValidationPipeline._bootstrap_ic_series(
        ic_series=ic_series,
        n_bootstrap=n_bootstrap,
        confidence=confidence,
        seed=seed,
        bootstrap_mode=bootstrap_mode,
    )
