"""
样本外验证协议模块 (P0-A)

提供因子级别的样本外验证能力：
- Walk-Forward 滚动验证：滑动窗口训练/测试，评估因子泛化能力
- Purged K-Fold CV：带清洗间隔的时间分层交叉验证，防止前视偏差
- Bootstrap IC 置信区间：非参数 Bootstrap 估计 IC 的置信区间

Author: AKShare MCP Server
Version: 2.0
"""

import math
import os
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field
from itertools import combinations
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
from scipy import stats

from ._validation_support import (
    _BOOTSTRAP_MODE_DEFAULT,
    _BOOTSTRAP_WARN_THRESHOLD,
    _CPU_COUNT,
    _VALIDATION_MAX_WORKERS_DEFAULT,
    _VALIDATION_PARALLEL_ENABLED,
    _VALIDATION_PARALLEL_MIN_WORKLOAD,
    _bootstrap_mean_vectorized,
    _calc_ic_pair,
    _coerce_return_matrix,
    _coerce_return_series,
    _group_return,
    _normalize_bootstrap_mode,
    _resolve_bootstrap_iterations,
    _run_bootstrap_ic_series_task,
    _run_purged_kfold_validate_task,
    _run_walk_forward_validate_task,
    _safe_sharpe_ratio,
    bootstrap_ic_ci,
    deflated_sharpe_ratio,
    hansen_spa_test,
    probability_of_backtest_overfitting,
    white_reality_check,
)

@dataclass
class ValidationResult:
    """单次验证结果"""
    train_ic: float = 0.0
    test_ic: float = 0.0
    train_rank_ic: float = 0.0
    test_rank_ic: float = 0.0
    train_size: int = 0
    test_size: int = 0
    train_start: int = 0
    train_end: int = 0
    test_start: int = 0
    test_end: int = 0
    group_returns: Optional[List[float]] = None
    long_short_return: float = 0.0

@dataclass
class ValidationSummary:
    """验证汇总统计"""
    n_folds: int = 0
    oos_ic_mean: float = 0.0
    oos_ic_std: float = 0.0
    oos_ic_ir: float = 0.0
    oos_rank_ic_mean: float = 0.0
    oos_rank_ic_std: float = 0.0
    oos_rank_ic_ir: float = 0.0
    is_ic_mean: float = 0.0
    is_ic_std: float = 0.0
    stability_ratio: float = 0.0
    degradation: float = 0.0
    oos_positive_ratio: float = 0.0
    oos_long_short_mean: float = 0.0
    fold_results: List[ValidationResult] = field(default_factory=list)
    ic_confidence_interval: Optional[Tuple[float, float]] = None
    method: str = ""

class WalkForwardValidator:
    """
    Walk-Forward 滚动验证器

    将时间序列数据按滑动窗口切分为训练集和测试集，
    在每个窗口内评估因子的样本内/样本外 IC 和分组收益，
    汇总得到因子的泛化能力指标。

    典型用法：
        validator = WalkForwardValidator(train_window=60, test_window=20, step=20)
        summary = validator.validate(factor_panel, return_panel)
    """

    def __init__(
        self,
        train_window: int = 60,
        test_window: int = 20,
        step: Optional[int] = None,
        n_groups: int = 5,
        min_samples_per_period: int = 10,
    ):
        """
        Args:
            train_window: 训练窗口（截面期数）
            test_window: 测试窗口（截面期数）
            step: 滑动步长，默认等于 test_window
            n_groups: 分组回测组数
            min_samples_per_period: 每个截面最少有效样本数
        """
        self.train_window = train_window
        self.test_window = test_window
        self.step = step or test_window
        self.n_groups = n_groups
        self.min_samples = min_samples_per_period

    def validate(
        self,
        factor_panel: np.ndarray,
        return_panel: np.ndarray,
    ) -> ValidationSummary:
        """
        执行 Walk-Forward 滚动验证。

        Args:
            factor_panel: 因子面板 shape=(n_periods, n_stocks)
            return_panel: 收益面板 shape=(n_periods, n_stocks)，
                          return_panel[t] 为 t 期因子对应的前瞻收益

        Returns:
            ValidationSummary
        """
        n_periods = factor_panel.shape[0]
        results: List[ValidationResult] = []

        i = 0
        while i + self.train_window + self.test_window <= n_periods:
            train_start = i
            train_end = i + self.train_window
            test_start = train_end
            test_end = test_start + self.test_window

            # 训练集：汇总多期截面 IC
            train_ics_p, train_ics_r = [], []
            for t in range(train_start, train_end):
                p_ic, r_ic = _calc_ic_pair(factor_panel[t], return_panel[t])
                if factor_panel[t][np.isfinite(factor_panel[t])].shape[0] >= self.min_samples:
                    train_ics_p.append(p_ic)
                    train_ics_r.append(r_ic)

            # 测试集
            test_ics_p, test_ics_r = [], []
            test_group_rets_all = []
            test_ls_all = []
            for t in range(test_start, test_end):
                p_ic, r_ic = _calc_ic_pair(factor_panel[t], return_panel[t])
                valid_n = factor_panel[t][np.isfinite(factor_panel[t])].shape[0]
                if valid_n >= self.min_samples:
                    test_ics_p.append(p_ic)
                    test_ics_r.append(r_ic)
                    gr, ls = _group_return(factor_panel[t], return_panel[t], self.n_groups)
                    if gr:
                        test_group_rets_all.append(gr)
                        test_ls_all.append(ls)

            if not train_ics_p or not test_ics_p:
                i += self.step
                continue

            vr = ValidationResult(
                train_ic=float(np.mean(train_ics_p)),
                test_ic=float(np.mean(test_ics_p)),
                train_rank_ic=float(np.mean(train_ics_r)),
                test_rank_ic=float(np.mean(test_ics_r)),
                train_size=len(train_ics_p),
                test_size=len(test_ics_p),
                train_start=train_start,
                train_end=train_end,
                test_start=test_start,
                test_end=test_end,
                long_short_return=float(np.mean(test_ls_all)) if test_ls_all else 0.0,
            )
            if test_group_rets_all:
                avg_gr = np.mean(test_group_rets_all, axis=0).tolist()
                vr.group_returns = avg_gr
            results.append(vr)
            i += self.step

        return self._build_summary(results, method="walk_forward")

    def _build_summary(
        self, results: List[ValidationResult], method: str
    ) -> ValidationSummary:
        if not results:
            return ValidationSummary(method=method)

        oos_ics = [r.test_ic for r in results]
        oos_rank_ics = [r.test_rank_ic for r in results]
        is_ics = [r.train_ic for r in results]

        oos_mean = float(np.mean(oos_ics))
        oos_std = float(np.std(oos_ics)) if len(oos_ics) > 1 else 0.0
        oos_ir = oos_mean / oos_std if oos_std > 0 else 0.0

        oos_rank_mean = float(np.mean(oos_rank_ics))
        oos_rank_std = float(np.std(oos_rank_ics)) if len(oos_rank_ics) > 1 else 0.0
        oos_rank_ir = oos_rank_mean / oos_rank_std if oos_rank_std > 0 else 0.0

        is_mean = float(np.mean(is_ics))
        is_std = float(np.std(is_ics)) if len(is_ics) > 1 else 0.0

        # 稳定性比率：OOS IC 均值 / IS IC 均值
        stability = oos_mean / is_mean if abs(is_mean) > 1e-8 else 0.0
        # 衰减：IS IC 均值 - OOS IC 均值
        degradation = is_mean - oos_mean

        oos_positive = sum(1 for ic in oos_ics if ic > 0) / len(oos_ics)
        oos_ls_mean = float(np.mean([r.long_short_return for r in results]))

        return ValidationSummary(
            n_folds=len(results),
            oos_ic_mean=oos_mean,
            oos_ic_std=oos_std,
            oos_ic_ir=oos_ir,
            oos_rank_ic_mean=oos_rank_mean,
            oos_rank_ic_std=oos_rank_std,
            oos_rank_ic_ir=oos_rank_ir,
            is_ic_mean=is_mean,
            is_ic_std=is_std,
            stability_ratio=float(stability),
            degradation=float(degradation),
            oos_positive_ratio=float(oos_positive),
            oos_long_short_mean=oos_ls_mean,
            fold_results=results,
            method=method,
        )

class PurgedKFoldCV:
    """
    Purged K-Fold 交叉验证

    时间分层 K-Fold，在训练集和测试集之间插入清洗间隔（purge gap），
    防止因子计算中的前视偏差（lookahead bias）。

    参考：de Prado, M.L. (2018) "Advances in Financial Machine Learning"
          Chapter 7: Cross-Validation in Finance

    典型用法：
        cv = PurgedKFoldCV(n_folds=5, purge_gap=5)
        summary = cv.validate(factor_panel, return_panel)
    """

    def __init__(
        self,
        n_folds: int = 5,
        purge_gap: int = 5,
        n_groups: int = 5,
        min_samples_per_period: int = 10,
    ):
        """
        Args:
            n_folds: 折数
            purge_gap: 清洗间隔（训练集末尾到测试集开头之间跳过的期数）
            n_groups: 分组回测组数
            min_samples_per_period: 每个截面最少有效样本数
        """
        self.n_folds = n_folds
        self.purge_gap = purge_gap
        self.n_groups = n_groups
        self.min_samples = min_samples_per_period

    def _generate_splits(
        self, n_periods: int
    ) -> List[Tuple[np.ndarray, np.ndarray]]:
        """生成带 purge gap 的时间分层 K-Fold 划分"""
        indices = np.arange(n_periods)
        fold_size = n_periods // self.n_folds
        splits = []

        for k in range(self.n_folds):
            test_start = k * fold_size
            test_end = (k + 1) * fold_size if k < self.n_folds - 1 else n_periods

            test_idx = indices[test_start:test_end]

            # 训练集：排除测试集及其前后 purge_gap
            purge_start = max(0, test_start - self.purge_gap)
            purge_end = min(n_periods, test_end + self.purge_gap)
            train_mask = np.ones(n_periods, dtype=bool)
            train_mask[purge_start:purge_end] = False
            train_idx = indices[train_mask]

            if len(train_idx) > 0 and len(test_idx) > 0:
                splits.append((train_idx, test_idx))

        return splits

    def validate(
        self,
        factor_panel: np.ndarray,
        return_panel: np.ndarray,
    ) -> ValidationSummary:
        """
        执行 Purged K-Fold 交叉验证。

        Args:
            factor_panel: 因子面板 shape=(n_periods, n_stocks)
            return_panel: 收益面板 shape=(n_periods, n_stocks)

        Returns:
            ValidationSummary
        """
        n_periods = factor_panel.shape[0]
        splits = self._generate_splits(n_periods)
        results: List[ValidationResult] = []

        for train_idx, test_idx in splits:
            # 训练集 IC
            train_ics_p, train_ics_r = [], []
            for t in train_idx:
                valid_n = np.isfinite(factor_panel[t]).sum()
                if valid_n >= self.min_samples:
                    p_ic, r_ic = _calc_ic_pair(factor_panel[t], return_panel[t])
                    train_ics_p.append(p_ic)
                    train_ics_r.append(r_ic)

            # 测试集 IC + 分组收益
            test_ics_p, test_ics_r = [], []
            test_ls_all = []
            test_group_rets_all = []
            for t in test_idx:
                valid_n = np.isfinite(factor_panel[t]).sum()
                if valid_n >= self.min_samples:
                    p_ic, r_ic = _calc_ic_pair(factor_panel[t], return_panel[t])
                    test_ics_p.append(p_ic)
                    test_ics_r.append(r_ic)
                    gr, ls = _group_return(factor_panel[t], return_panel[t], self.n_groups)
                    if gr:
                        test_group_rets_all.append(gr)
                        test_ls_all.append(ls)

            if not train_ics_p or not test_ics_p:
                continue

            vr = ValidationResult(
                train_ic=float(np.mean(train_ics_p)),
                test_ic=float(np.mean(test_ics_p)),
                train_rank_ic=float(np.mean(train_ics_r)),
                test_rank_ic=float(np.mean(test_ics_r)),
                train_size=len(train_ics_p),
                test_size=len(test_ics_p),
                train_start=int(train_idx[0]),
                train_end=int(train_idx[-1]),
                test_start=int(test_idx[0]),
                test_end=int(test_idx[-1]),
                long_short_return=float(np.mean(test_ls_all)) if test_ls_all else 0.0,
            )
            if test_group_rets_all:
                vr.group_returns = np.mean(test_group_rets_all, axis=0).tolist()
            results.append(vr)

        return WalkForwardValidator._build_summary(
            None, results, method="purged_kfold"
        )

class FactorValidationPipeline:
    """
    因子验证流水线

    整合 Walk-Forward + Purged K-Fold + Bootstrap CI，
    输出完整的因子样本外验证报告。
    """

    def __init__(
        self,
        wf_train_window: int = 60,
        wf_test_window: int = 20,
        wf_step: Optional[int] = None,
        kfold_n_folds: int = 5,
        kfold_purge_gap: int = 5,
        n_groups: int = 5,
        bootstrap_n: Optional[int] = None,
        bootstrap_confidence: float = 0.95,
        min_samples: int = 10,
        validation_parallel: bool = True,
        max_workers: int = _VALIDATION_MAX_WORKERS_DEFAULT,
        bootstrap_mode: Optional[str] = None,
    ):
        self.wf_validator = WalkForwardValidator(
            train_window=wf_train_window,
            test_window=wf_test_window,
            step=wf_step,
            n_groups=n_groups,
            min_samples_per_period=min_samples,
        )
        self.kfold_validator = PurgedKFoldCV(
            n_folds=kfold_n_folds,
            purge_gap=kfold_purge_gap,
            n_groups=n_groups,
            min_samples_per_period=min_samples,
        )
        self.bootstrap_n = int(bootstrap_n) if bootstrap_n is not None else None
        self.bootstrap_confidence = bootstrap_confidence
        self.validation_parallel = bool(validation_parallel and _VALIDATION_PARALLEL_ENABLED)
        workers = int(max_workers or _VALIDATION_MAX_WORKERS_DEFAULT)
        self.max_workers = max(1, min(workers, min(_CPU_COUNT, 8)))
        self.bootstrap_mode = _normalize_bootstrap_mode(bootstrap_mode or _BOOTSTRAP_MODE_DEFAULT)

    def run(
        self,
        factor_panel: np.ndarray,
        return_panel: np.ndarray,
        factor_name: str = "unknown",
        validation_parallel: bool = True,
        max_workers: int = _VALIDATION_MAX_WORKERS_DEFAULT,
        bootstrap_mode: Optional[str] = None,
        strategy_returns: Optional[np.ndarray] = None,
        family_returns: Optional[np.ndarray] = None,
        benchmark_returns: Optional[np.ndarray] = None,
        multiple_testing_config: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        执行完整验证流水线。

        Args:
            factor_panel: shape=(n_periods, n_stocks)
            return_panel: shape=(n_periods, n_stocks)
            factor_name: 因子名称

        Returns:
            完整验证报告字典
        """
        requested_parallel = bool(validation_parallel and self.validation_parallel and _VALIDATION_PARALLEL_ENABLED)
        workers = int(max_workers or self.max_workers)
        workers = max(1, min(workers, min(_CPU_COUNT, 8)))
        workload = int(factor_panel.shape[0] * factor_panel.shape[1])
        parallel_ready = bool(
            requested_parallel
            and workers > 1
            and workload >= _VALIDATION_PARALLEL_MIN_WORKLOAD
        )

        # 1) Build IC series for bootstrap on the full panel.
        # 截面最小样本数与 walk-forward 验证器对齐（而非写死 10），消除"bootstrap 用 10、
        # walk-forward 用 min_samples"两套阈值不一致的隐患；窄面板场景（min_samples_per_period=2）
        # 也能保留下探能力。下限 3 防止单/双股截面相关产生伪 IC。
        _min_xs = max(3, int(getattr(self.wf_validator, "min_samples", 10) or 10))
        all_ics_r: List[float] = []
        for t in range(factor_panel.shape[0]):
            _p_ic, r_ic = _calc_ic_pair(factor_panel[t], return_panel[t])
            if np.isfinite(factor_panel[t]).sum() >= _min_xs:
                all_ics_r.append(r_ic)
        ic_array = np.array(all_ics_r, dtype=np.float64)
        selected_bootstrap_mode = _normalize_bootstrap_mode(bootstrap_mode or self.bootstrap_mode)

        wf_summary: Optional[ValidationSummary] = None
        kf_summary: Optional[ValidationSummary] = None
        boot_ci: Optional[Dict[str, Any]] = None
        execution_mode = "serial"
        fallback_reason: Optional[str] = None

        if parallel_ready:
            try:
                wf_cfg = {
                    "train_window": self.wf_validator.train_window,
                    "test_window": self.wf_validator.test_window,
                    "step": self.wf_validator.step,
                    "n_groups": self.wf_validator.n_groups,
                    "min_samples": self.wf_validator.min_samples,
                }
                kf_cfg = {
                    "n_folds": self.kfold_validator.n_folds,
                    "purge_gap": self.kfold_validator.purge_gap,
                    "n_groups": self.kfold_validator.n_groups,
                    "min_samples": self.kfold_validator.min_samples,
                }
                with ProcessPoolExecutor(max_workers=min(workers, 3)) as executor:
                    wf_future = executor.submit(
                        _run_walk_forward_validate_task,
                        factor_panel,
                        return_panel,
                        wf_cfg,
                    )
                    kf_future = executor.submit(
                        _run_purged_kfold_validate_task,
                        factor_panel,
                        return_panel,
                        kf_cfg,
                    )
                    boot_future = executor.submit(
                        _run_bootstrap_ic_series_task,
                        ic_array,
                        self.bootstrap_n,
                        self.bootstrap_confidence,
                        selected_bootstrap_mode,
                        42,
                    )
                    wf_summary = wf_future.result()
                    kf_summary = kf_future.result()
                    boot_ci = boot_future.result()
                execution_mode = "parallel"
            except Exception as exc:
                fallback_reason = f"parallel_failed:{type(exc).__name__}"

        if wf_summary is None or kf_summary is None:
            wf_summary = self.wf_validator.validate(factor_panel, return_panel)
            kf_summary = self.kfold_validator.validate(factor_panel, return_panel)
            if boot_ci is None:
                boot_ci = self._bootstrap_ic_series(
                    ic_array,
                    self.bootstrap_n,
                    self.bootstrap_confidence,
                    bootstrap_mode=selected_bootstrap_mode,
                )
            execution_mode = "serial"
        elif boot_ci is None:
            boot_ci = self._bootstrap_ic_series(
                ic_array,
                self.bootstrap_n,
                self.bootstrap_confidence,
                bootstrap_mode=selected_bootstrap_mode,
            )

        # 4. 综合评级
        rating = self._compute_rating(wf_summary, kf_summary, boot_ci)
        if strategy_returns is not None:
            strategy_returns_input = strategy_returns
        else:
            implied_returns = np.asarray(factor_panel * return_panel, dtype=np.float64)
            valid_counts = np.isfinite(implied_returns).sum(axis=1)
            row_sums = np.nansum(implied_returns, axis=1)
            strategy_returns_input = np.divide(
                row_sums,
                valid_counts,
                out=np.full(implied_returns.shape[0], np.nan, dtype=np.float64),
                where=valid_counts > 0,
            )
        strategy_returns_arr = _coerce_return_series(strategy_returns_input)
        family_returns_arr = None
        if family_returns is not None:
            try:
                family_returns_arr = _coerce_return_matrix(family_returns)
            except Exception:
                family_returns_arr = None
        mt_cfg = dict(multiple_testing_config or {})
        sharpe_trials = mt_cfg.get("trial_sharpes")
        if sharpe_trials is None and family_returns_arr is not None and family_returns_arr.shape[1] >= 1:
            sharpe_trials = np.asarray(
                [
                    _safe_sharpe_ratio(
                        family_returns_arr[:, j],
                        periods_per_year=float(mt_cfg.get("periods_per_year", 252.0) or 252.0),
                    )
                    for j in range(family_returns_arr.shape[1])
                ],
                dtype=np.float64,
            )

        multiple_testing = {
            "available": False,
            "reason": "family_returns_not_provided",
            "deflated_sharpe": deflated_sharpe_ratio(
                strategy_returns_arr,
                n_trials=int(mt_cfg.get("n_trials") or (family_returns_arr.shape[1] if family_returns_arr is not None else 1)),
                sharpe_trials=sharpe_trials,
                correlation_matrix=mt_cfg.get("trial_correlation_matrix"),
                average_correlation=mt_cfg.get("average_correlation"),
                benchmark_sharpe=float(mt_cfg.get("benchmark_sharpe", 0.0) or 0.0),
                periods_per_year=float(mt_cfg.get("periods_per_year", 252.0) or 252.0),
            ),
        }
        if family_returns_arr is not None and family_returns_arr.shape[1] >= 2:
            multiple_testing = {
                "available": True,
                "deflated_sharpe": multiple_testing["deflated_sharpe"],
                "pbo": probability_of_backtest_overfitting(
                    family_returns_arr,
                    n_splits=int(mt_cfg.get("pbo_n_splits", 8) or 8),
                    metric=str(mt_cfg.get("pbo_metric", "sharpe") or "sharpe"),
                    periods_per_year=float(mt_cfg.get("periods_per_year", 252.0) or 252.0),
                    max_combinations=int(mt_cfg.get("pbo_max_combinations", 4096) or 4096),
                    seed=mt_cfg.get("seed", 42),
                ),
                "white_reality_check": white_reality_check(
                    family_returns_arr,
                    benchmark_returns=benchmark_returns,
                    n_bootstrap=int(mt_cfg.get("n_bootstrap", self.bootstrap_n or 500) or (self.bootstrap_n or 500)),
                    stationary_bootstrap_p=float(mt_cfg.get("stationary_bootstrap_p", 0.1) or 0.1),
                    seed=mt_cfg.get("seed", 42),
                ),
                "hansen_spa": hansen_spa_test(
                    family_returns_arr,
                    benchmark_returns=benchmark_returns,
                    n_bootstrap=int(mt_cfg.get("n_bootstrap", self.bootstrap_n or 500) or (self.bootstrap_n or 500)),
                    stationary_bootstrap_p=float(mt_cfg.get("stationary_bootstrap_p", 0.1) or 0.1),
                    seed=mt_cfg.get("seed", 42),
                    center=str(mt_cfg.get("spa_center", "consistent") or "consistent"),
                    hac_lags=mt_cfg.get("spa_hac_lags"),
                ),
            }

        return {
            "factor_name": factor_name,
            "n_periods": int(factor_panel.shape[0]),
            "n_stocks": int(factor_panel.shape[1]),
            "walk_forward": self._summary_to_dict(wf_summary),
            "purged_kfold": self._summary_to_dict(kf_summary),
            "bootstrap_ci": boot_ci,
            "multiple_testing": multiple_testing,
            "rating": rating,
            "validation_execution": {
                "mode": execution_mode,
                "parallel_requested": bool(requested_parallel),
                "parallel_effective": bool(execution_mode == "parallel"),
                "max_workers": int(workers),
                "workload": int(workload),
                "fallback_reason": fallback_reason,
            },
        }

    @staticmethod
    def _bootstrap_ic_series(
        ic_series: np.ndarray,
        n_bootstrap: Optional[int],
        confidence: float,
        seed: int = 42,
        bootstrap_mode: Optional[str] = None,
    ) -> Dict[str, Any]:
        """对 IC 时间序列做 Bootstrap 置信区间"""
        mode = _normalize_bootstrap_mode(bootstrap_mode or _BOOTSTRAP_MODE_DEFAULT)
        n_bootstrap_eff = _resolve_bootstrap_iterations(n_bootstrap, mode)

        n = len(ic_series)
        if n < 5:
            return {
                "ic_mean": 0.0,
                "ci_lower": 0.0,
                "ci_upper": 0.0,
                "se": 0.0,
                "sample_size": n,
                "n_bootstrap": 0,
                "bootstrap_mode": mode,
            }

        rng = np.random.RandomState(seed)
        boot_means = _bootstrap_mean_vectorized(
            ic_series.astype(np.float64, copy=False),
            n_bootstrap=n_bootstrap_eff,
            rng=rng,
        )

        alpha = 1.0 - confidence
        return {
            "ic_mean": float(np.mean(ic_series)),
            "ic_std": float(np.std(ic_series)),
            "ic_ir": float(np.mean(ic_series) / np.std(ic_series)) if np.std(ic_series) > 0 else 0.0,
            "ci_lower": float(np.percentile(boot_means, 100 * alpha / 2)),
            "ci_upper": float(np.percentile(boot_means, 100 * (1 - alpha / 2))),
            "se": float(np.std(boot_means)),
            "sample_size": n,
            "confidence": confidence,
            "n_bootstrap": int(n_bootstrap_eff),
            "bootstrap_mode": mode,
            "performance_hint": (
                "Large sample with full bootstrap; consider bootstrap_mode='fast' (n_bootstrap=300)."
                if n >= _BOOTSTRAP_WARN_THRESHOLD and mode == "full"
                else None
            ),
        }

    @staticmethod
    def _compute_rating(
        wf: ValidationSummary,
        kf: ValidationSummary,
        boot_ci: Dict[str, Any],
    ) -> Dict[str, Any]:
        """综合评级"""
        scores = {}

        # OOS IC 均值得分（0-30分）
        avg_oos_ic = (abs(wf.oos_rank_ic_mean) + abs(kf.oos_rank_ic_mean)) / 2
        scores["oos_ic"] = min(30.0, avg_oos_ic * 600)

        # OOS IC IR 得分（0-25分）
        avg_ir = (abs(wf.oos_rank_ic_ir) + abs(kf.oos_rank_ic_ir)) / 2
        scores["oos_ir"] = min(25.0, avg_ir * 50)

        # 稳定性得分（0-20分）
        avg_stability = (wf.stability_ratio + kf.stability_ratio) / 2
        scores["stability"] = min(20.0, max(0.0, avg_stability * 20))

        # CI 不含零得分（0-15分）
        ci_lower = boot_ci.get("ci_lower", 0.0)
        ci_upper = boot_ci.get("ci_upper", 0.0)
        if ci_lower > 0 or ci_upper < 0:
            scores["ci_significance"] = 15.0
        else:
            scores["ci_significance"] = 0.0

        # OOS 正比率得分（0-10分）
        avg_pos = (wf.oos_positive_ratio + kf.oos_positive_ratio) / 2
        scores["positive_ratio"] = avg_pos * 10

        total = sum(scores.values())

        if total >= 90:
            grade = "SSS"
        elif total >= 85:
            grade = "SS"
        elif total >= 80:
            grade = "S"
        elif total >= 70:
            grade = "A"
        elif total >= 55:
            grade = "B"
        elif total >= 40:
            grade = "C"
        else:
            grade = "D"

        return {
            "grade": grade,
            "total_score": float(total),
            "scores": {k: float(v) for k, v in scores.items()},
            "recommendation": (
                "Strong — 因子样本外表现稳健" if grade in ("SSS", "SS", "S", "A", "B")
                else "Weak — 因子泛化能力不足，建议审慎使用"
            ),
        }

    @staticmethod
    def _summary_to_dict(s: ValidationSummary) -> Dict[str, Any]:
        """将 ValidationSummary 转为可序列化字典"""
        return {
            "method": s.method,
            "n_folds": s.n_folds,
            "oos_ic_mean": s.oos_ic_mean,
            "oos_ic_std": s.oos_ic_std,
            "oos_ic_ir": s.oos_ic_ir,
            "oos_rank_ic_mean": s.oos_rank_ic_mean,
            "oos_rank_ic_std": s.oos_rank_ic_std,
            "oos_rank_ic_ir": s.oos_rank_ic_ir,
            "is_ic_mean": s.is_ic_mean,
            "is_ic_std": s.is_ic_std,
            "stability_ratio": s.stability_ratio,
            "degradation": s.degradation,
            "oos_positive_ratio": s.oos_positive_ratio,
            "oos_long_short_mean": s.oos_long_short_mean,
        }
