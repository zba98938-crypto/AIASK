"""内置策略 — IStrategy 子类封装

逻辑直接复用 engine.py 中 _build_strategy_masks() 的对应分支。
JIT 快速路径不受影响；这些类仅用于信号记录和 StrategyRegistry。
"""

from typing import Any, Dict, Optional

import numpy as np

from .strategy_base import IStrategy


def _rolling_mean(values: np.ndarray, window: int) -> np.ndarray:
    result = np.full(len(values), np.nan)
    if window <= 0 or len(values) < window:
        return result
    for i in range(window - 1, len(values)):
        result[i] = np.mean(values[i - window + 1 : i + 1])
    return result


def _path_noise_ratio(values: np.ndarray, window: int) -> np.ndarray:
    result = np.full(len(values), np.nan)
    if window <= 1 or len(values) <= window:
        return result
    for i in range(window, len(values)):
        segment = values[i - window : i + 1]
        path_length = float(np.sum(np.abs(np.diff(segment))))
        net_move = abs(float(segment[-1] - segment[0]))
        result[i] = path_length / max(net_move, 1e-6)
    return result


def _reversal_regime_label(
    closes: np.ndarray,
    index: int,
    *,
    lookback: int,
    volatility_window: int,
    bearish_threshold: float,
    bullish_threshold: float,
    volatility_threshold: float,
) -> str:
    if index < max(lookback, volatility_window) or index >= len(closes):
        return "unknown"
    base = float(closes[index - lookback] or 0.0)
    if base <= 0:
        return "unknown"
    ret_window = (float(closes[index]) - base) / base
    window_slice = closes[index - volatility_window : index + 1]
    if len(window_slice) < volatility_window + 1:
        return "unknown"
    daily_returns = np.diff(window_slice) / np.maximum(window_slice[:-1], 1e-12)
    annualized_volatility = float(np.std(daily_returns) * np.sqrt(250.0))
    is_volatile = annualized_volatility >= volatility_threshold
    if ret_window <= bearish_threshold:
        return "bear_volatile" if is_volatile else "bear_calm"
    if ret_window >= bullish_threshold:
        return "bull_volatile" if is_volatile else "bull_calm"
    return "range_volatile" if is_volatile else "range_calm"


class MaCrossStrategy(IStrategy):
    """均线交叉策略"""

    def __init__(self, short_period: int = 5, long_period: int = 20):
        self.short_period = short_period
        self.long_period = long_period

    @classmethod
    def name(cls) -> str:
        return "ma_cross"

    @classmethod
    def description(cls) -> str:
        return "均线交叉策略：短期均线上穿长期均线买入，下穿卖出"

    def get_parameters(self) -> Dict[str, Any]:
        return {"short_period": self.short_period, "long_period": self.long_period}

    def set_parameters(self, params: Dict[str, Any]) -> None:
        self.short_period = max(2, int(params.get("short_period", self.short_period)))
        self.long_period = max(
            self.short_period + 1, int(params.get("long_period", self.long_period))
        )

    def generate_signals(
        self, closes: np.ndarray, volumes: Optional[np.ndarray] = None
    ) -> np.ndarray:
        n = len(closes)
        signals = np.zeros(n, dtype=np.int8)
        if n < self.long_period + 2:
            return signals
        short_ma = np.full(n, np.nan)
        long_ma = np.full(n, np.nan)
        for i in range(self.short_period - 1, n):
            short_ma[i] = np.mean(closes[i - self.short_period + 1 : i + 1])
        for i in range(self.long_period - 1, n):
            long_ma[i] = np.mean(closes[i - self.long_period + 1 : i + 1])
        for i in range(self.long_period, n):
            if short_ma[i - 1] <= long_ma[i - 1] and short_ma[i] > long_ma[i]:
                signals[i] = 1
            elif short_ma[i - 1] >= long_ma[i - 1] and short_ma[i] < long_ma[i]:
                signals[i] = -1
        return signals


class MomentumStrategy(IStrategy):
    """动量策略"""

    def __init__(self, lookback: int = 20, threshold: float = 0.02):
        self.lookback = lookback
        self.threshold = threshold

    @classmethod
    def name(cls) -> str:
        return "momentum"

    @classmethod
    def description(cls) -> str:
        return "动量策略：N日涨幅超过阈值买入，跌幅超过阈值卖出"

    def get_parameters(self) -> Dict[str, Any]:
        return {"lookback": self.lookback, "threshold": self.threshold}

    def set_parameters(self, params: Dict[str, Any]) -> None:
        self.lookback = max(2, int(params.get("lookback", params.get("period", self.lookback))))
        self.threshold = float(params.get("threshold", self.threshold) or 0.02)

    def generate_signals(
        self, closes: np.ndarray, volumes: Optional[np.ndarray] = None
    ) -> np.ndarray:
        n = len(closes)
        signals = np.zeros(n, dtype=np.int8)
        if n < self.lookback + 2:
            return signals
        for i in range(self.lookback, n):
            base = closes[i - self.lookback]
            if base > 0:
                mom = (closes[i] - base) / base
                if mom > self.threshold:
                    signals[i] = 1
                elif mom < -self.threshold:
                    signals[i] = -1
        return signals


class RsiStrategy(IStrategy):
    """RSI 策略"""

    def __init__(
        self,
        rsi_period: int = 12,
        oversold: float = 18,
        overbought: float = 64,
        regime_filter_enabled: bool = False,
        allowed_entry_regimes: Optional[list[str]] = None,
        noise_filter_enabled: bool = False,
        noise_window: int = 6,
        noise_ceiling: float = 6.0,
        regime_lookback: int = 30,
        regime_volatility_window: int = 20,
        bearish_regime_threshold: float = -0.05,
        bullish_regime_threshold: float = 0.05,
        regime_volatility_threshold: float = 0.30,
        regime_break_threshold: float = 0.015,
        repair_confirmation_enabled: bool = False,
        repair_confirmation_window: int = 0,
        repair_confirmation_rebound_pct: float = 0.0,
        repair_confirmation_rsi_reclaim: float = 0.0,
        liquidity_confirmation_enabled: bool = False,
        liquidity_window: int = 8,
        liquidity_volume_floor_ratio: float = 0.8,
        structure_confirmation_enabled: bool = False,
        structure_window: int = 4,
        structure_close_location_min: float = 0.55,
        structure_body_return_min: float = 0.0015,
        mean_reversion_exit_min_hold_bars: int = 0,
        mean_reversion_exit_buffer: float = -0.002,
        max_hold_bars: int = 0,
        adverse_regime_exit_enabled: bool = False,
        adverse_exit_regimes: Optional[list[str]] = None,
        adverse_noise_ceiling: Optional[float] = None,
    ):
        self.rsi_period = rsi_period
        self.oversold = oversold
        self.overbought = overbought
        self.regime_filter_enabled = regime_filter_enabled
        self.allowed_entry_regimes = list(allowed_entry_regimes or ["bear_calm", "bear_volatile"])
        self.noise_filter_enabled = noise_filter_enabled
        self.noise_window = noise_window
        self.noise_ceiling = noise_ceiling
        self.regime_lookback = regime_lookback
        self.regime_volatility_window = regime_volatility_window
        self.bearish_regime_threshold = bearish_regime_threshold
        self.bullish_regime_threshold = bullish_regime_threshold
        self.regime_volatility_threshold = regime_volatility_threshold
        self.regime_break_threshold = regime_break_threshold
        self.repair_confirmation_enabled = repair_confirmation_enabled
        self.repair_confirmation_window = repair_confirmation_window
        self.repair_confirmation_rebound_pct = repair_confirmation_rebound_pct
        self.repair_confirmation_rsi_reclaim = repair_confirmation_rsi_reclaim
        self.liquidity_confirmation_enabled = liquidity_confirmation_enabled
        self.liquidity_window = liquidity_window
        self.liquidity_volume_floor_ratio = liquidity_volume_floor_ratio
        self.structure_confirmation_enabled = structure_confirmation_enabled
        self.structure_window = structure_window
        self.structure_close_location_min = structure_close_location_min
        self.structure_body_return_min = structure_body_return_min
        self.mean_reversion_exit_min_hold_bars = mean_reversion_exit_min_hold_bars
        self.mean_reversion_exit_buffer = mean_reversion_exit_buffer
        self.max_hold_bars = max_hold_bars
        self.adverse_regime_exit_enabled = adverse_regime_exit_enabled
        self.adverse_exit_regimes = list(adverse_exit_regimes or ["range_volatile"])
        self.adverse_noise_ceiling = adverse_noise_ceiling

    @classmethod
    def name(cls) -> str:
        return "rsi"

    @classmethod
    def description(cls) -> str:
        return "RSI策略：RSI低于超卖线买入，高于超买线卖出"

    def get_parameters(self) -> Dict[str, Any]:
        return {
            "rsi_period": self.rsi_period,
            "oversold": self.oversold,
            "overbought": self.overbought,
            "regime_filter_enabled": self.regime_filter_enabled,
            "allowed_entry_regimes": list(self.allowed_entry_regimes),
            "noise_filter_enabled": self.noise_filter_enabled,
            "noise_window": self.noise_window,
            "noise_ceiling": self.noise_ceiling,
            "regime_lookback": self.regime_lookback,
            "regime_volatility_window": self.regime_volatility_window,
            "bearish_regime_threshold": self.bearish_regime_threshold,
            "bullish_regime_threshold": self.bullish_regime_threshold,
            "regime_volatility_threshold": self.regime_volatility_threshold,
            "regime_break_threshold": self.regime_break_threshold,
            "repair_confirmation_enabled": self.repair_confirmation_enabled,
            "repair_confirmation_window": self.repair_confirmation_window,
            "repair_confirmation_rebound_pct": self.repair_confirmation_rebound_pct,
            "repair_confirmation_rsi_reclaim": self.repair_confirmation_rsi_reclaim,
            "liquidity_confirmation_enabled": self.liquidity_confirmation_enabled,
            "liquidity_window": self.liquidity_window,
            "liquidity_volume_floor_ratio": self.liquidity_volume_floor_ratio,
            "structure_confirmation_enabled": self.structure_confirmation_enabled,
            "structure_window": self.structure_window,
            "structure_close_location_min": self.structure_close_location_min,
            "structure_body_return_min": self.structure_body_return_min,
            "mean_reversion_exit_min_hold_bars": self.mean_reversion_exit_min_hold_bars,
            "mean_reversion_exit_buffer": self.mean_reversion_exit_buffer,
            "max_hold_bars": self.max_hold_bars,
            "adverse_regime_exit_enabled": self.adverse_regime_exit_enabled,
            "adverse_exit_regimes": list(self.adverse_exit_regimes),
            "adverse_noise_ceiling": self.adverse_noise_ceiling,
        }

    def set_parameters(self, params: Dict[str, Any]) -> None:
        self.rsi_period = max(2, int(params.get("rsi_period", self.rsi_period)))
        self.oversold = float(params.get("oversold", self.oversold) or self.oversold)
        self.overbought = float(params.get("overbought", self.overbought) or self.overbought)
        self.regime_filter_enabled = bool(
            params.get("regime_filter_enabled", self.regime_filter_enabled)
        )
        raw_regimes = params.get("allowed_entry_regimes", self.allowed_entry_regimes)
        if isinstance(raw_regimes, str):
            raw_regimes = [
                token.strip().lower()
                for token in raw_regimes.replace("|", ",").replace(";", ",").split(",")
                if token.strip()
            ]
        else:
            raw_regimes = [
                str(token or "").strip().lower()
                for token in list(raw_regimes or [])
                if str(token or "").strip()
            ]
        self.allowed_entry_regimes = list(raw_regimes or ["bear_calm", "bear_volatile"])
        self.noise_filter_enabled = bool(
            params.get("noise_filter_enabled", self.noise_filter_enabled)
        )
        self.noise_window = max(2, int(params.get("noise_window", self.noise_window) or self.noise_window))
        self.noise_ceiling = float(params.get("noise_ceiling", self.noise_ceiling) or self.noise_ceiling)
        self.regime_lookback = max(
            10,
            int(params.get("regime_lookback", self.regime_lookback) or self.regime_lookback),
        )
        self.regime_volatility_window = max(
            5,
            int(
                params.get("regime_volatility_window", self.regime_volatility_window)
                or self.regime_volatility_window
            ),
        )
        self.bearish_regime_threshold = float(
            params.get("bearish_regime_threshold", self.bearish_regime_threshold)
            or self.bearish_regime_threshold
        )
        self.bullish_regime_threshold = float(
            params.get("bullish_regime_threshold", self.bullish_regime_threshold)
            or self.bullish_regime_threshold
        )
        self.regime_volatility_threshold = float(
            params.get("regime_volatility_threshold", self.regime_volatility_threshold)
            or self.regime_volatility_threshold
        )
        self.regime_break_threshold = float(
            params.get("regime_break_threshold", self.regime_break_threshold)
            or self.regime_break_threshold
        )
        self.repair_confirmation_enabled = bool(
            params.get("repair_confirmation_enabled", self.repair_confirmation_enabled)
        )
        self.repair_confirmation_window = max(
            0,
            int(
                params.get(
                    "repair_confirmation_window",
                    self.repair_confirmation_window,
                )
                or self.repair_confirmation_window
            ),
        )
        self.repair_confirmation_rebound_pct = float(
            params.get(
                "repair_confirmation_rebound_pct",
                self.repair_confirmation_rebound_pct,
            )
            or self.repair_confirmation_rebound_pct
        )
        self.repair_confirmation_rsi_reclaim = float(
            params.get(
                "repair_confirmation_rsi_reclaim",
                self.repair_confirmation_rsi_reclaim,
            )
            or self.repair_confirmation_rsi_reclaim
        )
        self.liquidity_confirmation_enabled = bool(
            params.get(
                "liquidity_confirmation_enabled",
                self.liquidity_confirmation_enabled,
            )
        )
        self.liquidity_window = max(
            2,
            int(params.get("liquidity_window", self.liquidity_window) or self.liquidity_window),
        )
        self.liquidity_volume_floor_ratio = float(
            params.get(
                "liquidity_volume_floor_ratio",
                self.liquidity_volume_floor_ratio,
            )
            or self.liquidity_volume_floor_ratio
        )
        self.structure_confirmation_enabled = bool(
            params.get(
                "structure_confirmation_enabled",
                self.structure_confirmation_enabled,
            )
        )
        self.structure_window = max(
            2,
            int(params.get("structure_window", self.structure_window) or self.structure_window),
        )
        self.structure_close_location_min = float(
            params.get(
                "structure_close_location_min",
                self.structure_close_location_min,
            )
            or self.structure_close_location_min
        )
        self.structure_body_return_min = float(
            params.get(
                "structure_body_return_min",
                self.structure_body_return_min,
            )
            or self.structure_body_return_min
        )
        self.mean_reversion_exit_min_hold_bars = max(
            0,
            int(
                params.get(
                    "mean_reversion_exit_min_hold_bars",
                    self.mean_reversion_exit_min_hold_bars,
                )
                or self.mean_reversion_exit_min_hold_bars
            ),
        )
        self.mean_reversion_exit_buffer = float(
            params.get("mean_reversion_exit_buffer", self.mean_reversion_exit_buffer)
            or self.mean_reversion_exit_buffer
        )
        self.max_hold_bars = max(
            0,
            int(params.get("max_hold_bars", self.max_hold_bars) or self.max_hold_bars),
        )
        self.adverse_regime_exit_enabled = bool(
            params.get("adverse_regime_exit_enabled", self.adverse_regime_exit_enabled)
        )
        raw_adverse_regimes = params.get("adverse_exit_regimes", self.adverse_exit_regimes)
        if isinstance(raw_adverse_regimes, str):
            raw_adverse_regimes = [
                token.strip().lower()
                for token in raw_adverse_regimes.replace("|", ",").replace(";", ",").split(",")
                if token.strip()
            ]
        else:
            raw_adverse_regimes = [
                str(token or "").strip().lower()
                for token in list(raw_adverse_regimes or [])
                if str(token or "").strip()
            ]
        self.adverse_exit_regimes = list(raw_adverse_regimes or ["range_volatile"])
        raw_adverse_noise_ceiling = params.get("adverse_noise_ceiling", self.adverse_noise_ceiling)
        if raw_adverse_noise_ceiling in (None, "", [], {}):
            self.adverse_noise_ceiling = self.noise_ceiling
        else:
            self.adverse_noise_ceiling = float(raw_adverse_noise_ceiling)

    @staticmethod
    def _kline_series(
        klines: list[dict[str, Any]],
        field: str,
        *,
        fallback_field: Optional[str] = None,
    ) -> np.ndarray:
        payload = list(klines or [])
        values: list[float] = []
        for item in payload:
            raw = item.get(field)
            if raw in (None, "", [], {}):
                raw = item.get(fallback_field) if fallback_field else None
            try:
                values.append(float(raw or 0.0))
            except Exception:
                values.append(0.0)
        return np.asarray(values, dtype=float)

    def _signal_artifacts(
        self,
        closes: np.ndarray,
        *,
        volumes: Optional[np.ndarray] = None,
        opens: Optional[np.ndarray] = None,
        highs: Optional[np.ndarray] = None,
        lows: Optional[np.ndarray] = None,
    ) -> tuple[np.ndarray, dict[int, str], dict[int, str]]:
        n = len(closes)
        signals = np.zeros(n, dtype=np.int8)
        reasons: dict[int, str] = {}
        regimes: dict[int, str] = {}
        mean_window = max(6, self.rsi_period * 2)
        exit_window = max(20, mean_window - 4)
        volume_arr = (
            np.asarray(volumes, dtype=float)
            if volumes is not None and len(volumes) == n
            else np.full(n, np.nan)
        )
        open_arr = (
            np.asarray(opens, dtype=float)
            if opens is not None and len(opens) == n
            else np.full(n, np.nan)
        )
        high_arr = (
            np.asarray(highs, dtype=float)
            if highs is not None and len(highs) == n
            else np.full(n, np.nan)
        )
        low_arr = (
            np.asarray(lows, dtype=float)
            if lows is not None and len(lows) == n
            else np.full(n, np.nan)
        )
        start_index = max(
            self.rsi_period,
            mean_window - 1,
            exit_window - 1,
            self.regime_lookback,
            self.regime_volatility_window,
            self.noise_window,
            self.liquidity_window,
            self.structure_window,
        )
        if n <= start_index:
            return signals, reasons, regimes

        mean_line = _rolling_mean(closes, mean_window)
        exit_line = _rolling_mean(closes, exit_window)
        noise_ratio = _path_noise_ratio(closes, self.noise_window)
        liquidity_mean = _rolling_mean(volume_arr, self.liquidity_window)
        rsi_values = np.full(n, np.nan)

        for i in range(self.rsi_period, n):
            gains = 0.0
            losses = 0.0
            for j in range(i - self.rsi_period + 1, i + 1):
                change = closes[j] - closes[j - 1]
                if change > 0:
                    gains += change
                else:
                    losses -= change
            avg_gain = gains / self.rsi_period
            avg_loss = losses / self.rsi_period
            if avg_loss == 0:
                rsi_values[i] = 100.0
            else:
                rs = avg_gain / avg_loss
                rsi_values[i] = 100.0 - (100.0 / (1.0 + rs))

        in_position = False
        entry_index = -1
        pending_confirmation = False
        confirmation_start_index = -1
        confirmation_anchor_index = -1
        confirmation_anchor_price = 0.0
        allowed_entry_regimes = set(self.allowed_entry_regimes)
        adverse_exit_regimes = set(self.adverse_exit_regimes)
        for i in range(start_index, n):
            mean_value = mean_line[i]
            if not np.isfinite(mean_value) or mean_value <= 0:
                continue
            current_rsi = rsi_values[i]
            if not np.isfinite(current_rsi):
                continue
            current_regime = _reversal_regime_label(
                closes,
                i,
                lookback=self.regime_lookback,
                volatility_window=self.regime_volatility_window,
                bearish_threshold=self.bearish_regime_threshold,
                bullish_threshold=self.bullish_regime_threshold,
                volatility_threshold=self.regime_volatility_threshold,
            )
            regimes[i] = current_regime
            deviation = (closes[i] - mean_value) / mean_value
            current_noise = noise_ratio[i]
            regime_ready = (
                (not self.regime_filter_enabled) or current_regime in allowed_entry_regimes
            )
            noise_ready = True
            if self.noise_filter_enabled:
                noise_ready = bool(
                    np.isfinite(current_noise) and current_noise <= self.noise_ceiling
                )
            base_entry_ready = (
                current_rsi < self.oversold
                and deviation <= -0.015
                and regime_ready
                and noise_ready
            )
            entry_ready = False
            if not in_position:
                if self.repair_confirmation_enabled:
                    if base_entry_ready:
                        if not pending_confirmation:
                            pending_confirmation = True
                            confirmation_start_index = i
                            confirmation_anchor_index = i
                            confirmation_anchor_price = float(closes[i])
                        elif closes[i] <= confirmation_anchor_price:
                            confirmation_start_index = i
                            confirmation_anchor_index = i
                            confirmation_anchor_price = float(closes[i])
                    if pending_confirmation:
                        if closes[i] < confirmation_anchor_price:
                            confirmation_anchor_index = i
                            confirmation_anchor_price = float(closes[i])
                        confirmation_window_expired = (
                            self.repair_confirmation_window > 0
                            and confirmation_start_index >= 0
                            and (i - confirmation_start_index) > self.repair_confirmation_window
                        )
                        if (not regime_ready) or (not noise_ready) or confirmation_window_expired:
                            pending_confirmation = False
                            confirmation_start_index = -1
                            confirmation_anchor_index = -1
                            confirmation_anchor_price = 0.0
                        else:
                            liquidity_ready = True
                            if self.liquidity_confirmation_enabled:
                                current_volume = volume_arr[i]
                                baseline_volume = liquidity_mean[i]
                                if not (
                                    np.isfinite(current_volume)
                                    and np.isfinite(baseline_volume)
                                    and baseline_volume > 0.0
                                ):
                                    liquidity_ready = True
                                else:
                                    liquidity_ready = bool(
                                        current_volume
                                        >= baseline_volume * self.liquidity_volume_floor_ratio
                                    )
                            structure_ready = True
                            if self.structure_confirmation_enabled:
                                current_open = open_arr[i] if np.isfinite(open_arr[i]) else (closes[i - 1] if i >= 1 else closes[i])
                                current_high = high_arr[i] if np.isfinite(high_arr[i]) else max(closes[i], current_open)
                                current_low = low_arr[i] if np.isfinite(low_arr[i]) else min(closes[i], current_open)
                                prior_lows = low_arr[max(0, i - self.structure_window) : i]
                                prior_lows = prior_lows[np.isfinite(prior_lows)]
                                reference_low = (
                                    float(np.min(prior_lows))
                                    if prior_lows.size
                                    else float(current_low)
                                )
                                bar_range = max(float(current_high) - float(current_low), 1e-6)
                                close_location = (float(closes[i]) - float(current_low)) / bar_range
                                body_return = (
                                    (float(closes[i]) - float(current_open))
                                    / max(abs(float(current_open)), 1e-6)
                                )
                                structure_ready = bool(
                                    close_location >= self.structure_close_location_min
                                    and body_return >= self.structure_body_return_min
                                    and current_low >= reference_low
                                    and i >= 1
                                    and closes[i] >= closes[i - 1]
                                )
                            rebound_ready = (
                                confirmation_anchor_price > 0.0
                                and i > confirmation_anchor_index
                                and current_rsi >= self.repair_confirmation_rsi_reclaim
                                and i >= 1
                                and closes[i] >= closes[i - 1]
                                and (closes[i] - confirmation_anchor_price)
                                / confirmation_anchor_price
                                >= self.repair_confirmation_rebound_pct
                                and liquidity_ready
                                and structure_ready
                            )
                            if rebound_ready:
                                entry_ready = True
                                pending_confirmation = False
                                confirmation_start_index = -1
                                confirmation_anchor_index = -1
                                confirmation_anchor_price = 0.0
                else:
                    entry_ready = base_entry_ready

            regime_break = False
            exit_mean = exit_line[i]
            if np.isfinite(exit_mean) and exit_mean > 0 and i >= 1:
                regime_break = (
                    closes[i] < exit_mean * (1.0 - self.regime_break_threshold)
                    and closes[i] < closes[i - 1]
                )
            bars_held = i - entry_index if entry_index >= 0 else 0
            mean_reversion_exit = (
                bars_held >= self.mean_reversion_exit_min_hold_bars
                and deviation >= self.mean_reversion_exit_buffer
            )
            time_stop_exit = self.max_hold_bars > 0 and bars_held >= self.max_hold_bars
            adverse_noise_ceiling = (
                self.adverse_noise_ceiling
                if self.adverse_noise_ceiling not in (None, "", [], {})
                else self.noise_ceiling
            )
            adverse_regime_exit = (
                self.adverse_regime_exit_enabled
                and bars_held >= 1
                and current_regime in adverse_exit_regimes
                and i >= 1
                and closes[i] < closes[i - 1]
                and np.isfinite(current_noise)
                and current_noise >= float(adverse_noise_ceiling or self.noise_ceiling)
            )
            exit_ready = (
                current_rsi > self.overbought
                or mean_reversion_exit
                or regime_break
                or time_stop_exit
                or adverse_regime_exit
            )

            if not in_position and entry_ready:
                signals[i] = 1
                reasons[i] = f"oversold_repair_entry:{current_regime}"
                in_position = True
                entry_index = i
            elif in_position and exit_ready:
                signals[i] = -1
                if adverse_regime_exit:
                    reasons[i] = f"adverse_regime_exit:{current_regime}"
                elif regime_break:
                    reasons[i] = f"regime_break_exit:{current_regime}"
                elif current_rsi > self.overbought:
                    reasons[i] = "rsi_reset_exit"
                elif time_stop_exit:
                    reasons[i] = "time_stop_exit"
                else:
                    reasons[i] = "mean_reversion_exit"
                in_position = False
                entry_index = -1
        return signals, reasons, regimes

    def generate_signals(
        self, closes: np.ndarray, volumes: Optional[np.ndarray] = None
    ) -> np.ndarray:
        signals, _reasons, _regimes = self._signal_artifacts(closes, volumes=volumes)
        return signals

    def generate_entry_exit_masks_from_klines(self, klines: list[dict]) -> tuple[np.ndarray, np.ndarray]:
        closes = self._kline_series(klines, "close")
        signals, _reasons, _regimes = self._signal_artifacts(
            closes,
            volumes=self._kline_series(klines, "volume"),
            opens=self._kline_series(klines, "open", fallback_field="close"),
            highs=self._kline_series(klines, "high", fallback_field="close"),
            lows=self._kline_series(klines, "low", fallback_field="close"),
        )
        return (signals == 1), (signals == -1)

    def generate_signal_events_from_klines(self, klines: list[dict]) -> Optional[list[dict[str, Any]]]:
        closes = self._kline_series(klines, "close")
        signals, reasons, regimes = self._signal_artifacts(
            closes,
            volumes=self._kline_series(klines, "volume"),
            opens=self._kline_series(klines, "open", fallback_field="close"),
            highs=self._kline_series(klines, "high", fallback_field="close"),
            lows=self._kline_series(klines, "low", fallback_field="close"),
        )
        events: list[dict[str, Any]] = []
        for index, raw_signal in enumerate(signals):
            signal = int(raw_signal)
            if signal == 0:
                continue
            regime = regimes.get(index, "unknown")
            events.append(
                {
                    "index": index,
                    "signal": signal,
                    "action": "enter" if signal > 0 else "exit",
                    "reason": reasons.get(index, "rsi_stateful_signal"),
                    "regime": regime,
                }
            )
        return events


class BuyAndHoldStrategy(IStrategy):
    """买入持有策略"""

    @classmethod
    def name(cls) -> str:
        return "buy_and_hold"

    @classmethod
    def description(cls) -> str:
        return "买入持有策略：第一天买入，一直持有到最后"

    def get_parameters(self) -> Dict[str, Any]:
        return {}

    def set_parameters(self, params: Dict[str, Any]) -> None:
        pass

    def generate_signals(
        self, closes: np.ndarray, volumes: Optional[np.ndarray] = None
    ) -> np.ndarray:
        n = len(closes)
        signals = np.zeros(n, dtype=np.int8)
        if n > 1:
            signals[0] = 1  # 第一天买入
        return signals
