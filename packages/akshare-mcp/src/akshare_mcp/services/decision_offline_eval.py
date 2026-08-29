"""统一决策离线评估基线（Decision Offline Evaluation Baseline）

为 buy / hold / sell 决策提供历史标签定义、分层命中率计算
和离线评估报告生成能力。

文档参考
--------
- scikit-learn Probability Calibration:
  https://scikit-learn.org/stable/modules/calibration.html
- Alphalens 分层回测原理:
  https://alphalens.ml4trading.io/notebooks/overview.html

核心概念
--------
- 标签定义：以 horizon 天后收益率是否超过阈值作为 buy/sell 标签
- 分桶分析：将预测概率分成 N 桶，分别统计各桶实际命中率
- 离线基线：给出"随机猜测"基线和"历史均值"基线的对比
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any


# ── 标签定义 ──────────────────────────────────────────────────────────────────

def define_buy_label(
    forward_return: float,
    threshold: float = 0.05,
) -> int:
    """定义买入标签。

    Parameters
    ----------
    forward_return:
        horizon 天后的收益率（如 0.05 = 5%）。
    threshold:
        买入判断阈值，默认 5%。

    Returns
    -------
    1 = 买入正确（forward_return > threshold）
    0 = 买入错误
    """
    return 1 if float(forward_return) > float(threshold) else 0


def define_sell_label(
    forward_return: float,
    threshold: float = -0.03,
) -> int:
    """定义卖出标签。

    Returns
    -------
    1 = 卖出正确（forward_return < threshold）
    0 = 卖出错误
    """
    return 1 if float(forward_return) < float(threshold) else 0


def define_hold_label(
    forward_return: float,
    buy_threshold: float = 0.05,
    sell_threshold: float = -0.03,
) -> int:
    """定义持有标签（既没有强烈买入也没有强烈卖出信号时）。

    Returns
    -------
    1 = 持有正确（sell_threshold <= forward_return <= buy_threshold）
    0 = 持有错误
    """
    r = float(forward_return)
    return 1 if sell_threshold <= r <= buy_threshold else 0


# ── 分层命中率分析 ────────────────────────────────────────────────────────────

@dataclass
class ProbabilityBucketStat:
    """单个概率分桶统计。"""

    bucket_id: int
    prob_lower: float
    prob_upper: float
    count: int
    hit_count: int
    hit_rate: float          # 实际命中率（buy = 涨幅超阈值）
    mean_probability: float  # 桶内平均预测概率
    calibration_gap: float   # |mean_probability - hit_rate|

    def to_dict(self) -> dict[str, Any]:
        return {
            "bucket": f"[{self.prob_lower:.1f},{self.prob_upper:.1f})",
            "count": self.count,
            "hit_count": self.hit_count,
            "hit_rate": round(self.hit_rate, 4),
            "mean_probability": round(self.mean_probability, 4),
            "calibration_gap": round(self.calibration_gap, 4),
        }


def layered_hit_rate_analysis(
    probabilities: list[float],
    labels: list[int],
    n_buckets: int = 5,
) -> list[ProbabilityBucketStat]:
    """分层命中率分析。

    将预测概率分成 n_buckets 个桶，分析各桶的实际命中率，
    用于验证"高概率桶是否真的有更高命中率"。

    Parameters
    ----------
    probabilities:
        预测概率列表（如 buy_probability）。
    labels:
        实际标签列表（1 = 命中，0 = 未命中）。
    n_buckets:
        分桶数量（默认 5 个 20% 桶）。

    Returns
    -------
    ProbabilityBucketStat 列表，按 prob_lower 升序。
    """
    if len(probabilities) != len(labels) or not probabilities:
        return []

    bucket_size = 1.0 / n_buckets
    results: list[ProbabilityBucketStat] = []

    for i in range(n_buckets):
        lo = i * bucket_size
        hi = lo + bucket_size
        bucket_probs = []
        bucket_labels = []

        for p, y in zip(probabilities, labels):
            if lo <= p < hi or (i == n_buckets - 1 and p == 1.0):
                bucket_probs.append(p)
                bucket_labels.append(y)

        if not bucket_probs:
            continue

        hit_count = sum(bucket_labels)
        hit_rate = hit_count / len(bucket_labels)
        mean_prob = sum(bucket_probs) / len(bucket_probs)

        results.append(ProbabilityBucketStat(
            bucket_id=i + 1,
            prob_lower=lo,
            prob_upper=hi,
            count=len(bucket_probs),
            hit_count=hit_count,
            hit_rate=hit_rate,
            mean_probability=mean_prob,
            calibration_gap=abs(mean_prob - hit_rate),
        ))

    return results


# ── 单调性检验 ────────────────────────────────────────────────────────────────

def monotonicity_score(
    bucket_stats: list[ProbabilityBucketStat],
) -> float:
    """计算分桶命中率的单调性分数（0~1）。

    好的概率预测应满足：越高概率的桶，实际命中率也越高。

    Returns
    -------
    1.0 = 完全单调递增
    0.0 = 完全无序
    """
    hit_rates = [s.hit_rate for s in sorted(bucket_stats, key=lambda x: x.prob_lower)]
    if len(hit_rates) < 2:
        return 1.0
    n_pairs = len(hit_rates) - 1
    concordant = sum(
        1 for i in range(n_pairs) if hit_rates[i + 1] >= hit_rates[i]
    )
    return round(concordant / n_pairs, 4)


# ── 离线评估报告 ──────────────────────────────────────────────────────────────

@dataclass
class DecisionOfflineEvalReport:
    """决策离线评估完整报告。"""

    decision_type: str      # 'buy' / 'sell' / 'hold'
    horizon_days: int       # 预测窗口（天）
    sample_size: int
    hit_count: int
    overall_hit_rate: float
    random_baseline: float  # 随机猜测基线
    historical_mean_baseline: float  # 历史均值基线
    lift_vs_random: float   # 相对随机基线的提升
    lift_vs_historical: float  # 相对历史均值的提升
    monotonicity_score: float
    bucket_stats: list[dict[str, Any]] = field(default_factory=list)
    brier_score: float | None = None
    ece: float | None = None
    quality_band: str = "unknown"  # 'good' / 'fair' / 'poor' / 'unknown'
    notes: list[str] = field(default_factory=list)
    evaluation_version: str = "v1"

    def to_dict(self) -> dict[str, Any]:
        return {
            "decision_type": self.decision_type,
            "horizon_days": self.horizon_days,
            "sample_size": self.sample_size,
            "hit_count": self.hit_count,
            "overall_hit_rate": round(self.overall_hit_rate, 4),
            "random_baseline": round(self.random_baseline, 4),
            "historical_mean_baseline": round(self.historical_mean_baseline, 4),
            "lift_vs_random": round(self.lift_vs_random, 4),
            "lift_vs_historical": round(self.lift_vs_historical, 4),
            "monotonicity_score": self.monotonicity_score,
            "bucket_stats": self.bucket_stats,
            "brier_score": self.brier_score,
            "ece": self.ece,
            "quality_band": self.quality_band,
            "notes": self.notes,
            "evaluation_version": self.evaluation_version,
        }


def build_decision_offline_eval(
    probabilities: list[float],
    forward_returns: list[float],
    decision_type: str = "buy",
    horizon_days: int = 5,
    buy_threshold: float = 0.05,
    sell_threshold: float = -0.03,
    historical_positive_rate: float = 0.45,
    n_buckets: int = 5,
) -> DecisionOfflineEvalReport:
    """构建决策离线评估报告。

    Parameters
    ----------
    probabilities:
        模型预测的 buy/sell/hold 概率列表。
    forward_returns:
        对应的实际 horizon 天后收益率列表。
    decision_type:
        决策类型 'buy' / 'sell' / 'hold'。
    horizon_days:
        预测窗口（天），用于标注 horizon。
    buy_threshold:
        买入正确的收益率阈值（默认 5%）。
    sell_threshold:
        卖出正确的收益率阈值（默认 -3%）。
    historical_positive_rate:
        历史上正样本（上涨）的基准比例（约 45% 适合 A 股）。
    n_buckets:
        分层分析的桶数。

    Returns
    -------
    DecisionOfflineEvalReport 实例。
    """
    # 生成标签
    if decision_type == "buy":
        labels = [define_buy_label(r, threshold=buy_threshold) for r in forward_returns]
    elif decision_type == "sell":
        labels = [define_sell_label(r, threshold=sell_threshold) for r in forward_returns]
    else:  # hold
        labels = [define_hold_label(r, buy_threshold=buy_threshold, sell_threshold=sell_threshold)
                  for r in forward_returns]

    n = len(labels)
    if n == 0:
        return DecisionOfflineEvalReport(
            decision_type=decision_type,
            horizon_days=horizon_days,
            sample_size=0,
            hit_count=0,
            overall_hit_rate=0.0,
            random_baseline=0.5,
            historical_mean_baseline=historical_positive_rate,
            lift_vs_random=0.0,
            lift_vs_historical=0.0,
            monotonicity_score=0.0,
            notes=["样本量为 0，无法评估"],
        )

    hit_count = sum(labels)
    hit_rate = hit_count / n
    random_base = 0.5  # 随机猜测基线
    hist_base = historical_positive_rate

    lift_vs_random = (hit_rate - random_base) / random_base if random_base > 0 else 0.0
    lift_vs_historical = (hit_rate - hist_base) / hist_base if hist_base > 0 else 0.0

    # 分层命中率
    bucket_stats_raw = layered_hit_rate_analysis(probabilities, labels, n_buckets=n_buckets)
    mono_score = monotonicity_score(bucket_stats_raw)

    # Brier Score
    bs: float | None = None
    ece: float | None = None
    if probabilities:
        bs = round(sum((p - y) ** 2 for p, y in zip(probabilities, labels)) / n, 6)
        # 简化 ECE
        bucket_size = 1.0 / 10
        ece_sum = 0.0
        for i in range(10):
            lo = i * bucket_size
            hi = lo + bucket_size
            bp = [(p, y) for p, y in zip(probabilities, labels) if lo <= p < hi or (i == 9 and p == 1.0)]
            if bp:
                mp = sum(x[0] for x in bp) / len(bp)
                ma = sum(x[1] for x in bp) / len(bp)
                ece_sum += len(bp) / n * abs(mp - ma)
        ece = round(ece_sum, 6)

    # 质量评级
    quality = "unknown"
    if bs is not None:
        if bs < 0.10 and mono_score >= 0.8:
            quality = "good"
        elif bs < 0.20:
            quality = "fair"
        else:
            quality = "poor"

    notes: list[str] = []
    if n < 50:
        notes.append(f"样本量仅 {n}，统计估计不稳定，建议扩充至 100+ 条")
    if lift_vs_random < 0:
        notes.append("当前模型表现低于随机基线，需重新审视特征与模型")
    if mono_score < 0.6:
        notes.append("概率分桶命中率缺乏单调性，概率校准质量需改善")
    if bs is not None and bs > 0.25:
        notes.append(f"Brier Score {bs:.3f} 偏高，建议进行概率校准")

    return DecisionOfflineEvalReport(
        decision_type=decision_type,
        horizon_days=horizon_days,
        sample_size=n,
        hit_count=hit_count,
        overall_hit_rate=hit_rate,
        random_baseline=random_base,
        historical_mean_baseline=hist_base,
        lift_vs_random=round(lift_vs_random, 4),
        lift_vs_historical=round(lift_vs_historical, 4),
        monotonicity_score=mono_score,
        bucket_stats=[b.to_dict() for b in bucket_stats_raw],
        brier_score=bs,
        ece=ece,
        quality_band=quality,
        notes=notes,
    )


# ── 多决策类型联合报告 ────────────────────────────────────────────────────────

def build_multi_decision_eval(
    buy_probs: list[float],
    forward_returns: list[float],
    horizon_days: int = 5,
    buy_threshold: float = 0.05,
    sell_threshold: float = -0.03,
) -> dict[str, Any]:
    """同时评估 buy/hold/sell 三类决策，返回联合报告。"""
    buy_report = build_decision_offline_eval(
        buy_probs, forward_returns,
        decision_type="buy", horizon_days=horizon_days, buy_threshold=buy_threshold,
    )
    sell_report = build_decision_offline_eval(
        [1 - p for p in buy_probs], forward_returns,
        decision_type="sell", horizon_days=horizon_days, sell_threshold=sell_threshold,
    )
    hold_report = build_decision_offline_eval(
        [abs(p - 0.5) for p in buy_probs], forward_returns,
        decision_type="hold", horizon_days=horizon_days,
        buy_threshold=buy_threshold, sell_threshold=sell_threshold,
    )

    return {
        "horizon_days": horizon_days,
        "buy": buy_report.to_dict(),
        "sell": sell_report.to_dict(),
        "hold": hold_report.to_dict(),
        "summary": (
            f"buy 命中率 {buy_report.overall_hit_rate:.1%}（vs 随机 {buy_report.lift_vs_random:+.1%}），"
            f"sell 命中率 {sell_report.overall_hit_rate:.1%}（vs 随机 {sell_report.lift_vs_random:+.1%}）"
        ),
    }
