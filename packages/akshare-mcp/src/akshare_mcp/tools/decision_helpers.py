"""决策工具 — 纯数学/概率/技术辅助函数。"""

import math
import statistics
from datetime import date, datetime


def _maybe_float(value):
    try:
        if value is None or value == '':
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _clamp(value: float, lower: float, upper: float) -> float:
    return max(lower, min(upper, value))


def _estimate_volatility(closes: list[float], window: int = 20) -> float:
    if not closes or len(closes) < 3:
        return 0.0
    n = min(window, len(closes) - 1)
    rets = []
    for i in range(n):
        prev = float(closes[i + 1])
        curr = float(closes[i])
        if prev > 0:
            rets.append((curr - prev) / prev)
    if len(rets) < 2:
        return 0.0
    return float(statistics.pstdev(rets))


def _parse_loose_date(value) -> date | None:
    if value is None:
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    text = str(value or "").strip()
    if not text:
        return None
    for parser in (
        lambda item: date.fromisoformat(item[:10]),
        lambda item: datetime.fromisoformat(item.replace("Z", "+00:00")).date(),
    ):
        try:
            return parser(text)
        except Exception:
            continue
    digits = "".join(ch for ch in text if ch.isdigit())
    if len(digits) >= 8:
        try:
            return date.fromisoformat(f"{digits[:4]}-{digits[4:6]}-{digits[6:8]}")
        except Exception:
            return None
    return None


def _filter_klines_by_as_of(klines: list[dict], as_of: str) -> tuple[list[dict], dict]:
    rows = [dict(item) for item in list(klines or []) if isinstance(item, dict)]
    if not rows:
        return [], {
            "requested_as_of": as_of or "",
            "applied_as_of": None,
            "filtered_rows": 0,
            "dropped_future_rows": 0,
            "active": False,
        }

    rows.sort(
        key=lambda item: (
            _parse_loose_date(item.get("date") or item.get("trade_date") or item.get("timestamp")) or date.min,
            str(item.get("date") or item.get("trade_date") or item.get("timestamp") or ""),
        )
    )
    as_of_date = _parse_loose_date(as_of) if as_of else None
    if as_of_date is None:
        return rows, {
            "requested_as_of": as_of or "",
            "applied_as_of": None,
            "filtered_rows": len(rows),
            "dropped_future_rows": 0,
            "active": False,
        }

    filtered = []
    dropped = 0
    for row in rows:
        row_date = _parse_loose_date(row.get("date") or row.get("trade_date") or row.get("timestamp"))
        if row_date is None or row_date <= as_of_date:
            filtered.append(row)
        else:
            dropped += 1
    return filtered, {
        "requested_as_of": as_of,
        "applied_as_of": as_of_date.isoformat(),
        "filtered_rows": len(filtered),
        "dropped_future_rows": dropped,
        "active": True,
    }


def _calibrate_buy_probability(
    score: float,
    confidence: float,
    style: str,
    volatility: float,
    *,
    historical_calibration: dict | None = None,
) -> float:
    """将 score/confidence/波动率压缩为 [0,1] 的买入概率。

    P2-4.3.1 enhancement(诊断报告 §4.3.1):
    若 historical_calibration 提供(从 db 读取的过去 N 次预测 vs 实际命中率),
    则用 sklearn isotonic regression(若装了)做真实校准修正。
    无 sklearn 时退回 logit baseline。
    """
    style_bias = {
        "aggressive": 0.15,
        "balanced": 0.0,
        "conservative": -0.15,
    }.get(style, 0.0)

    # baseline logit 计算(向后兼容)
    score_term = (float(score) - 60.0) / 12.0
    confidence_term = (float(confidence) - 60.0) / 25.0
    vol_term = float(volatility) * 18.0
    logit = score_term + 0.7 * confidence_term - vol_term + style_bias

    logit = _clamp(logit, -30.0, 30.0)
    raw_prob = 1.0 / (1.0 + math.exp(-logit))

    # P2-4.3.1: 历史校准修正(若可用)
    if historical_calibration:
        calibrated = _apply_isotonic_calibration(raw_prob, historical_calibration)
        if calibrated is not None:
            return float(_clamp(calibrated, 0.0, 1.0))

    return float(_clamp(raw_prob, 0.0, 1.0))


def _apply_isotonic_calibration(raw_prob: float, history: dict) -> float | None:
    """P2-4.3.1: 使用 sklearn isotonic regression 校准 raw_prob。

    Args:
        raw_prob: logit baseline 输出的 [0,1] 概率
        history: dict 含 'predicted_probs' (list[float]) + 'actual_hit_rates' (list[0/1])

    Returns:
        校准后 [0,1],或 None(sklearn 不可用 / 历史样本不足)
    """
    try:
        from sklearn.isotonic import IsotonicRegression
    except ImportError:
        return None

    pred = history.get("predicted_probs") or []
    actual = history.get("actual_hit_rates") or []
    if not pred or len(pred) != len(actual) or len(pred) < 30:
        return None  # 样本太少,不做校准

    try:
        iso = IsotonicRegression(out_of_bounds="clip", y_min=0.0, y_max=1.0)
        iso.fit(pred, actual)
        calibrated = iso.predict([float(raw_prob)])
        if calibrated is not None and len(calibrated) > 0:
            return float(calibrated[0])
    except Exception:
        return None
    return None



def _estimate_target_price(
    recommendation: str,
    current_price: float,
    pe: float,
    score: float,
    industry_peer_pes: list[float] | None = None,
) -> tuple[float | None, str, float | None]:
    """目标价估算：优先行业中位数 PE 法，缺失时回退旧口径。"""
    if recommendation != 'buy':
        return None, 'none', None

    if not pe or pe <= 0:
        return float(current_price) * 1.15, 'fixed_gain_fallback', None

    eps = float(current_price) / float(pe)
    peers = [float(x) for x in (industry_peer_pes or []) if x and 0 < float(x) < 80]
    if len(peers) >= 3:
        industry_median_pe = float(statistics.median(peers))
        # 给行业估值法留 10% 上行空间，避免过于保守
        target_price = eps * industry_median_pe * 1.10
        return float(target_price), 'industry_median_pe', industry_median_pe

    pe_expansion = 1.0 + min(float(score), 100.0) / 500.0
    return float(eps * float(pe) * pe_expansion), 'pe_expansion_fallback', None




def _compute_rsi_from_window(window_prices: list[float]) -> float:
    if len(window_prices) < 2:
        return 50.0
    gains = 0.0
    losses = 0.0
    for i in range(1, len(window_prices)):
        change = float(window_prices[i] - window_prices[i - 1])
        if change > 0:
            gains += change
        else:
            losses -= change
    period = max(1, len(window_prices) - 1)
    avg_gain = gains / period
    avg_loss = losses / period
    if avg_loss <= 1e-12:
        return 100.0
    rs = avg_gain / avg_loss
    return float(100.0 - (100.0 / (1.0 + rs)))


def _build_threshold_backtest(
    closes: list[float],
    thresholds: list[int],
    horizon: int = 10,
) -> list[dict]:
    points = _collect_threshold_backtest_points(closes, horizon=horizon)
    if not points:
        return []

    reports: list[dict] = []
    for threshold in thresholds:
        subset = [p for p in points if p[0] >= float(threshold)]
        sample_count = len(subset)
        # P2-4.3.2 fix(诊断报告 §4.3.2):sample_count<10 时 emit warning
        # 历史问题:threshold=80 sample=3 严重不足,但 hit_rate=0% 当作"严格阈值不收敛"误导 AI
        warnings: list[str] = []
        if 0 < sample_count < 10:
            warnings.append(f"insufficient_sample_count={sample_count} (<10), hit_rate unreliable")
        if sample_count == 0:
            reports.append(
                {
                    "threshold": int(threshold),
                    "sample_count": 0,
                    "hit_rate": None,
                    "avg_forward_return": None,
                    "warnings": ["zero_sample_no_inference"],
                    "reliable": False,
                }
            )
            continue
        win_count = sum(1 for _, r in subset if r > 0)
        avg_ret = sum(r for _, r in subset) / sample_count
        reports.append(
            {
                "threshold": int(threshold),
                "sample_count": int(sample_count),
                "hit_rate": float(win_count / sample_count),
                "avg_forward_return": float(avg_ret),
                "warnings": warnings,
                "reliable": sample_count >= 10,
            }
        )
    # P2-4.3.2 fix:整体反向矛盾检测
    # 期望:严格阈值 (80) 应有 ≥ 宽松阈值 (40) 的 hit_rate
    # 实测反向 → emit threshold_inversion warning
    reliable_reports = [r for r in reports if r.get("reliable")]
    if len(reliable_reports) >= 2:
        sorted_by_threshold = sorted(reliable_reports, key=lambda r: r["threshold"])
        rates = [r["hit_rate"] for r in sorted_by_threshold]
        is_inverted = all(rates[i] >= rates[i + 1] - 0.05 for i in range(len(rates) - 1)) is False
        # 上面 logic 简化,实际看是否有 strict_threshold_hit < lenient_threshold_hit
        for i in range(len(sorted_by_threshold) - 1):
            r_lenient = sorted_by_threshold[i]
            r_strict = sorted_by_threshold[i + 1]
            if r_strict["hit_rate"] < r_lenient["hit_rate"] - 0.05:
                # 在 strict report 上加 inversion warning
                r_strict.setdefault("warnings", []).append(
                    f"threshold_inversion: stricter threshold {r_strict['threshold']} "
                    f"hit_rate={r_strict['hit_rate']:.3f} < lenient {r_lenient['threshold']} "
                    f"hit_rate={r_lenient['hit_rate']:.3f}"
                )
    return reports


def _collect_threshold_backtest_points(
    closes: list[float],
    horizon: int = 10,
) -> list[tuple[float, float]]:
    if not closes or len(closes) < 80:
        return []
    closes = [float(c) for c in closes]
    points: list[tuple[float, float]] = []
    start_idx = 30
    end_idx = len(closes) - int(horizon) - 1
    if end_idx <= start_idx:
        return []

    for idx in range(start_idx, end_idx + 1):
        price_now = closes[idx]
        if price_now <= 0:
            continue

        ma10 = sum(closes[idx - 9: idx + 1]) / 10.0
        ma30 = sum(closes[idx - 29: idx + 1]) / 30.0
        base10 = closes[idx - 10]
        momentum10 = ((price_now - base10) / base10) if base10 > 0 else 0.0
        rsi14 = _compute_rsi_from_window(closes[idx - 14: idx + 1])

        score = 50.0
        if momentum10 > 0.05:
            score += 15.0
        elif momentum10 < -0.05:
            score -= 15.0

        if price_now > ma10 > ma30:
            score += 20.0
        elif price_now < ma10 < ma30:
            score -= 20.0

        if rsi14 < 30.0:
            score += 15.0
        elif rsi14 > 70.0:
            score -= 15.0

        score = _clamp(score, 0.0, 100.0)
        price_future = closes[idx + horizon]
        forward_return = ((price_future - price_now) / price_now) if price_now > 0 else 0.0
        points.append((float(score), float(forward_return)))
    return points


def _build_probability_quality(
    *,
    current_score: float,
    buy_probability: float,
    selected_threshold: int,
    threshold_backtest: list[dict],
) -> dict:
    if not threshold_backtest:
        return {
            "quality": "low",
            "support_samples": 0,
            "sample_size": 0,
            "selected_threshold": int(selected_threshold),
            "empirical_hit_rate": None,
            "empirical_avg_forward_return": None,
            "calibration_gap": None,
            "calibration_bucket": None,
            "ece": None,
            "brier_score": None,
            "method": "threshold_backtest_proxy",
        }

    selected = None
    eligible = [
        row for row in threshold_backtest
        if row.get("sample_count") and int(row.get("threshold", 0)) <= float(current_score)
    ]
    if eligible:
        selected = max(eligible, key=lambda row: int(row.get("threshold", 0)))
    else:
        selected = min(threshold_backtest, key=lambda row: abs(int(row.get("threshold", 0)) - int(selected_threshold)))

    hit_rate = _maybe_float(selected.get("hit_rate"))
    avg_return = _maybe_float(selected.get("avg_forward_return"))
    support = int(selected.get("sample_count") or 0)
    calibration_gap = round(float(buy_probability - hit_rate), 4) if hit_rate is not None else None
    quality = "low"
    if support >= 30 and calibration_gap is not None and abs(calibration_gap) <= 0.08:
        quality = "high"
    elif support >= 15 and calibration_gap is not None and abs(calibration_gap) <= 0.15:
        quality = "medium"

    # 概率校准桶（Reliability Diagram 单桶）
    calibration_bucket = None
    if hit_rate is not None and support > 0:
        calibration_bucket = {
            "mean_predicted": round(float(buy_probability), 4),
            "mean_actual": round(float(hit_rate), 4),
            "count": support,
            "calibration_error": abs(calibration_gap) if calibration_gap is not None else None,
        }

    # ECE 近似（单桶简化）
    ece = None
    if calibration_gap is not None and support > 0:
        # 单桶 ECE = (count/total) * |gap|，此处 count=total，ECE = |gap|
        ece = round(abs(calibration_gap), 6)

    # Brier Score 近似（单样本：用 hit_rate 作为经验分布）
    brier_score_val = None
    if hit_rate is not None:
        p = max(0.0, min(1.0, float(buy_probability)))
        y = max(0.0, min(1.0, float(hit_rate)))
        brier_score_val = round((p - y) ** 2, 6)

    return {
        "quality": quality,
        "support_samples": support,
        "sample_size": support,
        "selected_threshold": int(selected.get("threshold", selected_threshold) or selected_threshold),
        "empirical_hit_rate": round(float(hit_rate), 4) if hit_rate is not None else None,
        "empirical_avg_forward_return": round(float(avg_return), 4) if avg_return is not None else None,
        "calibration_gap": calibration_gap,
        "calibration_bucket": calibration_bucket,
        "ece": ece,
        "brier_score": brier_score_val,
        "method": "threshold_backtest_proxy",
    }


def _build_prediction_interval(
    *,
    closes: list[float],
    current_score: float,
    thresholds: list[int],
    horizon: int = 10,
    confidence: float = 0.8,
) -> dict | None:
    points = _collect_threshold_backtest_points(closes, horizon=horizon)
    if not points:
        return None

    threshold = min(thresholds) if thresholds else 40
    eligible = [int(t) for t in (thresholds or []) if float(current_score) >= int(t)]
    if eligible:
        threshold = max(eligible)

    subset = [ret for score, ret in points if score >= float(threshold)]
    if len(subset) < 10:
        subset = [ret for _, ret in points]
        threshold = 0
    if len(subset) < 10:
        return None

    ordered = sorted(float(x) for x in subset)
    alpha = max(0.01, min(0.49, (1.0 - float(confidence)) / 2.0))
    lower_idx = int(math.floor(alpha * (len(ordered) - 1)))
    upper_idx = int(math.ceil((1.0 - alpha) * (len(ordered) - 1)))
    lower = ordered[max(0, min(len(ordered) - 1, lower_idx))]
    upper = ordered[max(0, min(len(ordered) - 1, upper_idx))]
    median = ordered[len(ordered) // 2]
    hit_rate = sum(1 for item in ordered if item > 0) / len(ordered)
    mean_val = sum(ordered) / len(ordered)
    # interval_width = upper - lower（预测区间宽度）
    interval_width = round(float(upper) - float(lower), 4)
    # observed_coverage = 实际在区间内的样本比例（用历史数据估计）
    in_interval = sum(1 for item in ordered if lower <= item <= upper)
    observed_coverage = round(in_interval / len(ordered), 4) if ordered else float(confidence)
    return {
        "horizon_days": int(horizon),
        "confidence_level": round(float(confidence), 4),
        "threshold_used": int(threshold),
        "sample_count": len(ordered),
        "expected_return": round(float(mean_val), 4),
        "median_return": round(float(median), 4),
        "lower_return": round(float(lower), 4),
        "upper_return": round(float(upper), 4),
        "interval_width": interval_width,
        "observed_coverage": observed_coverage,
        "hit_rate": round(float(hit_rate), 4),
        "coverage_proxy": round(float(confidence), 4),
        "method": "historical_forward_return_quantiles",
    }


def _context_section(analysis_context: dict | None, name: str) -> dict:
    if not isinstance(analysis_context, dict):
        return {}
    value = analysis_context.get(name, {})
    return value if isinstance(value, dict) else {}


def _derive_contextual_decision(analysis_context: dict | None) -> dict:
    valuation = _context_section(analysis_context, "valuation")
    fundamentals = _context_section(analysis_context, "fundamentals")
    technical = _context_section(analysis_context, "technical")
    momentum = _context_section(analysis_context, "momentum")
    risk = _context_section(analysis_context, "risk")

    positives: list[str] = []
    negatives: list[str] = []
    pe = _maybe_float(valuation.get("pe"))
    industry_gap = _maybe_float(valuation.get("industry_relative_pe"))
    if pe is not None and 0 < pe < 20:
        positives.append(f"估值处于可接受区间(PE={pe:.1f})")
    if industry_gap is not None and industry_gap < 0.9:
        positives.append("相对行业估值偏低")
    elif industry_gap is not None and industry_gap > 1.2:
        negatives.append("相对行业估值偏高")

    roe = _maybe_float(fundamentals.get("roe"))
    revenue_yoy = _maybe_float(fundamentals.get("revenue_yoy"))
    debt_ratio = _maybe_float(fundamentals.get("debt_ratio"))
    if roe is not None and roe >= 12:
        positives.append(f"盈利能力良好(ROE={roe:.1f})")
    if revenue_yoy is not None and revenue_yoy >= 10:
        positives.append(f"营收保持增长({revenue_yoy:.1f}%)")
    if debt_ratio is not None and debt_ratio >= 70:
        negatives.append(f"负债率偏高({debt_ratio:.1f}%)")

    rsi = _maybe_float(technical.get("rsi_14"))
    ma_alignment = technical.get("ma_alignment")
    macd_hist = _maybe_float(technical.get("macd_hist"))
    if rsi is not None and rsi < 35:
        positives.append(f"RSI接近超卖({rsi:.1f})")
    elif rsi is not None and rsi > 75:
        negatives.append(f"RSI偏热({rsi:.1f})")
    if ma_alignment == "bullish":
        positives.append("均线呈多头排列")
    elif ma_alignment == "bearish":
        negatives.append("均线呈空头排列")
    if macd_hist is not None and macd_hist < 0:
        negatives.append("MACD柱线仍在零轴下方")

    mom_20d = _maybe_float(momentum.get("mom_20d"))
    market_regime = momentum.get("market_regime")
    if mom_20d is not None and mom_20d > 0.05:
        positives.append("20日动量为正")
    elif mom_20d is not None and mom_20d < -0.05:
        negatives.append("20日动量偏弱")
    if market_regime == "bearish":
        negatives.append("当前处于偏弱市场环境")

    volatility = _maybe_float(risk.get("volatility_20d"))
    drawdown = _maybe_float(risk.get("max_drawdown_250d"))
    if volatility is not None and volatility > 0.04:
        negatives.append("短期波动率较高")
    if drawdown is not None and drawdown > 0.30:
        negatives.append("历史回撤偏大")

    if len(positives) >= 4 and len(negatives) <= 1:
        recommendation, action_text = "buy", "建议买入"
    elif len(negatives) >= 4:
        recommendation, action_text = "avoid", "建议回避"
    elif len(positives) >= len(negatives) and positives:
        recommendation, action_text = "hold", "可持有或逢低布局"
    else:
        recommendation, action_text = "wait", "建议继续观望"
    return {
        "recommendation": recommendation,
        "action_text": action_text,
        "positives": positives,
        "negatives": negatives,
    }
