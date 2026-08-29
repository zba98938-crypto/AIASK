"""DSL 规则策略。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional

import numpy as np

from ..strategy_dsl import build_close_volume_frame, build_ohlcv_frame, evaluate_dsl_masks, normalize_strategy_dsl
from .strategy_base import IStrategy, StrategySignalEvent


@dataclass
class _PositionState:
    in_position: bool = False
    entry_price: float = 0.0
    highest_price: float = 0.0
    holding_days: int = 0
    cooldown_left: int = 0
    units: float = 0.0


class DslRuleStrategy(IStrategy):
    def __init__(
        self,
        dsl: Optional[dict] = None,
        risk_rules: Optional[dict] = None,
        runtime_playbook: Optional[dict] = None,
    ):
        self.dsl = normalize_strategy_dsl(dsl or {
            "entry": {"any": [{"op": "gt", "left": {"field": "close"}, "right": {"indicator": "sma", "field": "close", "window": 20}}]},
            "exit": {"any": [{"op": "lt", "left": {"field": "close"}, "right": {"indicator": "sma", "field": "close", "window": 20}}]},
        })
        self.risk_rules = dict(risk_rules or {})
        self.runtime_playbook = dict(runtime_playbook or {})

    @classmethod
    def name(cls) -> str:
        return "dsl_rule"

    @classmethod
    def description(cls) -> str:
        return "DSL 规则策略：基于条件表达式组合生成买卖信号"

    def get_parameters(self) -> Dict[str, Any]:
        return {
            "dsl": self.dsl,
            "risk_rules": self.risk_rules,
            "runtime_playbook": self.runtime_playbook,
        }

    def set_parameters(self, params: Dict[str, Any]) -> None:
        payload = dict(params or {})
        self.dsl = normalize_strategy_dsl(payload.get("dsl") or self.dsl)
        self.risk_rules = dict(payload.get("risk_rules") or self.risk_rules or {})
        self.runtime_playbook = dict(payload.get("runtime_playbook") or self.runtime_playbook or {})

    def _exit_policy(self) -> Dict[str, Any]:
        runtime = dict(self.runtime_playbook or {})
        policy = dict(runtime.get("exit_policy") or {})
        if not policy and self.risk_rules:
            policy = {
                "initial_stop_loss_pct": self.risk_rules.get("stop_loss_pct") or self.risk_rules.get("stop_loss"),
                "take_profit_pct": self.risk_rules.get("take_profit_pct") or self.risk_rules.get("take_profit"),
                "time_stop_days": self.risk_rules.get("max_holding_days"),
            }
        return policy

    def _reentry_policy(self) -> Dict[str, Any]:
        runtime = dict(self.runtime_playbook or {})
        policy = dict(runtime.get("reentry_policy") or {})
        if not policy and self.risk_rules:
            policy = {
                "cooldown_days": self.risk_rules.get("cooldown_days") or self.risk_rules.get("cooldown_window_days"),
            }
        return policy

    def _adverse_move_policy(self) -> Dict[str, Any]:
        runtime = dict(self.runtime_playbook or {})
        return dict(runtime.get("adverse_move_policy") or {})

    def _cooldown_by_exit_reason(self) -> Dict[str, int]:
        runtime = dict(self.runtime_playbook or {})
        payload = dict(runtime.get("cooldown_by_exit_reason") or {})
        return {
            str(key or "").strip().lower(): int(value or 0)
            for key, value in payload.items()
            if str(key or "").strip()
        }

    def _build_signal_events(
        self,
        frame,
        entry_mask: np.ndarray,
        exit_mask: np.ndarray,
    ) -> list[StrategySignalEvent]:
        opens = np.asarray(frame["open"].to_numpy(dtype=float), dtype=float)
        highs = np.asarray(frame["high"].to_numpy(dtype=float), dtype=float)
        lows = np.asarray(frame["low"].to_numpy(dtype=float), dtype=float)
        closes = np.asarray(frame["close"].to_numpy(dtype=float), dtype=float)
        if len(closes) <= 0:
            return []

        exit_policy = self._exit_policy()
        reentry_policy = self._reentry_policy()
        adverse_move_policy = self._adverse_move_policy()
        cooldown_by_exit_reason = self._cooldown_by_exit_reason()
        stop_execution_mode = str(self.runtime_playbook.get("stop_execution_mode") or "").strip().lower()
        ohlc_stop_enabled = "ohlc" in stop_execution_mode or "gap_aware" in stop_execution_mode

        stop_loss_pct = float(
            exit_policy.get("initial_stop_loss_pct")
            or self.risk_rules.get("stop_loss_pct")
            or self.risk_rules.get("stop_loss")
            or 0.0
        )
        take_profit_pct = float(
            exit_policy.get("take_profit_pct")
            or self.risk_rules.get("take_profit_pct")
            or self.risk_rules.get("take_profit")
            or 0.0
        )
        trailing_stop_pct = float(exit_policy.get("trailing_stop_pct") or 0.0)
        trailing_activation_profit_pct = float(
            exit_policy.get("trailing_activation_profit_pct")
            or max(abs(stop_loss_pct), 0.0)
            or 0.0
        )
        max_holding_days = int(
            exit_policy.get("time_stop_days")
            or self.risk_rules.get("max_holding_days")
            or 0
        )
        cooldown_days = int(
            reentry_policy.get("cooldown_days")
            or self.risk_rules.get("cooldown_days")
            or self.risk_rules.get("cooldown_window_days")
            or 0
        )
        loss_bands = list(adverse_move_policy.get("loss_bands") or [])
        events: list[StrategySignalEvent] = []
        state = _PositionState()
        reduced_thresholds: set[tuple[int, float]] = set()

        for idx in range(len(closes)):
            if state.cooldown_left > 0 and not state.in_position:
                state.cooldown_left -= 1
            price = float(closes[idx] or 0.0)
            day_open = float(opens[idx] or price)
            day_high = float(highs[idx] or price)
            day_low = float(lows[idx] or price)
            if not state.in_position:
                if entry_mask[idx] and price > 0 and state.cooldown_left <= 0:
                    state.in_position = True
                    state.entry_price = price
                    state.highest_price = day_high if ohlc_stop_enabled else price
                    state.holding_days = 0
                    state.units = 1.0
                    reduced_thresholds.clear()
                    events.append({"index": idx, "signal": 1, "action": "enter", "units": 1.0})
                continue

            state.holding_days += 1
            state.highest_price = max(state.highest_price, day_high if ohlc_stop_enabled else price)
            pnl_ratio = (price / state.entry_price - 1.0) if state.entry_price > 0 else 0.0
            exit_action: Optional[str] = None
            if stop_loss_pct > 0 and state.entry_price > 0:
                stop_price = state.entry_price * (1.0 - abs(stop_loss_pct))
                if ohlc_stop_enabled and day_open > 0 and day_open <= stop_price:
                    exit_action = "gap_through_stop"
                elif ohlc_stop_enabled and day_low > 0 and day_low <= stop_price:
                    exit_action = "stop_loss"
                elif pnl_ratio <= -abs(stop_loss_pct):
                    exit_action = "stop_loss"
            if exit_action is None and take_profit_pct > 0 and state.entry_price > 0:
                take_profit_price = state.entry_price * (1.0 + abs(take_profit_pct))
                if ohlc_stop_enabled and day_high >= take_profit_price:
                    exit_action = "take_profit"
                elif pnl_ratio >= abs(take_profit_pct):
                    exit_action = "take_profit"
            if exit_action is None and trailing_stop_pct > 0 and state.entry_price > 0 and state.highest_price > 0:
                peak_profit = state.highest_price / state.entry_price - 1.0
                trailing_price = state.highest_price * (1.0 - abs(trailing_stop_pct))
                drawdown_from_peak = price / state.highest_price - 1.0
                intraday_trailing_hit = ohlc_stop_enabled and day_low > 0 and day_low <= trailing_price
                if peak_profit >= trailing_activation_profit_pct and (drawdown_from_peak <= -abs(trailing_stop_pct) or intraday_trailing_hit):
                    exit_action = "trailing_stop"

            reduce_units = 0.0
            reduce_label: Optional[str] = None
            for band in loss_bands:
                try:
                    threshold = abs(float(band.get("threshold_pct") or band.get("loss_pct") or 0.0))
                except Exception:
                    threshold = 0.0
                action = str(band.get("action") or "").strip().lower()
                if threshold <= 0 or action in {"", "hold"}:
                    continue
                if pnl_ratio > -threshold:
                    continue
                threshold_key = (idx, threshold)
                if action == "reduce":
                    if threshold_key in reduced_thresholds or state.units <= 0.5:
                        continue
                    reduce_units = min(0.5, state.units)
                    reduce_label = str(band.get("label") or action).strip().lower() or "reduce"
                    reduced_thresholds.add(threshold_key)
                    break
                if action in {"exit", "freeze_reentry"}:
                    exit_action = str(band.get("label") or action).strip().lower() or action
                    break
            if exit_action is None and max_holding_days > 0 and state.holding_days >= max_holding_days:
                exit_action = "time_stop"

            if reduce_units > 0:
                state.units = max(0.0, state.units - reduce_units)
                events.append(
                    {
                        "index": idx,
                        "signal": -1,
                        "action": "reduce",
                        "reason": reduce_label,
                        "units": reduce_units,
                        "remaining_units": state.units,
                    }
                )
                if state.units <= 0:
                    state.in_position = False
                    state.entry_price = 0.0
                    state.highest_price = 0.0
                    state.holding_days = 0
                    state.cooldown_left = max(state.cooldown_left, cooldown_days)
                continue

            if exit_mask[idx] and exit_action is None:
                exit_action = "dsl_exit"
            if exit_action is not None:
                events.append(
                    {
                        "index": idx,
                        "signal": -1,
                        "action": "exit",
                        "reason": exit_action,
                        "units": state.units or 1.0,
                    }
                )
                state.in_position = False
                state.entry_price = 0.0
                state.highest_price = 0.0
                state.holding_days = 0
                state.units = 0.0
                resolved_cooldown = cooldown_by_exit_reason.get(exit_action, cooldown_days)
                state.cooldown_left = max(state.cooldown_left, resolved_cooldown)

        return events

    def generate_signal_events(self, closes: np.ndarray, volumes: Optional[np.ndarray] = None) -> Optional[list[StrategySignalEvent]]:
        frame = build_close_volume_frame(closes, volumes)
        entry_mask, exit_mask = evaluate_dsl_masks(frame, self.dsl)
        return self._build_signal_events(frame, entry_mask, exit_mask)

    def generate_signal_events_from_klines(self, klines: list[dict]) -> Optional[list[StrategySignalEvent]]:
        frame = build_ohlcv_frame(list(klines or []))
        entry_mask, exit_mask = evaluate_dsl_masks(frame, self.dsl)
        return self._build_signal_events(frame, entry_mask, exit_mask)

    def generate_signals(self, closes: np.ndarray, volumes: Optional[np.ndarray] = None) -> np.ndarray:
        frame = build_close_volume_frame(closes, volumes)
        entry_mask, exit_mask = evaluate_dsl_masks(frame, self.dsl)
        events = self._build_signal_events(frame, entry_mask, exit_mask)
        signals = np.zeros(len(frame), dtype=np.int8)
        for event in events:
            idx = int(event.get("index") or 0)
            signal = int(event.get("signal") or 0)
            if 0 <= idx < len(signals):
                if signal > 0:
                    signals[idx] = 1
                elif signal < 0 and signals[idx] == 0:
                    signals[idx] = -1
        return signals

    def generate_entry_exit_masks_from_klines(self, klines: list[dict]) -> tuple[np.ndarray, np.ndarray]:
        frame = build_ohlcv_frame(list(klines or []))
        entry_mask, exit_mask = evaluate_dsl_masks(frame, self.dsl)
        events = self._build_signal_events(frame, entry_mask, exit_mask)
        adjusted_entry = np.zeros(len(frame), dtype=bool)
        adjusted_exit = np.zeros(len(frame), dtype=bool)
        for event in events:
            idx = int(event.get("index") or 0)
            signal = int(event.get("signal") or 0)
            action = str(event.get("action") or "").strip().lower()
            if 0 <= idx < len(frame):
                if signal > 0:
                    adjusted_entry[idx] = True
                elif signal < 0 and action == "exit":
                    adjusted_exit[idx] = True
        overlap = adjusted_entry & adjusted_exit
        adjusted_entry[overlap] = False
        adjusted_exit[overlap] = False
        return adjusted_entry, adjusted_exit
