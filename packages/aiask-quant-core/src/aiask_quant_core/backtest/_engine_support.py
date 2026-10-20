"""回测引擎 - BacktestEngine 核心类"""
from __future__ import annotations

# --- _engine_support_parts/runtime.py ---
from typing import List, Dict, Any, Optional, Union, Tuple
import numpy as np

from aiask_quant_core.slippage import SlippageCalculator
from .utils import (
    _ensure_dict_list,
    _compute_slippage_rate,
    _resolve_slippage_model,
    normalize_trade_date,
)
from .strategy_base import StrategySignalEvent
from .strategies import (
    _backtest_ma_cross_jit,
    _backtest_ma_cross_with_trades_jit,
    _backtest_momentum_jit,
    _backtest_rsi_jit,
)
import logging

logger = logging.getLogger(__name__)


_SLIPPAGE_NOTE = "JIT路径使用均值化滑点估算，实际交易成本可能偏高"
_ARRIVAL_PRICE_POLICY_BPS = {
    "close_proxy": 1.0,
    "close": 1.0,
    "same_close_proxy": 1.2,
    "twap_proxy": 1.2,
    "twap": 1.2,
    "vwap_proxy": 0.8,
    "vwap": 0.8,
    "next_open_proxy": 2.0,
    "next_open": 2.0,
    "event_open_proxy": 2.5,
}
_CAPACITY_BUCKET_BPS = {
    "mega": -2.0,
    "large": 0.0,
    "mid": 2.0,
    "small": 5.0,
    "micro": 8.0,
}
_POSITION_ASSUMPTION_PCT = {
    "single_name_full_notional": 1.0,
    "single_name": 1.0,
    "equal_weight_proxy": 0.2,
    "equal_weight": 0.2,
    "half_notional": 0.5,
}
_A_SHARE_MARKET_RULESETS = {"cn_equity", "a_share", "ashare", "cn_stock", "china_equity"}

def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return float(default)
    return float(result) if np.isfinite(result) else float(default)

def _resolve_market_ruleset(args: Dict[str, Any]) -> str:
    return str(args.get("market_ruleset") or "").strip().lower()

def _is_a_share_ruleset(args: Dict[str, Any]) -> bool:
    return _resolve_market_ruleset(args) in _A_SHARE_MARKET_RULESETS

def _resolve_position_pct(args: Dict[str, Any]) -> float:
    explicit_max_position = _safe_float(args.get("max_position_pct"), 0.0)
    if explicit_max_position > 0:
        return max(0.01, min(explicit_max_position, 1.0))

    position_assumption = str(args.get("position_assumption") or "").strip().lower()
    target_weight_scheme = str(args.get("target_weight_scheme") or "").strip().lower()
    if position_assumption in _POSITION_ASSUMPTION_PCT:
        return _POSITION_ASSUMPTION_PCT[position_assumption]
    if target_weight_scheme in _POSITION_ASSUMPTION_PCT:
        return _POSITION_ASSUMPTION_PCT[target_weight_scheme]
    if target_weight_scheme == "equal_weight":
        return 0.2
    return 1.0

def _estimate_implementation_shortfall(
    payload: Dict[str, Any],
    args: Dict[str, Any],
    *,
    closes: Optional[np.ndarray] = None,
    volumes: Optional[np.ndarray] = None,
    explicit_slippage_rate: float = 0.0,
    model_slippage_rate: float = 0.0,
    market_impact_bps: float = 0.0,
) -> tuple[float, str, Dict[str, Any], Dict[str, Any], Dict[str, Any]]:
    price_arr = np.asarray(closes if closes is not None else [], dtype=float)
    volume_arr = np.asarray(volumes if volumes is not None else [], dtype=float)
    positive_prices = price_arr[np.isfinite(price_arr) & (price_arr > 0)]
    positive_volumes = volume_arr[np.isfinite(volume_arr) & (volume_arr > 0)]

    average_price = float(np.mean(positive_prices)) if positive_prices.size else 0.0
    average_volume_20 = float(np.mean(positive_volumes[-20:])) if positive_volumes.size else 0.0
    initial_capital = _safe_float(args.get("initial_capital"), 100000.0)
    position_pct = _resolve_position_pct(args)
    estimated_order_notional = max(0.0, initial_capital * position_pct)
    estimated_order_shares = estimated_order_notional / average_price if average_price > 0 else 0.0

    estimated_participation_rate = (
        estimated_order_shares / average_volume_20
        if average_volume_20 > 0
        else 0.0
    )
    configured_participation_rate = _safe_float(args.get("capacity_participation_rate"), 0.0)
    effective_participation_rate = max(estimated_participation_rate, configured_participation_rate)
    adv_ratio_limit = _safe_float(args.get("adv_ratio_limit"), 0.0)
    adv_utilization = (effective_participation_rate / adv_ratio_limit) if adv_ratio_limit > 0 else None

    arrival_policy = str(args.get("arrival_price_policy") or "next_open_proxy").strip().lower()
    arrival_bps = _ARRIVAL_PRICE_POLICY_BPS.get(arrival_policy)
    if arrival_bps is None:
        arrival_bps = 2.0 if "open" in arrival_policy else 1.0

    effective_slippage_bps = max(0.0, explicit_slippage_rate if explicit_slippage_rate > 0 else model_slippage_rate) * 10000.0
    tradability_filter = bool(args.get("tradability_filter", False) or payload.get("tradability_filter"))
    total_days = int(payload.get("total_days") or len(price_arr) or 0)
    if total_days and payload.get("tradable_days") is not None:
        tradable_days = int(payload.get("tradable_days") or 0)
    elif total_days:
        tradable_days = int(np.sum(volume_arr > 0)) if volume_arr.size else total_days
    else:
        tradable_days = 0
    tradable_ratio = (tradable_days / total_days) if total_days else 1.0

    tradability_bps = 0.0
    if tradability_filter:
        tradability_bps += max(0.0, (0.98 - tradable_ratio) * 150.0)
    elif tradable_ratio < 0.95:
        tradability_bps += max(0.0, (0.95 - tradable_ratio) * 60.0)
    if average_volume_20 <= 0:
        tradability_bps = max(tradability_bps, 12.0)

    capacity_bps = 0.0
    if effective_participation_rate > 0:
        capacity_bps += min(45.0, effective_participation_rate * 2500.0)
    if adv_utilization is not None and adv_utilization > 1.0:
        capacity_bps += min(60.0, (adv_utilization - 1.0) * 25.0)
    capacity_bucket = str(args.get("capacity_bucket") or "").strip().lower()
    if capacity_bucket:
        capacity_bps += _CAPACITY_BUCKET_BPS.get(capacity_bucket, 0.0)
    capacity_bps = max(0.0, capacity_bps)

    estimated_total_bps = effective_slippage_bps + max(0.0, market_impact_bps) + arrival_bps + tradability_bps + capacity_bps
    explicit_override = _safe_float(args.get("implementation_shortfall_proxy"), 0.0)
    if explicit_override > 0:
        source = "explicit_input"
        final_total_bps = explicit_override
    else:
        source = "estimated"
        final_total_bps = estimated_total_bps

    components = {
        "override_input_bps": round(explicit_override, 4) if explicit_override > 0 else None,
        "estimated_total_bps": round(estimated_total_bps, 4),
        "effective_total_bps": round(final_total_bps, 4),
        "effective_slippage_bps": round(effective_slippage_bps, 4),
        "market_impact_bps": round(max(0.0, market_impact_bps), 4),
        "arrival_bps": round(arrival_bps, 4),
        "tradability_bps": round(tradability_bps, 4),
        "capacity_bps": round(capacity_bps, 4),
        "arrival_price_policy": arrival_policy,
    }
    tradability_summary = {
        "tradability_filter": tradability_filter,
        "tradable_days": tradable_days if total_days else None,
        "total_days": total_days or None,
        "tradable_ratio": round(tradable_ratio, 4) if total_days else None,
        "tradability_penalty_bps": round(tradability_bps, 4),
    }
    capacity_summary = {
        "capacity_participation_rate": round(configured_participation_rate, 6) if configured_participation_rate else None,
        "adv_ratio_limit": round(adv_ratio_limit, 6) if adv_ratio_limit else None,
        "average_volume_20": round(average_volume_20, 4) if average_volume_20 > 0 else None,
        "capacity_bucket": capacity_bucket or None,
        "estimated_position_pct": round(position_pct, 4),
        "estimated_order_notional": round(estimated_order_notional, 4),
        "estimated_order_shares": round(estimated_order_shares, 4) if estimated_order_shares > 0 else None,
        "estimated_participation_rate": round(estimated_participation_rate, 6) if estimated_participation_rate > 0 else 0.0,
        "effective_participation_rate": round(effective_participation_rate, 6) if effective_participation_rate > 0 else 0.0,
        "adv_utilization": round(adv_utilization, 4) if adv_utilization is not None else None,
        "capacity_penalty_bps": round(capacity_bps, 4),
    }
    return round(final_total_bps, 4), source, components, tradability_summary, capacity_summary

def _attach_equity_curve(payload: Dict[str, Any], equity: np.ndarray) -> None:
    """将权益曲线（降采样至最多500点）和滑点标注附加到回测结果"""
    eq = equity.tolist()
    step = max(1, len(eq) // 500)
    payload['equity_curve'] = eq[::step]
    payload['slippage_model_note'] = _SLIPPAGE_NOTE

def _extract_benchmark_returns(
    params: Optional[Dict[str, Any]],
    expected_len: int,
    *,
    strategy_dates: Optional[list[str]] = None,
) -> Optional[np.ndarray]:
    if not params or expected_len <= 0:
        return None
    raw = params.get('benchmark_returns')
    if isinstance(raw, (list, tuple, np.ndarray)):
        arr = np.asarray(raw, dtype=float)
        arr = arr[np.isfinite(arr)]
        return arr[-expected_len:] if arr.size else None

    bench_klines = params.get('benchmark_klines')
    if isinstance(bench_klines, list) and bench_klines:
        items = _ensure_dict_list(bench_klines)
        # Prefer calendar join when strategy dates are supplied.
        if strategy_dates:
            by_date: dict[str, float] = {}
            for row in items:
                day = normalize_trade_date(
                    row.get("date")
                    or row.get("trade_date")
                    or row.get("time")
                    or row.get("datetime")
                )
                if not day:
                    continue
                try:
                    by_date[day] = float(row.get("close", 0) or 0)
                except (TypeError, ValueError) as _swallowed:
                    logger.warning("packages/aiask-quant-core/src/aiask_quant_core/backtest/_engine_support.py:228: swallowed %s and continued: %s", type(_swallowed).__name__, _swallowed)
                    continue
            closes = []
            for day in strategy_dates:
                if day not in by_date:
                    return None  # incomplete calendar — do not silently mis-pair
                closes.append(by_date[day])
            closes_arr = np.asarray(closes, dtype=float)
            if closes_arr.size < 2:
                return None
            prev = closes_arr[:-1]
            curr = closes_arr[1:]
            mask = prev > 0
            returns = np.zeros(curr.shape[0], dtype=float)
            returns[mask] = (curr[mask] - prev[mask]) / prev[mask]
            returns = returns[np.isfinite(returns)]
            return returns if returns.size else None

        closes = np.array([float(k.get('close', 0) or 0) for k in items], dtype=float)
        if closes.size >= 2:
            prev = closes[:-1]
            curr = closes[1:]
            mask = prev > 0
            returns = np.zeros(curr.shape[0], dtype=float)
            returns[mask] = (curr[mask] - prev[mask]) / prev[mask]
            returns = returns[np.isfinite(returns)]
            return returns[-expected_len:] if returns.size else None
    return None

def _attach_advanced_metrics(payload: Dict[str, Any], equity: np.ndarray, params: Optional[Dict[str, Any]] = None) -> None:
    eq = np.asarray(equity, dtype=float)
    if eq.size < 2:
        payload.setdefault('annual_return', 0.0)
        payload.setdefault('annual_volatility', 0.0)
        payload.setdefault('sortino_ratio', 0.0)
        payload.setdefault('calmar_ratio', 0.0)
        payload.setdefault('omega_ratio', 0.0)
        payload.setdefault('benchmark_return', None)
        payload.setdefault('excess_return', None)
        payload.setdefault('information_ratio', None)
        return

    valid_prev = eq[:-1] > 0
    daily_returns = np.array([], dtype=float)
    if np.any(valid_prev):
        daily_returns = (eq[1:][valid_prev] - eq[:-1][valid_prev]) / eq[:-1][valid_prev]
        daily_returns = daily_returns[np.isfinite(daily_returns)]

    annual_return = 0.0
    final_capital = float(payload.get('final_capital') or 0.0)
    initial_capital = float(payload.get('initial_capital') or 0.0)
    if daily_returns.size > 0 and initial_capital > 0 and final_capital > 0:
        annual_return = float((final_capital / initial_capital) ** (252 / daily_returns.size) - 1)

    annual_volatility = float(np.std(daily_returns) * np.sqrt(252)) if daily_returns.size > 1 else 0.0
    risk_free_rate = 0.02
    downside = daily_returns[daily_returns < 0]
    downside_volatility = float(np.std(downside) * np.sqrt(252)) if downside.size > 0 else 0.0
    if downside_volatility > 0:
        sortino_ratio = (annual_return - risk_free_rate) / downside_volatility
    else:
        sortino_ratio = None if annual_return > risk_free_rate else 0.0

    max_drawdown = abs(float(payload.get('max_drawdown') or 0.0))
    calmar_ratio = annual_return / max_drawdown if max_drawdown > 0 else (None if annual_return > 0 else 0.0)

    gains = float(np.clip(daily_returns, 0, None).sum()) if daily_returns.size > 0 else 0.0
    losses = float(np.clip(-daily_returns, 0, None).sum()) if daily_returns.size > 0 else 0.0
    omega_ratio = gains / losses if losses > 0 else (None if gains > 0 else 0.0)

    payload['annual_return'] = round(float(annual_return), 6)
    payload['annual_volatility'] = round(float(annual_volatility), 6)
    payload['sortino_ratio'] = None if sortino_ratio is None else round(float(sortino_ratio), 6)
    payload['calmar_ratio'] = None if calmar_ratio is None else round(float(calmar_ratio), 6)
    payload['omega_ratio'] = None if omega_ratio is None else round(float(omega_ratio), 6)

    benchmark_returns = _extract_benchmark_returns(params, daily_returns.size)
    if benchmark_returns is not None and benchmark_returns.size > 0 and daily_returns.size > 0:
        aligned = min(int(benchmark_returns.size), int(daily_returns.size))
        strat_slice = daily_returns[-aligned:]
        bench_slice = benchmark_returns[-aligned:]
        benchmark_return = float(np.prod(1 + bench_slice) - 1)
        strategy_return = float(np.prod(1 + strat_slice) - 1)
        excess_returns = strat_slice - bench_slice
        tracking_error = float(np.std(excess_returns) * np.sqrt(252)) if aligned > 1 else 0.0
        information_ratio = ((float(np.mean(excess_returns)) * 252) / tracking_error) if tracking_error > 0 else 0.0
        payload['benchmark_return'] = round(benchmark_return, 6)
        payload['excess_return'] = round(strategy_return - benchmark_return, 6)
        payload['information_ratio'] = round(float(information_ratio), 6)
    else:
        payload['benchmark_return'] = None
        payload['excess_return'] = None
        payload['information_ratio'] = None

def _attach_execution_audit(
    payload: Dict[str, Any],
    params: Optional[Dict[str, Any]] = None,
    *,
    closes: Optional[np.ndarray] = None,
    volumes: Optional[np.ndarray] = None,
) -> None:
    args = dict(params or {})
    explicit_commission = _safe_float(args.get("commission", 0.0003), 0.0)
    explicit_slippage = _safe_float(args.get("slippage", 0.0), 0.0)
    model_slippage = _compute_slippage_rate(
        np.asarray(closes if closes is not None else [], dtype=float),
        np.asarray(volumes if volumes is not None else [], dtype=float),
        args,
        0.0,
    ) if closes is not None and volumes is not None else 0.0
    market_impact_bps = _safe_float(args.get("market_impact_bps", 0.0), 0.0)
    implementation_shortfall_proxy, implementation_source, shortfall_components, tradability_summary, capacity_summary = _estimate_implementation_shortfall(
        payload,
        args,
        closes=closes,
        volumes=volumes,
        explicit_slippage_rate=explicit_slippage,
        model_slippage_rate=model_slippage,
        market_impact_bps=market_impact_bps,
    )
    position_assumption = str(
        args.get("position_assumption")
        or ("equal_weight_proxy" if str(args.get("target_weight_scheme") or "").strip().lower() == "equal_weight" else "single_name_full_notional")
    ).strip() or "single_name_full_notional"
    market_ruleset = _resolve_market_ruleset(args)
    a_share_rules = _is_a_share_ruleset(args)
    sell_tax_rate = _safe_float(args.get("sell_tax_rate"), 0.001 if a_share_rules else 0.0)
    min_trade_lot = max(1, int(args.get("min_trade_lot", 100 if a_share_rules else 1) or (100 if a_share_rules else 1)))
    t_plus_one = bool(args.get("t_plus_one", a_share_rules))

    payload["cost_assumptions"] = {
        "commission_rate": round(explicit_commission, 8),
        "slippage_rate": round(explicit_slippage if explicit_slippage > 0 else model_slippage, 8),
        "slippage_model": args.get("slippage_model"),
        "arrival_price_policy": str(args.get("arrival_price_policy") or "next_open_proxy"),
        "market_impact_bps": round(market_impact_bps, 4),
        "implementation_shortfall_proxy": round(implementation_shortfall_proxy, 4),
        "implementation_shortfall_model_source": implementation_source,
        "market_ruleset": market_ruleset or None,
        "sell_tax_rate": round(sell_tax_rate, 6),
        "min_trade_lot": int(min_trade_lot),
        "t_plus_one": bool(t_plus_one),
    }
    payload["explicit_cost_breakdown"] = {
        "commission_rate": round(explicit_commission, 8),
        "sell_tax_rate": round(sell_tax_rate, 6),
    }
    payload["implicit_cost_breakdown"] = {
        "slippage_rate": round(explicit_slippage if explicit_slippage > 0 else model_slippage, 8),
        "market_impact_bps": round(market_impact_bps, 4),
        "implementation_shortfall_proxy": round(implementation_shortfall_proxy, 4),
        "implementation_shortfall_model_source": implementation_source,
        "market_ruleset": market_ruleset or None,
    }
    payload["implementation_shortfall_proxy"] = round(implementation_shortfall_proxy, 4)
    payload["implementation_shortfall_model_source"] = implementation_source
    payload["implementation_shortfall_components"] = shortfall_components
    payload["tradability_summary"] = tradability_summary
    payload["capacity_summary"] = capacity_summary
    payload["position_assumption"] = position_assumption
    payload["market_ruleset"] = market_ruleset or None
    payload["sell_tax_rate"] = round(sell_tax_rate, 6)
    payload["min_trade_lot"] = int(min_trade_lot)
    payload["t_plus_one"] = bool(t_plus_one)

def _compute_profit_factor(payload: Dict[str, Any]) -> float | None:
    """从 round_trip_positions 计算利润因子 (gross_profit / gross_loss)。"""
    round_trips = list(payload.get("round_trip_positions") or [])
    if not round_trips:
        # Fallback: use win_rate and trades_count for rough estimate
        win_rate = float(payload.get("win_rate") or 0.0)
        trades_count = int(payload.get("trades_count") or 0)
        if trades_count <= 0 or win_rate <= 0:
            return 0.0
        # Cannot compute without actual PnL data
        return 0.0

    gross_profit = 0.0
    gross_loss = 0.0
    for rt in round_trips:
        pnl = float(rt.get("realized_pnl") or 0.0)
        if pnl > 0:
            gross_profit += pnl
        elif pnl < 0:
            gross_loss += abs(pnl)

    if gross_loss <= 0:
        return None if gross_profit > 0 else 0.0
    return gross_profit / gross_loss


def _compute_expectancy(payload: Dict[str, Any]) -> float:
    """计算期望值 = avg_win * win_rate - avg_loss * (1 - win_rate)。"""
    round_trips = list(payload.get("round_trip_positions") or [])
    if not round_trips:
        return 0.0

    wins = [float(rt.get("realized_pnl") or 0.0) for rt in round_trips if float(rt.get("realized_pnl") or 0.0) > 0]
    losses = [abs(float(rt.get("realized_pnl") or 0.0)) for rt in round_trips if float(rt.get("realized_pnl") or 0.0) < 0]

    total = len(wins) + len(losses)
    if total <= 0:
        return 0.0

    avg_win = sum(wins) / len(wins) if wins else 0.0
    avg_loss = sum(losses) / len(losses) if losses else 0.0
    win_rate = len(wins) / total

    return avg_win * win_rate - avg_loss * (1 - win_rate)


def _finalize_backtest_payload(
    payload: Dict[str, Any],
    equity: np.ndarray,
    params: Optional[Dict[str, Any]] = None,
    *,
    closes: Optional[np.ndarray] = None,
    volumes: Optional[np.ndarray] = None,
) -> None:
    _attach_equity_curve(payload, equity)
    _attach_advanced_metrics(payload, equity, params=params)
    _attach_execution_audit(payload, params=params, closes=closes, volumes=volumes)
    # Compute profit_factor and expectancy from round trips
    profit_factor = _compute_profit_factor(payload)
    expectancy = _compute_expectancy(payload)
    payload["profit_factor"] = None if profit_factor is None else round(float(profit_factor), 4)
    payload["expectancy"] = round(expectancy, 4)
    # Compute avg_win / avg_loss
    round_trips = list(payload.get("round_trip_positions") or [])
    if round_trips:
        wins = [float(rt.get("realized_pnl") or 0.0) for rt in round_trips if float(rt.get("realized_pnl") or 0.0) > 0]
        losses = [abs(float(rt.get("realized_pnl") or 0.0)) for rt in round_trips if float(rt.get("realized_pnl") or 0.0) < 0]
        payload["avg_win"] = round(sum(wins) / len(wins), 4) if wins else 0.0
        payload["avg_loss"] = round(sum(losses) / len(losses), 4) if losses else 0.0
        payload["payoff_ratio"] = round(
            (sum(wins) / len(wins)) / (sum(losses) / len(losses)), 4
        ) if wins and losses and sum(losses) > 0 else 0.0

def _get_limit_ratio(code: str) -> float:
    """根据股票代码判断涨跌停幅度。"""
    c = str(code).strip()
    for prefix in ("sh", "sz", "bj", "SH", "SZ", "BJ"):
        if c.startswith(prefix):
            c = c[len(prefix):]
            break
    if c.startswith("300") or c.startswith("301") or c.startswith("688"):
        return 0.20
    return 0.10

def _build_tradability_mask(
    closes: np.ndarray,
    volumes: Optional[np.ndarray] = None,
    code: str = "",
    is_st: bool = False,
) -> np.ndarray:
    """构建可交易性掩码（停牌 + 涨跌停保守过滤）。"""
    n = len(closes)
    mask = np.ones(n, dtype=bool)

    if volumes is not None and len(volumes) == n:
        mask &= volumes > 0

    limit_ratio = 0.05 if is_st else _get_limit_ratio(code)
    tolerance = 0.002
    for i in range(1, n):
        prev = closes[i - 1]
        if prev <= 0:
            continue
        change = (closes[i] - prev) / prev
        if change >= limit_ratio - tolerance or change <= -(limit_ratio - tolerance):
            mask[i] = False
    return mask

def _round_down_lot(shares: int, lot_size: int) -> int:
    normalized_shares = max(0, int(shares or 0))
    normalized_lot = max(1, int(lot_size or 1))
    if normalized_lot <= 1:
        return normalized_shares
    return (normalized_shares // normalized_lot) * normalized_lot

def _rolling_average_volume(volumes: np.ndarray, index: int, window: int = 20) -> float:
    if index < 0:
        return 0.0
    start = max(0, int(index) - max(1, int(window or 20)) + 1)
    slice_ = np.asarray(volumes[start : int(index) + 1], dtype=float)
    positive = slice_[np.isfinite(slice_) & (slice_ > 0)]
    if positive.size == 0:
        return 0.0
    return float(np.mean(positive))

def _estimate_capacity_penalty_bps(
    args: Dict[str, Any],
    *,
    actual_participation_rate: float,
    adv_utilization: Optional[float],
) -> float:
    capacity_bps = 0.0
    if actual_participation_rate > 0:
        capacity_bps += min(45.0, actual_participation_rate * 2500.0)
    if adv_utilization is not None and adv_utilization > 1.0:
        capacity_bps += min(60.0, (adv_utilization - 1.0) * 25.0)
    capacity_bucket = str(args.get("capacity_bucket") or "").strip().lower()
    if capacity_bucket:
        capacity_bps += _CAPACITY_BUCKET_BPS.get(capacity_bucket, 0.0)
    return max(0.0, capacity_bps)

# --- _engine_support_parts/execution.py ---
def _resolve_order_fill(
    requested_shares: int,
    *,
    index: int,
    volumes: np.ndarray,
    tradability_mask: Optional[np.ndarray],
    lot_size: int,
    args: Dict[str, Any],
) -> Dict[str, Any]:
    requested = max(0, int(requested_shares or 0))
    normalized_lot = max(1, int(lot_size or 1))
    if requested <= 0:
        return {
            "requested_shares": 0,
            "filled_shares": 0,
            "rejected_shares": 0,
            "partial_fill": False,
            "blocked_reason": "no_requested_shares",
            "actual_participation_rate": 0.0,
            "adv_utilization": None,
            "rolling_adv_20": _rolling_average_volume(volumes, index),
            "capacity_limited": False,
            "execution_penalty_bps": 0.0,
        }

    day_volume = float(volumes[index]) if 0 <= index < len(volumes) else 0.0
    rolling_adv_20 = _rolling_average_volume(volumes, index)
    tradable = True if tradability_mask is None else bool(tradability_mask[index])
    if not tradable:
        return {
            "requested_shares": requested,
            "filled_shares": 0,
            "rejected_shares": requested,
            "partial_fill": False,
            "blocked_reason": "not_tradable",
            "day_volume": day_volume,
            "rolling_adv_20": rolling_adv_20,
            "actual_participation_rate": 0.0,
            "adv_utilization": None,
            "capacity_limited": False,
            "execution_penalty_bps": 0.0,
        }
    if day_volume <= 0:
        return {
            "requested_shares": requested,
            "filled_shares": 0,
            "rejected_shares": requested,
            "partial_fill": False,
            "blocked_reason": "zero_volume",
            "day_volume": day_volume,
            "rolling_adv_20": rolling_adv_20,
            "actual_participation_rate": 0.0,
            "adv_utilization": None,
            "capacity_limited": False,
            "execution_penalty_bps": 0.0,
        }

    max_fillable = requested
    capacity_caps: list[int] = []
    capacity_participation_rate = _safe_float(args.get("capacity_participation_rate"), 0.0)
    adv_ratio_limit = _safe_float(args.get("adv_ratio_limit"), 0.0)
    if capacity_participation_rate > 0:
        capacity_caps.append(max(0, int(np.floor(day_volume * capacity_participation_rate))))
    if adv_ratio_limit > 0 and rolling_adv_20 > 0:
        capacity_caps.append(max(0, int(np.floor(rolling_adv_20 * adv_ratio_limit))))
    if capacity_caps:
        max_fillable = min(max_fillable, min(capacity_caps))
    filled = _round_down_lot(max_fillable, normalized_lot)
    if filled <= 0:
        blocked_reason = "capacity_below_lot" if capacity_caps else "below_min_trade_lot"
        return {
            "requested_shares": requested,
            "filled_shares": 0,
            "rejected_shares": requested,
            "partial_fill": False,
            "blocked_reason": blocked_reason,
            "day_volume": day_volume,
            "rolling_adv_20": rolling_adv_20,
            "actual_participation_rate": 0.0,
            "adv_utilization": None,
            "capacity_limited": bool(capacity_caps),
            "execution_penalty_bps": 0.0,
        }

    rejected = max(0, requested - filled)
    actual_participation_rate = (filled / day_volume) if day_volume > 0 else 0.0
    actual_adv_ratio = (filled / rolling_adv_20) if rolling_adv_20 > 0 else 0.0
    adv_utilization = (actual_adv_ratio / adv_ratio_limit) if adv_ratio_limit > 0 else None

    arrival_policy = str(args.get("arrival_price_policy") or "next_open_proxy").strip().lower()
    arrival_bps = _ARRIVAL_PRICE_POLICY_BPS.get(arrival_policy)
    if arrival_bps is None:
        arrival_bps = 2.0 if "open" in arrival_policy else 1.0
    market_impact_bps = _safe_float(args.get("market_impact_bps"), 0.0)
    capacity_penalty_bps = _estimate_capacity_penalty_bps(
        args,
        actual_participation_rate=actual_participation_rate,
        adv_utilization=adv_utilization,
    )

    return {
        "requested_shares": requested,
        "filled_shares": filled,
        "rejected_shares": rejected,
        "partial_fill": rejected > 0,
        "blocked_reason": "capacity_limited" if rejected > 0 else None,
        "day_volume": day_volume,
        "rolling_adv_20": rolling_adv_20,
        "actual_participation_rate": round(actual_participation_rate, 6),
        "actual_adv_ratio": round(actual_adv_ratio, 6) if actual_adv_ratio > 0 else 0.0,
        "adv_utilization": round(adv_utilization, 4) if adv_utilization is not None else None,
        "capacity_limited": bool(capacity_caps and rejected > 0),
        "execution_penalty_bps": round(arrival_bps + max(0.0, market_impact_bps) + capacity_penalty_bps, 4),
        "capacity_penalty_bps": round(capacity_penalty_bps, 4),
    }

def _build_strategy_masks(
    strategy: str,
    closes: np.ndarray,
    params: Dict[str, Any],
    volumes: Optional[np.ndarray] = None,
    klines: Optional[List[Dict[str, Any]]] = None,
) -> Optional[Tuple[np.ndarray, np.ndarray]]:
    """为策略构建 entry/exit 掩码。"""
    n = len(closes)
    strategy = str(strategy or "").strip().lower()

    from .strategy_registry import StrategyRegistry

    runtime_instance, execution_semantic_mode = StrategyRegistry.create_runtime_strategy(strategy, params)
    if runtime_instance is not None:
        if klines and hasattr(runtime_instance, "generate_entry_exit_masks_from_klines"):
            try:
                return runtime_instance.generate_entry_exit_masks_from_klines(klines)
            except Exception as _swallowed:
                logger.warning("packages/aiask-quant-core/src/aiask_quant_core/backtest/_engine_support.py:669: swallowed %s and continued: %s", type(_swallowed).__name__, _swallowed)
                pass
        if execution_semantic_mode == "compiled_dsl" or hasattr(runtime_instance, "generate_entry_exit_masks"):
            try:
                return runtime_instance.generate_entry_exit_masks(closes, volumes)
            except Exception as _swallowed:
                logger.warning("packages/aiask-quant-core/src/aiask_quant_core/backtest/_engine_support.py:674: swallowed %s and continued: %s", type(_swallowed).__name__, _swallowed)
                pass

    if strategy == "ma_cross":
        short_period = max(2, int(params.get("short_period", 5)))
        long_period = max(short_period + 1, int(params.get("long_period", 20)))
        if n < long_period + 2:
            return None

        short_ma = np.full(n, np.nan, dtype=np.float64)
        long_ma = np.full(n, np.nan, dtype=np.float64)
        for i in range(short_period - 1, n):
            short_ma[i] = float(np.mean(closes[i - short_period + 1 : i + 1]))
        for i in range(long_period - 1, n):
            long_ma[i] = float(np.mean(closes[i - long_period + 1 : i + 1]))

        entry = np.zeros(n, dtype=bool)
        exit_ = np.zeros(n, dtype=bool)
        for i in range(long_period, n):
            if (
                short_ma[i - 1] <= long_ma[i - 1]
                and short_ma[i] > long_ma[i]
            ):
                entry[i] = True
            elif (
                short_ma[i - 1] >= long_ma[i - 1]
                and short_ma[i] < long_ma[i]
            ):
                exit_[i] = True
        return entry, exit_

    if strategy == "momentum":
        lookback = max(2, int(params.get("lookback", params.get("period", 20))))
        threshold = float(params.get("threshold", 0.02) or 0.02)
        if n < lookback + 2:
            return None

        momentum = np.zeros(n, dtype=np.float64)
        for i in range(lookback, n):
            base = closes[i - lookback]
            if base > 0:
                momentum[i] = (closes[i] - base) / base

        entry = momentum > threshold
        exit_ = momentum < -threshold
        entry[:lookback] = False
        exit_[:lookback] = False
        return entry, exit_

    if strategy == "rsi":
        rsi_period = max(2, int(params.get("rsi_period", 14)))
        oversold = float(params.get("oversold", 30) or 30)
        overbought = float(params.get("overbought", 70) or 70)
        if n < rsi_period + 2:
            return None

        rsi = np.full(n, np.nan, dtype=np.float64)
        for i in range(rsi_period, n):
            gains = 0.0
            losses = 0.0
            for j in range(i - rsi_period + 1, i + 1):
                change = closes[j] - closes[j - 1]
                if change > 0:
                    gains += change
                else:
                    losses -= change
            avg_gain = gains / rsi_period
            avg_loss = losses / rsi_period
            if avg_loss == 0:
                rsi[i] = 100.0
            else:
                rs = avg_gain / avg_loss
                rsi[i] = 100.0 - (100.0 / (1.0 + rs))

        entry = rsi < oversold
        exit_ = rsi > overbought
        entry[:rsi_period] = False
        exit_[:rsi_period] = False
        return entry, exit_

    # Fallback: try StrategyRegistry for user-submitted strategies
    klass = StrategyRegistry.get(strategy)
    if klass is not None:
        instance = klass()
        instance.set_parameters(params)
        return instance.generate_entry_exit_masks(closes, volumes)

    return None

def _simulate_trades_from_masks(
    closes: np.ndarray,
    volumes: np.ndarray,
    entry_mask: np.ndarray,
    exit_mask: np.ndarray,
    initial_capital: float,
    commission_rate: float,
    slippage_calc: Optional[SlippageCalculator] = None,
    tradability_mask: Optional[np.ndarray] = None,
    return_trades: bool = False,
    klines: Optional[List[Dict[str, Any]]] = None,
    params: Optional[Dict[str, Any]] = None,
    signal_events: Optional[List[StrategySignalEvent]] = None,
) -> Dict[str, Any]:
    """按 entry/exit 掩码执行交易仿真，支持逐笔滑点与可交易过滤。"""
    n = len(closes)
    args = dict(params or {})
    a_share_rules = _is_a_share_ruleset(args)
    lot_size = max(1, int(args.get("min_trade_lot", 100 if a_share_rules else 1) or (100 if a_share_rules else 1)))
    sell_tax_rate = _safe_float(args.get("sell_tax_rate"), 0.001 if a_share_rules else 0.0)
    t_plus_one = bool(args.get("t_plus_one", a_share_rules))
    position_pct = _resolve_position_pct(args)
    payload_context = {
        "tradable_days": int(np.sum(tradability_mask)) if tradability_mask is not None else int(np.sum(volumes > 0)),
        "total_days": int(n),
        "tradability_filter": bool(tradability_mask is not None),
    }
    explicit_slippage = _safe_float(args.get("slippage", 0.0), 0.0)
    model_slippage = _compute_slippage_rate(closes, volumes, args, 0.0)
    market_impact_bps = _safe_float(args.get("market_impact_bps", 0.0), 0.0)
    _implementation_shortfall_proxy, _shortfall_source, shortfall_components, _tradability_summary, _capacity_summary = _estimate_implementation_shortfall(
        payload_context,
        args,
        closes=closes,
        volumes=volumes,
        explicit_slippage_rate=explicit_slippage,
        model_slippage_rate=model_slippage,
        market_impact_bps=market_impact_bps,
    )
    base_slippage_bps = 0.0 if slippage_calc is not None else float(shortfall_components.get("effective_slippage_bps") or 0.0)
    per_side_extra_cost_rate = max(0.0, base_slippage_bps / 10000.0)
    cash = float(initial_capital)
    shares = 0
    buy_price = 0.0
    buy_index = -1
    pending_exit = False
    trades = 0
    wins = 0
    equity = np.full(n, float(initial_capital), dtype=np.float64)
    trades_detail: List[Dict[str, Any]] = []
    fills_detail: List[Dict[str, Any]] = []
    total_traded_notional = 0.0
    holding_periods: List[int] = []
    order_attempt_count = 0
    failed_order_count = 0
    partial_fill_count = 0
    requested_shares_total = 0
    filled_shares_total = 0
    rejected_shares_total = 0
    blocked_reason_counts: Dict[str, int] = {}
    actual_participation_rates: List[float] = []
    adv_utilizations: List[float] = []
    execution_penalty_bps_notional = 0.0
    execution_penalty_bps_weight = 0.0
    reduce_units_by_index: Dict[int, float] = {}
    exit_reason_by_index: Dict[int, str] = {}
    round_trip_positions: List[Dict[str, Any]] = []
    current_round_trip: Optional[Dict[str, Any]] = None
    round_trip_sequence = 0
    closed_round_trip_count = 0
    winning_round_trip_count = 0
    if signal_events:
        for event in signal_events:
            idx = int(event.get("index") or 0)
            signal = int(event.get("signal") or 0)
            action = str(event.get("action") or "").strip().lower()
            if signal < 0 and action == "reduce":
                units = float(event.get("units") or 0.0)
                if 0 <= idx < n and units > 0:
                    reduce_units_by_index[idx] = min(1.0, max(reduce_units_by_index.get(idx, 0.0), units))
                    exit_reason_by_index[idx] = str(event.get("reason") or action)
            elif signal < 0 and action == "exit" and 0 <= idx < n:
                exit_reason_by_index[idx] = str(event.get("reason") or action)

    def _trade_time(index: int) -> str:
        if klines is None or index < 0 or index >= len(klines):
            return ""
        row = klines[index]
        return str(row.get("date", row.get("trade_date", row.get("time", ""))))

    def _start_round_trip(index: int, quantity: int, entry_price: float) -> None:
        nonlocal current_round_trip, round_trip_sequence
        round_trip_sequence += 1
        current_round_trip = {
            "round_trip_id": f"round_trip_{round_trip_sequence}",
            "status": "open",
            "entry_index": int(index),
            "entry_time": _trade_time(index),
            "entry_qty": int(quantity),
            "entry_notional": float(quantity * entry_price),
            "remaining_qty": int(quantity),
            "exited_qty": 0,
            "exit_notional": 0.0,
            "avg_entry_price": float(entry_price),
            "avg_exit_price": None,
            "realized_pnl": 0.0,
            "realized_return": None,
            "hold_days": None,
            "exit_index": None,
            "exit_time": None,
            "exit_reason": None,
            "partial_exit_count": 0,
            "exit_actions": [],
        }

    def _record_round_trip_exit(
        index: int,
        quantity: int,
        exit_price: float,
        realized_pnl: float,
        *,
        action: str,
        reason: Optional[str],
    ) -> None:
        nonlocal current_round_trip, closed_round_trip_count, winning_round_trip_count
        if current_round_trip is None or quantity <= 0:
            return
        current_round_trip["exited_qty"] = int(current_round_trip.get("exited_qty") or 0) + int(quantity)
        current_round_trip["remaining_qty"] = max(
            0,
            int(current_round_trip.get("remaining_qty") or 0) - int(quantity),
        )
        current_round_trip["exit_notional"] = float(current_round_trip.get("exit_notional") or 0.0) + float(quantity * exit_price)
        current_round_trip["realized_pnl"] = float(current_round_trip.get("realized_pnl") or 0.0) + float(realized_pnl)
        if int(current_round_trip.get("remaining_qty") or 0) > 0:
            current_round_trip["partial_exit_count"] = int(current_round_trip.get("partial_exit_count") or 0) + 1
        current_round_trip["exit_actions"] = list(current_round_trip.get("exit_actions") or []) + [
            {
                "index": int(index),
                "time": _trade_time(index),
                "action": action,
                "reason": reason,
                "qty": int(quantity),
                "price": float(exit_price),
                "realized_pnl": float(realized_pnl),
            }
        ]
        current_round_trip["exit_reason"] = str(reason or action or current_round_trip.get("exit_reason") or "").strip() or None
        if int(current_round_trip.get("remaining_qty") or 0) <= 0:
            entry_notional = float(current_round_trip.get("entry_notional") or 0.0)
            exited_qty = int(current_round_trip.get("exited_qty") or 0)
            current_round_trip.update(
                {
                    "status": "closed",
                    "avg_exit_price": float(current_round_trip.get("exit_notional") or 0.0) / max(exited_qty, 1),
                    "realized_return": (
                        float(current_round_trip.get("realized_pnl") or 0.0) / entry_notional
                        if entry_notional > 0
                        else 0.0
                    ),
                    "hold_days": max(1, int(index - int(current_round_trip.get("entry_index") or index))),
                    "exit_index": int(index),
                    "exit_time": _trade_time(index),
                }
            )
            round_trip_positions.append(current_round_trip)
            closed_round_trip_count += 1
            if float(current_round_trip.get("realized_pnl") or 0.0) > 0:
                winning_round_trip_count += 1
            current_round_trip = None

    def _finalize_open_round_trip(index: int, *, status: str, reason: Optional[str]) -> None:
        nonlocal current_round_trip
        if current_round_trip is None:
            return
        entry_notional = float(current_round_trip.get("entry_notional") or 0.0)
        exited_qty = int(current_round_trip.get("exited_qty") or 0)
        current_round_trip.update(
            {
                "status": status,
                "avg_exit_price": (
                    float(current_round_trip.get("exit_notional") or 0.0) / exited_qty
                    if exited_qty > 0
                    else None
                ),
                "realized_return": (
                    float(current_round_trip.get("realized_pnl") or 0.0) / entry_notional
                    if entry_notional > 0
                    else 0.0
                ),
                "hold_days": max(1, int(index - int(current_round_trip.get("entry_index") or index))),
                "exit_index": int(index) if exited_qty > 0 else None,
                "exit_time": _trade_time(index) if exited_qty > 0 else None,
                "exit_reason": str(reason or current_round_trip.get("exit_reason") or status).strip() or status,
            }
        )
        round_trip_positions.append(current_round_trip)
        current_round_trip = None

    def _round_trip_metrics() -> tuple[float | None, float | None]:
        closed_positions = [
            item for item in round_trip_positions if str(item.get("status") or "").lower() == "closed"
        ]
        # None, not 0.0: with no closed round trip there is no win rate to
        # report, and 0.0 is indistinguishable from "measured, never won".
        win_rate: float | None = (
            float(winning_round_trip_count / max(closed_round_trip_count, 1))
            if closed_round_trip_count > 0
            else None
        )
        avg_hold_days = (
            float(np.mean([float(item.get("hold_days") or 0.0) for item in closed_positions]))
            if closed_positions
            else None
        )
        return win_rate, avg_hold_days

    for i in range(n - 1):
        tradable = True if tradability_mask is None else bool(tradability_mask[i])
        # Next-bar execution: signal on bar i, execute at bar i+1 close (proxy for next open)
        next_tradable = True if tradability_mask is None else bool(tradability_mask[i + 1])
        if entry_mask[i] and shares == 0 and cash > 0 and tradable and next_tradable:
            exec_price = float(closes[i + 1])
            approx_price = exec_price * (1 + commission_rate + per_side_extra_cost_rate)
            max_entry_notional = max(0.0, (cash + shares * closes[i]) * position_pct)
            affordable_cash = min(cash, max_entry_notional) if max_entry_notional > 0 else cash
            est_shares = int(affordable_cash / approx_price) if approx_price > 0 else 0
            est_shares = _round_down_lot(est_shares, lot_size)
            fill_info = _resolve_order_fill(
                est_shares,
                index=i + 1,
                volumes=volumes,
                tradability_mask=tradability_mask,
                lot_size=lot_size,
                args=args,
            )
            order_attempt_count += 1
            requested_shares_total += int(fill_info.get("requested_shares") or 0)
            filled_shares_total += int(fill_info.get("filled_shares") or 0)
            rejected_shares_total += int(fill_info.get("rejected_shares") or 0)
            if fill_info.get("actual_participation_rate"):
                actual_participation_rates.append(float(fill_info["actual_participation_rate"]))
            if fill_info.get("adv_utilization") is not None:
                adv_utilizations.append(float(fill_info["adv_utilization"]))
            if int(fill_info.get("filled_shares") or 0) <= 0:
                failed_order_count += 1
                blocked_reason = str(fill_info.get("blocked_reason") or "entry_blocked")
                blocked_reason_counts[blocked_reason] = blocked_reason_counts.get(blocked_reason, 0) + 1
                if return_trades:
                    trade_time = ""
                    if klines is not None and i + 1 < len(klines):
                        row = klines[i + 1]
                        trade_time = str(row.get("date", row.get("trade_date", row.get("time", ""))))
                    fills_detail.append(
                        {
                            "index": int(i + 1),
                            "time": trade_time,
                            "signal": 1,
                            "requested_shares": int(fill_info.get("requested_shares") or 0),
                            "filled_shares": 0,
                            "rejected_shares": int(fill_info.get("rejected_shares") or 0),
                            "fill_ratio": 0.0,
                            "blocked_reason": blocked_reason,
                        }
                    )
                equity[i] = cash
                continue
            if fill_info.get("partial_fill"):
                partial_fill_count += 1
                blocked_reason = str(fill_info.get("blocked_reason") or "capacity_limited")
                blocked_reason_counts[blocked_reason] = blocked_reason_counts.get(blocked_reason, 0) + 1

            est_shares = int(fill_info.get("filled_shares") or 0)
            dynamic_extra_cost_rate = max(
                per_side_extra_cost_rate,
                float(fill_info.get("execution_penalty_bps") or 0.0) / 10000.0 / 2.0,
            )
            if slippage_calc is not None:
                slip = slippage_calc.calculate(
                    price=exec_price,
                    volume=float(volumes[i + 1]) if i + 1 < len(volumes) else 0.0,
                    order_size=float(est_shares),
                    is_buy=True,
                )
                exec_price = float(slip.get("execution_price", exec_price))

            buy_price = exec_price * (1 + commission_rate + dynamic_extra_cost_rate)
            max_entry_notional = max(0.0, (cash + shares * closes[i]) * position_pct)
            affordable_cash = min(cash, max_entry_notional) if max_entry_notional > 0 else cash
            max_shares = int(affordable_cash / buy_price) if buy_price > 0 else 0
            max_shares = _round_down_lot(min(max_shares, est_shares), lot_size)
            if max_shares > 0:
                shares = max_shares
                cash -= shares * buy_price
                trades += 1
                buy_index = int(i + 1)
                pending_exit = False
                total_traded_notional += float(shares * buy_price)
                _start_round_trip(int(i + 1), shares, buy_price)
                execution_penalty_bps_notional += float(fill_info.get("execution_penalty_bps") or 0.0) * float(shares * buy_price)
                execution_penalty_bps_weight += float(shares * buy_price)
                if return_trades:
                    trade_time = _trade_time(int(i + 1))
                    trades_detail.append(
                        {
                            "index": int(i + 1),
                            "time": trade_time,
                            "price": float(buy_price),
                            "signal": 1,
                            "shares": int(shares),
                            "profit": 0.0,
                        }
                    )
                    fills_detail.append(
                        {
                            "index": int(i + 1),
                            "time": trade_time,
                            "price": float(buy_price),
                            "signal": 1,
                            "requested_shares": int(fill_info.get("requested_shares") or shares),
                            "filled_shares": int(shares),
                            "rejected_shares": int(fill_info.get("rejected_shares") or 0),
                            "fill_ratio": round(int(shares) / max(int(fill_info.get("requested_shares") or shares), 1), 6),
                            "blocked_reason": fill_info.get("blocked_reason"),
                            "partial_fill": bool(fill_info.get("partial_fill")),
                        }
                    )

        elif (pending_exit or exit_mask[i] or i in reduce_units_by_index) and shares > 0 and tradable and next_tradable:
            if t_plus_one and buy_index >= 0 and (i + 1) <= buy_index:
                equity[i] = cash + shares * closes[i]
                continue
            pending_exit = True
            exec_price = float(closes[i + 1])
            requested_exit_shares = shares
            if i in reduce_units_by_index:
                requested_exit_shares = _round_down_lot(max(1, int(round(float(shares) * float(reduce_units_by_index[i])))), lot_size)
                if requested_exit_shares <= 0:
                    requested_exit_shares = shares
            fill_info = _resolve_order_fill(
                requested_exit_shares,
                index=i + 1,
                volumes=volumes,
                tradability_mask=tradability_mask,
                lot_size=lot_size,
                args=args,
            )
            order_attempt_count += 1
            requested_shares_total += int(fill_info.get("requested_shares") or 0)
            filled_shares_total += int(fill_info.get("filled_shares") or 0)
            rejected_shares_total += int(fill_info.get("rejected_shares") or 0)
            if fill_info.get("actual_participation_rate"):
                actual_participation_rates.append(float(fill_info["actual_participation_rate"]))
            if fill_info.get("adv_utilization") is not None:
                adv_utilizations.append(float(fill_info["adv_utilization"]))
            if int(fill_info.get("filled_shares") or 0) <= 0:
                failed_order_count += 1
                blocked_reason = str(fill_info.get("blocked_reason") or "exit_blocked")
                blocked_reason_counts[blocked_reason] = blocked_reason_counts.get(blocked_reason, 0) + 1
                if return_trades:
                    trade_time = ""
                    if klines is not None and i + 1 < len(klines):
                        row = klines[i + 1]
                        trade_time = str(row.get("date", row.get("trade_date", row.get("time", ""))))
                    fills_detail.append(
                        {
                            "index": int(i + 1),
                            "time": trade_time,
                            "signal": -1,
                            "action": "reduce" if i in reduce_units_by_index else "exit",
                            "reason": exit_reason_by_index.get(i),
                            "requested_shares": int(fill_info.get("requested_shares") or 0),
                            "filled_shares": 0,
                            "rejected_shares": int(fill_info.get("rejected_shares") or 0),
                            "fill_ratio": 0.0,
                            "blocked_reason": blocked_reason,
                        }
                    )
                equity[i] = cash + shares * closes[i]
                continue
            if fill_info.get("partial_fill"):
                partial_fill_count += 1
                blocked_reason = str(fill_info.get("blocked_reason") or "capacity_limited")
                blocked_reason_counts[blocked_reason] = blocked_reason_counts.get(blocked_reason, 0) + 1
            sell_shares = int(fill_info.get("filled_shares") or 0)
            dynamic_extra_cost_rate = max(
                per_side_extra_cost_rate,
                float(fill_info.get("execution_penalty_bps") or 0.0) / 10000.0 / 2.0,
            )
            if slippage_calc is not None:
                slip = slippage_calc.calculate(
                    price=exec_price,
                    volume=float(volumes[i + 1]) if i + 1 < len(volumes) else 0.0,
                    order_size=float(sell_shares),
                    is_buy=False,
                )
                exec_price = float(slip.get("execution_price", exec_price))

            sell_price = exec_price * (1 - commission_rate - dynamic_extra_cost_rate - sell_tax_rate)
            sell_price = max(0.0, sell_price)
            revenue = sell_shares * sell_price
            avg_entry_cost = (
                float(current_round_trip.get("entry_notional") or 0.0) / max(int(current_round_trip.get("entry_qty") or 0), 1)
                if current_round_trip is not None
                else float(buy_price)
            )
            profit = revenue - (sell_shares * avg_entry_cost)
            cash += revenue
            trades += 1
            total_traded_notional += float(revenue)
            execution_penalty_bps_notional += float(fill_info.get("execution_penalty_bps") or 0.0) * float(revenue)
            execution_penalty_bps_weight += float(revenue)
            if buy_index >= 0 and shares == sell_shares:
                holding_periods.append(max(1, int(i + 1 - buy_index)))
            _record_round_trip_exit(
                int(i + 1),
                sell_shares,
                sell_price,
                profit,
                action="reduce" if i in reduce_units_by_index else "exit",
                reason=exit_reason_by_index.get(i),
            )
            if return_trades:
                trade_time = _trade_time(int(i + 1))
                trades_detail.append(
                    {
                        "index": int(i + 1),
                            "time": trade_time,
                            "price": float(sell_price),
                            "signal": -1,
                            "action": "reduce" if i in reduce_units_by_index else "exit",
                            "reason": exit_reason_by_index.get(i),
                            "shares": int(sell_shares),
                            "profit": float(profit),
                            "holding_days": max(1, int(i + 1 - buy_index)) if buy_index >= 0 else 0,
                        }
                    )
                fills_detail.append(
                    {
                        "index": int(i + 1),
                        "time": trade_time,
                        "price": float(sell_price),
                        "signal": -1,
                        "action": "reduce" if i in reduce_units_by_index else "exit",
                        "reason": exit_reason_by_index.get(i),
                        "requested_shares": int(fill_info.get("requested_shares") or sell_shares),
                        "filled_shares": int(sell_shares),
                        "rejected_shares": int(fill_info.get("rejected_shares") or 0),
                        "fill_ratio": round(int(sell_shares) / max(int(fill_info.get("requested_shares") or sell_shares), 1), 6),
                        "blocked_reason": fill_info.get("blocked_reason"),
                        "partial_fill": bool(fill_info.get("partial_fill")),
                    }
                )
            shares = max(0, shares - sell_shares)
            if shares == 0:
                buy_index = -1
                pending_exit = False
            elif i in reduce_units_by_index:
                pending_exit = False

        equity[i] = cash + shares * closes[i]
    equity[n - 1] = cash + shares * float(closes[n - 1])

    if shares > 0:
        i = n - 1
        exec_price = float(closes[i])
        fill_info = _resolve_order_fill(
            shares,
            index=i,
            volumes=volumes,
            tradability_mask=tradability_mask,
            lot_size=lot_size,
            args=args,
        )
        order_attempt_count += 1
        requested_shares_total += int(fill_info.get("requested_shares") or 0)
        filled_shares_total += int(fill_info.get("filled_shares") or 0)
        rejected_shares_total += int(fill_info.get("rejected_shares") or 0)
        if fill_info.get("actual_participation_rate"):
            actual_participation_rates.append(float(fill_info["actual_participation_rate"]))
        if fill_info.get("adv_utilization") is not None:
            adv_utilizations.append(float(fill_info["adv_utilization"]))
        if int(fill_info.get("filled_shares") or 0) <= 0:
            failed_order_count += 1
            blocked_reason = str(fill_info.get("blocked_reason") or "final_exit_blocked")
            blocked_reason_counts[blocked_reason] = blocked_reason_counts.get(blocked_reason, 0) + 1
            if return_trades:
                trade_time = _trade_time(int(i))
                fills_detail.append(
                    {
                        "index": int(i),
                        "time": trade_time,
                        "signal": -1,
                        "requested_shares": int(fill_info.get("requested_shares") or 0),
                        "filled_shares": 0,
                        "rejected_shares": int(fill_info.get("rejected_shares") or 0),
                        "fill_ratio": 0.0,
                        "blocked_reason": blocked_reason,
                    }
                )
            equity[n - 1] = cash + shares * float(closes[n - 1])
            _finalize_open_round_trip(int(i), status="incomplete", reason=blocked_reason)
            shares = 0
            buy_index = -1
            win_rate, avg_holding_days = _round_trip_metrics()
            return {
                "final_capital": float(equity[n - 1]),
                "total_return": float((float(equity[n - 1]) - initial_capital) / initial_capital if initial_capital > 0 else 0.0),
                "max_drawdown": float(np.max((np.maximum.accumulate(equity) - equity) / np.maximum(np.maximum.accumulate(equity), 1e-9))),
                "sharpe_ratio": 0.0,
                "trades_count": int(trades),
                # None when unmeasurable: a backtest with no closed round trip
                # has no win rate, and 0.0 reads as "measured, never won".
                "win_rate": float(win_rate) if win_rate is not None else None,
                "avg_holding_days": float(avg_holding_days) if avg_holding_days is not None else None,
                "turnover_proxy": float(total_traded_notional / initial_capital) if initial_capital > 0 else 0.0,
                "equity": equity,
                "round_trip_positions": round_trip_positions,
                "closed_round_trip_count": int(closed_round_trip_count),
                "winning_round_trip_count": int(winning_round_trip_count),
                "trades": trades_detail if return_trades else None,
                "fills": fills_detail if return_trades else None,
                "execution_summary": {
                    "order_attempt_count": int(order_attempt_count),
                    "failed_order_count": int(failed_order_count),
                    "partial_fill_count": int(partial_fill_count),
                    "requested_shares": int(requested_shares_total),
                    "filled_shares": int(filled_shares_total),
                    "rejected_shares": int(rejected_shares_total),
                    "fill_rate": round(filled_shares_total / max(requested_shares_total, 1), 6) if requested_shares_total > 0 else 0.0,
                    "failed_fill_rate": round(failed_order_count / max(order_attempt_count, 1), 6) if order_attempt_count > 0 else 0.0,
                    "blocked_reason_counts": blocked_reason_counts,
                },
            }
        if fill_info.get("partial_fill"):
            partial_fill_count += 1
            blocked_reason = str(fill_info.get("blocked_reason") or "capacity_limited")
            blocked_reason_counts[blocked_reason] = blocked_reason_counts.get(blocked_reason, 0) + 1
        sell_shares = int(fill_info.get("filled_shares") or 0)
        dynamic_extra_cost_rate = max(
            per_side_extra_cost_rate,
            float(fill_info.get("execution_penalty_bps") or 0.0) / 10000.0 / 2.0,
        )
        if slippage_calc is not None:
            slip = slippage_calc.calculate(
                price=exec_price,
                volume=float(volumes[i]) if i < len(volumes) else 0.0,
                order_size=float(sell_shares),
                is_buy=False,
            )
            exec_price = float(slip.get("execution_price", exec_price))
        sell_price = exec_price * (1 - commission_rate - dynamic_extra_cost_rate - sell_tax_rate)
        sell_price = max(0.0, sell_price)
        revenue = sell_shares * sell_price
        avg_entry_cost = (
            float(current_round_trip.get("entry_notional") or 0.0) / max(int(current_round_trip.get("entry_qty") or 0), 1)
            if current_round_trip is not None
            else float(buy_price)
        )
        profit = revenue - (sell_shares * avg_entry_cost)
        cash += revenue
        trades += 1
        total_traded_notional += float(revenue)
        execution_penalty_bps_notional += float(fill_info.get("execution_penalty_bps") or 0.0) * float(revenue)
        execution_penalty_bps_weight += float(revenue)
        if buy_index >= 0:
            holding_periods.append(max(1, int(i - buy_index)))
        _record_round_trip_exit(
            int(i),
            sell_shares,
            sell_price,
            profit,
            action="exit",
            reason="final_exit",
        )
        if return_trades:
            trade_time = _trade_time(int(i))
            trades_detail.append(
                {
                        "index": int(i),
                        "time": trade_time,
                        "price": float(sell_price),
                        "signal": -1,
                        "shares": int(sell_shares),
                        "profit": float(profit),
                        "holding_days": max(1, int(i - buy_index)) if buy_index >= 0 else 0,
                    }
                )
            fills_detail.append(
                {
                    "index": int(i),
                    "time": trade_time,
                    "price": float(sell_price),
                    "signal": -1,
                    "requested_shares": int(fill_info.get("requested_shares") or sell_shares),
                    "filled_shares": int(sell_shares),
                    "rejected_shares": int(fill_info.get("rejected_shares") or 0),
                    "fill_ratio": round(int(sell_shares) / max(int(fill_info.get("requested_shares") or sell_shares), 1), 6),
                    "blocked_reason": fill_info.get("blocked_reason"),
                    "partial_fill": bool(fill_info.get("partial_fill")),
                }
            )
        shares = max(0, shares - sell_shares)
        buy_index = -1
        if shares > 0:
            _finalize_open_round_trip(int(i), status="incomplete", reason="final_exit_partial_fill")

    final_capital = float(cash + shares * float(closes[n - 1]))
    total_return = (final_capital - initial_capital) / initial_capital if initial_capital > 0 else 0.0

    max_dd = 0.0
    peak = float(equity[0]) if len(equity) else float(initial_capital)
    for val in equity:
        if val > peak:
            peak = float(val)
        if peak > 0:
            dd = (peak - float(val)) / peak
            if dd > max_dd:
                max_dd = dd

    sharpe = 0.0
    if len(equity) > 1:
        eq_prev = equity[:-1]
        eq_next = equity[1:]
        valid = eq_prev > 0
        if np.any(valid):
            rets = (eq_next[valid] - eq_prev[valid]) / eq_prev[valid]
            if len(rets) > 1:
                std = float(np.std(rets))
                if std > 0:
                    annual_ret = float(np.mean(rets)) * 252.0
                    annual_std = std * np.sqrt(252.0)
                    risk_free_rate = 0.02  # 年化无风险利率
                    sharpe = float((annual_ret - risk_free_rate) / annual_std)

    if current_round_trip is not None:
        _finalize_open_round_trip(int(n - 1), status="open_mark_to_market", reason="backtest_ended_with_open_position")
    win_rate, avg_holding_days = _round_trip_metrics()
    turnover_proxy = (total_traded_notional / initial_capital) if initial_capital > 0 else 0.0
    fill_rate = (filled_shares_total / requested_shares_total) if requested_shares_total > 0 else 0.0
    failed_fill_rate = (failed_order_count / order_attempt_count) if order_attempt_count > 0 else 0.0
    execution_summary = {
        "order_attempt_count": int(order_attempt_count),
        "filled_order_count": int(max(0, order_attempt_count - failed_order_count)),
        "failed_order_count": int(failed_order_count),
        "partial_fill_count": int(partial_fill_count),
        "requested_shares": int(requested_shares_total),
        "filled_shares": int(filled_shares_total),
        "rejected_shares": int(rejected_shares_total),
        "fill_rate": round(fill_rate, 6),
        "failed_fill_rate": round(failed_fill_rate, 6),
        "blocked_reason_counts": dict(blocked_reason_counts),
        "avg_participation_rate": round(float(np.mean(actual_participation_rates)), 6) if actual_participation_rates else 0.0,
        "max_participation_rate": round(float(np.max(actual_participation_rates)), 6) if actual_participation_rates else 0.0,
        "avg_adv_utilization": round(float(np.mean(adv_utilizations)), 6) if adv_utilizations else None,
        "max_adv_utilization": round(float(np.max(adv_utilizations)), 6) if adv_utilizations else None,
        "avg_execution_penalty_bps": round(
            execution_penalty_bps_notional / execution_penalty_bps_weight,
            4,
        ) if execution_penalty_bps_weight > 0 else 0.0,
    }
    return {
        "final_capital": final_capital,
        "total_return": float(total_return),
        "max_drawdown": float(max_dd),
        "sharpe_ratio": float(sharpe),
        "trades_count": int(trades),
        # None when unmeasurable, as above.
        "win_rate": float(win_rate) if win_rate is not None else None,
        "avg_holding_days": float(avg_holding_days) if avg_holding_days is not None else None,
        "turnover_proxy": float(turnover_proxy),
        "equity": equity,
        "round_trip_positions": round_trip_positions,
        "closed_round_trip_count": int(closed_round_trip_count),
        "winning_round_trip_count": int(winning_round_trip_count),
        "trades": trades_detail if return_trades else None,
        "fills": fills_detail if return_trades else None,
        "execution_summary": execution_summary,
    }

