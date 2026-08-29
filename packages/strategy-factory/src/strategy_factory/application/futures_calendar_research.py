"""Futures calendar-spread research adapter for SC crude oil.

This module keeps the futures research path separate from the stock-oriented
factory backtest runtime. It rebuilds the SC curve from local spread inputs,
runs directional / spread backtests, produces Gate-3 candidate bundles, and
prepares a structured research_context payload that can be injected into the
existing StrategyLLMProvider.
"""



from __future__ import annotations

import asyncio
import json
import math
from copy import deepcopy
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import numpy as np
import pandas as pd


def _resolve_repo_root() -> Path:
    current = Path(__file__).resolve()
    for parent in current.parents:
        if (parent / "package.json").exists() and (parent / "docs").is_dir():
            return parent
    return current.parents[5]


def _resolve_sc_pack_artifact(pattern: str, fallback: str) -> Path:
    matches = sorted(DEFAULT_SC_PACK_DIR.glob(pattern))
    if matches:
        return matches[0]
    return DEFAULT_SC_PACK_DIR / fallback


DEFAULT_SC_PACK_DIR = _resolve_repo_root() / "docs" / "原油" / "ai_ready"
DEFAULT_SC_DATA_PATH = _resolve_sc_pack_artifact(
    "tables/timeseries/dataset_*_sc_spread_timeseries_all_daily.csv",
    "tables/timeseries/dataset_18_sc_spread_timeseries_all_daily.csv",
)
DEFAULT_SC_MEMO_PATH = _resolve_sc_pack_artifact(
    "strategy_notes/doc_*_crude_oil_strategy_memo.md",
    "strategy_notes/doc_05_crude_oil_strategy_memo.md",
)
DEFAULT_SC_NOTES_PATH = _resolve_sc_pack_artifact(
    "strategy_notes/doc_*_sc_spread_data_notes.md",
    "strategy_notes/doc_07_sc_spread_data_notes.md",
)
DEFAULT_OUTPUT_DIR = DEFAULT_SC_PACK_DIR / "outputs" / "sc_calendar_research"
CAPITAL_BUCKETS = (1_000_000.0, 5_000_000.0, 10_000_000.0, 30_000_000.0, 100_000_000.0)
GENERALIZATION_UNDERLYINGS = (
    ("I", "铁矿石"),
    ("EC", "集运"),
    ("CU", "沪铜"),
    ("AL", "沪铝"),
    ("ZN", "沪锌"),
    ("NI", "沪镍"),
)
_EMPTY_VALUES = (None, "", [], {})


@dataclass(frozen=True)
class ExecutionProfile:
    commission_rate: float
    slippage_bps: float
    market_impact_bps: float
    margin_rate: float
    contract_multiplier: int
    capacity_participation_rate: float
    liquidity_bucket: str
    max_contracts_per_rebalance: int
    liquidity_reference_contracts: int
    far_month_liquidity_haircut: float
    margin_budget_fraction: float
    drawdown_budget_fraction: float
    market_ruleset: str = "cn_futures"


@dataclass(frozen=True)
class TrendConfig:
    leg_month: int
    carry_threshold: float
    volatility_cap: float
    price_to_ma60_cap: float
    stop_loss_pct: float
    near_premium_floor: float = -2.0
    exit_premium_floor: float = -3.0
    ma_fast_window: int = 20
    ma_slow_window: int = 60
    slope_window: int = 10

    @property
    def family_name(self) -> str:
        return "trend"

    @property
    def strategy_code(self) -> str:
        carry_token = str(self.carry_threshold).replace(".", "p")
        vol_token = str(self.volatility_cap).replace(".", "p")
        stop_token = str(self.stop_loss_pct).replace(".", "p")
        return f"trend_m{self.leg_month}_carry{carry_token}_vol{vol_token}_stop{stop_token}"


@dataclass(frozen=True)
class SpreadConfig:
    leg_name: str
    entry_z_low: float
    entry_z_high: float
    exit_z: float
    stop_move: float
    slope_floor: float
    max_holding_days: int = 40
    require_price_trend: bool = True
    z_window: int = 40
    near_premium_floor: float = -2.0
    exit_premium_floor: float = -3.0

    @property
    def family_name(self) -> str:
        return "spread"

    @property
    def strategy_code(self) -> str:
        entry_low_token = str(self.entry_z_low).replace(".", "p").replace("-", "n")
        entry_high_token = str(self.entry_z_high).replace(".", "p").replace("-", "n")
        exit_token = str(self.exit_z).replace(".", "p").replace("-", "n")
        stop_token = str(abs(self.stop_move)).replace(".", "p")
        hold_token = str(int(self.max_holding_days))
        trend_token = "trend" if self.require_price_trend else "carry"
        return (
            f"spread_{self.leg_name}_band{entry_low_token}_to_{entry_high_token}"
            f"_exit{exit_token}_stop{stop_token}_hold{hold_token}_{trend_token}"
        )


@dataclass(frozen=True)
class StrategyResult:
    family: str
    strategy_code: str
    name: str
    config: dict[str, Any]
    execution_profile: dict[str, Any]
    summary: dict[str, Any]
    regime_panel: dict[str, Any]
    capacity_panel: list[dict[str, Any]]
    trades: list[dict[str, Any]]
    candidate: dict[str, Any]
    raw_dsl: dict[str, Any]
    compiled_dsl: dict[str, Any]


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return int(default)


def _normalize_contract_code(contract_code: str) -> str:
    token = str(contract_code or "").strip().lower()
    if token:
        return token
    return "sc0000"


def _shift_contract_code(contract_code: str, months: int) -> str:
    normalized = _normalize_contract_code(contract_code)
    prefix = "".join(ch for ch in normalized if not ch.isdigit()) or "sc"
    digits = "".join(ch for ch in normalized if ch.isdigit())
    if len(digits) < 4:
        return normalized
    year = 2000 + int(digits[:2])
    month = int(digits[2:4])
    absolute_month = (year * 12 + (month - 1)) + int(months)
    shifted_year = absolute_month // 12
    shifted_month = absolute_month % 12 + 1
    return f"{prefix}{str(shifted_year % 100).zfill(2)}{str(shifted_month).zfill(2)}"


def _annualized_return(equity: pd.Series) -> float:
    if equity.empty:
        return 0.0
    initial = _safe_float(equity.iloc[0], 0.0)
    final = _safe_float(equity.iloc[-1], 0.0)
    if initial <= 0 or final <= 0:
        return -1.0
    periods = max(len(equity), 1)
    return float((final / initial) ** (252.0 / periods) - 1.0)


def _sharpe_ratio(returns: pd.Series, annualization: float = 252.0) -> float:
    series = pd.Series(returns).replace([np.inf, -np.inf], np.nan).dropna()
    if len(series) < 2:
        return 0.0
    std = float(series.std(ddof=1) or 0.0)
    if std <= 1e-12:
        return 0.0
    return float(series.mean() / std * math.sqrt(annualization))


def _max_drawdown(equity: pd.Series) -> float:
    if equity.empty:
        return 0.0
    running_peak = equity.cummax().replace(0.0, np.nan)
    drawdown = equity.div(running_peak).replace([np.inf, -np.inf], np.nan) - 1.0
    return float(drawdown.min(skipna=True) or 0.0)


def _trade_win_rate(trades: list[dict[str, Any]]) -> float:
    closed = [trade for trade in trades if trade.get("status") == "closed"]
    if not closed:
        return 0.0
    wins = sum(1 for trade in closed if _safe_float(trade.get("net_pnl")) > 0)
    return float(wins / len(closed))


def _forward_sharpe_5d(forward_returns: pd.Series) -> float:
    return _sharpe_ratio(forward_returns, annualization=252.0 / 5.0)


def _markdown_table(rows: list[dict[str, Any]], columns: list[tuple[str, str]]) -> str:
    if not rows:
        return "_No rows._"
    header = "| " + " | ".join(label for label, _ in columns) + " |"
    divider = "| " + " | ".join("---" for _ in columns) + " |"
    body_lines = []
    for row in rows:
        body_lines.append(
            "| "
            + " | ".join(str(row.get(key, "")) for _, key in columns)
            + " |"
        )
    return "\n".join([header, divider, *body_lines])


def _round_dict_values(payload: dict[str, Any], *, digits: int = 6) -> dict[str, Any]:
    rounded: dict[str, Any] = {}
    for key, value in payload.items():
        if isinstance(value, float):
            rounded[key] = round(value, digits)
        elif isinstance(value, dict):
            rounded[key] = _round_dict_values(value, digits=digits)
        elif isinstance(value, list):
            rounded[key] = [
                _round_dict_values(item, digits=digits) if isinstance(item, dict) else round(item, digits) if isinstance(item, float) else item
                for item in value
            ]
        else:
            rounded[key] = value
    return rounded


def _ensure_output_dir(output_dir: Path) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    return output_dir


def _to_ohlcv_projection(series: pd.Series) -> pd.DataFrame:
    close = pd.Series(series).astype(float)
    open_ = close.shift(1).fillna(close)
    high = pd.concat([open_, close], axis=1).max(axis=1)
    low = pd.concat([open_, close], axis=1).min(axis=1)
    return pd.DataFrame(
        {
            "open": open_,
            "high": high,
            "low": low,
            "close": close,
            "volume": np.full(len(close), 1.0),
        }
    )


def _load_optional_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except Exception:
        return ""

class FuturesCalendarResearchAdapter:
    """Local SC futures calendar adapter with report / candidate outputs."""

    def __init__(
        self,
        *,
        data_path: Path = DEFAULT_SC_DATA_PATH,
        output_dir: Path = DEFAULT_OUTPUT_DIR,
        memo_path: Path = DEFAULT_SC_MEMO_PATH,
        notes_path: Path = DEFAULT_SC_NOTES_PATH,
    ) -> None:
        self.data_path = Path(data_path)
        self.output_dir = Path(output_dir)
        self.memo_path = Path(memo_path)
        self.notes_path = Path(notes_path)
        self._memo_text = _load_optional_text(self.memo_path)
        self._notes_text = _load_optional_text(self.notes_path)
        self._contract_lookup_cache: dict[int, tuple[list[dict[str, float]], list[dict[str, int]]]] = {}

    @staticmethod
    def load_curve_frame(data_path: Path) -> pd.DataFrame:
        frame = pd.read_csv(data_path, parse_dates=["trading_date"]).sort_values("trading_date").reset_index(drop=True)
        for month_index in range(2, 13):
            prev_price = "price_01" if month_index == 2 else f"price_{month_index - 1:02d}"
            spread_name = f"spread_{month_index - 1}_{month_index}"
            frame[f"price_{month_index:02d}"] = frame[prev_price] - frame[spread_name]
        for month_index in range(2, 13):
            frame[f"contract_{month_index:02d}"] = [
                _shift_contract_code(contract_code, month_index - 1)
                for contract_code in frame["contract_01"]
            ]
        return frame

    @staticmethod
    def add_features(frame: pd.DataFrame) -> pd.DataFrame:
        enriched = frame.copy()
        enriched["regime"] = np.where(
            enriched["spread_1_2"] > 0,
            "backwardation",
            "contango_or_flat",
        )
        enriched["backwardation_flag"] = (enriched["spread_1_2"] > 0).astype(int)
        enriched["curve_monotone"] = (
            (enriched["spread_1_2"] >= enriched["spread_2_3"])
            & (enriched["spread_2_3"] >= enriched["spread_3_4"])
        )
        enriched["curve_expansion"] = (
            (enriched["spread_1_2"].diff(5) > 0)
            & (enriched["spread_2_3"].diff(5) > 0)
            & (enriched["spread_3_4"].diff(5) > 0)
        )
        enriched["curve_slope_1_4"] = enriched["spread_1_2"] + enriched["spread_2_3"] + enriched["spread_3_4"]
        enriched["spread_1_3"] = enriched["spread_1_2"] + enriched["spread_2_3"]
        enriched["spread_2_4"] = enriched["spread_2_3"] + enriched["spread_3_4"]
        enriched["roll_node"] = (enriched["contract_01"] != enriched["contract_01"].shift(1)).astype(int)
        next_roll = (enriched["contract_01"] != enriched["contract_01"].shift(-1)).astype(int)
        enriched["roll_next_3d"] = next_roll.iloc[::-1].rolling(3, min_periods=1).max().iloc[::-1].fillna(0).astype(int)
        enriched["roll_next_5d"] = next_roll.iloc[::-1].rolling(5, min_periods=1).max().iloc[::-1].fillna(0).astype(int)
        for column in (
            "price_01",
            "price_02",
            "price_03",
            "price_04",
            "spread_1_2",
            "spread_2_3",
            "spread_3_4",
            "spread_1_3",
            "spread_2_4",
        ):
            enriched[f"{column}_ma20"] = enriched[column].rolling(20).mean()
            enriched[f"{column}_ma60"] = enriched[column].rolling(60).mean()
            enriched[f"{column}_std20"] = enriched[column].rolling(20).std()
            enriched[f"{column}_z40"] = (
                (enriched[column] - enriched[column].rolling(40).mean())
                / enriched[column].rolling(40).std().replace(0.0, np.nan)
            )
            enriched[f"{column}_slope10"] = enriched[column].diff(10) / 10.0
        for column in ("price_03", "price_04"):
            price_series = enriched[column].replace(0.0, np.nan)
            enriched[f"{column}_vol20"] = price_series.pct_change().rolling(20).std()
            enriched[f"{column}_price_to_ma60"] = price_series / enriched[f"{column}_ma60"].replace(0.0, np.nan)
        enriched["price_uptrend"] = (
            (enriched["price_01"] > enriched["price_01_ma20"])
            & (enriched["price_01_ma20"] > enriched["price_01_ma60"])
        )
        enriched["price_downtrend"] = (
            (enriched["price_01"] < enriched["price_01_ma20"])
            & (enriched["price_01_ma20"] < enriched["price_01_ma60"])
        )
        return enriched

    @staticmethod
    def _trend_execution_profile(config: TrendConfig) -> ExecutionProfile:
        if config.leg_month == 3:
            return ExecutionProfile(
                commission_rate=0.00005,
                slippage_bps=3.2,
                market_impact_bps=1.6,
                margin_rate=0.15,
                contract_multiplier=1000,
                capacity_participation_rate=0.11,
                liquidity_bucket="far_month_medium",
                max_contracts_per_rebalance=26,
                liquidity_reference_contracts=220,
                far_month_liquidity_haircut=0.82,
                margin_budget_fraction=0.88,
                drawdown_budget_fraction=0.26,
            )
        return ExecutionProfile(
            commission_rate=0.00005,
            slippage_bps=3.5,
            market_impact_bps=1.8,
            margin_rate=0.15,
            contract_multiplier=1000,
            capacity_participation_rate=0.10,
            liquidity_bucket="far_month_light",
            max_contracts_per_rebalance=22,
            liquidity_reference_contracts=180,
            far_month_liquidity_haircut=0.78,
            margin_budget_fraction=0.85,
            drawdown_budget_fraction=0.25,
        )

    @staticmethod
    def _spread_execution_profile(config: SpreadConfig) -> ExecutionProfile:
        if config.leg_name == "1_2":
            return ExecutionProfile(
                commission_rate=0.00005,
                slippage_bps=3.5,
                market_impact_bps=1.8,
                margin_rate=0.15,
                contract_multiplier=1000,
                capacity_participation_rate=0.10,
                liquidity_bucket="near_month_high",
                max_contracts_per_rebalance=24,
                liquidity_reference_contracts=210,
                far_month_liquidity_haircut=0.95,
                margin_budget_fraction=0.70,
                drawdown_budget_fraction=0.18,
            )
        if config.leg_name == "2_3":
            return ExecutionProfile(
                commission_rate=0.00004,
                slippage_bps=2.8,
                market_impact_bps=1.2,
                margin_rate=0.15,
                contract_multiplier=1000,
                capacity_participation_rate=0.11,
                liquidity_bucket="near_mid_high",
                max_contracts_per_rebalance=22,
                liquidity_reference_contracts=220,
                far_month_liquidity_haircut=0.90,
                margin_budget_fraction=0.74,
                drawdown_budget_fraction=0.20,
            )
        if config.leg_name == "3_4":
            return ExecutionProfile(
                commission_rate=0.00004,
                slippage_bps=2.6,
                market_impact_bps=1.1,
                margin_rate=0.15,
                contract_multiplier=1000,
                capacity_participation_rate=0.10,
                liquidity_bucket="mid_far_medium",
                max_contracts_per_rebalance=20,
                liquidity_reference_contracts=180,
                far_month_liquidity_haircut=0.84,
                margin_budget_fraction=0.76,
                drawdown_budget_fraction=0.20,
            )
        if config.leg_name == "1_3":
            return ExecutionProfile(
                commission_rate=0.00005,
                slippage_bps=3.4,
                market_impact_bps=1.5,
                margin_rate=0.15,
                contract_multiplier=1000,
                capacity_participation_rate=0.09,
                liquidity_bucket="synthetic_medium",
                max_contracts_per_rebalance=16,
                liquidity_reference_contracts=150,
                far_month_liquidity_haircut=0.80,
                margin_budget_fraction=0.70,
                drawdown_budget_fraction=0.19,
            )
        return ExecutionProfile(
            commission_rate=0.00004,
            slippage_bps=3.0,
            market_impact_bps=1.3,
            margin_rate=0.15,
            contract_multiplier=1000,
            capacity_participation_rate=0.10,
            liquidity_bucket="synthetic_far_light",
            max_contracts_per_rebalance=16,
            liquidity_reference_contracts=150,
            far_month_liquidity_haircut=0.82,
            margin_budget_fraction=0.72,
            drawdown_budget_fraction=0.19,
        )

    @staticmethod
    def _trend_signal(frame: pd.DataFrame, config: TrendConfig) -> pd.Series:
        price_column = f"price_{config.leg_month:02d}"
        spread_column = f"spread_{config.leg_month - 1}_{config.leg_month}"
        return (
            (frame[spread_column] > config.carry_threshold)
            & frame["curve_monotone"]
            & (frame["curve_slope_1_4"] > 0.0)
            & (frame["spread_1_2"] > config.near_premium_floor)
            & (frame[price_column] > frame[f"{price_column}_ma20"])
            & (frame[f"{price_column}_ma20"] > frame[f"{price_column}_ma60"])
            & (frame[f"{price_column}_price_to_ma60"] < config.price_to_ma60_cap)
            & (frame[f"{price_column}_vol20"] < config.volatility_cap)
            & (frame[f"{price_column}_slope10"] > 0.0)
            & (frame["roll_next_5d"] == 0)
        ).fillna(False)

    @staticmethod
    def _trend_exit_signal(frame: pd.DataFrame, config: TrendConfig) -> pd.Series:
        price_column = f"price_{config.leg_month:02d}"
        spread_column = f"spread_{config.leg_month - 1}_{config.leg_month}"
        return (
            (frame[spread_column] <= 0.0)
            | (frame["spread_1_2"] <= config.exit_premium_floor)
            | (frame[price_column] < frame[f"{price_column}_ma20"])
            | (frame[f"{price_column}_ma20"] < frame[f"{price_column}_ma60"])
            | (frame["roll_next_3d"] == 1)
        ).fillna(False)

    @staticmethod
    def _spread_definition(config: SpreadConfig) -> tuple[pd.Series, str, str]:
        leg_map = {
            "1_2": ("spread_1_2", "price_01", "price_02"),
            "2_3": ("spread_2_3", "price_02", "price_03"),
            "3_4": ("spread_3_4", "price_03", "price_04"),
            "1_3": ("spread_1_3", "price_01", "price_03"),
            "2_4": ("spread_2_4", "price_02", "price_04"),
        }
        spread_column, near_price_column, far_price_column = leg_map[config.leg_name]
        return spread_column, near_price_column, far_price_column

    @classmethod
    def _spread_signal(cls, frame: pd.DataFrame, config: SpreadConfig) -> pd.Series:
        spread_column, _, _ = cls._spread_definition(config)
        zscore_column = f"{spread_column}_z40"
        price_filter = pd.Series(True, index=frame.index)
        if config.require_price_trend:
            price_filter = frame["price_uptrend"]
        return (
            (frame[zscore_column] >= config.entry_z_low)
            & (frame[zscore_column] <= config.entry_z_high)
            & (frame["spread_1_2"] > config.near_premium_floor)
            & frame["curve_monotone"]
            & (frame["curve_slope_1_4"] > config.slope_floor)
            & price_filter
            & (frame["roll_next_5d"] == 0)
        ).fillna(False)

    @classmethod
    def _spread_exit_signal(cls, frame: pd.DataFrame, config: SpreadConfig) -> pd.Series:
        spread_column, _, _ = cls._spread_definition(config)
        zscore_column = f"{spread_column}_z40"
        return (
            (frame[zscore_column] >= config.exit_z)
            | (frame["spread_1_2"] <= config.exit_premium_floor)
            | (frame["curve_slope_1_4"] < min(config.slope_floor, 0.0))
            | frame["price_downtrend"]
            | (frame["roll_next_3d"] == 1)
        ).fillna(False)

    @staticmethod
    def _effective_cost_rate(
        *,
        execution_profile: ExecutionProfile,
        contracts: int,
        participation_cap: int,
        leg_multiplier: int,
    ) -> tuple[float, float]:
        utilization = contracts / max(float(participation_cap or 1), 1.0)
        scale = 1.0 + min(max(utilization, 0.0), 1.0) * 0.5
        commission_rate = execution_profile.commission_rate * leg_multiplier
        bps_cost = (execution_profile.slippage_bps + execution_profile.market_impact_bps) * scale * leg_multiplier
        return commission_rate, bps_cost / 10000.0

    @staticmethod
    def _drawdown_cap(
        *,
        capital: float,
        execution_profile: ExecutionProfile,
        stress_loss_per_contract: float,
    ) -> int:
        if stress_loss_per_contract <= 0:
            return execution_profile.max_contracts_per_rebalance
        cap = math.floor(
            capital * execution_profile.drawdown_budget_fraction / max(stress_loss_per_contract, 1.0)
        )
        return max(cap, 1)

    def _get_contract_lookups(
        self,
        frame: pd.DataFrame,
    ) -> tuple[list[dict[str, float]], list[dict[str, int]]]:
        cache_key = id(frame)
        cached = self._contract_lookup_cache.get(cache_key)
        if cached is not None:
            return cached
        price_maps: list[dict[str, float]] = []
        rank_maps: list[dict[str, int]] = []
        for _, row in frame.iterrows():
            price_map: dict[str, float] = {}
            rank_map: dict[str, int] = {}
            for month_index in range(1, 13):
                code = str(row.get(f"contract_{month_index:02d}") or "").strip().lower()
                price = row.get(f"price_{month_index:02d}")
                if not code or pd.isna(price):
                    continue
                price_map[code] = float(price)
                rank_map[code] = month_index
            price_maps.append(price_map)
            rank_maps.append(rank_map)
        cached = (price_maps, rank_maps)
        self._contract_lookup_cache[cache_key] = cached
        return cached

    @staticmethod
    def _lookup_contract_price(
        lookup: dict[str, float],
        contract_code: str,
    ) -> Optional[float]:
        price = lookup.get(str(contract_code or "").strip().lower())
        if price is None or pd.isna(price):
            return None
        return float(price)

    @staticmethod
    def _regime_summary(
        returns: pd.Series,
        trades: list[dict[str, Any]],
        regime_mask: pd.Series,
    ) -> dict[str, Any]:
        masked_returns = pd.Series(returns).where(regime_mask.fillna(False), 0.0)
        trade_subset = [
            trade for trade in trades
            if str(trade.get("entry_regime") or "").strip()
            == ("backwardation" if bool(regime_mask[trade.get("entry_index", 0)]) else "contango_or_flat")
        ]
        equity = (1.0 + masked_returns.fillna(0.0)).cumprod()
        return {
            "annualized_return": _annualized_return(equity),
            "sharpe_ratio": _sharpe_ratio(masked_returns.fillna(0.0)),
            "max_drawdown": _max_drawdown(equity),
            "trade_count": len([trade for trade in trade_subset if trade.get("status") == "closed"]),
            "win_rate": _trade_win_rate(trade_subset),
        }

    @staticmethod
    def _instrument_profile_from_series(
        *,
        series: pd.Series,
        underlying: str,
        curve_legs: list[dict[str, Any]],
        roll_rule: dict[str, Any],
    ) -> dict[str, Any]:
        close = pd.Series(series).dropna().astype(float)
        returns = close.pct_change().dropna()
        if close.empty or returns.empty:
            return {
                "asset_class": "futures",
                "underlying": underlying,
                "curve_legs": deepcopy(curve_legs),
                "roll_rule": deepcopy(roll_rule),
                "measurement_source": "sc_calendar_research_adapter",
                "measured_profile_complete": False,
                "board_bucket": "futures",
                "symbol": underlying,
            }
        annual_volatility = float(returns.std(ddof=1) * math.sqrt(252))
        abs_returns = returns.abs()
        gap_p95 = float(abs_returns.quantile(0.95))
        atr14_pct = float(abs_returns.rolling(14, min_periods=5).mean().dropna().mean() or abs_returns.mean())
        intraday_range_p90 = float(abs_returns.quantile(0.90))
        direction = close.diff(60).iloc[-1] if len(close) > 61 else close.diff().sum()
        path = close.diff().abs().rolling(60, min_periods=10).sum().iloc[-1] if len(close) > 61 else close.diff().abs().sum()
        path_value = _safe_float(path)
        trend_efficiency = float(abs(direction) / path_value) if path_value > 0 else 0.0
        return {
            "asset_class": "futures",
            "underlying": underlying,
            "curve_legs": deepcopy(curve_legs),
            "roll_rule": deepcopy(roll_rule),
            "annual_volatility_realized_252d": annual_volatility,
            "annual_volatility": annual_volatility,
            "atr14_pct_realized": atr14_pct,
            "atr14_pct": atr14_pct,
            "gap_p95_realized": gap_p95,
            "gap_p95": gap_p95,
            "intraday_range_p90": intraday_range_p90,
            "trend_efficiency_60d_realized": trend_efficiency,
            "trend_efficiency_60d": trend_efficiency,
            "turnover_median": 1.0,
            "volume_ratio_p80": 1.0,
            "volume_ratio_p90": 1.0,
            "turnover_rate_p80": 1.0,
            "turnover_rate_p90": 1.0,
            "measurement_source": "sc_calendar_research_adapter",
            "measurement_sources": {
                "annual_volatility_realized_252d": "research_adapter",
                "atr14_pct_realized": "research_adapter",
                "gap_p95_realized": "research_adapter",
                "intraday_range_p90": "research_adapter",
                "trend_efficiency_60d_realized": "research_adapter",
                "volume_ratio_p80": "research_adapter_proxy",
                "volume_ratio_p90": "research_adapter_proxy",
                "turnover_rate_p80": "research_adapter_proxy",
                "turnover_rate_p90": "research_adapter_proxy",
            },
            "measured_profile_complete": True,
            "board_bucket": "futures",
            "symbol": underlying,
        }

    @staticmethod
    def _compile_strategy_blueprint_safe(candidate: dict[str, Any], market_frame: pd.DataFrame) -> dict[str, Any]:
        from ...infrastructure.mcp_services import get_strategy_dsl_compiler

        compile_strategy_blueprint = get_strategy_dsl_compiler()
        compiled = compile_strategy_blueprint(candidate, market_frame=market_frame, tune_for_factory=True)
        return dict(compiled or {})

    @staticmethod
    def _complete_trade_record(
        *,
        trade: dict[str, Any],
        exit_date: pd.Timestamp,
        exit_value: float,
        net_pnl: float,
        gross_pnl: float,
        exit_reason: str,
        holding_days: int,
    ) -> dict[str, Any]:
        completed = dict(trade)
        completed.update(
            {
                "status": "closed",
                "exit_date": str(exit_date.date()),
                "exit_value": round(exit_value, 6),
                "net_pnl": round(net_pnl, 2),
                "gross_pnl": round(gross_pnl, 2),
                "exit_reason": exit_reason,
                "holding_days": int(holding_days),
            }
        )
        return completed
    def _simulate_trend(
        self,
        frame: pd.DataFrame,
        config: TrendConfig,
        *,
        capital: float,
        execution_profile: ExecutionProfile,
    ) -> dict[str, Any]:
        entry_signal = self._trend_signal(frame, config)
        exit_signal = self._trend_exit_signal(frame, config)
        price_column = f"price_{config.leg_month:02d}"
        price_series = frame[price_column].astype(float)
        gross_equity = float(capital)
        net_equity = float(capital)
        position_contracts = 0
        entry_price = 0.0
        entry_index = -1
        entry_date: Optional[pd.Timestamp] = None
        entry_regime = ""
        gross_returns: list[float] = []
        net_returns: list[float] = []
        equity_path: list[float] = []
        trades: list[dict[str, Any]] = []
        gross_pnl_running = 0.0
        net_pnl_running = 0.0
        stress_move = max(
            _safe_float(price_series.pct_change().abs().quantile(0.95), 0.02) * float(price_series.median()) * 2.2,
            0.02 * float(price_series.median()),
        )

        for index, row in frame.iterrows():
            current_price = float(row[price_column])
            previous_price = float(price_series.iloc[index - 1]) if index > 0 else current_price
            gross_pnl = position_contracts * execution_profile.contract_multiplier * (current_price - previous_price)
            gross_equity += gross_pnl
            net_equity += gross_pnl
            gross_cost = 0.0
            net_cost = 0.0
            if position_contracts > 0 and int(row["roll_node"] or 0):
                notional = abs(position_contracts) * current_price * execution_profile.contract_multiplier
                _, roll_cost_rate = self._effective_cost_rate(
                    execution_profile=execution_profile,
                    contracts=abs(position_contracts),
                    participation_cap=max(
                        1,
                        math.floor(
                            execution_profile.liquidity_reference_contracts
                            * execution_profile.capacity_participation_rate
                            * execution_profile.far_month_liquidity_haircut
                        ),
                    ),
                    leg_multiplier=1,
                )
                roll_cost = notional * (roll_cost_rate * 0.35)
                net_equity -= roll_cost
                net_cost += roll_cost
            if position_contracts > 0:
                trade_return = (current_price - entry_price) / max(entry_price, 1e-9)
                should_exit = bool(exit_signal.iloc[index]) or trade_return <= -config.stop_loss_pct
                if should_exit:
                    participation_cap = max(
                        1,
                        math.floor(
                            execution_profile.liquidity_reference_contracts
                            * execution_profile.capacity_participation_rate
                            * execution_profile.far_month_liquidity_haircut
                        ),
                    )
                    commission_rate, cost_rate = self._effective_cost_rate(
                        execution_profile=execution_profile,
                        contracts=abs(position_contracts),
                        participation_cap=participation_cap,
                        leg_multiplier=1,
                    )
                    notional = abs(position_contracts) * current_price * execution_profile.contract_multiplier
                    trade_cost = notional * (commission_rate + cost_rate)
                    net_equity -= trade_cost
                    net_cost += trade_cost
                    last_trade = trades[-1]
                    trades[-1] = self._complete_trade_record(
                        trade=last_trade,
                        exit_date=row["trading_date"],
                        exit_value=current_price,
                        gross_pnl=last_trade.get("gross_pnl", 0.0) + gross_pnl_running + gross_pnl,
                        net_pnl=last_trade.get("net_pnl", 0.0) + net_pnl_running + gross_pnl - net_cost,
                        exit_reason="trend_decay_or_delivery" if bool(exit_signal.iloc[index]) else "stop_loss",
                        holding_days=index - entry_index,
                    )
                    gross_pnl_running = 0.0
                    net_pnl_running = 0.0
                    position_contracts = 0
                    entry_price = 0.0
                    entry_index = -1
                    entry_date = None
                    entry_regime = ""
            if position_contracts == 0 and bool(entry_signal.iloc[index]):
                participation_cap = max(
                    1,
                    math.floor(
                        execution_profile.liquidity_reference_contracts
                        * execution_profile.capacity_participation_rate
                        * execution_profile.far_month_liquidity_haircut
                    ),
                )
                margin_per_contract = current_price * execution_profile.contract_multiplier * execution_profile.margin_rate
                margin_cap = math.floor(net_equity * execution_profile.margin_budget_fraction / max(margin_per_contract, 1.0))
                drawdown_cap = self._drawdown_cap(
                    capital=net_equity,
                    execution_profile=execution_profile,
                    stress_loss_per_contract=stress_move * execution_profile.contract_multiplier,
                )
                capacity_limit = max(
                    1,
                    min(
                        margin_cap,
                        participation_cap,
                        execution_profile.max_contracts_per_rebalance,
                        drawdown_cap,
                    ),
                )
                if capacity_limit > 0:
                    commission_rate, cost_rate = self._effective_cost_rate(
                        execution_profile=execution_profile,
                        contracts=capacity_limit,
                        participation_cap=participation_cap,
                        leg_multiplier=1,
                    )
                    notional = capacity_limit * current_price * execution_profile.contract_multiplier
                    entry_cost = notional * (commission_rate + cost_rate)
                    net_equity -= entry_cost
                    net_cost += entry_cost
                    position_contracts = capacity_limit
                    entry_price = current_price
                    entry_index = index
                    entry_date = row["trading_date"]
                    entry_regime = str(row["regime"])
                    trades.append(
                        {
                            "status": "open",
                            "family": "trend",
                            "entry_index": index,
                            "entry_date": str(entry_date.date()),
                            "entry_value": round(current_price, 6),
                            "entry_regime": entry_regime,
                            "contracts": int(capacity_limit),
                            "gross_pnl": 0.0,
                            "net_pnl": -round(entry_cost, 2),
                        }
                    )
                    gross_pnl_running = 0.0
                    net_pnl_running = -entry_cost
            if position_contracts > 0:
                gross_pnl_running += gross_pnl
                net_pnl_running += gross_pnl - net_cost
            gross_returns.append(gross_pnl / max(gross_equity - gross_pnl, 1.0))
            net_returns.append((gross_pnl - net_cost) / max(net_equity - gross_pnl + net_cost, 1.0))
            equity_path.append(net_equity)

        if position_contracts > 0 and trades:
            last_trade = trades[-1]
            trades[-1] = self._complete_trade_record(
                trade=last_trade,
                exit_date=frame["trading_date"].iloc[-1],
                exit_value=float(price_series.iloc[-1]),
                gross_pnl=last_trade.get("gross_pnl", 0.0) + gross_pnl_running,
                net_pnl=last_trade.get("net_pnl", 0.0) + net_pnl_running,
                exit_reason="final_mark",
                holding_days=max(len(frame) - 1 - last_trade.get("entry_index", 0), 1),
            )

        gross_returns_series = pd.Series(gross_returns, index=frame.index).fillna(0.0)
        net_returns_series = pd.Series(net_returns, index=frame.index).fillna(0.0)
        equity_series = pd.Series(equity_path, index=frame.index)
        entry_forward_returns = price_series.shift(-5).div(price_series).sub(1.0).where(entry_signal, np.nan)
        trade_count = len([trade for trade in trades if trade.get("status") == "closed"])
        summary = {
            "annualized_return": _annualized_return(equity_series),
            "total_return": _safe_float(equity_series.iloc[-1] / max(capital, 1.0) - 1.0),
            "sharpe_ratio": _sharpe_ratio(gross_returns_series),
            "post_cost_sharpe": _sharpe_ratio(net_returns_series),
            "max_drawdown": _max_drawdown(equity_series),
            "win_rate": _trade_win_rate(trades),
            "trade_count": trade_count,
            "trade_density": trade_count / max(len(frame) / 252.0, 1e-9),
            "forward_sharpe_5d": _forward_sharpe_5d(entry_forward_returns),
            "alpha_decay": max(
                0.0,
                _sharpe_ratio(gross_returns_series) - max(_forward_sharpe_5d(entry_forward_returns), 0.0),
            ),
            "ending_equity": _safe_float(equity_series.iloc[-1]),
        }
        regime_panel = {
            "overall": {
                key: summary[key]
                for key in ("annualized_return", "sharpe_ratio", "post_cost_sharpe", "max_drawdown", "win_rate", "trade_count")
            },
            "backwardation": self._regime_summary(net_returns_series, trades, frame["regime"].eq("backwardation")),
            "contango_or_flat": self._regime_summary(net_returns_series, trades, frame["regime"].eq("contango_or_flat")),
        }
        return {
            "family": "trend",
            "entry_signal": entry_signal,
            "summary": summary,
            "regime_panel": regime_panel,
            "trades": trades,
            "equity_series": equity_series,
            "returns_pre_cost": gross_returns_series,
            "returns_post_cost": net_returns_series,
            "signal_series": price_series,
        }

    def _simulate_spread(
        self,
        frame: pd.DataFrame,
        config: SpreadConfig,
        *,
        capital: float,
        execution_profile: ExecutionProfile,
    ) -> dict[str, Any]:
        spread_column, near_price_column, far_price_column = self._spread_definition(config)
        entry_signal = self._spread_signal(frame, config)
        exit_signal = self._spread_exit_signal(frame, config)
        spread_series = frame[spread_column].astype(float)
        near_prices = frame[near_price_column].astype(float)
        far_prices = frame[far_price_column].astype(float)
        near_month = _safe_int(near_price_column.split("_")[1], 1)
        far_month = _safe_int(far_price_column.split("_")[1], near_month + 1)
        price_lookups, rank_lookups = self._get_contract_lookups(frame)
        gross_equity = float(capital)
        net_equity = float(capital)
        position_contracts = 0
        entry_spread = 0.0
        entry_index = -1
        entry_date: Optional[pd.Timestamp] = None
        entry_regime = ""
        entry_long_code = ""
        entry_short_code = ""
        gross_returns: list[float] = []
        net_returns: list[float] = []
        equity_path: list[float] = []
        trades: list[dict[str, Any]] = []
        gross_pnl_running = 0.0
        net_pnl_running = 0.0
        stress_move = max(float(spread_series.diff().abs().quantile(0.95) or 0.0) * 2.0, 3.0)

        for index, row in frame.iterrows():
            current_spread = float(spread_series.iloc[index])
            current_long_price = None
            current_short_price = None
            gross_pnl = 0.0
            if position_contracts > 0:
                current_lookup = price_lookups[index]
                previous_lookup = price_lookups[index - 1] if index > 0 else current_lookup
                current_long_price = self._lookup_contract_price(current_lookup, entry_long_code)
                current_short_price = self._lookup_contract_price(current_lookup, entry_short_code)
                previous_long_price = self._lookup_contract_price(previous_lookup, entry_long_code) or current_long_price
                previous_short_price = self._lookup_contract_price(previous_lookup, entry_short_code) or current_short_price
                if (
                    current_long_price is not None
                    and current_short_price is not None
                    and previous_long_price is not None
                    and previous_short_price is not None
                ):
                    gross_pnl = position_contracts * execution_profile.contract_multiplier * (
                        (current_long_price - previous_long_price)
                        - (current_short_price - previous_short_price)
                    )
                    current_spread = current_long_price - current_short_price
            gross_equity += gross_pnl
            net_equity += gross_pnl
            net_cost = 0.0
            if position_contracts > 0:
                long_rank = rank_lookups[index].get(entry_long_code, 99)
                should_exit = (
                    bool(exit_signal.iloc[index])
                    or (current_spread - entry_spread) <= config.stop_move
                    or (index - entry_index) >= int(config.max_holding_days)
                    or (long_rank <= 2 and int(row["roll_next_3d"] or 0) == 1)
                    or current_long_price is None
                    or current_short_price is None
                )
                if should_exit:
                    participation_cap = max(
                        1,
                        math.floor(
                            execution_profile.liquidity_reference_contracts
                            * execution_profile.capacity_participation_rate
                            * execution_profile.far_month_liquidity_haircut
                        ),
                    )
                    commission_rate, cost_rate = self._effective_cost_rate(
                        execution_profile=execution_profile,
                        contracts=abs(position_contracts),
                        participation_cap=participation_cap,
                        leg_multiplier=2,
                    )
                    gross_notional = abs(position_contracts) * execution_profile.contract_multiplier * max(
                        (current_long_price or float(row[near_price_column])) + (current_short_price or float(row[far_price_column])),
                        1.0,
                    )
                    trade_cost = gross_notional * (commission_rate + cost_rate)
                    net_equity -= trade_cost
                    net_cost += trade_cost
                    last_trade = trades[-1]
                    trades[-1] = self._complete_trade_record(
                        trade=last_trade,
                        exit_date=row["trading_date"],
                        exit_value=current_spread,
                        gross_pnl=last_trade.get("gross_pnl", 0.0) + gross_pnl_running + gross_pnl,
                        net_pnl=last_trade.get("net_pnl", 0.0) + net_pnl_running + gross_pnl - net_cost,
                        exit_reason=(
                            "mean_reversion_or_delivery"
                            if bool(exit_signal.iloc[index]) or (long_rank <= 2 and int(row["roll_next_3d"] or 0) == 1)
                            else "stop_loss_or_time_stop"
                        ),
                        holding_days=index - entry_index,
                    )
                    gross_pnl_running = 0.0
                    net_pnl_running = 0.0
                    position_contracts = 0
                    entry_spread = 0.0
                    entry_index = -1
                    entry_date = None
                    entry_regime = ""
                    entry_long_code = ""
                    entry_short_code = ""
            if position_contracts == 0 and bool(entry_signal.iloc[index]):
                participation_cap = max(
                    1,
                    math.floor(
                        execution_profile.liquidity_reference_contracts
                        * execution_profile.capacity_participation_rate
                        * execution_profile.far_month_liquidity_haircut
                    ),
                )
                gross_margin_per_contract = execution_profile.contract_multiplier * execution_profile.margin_rate * (
                    float(row[near_price_column]) + float(row[far_price_column])
                )
                margin_cap = math.floor(net_equity * execution_profile.margin_budget_fraction / max(gross_margin_per_contract, 1.0))
                drawdown_cap = self._drawdown_cap(
                    capital=net_equity,
                    execution_profile=execution_profile,
                    stress_loss_per_contract=stress_move * execution_profile.contract_multiplier,
                )
                capacity_limit = max(
                    1,
                    min(
                        margin_cap,
                        participation_cap,
                        execution_profile.max_contracts_per_rebalance,
                        drawdown_cap,
                    ),
                )
                if capacity_limit > 0:
                    long_code = str(row.get(f"contract_{near_month:02d}") or "").strip().lower()
                    short_code = str(row.get(f"contract_{far_month:02d}") or "").strip().lower()
                    commission_rate, cost_rate = self._effective_cost_rate(
                        execution_profile=execution_profile,
                        contracts=capacity_limit,
                        participation_cap=participation_cap,
                        leg_multiplier=2,
                    )
                    gross_notional = capacity_limit * execution_profile.contract_multiplier * (
                        float(row[near_price_column]) + float(row[far_price_column])
                    )
                    entry_cost = gross_notional * (commission_rate + cost_rate)
                    net_equity -= entry_cost
                    net_cost += entry_cost
                    position_contracts = capacity_limit
                    entry_spread = current_spread
                    entry_index = index
                    entry_date = row["trading_date"]
                    entry_regime = str(row["regime"])
                    entry_long_code = long_code
                    entry_short_code = short_code
                    trades.append(
                        {
                            "status": "open",
                            "family": "spread",
                            "entry_index": index,
                            "entry_date": str(entry_date.date()),
                            "entry_value": round(current_spread, 6),
                            "entry_regime": entry_regime,
                            "contracts": int(capacity_limit),
                            "long_contract": entry_long_code,
                            "short_contract": entry_short_code,
                            "gross_pnl": 0.0,
                            "net_pnl": -round(entry_cost, 2),
                        }
                    )
                    gross_pnl_running = 0.0
                    net_pnl_running = -entry_cost
            if position_contracts > 0:
                gross_pnl_running += gross_pnl
                net_pnl_running += gross_pnl - net_cost
            gross_returns.append(gross_pnl / max(gross_equity - gross_pnl, 1.0))
            net_returns.append((gross_pnl - net_cost) / max(net_equity - gross_pnl + net_cost, 1.0))
            equity_path.append(net_equity)

        if position_contracts > 0 and trades:
            terminal_long_price = self._lookup_contract_price(price_lookups[-1], entry_long_code)
            terminal_short_price = self._lookup_contract_price(price_lookups[-1], entry_short_code)
            terminal_spread = (
                terminal_long_price - terminal_short_price
                if terminal_long_price is not None and terminal_short_price is not None
                else float(spread_series.iloc[-1])
            )
            last_trade = trades[-1]
            trades[-1] = self._complete_trade_record(
                trade=last_trade,
                exit_date=frame["trading_date"].iloc[-1],
                exit_value=terminal_spread,
                gross_pnl=last_trade.get("gross_pnl", 0.0) + gross_pnl_running,
                net_pnl=last_trade.get("net_pnl", 0.0) + net_pnl_running,
                exit_reason="final_mark",
                holding_days=max(len(frame) - 1 - last_trade.get("entry_index", 0), 1),
            )

        gross_returns_series = pd.Series(gross_returns, index=frame.index).fillna(0.0)
        net_returns_series = pd.Series(net_returns, index=frame.index).fillna(0.0)
        equity_series = pd.Series(equity_path, index=frame.index)
        spread_notional = near_prices.abs().add(far_prices.abs()).replace(0.0, np.nan)
        entry_forward_returns = (
            spread_series.shift(-5).sub(spread_series).div(spread_notional)
        ).where(entry_signal, np.nan)
        trade_count = len([trade for trade in trades if trade.get("status") == "closed"])
        summary = {
            "annualized_return": _annualized_return(equity_series),
            "total_return": _safe_float(equity_series.iloc[-1] / max(capital, 1.0) - 1.0),
            "sharpe_ratio": _sharpe_ratio(gross_returns_series),
            "post_cost_sharpe": _sharpe_ratio(net_returns_series),
            "max_drawdown": _max_drawdown(equity_series),
            "win_rate": _trade_win_rate(trades),
            "trade_count": trade_count,
            "trade_density": trade_count / max(len(frame) / 252.0, 1e-9),
            "forward_sharpe_5d": _forward_sharpe_5d(entry_forward_returns),
            "alpha_decay": max(
                0.0,
                _sharpe_ratio(gross_returns_series) - max(_forward_sharpe_5d(entry_forward_returns), 0.0),
            ),
            "ending_equity": _safe_float(equity_series.iloc[-1]),
        }
        regime_panel = {
            "overall": {
                key: summary[key]
                for key in ("annualized_return", "sharpe_ratio", "post_cost_sharpe", "max_drawdown", "win_rate", "trade_count")
            },
            "backwardation": self._regime_summary(net_returns_series, trades, frame["regime"].eq("backwardation")),
            "contango_or_flat": self._regime_summary(net_returns_series, trades, frame["regime"].eq("contango_or_flat")),
        }
        return {
            "family": "spread",
            "entry_signal": entry_signal,
            "summary": summary,
            "regime_panel": regime_panel,
            "trades": trades,
            "equity_series": equity_series,
            "returns_pre_cost": gross_returns_series,
            "returns_post_cost": net_returns_series,
            "signal_series": spread_series,
        }

    def _run_trend_grid(self, frame: pd.DataFrame, *, capital: float) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        for leg_month in (3, 4):
            for carry_threshold in (0.0, 0.5, 1.0):
                for volatility_cap in (0.03, 0.04, 0.05, 0.06):
                    for price_to_ma60_cap in (1.08, 1.12, 1.20):
                        for stop_loss_pct in (0.05, 0.07):
                            config = TrendConfig(
                                leg_month=leg_month,
                                carry_threshold=carry_threshold,
                                volatility_cap=volatility_cap,
                                price_to_ma60_cap=price_to_ma60_cap,
                                stop_loss_pct=stop_loss_pct,
                            )
                            execution_profile = self._trend_execution_profile(config)
                            backtest = self._simulate_trend(
                                frame,
                                config,
                                capital=capital,
                                execution_profile=execution_profile,
                            )
                            results.append(
                                {
                                    "config": config,
                                    "execution_profile": execution_profile,
                                    "backtest": backtest,
                                }
                            )
        return results
    def _run_spread_grid(self, frame: pd.DataFrame, *, capital: float) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        for leg_name in ("1_2", "2_3", "3_4", "1_3", "2_4"):
            for entry_z_low, entry_z_high in (
                (-1.0, 0.0),
                (-0.75, 0.25),
                (-0.5, 0.5),
                (-0.25, 0.75),
            ):
                for exit_z in (0.75, 1.0, 1.25):
                    for stop_move in (-3.0, -5.0):
                        for slope_floor in (0.0, 0.5):
                            for max_holding_days in (30, 45):
                                for require_price_trend in (True, False):
                                    config = SpreadConfig(
                                        leg_name=leg_name,
                                        entry_z_low=entry_z_low,
                                        entry_z_high=entry_z_high,
                                        exit_z=exit_z,
                                        stop_move=stop_move,
                                        slope_floor=slope_floor,
                                        max_holding_days=max_holding_days,
                                        require_price_trend=require_price_trend,
                                    )
                                    execution_profile = self._spread_execution_profile(config)
                                    backtest = self._simulate_spread(
                                        frame,
                                        config,
                                        capital=capital,
                                        execution_profile=execution_profile,
                                    )
                                    results.append(
                                        {
                                            "config": config,
                                            "execution_profile": execution_profile,
                                            "backtest": backtest,
                                        }
                                    )
        return results

    @staticmethod
    def _rank_results(raw_results: list[dict[str, Any]], *, minimum_annualized_return: float = 0.10) -> list[dict[str, Any]]:
        filtered = [
            item for item in raw_results
            if _safe_float(item["backtest"]["summary"].get("annualized_return")) >= minimum_annualized_return
            and _safe_int(item["backtest"]["summary"].get("trade_count")) >= 6
        ]
        if not filtered:
            fallback_pool = [
                item for item in raw_results
                if _safe_int(item["backtest"]["summary"].get("trade_count")) >= 3
            ] or list(raw_results)
            return sorted(
                fallback_pool,
                key=lambda item: (
                    _safe_float(item["backtest"]["summary"].get("annualized_return")),
                    _safe_float(item["backtest"]["summary"].get("post_cost_sharpe")),
                    _safe_float(item["backtest"]["summary"].get("win_rate")),
                    -abs(_safe_float(item["backtest"]["summary"].get("max_drawdown"))),
                    _safe_int(item["backtest"]["summary"].get("trade_count")),
                ),
                reverse=True,
            )
        return sorted(
            filtered,
            key=lambda item: (
                _safe_float(item["backtest"]["summary"].get("post_cost_sharpe")),
                _safe_float(item["backtest"]["summary"].get("annualized_return")),
                _safe_float(item["backtest"]["summary"].get("win_rate")),
                -abs(_safe_float(item["backtest"]["summary"].get("max_drawdown"))),
                _safe_int(item["backtest"]["summary"].get("trade_count")),
            ),
            reverse=True,
        )

    def _stress_test(
        self,
        frame: pd.DataFrame,
        *,
        family: str,
        config: TrendConfig | SpreadConfig,
        execution_profile: ExecutionProfile,
    ) -> list[dict[str, Any]]:
        panel: list[dict[str, Any]] = []
        simulator = self._simulate_trend if family == "trend" else self._simulate_spread
        for capital in CAPITAL_BUCKETS:
            backtest = simulator(
                frame,
                config,
                capital=float(capital),
                execution_profile=execution_profile,
            )
            if family == "trend":
                price_column = f"price_{config.leg_month:02d}"
                stress_reference = float(frame[price_column].median() or frame["price_01"].median())
                leg_multiplier = 1
            else:
                spread_column, near_price_column, far_price_column = self._spread_definition(config)
                stress_reference = float(
                    frame[near_price_column].add(frame[far_price_column]).median()
                    or frame["price_01"].median()
                )
                leg_multiplier = 2
            gross_margin_per_contract = (
                stress_reference
                * execution_profile.contract_multiplier
                * execution_profile.margin_rate
                * leg_multiplier
            )
            margin_cap = math.floor(capital * execution_profile.margin_budget_fraction / max(gross_margin_per_contract, 1.0))
            participation_cap = math.floor(
                execution_profile.liquidity_reference_contracts
                * execution_profile.capacity_participation_rate
                * execution_profile.far_month_liquidity_haircut
            )
            if family == "trend":
                stress_loss_per_contract = max(stress_reference * 0.06 * execution_profile.contract_multiplier, 1.0)
            else:
                stress_loss_per_contract = max(4.0 * execution_profile.contract_multiplier, 1.0)
            drawdown_cap = self._drawdown_cap(
                capital=capital,
                execution_profile=execution_profile,
                stress_loss_per_contract=stress_loss_per_contract,
            )
            actual_contract_cap = max(
                1,
                min(
                    margin_cap,
                    participation_cap,
                    execution_profile.max_contracts_per_rebalance,
                    drawdown_cap,
                ),
            )
            constraint_map = {
                "participation": participation_cap,
                "margin": margin_cap,
                "max_contracts": execution_profile.max_contracts_per_rebalance,
                "drawdown": drawdown_cap,
            }
            binding_constraint = min(constraint_map, key=lambda key: constraint_map[key])
            summary = backtest["summary"]
            panel.append(
                {
                    "capital": int(capital),
                    "annualized_return": round(_safe_float(summary.get("annualized_return")), 6),
                    "post_cost_sharpe": round(_safe_float(summary.get("post_cost_sharpe")), 6),
                    "max_drawdown": round(_safe_float(summary.get("max_drawdown")), 6),
                    "win_rate": round(_safe_float(summary.get("win_rate")), 6),
                    "trade_count": int(summary.get("trade_count") or 0),
                    "capacity_limit_contracts": int(actual_contract_cap),
                    "binding_constraint": binding_constraint,
                    "participation_cap": int(participation_cap),
                    "margin_cap": int(margin_cap),
                    "max_contracts_cap": int(execution_profile.max_contracts_per_rebalance),
                    "drawdown_cap": int(drawdown_cap),
                }
            )
        return panel

    def _build_evidence_chain(
        self,
        *,
        family: str,
        summary: dict[str, Any],
        regime_panel: dict[str, Any],
    ) -> dict[str, Any]:
        evidence_items = [
            {
                "evidence_id": f"{family}_memo",
                "source_type": "local_research_memo",
                "direction": "supportive",
                "summary": "本地研究备忘强调 SC 远月 carry 与近端升贴水切换特征，支持趋势 + 跨月套利双族。",
                "source_path": str(self.memo_path),
            },
            {
                "evidence_id": f"{family}_roll_rule",
                "source_type": "data_definition",
                "direction": "supportive",
                "summary": "本地数据说明明确到期换月与交割保护口径，适合作为常数期限研究适配器的 roll 规则。",
                "source_path": str(self.notes_path),
            },
            {
                "evidence_id": f"{family}_backtest",
                "source_type": "full_sample_backtest",
                "direction": "supportive",
                "summary": (
                    f"全样本后成本年化 {summary['annualized_return']:.2%}，"
                    f"post-cost Sharpe {summary['post_cost_sharpe']:.2f}，"
                    f"最大回撤 {summary['max_drawdown']:.2%}。"
                ),
            },
            {
                "evidence_id": f"{family}_regime",
                "source_type": "regime_panel",
                "direction": "supportive",
                "summary": (
                    f"backwardation 年化 {regime_panel['backwardation']['annualized_return']:.2%}，"
                    f"contango/flat 年化 {regime_panel['contango_or_flat']['annualized_return']:.2%}。"
                ),
            },
        ]
        return {"evidences": evidence_items}

    @staticmethod
    def _build_confidence_contract(summary: dict[str, Any]) -> dict[str, Any]:
        return {
            "prediction_quality": {
                "support_samples": int(summary.get("trade_count") or 0),
                "historical_hit_rate": round(_safe_float(summary.get("win_rate")), 6),
                "post_cost_sharpe": round(_safe_float(summary.get("post_cost_sharpe")), 6),
                "alpha_decay": round(_safe_float(summary.get("alpha_decay")), 6),
            }
        }

    @staticmethod
    def _build_constraint_check() -> dict[str, Any]:
        return {
            "coverage_ratio": 1.0,
            "intersection_ratio": 1.0,
            "target_overlap_count": 1,
            "constraint_violation": False,
            "alignment_contract_violation": False,
        }
    def _build_candidate(
        self,
        frame: pd.DataFrame,
        *,
        family: str,
        config: TrendConfig | SpreadConfig,
        execution_profile: ExecutionProfile,
        backtest: dict[str, Any],
        capacity_panel: list[dict[str, Any]],
    ) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
        summary = backtest["summary"]
        regime_panel = backtest["regime_panel"]
        if family == "trend":
            leg_month = int(config.leg_month)
            strategy_name = f"SC Trend Carry M{leg_month}"
            signal_series = backtest["signal_series"]
            price_column = f"price_{leg_month:02d}"
            curve_legs = [{"side": "long", "leg_type": "month_offset", "month": leg_month}]
            instrument_profile = self._instrument_profile_from_series(
                series=signal_series,
                underlying="SC",
                curve_legs=curve_legs,
                roll_rule={
                    "rule_type": "constant_maturity_roll",
                    "exit_before_front_delivery_days": 3,
                    "front_contract_column": "contract_01",
                },
            )
            raw_dsl = {
                "version": "1.0",
                "timeframe": "daily",
                "entry": {
                    "all": [
                        {
                            "op": "gt",
                            "left": {"field": "close"},
                            "right": {"indicator": "sma", "field": "close", "window": 20},
                            "trade_plan_node_id": "entry_trend_1",
                        },
                        {
                            "op": "gt",
                            "left": {"indicator": "sma", "field": "close", "window": 20},
                            "right": {"indicator": "sma", "field": "close", "window": 60},
                        },
                    ]
                },
                "exit": {
                    "any": [
                        {
                            "op": "lt",
                            "left": {"field": "close"},
                            "right": {"indicator": "sma", "field": "close", "window": 20},
                            "trade_plan_node_id": "exit_trend_1",
                        },
                        {
                            "op": "lt",
                            "left": {"indicator": "sma", "field": "close", "window": 20},
                            "right": {"indicator": "sma", "field": "close", "window": 60},
                        },
                    ]
                },
                "metadata": {
                    "signal_reference_series": price_column,
                    "trade_leg_definition": deepcopy(curve_legs),
                },
            }
            trade_plan = {
                "entry": {
                    "node_id": "entry_trend_1",
                    "claim_ids": ["claim_trend_1"],
                    "entry_bias": "carry_trend_follow",
                },
                "exit": {
                    "node_id": "exit_trend_1",
                    "claim_ids": ["claim_trend_1"],
                    "exit_bias": "trend_decay_or_delivery_protection",
                },
                "steps": [
                    {
                        "node_id": "step_trend_hold",
                        "claim_ids": ["claim_trend_1"],
                        "summary": "保持第 3/4 月常数期限多头，roll node 前主动收缩。",
                    }
                ],
            }
            prediction_contract = {
                "claims": [
                    {
                        "claim_id": "claim_trend_1",
                        "expected_move": "up",
                        "expected_horizon": 15,
                        "evidence_ids": ["trend_memo", "trend_backtest", "trend_regime"],
                        "failure_condition": "near_month_extreme_premium_or_trend_break",
                    }
                ]
            }
            portfolio_spec = {
                "position_assumption": "single_futures_directional",
                "target_weight_scheme": "single_contract_margin_budget",
                "max_position_pct": round(execution_profile.margin_budget_fraction, 4),
                "position_sizing_rationale": "用 gross margin 约束远月方向仓位，不假设跨腿保证金优惠。",
            }
            position_sizing = {
                "mode": "margin_budget",
                "position_assumption": "single_futures_directional",
                "margin_budget_fraction": round(execution_profile.margin_budget_fraction, 4),
            }
            holding_horizon = {
                "min_days": 5,
                "max_days": 25,
                "rebalance_interval_days": 1,
                "cooldown_window_days": 2,
                "expected_turnover_band": "medium",
            }
            risk_rules = {
                "stop_loss_pct": round(config.stop_loss_pct, 4),
                "time_stop_days": 25,
                "delivery_protection_days": 3,
                "near_month_premium_floor": round(config.exit_premium_floor, 4),
            }
            rebalance_rule = {
                "mode": "roll_or_signal",
                "frequency_days": 1,
                "roll_guard_days": 3,
            }
            hypothesis_text = "Backwardation carry 与远月趋势延续在近端未出现异常升水时可共振放大。"
        else:
            spread_column, near_price_column, far_price_column = self._spread_definition(config)
            strategy_name = f"SC Spread {config.leg_name.replace('_', '-')}"
            signal_series = backtest["signal_series"]
            near_month = _safe_int(near_price_column.split("_")[1], 1)
            far_month = _safe_int(far_price_column.split("_")[1], near_month + 1)
            curve_legs = [
                {"side": "long", "leg_type": "month_offset", "month": near_month},
                {"side": "short", "leg_type": "month_offset", "month": far_month},
            ]
            instrument_profile = self._instrument_profile_from_series(
                series=signal_series,
                underlying="SC",
                curve_legs=curve_legs,
                roll_rule={
                    "rule_type": "calendar_spread_roll",
                    "exit_before_front_delivery_days": 3,
                    "front_contract_column": "contract_01",
                },
            )
            raw_dsl = {
                "version": "1.0",
                "timeframe": "daily",
                "entry": {
                    "all": [
                        {
                            "op": "gte",
                            "left": {"indicator": "zscore", "field": "close", "window": 40},
                            "right": {"value": round(config.entry_z_low, 4)},
                            "trade_plan_node_id": "entry_spread_1",
                        },
                        {
                            "op": "lte",
                            "left": {"indicator": "zscore", "field": "close", "window": 40},
                            "right": {"value": round(config.entry_z_high, 4)},
                        }
                    ]
                },
                "exit": {
                    "any": [
                        {
                            "op": "gt",
                            "left": {"indicator": "zscore", "field": "close", "window": 40},
                            "right": {"value": round(config.exit_z, 4)},
                            "trade_plan_node_id": "exit_spread_1",
                        }
                    ]
                },
                "metadata": {
                    "signal_reference_series": spread_column,
                    "trade_leg_definition": deepcopy(curve_legs),
                    "mark_to_market_mode": "actual_contract_legs",
                    "entry_z_band": {
                        "low": round(config.entry_z_low, 4),
                        "high": round(config.entry_z_high, 4),
                    },
                },
            }
            trade_plan = {
                "entry": {
                    "node_id": "entry_spread_1",
                    "claim_ids": ["claim_spread_1"],
                    "entry_bias": "buy_near_sell_far_on_pullback",
                },
                "exit": {
                    "node_id": "exit_spread_1",
                    "claim_ids": ["claim_spread_1"],
                    "exit_bias": "mean_reversion_or_delivery_protection",
                },
                "steps": [
                    {
                        "node_id": "step_spread_hold",
                        "claim_ids": ["claim_spread_1"],
                        "summary": "维持买近抛远对冲，温和 pullback 后等待价差修复，时间止盈或交割保护离场。",
                    }
                ],
            }
            prediction_contract = {
                "claims": [
                    {
                        "claim_id": "claim_spread_1",
                        "expected_move": "spread_widen",
                        "expected_horizon": int(config.max_holding_days),
                        "evidence_ids": ["spread_memo", "spread_backtest", "spread_regime"],
                        "failure_condition": "term_structure_reversal_or_delivery_pressure",
                    }
                ]
            }
            portfolio_spec = {
                "position_assumption": "paired_futures_spread",
                "target_weight_scheme": "paired_margin_budget",
                "max_position_pct": round(execution_profile.margin_budget_fraction, 4),
                "position_sizing_rationale": "spread gross margin 保守计提，容量受 far-month haircut 限制。",
            }
            position_sizing = {
                "mode": "paired_margin_budget",
                "position_assumption": "paired_futures_spread",
                "margin_budget_fraction": round(execution_profile.margin_budget_fraction, 4),
            }
            holding_horizon = {
                "min_days": 3,
                "max_days": int(config.max_holding_days),
                "rebalance_interval_days": 1,
                "cooldown_window_days": 1,
                "expected_turnover_band": "medium",
            }
            risk_rules = {
                "stop_loss_abs_move": round(abs(config.stop_move), 4),
                "time_stop_days": int(config.max_holding_days),
                "delivery_protection_days": 3,
                "near_month_premium_floor": round(config.exit_premium_floor, 4),
            }
            rebalance_rule = {
                "mode": "signal_or_roll",
                "frequency_days": 1,
                "roll_guard_days": 3,
            }
            hypothesis_text = (
                "SC 期限结构在 backwardation 主导阶段具备稳定 carry，"
                "温和 pullback 后的 2-4/3-4 远端跨月更适合买近抛远并持有到结构修复。"
            )

        evidence_chain = self._build_evidence_chain(
            family=family,
            summary=summary,
            regime_panel=regime_panel,
        )
        constraint_check = self._build_constraint_check()
        research_task = {
            "task_id": "sc_calendar_full_sample_20180726_20250219",
            "task_source": "snapshot",
            "opportunity_type": f"sc_{family}_calendar",
            "target_symbols": ["SC"],
            "preferred_strategy_types": ["dsl_rule", "open_dsl"],
            "allowed_strategy_types": ["dsl_rule", "open_dsl"],
            "validation_focus": "candidate_target_only",
            "objective_profile": "high_precision",
            "trade_density_preference": "low",
            "regime_required": True,
            "cost_robust_required": True,
        }
        execution_assumptions = {
            "initial_capital": 1_000_000,
            "commission_rate": round(execution_profile.commission_rate, 6),
            "slippage_bps": round(execution_profile.slippage_bps, 4),
            "slippage_model": "fixed_plus_capacity_scaled",
            "market_impact_bps": round(execution_profile.market_impact_bps, 4),
            "tradability_filter": True,
            "capacity_participation_rate": round(execution_profile.capacity_participation_rate, 4),
            "margin_rate": round(execution_profile.margin_rate, 4),
            "contract_multiplier": int(execution_profile.contract_multiplier),
            "liquidity_bucket": execution_profile.liquidity_bucket,
            "max_contracts_per_rebalance": int(execution_profile.max_contracts_per_rebalance),
            "market_ruleset": execution_profile.market_ruleset,
            "sell_tax_rate": 0.0,
            "min_trade_lot": 1,
            "t_plus_one": False,
        }
        hypothesis_artifact = {
            "alpha_hypothesis": hypothesis_text,
            "failure_mode": {
                "primary_failure_mode": "delivery_or_structure_reversal",
                "stop_rule": deepcopy(risk_rules),
            },
            "target_universe_hypothesis": {
                "target_symbols": ["SC"],
                "target_symbol_policy": "prefer_intersection",
                "selection_mode": "explicit",
            },
            "family_hint": family,
            "holding_rationale": (
                "SC 远月方向单边依赖 backwardation carry + 趋势延续。"
                if family == "trend"
                else "买近抛远跨月对冲依赖 spread pullback 后的期限结构修复。"
            ),
            "alpha_half_life": 12 if family == "trend" else 8,
            "cost_sensitivity_grid": {
                "base_case": {
                    "commission_rate": execution_assumptions["commission_rate"],
                    "slippage_bps": execution_assumptions["slippage_bps"],
                    "market_impact_bps": execution_assumptions["market_impact_bps"],
                    "capacity_participation_rate": execution_assumptions["capacity_participation_rate"],
                },
                "stress_case": {
                    "commission_rate": round(execution_assumptions["commission_rate"] * 1.5, 6),
                    "slippage_bps": round(execution_assumptions["slippage_bps"] * 1.5, 4),
                    "market_impact_bps": round(execution_assumptions["market_impact_bps"] * 1.75, 4),
                    "capacity_participation_rate": round(execution_assumptions["capacity_participation_rate"] * 0.75, 4),
                },
            },
            "position_model": portfolio_spec["position_assumption"],
            "capacity_assumption": {
                "capital_buckets": deepcopy(capacity_panel),
                "liquidity_reference_contracts": execution_profile.liquidity_reference_contracts,
                "far_month_liquidity_haircut": execution_profile.far_month_liquidity_haircut,
            },
            "objective_profile": "high_precision",
            "trade_density_preference": "low",
            "entry_selectivity": "strict" if family == "trend" else "narrow",
            "regime_required": True,
            "cost_robust_required": True,
            "market_regime_assumption": {
                "preferred_regime": "backwardation",
                "avoid_regime": "extreme_near_month_premium",
                "regime_panel": deepcopy(regime_panel),
            },
            "validation_focus": "candidate_target_only",
        }
        candidate = {
            "status": "submitted",
            "name": strategy_name,
            "strategy_type": "dsl_rule",
            "generator_mode": "futures_calendar_research_adapter",
            "candidate_family": f"sc_{family}",
            "target_symbols": ["SC"],
            "stock_pool": {
                "selection_mode": "explicit",
                "symbols": ["SC"],
                "rationale": "SC 主任务只使用本地 ai_ready 曲线，目标标的固定为原油 SC。",
            },
            "research_task": research_task,
            "hypothesis": hypothesis_text,
            "hypothesis_artifact": hypothesis_artifact,
            "evidence_chain": evidence_chain,
            "prediction_contract": prediction_contract,
            "confidence_contract": self._build_confidence_contract(summary),
            "holding_horizon": holding_horizon,
            "trade_plan": trade_plan,
            "risk_rules": risk_rules,
            "position_sizing": position_sizing,
            "execution_notes": (
                "保守按 gross margin 与 far-month 流动性折扣约束仓位，交割保护窗口前主动减仓。"
            ),
            "rebalance_rule": rebalance_rule,
            "portfolio_spec": portfolio_spec,
            "execution_assumptions": execution_assumptions,
            "validation_profile": {
                "profile": "trade_rule_validation",
                "validation_focus": "candidate_target_only",
                "primary_validation_layer": "target",
                "objective_profile": "high_precision",
                "trade_density_preference": "low",
                "entry_selectivity": "strict" if family == "trend" else "narrow",
                "regime_required": True,
                "cost_robust_required": True,
            },
            "instrument_profile": instrument_profile,
            "constraint_check": constraint_check,
            "candidate_provenance": {
                "generator_mode": "futures_calendar_research_adapter",
                "source_data_path": str(self.data_path),
                "factory_target_status": "submitted",
            },
            "tags": [
                "futures",
                "calendar_spread" if family == "spread" else "calendar_trend",
                "gate3_candidate",
                "submitted",
            ],
            "dsl": {
                **deepcopy(raw_dsl),
                "metadata": {
                    **deepcopy(raw_dsl.get("metadata") or {}),
                    "target_symbols": ["SC"],
                    "stock_pool": {
                        "selection_mode": "explicit",
                        "symbols": ["SC"],
                    },
                    "portfolio_spec": deepcopy(portfolio_spec),
                    "execution_assumptions": deepcopy(execution_assumptions),
                    "validation_profile": {
                        "profile": "trade_rule_validation",
                        "validation_focus": "candidate_target_only",
                        "primary_validation_layer": "target",
                    },
                    "targeting_policy": {
                        "target_symbol_policy": "prefer_intersection",
                        "universe_expansion_policy": "allow_market_fallback",
                        "validation_focus": "candidate_target_only",
                    },
                    "constraint_check": deepcopy(constraint_check),
                    "instrument_profile": deepcopy(instrument_profile),
                },
            },
        }
        compiled_dsl = self._compile_strategy_blueprint_safe(
            candidate,
            market_frame=_to_ohlcv_projection(signal_series),
        )
        return candidate, raw_dsl, compiled_dsl

    def _materialize_result(
        self,
        frame: pd.DataFrame,
        *,
        family: str,
        ranked_item: dict[str, Any],
    ) -> StrategyResult:
        config = ranked_item["config"]
        execution_profile = ranked_item["execution_profile"]
        backtest = ranked_item["backtest"]
        capacity_panel = self._stress_test(
            frame,
            family=family,
            config=config,
            execution_profile=execution_profile,
        )
        candidate, raw_dsl, compiled_dsl = self._build_candidate(
            frame,
            family=family,
            config=config,
            execution_profile=execution_profile,
            backtest=backtest,
            capacity_panel=capacity_panel,
        )
        return StrategyResult(
            family=family,
            strategy_code=config.strategy_code,
            name=candidate["name"],
            config=asdict(config),
            execution_profile=asdict(execution_profile),
            summary=_round_dict_values(backtest["summary"]),
            regime_panel=_round_dict_values(backtest["regime_panel"]),
            capacity_panel=_round_dict_values({"rows": capacity_panel}).get("rows", []),
            trades=[_round_dict_values(trade) for trade in backtest["trades"]],
            candidate=_round_dict_values(candidate),
            raw_dsl=_round_dict_values(raw_dsl),
            compiled_dsl=_round_dict_values(compiled_dsl),
        )
    def build_research_context(
        self,
        results: list[StrategyResult],
        *,
        top_rankings: dict[str, list[dict[str, Any]]],
    ) -> dict[str, Any]:
        strategy_blocks = []
        for result in results:
            strategy_blocks.append(
                {
                    "strategy_code": result.strategy_code,
                    "name": result.name,
                    "family": result.family,
                    "instrument_profile": deepcopy(result.candidate.get("instrument_profile") or {}),
                    "parameters": deepcopy(result.config),
                    "summary": deepcopy(result.summary),
                    "execution_assumptions": deepcopy(result.candidate.get("execution_assumptions") or {}),
                    "failure_mode": deepcopy((result.candidate.get("hypothesis_artifact") or {}).get("failure_mode") or {}),
                }
            )
        leaderboard = []
        for family, items in top_rankings.items():
            for rank, item in enumerate(items[:5], start=1):
                summary = dict(item["backtest"]["summary"] or {})
                leaderboard.append(
                    {
                        "family": family,
                        "rank": rank,
                        "strategy_code": item["config"].strategy_code,
                        "annualized_return": round(_safe_float(summary.get("annualized_return")), 6),
                        "post_cost_sharpe": round(_safe_float(summary.get("post_cost_sharpe")), 6),
                        "max_drawdown": round(_safe_float(summary.get("max_drawdown")), 6),
                        "trade_count": _safe_int(summary.get("trade_count")),
                        "alpha_decay": round(_safe_float(summary.get("alpha_decay")), 6),
                    }
                )
        generalization_seed = {
            "logic_abstraction": [
                "稳定 backwardation + 近端不出现极端升水时，远月 carry 与趋势延续可以共振。",
                "spread z-score 回落但曲线单调性未破坏时，买近抛远更适合作为主交易方向。",
                "容量评估必须把 far-month 流动性折扣、gross margin 与交割保护同时纳入。",
            ],
            "failure_modes": [
                "现货持续弱势导致近端异常升水，期限结构反转。",
                "极端行情下单边趋势主导，短 spread 方向风险急剧放大。",
                "远月流动性恶化使得成本和回撤同步恶化。",
            ],
        }
        return {
            "strategy_context": {
                "adapter_name": "futures_calendar_research_adapter",
                "underlying": "SC",
                "objective_profile": "high_precision",
                "trade_density_preference": "low",
                "data_path": str(self.data_path),
                "window": {"start": "2018-07-26", "end": "2025-02-19"},
                "memo_summary": self._memo_text[:800],
                "roll_rule_summary": self._notes_text[:500],
                "strategies": strategy_blocks,
            },
            "backtest_summary": {
                "leaderboard": leaderboard,
                "selection_rule": (
                    "先筛 annualized_return>10% 且 trade_count>=6；"
                    "若无候选达标，则回退为按 annualized_return / post_cost_sharpe / win_rate / drawdown / trade_count 选最优候选。"
                ),
            },
            "regime_panel": {
                result.strategy_code: deepcopy(result.regime_panel)
                for result in results
            },
            "capacity_panel": {
                result.strategy_code: deepcopy(result.capacity_panel)
                for result in results
            },
            "generalization_seed": generalization_seed,
        }

    @staticmethod
    def _generalization_report(enable_online_validation: bool) -> dict[str, Any]:
        rows: list[dict[str, Any]] = []
        history_lookup: dict[str, Any] = {}
        fee_lookup: dict[str, Any] = {}
        if enable_online_validation:
            # PR-DQ5: 不直接调用外部 API。期货手续费和历史数据应从本地缓存读取。
            # 当本地无缓存时优雅降级（空 dict），不阻塞流程。
            import logging as _logging
            _logging.getLogger(__name__).debug(
                "futures_calendar_research: online_validation requested but external API calls disabled per data quality policy"
            )
        for code, label in GENERALIZATION_UNDERLYINGS:
            history = history_lookup.get(code)
            fee_row = fee_lookup.get(code) or fee_lookup.get(f"{code}0") or {}
            history_ok = isinstance(history, pd.DataFrame) and not history.empty and len(history) >= 120
            fee_ok = bool(fee_row)
            validation_mode = (
                "light_online_validation"
                if enable_online_validation and history_ok
                else "candidate_generation_only"
            )
            rows.append(
                {
                    "underlying": code,
                    "name": label,
                    "validation_mode": validation_mode,
                    "history_available": bool(history_ok),
                    "fee_available": bool(fee_ok),
                    "light_logic_fit": "stable_curve_needed",
                    "notes": (
                        "在线历史与费用可用，保留为轻量验证种子。"
                        if validation_mode == "light_online_validation"
                        else "保留候选生成 + 轻量逻辑验证，不阻断 SC 主闭环。"
                    ),
                }
            )
        return {"rows": rows}

    async def _llm_optimize_candidates(
        self,
        *,
        research_context: dict[str, Any],
        baseline_candidates: list[dict[str, Any]],
    ) -> dict[str, Any]:
        try:
            from ...infrastructure.mcp_services import get_strategy_llm_provider_loader
            get_strategy_llm_provider = get_strategy_llm_provider_loader()
        except Exception as exc:
            return {
                "status": "provider_import_failed",
                "reason": str(exc),
                "candidates": baseline_candidates,
            }
        provider = get_strategy_llm_provider()
        if not provider.is_enabled():
            return {
                "status": "provider_disabled",
                "reason": "StrategyLLMProvider is not configured in the current environment.",
                "candidates": baseline_candidates,
            }
        task = {
            "task_id": "sc_calendar_llm_refine",
            "task_source": "snapshot",
            "opportunity_type": "futures_calendar_refine",
            "target_symbols": ["SC"],
            "preferred_strategy_types": ["dsl_rule", "open_dsl"],
            "allowed_strategy_types": ["dsl_rule", "open_dsl"],
            "validation_focus": "candidate_target_only",
        }
        llm_timeout_sec = min(max(_safe_float(getattr(provider.config, "connect_timeout_sec", 5.0), 5.0) + 2.0, 5.0), 8.0)
        try:
            provider_payload = await asyncio.wait_for(
                provider.generate_candidates(
                    snapshot={"underlying": "SC"},
                    market_frame=_to_ohlcv_projection(pd.Series(np.linspace(1.0, 2.0, 80))),
                    research_context=research_context,
                    parent_strategies=baseline_candidates,
                    history_summary=[],
                    research_task=task,
                    limit=2,
                ),
                timeout=llm_timeout_sec,
            )
        except asyncio.TimeoutError:
            return {
                "status": "provider_timeout",
                "reason": f"StrategyLLMProvider did not respond within {llm_timeout_sec:.1f}s; fallback to baseline candidates.",
                "candidates": baseline_candidates,
            }
        except Exception as exc:
            return {
                "status": "provider_failed",
                "reason": str(exc),
                "candidates": baseline_candidates,
            }
        candidates = list((provider_payload or {}).get("candidates") or [])
        return {
            "status": "provider_succeeded" if candidates else "provider_empty",
            "reason": None,
            "candidates": candidates or baseline_candidates,
            "provider_metrics": dict((provider_payload or {}).get("request_metrics") or {}),
        }

    def _write_reports(
        self,
        *,
        output_dir: Path,
        trend_result: StrategyResult,
        spread_result: StrategyResult,
        top_rankings: dict[str, list[dict[str, Any]]],
        research_context: dict[str, Any],
        llm_bundle: dict[str, Any],
        generalization_report: dict[str, Any],
    ) -> dict[str, str]:
        output_dir = _ensure_output_dir(output_dir)
        trend_summary = dict(trend_result.summary)
        spread_summary = dict(spread_result.summary)
        screen_pass_counts = {
            family: sum(
                1
                for item in items
                if _safe_float((item.get("backtest") or {}).get("summary", {}).get("annualized_return")) >= 0.10
                and _safe_int((item.get("backtest") or {}).get("summary", {}).get("trade_count")) >= 6
            )
            for family, items in top_rankings.items()
        }
        ranking_rows = []
        for family, items in top_rankings.items():
            for rank, item in enumerate(items[:5], start=1):
                summary = dict(item["backtest"]["summary"] or {})
                ranking_rows.append(
                    {
                        "family": family,
                        "rank": rank,
                        "strategy_code": item["config"].strategy_code,
                        "annualized_return": f"{_safe_float(summary.get('annualized_return')):.2%}",
                        "post_cost_sharpe": f"{_safe_float(summary.get('post_cost_sharpe')):.2f}",
                        "max_drawdown": f"{_safe_float(summary.get('max_drawdown')):.2%}",
                        "trade_count": _safe_int(summary.get("trade_count")),
                        "alpha_decay": f"{_safe_float(summary.get('alpha_decay')):.2f}",
                    }
                )
        ranking_table = _markdown_table(
            ranking_rows,
            [
                ("Family", "family"),
                ("Rank", "rank"),
                ("Code", "strategy_code"),
                ("Ann.Return", "annualized_return"),
                ("Post Sharpe", "post_cost_sharpe"),
                ("Max DD", "max_drawdown"),
                ("Trades", "trade_count"),
                ("Alpha Decay", "alpha_decay"),
            ],
        )
        full_report_md = "\n".join(
            [
                "# SC 原油跨月价差全量回测报告",
                "",
                f"- 数据源：`{self.data_path}`",
                "- 窗口：2018-07-26 至 2025-02-19",
                "- regime 口径：`spread_1_2 > 0 -> backwardation`，其余归入 `contango_or_flat`。",
                "- 交割保护：front roll 前 3 日禁止持仓。",
                "",
                "## 候选排序",
                ranking_table,
                "",
                "## 门槛筛选",
                f"- 趋势策略通过 `annualized_return>10% & trade_count>=6` 的候选数：{screen_pass_counts.get('trend', 0)}",
                f"- 套利策略通过 `annualized_return>10% & trade_count>=6` 的候选数：{screen_pass_counts.get('spread', 0)}",
                "- 若某一策略族无达标候选，本报告保留保守成本/容量假设下的最优备选，不强行把未达标结果包装成通过门槛。",
                "",
                "## 趋势策略冠军",
                f"- 名称：`{trend_result.name}`",
                f"- 年化：{_safe_float(trend_summary.get('annualized_return')):.2%}",
                f"- Post-cost Sharpe：{_safe_float(trend_summary.get('post_cost_sharpe')):.2f}",
                f"- 最大回撤：{_safe_float(trend_summary.get('max_drawdown')):.2%}",
                f"- 交易数：{_safe_int(trend_summary.get('trade_count'))}",
                "",
                "## 套利策略冠军",
                f"- 名称：`{spread_result.name}`",
                f"- 年化：{_safe_float(spread_summary.get('annualized_return')):.2%}",
                f"- Post-cost Sharpe：{_safe_float(spread_summary.get('post_cost_sharpe')):.2f}",
                f"- 最大回撤：{_safe_float(spread_summary.get('max_drawdown')):.2%}",
                f"- 交易数：{_safe_int(spread_summary.get('trade_count'))}",
                "",
                "## 研究上下文",
                f"- research_context blocks：{', '.join(research_context.keys())}",
                f"- LLM enrichment status：`{llm_bundle.get('status')}`",
                f"- LLM note：{llm_bundle.get('reason') or 'provider returned optimized candidates.'}",
                "",
            ]
        )
        full_report_path = output_dir / "sc_full_backtest_report.md"
        full_report_path.write_text(full_report_md, encoding="utf-8")

        stress_rows = []
        for strategy_label, result in (("trend", trend_result), ("spread", spread_result)):
            for item in result.capacity_panel:
                stress_rows.append({"strategy": strategy_label, **item})
        stress_frame = pd.DataFrame(stress_rows)
        stress_csv_path = output_dir / "sc_capacity_stress_matrix.csv"
        stress_frame.to_csv(stress_csv_path, index=False)
        stress_md_path = output_dir / "sc_capacity_stress_matrix.md"
        stress_md_path.write_text(
            "# SC 五档资金压力测试\n\n"
            + _markdown_table(
                stress_rows,
                [
                    ("Strategy", "strategy"),
                    ("Capital", "capital"),
                    ("Ann.Return", "annualized_return"),
                    ("Post Sharpe", "post_cost_sharpe"),
                    ("Max DD", "max_drawdown"),
                    ("Trades", "trade_count"),
                    ("Cap", "capacity_limit_contracts"),
                    ("Bind", "binding_constraint"),
                ],
            )
            + "\n",
            encoding="utf-8",
        )

        candidate_bundle = {
            "llm_status": llm_bundle.get("status"),
            "llm_reason": llm_bundle.get("reason"),
            "baseline_candidates": [trend_result.candidate, spread_result.candidate],
            "optimized_candidates": list(llm_bundle.get("candidates") or []),
            "compiled_dsl": {
                trend_result.strategy_code: trend_result.compiled_dsl,
                spread_result.strategy_code: spread_result.compiled_dsl,
            },
            "research_context": research_context,
        }
        candidate_json_path = output_dir / "sc_ai_candidate_bundle.json"
        candidate_json_path.write_text(
            json.dumps(candidate_bundle, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        candidate_md_path = output_dir / "sc_ai_candidate_bundle.md"
        candidate_md_path.write_text(
            "\n".join(
                [
                    "# SC AI Candidate Bundle",
                    "",
                    f"- LLM status: `{llm_bundle.get('status')}`",
                    f"- LLM note: {llm_bundle.get('reason') or 'provider returned optimized candidates.'}",
                    "",
                    "## Baseline Candidates",
                    f"- `{trend_result.name}` -> `{trend_result.strategy_code}`",
                    f"- `{spread_result.name}` -> `{spread_result.strategy_code}`",
                    "",
                    "## Research Context Blocks",
                    f"- {', '.join(research_context.keys())}",
                    "",
                ]
            ),
            encoding="utf-8",
        )

        generalization_md_path = output_dir / "cross_asset_candidate_validation.md"
        generalization_md_path.write_text(
            "# 跨品种候选验证报告\n\n"
            + _markdown_table(
                list(generalization_report.get("rows") or []),
                [
                    ("Underlying", "underlying"),
                    ("Name", "name"),
                    ("Mode", "validation_mode"),
                    ("History", "history_available"),
                    ("Fee", "fee_available"),
                    ("Notes", "notes"),
                ],
            )
            + "\n",
            encoding="utf-8",
        )

        research_payload_path = output_dir / "sc_research_context_payload.json"
        research_payload_path.write_text(
            json.dumps(research_context, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        return {
            "full_backtest_report_md": str(full_report_path),
            "capacity_stress_csv": str(stress_csv_path),
            "capacity_stress_md": str(stress_md_path),
            "candidate_bundle_json": str(candidate_json_path),
            "candidate_bundle_md": str(candidate_md_path),
            "cross_asset_validation_md": str(generalization_md_path),
            "research_context_json": str(research_payload_path),
        }

    async def run(
        self,
        *,
        enable_online_generalization: bool = False,
    ) -> dict[str, Any]:
        output_dir = _ensure_output_dir(self.output_dir)
        frame = self.add_features(self.load_curve_frame(self.data_path))
        trend_ranked = self._rank_results(self._run_trend_grid(frame, capital=1_000_000.0))
        spread_ranked = self._rank_results(self._run_spread_grid(frame, capital=1_000_000.0))
        trend_result = self._materialize_result(frame, family="trend", ranked_item=trend_ranked[0])
        spread_result = self._materialize_result(frame, family="spread", ranked_item=spread_ranked[0])
        research_context = self.build_research_context(
            [trend_result, spread_result],
            top_rankings={"trend": trend_ranked, "spread": spread_ranked},
        )
        llm_bundle = await self._llm_optimize_candidates(
            research_context=research_context,
            baseline_candidates=[trend_result.candidate, spread_result.candidate],
        )
        generalization_report = self._generalization_report(
            enable_online_validation=enable_online_generalization,
        )
        output_paths = self._write_reports(
            output_dir=output_dir,
            trend_result=trend_result,
            spread_result=spread_result,
            top_rankings={"trend": trend_ranked, "spread": spread_ranked},
            research_context=research_context,
            llm_bundle=llm_bundle,
            generalization_report=generalization_report,
        )
        return {
            "output_dir": str(output_dir),
            "trend": asdict(trend_result),
            "spread": asdict(spread_result),
            "research_context": research_context,
            "llm_bundle": llm_bundle,
            "generalization_report": generalization_report,
            "output_paths": output_paths,
        }


__all__ = [
    "CAPITAL_BUCKETS",
    "DEFAULT_OUTPUT_DIR",
    "DEFAULT_SC_PACK_DIR",
    "DEFAULT_SC_DATA_PATH",
    "FuturesCalendarResearchAdapter",
    "run_sc_calendar_research",
]
