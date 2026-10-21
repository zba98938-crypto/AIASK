"""回测引擎 - BacktestEngine 核心类"""


from __future__ import annotations

from typing import List, Dict, Any, Optional, Union, Tuple
import numpy as np

from aiask_quant_core.slippage import SlippageCalculator
from .utils import (
    _ensure_dict_list,
    _compute_slippage_rate,
    _resolve_slippage_model,
    normalize_trade_date,
    align_klines_by_trade_date,
)
from .strategies import (
    _backtest_ma_cross_jit,
    _backtest_ma_cross_with_trades_jit,
    _backtest_momentum_jit,
    _backtest_rsi_jit,
)


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


def _normalize_portfolio_weight_scheme(params: Optional[Dict[str, Any]], code_count: int) -> str:
    args = dict(params or {})
    scheme = str(args.get("target_weight_scheme") or "").strip().lower()
    if scheme in {"equal", "equal_weight_proxy"}:
        return "equal_weight"
    if scheme == "target_weight_map":
        return "target_weight_map"
    if scheme in {"single_name", "single_name_full_notional"}:
        return "single_name"
    return "equal_weight" if code_count > 1 else "single_name"


def _resolve_portfolio_weights(codes: List[str], params: Optional[Dict[str, Any]]) -> tuple[dict[str, float], str, str]:
    normalized_codes = [str(code or "").strip() for code in list(codes or []) if str(code or "").strip()]
    if not normalized_codes:
        return {}, "single_name", "single_name"

    args = dict(params or {})
    scheme = _normalize_portfolio_weight_scheme(args, len(normalized_codes))
    allocation_mode = "equal_weight"
    weights: dict[str, float] = {}

    if scheme == "single_name":
        weights = {normalized_codes[0]: 1.0}
        allocation_mode = "single_name"
    elif scheme == "target_weight_map":
        raw_map = dict(args.get("target_weight_map") or {})
        for code in normalized_codes:
            try:
                value = float(raw_map.get(code, 0.0) or 0.0)
            except (TypeError, ValueError):
                value = 0.0
            if value > 0:
                weights[code] = value
        total = float(sum(weights.values()))
        if total > 0:
            weights = {code: float(value) / total for code, value in weights.items()}
            allocation_mode = "target_weight_map"
        else:
            scheme = "equal_weight"

    if scheme == "equal_weight":
        equal_weight = 1.0 / float(len(normalized_codes))
        weights = {code: equal_weight for code in normalized_codes}
        allocation_mode = "equal_weight"

    max_position_pct = _safe_float(args.get("max_position_pct"), 0.0)
    if max_position_pct > 0:
        weights = {code: min(weight, max_position_pct) for code, weight in weights.items() if weight > 0}
        total = float(sum(weights.values()))
        if total > 1.0 and total > 0:
            weights = {code: float(value) / total for code, value in weights.items()}
    return weights, scheme, allocation_mode


def _build_buy_and_hold_masks(length: int) -> tuple[np.ndarray, np.ndarray]:
    entry = np.zeros(length, dtype=bool)
    exit_ = np.zeros(length, dtype=bool)
    if length >= 2:
        entry[0] = True
        exit_[length - 2] = True
    return entry, exit_


def _build_portfolio_strategy_masks(
    strategy: str,
    klines: List[Dict[str, Any]],
    closes: np.ndarray,
    volumes: np.ndarray,
    params: Dict[str, Any],
) -> Optional[Tuple[np.ndarray, np.ndarray, Optional[List[Dict[str, Any]]]]]:
    normalized_strategy = str(strategy or "").strip().lower()
    if normalized_strategy == "buy_and_hold":
        entry, exit_ = _build_buy_and_hold_masks(len(closes))
        return entry, exit_, None

    from .strategy_registry import StrategyRegistry as _Reg

    inst, _execution_semantic_mode = _Reg.create_runtime_strategy(normalized_strategy, params)
    if inst is not None:
        signal_events = None
        if hasattr(inst, 'generate_signal_events_from_klines'):
            signal_events = inst.generate_signal_events_from_klines(klines)
        elif hasattr(inst, 'generate_signal_events'):
            signal_events = inst.generate_signal_events(closes, volumes)
        if hasattr(inst, 'generate_entry_exit_masks_from_klines'):
            entry_mask, exit_mask = inst.generate_entry_exit_masks_from_klines(klines)
        else:
            entry_mask, exit_mask = inst.generate_entry_exit_masks(closes, volumes)
        return entry_mask, exit_mask, signal_events

    masks = _build_strategy_masks(normalized_strategy, closes, params, volumes=volumes, klines=klines)
    if masks is not None:
        return masks[0], masks[1], None

    return None


def _summarize_portfolio_equity(equity: np.ndarray, initial_capital: float) -> tuple[float, float, float]:
    final_capital = float(equity[-1]) if equity.size else float(initial_capital)
    total_return = (final_capital - initial_capital) / initial_capital if initial_capital > 0 else 0.0

    max_dd = 0.0
    if equity.size:
        peak = float(equity[0])
        for value in equity:
            if value > peak:
                peak = float(value)
            if peak > 0:
                drawdown = (peak - float(value)) / peak
                if drawdown > max_dd:
                    max_dd = drawdown

    sharpe = 0.0
    if equity.size > 1:
        prev = equity[:-1]
        curr = equity[1:]
        valid = prev > 0
        if np.any(valid):
            returns = (curr[valid] - prev[valid]) / prev[valid]
            returns = returns[np.isfinite(returns)]
            if returns.size > 1:
                std = float(np.std(returns))
                if std > 0:
                    annual_ret = float(np.mean(returns)) * 252.0
                    annual_std = std * np.sqrt(252.0)
                    sharpe = float((annual_ret - 0.02) / annual_std)
    return final_capital, float(total_return), float(max_dd), float(sharpe)

from ._engine_support import (
    _build_strategy_masks,
    _build_tradability_mask,
    _estimate_implementation_shortfall,
    _finalize_backtest_payload,
    _resolve_order_fill,
    _round_down_lot,
    _safe_float,
    _simulate_trades_from_masks,
)


class BacktestEngine:
# --- engine_parts/runtime.py ---
    """回测引擎"""
# --- engine_parts/execution.py ---

    @staticmethod
    def run_portfolio_backtest(
        market_data: Dict[str, List[Union[Dict[str, Any], Any]]],
        strategy: str = 'ma_cross',
        params: Optional[Dict[str, Any]] = None,
        return_trades: bool = False,
    ) -> Dict[str, Any]:
        if not market_data:
            return {'success': False, 'error': 'No portfolio market data'}

        normalized_data: dict[str, list[dict[str, Any]]] = {}
        for raw_code, raw_klines in dict(market_data or {}).items():
            code = str(raw_code or '').strip()
            if not code or not raw_klines:
                continue
            rows = _ensure_dict_list(raw_klines)
            if rows:
                normalized_data[code] = rows
        if len(normalized_data) <= 1:
            return {'success': False, 'error': 'Portfolio backtest requires at least two instruments'}

        # Align instruments by trading calendar (intersection of dates).
        common_dates, date_aligned = align_klines_by_trade_date(normalized_data)
        common_len = len(common_dates)
        if len(date_aligned) <= 1:
            return {'success': False, 'error': 'Insufficient dated kline data for portfolio backtest'}
        if common_len < 3:
            return {'success': False, 'error': 'Insufficient shared trading dates for portfolio backtest'}
        normalized_data = date_aligned

        args = dict(params or {})
        initial_capital = float(args.get("initial_capital", 100000) or 100000)
        commission = float(args.get("commission", 0.0003) or 0.0)
        codes = list(normalized_data.keys())
        weights, normalized_scheme, allocation_mode = _resolve_portfolio_weights(codes, args)
        active_codes = [code for code in codes if float(weights.get(code, 0.0) or 0.0) > 0]
        if len(active_codes) <= 1:
            return {'success': False, 'error': 'Portfolio backtest requires at least two weighted instruments'}

        slippage_model_raw = args.get("slippage_model")
        slippage_calc: Optional[SlippageCalculator] = None
        if slippage_model_raw:
            normalized_slippage_model = str(slippage_model_raw).strip().lower()
            if normalized_slippage_model in {"fixed", "volume_based", "market_impact"}:
                slippage_calc = SlippageCalculator(
                    model_type=_resolve_slippage_model(normalized_slippage_model)
                )

        a_share_rules = str(args.get("market_ruleset") or "cn_equity").strip().lower() in {"cn_equity", "a_share", "ashare", "cn_stock", "china_equity"}
        lot_size = max(1, int(args.get("min_trade_lot", 100 if a_share_rules else 1) or (100 if a_share_rules else 1)))
        sell_tax_rate = _safe_float(args.get("sell_tax_rate"), 0.001 if a_share_rules else 0.0)
        t_plus_one = bool(args.get("t_plus_one", a_share_rules))

        portfolio_payload_context = {
            "tradable_days": 0,
            "total_days": int(common_len),
            "tradability_filter": bool(args.get("tradability_filter", False)),
            "alignment": "trade_date_intersection",
        }
        closes_by_code: dict[str, np.ndarray] = {}
        volumes_by_code: dict[str, np.ndarray] = {}
        tradability_masks: dict[str, np.ndarray | None] = {}
        entry_masks: dict[str, np.ndarray] = {}
        exit_masks: dict[str, np.ndarray] = {}
        signal_events_by_code: dict[str, list[dict[str, Any]]] = {}
        aligned_klines: dict[str, list[dict[str, Any]]] = {}
        tradable_day_min: Optional[int] = None

        for code in active_codes:
            rows = list(normalized_data.get(code) or [])
            closes = np.array([float(item.get('close', 0.0) or 0.0) for item in rows], dtype=float)
            volumes = np.array([float(item.get('volume', 0.0) or 0.0) for item in rows], dtype=float)
            if closes.size != common_len or np.count_nonzero(closes > 0) < 3:
                return {'success': False, 'error': f'Insufficient aligned kline data for {code}'}

            tradability_mask: Optional[np.ndarray] = None
            if bool(args.get("tradability_filter", False)):
                tradability_mask = _build_tradability_mask(
                    closes=closes,
                    volumes=volumes,
                    code=code,
                    is_st=bool(args.get("is_st", False)),
                )
                tradable_days = int(np.sum(tradability_mask))
                tradable_day_min = tradable_days if tradable_day_min is None else min(tradable_day_min, tradable_days)
            masks = _build_portfolio_strategy_masks(strategy, rows, closes, volumes, args)
            if masks is None:
                return {'success': False, 'error': f'Insufficient data for strategy signals on {code}'}

            closes_by_code[code] = closes
            volumes_by_code[code] = volumes
            tradability_masks[code] = tradability_mask
            entry_masks[code], exit_masks[code], signal_events = masks
            signal_events_by_code[code] = list(signal_events or [])
            aligned_klines[code] = rows

        if tradable_day_min is not None:
            portfolio_payload_context["tradable_days"] = int(tradable_day_min)

        explicit_slippage = _safe_float(args.get("slippage", 0.0), 0.0)
        portfolio_closes = np.average(
            np.vstack([closes_by_code[code] for code in active_codes]),
            axis=0,
            weights=[float(weights.get(code, 0.0) or 0.0) for code in active_codes],
        )
        portfolio_volumes = np.sum(
            np.vstack([volumes_by_code[code] for code in active_codes]),
            axis=0,
        )
        model_slippage_rate = _compute_slippage_rate(portfolio_closes, portfolio_volumes, args, 0.0)
        market_impact_bps = _safe_float(args.get("market_impact_bps", 0.0), 0.0)
        _implementation_shortfall_proxy, _shortfall_source, shortfall_components, _tradability_summary, _capacity_summary = _estimate_implementation_shortfall(
            portfolio_payload_context,
            args,
            closes=portfolio_closes,
            volumes=portfolio_volumes,
            explicit_slippage_rate=explicit_slippage,
            model_slippage_rate=model_slippage_rate,
            market_impact_bps=market_impact_bps,
        )
        base_slippage_bps = 0.0 if slippage_calc is not None else float(shortfall_components.get("effective_slippage_bps") or 0.0)
        per_side_extra_cost_rate = max(0.0, base_slippage_bps / 10000.0)

        cash = float(initial_capital)
        holdings: dict[str, int] = {code: 0 for code in active_codes}
        position_cost_basis: dict[str, float] = {}
        entry_indices: dict[str, int] = {}
        pending_exit: dict[str, bool] = {code: False for code in active_codes}
        position_realized_profit: dict[str, float] = {code: 0.0 for code in active_codes}
        equity = np.full(common_len, float(initial_capital), dtype=np.float64)
        cash_curve = np.full(common_len, float(initial_capital), dtype=np.float64)
        gross_exposure_curve = np.zeros(common_len, dtype=np.float64)
        net_exposure_curve = np.zeros(common_len, dtype=np.float64)
        trades_detail: List[Dict[str, Any]] = []
        fills_detail: List[Dict[str, Any]] = []
        total_traded_notional = 0.0
        holding_periods: List[int] = []
        completed_round_trips = 0
        wins = 0
        executed_codes: set[str] = set()
        order_attempt_count = 0
        failed_order_count = 0
        partial_fill_count = 0
        requested_shares_total = 0
        filled_shares_total = 0
        rejected_shares_total = 0
        blocked_reason_counts: dict[str, int] = {}
        actual_participation_rates: list[float] = []
        adv_utilizations: list[float] = []
        execution_penalty_bps_notional = 0.0
        execution_penalty_bps_weight = 0.0

        def _mark_equity(index: int) -> None:
            gross_notional = sum(float(holdings[code]) * float(closes_by_code[code][index]) for code in active_codes)
            mark_to_market = cash + gross_notional
            equity[index] = mark_to_market
            cash_curve[index] = cash
            exposure_ratio = gross_notional / mark_to_market if mark_to_market > 0 else 0.0
            gross_exposure_curve[index] = exposure_ratio
            net_exposure_curve[index] = exposure_ratio

        def _record_fill_attempt(
            *,
            code: str,
            fill_info: dict[str, Any],
            index: int,
            signal: int,
            price: Optional[float] = None,
            profit: Optional[float] = None,
            holding_days: Optional[int] = None,
        ) -> None:
            nonlocal order_attempt_count, failed_order_count, partial_fill_count
            nonlocal requested_shares_total, filled_shares_total, rejected_shares_total
            requested = int(fill_info.get("requested_shares") or 0)
            filled = int(fill_info.get("filled_shares") or 0)
            rejected = int(fill_info.get("rejected_shares") or 0)
            order_attempt_count += 1
            requested_shares_total += requested
            filled_shares_total += filled
            rejected_shares_total += rejected
            participation = fill_info.get("actual_participation_rate")
            if participation:
                actual_participation_rates.append(float(participation))
            adv_util = fill_info.get("adv_utilization")
            if adv_util is not None:
                adv_utilizations.append(float(adv_util))
            if filled <= 0:
                failed_order_count += 1
                reason = str(fill_info.get("blocked_reason") or "fill_blocked")
                blocked_reason_counts[reason] = blocked_reason_counts.get(reason, 0) + 1
            elif fill_info.get("partial_fill"):
                partial_fill_count += 1
                reason = str(fill_info.get("blocked_reason") or "capacity_limited")
                blocked_reason_counts[reason] = blocked_reason_counts.get(reason, 0) + 1

            if not return_trades:
                return
            fills_detail.append(
                {
                    "code": code,
                    "index": int(index),
                    "time": str(aligned_klines[code][index].get('date', aligned_klines[code][index].get('trade_date', aligned_klines[code][index].get('time', '')))),
                    "signal": int(signal),
                    "price": None if price is None else float(price),
                    "requested_shares": requested,
                    "filled_shares": filled,
                    "rejected_shares": rejected,
                    "fill_ratio": round(filled / max(requested, 1), 6) if requested > 0 else 0.0,
                    "blocked_reason": fill_info.get("blocked_reason"),
                    "partial_fill": bool(fill_info.get("partial_fill")),
                    "profit": None if profit is None else float(profit),
                    "holding_days": None if holding_days is None else int(holding_days),
                }
            )

        for i in range(common_len - 1):
            next_index = int(i + 1)

            for code in active_codes:
                shares = int(holdings.get(code) or 0)
                reduce_event = next(
                    (
                        item for item in signal_events_by_code.get(code, [])
                        if int(item.get('index') or -1) == i and int(item.get('signal') or 0) < 0 and str(item.get('action') or '').strip().lower() == 'reduce'
                    ),
                    None,
                )
                if shares <= 0 or not (bool(exit_masks[code][i]) or bool(pending_exit.get(code)) or reduce_event is not None):
                    continue
                buy_index = int(entry_indices.get(code, -1))
                if t_plus_one and buy_index >= 0 and next_index <= buy_index:
                    continue

                pending_exit[code] = True
                fill_info = _resolve_order_fill(
                    shares,
                    index=next_index,
                    volumes=volumes_by_code[code],
                    tradability_mask=tradability_masks.get(code),
                    lot_size=lot_size,
                    args=args,
                )
                requested_exit_shares = shares
                if reduce_event is not None:
                    requested_exit_shares = _round_down_lot(
                        max(1, int(round(float(shares) * float(reduce_event.get('units') or 0.0)))),
                        lot_size,
                    )
                    if requested_exit_shares <= 0:
                        requested_exit_shares = shares
                    fill_info = _resolve_order_fill(
                        requested_exit_shares,
                        index=next_index,
                        volumes=volumes_by_code[code],
                        tradability_mask=tradability_masks.get(code),
                        lot_size=lot_size,
                        args=args,
                    )
                if int(fill_info.get("filled_shares") or 0) <= 0:
                    _record_fill_attempt(code=code, fill_info=fill_info, index=next_index, signal=-1)
                    continue

                filled_shares = int(fill_info.get("filled_shares") or 0)
                exec_price = float(closes_by_code[code][next_index])
                dynamic_extra_cost_rate = max(
                    per_side_extra_cost_rate,
                    float(fill_info.get("execution_penalty_bps") or 0.0) / 10000.0 / 2.0,
                )
                if slippage_calc is not None:
                    slip = slippage_calc.calculate(
                        price=exec_price,
                        volume=float(volumes_by_code[code][next_index]) if next_index < len(volumes_by_code[code]) else 0.0,
                        order_size=float(filled_shares),
                        is_buy=False,
                    )
                    exec_price = float(slip.get("execution_price", exec_price))
                sell_price = max(0.0, exec_price * (1 - commission - dynamic_extra_cost_rate - sell_tax_rate))
                revenue = float(filled_shares) * sell_price
                shares_before = int(holdings.get(code) or 0)
                average_cost = (
                    float(position_cost_basis.get(code, 0.0)) / float(shares_before)
                    if shares_before > 0
                    else 0.0
                )
                realized_cost = average_cost * float(filled_shares)
                profit = float(revenue - realized_cost)
                total_traded_notional += revenue
                execution_penalty_bps_notional += float(fill_info.get("execution_penalty_bps") or 0.0) * revenue
                execution_penalty_bps_weight += revenue
                cash += revenue
                holdings[code] = max(0, shares_before - filled_shares)
                position_cost_basis[code] = average_cost * float(holdings[code])
                position_realized_profit[code] = float(position_realized_profit.get(code, 0.0) + profit)
                holding_days = max(1, next_index - buy_index) if buy_index >= 0 else 0
                if return_trades:
                    trades_detail.append(
                        {
                            'code': code,
                            'index': next_index,
                            'time': str(aligned_klines[code][next_index].get('date', aligned_klines[code][next_index].get('trade_date', aligned_klines[code][next_index].get('time', '')))),
                            'price': float(sell_price),
                            'signal': -1,
                            'action': 'reduce' if reduce_event is not None else 'exit',
                            'reason': (reduce_event or {}).get('reason'),
                            'shares': int(filled_shares),
                            'profit': float(profit),
                            'holding_days': holding_days,
                        }
                    )
                _record_fill_attempt(
                    code=code,
                    fill_info=fill_info,
                    index=next_index,
                    signal=-1,
                    price=float(sell_price),
                    profit=float(profit),
                    holding_days=holding_days,
                )
                if reduce_event is not None and holdings[code] > 0:
                    pending_exit[code] = False
                if holdings[code] <= 0:
                    if float(position_realized_profit.get(code, 0.0)) > 0:
                        wins += 1
                    if buy_index >= 0:
                        holding_periods.append(max(1, next_index - buy_index))
                    completed_round_trips += 1
                    pending_exit[code] = False
                    position_realized_profit[code] = 0.0
                    position_cost_basis.pop(code, None)
                    entry_indices.pop(code, None)

            equity_before_entry = cash
            for code in active_codes:
                shares = int(holdings.get(code) or 0)
                if shares > 0:
                    equity_before_entry += float(shares) * float(closes_by_code[code][i])

            for code in sorted(active_codes, key=lambda item: (-float(weights.get(item, 0.0) or 0.0), item)):
                if int(holdings.get(code) or 0) > 0 or not bool(entry_masks[code][i]):
                    continue
                tradability_mask = tradability_masks.get(code)
                tradable_now = True if tradability_mask is None else bool(tradability_mask[i])
                tradable_next = True if tradability_mask is None else bool(tradability_mask[next_index])
                if not (tradable_now and tradable_next):
                    continue

                base_weight = float(weights.get(code, 0.0) or 0.0)
                if base_weight <= 0:
                    continue
                target_notional = max(0.0, equity_before_entry * base_weight)
                if target_notional <= 0 or cash <= 0:
                    continue

                exec_price = float(closes_by_code[code][next_index])
                approx_price = exec_price * (1 + commission + per_side_extra_cost_rate)
                if approx_price <= 0:
                    continue
                affordable_notional = min(cash, target_notional)
                estimated_shares = _round_down_lot(int(affordable_notional / approx_price), lot_size)
                if estimated_shares <= 0:
                    continue

                fill_info = _resolve_order_fill(
                    estimated_shares,
                    index=next_index,
                    volumes=volumes_by_code[code],
                    tradability_mask=tradability_masks.get(code),
                    lot_size=lot_size,
                    args=args,
                )
                if int(fill_info.get("filled_shares") or 0) <= 0:
                    _record_fill_attempt(code=code, fill_info=fill_info, index=next_index, signal=1)
                    continue

                dynamic_extra_cost_rate = max(
                    per_side_extra_cost_rate,
                    float(fill_info.get("execution_penalty_bps") or 0.0) / 10000.0 / 2.0,
                )
                if slippage_calc is not None:
                    slip = slippage_calc.calculate(
                        price=exec_price,
                        volume=float(volumes_by_code[code][next_index]) if next_index < len(volumes_by_code[code]) else 0.0,
                        order_size=float(fill_info.get("filled_shares") or estimated_shares),
                        is_buy=True,
                    )
                    exec_price = float(slip.get("execution_price", exec_price))

                buy_price = exec_price * (1 + commission + dynamic_extra_cost_rate)
                if buy_price <= 0:
                    continue
                shares = _round_down_lot(
                    int(min(cash / buy_price, float(fill_info.get("filled_shares") or estimated_shares))),
                    lot_size,
                )
                if shares <= 0:
                    _record_fill_attempt(
                        code=code,
                        fill_info={
                            **dict(fill_info),
                            "filled_shares": 0,
                            "rejected_shares": int(fill_info.get("requested_shares") or estimated_shares),
                            "blocked_reason": "cash_insufficient_after_slippage",
                            "partial_fill": False,
                        },
                        index=next_index,
                        signal=1,
                    )
                    continue

                trade_cost = float(shares) * buy_price
                cash -= trade_cost
                holdings[code] = shares
                position_cost_basis[code] = trade_cost
                entry_indices[code] = next_index
                pending_exit[code] = False
                position_realized_profit[code] = 0.0
                total_traded_notional += trade_cost
                execution_penalty_bps_notional += float(fill_info.get("execution_penalty_bps") or 0.0) * trade_cost
                execution_penalty_bps_weight += trade_cost
                executed_codes.add(code)
                if return_trades:
                    trades_detail.append(
                        {
                            'code': code,
                            'index': next_index,
                            'time': str(aligned_klines[code][next_index].get('date', aligned_klines[code][next_index].get('trade_date', aligned_klines[code][next_index].get('time', '')))),
                            'price': float(buy_price),
                            'signal': 1,
                            'shares': int(shares),
                            'profit': 0.0,
                        }
                    )
                _record_fill_attempt(
                    code=code,
                    fill_info={
                        **dict(fill_info),
                        "filled_shares": int(shares),
                        "rejected_shares": max(0, int(fill_info.get("requested_shares") or shares) - int(shares)),
                        "partial_fill": int(shares) < int(fill_info.get("requested_shares") or shares),
                        "blocked_reason": (
                            "cash_insufficient_after_slippage"
                            if int(shares) < int(fill_info.get("filled_shares") or shares)
                            else fill_info.get("blocked_reason")
                        ),
                    },
                    index=next_index,
                    signal=1,
                    price=float(buy_price),
                    profit=0.0,
                )

            _mark_equity(i)

        last_index = common_len - 1
        for code in active_codes:
            shares = int(holdings.get(code) or 0)
            if shares <= 0:
                continue
            fill_info = _resolve_order_fill(
                shares,
                index=last_index,
                volumes=volumes_by_code[code],
                tradability_mask=tradability_masks.get(code),
                lot_size=lot_size,
                args=args,
            )
            if int(fill_info.get("filled_shares") or 0) <= 0:
                _record_fill_attempt(code=code, fill_info=fill_info, index=last_index, signal=-1)
                continue

            filled_shares = int(fill_info.get("filled_shares") or 0)
            exec_price = float(closes_by_code[code][last_index])
            dynamic_extra_cost_rate = max(
                per_side_extra_cost_rate,
                float(fill_info.get("execution_penalty_bps") or 0.0) / 10000.0 / 2.0,
            )
            if slippage_calc is not None:
                slip = slippage_calc.calculate(
                    price=exec_price,
                    volume=float(volumes_by_code[code][last_index]) if last_index < len(volumes_by_code[code]) else 0.0,
                    order_size=float(filled_shares),
                    is_buy=False,
                )
                exec_price = float(slip.get("execution_price", exec_price))
            sell_price = max(0.0, exec_price * (1 - commission - dynamic_extra_cost_rate - sell_tax_rate))
            revenue = float(filled_shares) * sell_price
            shares_before = int(holdings.get(code) or 0)
            average_cost = (
                float(position_cost_basis.get(code, 0.0)) / float(shares_before)
                if shares_before > 0
                else 0.0
            )
            realized_cost = average_cost * float(filled_shares)
            profit = revenue - realized_cost
            position_realized_profit[code] = float(position_realized_profit.get(code, 0.0) + profit)
            holdings[code] = max(0, shares_before - filled_shares)
            position_cost_basis[code] = average_cost * float(holdings[code])
            buy_index = int(entry_indices.get(code, -1))
            if buy_index >= 0 and holdings[code] <= 0:
                holding_periods.append(max(1, last_index - buy_index))
            total_traded_notional += revenue
            execution_penalty_bps_notional += float(fill_info.get("execution_penalty_bps") or 0.0) * revenue
            execution_penalty_bps_weight += revenue
            cash += revenue
            holding_days = max(1, last_index - buy_index) if buy_index >= 0 else 0
            if return_trades:
                trades_detail.append(
                    {
                        'code': code,
                        'index': last_index,
                        'time': str(aligned_klines[code][last_index].get('date', aligned_klines[code][last_index].get('trade_date', aligned_klines[code][last_index].get('time', '')))),
                        'price': float(sell_price),
                        'signal': -1,
                        'shares': int(filled_shares),
                        'profit': float(profit),
                        'holding_days': holding_days,
                    }
                )
            _record_fill_attempt(
                code=code,
                fill_info=fill_info,
                index=last_index,
                signal=-1,
                price=float(sell_price),
                profit=float(profit),
                holding_days=holding_days,
            )
            if holdings[code] <= 0:
                if float(position_realized_profit.get(code, 0.0)) > 0:
                    wins += 1
                completed_round_trips += 1
                position_realized_profit[code] = 0.0
                position_cost_basis.pop(code, None)
                entry_indices.pop(code, None)
                pending_exit[code] = False

        _mark_equity(last_index)
        final_capital, total_return, max_dd, sharpe = _summarize_portfolio_equity(equity, initial_capital)
        avg_holding_days = float(np.mean(holding_periods)) if holding_periods else 0.0
        turnover_proxy = (total_traded_notional / initial_capital) if initial_capital > 0 else 0.0
        win_rate = (wins / completed_round_trips) if completed_round_trips > 0 else 0.0
        execution_summary = {
            "order_attempt_count": int(order_attempt_count),
            "filled_order_count": int(max(0, order_attempt_count - failed_order_count)),
            "failed_order_count": int(failed_order_count),
            "partial_fill_count": int(partial_fill_count),
            "requested_shares": int(requested_shares_total),
            "filled_shares": int(filled_shares_total),
            "rejected_shares": int(rejected_shares_total),
            "fill_rate": round(filled_shares_total / max(requested_shares_total, 1), 6) if requested_shares_total > 0 else 0.0,
            "failed_fill_rate": round(failed_order_count / max(order_attempt_count, 1), 6) if order_attempt_count > 0 else 0.0,
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

        payload = {
            'strategy': strategy,
            'portfolio_mode': 'shared_cash',
            'portfolio_engine_version': 'shared_cash_v1',
            'component_count': len(active_codes),
            'component_codes': active_codes,
            'allocation_mode': allocation_mode,
            'allocation_weights': {code: round(float(weights.get(code, 0.0) or 0.0), 6) for code in active_codes if float(weights.get(code, 0.0) or 0.0) > 0},
            'requested_weight_scheme': normalized_scheme,
            'executed_component_codes': sorted(executed_codes),
            'initial_capital': float(initial_capital),
            'final_capital': float(final_capital),
            'total_return': float(total_return),
            'max_drawdown': float(max_dd),
            'sharpe_ratio': float(sharpe),
            'trades_count': int(len(trades_detail)) if return_trades else int(max(0, order_attempt_count - failed_order_count)),
            'win_rate': float(win_rate),
            'avg_holding_days': float(avg_holding_days),
            'turnover_proxy': float(turnover_proxy),
            'cash_curve': cash_curve.astype(float, copy=False).tolist(),
            'gross_exposure_curve': gross_exposure_curve.astype(float, copy=False).tolist(),
            'net_exposure_curve': net_exposure_curve.astype(float, copy=False).tolist(),
            'execution_summary': execution_summary,
            'params': args,
        }
        if return_trades:
            payload['trades'] = list(trades_detail)
            payload['fills'] = list(fills_detail)
        _finalize_backtest_payload(payload, equity, params=args, closes=portfolio_closes, volumes=portfolio_volumes)
        payload.setdefault('tradability_summary', {})
        payload['tradability_summary'].update(
            {
                'failed_order_count': int(execution_summary['failed_order_count']),
                'blocked_reason_counts': dict(execution_summary['blocked_reason_counts']),
            }
        )
        payload.setdefault('capacity_summary', {})
        payload['capacity_summary'].update(
            {
                'avg_participation_rate': execution_summary['avg_participation_rate'],
                'max_participation_rate': execution_summary['max_participation_rate'],
                'avg_adv_utilization': execution_summary['avg_adv_utilization'],
                'max_adv_utilization': execution_summary['max_adv_utilization'],
                'partial_fill_count': int(execution_summary['partial_fill_count']),
            }
        )
        payload.setdefault('implementation_shortfall_components', {})
        payload['implementation_shortfall_components'].update(
            {
                'avg_execution_penalty_bps': execution_summary['avg_execution_penalty_bps'],
                'execution_fill_rate': execution_summary['fill_rate'],
            }
        )
        payload['equity_curve'] = list(payload.get('equity_curve') or [])
        return {'success': True, 'data': payload}
# --- engine_parts/analytics.py ---

    @staticmethod
    def run_backtest(
        code: str,
        klines: List[Union[Dict[str, Any], Any]],
        strategy: str = 'ma_cross',
        params: Optional[Dict[str, Any]] = None,
        return_trades: bool = False
    ) -> Dict[str, Any]:
        """运行回测"""
        if not klines:
            return {'success': False, 'error': 'No kline data'}

        klines = _ensure_dict_list(klines)
        params = params or {}
        initial_capital = float(params.get("initial_capital", 100000) or 100000)
        commission = float(params.get("commission", 0.0003) or 0.0)

        closes = np.array([k['close'] for k in klines])
        volumes = np.array([k.get('volume', 0.0) for k in klines])

        # 兼容两种成本口径：
        # 1) 显式 slippage（费率）参数
        # 2) slippage_model 推导费率（旧口径）
        explicit_slippage = float(params.get("slippage", 0.0) or 0.0)
        model_slippage_rate = _compute_slippage_rate(closes, volumes, params, 0.0)
        slippage_rate = explicit_slippage if explicit_slippage > 0 else model_slippage_rate
        total_cost_rate = max(0.0, commission + slippage_rate)

        slippage_model_raw = params.get("slippage_model")
        slippage_calc: Optional[SlippageCalculator] = None
        if slippage_model_raw:
            normalized = str(slippage_model_raw).strip().lower()
            if normalized in {"fixed", "volume_based", "market_impact"}:
                slippage_calc = SlippageCalculator(
                    model_type=_resolve_slippage_model(normalized)
                )

        tradability_mask: Optional[np.ndarray] = None
        if bool(params.get("tradability_filter", False)):
            tradability_mask = _build_tradability_mask(
                closes=closes,
                volumes=volumes,
                code=code,
                is_st=bool(params.get("is_st", False)),
            )

        has_execution_overrides = any(
            [
                params.get("max_position_pct") is not None,
                bool(str(params.get("position_assumption") or "").strip()),
                bool(str(params.get("target_weight_scheme") or "").strip()),
                bool(str(params.get("market_ruleset") or "").strip()),
                bool(params.get("t_plus_one")),
                int(params.get("min_trade_lot") or 0) > 1,
                _safe_float(params.get("market_impact_bps", 0.0), 0.0) > 0,
                _safe_float(params.get("sell_tax_rate", 0.0), 0.0) > 0,
            ]
        )
        advanced_exec_enabled = (slippage_calc is not None) or (tradability_mask is not None) or has_execution_overrides
        from .strategy_registry import StrategyRegistry as _Reg

        runtime_inst, execution_semantic_mode = _Reg.create_runtime_strategy(strategy, params)
        if runtime_inst is not None and execution_semantic_mode == "compiled_dsl":
            if hasattr(runtime_inst, 'generate_entry_exit_masks_from_klines'):
                _entry, _exit = runtime_inst.generate_entry_exit_masks_from_klines(klines)
            else:
                _entry, _exit = runtime_inst.generate_entry_exit_masks(closes, volumes)
            _sim = _simulate_trades_from_masks(
                closes=closes,
                volumes=volumes,
                entry_mask=_entry,
                exit_mask=_exit,
                initial_capital=initial_capital,
                commission_rate=commission,
                slippage_calc=slippage_calc,
                tradability_mask=tradability_mask,
                return_trades=return_trades,
                klines=klines,
                params=params,
                signal_events=(
                    runtime_inst.generate_signal_events_from_klines(klines)
                    if hasattr(runtime_inst, 'generate_signal_events_from_klines')
                    else runtime_inst.generate_signal_events(closes, volumes)
                    if hasattr(runtime_inst, 'generate_signal_events')
                    else None
                ),
            )
            _payload = {
                'code': code, 'strategy': strategy,
                'initial_capital': initial_capital,
                'final_capital': float(_sim['final_capital']),
                'total_return': float(_sim['total_return']),
                'max_drawdown': float(_sim['max_drawdown']),
                'sharpe_ratio': float(_sim['sharpe_ratio']),
                'trades_count': int(_sim['trades_count']),
                'win_rate': float(_sim['win_rate']),
                'avg_holding_days': float(_sim.get('avg_holding_days') or 0.0),
                'turnover_proxy': float(_sim.get('turnover_proxy') or 0.0),
                'params': params,
            }
            if return_trades:
                _payload['trades'] = _sim.get('trades') or []
                _payload['fills'] = _sim.get('fills') or []
            _payload['round_trip_positions'] = _sim.get('round_trip_positions') or []
            _payload['closed_round_trip_count'] = int(_sim.get('closed_round_trip_count') or 0)
            _payload['winning_round_trip_count'] = int(_sim.get('winning_round_trip_count') or 0)
            _payload['execution_summary'] = dict(_sim.get('execution_summary') or {})
            _finalize_backtest_payload(_payload, _sim['equity'], params=params, closes=closes, volumes=volumes)
            return {'success': True, 'data': _payload}

        if strategy == 'ma_cross':
            short_period = params.get('short_period', 5)
            long_period = params.get('long_period', 20)

            if advanced_exec_enabled:
                masks = _build_strategy_masks(strategy, closes, params, volumes=volumes, klines=klines)
                if masks is None:
                    return {'success': False, 'error': 'Insufficient data for strategy signals'}
                entry_mask, exit_mask = masks
                sim = _simulate_trades_from_masks(
                    closes=closes,
                    volumes=volumes,
                    entry_mask=entry_mask,
                    exit_mask=exit_mask,
                    initial_capital=initial_capital,
                    commission_rate=commission,
                    slippage_calc=slippage_calc,
                    tradability_mask=tradability_mask,
                    return_trades=return_trades,
                    klines=klines,
                    params=params,
                    signal_events=None,
                )
                payload = {
                    'code': code, 'strategy': strategy,
                    'initial_capital': initial_capital,
                    'final_capital': float(sim['final_capital']),
                    'total_return': float(sim['total_return']),
                    'max_drawdown': float(sim['max_drawdown']),
                    'sharpe_ratio': float(sim['sharpe_ratio']),
                    'trades_count': int(sim['trades_count']),
                    'win_rate': float(sim['win_rate']),
                    'avg_holding_days': float(sim.get('avg_holding_days') or 0.0),
                    'turnover_proxy': float(sim.get('turnover_proxy') or 0.0),
                    'params': params,
                }
                if return_trades:
                    payload['trades'] = sim.get('trades') or []
                    payload['fills'] = sim.get('fills') or []
                payload['round_trip_positions'] = sim.get('round_trip_positions') or []
                payload['closed_round_trip_count'] = int(sim.get('closed_round_trip_count') or 0)
                payload['winning_round_trip_count'] = int(sim.get('winning_round_trip_count') or 0)
                payload['execution_summary'] = dict(sim.get('execution_summary') or {})
                if slippage_calc is not None:
                    payload['slippage_model'] = str(slippage_model_raw).strip().lower()
                if tradability_mask is not None:
                    payload['tradability_filter'] = True
                    payload['tradable_days'] = int(np.sum(tradability_mask))
                    payload['total_days'] = int(len(tradability_mask))
                _finalize_backtest_payload(payload, sim['equity'], params=params, closes=closes, volumes=volumes)
                return {'success': True, 'data': payload}

            if return_trades:
                result = _backtest_ma_cross_with_trades_jit(
                    closes, short_period, long_period, initial_capital, total_cost_rate
                )
                (final_capital, total_return, max_dd, sharpe, total_trades, win_rate, equity,
                 trade_count, trade_indices, trade_types, trade_prices, trade_shares, trade_profits) = result

                trades_detail = []
                for i in range(trade_count):
                    idx = int(trade_indices[i])
                    trades_detail.append({
                        'index': idx,
                        'time': klines[idx].get('date', klines[idx].get('trade_date', '')),
                        'price': float(trade_prices[i]),
                        'signal': int(trade_types[i]),
                        'shares': int(trade_shares[i]),
                        'profit': float(trade_profits[i])
                    })

                data = {
                    'code': code, 'strategy': strategy,
                    'initial_capital': initial_capital,
                    'final_capital': float(final_capital),
                    'total_return': float(total_return),
                    'max_drawdown': float(max_dd),
                    'sharpe_ratio': float(sharpe),
                    'trades_count': int(total_trades),
                    'win_rate': float(win_rate),
                    'params': params,
                    'trades': trades_detail
                }
                _finalize_backtest_payload(data, equity, params=params, closes=closes, volumes=volumes)
                return {'success': True, 'data': data}

            result = _backtest_ma_cross_jit(
                closes, short_period, long_period, initial_capital, total_cost_rate
            )
            final_capital, total_return, max_dd, sharpe, trades, win_rate, equity = result

            data = {
                'code': code, 'strategy': strategy,
                'initial_capital': initial_capital,
                'final_capital': float(final_capital),
                'total_return': float(total_return),
                'max_drawdown': float(max_dd),
                'sharpe_ratio': float(sharpe),
                'trades_count': int(trades),
                'win_rate': float(win_rate),
                'params': params,
            }
            _finalize_backtest_payload(data, equity, params=params, closes=closes, volumes=volumes)
            return {'success': True, 'data': data}

        elif strategy == 'buy_and_hold':
            entry_idx = 0
            exit_idx = len(closes) - 1
            if tradability_mask is not None:
                tradable_idx = np.where(tradability_mask)[0]
                if len(tradable_idx) < 2:
                    return {'success': False, 'error': 'No enough tradable days for buy_and_hold'}
                entry_idx = int(tradable_idx[0])
                exit_idx = int(tradable_idx[-1])

            entry_price = float(closes[entry_idx])
            exit_price = float(closes[exit_idx])
            if slippage_calc is not None:
                buy_slip = slippage_calc.calculate(
                    price=entry_price,
                    volume=float(volumes[entry_idx]) if entry_idx < len(volumes) else 0.0,
                    order_size=float(initial_capital / max(entry_price, 1e-8)),
                    is_buy=True,
                )
                entry_price = float(buy_slip.get("execution_price", entry_price))
                sell_slip = slippage_calc.calculate(
                    price=exit_price,
                    volume=float(volumes[exit_idx]) if exit_idx < len(volumes) else 0.0,
                    order_size=float(initial_capital / max(entry_price, 1e-8)),
                    is_buy=False,
                )
                exit_price = float(sell_slip.get("execution_price", exit_price))

            buy_price = entry_price * (1 + total_cost_rate)
            shares = initial_capital / buy_price if buy_price > 0 else 0.0
            final_capital = shares * exit_price * (1 - total_cost_rate)
            total_return = (final_capital - initial_capital) / initial_capital

            equity = shares * closes
            peak = np.maximum.accumulate(equity)
            drawdown = (peak - equity) / peak
            max_dd = float(np.max(drawdown))

            data = {
                'code': code, 'strategy': strategy,
                'initial_capital': initial_capital,
                'final_capital': float(final_capital),
                'total_return': float(total_return),
                'max_drawdown': max_dd,
                'sharpe_ratio': 0.0,
                'trades_count': 1,
                'win_rate': 1.0 if total_return > 0 else 0.0,
                'avg_holding_days': float(max(1, exit_idx - entry_idx)),
                'turnover_proxy': float(((shares * buy_price) + (shares * exit_price)) / initial_capital) if initial_capital > 0 else 0.0,
            }
            if tradability_mask is not None:
                data['tradability_filter'] = True
                data['entry_index'] = entry_idx
                data['exit_index'] = exit_idx
            if slippage_calc is not None:
                data['slippage_model'] = str(slippage_model_raw).strip().lower()
            _finalize_backtest_payload(data, equity, params=params, closes=closes, volumes=volumes)
            return {
                'success': True,
                'data': data
            }

        elif strategy == 'momentum':
            lookback = params.get('lookback', 20)
            threshold = params.get('threshold', 0.02)
            if advanced_exec_enabled:
                masks = _build_strategy_masks(strategy, closes, params, volumes=volumes, klines=klines)
                if masks is None:
                    return {'success': False, 'error': 'Insufficient data for strategy signals'}
                entry_mask, exit_mask = masks
                sim = _simulate_trades_from_masks(
                    closes=closes,
                    volumes=volumes,
                    entry_mask=entry_mask,
                    exit_mask=exit_mask,
                    initial_capital=initial_capital,
                    commission_rate=commission,
                    slippage_calc=slippage_calc,
                    tradability_mask=tradability_mask,
                    return_trades=return_trades,
                    klines=klines,
                    params=params,
                    signal_events=None,
                )
                payload = {
                    'code': code, 'strategy': strategy,
                    'initial_capital': initial_capital,
                    'final_capital': float(sim['final_capital']),
                    'total_return': float(sim['total_return']),
                    'max_drawdown': float(sim['max_drawdown']),
                    'sharpe_ratio': float(sim['sharpe_ratio']),
                    'trades_count': int(sim['trades_count']),
                    'win_rate': float(sim['win_rate']),
                    'avg_holding_days': float(sim.get('avg_holding_days') or 0.0),
                    'turnover_proxy': float(sim.get('turnover_proxy') or 0.0),
                    'params': params,
                }
                if return_trades:
                    payload['trades'] = sim.get('trades') or []
                    payload['fills'] = sim.get('fills') or []
                payload['round_trip_positions'] = sim.get('round_trip_positions') or []
                payload['closed_round_trip_count'] = int(sim.get('closed_round_trip_count') or 0)
                payload['winning_round_trip_count'] = int(sim.get('winning_round_trip_count') or 0)
                payload['execution_summary'] = dict(sim.get('execution_summary') or {})
                if slippage_calc is not None:
                    payload['slippage_model'] = str(slippage_model_raw).strip().lower()
                if tradability_mask is not None:
                    payload['tradability_filter'] = True
                    payload['tradable_days'] = int(np.sum(tradability_mask))
                    payload['total_days'] = int(len(tradability_mask))
                _finalize_backtest_payload(payload, sim['equity'], params=params, closes=closes, volumes=volumes)
                return {'success': True, 'data': payload}

            result = _backtest_momentum_jit(
                closes, lookback, threshold, initial_capital, total_cost_rate
            )
            final_capital, total_return, max_dd, sharpe, trades, win_rate, equity = result

            data = {
                'code': code, 'strategy': strategy,
                'initial_capital': initial_capital,
                'final_capital': float(final_capital),
                'total_return': float(total_return),
                'max_drawdown': float(max_dd),
                'sharpe_ratio': float(sharpe),
                'trades_count': int(trades),
                'win_rate': float(win_rate),
                'params': params,
            }
            _finalize_backtest_payload(data, equity, params=params, closes=closes, volumes=volumes)
            return {'success': True, 'data': data}

        elif strategy == 'rsi':
            rsi_period = params.get('rsi_period', 12)
            oversold = params.get('oversold', 18)
            overbought = params.get('overbought', 64)
            regime_filter_enabled = 1 if bool(params.get('regime_filter_enabled', True)) else 0
            noise_filter_enabled = 1 if bool(params.get('noise_filter_enabled', True)) else 0
            noise_window = int(params.get('noise_window', 6) or 6)
            noise_ceiling = float(params.get('noise_ceiling', 6.0) or 6.0)
            bearish_regime_threshold = float(
                params.get('bearish_regime_threshold', -0.05) or -0.05
            )
            regime_break_threshold = float(
                params.get('regime_break_threshold', 0.015) or 0.015
            )
            repair_confirmation_enabled = 1 if bool(
                params.get('repair_confirmation_enabled', True)
            ) else 0
            repair_confirmation_window = int(
                params.get('repair_confirmation_window', 6) or 6
            )
            repair_confirmation_rebound_pct = float(
                params.get('repair_confirmation_rebound_pct', 0.008) or 0.008
            )
            repair_confirmation_rsi_reclaim = float(
                params.get('repair_confirmation_rsi_reclaim', 24.0) or 24.0
            )
            liquidity_confirmation_enabled = 1 if bool(
                params.get('liquidity_confirmation_enabled', True)
            ) else 0
            liquidity_window = int(params.get('liquidity_window', 8) or 8)
            liquidity_volume_floor_ratio = float(
                params.get('liquidity_volume_floor_ratio', 0.8) or 0.8
            )
            structure_confirmation_enabled = 1 if bool(
                params.get('structure_confirmation_enabled', True)
            ) else 0
            structure_window = int(params.get('structure_window', 4) or 4)
            structure_close_location_min = float(
                params.get('structure_close_location_min', 0.55) or 0.55
            )
            structure_body_return_min = float(
                params.get('structure_body_return_min', 0.0015) or 0.0015
            )
            mean_reversion_exit_min_hold_bars = int(
                params.get('mean_reversion_exit_min_hold_bars', 4) or 4
            )
            mean_reversion_exit_buffer = float(
                params.get('mean_reversion_exit_buffer', -0.002) or -0.002
            )
            max_hold_bars = int(params.get('max_hold_bars', 6) or 6)
            adverse_regime_exit_enabled = 1 if bool(
                params.get('adverse_regime_exit_enabled', True)
            ) else 0
            adverse_noise_ceiling = float(
                params.get('adverse_noise_ceiling', noise_ceiling) or noise_ceiling
            )
            if advanced_exec_enabled:
                masks = _build_strategy_masks(strategy, closes, params, volumes=volumes, klines=klines)
                if masks is None:
                    return {'success': False, 'error': 'Insufficient data for strategy signals'}
                entry_mask, exit_mask = masks
                sim = _simulate_trades_from_masks(
                    closes=closes,
                    volumes=volumes,
                    entry_mask=entry_mask,
                    exit_mask=exit_mask,
                    initial_capital=initial_capital,
                    commission_rate=commission,
                    slippage_calc=slippage_calc,
                    tradability_mask=tradability_mask,
                    return_trades=return_trades,
                    klines=klines,
                    params=params,
                    signal_events=None,
                )
                payload = {
                    'code': code, 'strategy': strategy,
                    'initial_capital': initial_capital,
                    'final_capital': float(sim['final_capital']),
                    'total_return': float(sim['total_return']),
                    'max_drawdown': float(sim['max_drawdown']),
                    'sharpe_ratio': float(sim['sharpe_ratio']),
                    'trades_count': int(sim['trades_count']),
                    'win_rate': float(sim['win_rate']),
                    'avg_holding_days': float(sim.get('avg_holding_days') or 0.0),
                    'turnover_proxy': float(sim.get('turnover_proxy') or 0.0),
                    'params': params,
                }
                if return_trades:
                    payload['trades'] = sim.get('trades') or []
                    payload['fills'] = sim.get('fills') or []
                payload['round_trip_positions'] = sim.get('round_trip_positions') or []
                payload['closed_round_trip_count'] = int(sim.get('closed_round_trip_count') or 0)
                payload['winning_round_trip_count'] = int(sim.get('winning_round_trip_count') or 0)
                payload['execution_summary'] = dict(sim.get('execution_summary') or {})
                if slippage_calc is not None:
                    payload['slippage_model'] = str(slippage_model_raw).strip().lower()
                if tradability_mask is not None:
                    payload['tradability_filter'] = True
                    payload['tradable_days'] = int(np.sum(tradability_mask))
                    payload['total_days'] = int(len(tradability_mask))
                _finalize_backtest_payload(payload, sim['equity'], params=params, closes=closes, volumes=volumes)
                return {'success': True, 'data': payload}

            result = _backtest_rsi_jit(
                closes,
                volumes,
                rsi_period,
                oversold,
                overbought,
                regime_filter_enabled,
                noise_filter_enabled,
                noise_window,
                noise_ceiling,
                bearish_regime_threshold,
                regime_break_threshold,
                repair_confirmation_enabled,
                repair_confirmation_window,
                repair_confirmation_rebound_pct,
                repair_confirmation_rsi_reclaim,
                liquidity_confirmation_enabled,
                liquidity_window,
                liquidity_volume_floor_ratio,
                structure_confirmation_enabled,
                structure_window,
                structure_close_location_min,
                structure_body_return_min,
                mean_reversion_exit_min_hold_bars,
                mean_reversion_exit_buffer,
                max_hold_bars,
                adverse_regime_exit_enabled,
                adverse_noise_ceiling,
                initial_capital,
                total_cost_rate,
            )
            final_capital, total_return, max_dd, sharpe, trades, win_rate, equity = result

            data = {
                'code': code, 'strategy': strategy,
                'initial_capital': initial_capital,
                'final_capital': float(final_capital),
                'total_return': float(total_return),
                'max_drawdown': float(max_dd),
                'sharpe_ratio': float(sharpe),
                'trades_count': int(trades),
                'win_rate': float(win_rate),
                'params': params,
            }
            _finalize_backtest_payload(data, equity, params=params, closes=closes, volumes=volumes)
            return {'success': True, 'data': data}

        # Generic registry fallback for custom/factory strategies
        _inst, _execution_semantic_mode = _Reg.create_runtime_strategy(strategy, params)
        if _inst is not None:
            if hasattr(_inst, 'generate_entry_exit_masks_from_klines'):
                _masks = _inst.generate_entry_exit_masks_from_klines(klines)
            else:
                _masks = _inst.generate_entry_exit_masks(closes, volumes)
            if _masks is not None and _masks[0] is not None:
                _entry, _exit = _masks
                _sim = _simulate_trades_from_masks(
                    closes=closes, volumes=volumes,
                    entry_mask=_entry, exit_mask=_exit,
                    initial_capital=initial_capital,
                    commission_rate=commission,
                    slippage_calc=slippage_calc,
                    tradability_mask=tradability_mask,
                    return_trades=return_trades,
                    klines=klines,
                    params=params,
                    signal_events=(
                        _inst.generate_signal_events_from_klines(klines)
                        if hasattr(_inst, 'generate_signal_events_from_klines')
                        else _inst.generate_signal_events(closes, volumes)
                        if hasattr(_inst, 'generate_signal_events')
                        else None
                    ),
                )
                _avg_holding_days = _sim.get('avg_holding_days')
                _turnover_proxy = _sim.get('turnover_proxy')
                _payload = {
                    'code': code, 'strategy': strategy,
                    'initial_capital': initial_capital,
                    'final_capital': float(_sim['final_capital']),
                    'total_return': float(_sim['total_return']),
                    'max_drawdown': float(_sim['max_drawdown']),
                    'sharpe_ratio': float(_sim['sharpe_ratio']),
                    'trades_count': int(_sim['trades_count']),
                    # None when unmeasurable: a backtest with no closed round trip
                    # has no win rate; fabricating 0.0 would read as "measured,
                    # never won" and poison downstream medians.
                    'win_rate': None if _sim['win_rate'] is None else float(_sim['win_rate']),
                    'avg_holding_days': None if _avg_holding_days is None else float(_avg_holding_days),
                    'turnover_proxy': None if _turnover_proxy is None else float(_turnover_proxy),
                    'params': params,
                }
                if return_trades:
                    _payload['trades'] = _sim.get('trades') or []
                    _payload['fills'] = _sim.get('fills') or []
                _payload['round_trip_positions'] = _sim.get('round_trip_positions') or []
                _payload['closed_round_trip_count'] = int(_sim.get('closed_round_trip_count') or 0)
                _payload['winning_round_trip_count'] = int(_sim.get('winning_round_trip_count') or 0)
                _payload['execution_summary'] = dict(_sim.get('execution_summary') or {})
                _finalize_backtest_payload(_payload, _sim['equity'], params=params, closes=closes, volumes=volumes)
                return {'success': True, 'data': _payload}

        return {'success': False, 'error': f'Unknown strategy: {strategy}'}
# --- engine_parts/artifacts.py ---

    @staticmethod
    def optimize_parameters(
        code: str,
        klines: List[Union[Dict[str, Any], Any]],
        strategy: str = 'ma_cross',
        param_ranges: Optional[Dict[str, List]] = None
    ) -> Dict[str, Any]:
        """参数优化（网格搜索）"""
        if not klines:
            return {'success': False, 'error': 'No kline data'}

        klines = _ensure_dict_list(klines)
        param_ranges = param_ranges or {}

        if strategy == 'ma_cross':
            short_periods = param_ranges.get('short_period', [5, 10, 15])
            long_periods = param_ranges.get('long_period', [20, 30, 40])

            best_params = None
            best_metric = -float('inf')
            all_results = []

            for short in short_periods:
                for long in long_periods:
                    if short >= long:
                        continue
                    params = {
                        'initial_capital': 100000, 'commission': 0.0003,
                        'short_period': short, 'long_period': long,
                    }
                    result = BacktestEngine.run_backtest(code, klines, strategy, params)
                    if result['success']:
                        data = result['data']
                        metric = data['sharpe_ratio'] * (1 - data['max_drawdown'])
                        all_results.append({
                            'params': params, 'metric': metric,
                            'total_return': data['total_return'],
                            'sharpe_ratio': data['sharpe_ratio'],
                            'max_drawdown': data['max_drawdown'],
                        })
                        if metric > best_metric:
                            best_metric = metric
                            best_params = params

            return {
                'success': True,
                'data': {
                    'best_params': best_params,
                    'best_metric': best_metric,
                    'all_results': all_results,
                }
            }

        return {'success': False, 'error': f'Parameter optimization not supported for strategy: {strategy}'}

    @staticmethod
    def monte_carlo_simulation(
        code: str,
        klines: List[Union[Dict[str, Any], Any]],
        strategy: str = 'ma_cross',
        params: Optional[Dict[str, Any]] = None,
        runs: int = 1000,
        bootstrap_method: str = 'normal',
    ) -> Dict[str, Any]:
        """蒙特卡洛模拟

        Args:
            bootstrap_method: 'normal' (正态分布) 或 'block' (Block Bootstrap，保留序列自相关)
        """
        if not klines:
            return {'success': False, 'error': 'No kline data'}

        klines = _ensure_dict_list(klines)
        params = params or {}
        closes = np.array([k['close'] for k in klines])

        returns = np.diff(closes) / closes[:-1]
        mean_return = np.mean(returns)
        std_return = np.std(returns)

        # Block Bootstrap 参数
        block_size = max(5, int(params.get('block_size', 20)))

        final_capitals = []
        max_drawdowns = []

        for _ in range(runs):
            if bootstrap_method == 'block' and len(returns) >= block_size:
                # Block Bootstrap: 随机抽取连续块拼接，保留序列自相关
                n_blocks = max(1, len(returns) // block_size + 1)
                sim_parts = []
                for _ in range(n_blocks):
                    start = np.random.randint(0, max(1, len(returns) - block_size + 1))
                    sim_parts.append(returns[start:start + block_size])
                simulated_returns = np.concatenate(sim_parts)[:len(returns)]
            else:
                simulated_returns = np.random.normal(mean_return, std_return, len(returns))

            simulated_closes = closes[0] * np.cumprod(1 + simulated_returns)
            simulated_closes = np.insert(simulated_closes, 0, closes[0])

            simulated_klines = [
                {'close': float(c), 'date': klines[i]['date']}
                for i, c in enumerate(simulated_closes)
            ]

            result = BacktestEngine.run_backtest(code, simulated_klines, strategy, params)
            if result['success']:
                final_capitals.append(result['data']['final_capital'])
                max_drawdowns.append(result['data']['max_drawdown'])

        if not final_capitals:
            return {'success': False, 'error': 'Simulation failed'}

        final_capitals = np.array(final_capitals)
        max_drawdowns = np.array(max_drawdowns)

        return {
            'success': True,
            'data': {
                'runs': runs,
                'bootstrap_method': bootstrap_method,
                'best_case': float(np.max(final_capitals)),
                'worst_case': float(np.min(final_capitals)),
                'average': float(np.mean(final_capitals)),
                'median': float(np.median(final_capitals)),
                'confidence_95': float(np.percentile(final_capitals, 5)),
                'avg_drawdown': float(np.mean(max_drawdowns)),
                'max_drawdown': float(np.max(max_drawdowns)),
            }
        }

    @staticmethod
    def walk_forward_analysis(
        code: str,
        klines: List[Union[Dict[str, Any], Any]],
        strategy: str = 'ma_cross',
        param_ranges: Optional[Dict[str, List]] = None,
        train_window: int = 250,
        test_window: int = 60
    ) -> Dict[str, Any]:
        """Walk-Forward分析"""
        klines = _ensure_dict_list(klines)

        if len(klines) < train_window + test_window:
            return {'success': False, 'error': 'Insufficient data for walk-forward analysis'}

        segments = []
        capital = 100000

        i = 0
        while i + train_window + test_window <= len(klines):
            train_klines = klines[i:i+train_window]
            opt_result = BacktestEngine.optimize_parameters(
                code, train_klines, strategy, param_ranges
            )
            if not opt_result['success']:
                break

            best_params = opt_result['data']['best_params']
            test_klines = klines[i+train_window:i+train_window+test_window]
            test_result = BacktestEngine.run_backtest(code, test_klines, strategy, best_params)

            if test_result['success']:
                data = test_result['data']
                segments.append({
                    'period': f"{test_klines[0]['date']} to {test_klines[-1]['date']}",
                    'params': best_params,
                    'return': data['total_return'],
                    'sharpe': data['sharpe_ratio'],
                    'max_drawdown': data['max_drawdown'],
                })
                capital *= (1 + data['total_return'])

            i += test_window

        if not segments:
            return {'success': False, 'error': 'Walk-forward analysis failed'}

        overall_return = (capital - 100000) / 100000

        return {
            'success': True,
            'data': {
                'segments': segments,
                'overall_return': overall_return,
                'final_capital': capital,
            }
        }


backtest_engine = BacktestEngine()
