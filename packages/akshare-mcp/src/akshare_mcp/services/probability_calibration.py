"""概率校准框架（Probability Calibration）

为 buy_probability / hold_probability / sell_probability 提供：
1. Platt Scaling（逻辑斯蒂校准）
2. Isotonic Regression（保序回归校准）
3. Reliability Diagram（可靠性图分桶统计）
4. Brier Score & ECE（期望校准误差）计算
5. Prediction Interval（预测区间）估算

外部基线参考：
- scikit-learn Probability Calibration:
  https://scikit-learn.org/stable/modules/calibration.html
- MAPIE Documentation: https://mapie.readthedocs.io/

注意：本模块同时提供纯 Python 兜底实现与 sklearn 主路径。
sklearn 主路径用 LogisticRegression(Platt/sigmoid) / IsotonicRegression 直接拟合
(score -> label)，不依赖 CalibratedClassifierCV 的内部 CV 折叠（在 sklearn>=1.6 易碎）。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Literal

# ── 工具函数 ──────────────────────────────────────────────────────────────────

def _clamp(v: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, float(v)))


def _sigmoid(x: float) -> float:
    """数值稳定的 sigmoid。"""
    if x >= 0:
        return 1.0 / (1.0 + math.exp(-x))
    ex = math.exp(x)
    return ex / (1.0 + ex)


def _log_loss_single(p: float, y: float) -> float:
    p = _clamp(p, 1e-9, 1 - 1e-9)
    return -(y * math.log(p) + (1 - y) * math.log(1 - p))


# ── Platt Scaling ─────────────────────────────────────────────────────────────

def platt_scale(
    raw_score: float,
    a: float = 1.0,
    b: float = 0.0,
) -> float:
    """Platt Scaling：将原始评分映射到校准概率。

    calibrated = sigmoid(a * raw_score + b)

    Parameters
    ----------
    raw_score:
        原始模型评分（可以是任意实数）。
    a, b:
        Platt 参数。默认 a=1, b=0 即 sigmoid(raw_score)。
        实际使用时应在保留集上拟合 a, b。

    Returns
    -------
    校准后的概率 [0, 1]。
    """
    return _clamp(_sigmoid(a * float(raw_score) + b))


def fit_platt_params(
    raw_scores: list[float],
    labels: list[float],
    lr: float = 0.1,
    max_iter: int = 200,
) -> tuple[float, float]:
    """用梯度下降拟合 Platt a, b 参数（轻量版，不依赖 sklearn）。

    Parameters
    ----------
    raw_scores:
        原始评分列表。
    labels:
        对应的二值标签 (0 / 1)。
    lr:
        学习率。
    max_iter:
        最大迭代次数。

    Returns
    -------
    (a, b) 元组。
    """
    if len(raw_scores) != len(labels) or not raw_scores:
        return (1.0, 0.0)

    a, b = 1.0, 0.0
    n = len(raw_scores)
    for _ in range(max_iter):
        da, db = 0.0, 0.0
        for s, y in zip(raw_scores, labels):
            p = _sigmoid(a * s + b)
            err = p - y
            da += err * s
            db += err
        a -= lr * da / n
        b -= lr * db / n
    return (round(a, 6), round(b, 6))


def fit_isotonic_table(
    raw_scores: list[float],
    labels: list[float],
    *,
    n_bins: int = 10,
) -> list[tuple[float, float]]:
    """拟合轻量级 isotonic 校准表。

    说明：
    - 这里不依赖 sklearn，使用分桶命中率并做单调化处理。
    - 生产主路径优先使用 sklearn isotonic；本实现作为可靠 fallback。
    """
    if len(raw_scores) != len(labels) or not raw_scores:
        return []

    ordered = sorted(zip((float(s) for s in raw_scores), (float(y) for y in labels)), key=lambda item: item[0])
    bucket_count = max(1, min(int(n_bins or 10), len(ordered)))
    bucket_size = max(1, math.ceil(len(ordered) / bucket_count))
    table: list[tuple[float, float]] = []
    previous_prob = 0.0

    for start in range(0, len(ordered), bucket_size):
        bucket = ordered[start:start + bucket_size]
        if not bucket:
            continue
        center = sum(item[0] for item in bucket) / len(bucket)
        empirical = sum(item[1] for item in bucket) / len(bucket)
        monotonic_prob = max(previous_prob, _clamp(empirical))
        previous_prob = monotonic_prob
        table.append((round(center, 6), round(monotonic_prob, 6)))

    return table


# ── Isotonic Calibration（保序回归） ─────────────────────────────────────────

def isotonic_calibrate(
    raw_score: float,
    calibration_table: list[tuple[float, float]],
) -> float:
    """基于校准查找表做保序插值。

    calibration_table: [(raw_score_bin_center, calibrated_prob), ...]，
    需按 raw_score 升序排列。

    Parameters
    ----------
    raw_score:
        待校准的原始评分。
    calibration_table:
        保序回归拟合后的分桶中心点与校准概率对应表。

    Returns
    -------
    插值后的校准概率 [0, 1]。
    """
    if not calibration_table:
        return _clamp(raw_score)

    scores = [t[0] for t in calibration_table]
    probs = [t[1] for t in calibration_table]

    if raw_score <= scores[0]:
        return _clamp(probs[0])
    if raw_score >= scores[-1]:
        return _clamp(probs[-1])

    for i in range(len(scores) - 1):
        if scores[i] <= raw_score <= scores[i + 1]:
            t = (raw_score - scores[i]) / (scores[i + 1] - scores[i])
            return _clamp(probs[i] + t * (probs[i + 1] - probs[i]))

    return _clamp(probs[-1])


# ── Reliability Diagram ───────────────────────────────────────────────────────

@dataclass
class ReliabilityBin:
    bin_lower: float
    bin_upper: float
    mean_predicted: float
    mean_actual: float
    count: int
    calibration_error: float  # |mean_predicted - mean_actual|

    def to_dict(self) -> dict[str, Any]:
        return {
            "bin": f"[{self.bin_lower:.1f},{self.bin_upper:.1f})",
            "mean_predicted": round(self.mean_predicted, 4),
            "mean_actual": round(self.mean_actual, 4),
            "count": self.count,
            "calibration_error": round(self.calibration_error, 4),
        }


def reliability_diagram(
    probabilities: list[float],
    labels: list[float],
    n_bins: int = 10,
) -> list[ReliabilityBin]:
    """计算可靠性图分桶统计。

    Parameters
    ----------
    probabilities:
        预测概率列表（校准前或校准后）。
    labels:
        对应的二值标签 (0 / 1)。
    n_bins:
        分桶数量（默认 10 个 10% 分桶）。

    Returns
    -------
    ReliabilityBin 列表，按 bin_lower 升序排列。
    """
    if len(probabilities) != len(labels):
        return []

    bins: list[ReliabilityBin] = []
    bin_size = 1.0 / n_bins

    for i in range(n_bins):
        lo = i * bin_size
        hi = lo + bin_size
        bucket_probs = []
        bucket_labels = []
        for p, y in zip(probabilities, labels):
            if lo <= p < hi or (i == n_bins - 1 and p == 1.0):
                bucket_probs.append(p)
                bucket_labels.append(y)

        if not bucket_probs:
            continue

        mean_pred = sum(bucket_probs) / len(bucket_probs)
        mean_actual = sum(bucket_labels) / len(bucket_labels)
        bins.append(ReliabilityBin(
            bin_lower=lo,
            bin_upper=hi,
            mean_predicted=mean_pred,
            mean_actual=mean_actual,
            count=len(bucket_probs),
            calibration_error=abs(mean_pred - mean_actual),
        ))

    return bins


# ── Brier Score ───────────────────────────────────────────────────────────────

def brier_score(
    probabilities: list[float],
    labels: list[float],
) -> float | None:
    """计算 Brier Score（越低越好，完美校准 = 0）。

    Brier = mean((p - y)^2)
    """
    if not probabilities or len(probabilities) != len(labels):
        return None
    n = len(probabilities)
    return round(sum((p - y) ** 2 for p, y in zip(probabilities, labels)) / n, 6)


def brier_score_single(probability: float, empirical_hit_rate: float) -> float:
    """单样本 Brier Score 近似（用经验命中率作为标签的期望值）。"""
    p = _clamp(probability)
    y = _clamp(empirical_hit_rate)
    return round((p - y) ** 2, 6)


# ── ECE（期望校准误差）────────────────────────────────────────────────────────

def expected_calibration_error(
    probabilities: list[float],
    labels: list[float],
    n_bins: int = 10,
) -> float | None:
    """计算 ECE（Expected Calibration Error）。

    ECE = sum_b (|B_b| / N) * |avg_pred_b - avg_actual_b|

    越接近 0 表示越好的校准。
    """
    if not probabilities or len(probabilities) != len(labels):
        return None

    bins = reliability_diagram(probabilities, labels, n_bins=n_bins)
    if not bins:
        return None

    n = len(probabilities)
    ece = sum(b.count / n * b.calibration_error for b in bins)
    return round(ece, 6)


@dataclass
class CalibrationSeriesResult:
    """批量概率校准结果。"""

    probabilities: list[float]
    method: str
    backend_requested: str
    backend_used: str
    fallback_used: bool
    fallback_reason: str | None = None
    cv_folds: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "probabilities": [round(_clamp(value), 6) for value in self.probabilities],
            "method": self.method,
            "backend_requested": self.backend_requested,
            "backend_used": self.backend_used,
            "fallback_used": self.fallback_used,
            "fallback_reason": self.fallback_reason,
            "cv_folds": self.cv_folds,
        }


def calibrate_probability_series(
    probabilities: list[float],
    labels: list[float],
    *,
    raw_scores: list[float] | None = None,
    method: Literal["auto", "sigmoid", "isotonic", "raw"] = "auto",
    prefer_sklearn: bool = True,
    cv: int = 3,
) -> CalibrationSeriesResult:
    """使用生产主路径优先、轻量实现兜底的批量概率校准。"""
    probs = [_clamp(float(item)) for item in list(probabilities or [])]
    ys = [float(item) for item in list(labels or [])]
    scores = [float(item) for item in list(raw_scores or probs)]
    if not probs or len(probs) != len(ys) or len(scores) != len(ys):
        return CalibrationSeriesResult(
            probabilities=probs,
            method="raw",
            backend_requested="none",
            backend_used="raw",
            fallback_used=False,
        )

    if method == "raw":
        return CalibrationSeriesResult(
            probabilities=probs,
            method="raw",
            backend_requested="raw",
            backend_used="raw",
            fallback_used=False,
        )

    normalized_method = "sigmoid" if method == "auto" else str(method or "sigmoid").strip().lower()
    if normalized_method not in {"sigmoid", "isotonic"}:
        normalized_method = "sigmoid"

    if prefer_sklearn:
        # F-N43-3 fix (诊断报告 §N43): 旧实现用 CalibratedClassifierCV + 自定义假估计器
        # (_RawScoreEstimator)，在 sklearn>=1.6 的 tag 体系下被判为 regressor 而恒抛
        # ValueError，导致 platt/isotonic 每次都静默降级 builtin。
        # 修复: 后验校准已持有 (score -> label) 对，直接拟合 LogisticRegression(Platt/sigmoid)
        # 或 IsotonicRegression，这是标准且稳健的 post-hoc 校准方式，不再依赖 CV 折叠假估计器。
        try:
            import numpy as _np

            class_values = [int(round(item)) for item in ys]
            if len(set(class_values)) >= 2:
                X = _np.asarray(scores, dtype=float).reshape(-1, 1)
                y = _np.asarray(class_values, dtype=int)
                if normalized_method == "isotonic":
                    from sklearn.isotonic import IsotonicRegression

                    iso = IsotonicRegression(out_of_bounds="clip", y_min=0.0, y_max=1.0)
                    calibrated = iso.fit_transform(
                        _np.asarray(scores, dtype=float), y.astype(float)
                    ).tolist()
                    backend_name = "sklearn_isotonic_regression"
                else:
                    from sklearn.linear_model import LogisticRegression

                    # C 很大 ≈ 无正则，等价于经典 Platt scaling
                    lr = LogisticRegression(C=1e6, solver="lbfgs", max_iter=1000)
                    lr.fit(X, y)
                    calibrated = lr.predict_proba(X)[:, 1].tolist()
                    backend_name = "sklearn_logistic_platt"
                return CalibrationSeriesResult(
                    probabilities=[_clamp(value) for value in calibrated],
                    method=normalized_method,
                    backend_requested=backend_name,
                    backend_used=backend_name,
                    fallback_used=False,
                    cv_folds=None,
                )
            else:
                fallback_reason = "sklearn_calibration_insufficient_class_support"
        except Exception as exc:
            fallback_reason = f"sklearn_calibration_failed:{type(exc).__name__}"
    else:
        fallback_reason = "sklearn_disabled"

    if normalized_method == "isotonic":
        table = fit_isotonic_table(scores, ys)
        calibrated = [isotonic_calibrate(score, table) for score in scores]
    else:
        a, b = fit_platt_params(scores, ys)
        calibrated = [platt_scale(score, a=a, b=b) for score in scores]

    return CalibrationSeriesResult(
        probabilities=[_clamp(value) for value in calibrated],
        method=normalized_method,
        backend_requested="sklearn_calibrated_classifier_cv",
        backend_used="builtin_lightweight",
        fallback_used=True,
        fallback_reason=fallback_reason,
    )


# ── Prediction Interval ───────────────────────────────────────────────────────

@dataclass
class PredictionInterval:
    """预测区间（Prediction Interval）。

    基于 calibrated_probability 和不确定性估计，
    提供 [lower, upper] 区间与 coverage 目标。
    """

    point_estimate: float
    lower: float
    upper: float
    coverage_target: float  # 如 0.90 表示 90% 置信区间
    interval_width: float
    method: str  # 'normal_approx' / 'isotonic_band' / 'empirical'
    sample_size: int
    calibrated: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "point_estimate": round(self.point_estimate, 4),
            "lower": round(self.lower, 4),
            "upper": round(self.upper, 4),
            "coverage_target": self.coverage_target,
            "interval_width": round(self.interval_width, 4),
            "method": self.method,
            "sample_size": self.sample_size,
            "calibrated": self.calibrated,
        }


def estimate_prediction_interval(
    calibrated_probability: float,
    sample_size: int = 50,
    coverage_target: float = 0.90,
    method: Literal["normal_approx", "wilson"] = "wilson",
    calibrated: bool = True,
) -> PredictionInterval:
    """基于校准概率估算预测区间。

    Parameters
    ----------
    calibrated_probability:
        已校准的预测概率 [0, 1]。
    sample_size:
        估计误差时使用的有效样本量（越大区间越窄）。
    coverage_target:
        置信水平（如 0.90 = 90% 覆盖率）。
    method:
        区间计算方法：
        - "normal_approx"：正态近似（大样本适用）
        - "wilson"：Wilson 区间（小样本更稳健）
    calibrated:
        是否已经过概率校准。

    Returns
    -------
    PredictionInterval 实例。
    """
    p = _clamp(calibrated_probability)
    n = max(1, int(sample_size))

    # z 值（近似）
    z_map = {0.90: 1.645, 0.95: 1.960, 0.99: 2.576}
    z = z_map.get(coverage_target, 1.960)

    if method == "wilson":
        denom = 1 + z ** 2 / n
        center = (p + z ** 2 / (2 * n)) / denom
        half = z * math.sqrt(p * (1 - p) / n + z ** 2 / (4 * n ** 2)) / denom
        lower = _clamp(center - half)
        upper = _clamp(center + half)
    else:
        # 正态近似
        half = z * math.sqrt(p * (1 - p) / n) if n > 0 else 0.0
        lower = _clamp(p - half)
        upper = _clamp(p + half)

    return PredictionInterval(
        point_estimate=p,
        lower=lower,
        upper=upper,
        coverage_target=coverage_target,
        interval_width=round(upper - lower, 4),
        method=method,
        sample_size=n,
        calibrated=calibrated,
    )


# ── 综合校准质量报告 ──────────────────────────────────────────────────────────

@dataclass
class CalibrationQualityReport:
    """综合校准质量报告，用于 prediction_quality 字段输出。"""

    brier_score: float | None
    ece: float | None
    calibration_bins: list[dict[str, Any]]
    sample_size: int
    calibration_method: str  # 'platt' / 'isotonic' / 'raw' / 'none'
    calibration_version: str
    quality_band: str  # 'good' / 'fair' / 'poor' / 'unknown'
    calibration_backend: str = "builtin_lightweight"
    backend_requested: str = "builtin_lightweight"
    backend_used: str = "builtin_lightweight"
    fallback_used: bool = False
    fallback_reason: str | None = None
    cv_folds: int | None = None
    notes: list[str] = field(default_factory=list)

    @staticmethod
    def _quality_band(brier: float | None, ece: float | None) -> str:
        if brier is None and ece is None:
            return "unknown"
        score = 0
        if brier is not None:
            if brier < 0.05:
                score += 2
            elif brier < 0.15:
                score += 1
        if ece is not None:
            if ece < 0.03:
                score += 2
            elif ece < 0.08:
                score += 1
        if score >= 3:
            return "good"
        if score >= 1:
            return "fair"
        return "poor"

    def to_dict(self) -> dict[str, Any]:
        return {
            "brier_score": self.brier_score,
            "ece": self.ece,
            "calibration_bins": self.calibration_bins,
            "sample_size": self.sample_size,
            "calibration_method": self.calibration_method,
            "calibration_version": self.calibration_version,
            "calibration_backend": self.calibration_backend,
            "backend_requested": self.backend_requested,
            "backend_used": self.backend_used,
            "fallback_used": self.fallback_used,
            "fallback_reason": self.fallback_reason,
            "cv_folds": self.cv_folds,
            "quality_band": self.quality_band,
            "notes": self.notes,
        }


def build_calibration_quality_report(
    probabilities: list[float],
    labels: list[float],
    calibration_method: str = "raw",
    calibration_version: str = "v0",
    calibration_backend: str = "builtin_lightweight",
    backend_requested: str = "builtin_lightweight",
    backend_used: str = "builtin_lightweight",
    fallback_used: bool = False,
    fallback_reason: str | None = None,
    cv_folds: int | None = None,
    n_bins: int = 10,
) -> CalibrationQualityReport:
    """根据预测概率和实际标签构建校准质量报告。

    Parameters
    ----------
    probabilities:
        预测概率列表。
    labels:
        二值标签列表（1 = 买入信号正确，0 = 错误）。
    calibration_method:
        所用校准方法。
    calibration_version:
        校准版本标识符。
    n_bins:
        可靠性图分桶数。

    Returns
    -------
    CalibrationQualityReport 实例。
    """
    bs = brier_score(probabilities, labels)
    ece = expected_calibration_error(probabilities, labels, n_bins=n_bins)
    bins = reliability_diagram(probabilities, labels, n_bins=n_bins)
    band = CalibrationQualityReport._quality_band(bs, ece)

    notes: list[str] = []
    if bs is not None and bs > 0.25:
        notes.append("Brier Score 偏高，预测概率与实际命中率偏差较大")
    if ece is not None and ece > 0.10:
        notes.append("ECE 超过 0.10，概率校准质量偏差，建议重新校准")
    if len(probabilities) < 30:
        notes.append(f"样本量仅 {len(probabilities)}，统计估计不稳定")
    if fallback_used and fallback_reason:
        notes.append(f"已降级到轻量校准路径: {fallback_reason}")

    return CalibrationQualityReport(
        brier_score=bs,
        ece=ece,
        calibration_bins=[b.to_dict() for b in bins],
        sample_size=len(probabilities),
        calibration_method=calibration_method,
        calibration_version=calibration_version,
        calibration_backend=calibration_backend,
        backend_requested=backend_requested,
        backend_used=backend_used,
        fallback_used=fallback_used,
        fallback_reason=fallback_reason,
        cv_folds=cv_folds,
        quality_band=band,
        notes=notes,
    )
