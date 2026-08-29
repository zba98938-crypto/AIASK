"""情绪分析服务 — 三分量复合评分"""
import math
from datetime import date, datetime
from typing import List, Dict, Any, Optional

import numpy as np

from .signal_quality_registry import get_default_signal_quality_registry

# ── 新闻情绪关键词库 ──
_BULLISH_KEYWORDS = [
    '利好', '大涨', '涨停', '突破', '新高', '放量', '加仓', '抄底',
    '回暖', '反弹', '景气', '超预期', '增持', '回购', '分红',
    '订单', '中标', '扩产', '盈利', '业绩增长',
]
_BEARISH_KEYWORDS = [
    '利空', '大跌', '跌停', '破位', '新低', '缩量', '减持', '清仓',
    '暴雷', '亏损', '退市', '违规', '处罚', '下调', '预警',
    '裁员', '诉讼', '暴跌', '崩盘', '爆仓',
]


class SentimentAnalyzer:
    @staticmethod
    def _score_bucket(score: float) -> str:
        if float(score) >= 70.0:
            return "bullish"
        if float(score) <= 30.0:
            return "bearish"
        return "neutral"

    @staticmethod
    def _summarize_forward_returns(values: list[float]) -> dict[str, float | int | None]:
        if not values:
            return {
                "samples": 0,
                "hit_rate": None,
                "avg_return": None,
                "median_return": None,
                "lower_return": None,
                "upper_return": None,
            }
        ordered = sorted(float(x) for x in values)
        lower_idx = int(max(0, math.floor(0.1 * (len(ordered) - 1))))
        upper_idx = int(max(0, math.ceil(0.9 * (len(ordered) - 1))))
        return {
            "samples": len(ordered),
            "hit_rate": round(float(np.mean(np.array(ordered) > 0)), 4),
            "avg_return": round(float(np.mean(ordered)), 4),
            "median_return": round(float(np.median(ordered)), 4),
            "lower_return": round(float(ordered[lower_idx]), 4),
            "upper_return": round(float(ordered[upper_idx]), 4),
        }

    @staticmethod
    def _parse_kline_date(value: Any) -> date | None:
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

    @classmethod
    def _sort_klines(cls, klines: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        return sorted(
            [dict(item) for item in list(klines or []) if isinstance(item, dict)],
            key=lambda item: (
                cls._parse_kline_date(item.get("date")) or date.min,
                str(item.get("date") or ""),
            ),
        )

    # ── 分量 1: 价量动量（40%权重） ──
    @classmethod
    def _price_momentum_score(cls, klines: List[Dict[str, Any]]) -> float:
        """基于价格变化和量比计算动量得分 0~100

        P3-5.19 fix: 整合 RSI 信号(诊断报告 §5.19)
        历史问题:sentiment.score 仅用价格变化+量比,与 should_i_buy/should_i_sell 内部 RSI 不一致
        修复:在 momentum 分量中加入 RSI 偏离 50 的信号(权重 30% 在 momentum 内部)
        """
        ordered_klines = cls._sort_klines(klines)
        if not ordered_klines or len(ordered_klines) < 20:
            return 50.0
        closes = [k['close'] for k in ordered_klines]
        volumes = [k['volume'] for k in ordered_klines]
        base_close = float(closes[-20] or 0.0)
        if abs(base_close) < 1e-9:
            return 50.0
        price_change = (closes[-1] - base_close) / base_close
        volume_ratio = np.mean(volumes[-5:]) / max(np.mean(volumes[-20:-5]), 1e-9)
        # P3-5.19: 简版 RSI(14) 计算,用于强化动量
        rsi_score_offset = 0.0
        if len(closes) >= 15:
            try:
                deltas = [float(closes[i]) - float(closes[i - 1]) for i in range(-14, 0)]
                gains = [d for d in deltas if d > 0]
                losses = [-d for d in deltas if d < 0]
                avg_gain = sum(gains) / 14 if gains else 0.0
                avg_loss = sum(losses) / 14 if losses else 1e-9
                rs = avg_gain / max(avg_loss, 1e-9)
                rsi = 100 - (100 / (1 + rs))
                # rsi 偏离 50: 70+ 偏多 / 30- 偏空,贡献 ±15 分
                if rsi >= 70:
                    rsi_score_offset = 15.0 * min((rsi - 50) / 50, 1.0)
                elif rsi <= 30:
                    rsi_score_offset = -15.0 * min((50 - rsi) / 50, 1.0)
                else:
                    rsi_score_offset = (rsi - 50) / 50 * 5.0
            except Exception:
                rsi_score_offset = 0.0
        score = 50 + price_change * 100 + (volume_ratio - 1) * 20 + rsi_score_offset
        return max(0.0, min(100.0, score))

    @classmethod
    def _build_price_momentum_validation(
        cls,
        ordered_klines: List[Dict[str, Any]],
        current_price_momentum_score: float,
        *,
        forward_days: tuple[int, ...] = (5, 10, 20),
    ) -> Dict[str, Any]:
        if len(ordered_klines) < 60:
            return {
                "available": False,
                "reason": "insufficient_kline",
                "method": "price_momentum_bucket_proxy",
            }

        closes = [float(item.get("close", 0) or 0) for item in ordered_klines]
        bucket = cls._score_bucket(current_price_momentum_score)
        max_forward = max(int(day) for day in forward_days)
        matched_scores = 0
        forward_map = {int(day): [] for day in forward_days}

        for idx in range(19, len(ordered_klines) - max_forward):
            price_now = closes[idx]
            if price_now <= 0:
                continue
            score = cls._price_momentum_score(ordered_klines[idx - 19: idx + 1])
            if cls._score_bucket(score) != bucket:
                continue
            matched_scores += 1
            for day in forward_days:
                future_idx = idx + int(day)
                if future_idx >= len(closes):
                    continue
                forward_map[int(day)].append((closes[future_idx] - price_now) / price_now)

        if matched_scores <= 0:
            return {
                "available": False,
                "reason": "no_bucket_matches",
                "method": "price_momentum_bucket_proxy",
                "bucket": bucket,
            }

        return {
            "available": True,
            "method": "price_momentum_bucket_proxy",
            "bucket": bucket,
            "proxy_component": "price_momentum",
            "sample_count": int(matched_scores),
            "forward_returns": {
                f"{int(day)}d": cls._summarize_forward_returns(values)
                for day, values in forward_map.items()
            },
        }

    @staticmethod
    def _classify_headline(title: str) -> str:
        """Classify a headline into bullish / neutral / bearish by keyword balance."""
        text = str(title or "").strip()
        if not text:
            return "neutral"

        bullish_hits = sum(1 for keyword in _BULLISH_KEYWORDS if keyword in text)
        bearish_hits = sum(1 for keyword in _BEARISH_KEYWORDS if keyword in text)

        if bullish_hits > bearish_hits:
            return "bullish"
        if bearish_hits > bullish_hits:
            return "bearish"
        return "neutral"

    @staticmethod
    def _empty_news_oos_bucket_stats() -> Dict[str, Dict[str, Dict[str, float | int | None]]]:
        return {
            bucket: {
                period_key: {
                    "samples": 0,
                    "hit_rate": None,
                    "avg_return": None,
                    "median_return": None,
                    "lower_return": None,
                    "upper_return": None,
                }
                for period_key in ("5d", "10d", "20d")
            }
            for bucket in ("bullish", "neutral", "bearish")
        }

    @classmethod
    def _build_news_sentiment_oos_validation(
        cls,
        klines: List[Dict[str, Any]],
        *,
        headline_labels: Optional[List[Dict[str, Any]]] = None,
        proxy_lookback: int = 5,
        forward_days: tuple[int, ...] = (5, 10, 20),
    ) -> Dict[str, Any]:
        ordered_klines = cls._sort_klines(klines)
        empty_bucket_stats = cls._empty_news_oos_bucket_stats()
        normalized_labels = [
            dict(item)
            for item in list(headline_labels or [])
            if isinstance(item, dict) and str(item.get("label") or "").strip()
        ]
        if ordered_klines and normalized_labels:
            closes = [float(item.get("close", 0) or 0) for item in ordered_klines]
            kline_dates = [cls._parse_kline_date(item.get("date")) for item in ordered_klines]
            date_to_index = {
                one_date: idx
                for idx, one_date in enumerate(kline_dates)
                if one_date is not None
            }
            bucket_forward_map: dict[str, dict[int, list[float]]] = {
                bucket: {int(day): [] for day in forward_days}
                for bucket in ("bullish", "neutral", "bearish")
            }
            max_forward = max(int(day) for day in forward_days)
            for label_row in normalized_labels:
                label = str(label_row.get("label") or "").strip().lower()
                if label not in bucket_forward_map:
                    continue
                published_at = (
                    label_row.get("published_at")
                    or label_row.get("date")
                    or ((label_row.get("payload") or {}).get("published_at") if isinstance(label_row.get("payload"), dict) else None)
                )
                signal_date = cls._parse_kline_date(published_at)
                if signal_date is None:
                    continue
                idx = date_to_index.get(signal_date)
                if idx is None:
                    future_candidates = [
                        position
                        for position, one_date in enumerate(kline_dates)
                        if one_date is not None and one_date >= signal_date
                    ]
                    if not future_candidates:
                        continue
                    idx = future_candidates[0]
                current_price = closes[idx]
                if current_price <= 0 or idx + max_forward >= len(closes):
                    continue
                for day in forward_days:
                    future_price = closes[idx + int(day)]
                    if future_price <= 0:
                        continue
                    bucket_forward_map[label][int(day)].append((future_price - current_price) / current_price)

            bucket_stats = {
                bucket: {
                    f"{int(day)}d": cls._summarize_forward_returns(values)
                    for day, values in day_map.items()
                }
                for bucket, day_map in bucket_forward_map.items()
            }

            def _avg(bucket: str, period_key: str) -> float | None:
                value = bucket_stats.get(bucket, {}).get(period_key, {}).get("avg_return")
                return float(value) if value is not None else None

            alpha_curve = {
                period_key: (
                    round(float(_avg("bullish", period_key) - _avg("bearish", period_key)), 4)
                    if _avg("bullish", period_key) is not None and _avg("bearish", period_key) is not None
                    else None
                )
                for period_key in ("5d", "10d", "20d")
            }
            alpha_5d = alpha_curve.get("5d")
            alpha_10d = alpha_curve.get("10d")
            alpha_20d = alpha_curve.get("20d")
            if alpha_5d is None:
                signal_stability = "unknown"
            elif (alpha_10d is None and alpha_20d is None) or (
                alpha_5d > 0 and (alpha_10d is None or alpha_10d >= 0) and (alpha_20d is None or alpha_20d >= 0)
            ):
                signal_stability = "stable"
            else:
                signal_stability = "degraded"
            if alpha_5d is None:
                decay_note = "alpha_unavailable"
            elif alpha_20d is None:
                decay_note = "long_horizon_insufficient_samples"
            elif alpha_20d >= alpha_5d:
                decay_note = "signal_persists_or_strengthens"
            elif alpha_20d >= 0:
                decay_note = "signal_decays_but_remains_positive"
            else:
                decay_note = "signal_reverses_on_longer_horizon"
            available = any(
                (bucket_stats.get(bucket, {}).get("5d", {}).get("samples") or 0) > 0
                for bucket in ("bullish", "neutral", "bearish")
            )
            if available:
                return {
                    "available": True,
                    "method": "headline_label_oos_v1",
                    "proxy_only": False,
                    "label_sample_count": len(normalized_labels),
                    "bucket_stats": bucket_stats,
                    "alpha_5d_bull_vs_bear": alpha_5d,
                    "signal_stability": signal_stability,
                    "decay_analysis": {
                        "decay_note": decay_note,
                        "alpha_curve": alpha_curve,
                    },
                }

        min_points = max(60, proxy_lookback + max(int(day) for day in forward_days) + 20)
        if len(ordered_klines) < min_points:
            return {
                "available": False,
                "reason": f"insufficient_kline:{len(ordered_klines)}<{min_points}",
                "method": "headline_label_oos_v1_fallback_proxy",
                "proxy_only": True,
                "fallback_reason": "insufficient_headline_labels",
                "bucket_stats": empty_bucket_stats,
                "alpha_5d_bull_vs_bear": None,
                "signal_stability": "unknown",
                "decay_analysis": {
                    "decay_note": "insufficient_history",
                    "alpha_curve": {},
                },
            }

        closes = [float(item.get("close", 0) or 0) for item in ordered_klines]
        bucket_forward_map: dict[str, dict[int, list[float]]] = {
            bucket: {int(day): [] for day in forward_days}
            for bucket in ("bullish", "neutral", "bearish")
        }

        max_forward = max(int(day) for day in forward_days)
        for idx in range(proxy_lookback, len(closes) - max_forward):
            base_price = closes[idx - proxy_lookback]
            current_price = closes[idx]
            if base_price <= 0 or current_price <= 0:
                continue

            recent_return = (current_price - base_price) / base_price
            if recent_return >= 0.02:
                bucket = "bullish"
            elif recent_return <= -0.02:
                bucket = "bearish"
            else:
                bucket = "neutral"

            for day in forward_days:
                future_price = closes[idx + int(day)]
                if future_price <= 0:
                    continue
                bucket_forward_map[bucket][int(day)].append((future_price - current_price) / current_price)

        bucket_stats = {
            bucket: {
                f"{int(day)}d": cls._summarize_forward_returns(values)
                for day, values in day_map.items()
            }
            for bucket, day_map in bucket_forward_map.items()
        }

        def _avg(bucket: str, period_key: str) -> float | None:
            value = bucket_stats.get(bucket, {}).get(period_key, {}).get("avg_return")
            return float(value) if value is not None else None

        alpha_curve = {
            period_key: (
                round(float(_avg("bullish", period_key) - _avg("bearish", period_key)), 4)
                if _avg("bullish", period_key) is not None and _avg("bearish", period_key) is not None
                else None
            )
            for period_key in ("5d", "10d", "20d")
        }
        alpha_5d = alpha_curve.get("5d")
        alpha_10d = alpha_curve.get("10d")
        alpha_20d = alpha_curve.get("20d")

        if alpha_5d is None:
            signal_stability = "unknown"
        elif (alpha_10d is None and alpha_20d is None) or (
            alpha_5d > 0 and (alpha_10d is None or alpha_10d >= 0) and (alpha_20d is None or alpha_20d >= 0)
        ):
            signal_stability = "stable"
        else:
            signal_stability = "degraded"

        if alpha_5d is None:
            decay_note = "alpha_unavailable"
        elif alpha_20d is None:
            decay_note = "long_horizon_insufficient_samples"
        elif alpha_20d >= alpha_5d:
            decay_note = "signal_persists_or_strengthens"
        elif alpha_20d >= 0:
            decay_note = "signal_decays_but_remains_positive"
        else:
            decay_note = "signal_reverses_on_longer_horizon"

        available = any(
            (bucket_stats.get(bucket, {}).get("5d", {}).get("samples") or 0) > 0
            for bucket in ("bullish", "neutral", "bearish")
        )

        return {
            "available": available,
            "method": "headline_label_oos_v1_fallback_proxy",
            "proxy_only": True,
            "fallback_reason": "insufficient_headline_labels",
            "proxy_component": "recent_return_regime",
            "bucket_stats": bucket_stats,
            "alpha_5d_bull_vs_bear": alpha_5d,
            "signal_stability": signal_stability,
            "decay_analysis": {
                "decay_note": decay_note,
                "alpha_curve": alpha_curve,
            },
        }

    # ── 分量 2: 新闻情绪（30%权重） ──
    @classmethod
    def _news_sentiment_score(cls, headlines: List[str], decay_half_life: int = 5) -> float:
        """基于关键词匹配 + 时间衰减计算新闻情绪得分 0~100
        headlines 按时间倒序排列（最新在前）"""
        if not headlines:
            return 50.0
        decay_rate = math.log(2) / max(decay_half_life, 1)
        total_weight = 0.0
        weighted_score = 0.0
        for i, title in enumerate(headlines):
            w = math.exp(-decay_rate * i)
            total_weight += w
            label = cls._classify_headline(title)
            if label == "bullish":
                s = 80.0
            elif label == "bearish":
                s = 20.0
            else:
                s = 50.0
            weighted_score += w * s
        return max(0.0, min(100.0, weighted_score / max(total_weight, 1e-9)))

    # ── 分量 3: 资金流向（30%权重） ──
    @staticmethod
    def _fund_flow_score(fund_flow_data: Optional[Dict[str, Any]]) -> float:
        """基于北向资金净买入和融资余额变化率计算得分 0~100"""
        if not fund_flow_data:
            return 50.0
        score = 50.0
        # 北向资金净买入（正为流入）
        north_net_raw = float(fund_flow_data.get('north_net_buy', 0) or 0)
        north_unit = str(
            fund_flow_data.get('north_net_buy_unit')
            or fund_flow_data.get('north_net_unit')
            or 'yuan'
        ).strip().lower()
        unit_multiplier = {
            'yuan': 1.0,
            'cny': 1.0,
            'rmb': 1.0,
            'ten_thousand_cny': 1e4,
            'wanyuan': 1e4,
            '万元': 1e4,
            'yi': 1e8,
            '亿元': 1e8,
        }.get(north_unit, 1.0)
        north_net = north_net_raw * unit_multiplier
        if north_net != 0:
            # 归一化：假设 ±10亿 为满分偏移
            score += max(-25.0, min(25.0, north_net / 1e9 * 25.0))
        # 融资余额变化率
        margin_change = float(fund_flow_data.get('margin_change_rate', 0) or 0)
        if margin_change != 0:
            score += max(-25.0, min(25.0, margin_change * 250.0))
        return max(0.0, min(100.0, score))

    def analyze_sentiment(
        self,
        klines: List[Dict[str, Any]],
        news_headlines: Optional[List[str]] = None,
        fund_flow_data: Optional[Dict[str, Any]] = None,
        headline_labels: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        """三分量复合情绪评分"""
        # P2-4.5.8 fix: 区分"真实计算的分量"与"无数据时默认 50.0"
        # 历史问题(诊断报告 §4.5.8):3 components 中 2/3 默认 50,但工具不显式标记
        # AI 看到 score=50 不知是真 neutral 还是数据缺失
        # 修复:effective_components 列出真正有数据的分量,加 component_availability
        pm = self._price_momentum_score(klines)
        pm_available = bool(klines)
        news_provided = bool(news_headlines)
        ns = self._news_sentiment_score(news_headlines or [])
        ff_provided = bool(fund_flow_data)
        ff = self._fund_flow_score(fund_flow_data)
        ordered_klines = self._sort_klines(klines)
        news_oos_validation = self._build_news_sentiment_oos_validation(
            ordered_klines,
            headline_labels=headline_labels,
        )

        # 加权复合
        composite = pm * 0.4 + ns * 0.3 + ff * 0.3

        if composite > 70:
            sentiment = 'bullish'
        elif composite < 30:
            sentiment = 'bearish'
        else:
            sentiment = 'neutral'

        historical_validation = self._build_price_momentum_validation(
            ordered_klines,
            current_price_momentum_score=pm,
        )

        # P2-4.5.8: 列出真正有数据的分量
        effective_components = [
            name for name, available in (
                ("price_momentum", pm_available),
                ("news_sentiment", news_provided),
                ("fund_flow", ff_provided),
            )
            if available
        ]
        availability_warnings: list[str] = []
        if not news_provided:
            availability_warnings.append("news_sentiment_default_neutral_50")
        if not ff_provided:
            availability_warnings.append("fund_flow_default_neutral_50")
        if len(effective_components) <= 1:
            availability_warnings.append(
                f"sentiment_low_confidence: only {len(effective_components)}/3 components have real data"
            )

        result = {
            'sentiment': sentiment,
            'score': round(composite, 2),
            'components': {
                'price_momentum': round(pm, 2),
                'news_sentiment': round(ns, 2),
                'fund_flow': round(ff, 2),
            },
            'component_availability': {
                'price_momentum': pm_available,
                'news_sentiment': news_provided,
                'fund_flow': ff_provided,
            },
            'effective_components': effective_components,
            'effective_component_count': len(effective_components),
            'availability_warnings': availability_warnings,
            'weights': {'price_momentum': 0.4, 'news_sentiment': 0.3, 'fund_flow': 0.3},
            'historical_validation': historical_validation,
            'news_oos_validation': news_oos_validation,
            'data_quality': {
                'headline_count': len(news_headlines or []),
                'headline_label_count': len(headline_labels or []),
                'fund_flow_available': bool(fund_flow_data),
                'price_history_points': len(ordered_klines),
                'historical_validation_available': bool(historical_validation.get('available')),
                'news_oos_validation_available': bool(news_oos_validation.get('available')),
            },
        }
        try:
            as_of = str((ordered_klines[-1] or {}).get('date') or '') if ordered_klines else ''
            code = str(
                (ordered_klines[-1] or {}).get('code')
                or (ordered_klines[-1] or {}).get('symbol')
                or (fund_flow_data or {}).get('code')
                or ''
            ).strip()
            if code:
                get_default_signal_quality_registry().register_sentiment(
                    code=code,
                    as_of=as_of,
                    payload=result,
                )
        except Exception:
            pass
        return result
    
    @staticmethod
    def calculate_fear_greed_index(
        index_klines: List[Dict[str, Any]] | None = None,
        breadth_data: Dict[str, Any] | None = None,
    ) -> Dict[str, Any]:
        """计算恐惧贪婪指数（支持外部传入数据，便于测试与回放）。"""

        def _clamp(v: float) -> int:
            return int(max(0, min(100, round(v))))

        components = {
            'momentum': 50,
            'volatility': 50,
            'volume': 50,
            'breadth': 50,
        }

        # 1) 指数K线驱动：动量、波动、成交量
        ordered_klines = SentimentAnalyzer._sort_klines(index_klines or [])
        if ordered_klines and len(ordered_klines) >= 5:
            closes = [float(k.get('close', 0) or 0) for k in ordered_klines]
            volumes = [float(k.get('volume', 0) or 0) for k in ordered_klines]

            if len(closes) >= 5 and closes[-5] > 0:
                momentum_pct = (closes[-1] - closes[-5]) / closes[-5] * 100.0
                components['momentum'] = _clamp(50 + momentum_pct * 4.0)

            if len(closes) >= 20:
                recent = closes[-20:]
                base = np.mean(recent) if np.mean(recent) else 1.0
                # 波动越大，越偏恐惧（分值越低）
                vol_pct = float(np.std(recent) / base * 100.0)
                components['volatility'] = _clamp(75 - vol_pct * 8.0)

            if len(volumes) >= 20:
                short_v = float(np.mean(volumes[-5:]))
                long_v = float(np.mean(volumes[-20:-5])) if np.mean(volumes[-20:-5]) else 0.0
                if long_v > 0:
                    vr = short_v / long_v
                    components['volume'] = _clamp(50 + (vr - 1.0) * 25.0)

        # 2) 市场广度：外部传入 breadth_data 优先（测试契约依赖）
        if breadth_data:
            lu = float(breadth_data.get('limit_up_count', 0) or 0)
            ld = float(breadth_data.get('limit_down_count', 0) or 0)
            adv = float(breadth_data.get('advance_count', 0) or 0)
            dec = float(breadth_data.get('decline_count', 0) or 0)
            limit_balance = (lu - ld) / max(lu + ld, 1.0)
            adv_balance = (adv - dec) / max(adv + dec, 1.0)
            breadth_score = 50 + limit_balance * 30 + adv_balance * 20
            components['breadth'] = _clamp(breadth_score)

        index = _clamp(sum(components.values()) / 4.0)
        if index >= 80:
            level = 'extreme_greed'
        elif index >= 60:
            level = 'greed'
        elif index <= 20:
            level = 'extreme_fear'
        elif index <= 40:
            level = 'fear'
        else:
            level = 'neutral'

        return {
            'index': index,
            'level': level,
            'components': components,
        }


sentiment_analyzer = SentimentAnalyzer()
