"""Expanded factory strategies for P1 runtime coverage.

These strategies provide lightweight, deterministic signal generation so the
strategy factory's expanded rule families are executable in the backtest
engine instead of being whitelisted-only types.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

import numpy as np

from .strategy_base import IStrategy


def _rolling_mean(values: np.ndarray, window: int) -> np.ndarray:
    result = np.full(len(values), np.nan, dtype=float)
    if window <= 0 or len(values) < window:
        return result
    for i in range(window - 1, len(values)):
        result[i] = float(np.mean(values[i - window + 1 : i + 1]))
    return result


def _rolling_volatility(closes: np.ndarray, window: int) -> np.ndarray:
    result = np.full(len(closes), np.nan, dtype=float)
    if len(closes) < window + 1:
        return result
    for i in range(window, len(closes)):
        window_slice = closes[i - window : i + 1]
        prev = window_slice[:-1]
        curr = window_slice[1:]
        valid = prev > 0
        if not np.any(valid):
            continue
        returns = (curr[valid] - prev[valid]) / prev[valid]
        if returns.size >= 2:
            result[i] = float(np.std(returns))
    return result


def _compute_rsi(closes: np.ndarray, period: int) -> np.ndarray:
    rsi = np.full(len(closes), np.nan, dtype=float)
    if len(closes) < period + 1:
        return rsi
    for i in range(period, len(closes)):
        gains = 0.0
        losses = 0.0
        for j in range(i - period + 1, i + 1):
            change = float(closes[j] - closes[j - 1])
            if change > 0:
                gains += change
            else:
                losses -= change
        avg_gain = gains / period
        avg_loss = losses / period
        if avg_loss == 0:
            rsi[i] = 100.0
        else:
            rs = avg_gain / avg_loss
            rsi[i] = 100.0 - (100.0 / (1.0 + rs))
    return rsi


def _clamped_rank(window: np.ndarray, value: float) -> float:
    valid = window[np.isfinite(window)]
    if valid.size < 5 or not np.isfinite(value):
        return np.nan
    return float(np.sum(valid < value)) / float(valid.size)


class VolatilityBreakoutStrategy(IStrategy):
    def __init__(self, lookback: int = 20, threshold: float = 0.025):
        self.lookback = lookback
        self.threshold = threshold

    @classmethod
    def name(cls) -> str:
        return "volatility_breakout"

    @classmethod
    def description(cls) -> str:
        return "波动率突破策略：突破滚动波动率阈值且价格站上均线时买入"

    def get_parameters(self) -> Dict[str, Any]:
        return {"lookback": self.lookback, "threshold": self.threshold}

    def set_parameters(self, params: Dict[str, Any]) -> None:
        self.lookback = max(5, int(params.get("lookback", self.lookback) or self.lookback))
        self.threshold = max(0.005, float(params.get("threshold", self.threshold) or self.threshold))

    def generate_signals(self, closes: np.ndarray, volumes: Optional[np.ndarray] = None) -> np.ndarray:
        signals = np.zeros(len(closes), dtype=np.int8)
        if len(closes) < self.lookback + 2:
            return signals
        rolling_ma = _rolling_mean(closes, self.lookback)
        rolling_vol = _rolling_volatility(closes, self.lookback)
        for i in range(self.lookback, len(closes)):
            base = float(closes[i - self.lookback])
            if base <= 0 or not np.isfinite(rolling_ma[i]) or not np.isfinite(rolling_vol[i]):
                continue
            breakout = float(closes[i] - base) / base
            effective_threshold = max(self.threshold, float(rolling_vol[i]) * 0.9)
            if closes[i] > rolling_ma[i] and breakout > effective_threshold:
                signals[i] = 1
            elif breakout < -effective_threshold * 0.6 or closes[i] < rolling_ma[i] * 0.985:
                signals[i] = -1
        return signals


class EventStructureBreakoutStrategy(IStrategy):
    def __init__(
        self,
        breakout_window: int = 12,
        breakout_buffer_pct: float = 0.002,
        contraction_window: int = 5,
        contraction_max_range_ratio: float = 0.06,
        volume_window: int = 8,
        breakout_volume_ratio_min: float = 1.0,
        structure_window: int = 4,
        structure_close_location_min: float = 0.62,
        structure_body_return_min: float = 0.003,
        event_impulse_window: int = 5,
        event_impulse_threshold: float = 0.015,
        max_hold_bars: int = 8,
        breakout_failure_close_buffer: float = -0.012,
        adverse_volume_ratio_max: float = 0.85,
    ):
        self.breakout_window = breakout_window
        self.breakout_buffer_pct = breakout_buffer_pct
        self.contraction_window = contraction_window
        self.contraction_max_range_ratio = contraction_max_range_ratio
        self.volume_window = volume_window
        self.breakout_volume_ratio_min = breakout_volume_ratio_min
        self.structure_window = structure_window
        self.structure_close_location_min = structure_close_location_min
        self.structure_body_return_min = structure_body_return_min
        self.event_impulse_window = event_impulse_window
        self.event_impulse_threshold = event_impulse_threshold
        self.max_hold_bars = max_hold_bars
        self.breakout_failure_close_buffer = breakout_failure_close_buffer
        self.adverse_volume_ratio_max = adverse_volume_ratio_max

    @classmethod
    def name(cls) -> str:
        return "event_structure_breakout"

    @classmethod
    def description(cls) -> str:
        return "事件结构突破策略：催化后缩量整理并放量突破时入场，跌回突破位或结构走坏时退出"

    def get_parameters(self) -> Dict[str, Any]:
        return {
            "breakout_window": self.breakout_window,
            "breakout_buffer_pct": self.breakout_buffer_pct,
            "contraction_window": self.contraction_window,
            "contraction_max_range_ratio": self.contraction_max_range_ratio,
            "volume_window": self.volume_window,
            "breakout_volume_ratio_min": self.breakout_volume_ratio_min,
            "structure_window": self.structure_window,
            "structure_close_location_min": self.structure_close_location_min,
            "structure_body_return_min": self.structure_body_return_min,
            "event_impulse_window": self.event_impulse_window,
            "event_impulse_threshold": self.event_impulse_threshold,
            "max_hold_bars": self.max_hold_bars,
            "breakout_failure_close_buffer": self.breakout_failure_close_buffer,
            "adverse_volume_ratio_max": self.adverse_volume_ratio_max,
            "lookback": self.breakout_window,
            "threshold": self.breakout_buffer_pct,
        }

    def set_parameters(self, params: Dict[str, Any]) -> None:
        self.breakout_window = max(
            8,
            int(params.get("breakout_window", params.get("lookback", self.breakout_window)) or self.breakout_window),
        )
        self.breakout_buffer_pct = max(
            0.001,
            float(
                params.get(
                    "breakout_buffer_pct",
                    params.get("threshold", self.breakout_buffer_pct),
                )
                or self.breakout_buffer_pct
            ),
        )
        self.contraction_window = max(
            3,
            int(params.get("contraction_window", self.contraction_window) or self.contraction_window),
        )
        self.contraction_max_range_ratio = max(
            0.01,
            float(
                params.get("contraction_max_range_ratio", self.contraction_max_range_ratio)
                or self.contraction_max_range_ratio
            ),
        )
        self.volume_window = max(
            3,
            int(params.get("volume_window", self.volume_window) or self.volume_window),
        )
        self.breakout_volume_ratio_min = max(
            0.8,
            float(
                params.get("breakout_volume_ratio_min", self.breakout_volume_ratio_min)
                or self.breakout_volume_ratio_min
            ),
        )
        self.structure_window = max(
            2,
            int(params.get("structure_window", self.structure_window) or self.structure_window),
        )
        self.structure_close_location_min = max(
            0.4,
            float(
                params.get("structure_close_location_min", self.structure_close_location_min)
                or self.structure_close_location_min
            ),
        )
        self.structure_body_return_min = float(
            params.get("structure_body_return_min", self.structure_body_return_min)
            or self.structure_body_return_min
        )
        self.event_impulse_window = max(
            2,
            int(params.get("event_impulse_window", self.event_impulse_window) or self.event_impulse_window),
        )
        self.event_impulse_threshold = float(
            params.get("event_impulse_threshold", self.event_impulse_threshold)
            or self.event_impulse_threshold
        )
        self.max_hold_bars = max(
            2,
            int(params.get("max_hold_bars", self.max_hold_bars) or self.max_hold_bars),
        )
        self.breakout_failure_close_buffer = float(
            params.get("breakout_failure_close_buffer", self.breakout_failure_close_buffer)
            or self.breakout_failure_close_buffer
        )
        self.adverse_volume_ratio_max = max(
            0.3,
            float(params.get("adverse_volume_ratio_max", self.adverse_volume_ratio_max) or self.adverse_volume_ratio_max),
        )

    def _build_synthetic_klines(
        self,
        closes: np.ndarray,
        volumes: Optional[np.ndarray] = None,
    ) -> list[dict[str, float]]:
        volume_arr = np.asarray(volumes if volumes is not None else np.zeros(len(closes)), dtype=float)
        klines: list[dict[str, float]] = []
        prev_close = float(closes[0]) if len(closes) else 0.0
        for idx, close_value in enumerate(np.asarray(closes, dtype=float)):
            close_price = float(close_value)
            open_price = prev_close if idx > 0 else close_price
            high_price = max(open_price, close_price)
            low_price = min(open_price, close_price)
            klines.append(
                {
                    "open": open_price,
                    "high": high_price,
                    "low": low_price,
                    "close": close_price,
                    "volume": float(volume_arr[idx]) if idx < len(volume_arr) else 0.0,
                }
            )
            prev_close = close_price
        return klines

    def _generate_masks_from_klines(
        self,
        klines: list[dict[str, Any]],
    ) -> tuple[np.ndarray, np.ndarray]:
        ordered = list(klines or [])
        closes = np.array([float((item or {}).get("close", 0.0) or 0.0) for item in ordered], dtype=float)
        opens = np.array([float((item or {}).get("open", 0.0) or 0.0) for item in ordered], dtype=float)
        highs = np.array([float((item or {}).get("high", 0.0) or 0.0) for item in ordered], dtype=float)
        lows = np.array([float((item or {}).get("low", 0.0) or 0.0) for item in ordered], dtype=float)
        volumes = np.array([float((item or {}).get("volume", 0.0) or 0.0) for item in ordered], dtype=float)
        entry = np.zeros(len(closes), dtype=bool)
        exit_ = np.zeros(len(closes), dtype=bool)

        start_index = max(
            self.breakout_window,
            self.contraction_window,
            self.volume_window,
            self.structure_window,
            self.event_impulse_window + 1,
        )
        if len(closes) <= start_index:
            return entry, exit_

        short_mean = _rolling_mean(closes, self.structure_window)
        volume_mean = _rolling_mean(volumes, self.volume_window)
        in_position = False
        entry_index = -1
        breakout_level = 0.0

        for i in range(start_index, len(closes)):
            breakout_slice = highs[i - self.breakout_window : i]
            contraction_highs = highs[i - self.contraction_window : i]
            contraction_lows = lows[i - self.contraction_window : i]
            if breakout_slice.size == 0 or contraction_highs.size == 0 or contraction_lows.size == 0:
                continue
            prior_high = float(np.max(breakout_slice))
            contraction_range = float(np.max(contraction_highs) - np.min(contraction_lows))
            contraction_anchor = max(float(closes[i - 1]), 1e-6)
            contraction_ratio = contraction_range / contraction_anchor

            baseline_volume = float(volume_mean[i - 1]) if i - 1 >= 0 and np.isfinite(volume_mean[i - 1]) else 0.0
            volume_ratio = float(volumes[i]) / baseline_volume if baseline_volume > 0 else 1.0

            pre_contraction_end = max(i - self.contraction_window, 1)
            pre_contraction_start = max(0, pre_contraction_end - self.event_impulse_window)
            pre_contraction_slice = closes[pre_contraction_start:pre_contraction_end]
            if pre_contraction_slice.size == 0:
                continue
            impulse_base = float(np.min(pre_contraction_slice))
            impulse_peak = float(np.max(pre_contraction_slice))
            if impulse_base <= 0:
                continue
            impulse_return = (impulse_peak - impulse_base) / impulse_base

            bar_range = max(float(highs[i] - lows[i]), 1e-6)
            close_location = (float(closes[i]) - float(lows[i])) / bar_range
            body_return = (float(closes[i]) - float(opens[i])) / max(abs(float(opens[i])), 1e-6)
            recent_close_ceiling = float(np.max(closes[i - self.structure_window : i]))
            structure_ready = (
                close_location >= self.structure_close_location_min
                and body_return >= self.structure_body_return_min
                and float(closes[i]) >= recent_close_ceiling
                and (
                    not np.isfinite(short_mean[i])
                    or float(closes[i]) >= float(short_mean[i])
                )
            )
            breakout_ready = float(closes[i]) >= prior_high * (1.0 + self.breakout_buffer_pct)
            event_ready = impulse_return >= self.event_impulse_threshold
            contraction_ready = contraction_ratio <= self.contraction_max_range_ratio
            volume_ready = volume_ratio >= self.breakout_volume_ratio_min

            if (
                not in_position
                and breakout_ready
                and event_ready
                and contraction_ready
                and volume_ready
                and structure_ready
            ):
                entry[i] = True
                in_position = True
                entry_index = i
                breakout_level = prior_high
                continue

            if not in_position:
                continue

            holding_bars = i - entry_index
            below_breakout = float(closes[i]) <= breakout_level * (1.0 + self.breakout_failure_close_buffer)
            trend_lost = np.isfinite(short_mean[i]) and float(closes[i]) < float(short_mean[i])
            volume_faded = volume_ratio <= self.adverse_volume_ratio_max
            time_stop = holding_bars >= self.max_hold_bars
            if below_breakout or time_stop or (trend_lost and volume_faded):
                exit_[i] = True
                in_position = False
                entry_index = -1
                breakout_level = 0.0

        return entry, exit_

    def generate_signals(self, closes: np.ndarray, volumes: Optional[np.ndarray] = None) -> np.ndarray:
        klines = self._build_synthetic_klines(closes, volumes)
        entry, exit_ = self._generate_masks_from_klines(klines)
        signals = np.zeros(len(closes), dtype=np.int8)
        signals[entry] = 1
        signals[exit_] = -1
        return signals

    def generate_entry_exit_masks_from_klines(self, klines: list[dict[str, Any]]):
        return self._generate_masks_from_klines(klines)


class GapFillStrategy(IStrategy):
    def __init__(
        self,
        gap_threshold: float = 0.02,
        rsi_period: int = 5,
        oversold: float = 24.0,
        overbought: float = 58.0,
    ):
        self.gap_threshold = gap_threshold
        self.rsi_period = rsi_period
        self.oversold = oversold
        self.overbought = overbought

    @classmethod
    def name(cls) -> str:
        return "gap_fill"

    @classmethod
    def description(cls) -> str:
        return "跳空回补策略：利用向下跳空后的回补行为进行短线均值回归"

    def get_parameters(self) -> Dict[str, Any]:
        return {
            "gap_threshold": self.gap_threshold,
            "rsi_period": self.rsi_period,
            "oversold": self.oversold,
            "overbought": self.overbought,
        }

    def set_parameters(self, params: Dict[str, Any]) -> None:
        self.gap_threshold = max(0.005, float(params.get("gap_threshold", self.gap_threshold) or self.gap_threshold))
        self.rsi_period = max(2, int(params.get("rsi_period", self.rsi_period) or self.rsi_period))
        self.oversold = float(params.get("oversold", self.oversold) or self.oversold)
        self.overbought = float(params.get("overbought", self.overbought) or self.overbought)

    def generate_signals(self, closes: np.ndarray, volumes: Optional[np.ndarray] = None) -> np.ndarray:
        signals = np.zeros(len(closes), dtype=np.int8)
        if len(closes) < self.rsi_period + 3:
            return signals
        rsi = _compute_rsi(closes, self.rsi_period)
        mean_line = _rolling_mean(closes, max(5, self.rsi_period * 2))
        for i in range(max(5, self.rsi_period), len(closes)):
            if not np.isfinite(rsi[i]) or not np.isfinite(mean_line[i]):
                continue
            if rsi[i] <= self.oversold and closes[i] < mean_line[i] * 0.99:
                signals[i] = 1
            elif rsi[i] >= self.overbought or closes[i] >= mean_line[i] * 1.01:
                signals[i] = -1
        return signals

    def generate_entry_exit_masks_from_klines(self, klines: list[dict[str, Any]]):
        closes = np.array([float((item or {}).get("close", 0.0) or 0.0) for item in klines], dtype=float)
        entry = np.zeros(len(closes), dtype=bool)
        exit_ = np.zeros(len(closes), dtype=bool)
        if len(closes) < self.rsi_period + 3:
            return entry, exit_

        rsi = _compute_rsi(closes, self.rsi_period)
        for i in range(1, len(klines)):
            row = dict(klines[i] or {})
            prev_close = float((klines[i - 1] or {}).get("close", 0.0) or 0.0)
            open_price = float(row.get("open", 0.0) or 0.0)
            close_price = float(row.get("close", 0.0) or 0.0)
            if prev_close <= 0 or open_price <= 0 or close_price <= 0:
                continue
            gap = (open_price - prev_close) / prev_close
            if gap <= -self.gap_threshold and close_price >= open_price and rsi[i] <= max(self.oversold + 15.0, 45.0):
                entry[i] = True
            if close_price >= prev_close or rsi[i] >= self.overbought or gap >= self.gap_threshold:
                exit_[i] = True
        return entry, exit_


class MeanReversionShortStrategy(IStrategy):
    def __init__(self, rsi_period: int = 6, oversold: float = 26.0, overbought: float = 62.0):
        self.rsi_period = rsi_period
        self.oversold = oversold
        self.overbought = overbought

    @classmethod
    def name(cls) -> str:
        return "mean_reversion_short"

    @classmethod
    def description(cls) -> str:
        return "短周期均值回归策略：短周期 RSI 和均线乖离共同确认反转"

    def get_parameters(self) -> Dict[str, Any]:
        return {
            "rsi_period": self.rsi_period,
            "oversold": self.oversold,
            "overbought": self.overbought,
        }

    def set_parameters(self, params: Dict[str, Any]) -> None:
        self.rsi_period = max(2, int(params.get("rsi_period", self.rsi_period) or self.rsi_period))
        self.oversold = float(params.get("oversold", self.oversold) or self.oversold)
        self.overbought = float(params.get("overbought", self.overbought) or self.overbought)

    def generate_signals(self, closes: np.ndarray, volumes: Optional[np.ndarray] = None) -> np.ndarray:
        signals = np.zeros(len(closes), dtype=np.int8)
        if len(closes) < self.rsi_period + 3:
            return signals
        rsi = _compute_rsi(closes, self.rsi_period)
        mean_line = _rolling_mean(closes, max(5, self.rsi_period * 2))
        for i in range(max(5, self.rsi_period), len(closes)):
            if not np.isfinite(rsi[i]) or not np.isfinite(mean_line[i]) or mean_line[i] <= 0:
                continue
            deviation = (float(closes[i]) - float(mean_line[i])) / float(mean_line[i])
            if rsi[i] <= self.oversold and deviation <= -0.015:
                signals[i] = 1
            elif rsi[i] >= self.overbought or deviation >= 0.02:
                signals[i] = -1
        return signals


class SectorRotationStrategy(IStrategy):
    def __init__(self):
        self._lookback = 20
        self._factor_weights: Dict[str, float] = {"momentum": 0.45, "quality": 0.30, "value": 0.25}

    @classmethod
    def name(cls) -> str:
        return "sector_rotation"

    @classmethod
    def description(cls) -> str:
        return "行业轮动策略：用动量、稳定性与估值回归代理构造轮动打分"

    def get_parameters(self) -> Dict[str, Any]:
        return {"lookback": self._lookback, "factor_weights": dict(self._factor_weights)}

    def set_parameters(self, params: Dict[str, Any]) -> None:
        self._lookback = max(10, int(params.get("lookback", self._lookback) or self._lookback))
        weights = dict(params.get("factor_weights") or {})
        if weights:
            total = sum(float(value or 0.0) for value in weights.values()) or 1.0
            self._factor_weights = {
                str(key): float(value or 0.0) / total
                for key, value in weights.items()
            }

    def generate_signals(self, closes: np.ndarray, volumes: Optional[np.ndarray] = None) -> np.ndarray:
        signals = np.zeros(len(closes), dtype=np.int8)
        if len(closes) < self._lookback + 5:
            return signals
        score = np.full(len(closes), np.nan, dtype=float)
        for i in range(self._lookback, len(closes)):
            base = float(closes[i - self._lookback])
            short_base = float(closes[max(0, i - 10)])
            if base <= 0 or short_base <= 0:
                continue
            window = closes[i - self._lookback : i + 1]
            returns = np.diff(window) / np.maximum(window[:-1], 1e-8)
            momentum = (float(closes[i]) - base) / base
            short_momentum = (float(closes[i]) - short_base) / short_base
            stability = -float(np.std(returns)) if returns.size > 1 else 0.0
            mean_line = float(np.mean(window))
            value_reversion = -((float(closes[i]) - mean_line) / mean_line) if mean_line > 0 else 0.0
            score[i] = (
                self._factor_weights.get("momentum", 0.0) * (0.6 * momentum + 0.4 * short_momentum)
                + self._factor_weights.get("quality", 0.0) * stability
                + self._factor_weights.get("value", 0.0) * value_reversion
            )
        for i in range(self._lookback, len(closes)):
            rank = _clamped_rank(score[max(0, i - self._lookback) : i + 1], score[i])
            if not np.isfinite(rank):
                continue
            if rank >= 0.72:
                signals[i] = 1
            elif rank <= 0.28:
                signals[i] = -1
        return signals


class NorthCapitalTrackStrategy(IStrategy):
    def __init__(self, lookback: int = 15, threshold: float = 0.015):
        self.lookback = lookback
        self.threshold = threshold

    @classmethod
    def name(cls) -> str:
        return "north_capital_track"

    @classmethod
    def description(cls) -> str:
        return "北向资金跟踪策略：用价量共振近似外资持续流入的跟随交易"

    def get_parameters(self) -> Dict[str, Any]:
        return {"lookback": self.lookback, "threshold": self.threshold}

    def set_parameters(self, params: Dict[str, Any]) -> None:
        self.lookback = max(5, int(params.get("lookback", self.lookback) or self.lookback))
        self.threshold = max(0.005, float(params.get("threshold", self.threshold) or self.threshold))

    def generate_signals(self, closes: np.ndarray, volumes: Optional[np.ndarray] = None) -> np.ndarray:
        signals = np.zeros(len(closes), dtype=np.int8)
        if len(closes) < self.lookback + 2:
            return signals
        volume_arr = np.asarray(volumes if volumes is not None else np.zeros(len(closes)), dtype=float)
        for i in range(self.lookback, len(closes)):
            base = float(closes[i - self.lookback])
            if base <= 0:
                continue
            trend = (float(closes[i]) - base) / base
            short_volume = float(np.mean(volume_arr[max(0, i - 4) : i + 1])) if volume_arr.size else 0.0
            long_volume = float(np.mean(volume_arr[i - self.lookback : i + 1])) if volume_arr.size else 0.0
            volume_ratio = short_volume / long_volume if long_volume > 0 else 1.0
            if trend > self.threshold and volume_ratio >= 1.1:
                signals[i] = 1
            elif trend < -self.threshold * 0.8 or (trend < self.threshold * 0.25 and volume_ratio <= 0.9):
                signals[i] = -1
        return signals


class MarginDivergenceStrategy(IStrategy):
    def __init__(
        self,
        fear_threshold: int = 43,
        greed_threshold: int = 60,
        lookback: int = 12,
        rebound_window: int = 3,
        repair_drawdown_floor: float = -0.06,
        repair_rebound_pct: float = 0.012,
        dryup_window: int = 3,
        dryup_max_ratio: float = 0.9,
        liquidity_window: int = 8,
        entry_volume_floor_ratio: float = 1.0,
        structure_window: int = 4,
        structure_close_location_min: float = 0.58,
        structure_body_return_min: float = 0.002,
        max_hold_bars: int = 8,
        adverse_volume_break_ratio: float = 0.72,
        adverse_close_break_pct: float = -0.012,
    ):
        self.fear_threshold = fear_threshold
        self.greed_threshold = greed_threshold
        self.lookback = lookback
        self.rebound_window = rebound_window
        self.repair_drawdown_floor = repair_drawdown_floor
        self.repair_rebound_pct = repair_rebound_pct
        self.dryup_window = dryup_window
        self.dryup_max_ratio = dryup_max_ratio
        self.liquidity_window = liquidity_window
        self.entry_volume_floor_ratio = entry_volume_floor_ratio
        self.structure_window = structure_window
        self.structure_close_location_min = structure_close_location_min
        self.structure_body_return_min = structure_body_return_min
        self.max_hold_bars = max_hold_bars
        self.adverse_volume_break_ratio = adverse_volume_break_ratio
        self.adverse_close_break_pct = adverse_close_break_pct

    @classmethod
    def name(cls) -> str:
        return "margin_divergence"

    @classmethod
    def description(cls) -> str:
        return "流动性背离修复策略：放量修复与结构确认共振时入场，量价失真时退出"

    def get_parameters(self) -> Dict[str, Any]:
        return {
            "fear_threshold": self.fear_threshold,
            "greed_threshold": self.greed_threshold,
            "lookback": self.lookback,
            "rebound_window": self.rebound_window,
            "repair_drawdown_floor": self.repair_drawdown_floor,
            "repair_rebound_pct": self.repair_rebound_pct,
            "dryup_window": self.dryup_window,
            "dryup_max_ratio": self.dryup_max_ratio,
            "liquidity_window": self.liquidity_window,
            "entry_volume_floor_ratio": self.entry_volume_floor_ratio,
            "structure_window": self.structure_window,
            "structure_close_location_min": self.structure_close_location_min,
            "structure_body_return_min": self.structure_body_return_min,
            "max_hold_bars": self.max_hold_bars,
            "adverse_volume_break_ratio": self.adverse_volume_break_ratio,
            "adverse_close_break_pct": self.adverse_close_break_pct,
        }

    def set_parameters(self, params: Dict[str, Any]) -> None:
        self.fear_threshold = int(params.get("fear_threshold", self.fear_threshold) or self.fear_threshold)
        self.greed_threshold = int(params.get("greed_threshold", self.greed_threshold) or self.greed_threshold)
        self.lookback = max(5, int(params.get("lookback", self.lookback) or self.lookback))
        self.rebound_window = max(2, int(params.get("rebound_window", self.rebound_window) or self.rebound_window))
        self.repair_drawdown_floor = float(
            params.get("repair_drawdown_floor", self.repair_drawdown_floor) or self.repair_drawdown_floor
        )
        self.repair_rebound_pct = float(
            params.get("repair_rebound_pct", self.repair_rebound_pct) or self.repair_rebound_pct
        )
        self.dryup_window = max(2, int(params.get("dryup_window", self.dryup_window) or self.dryup_window))
        self.dryup_max_ratio = float(
            params.get("dryup_max_ratio", self.dryup_max_ratio) or self.dryup_max_ratio
        )
        self.liquidity_window = max(
            self.dryup_window + 1,
            int(params.get("liquidity_window", self.liquidity_window) or self.liquidity_window),
        )
        self.entry_volume_floor_ratio = float(
            params.get("entry_volume_floor_ratio", self.entry_volume_floor_ratio)
            or self.entry_volume_floor_ratio
        )
        self.structure_window = max(
            2,
            int(params.get("structure_window", self.structure_window) or self.structure_window),
        )
        self.structure_close_location_min = float(
            params.get("structure_close_location_min", self.structure_close_location_min)
            or self.structure_close_location_min
        )
        self.structure_body_return_min = float(
            params.get("structure_body_return_min", self.structure_body_return_min)
            or self.structure_body_return_min
        )
        self.max_hold_bars = max(2, int(params.get("max_hold_bars", self.max_hold_bars) or self.max_hold_bars))
        self.adverse_volume_break_ratio = float(
            params.get("adverse_volume_break_ratio", self.adverse_volume_break_ratio)
            or self.adverse_volume_break_ratio
        )
        self.adverse_close_break_pct = float(
            params.get("adverse_close_break_pct", self.adverse_close_break_pct)
            or self.adverse_close_break_pct
        )

    def _build_synthetic_klines(
        self,
        closes: np.ndarray,
        volumes: Optional[np.ndarray] = None,
    ) -> list[dict[str, float]]:
        volume_arr = np.asarray(volumes if volumes is not None else np.zeros(len(closes)), dtype=float)
        klines: list[dict[str, float]] = []
        prev_close = float(closes[0]) if len(closes) else 0.0
        for idx, close_value in enumerate(np.asarray(closes, dtype=float)):
            close_price = float(close_value)
            open_price = prev_close if idx > 0 else close_price
            high_price = max(open_price, close_price)
            low_price = min(open_price, close_price)
            klines.append(
                {
                    "open": open_price,
                    "high": high_price,
                    "low": low_price,
                    "close": close_price,
                    "volume": float(volume_arr[idx]) if idx < len(volume_arr) else 0.0,
                }
            )
            prev_close = close_price
        return klines

    def _generate_masks_from_arrays(
        self,
        closes: np.ndarray,
        *,
        opens: np.ndarray,
        highs: np.ndarray,
        lows: np.ndarray,
        volumes: np.ndarray,
    ) -> tuple[np.ndarray, np.ndarray]:
        entry = np.zeros(len(closes), dtype=bool)
        exit_ = np.zeros(len(closes), dtype=bool)
        start_index = max(
            self.lookback,
            self.rebound_window,
            self.liquidity_window,
            self.structure_window,
            self.dryup_window + 1,
        )
        if len(closes) <= start_index:
            return entry, exit_

        long_volume_mean = _rolling_mean(volumes, self.liquidity_window)
        in_position = False
        entry_index = -1
        entry_price = 0.0

        for i in range(start_index, len(closes)):
            medium_base = float(closes[i - self.lookback])
            rebound_base = float(closes[i - self.rebound_window])
            if medium_base <= 0 or rebound_base <= 0:
                continue

            medium_return = (float(closes[i]) - medium_base) / medium_base
            rebound_return = (float(closes[i]) - rebound_base) / rebound_base
            current_volume = float(volumes[i]) if i < len(volumes) else 0.0
            baseline_volume = float(long_volume_mean[i]) if np.isfinite(long_volume_mean[i]) else 0.0
            entry_volume_ratio = current_volume / baseline_volume if baseline_volume > 0 else 1.0

            dryup_slice = volumes[max(0, i - self.dryup_window) : i]
            dryup_mean = float(np.mean(dryup_slice)) if dryup_slice.size else 0.0
            dryup_ratio = dryup_mean / baseline_volume if baseline_volume > 0 else 1.0

            bar_low = float(lows[i])
            bar_high = float(highs[i])
            bar_open = float(opens[i])
            bar_range = max(bar_high - bar_low, 1e-6)
            close_location = (float(closes[i]) - bar_low) / bar_range
            body_return = (float(closes[i]) - bar_open) / max(abs(bar_open), 1e-6)

            recent_lows = lows[max(0, i - self.structure_window) : i]
            recent_lows = recent_lows[np.isfinite(recent_lows)]
            recent_low_floor = float(np.min(recent_lows)) if recent_lows.size else bar_low

            pseudo_fg = 50.0 + medium_return * 220.0 + (entry_volume_ratio - 1.0) * 12.0
            repair_ready = (
                pseudo_fg <= float(self.fear_threshold)
                and medium_return <= self.repair_drawdown_floor
                and rebound_return >= self.repair_rebound_pct
                and dryup_ratio <= self.dryup_max_ratio
                and entry_volume_ratio >= self.entry_volume_floor_ratio
                and close_location >= self.structure_close_location_min
                and body_return >= self.structure_body_return_min
                and bar_low >= recent_low_floor
                and i >= 1
                and closes[i] >= closes[i - 1]
            )

            if not in_position and repair_ready:
                entry[i] = True
                in_position = True
                entry_index = i
                entry_price = float(closes[i])
                continue

            if not in_position:
                continue

            hold_bars = i - entry_index
            medium_divergence = rebound_return - medium_return
            pseudo_exit_fg = 50.0 + rebound_return * 350.0 + (entry_volume_ratio - 1.0) * 15.0
            adverse_close = (
                (float(closes[i]) - entry_price) / max(abs(entry_price), 1e-6)
                <= self.adverse_close_break_pct
            )
            adverse_volume = entry_volume_ratio <= self.adverse_volume_break_ratio and float(closes[i]) < float(closes[i - 1])
            reversal_break = medium_divergence < 0.0 and rebound_return <= 0.0
            greed_exit = pseudo_exit_fg >= float(self.greed_threshold)
            timed_out = hold_bars >= self.max_hold_bars
            if timed_out or greed_exit or adverse_close or adverse_volume or reversal_break:
                exit_[i] = True
                in_position = False
                entry_index = -1
                entry_price = 0.0

        return entry, exit_

    def generate_entry_exit_masks_from_klines(self, klines: list[dict[str, Any]]):
        closes = np.asarray([float((item or {}).get("close", 0.0) or 0.0) for item in klines], dtype=float)
        opens = np.asarray(
            [
                float((item or {}).get("open", (item or {}).get("close", 0.0)) or 0.0)
                for item in klines
            ],
            dtype=float,
        )
        highs = np.asarray(
            [
                float(
                    (item or {}).get(
                        "high",
                        max(
                            float((item or {}).get("open", (item or {}).get("close", 0.0)) or 0.0),
                            float((item or {}).get("close", 0.0) or 0.0),
                        ),
                    )
                    or 0.0
                )
                for item in klines
            ],
            dtype=float,
        )
        lows = np.asarray(
            [
                float(
                    (item or {}).get(
                        "low",
                        min(
                            float((item or {}).get("open", (item or {}).get("close", 0.0)) or 0.0),
                            float((item or {}).get("close", 0.0) or 0.0),
                        ),
                    )
                    or 0.0
                )
                for item in klines
            ],
            dtype=float,
        )
        volumes = np.asarray([float((item or {}).get("volume", 0.0) or 0.0) for item in klines], dtype=float)
        return self._generate_masks_from_arrays(
            closes,
            opens=opens,
            highs=highs,
            lows=lows,
            volumes=volumes,
        )

    def generate_signals(self, closes: np.ndarray, volumes: Optional[np.ndarray] = None) -> np.ndarray:
        synthetic_klines = self._build_synthetic_klines(closes, volumes)
        entry, exit_ = self.generate_entry_exit_masks_from_klines(synthetic_klines)
        signals = np.zeros(len(closes), dtype=np.int8)
        signals[np.asarray(entry, dtype=bool)] = 1
        signals[np.asarray(exit_, dtype=bool)] = -1
        return signals


__all__ = [
    "VolatilityBreakoutStrategy",
    "GapFillStrategy",
    "MeanReversionShortStrategy",
    "SectorRotationStrategy",
    "NorthCapitalTrackStrategy",
    "MarginDivergenceStrategy",
]
