"""JIT优化的回测策略函数"""

import numpy as np
from numba import jit


@jit(nopython=True)
def _backtest_ma_cross_jit(
    closes: np.ndarray,
    short_period: int,
    long_period: int,
    initial_capital: float,
    total_cost_rate: float
) -> tuple:
    """Numba优化的均线交叉回测核心"""
    n = len(closes)

    short_ma = np.zeros(n)
    long_ma = np.zeros(n)

    for i in range(short_period - 1, n):
        short_ma[i] = np.mean(closes[i-short_period+1:i+1])

    for i in range(long_period - 1, n):
        long_ma[i] = np.mean(closes[i-long_period+1:i+1])

    cash = initial_capital
    shares = 0
    equity = np.zeros(n)
    trades = 0
    wins = 0

    for i in range(long_period, n - 1):
        if short_ma[i-1] <= long_ma[i-1] and short_ma[i] > long_ma[i] and cash > 0:
            buy_price = closes[i + 1] * (1 + total_cost_rate)
            shares = int(cash / buy_price)
            cash -= shares * buy_price
            trades += 1
        elif short_ma[i-1] >= long_ma[i-1] and short_ma[i] < long_ma[i] and shares > 0:
            sell_price = closes[i + 1] * (1 - total_cost_rate)
            profit = shares * sell_price - shares * closes[i]
            if profit > 0:
                wins += 1
            cash += shares * sell_price
            shares = 0

        equity[i] = cash + shares * closes[i]

    equity[n - 1] = cash + shares * closes[n - 1]

    if shares > 0:
        cash += shares * closes[-1] * (1 - total_cost_rate)
        shares = 0

    final_capital = cash
    total_return = (final_capital - initial_capital) / initial_capital

    max_dd = 0.0
    peak = equity[long_period]
    for i in range(long_period, n):
        if equity[i] > peak:
            peak = equity[i]
        dd = (peak - equity[i]) / peak if peak > 0 else 0
        if dd > max_dd:
            max_dd = dd

    returns = np.diff(equity[long_period:]) / equity[long_period:-1]
    valid_mask = equity[long_period:-1] > 0
    returns = returns[valid_mask]
    sharpe = 0.0
    if len(returns) > 0:
        mean_return = np.mean(returns)
        std_return = np.std(returns)
        if std_return > 0:
            annual_ret = mean_return * 252.0
            annual_std = std_return * np.sqrt(252.0)
            # Match mask/engine path: deduct 2% annual risk-free rate.
            sharpe = (annual_ret - 0.02) / annual_std

    win_rate = wins / trades if trades > 0 else 0.0  # trades仅计买入，即round-trip数

    return final_capital, total_return, max_dd, sharpe, trades, win_rate, equity


@jit(nopython=True)
def _backtest_ma_cross_with_trades_jit(
    closes: np.ndarray,
    short_period: int,
    long_period: int,
    initial_capital: float,
    total_cost_rate: float,
    max_trades: int = 1000
) -> tuple:
    """带交易记录的均线交叉回测 (Numba JIT 优化版本)"""
    n = len(closes)

    trade_indices = np.zeros(max_trades, dtype=np.int64)
    trade_types = np.zeros(max_trades, dtype=np.int64)
    trade_prices = np.zeros(max_trades, dtype=np.float64)
    trade_shares = np.zeros(max_trades, dtype=np.int64)
    trade_profits = np.zeros(max_trades, dtype=np.float64)
    trade_count = 0

    short_ma = np.zeros(n, dtype=np.float64)
    long_ma = np.zeros(n, dtype=np.float64)

    for i in range(short_period - 1, n):
        short_ma[i] = np.mean(closes[i-short_period+1:i+1])

    for i in range(long_period - 1, n):
        long_ma[i] = np.mean(closes[i-long_period+1:i+1])

    cash = initial_capital
    shares = 0
    buy_price = 0.0
    equity = np.zeros(n, dtype=np.float64)
    wins = 0

    for i in range(long_period, n - 1):
        if short_ma[i-1] <= long_ma[i-1] and short_ma[i] > long_ma[i] and cash > 0:
            buy_price = closes[i + 1] * (1 + total_cost_rate)
            buy_shares = int(cash / buy_price)
            if buy_shares > 0:
                cost = buy_shares * buy_price
                cash -= cost
                shares = buy_shares
                if trade_count < max_trades:
                    trade_indices[trade_count] = i + 1
                    trade_types[trade_count] = 1
                    trade_prices[trade_count] = buy_price
                    trade_shares[trade_count] = buy_shares
                    trade_profits[trade_count] = 0.0
                    trade_count += 1

        elif short_ma[i-1] >= long_ma[i-1] and short_ma[i] < long_ma[i] and shares > 0:
            sell_price = closes[i + 1] * (1 - total_cost_rate)
            revenue = shares * sell_price
            profit = revenue - shares * buy_price
            if profit > 0:
                wins += 1
            if trade_count < max_trades:
                trade_indices[trade_count] = i + 1
                trade_types[trade_count] = -1
                trade_prices[trade_count] = sell_price
                trade_shares[trade_count] = shares
                trade_profits[trade_count] = profit
                trade_count += 1
            cash += revenue
            shares = 0

        equity[i] = cash + shares * closes[i]

    equity[n - 1] = cash + shares * closes[n - 1]

    if shares > 0:
        sell_price = closes[-1] * (1 - total_cost_rate)
        revenue = shares * sell_price
        profit = revenue - shares * buy_price
        if trade_count < max_trades:
            trade_indices[trade_count] = n - 1
            trade_types[trade_count] = -1
            trade_prices[trade_count] = sell_price
            trade_shares[trade_count] = shares
            trade_profits[trade_count] = profit
            trade_count += 1
        cash += revenue
        shares = 0

    final_capital = cash
    total_return = (final_capital - initial_capital) / initial_capital

    max_dd = 0.0
    peak = equity[long_period]
    for i in range(long_period, n):
        if equity[i] > peak:
            peak = equity[i]
        dd = (peak - equity[i]) / peak if peak > 0 else 0
        if dd > max_dd:
            max_dd = dd

    returns = np.diff(equity[long_period:]) / equity[long_period:-1]
    valid_mask = equity[long_period:-1] > 0
    returns = returns[valid_mask]
    sharpe = 0.0
    if len(returns) > 0:
        mean_return = np.mean(returns)
        std_return = np.std(returns)
        if std_return > 0:
            annual_ret = mean_return * 252.0
            annual_std = std_return * np.sqrt(252.0)
            # Match mask/engine path: deduct 2% annual risk-free rate.
            sharpe = (annual_ret - 0.02) / annual_std

    total_trades = trade_count
    win_rate = wins / (trade_count // 2) if trade_count > 0 else 0.0

    return (
        final_capital, total_return, max_dd, sharpe, total_trades, win_rate, equity,
        trade_count, trade_indices[:trade_count], trade_types[:trade_count],
        trade_prices[:trade_count], trade_shares[:trade_count], trade_profits[:trade_count]
    )



@jit(nopython=True)
def _backtest_momentum_jit(
    closes: np.ndarray,
    lookback: int,
    threshold: float,
    initial_capital: float,
    total_cost_rate: float
) -> tuple:
    """Numba优化的动量策略回测"""
    n = len(closes)
    cash = initial_capital
    shares = 0
    equity = np.full(n, initial_capital)
    trades = 0
    wins = 0

    for i in range(lookback, n - 1):
        momentum = (closes[i] - closes[i-lookback]) / closes[i-lookback]

        if momentum > threshold and shares == 0:
            buy_price = closes[i + 1] * (1 + total_cost_rate)
            max_shares = int(cash / buy_price)
            if max_shares > 0:
                cost = max_shares * buy_price
                shares = max_shares
                cash -= cost
                trades += 1

        elif momentum < -threshold and shares > 0:
            sell_price = closes[i + 1] * (1 - total_cost_rate)
            revenue = shares * sell_price
            profit = revenue - (shares * buy_price)
            if profit > 0:
                wins += 1
            cash += revenue
            shares = 0
            trades += 1

        equity[i] = cash + shares * closes[i]

    equity[n - 1] = cash + shares * closes[n - 1]

    if shares > 0:
        sell_price = closes[-1] * (1 - total_cost_rate)
        revenue = shares * sell_price
        cash += revenue
        shares = 0
        trades += 1

    final_capital = cash
    total_return = (final_capital - initial_capital) / initial_capital

    max_dd = 0.0
    peak = equity[lookback]
    for i in range(lookback, n):
        if equity[i] > peak:
            peak = equity[i]
        dd = (peak - equity[i]) / peak if peak > 0 else 0
        if dd > max_dd:
            max_dd = dd

    returns = np.diff(equity[lookback:]) / equity[lookback:-1]
    valid_mask = equity[lookback:-1] > 0
    returns = returns[valid_mask]
    sharpe = 0.0
    if len(returns) > 0:
        mean_return = np.mean(returns)
        std_return = np.std(returns)
        if std_return > 0:
            annual_ret = mean_return * 252.0
            annual_std = std_return * np.sqrt(252.0)
            # Match mask/engine path: deduct 2% annual risk-free rate.
            sharpe = (annual_ret - 0.02) / annual_std

    win_rate = wins / max(1, trades // 2) if trades > 0 else 0.0  # trades计买+卖，round-trip = trades//2

    return final_capital, total_return, max_dd, sharpe, trades, win_rate, equity


@jit(nopython=True)
def _backtest_rsi_jit(
    closes: np.ndarray,
    volumes: np.ndarray,
    rsi_period: int,
    oversold: float,
    overbought: float,
    regime_filter_enabled: int,
    noise_filter_enabled: int,
    noise_window: int,
    noise_ceiling: float,
    bearish_regime_threshold: float,
    regime_break_threshold: float,
    repair_confirmation_enabled: int,
    repair_confirmation_window: int,
    repair_confirmation_rebound_pct: float,
    repair_confirmation_rsi_reclaim: float,
    liquidity_confirmation_enabled: int,
    liquidity_window: int,
    liquidity_volume_floor_ratio: float,
    structure_confirmation_enabled: int,
    structure_window: int,
    structure_close_location_min: float,
    structure_body_return_min: float,
    mean_reversion_exit_min_hold_bars: int,
    mean_reversion_exit_buffer: float,
    max_hold_bars: int,
    adverse_regime_exit_enabled: int,
    adverse_noise_ceiling: float,
    initial_capital: float,
    total_cost_rate: float
) -> tuple:
    """Numba优化的RSI策略回测"""
    n = len(closes)
    mean_window = max(6, rsi_period * 2)
    exit_window = max(20, mean_window - 4)
    regime_lookback = 30
    regime_volatility_window = 20

    rsi = np.zeros(n)
    for i in range(rsi_period, n):
        gains = 0.0
        losses = 0.0
        for j in range(i - rsi_period + 1, i + 1):
            change = closes[j] - closes[j-1]
            if change > 0:
                gains += change
            else:
                losses -= change

        avg_gain = gains / rsi_period
        avg_loss = losses / rsi_period

        if avg_loss == 0:
            rsi[i] = 100
        else:
            rs = avg_gain / avg_loss
            rsi[i] = 100 - (100 / (1 + rs))

    mean_line = np.zeros(n)
    mean_ready = np.zeros(n, dtype=np.int8)
    for i in range(mean_window - 1, n):
        total = 0.0
        for j in range(i - mean_window + 1, i + 1):
            total += closes[j]
        mean_line[i] = total / mean_window
        mean_ready[i] = 1

    exit_line = np.zeros(n)
    exit_ready = np.zeros(n, dtype=np.int8)
    for i in range(exit_window - 1, n):
        total = 0.0
        for j in range(i - exit_window + 1, i + 1):
            total += closes[j]
        exit_line[i] = total / exit_window
        exit_ready[i] = 1

    noise_ratio = np.zeros(n)
    noise_ready = np.zeros(n, dtype=np.int8)
    for i in range(max(noise_window, 2), n):
        path_length = 0.0
        for j in range(i - noise_window + 1, i + 1):
            path_length += abs(closes[j] - closes[j - 1])
        net_move = abs(closes[i] - closes[i - noise_window])
        noise_ratio[i] = path_length / (net_move if net_move > 1e-6 else 1e-6)
        noise_ready[i] = 1

    volume_mean = np.zeros(n)
    volume_ready = np.zeros(n, dtype=np.int8)
    effective_liquidity_window = max(liquidity_window, 2)
    if len(volumes) == n:
        for i in range(effective_liquidity_window - 1, n):
            total = 0.0
            for j in range(i - effective_liquidity_window + 1, i + 1):
                total += max(volumes[j], 0.0)
            volume_mean[i] = total / effective_liquidity_window
            volume_ready[i] = 1

    cash = initial_capital
    shares = 0
    equity = np.full(n, initial_capital)
    trades = 0
    wins = 0
    buy_price = 0.0
    entry_index = -1
    pending_confirmation = 0
    confirmation_start_index = -1
    confirmation_anchor_index = -1
    confirmation_anchor_price = 0.0
    start_index = max(
        rsi_period,
        mean_window - 1,
        exit_window - 1,
        regime_lookback,
        regime_volatility_window,
        noise_window,
        effective_liquidity_window,
        structure_window,
    )

    for i in range(start_index, n - 1):
        if mean_ready[i] == 0 or mean_line[i] <= 0:
            equity[i] = cash + shares * closes[i]
            continue
        deviation = (closes[i] - mean_line[i]) / mean_line[i]
        regime_code = 0
        base_close = closes[i - regime_lookback]
        if base_close > 0:
            ret_window = (closes[i] - base_close) / base_close
            mean_return = 0.0
            valid_count = 0
            for j in range(i - regime_volatility_window + 1, i + 1):
                if closes[j - 1] > 0:
                    mean_return += (closes[j] - closes[j - 1]) / closes[j - 1]
                    valid_count += 1
            if valid_count > 0:
                mean_return = mean_return / valid_count
                variance = 0.0
                for j in range(i - regime_volatility_window + 1, i + 1):
                    if closes[j - 1] > 0:
                        one_return = (closes[j] - closes[j - 1]) / closes[j - 1]
                        diff = one_return - mean_return
                        variance += diff * diff
                annualized_volatility = ((variance / valid_count) ** 0.5) * (250.0 ** 0.5)
                is_volatile = annualized_volatility >= 0.30
                if ret_window <= bearish_regime_threshold:
                    regime_code = 2 if is_volatile else 1
                elif ret_window >= 0.05:
                    regime_code = 4 if is_volatile else 3
                else:
                    regime_code = 6 if is_volatile else 5
        regime_ready = 1
        if regime_filter_enabled == 1:
            regime_ready = 1 if (regime_code == 1 or regime_code == 2) else 0
        noise_ready_flag = 1
        if noise_filter_enabled == 1:
            noise_ready_flag = 1 if (noise_ready[i] == 1 and noise_ratio[i] <= noise_ceiling) else 0
        base_entry_ready = 1 if (
            rsi[i] < oversold
            and deviation <= -0.015
            and regime_ready == 1
            and noise_ready_flag == 1
        ) else 0
        entry_ready = 0
        if shares == 0:
            if repair_confirmation_enabled == 1:
                if base_entry_ready == 1:
                    if pending_confirmation == 0:
                        pending_confirmation = 1
                        confirmation_start_index = i
                        confirmation_anchor_index = i
                        confirmation_anchor_price = closes[i]
                    elif closes[i] <= confirmation_anchor_price:
                        confirmation_start_index = i
                        confirmation_anchor_index = i
                        confirmation_anchor_price = closes[i]
                if pending_confirmation == 1:
                    if closes[i] < confirmation_anchor_price:
                        confirmation_anchor_index = i
                        confirmation_anchor_price = closes[i]
                    confirmation_window_expired = (
                        repair_confirmation_window > 0
                        and confirmation_start_index >= 0
                        and (i - confirmation_start_index) > repair_confirmation_window
                    )
                    if regime_ready == 0 or noise_ready_flag == 0 or confirmation_window_expired:
                        pending_confirmation = 0
                        confirmation_start_index = -1
                        confirmation_anchor_index = -1
                        confirmation_anchor_price = 0.0
                    else:
                        liquidity_ready = 1
                        if liquidity_confirmation_enabled == 1:
                            liquidity_ready = 1 if (
                                volume_ready[i] == 1
                                and volume_mean[i] > 0.0
                                and volumes[i] >= volume_mean[i] * liquidity_volume_floor_ratio
                            ) else 0
                        structure_ready = 1
                        if structure_confirmation_enabled == 1:
                            current_body_return = 0.0
                            if i >= 1 and closes[i - 1] > 0.0:
                                current_body_return = (closes[i] - closes[i - 1]) / closes[i - 1]
                            prior_min_close = closes[i]
                            for j in range(max(0, i - structure_window), i):
                                if closes[j] < prior_min_close:
                                    prior_min_close = closes[j]
                            close_location_proxy = 1.0 if (i >= 1 and closes[i] >= closes[i - 1]) else 0.0
                            structure_ready = 1 if (
                                close_location_proxy >= structure_close_location_min
                                and current_body_return >= structure_body_return_min
                                and closes[i] >= prior_min_close
                            ) else 0
                        rebound_ready = (
                            confirmation_anchor_price > 0.0
                            and i > confirmation_anchor_index
                            and i >= 1
                            and rsi[i] >= repair_confirmation_rsi_reclaim
                            and closes[i] >= closes[i - 1]
                            and (closes[i] - confirmation_anchor_price) / confirmation_anchor_price
                            >= repair_confirmation_rebound_pct
                            and liquidity_ready == 1
                            and structure_ready == 1
                        )
                        if rebound_ready:
                            entry_ready = 1
                            pending_confirmation = 0
                            confirmation_start_index = -1
                            confirmation_anchor_index = -1
                            confirmation_anchor_price = 0.0
            else:
                entry_ready = base_entry_ready
        regime_break = False
        mean_reversion_exit = False
        time_stop_exit = False
        adverse_regime_exit = False
        if shares > 0 and exit_ready[i] == 1 and exit_line[i] > 0:
            regime_break = closes[i] < exit_line[i] * (1.0 - regime_break_threshold) and closes[i] < closes[i - 1]
        if shares > 0:
            bars_held = i - entry_index if entry_index >= 0 else 0
            mean_reversion_exit = (
                bars_held >= mean_reversion_exit_min_hold_bars
                and deviation >= mean_reversion_exit_buffer
            )
            time_stop_exit = max_hold_bars > 0 and bars_held >= max_hold_bars
            adverse_regime_exit = (
                adverse_regime_exit_enabled == 1
                and bars_held >= 1
                and regime_code == 6
                and noise_ready[i] == 1
                and noise_ratio[i] >= adverse_noise_ceiling
                and closes[i] < closes[i - 1]
            )

        if entry_ready == 1 and shares == 0:
            buy_price = closes[i + 1] * (1 + total_cost_rate)
            max_shares = int(cash / buy_price)
            if max_shares > 0:
                cost = max_shares * buy_price
                shares = max_shares
                cash -= cost
                trades += 1
                entry_index = i

        elif shares > 0 and (
            rsi[i] > overbought
            or mean_reversion_exit
            or regime_break
            or time_stop_exit
            or adverse_regime_exit
        ):
            sell_price = closes[i + 1] * (1 - total_cost_rate)
            revenue = shares * sell_price
            profit = revenue - (shares * buy_price)
            if profit > 0:
                wins += 1
            cash += revenue
            shares = 0
            trades += 1
            entry_index = -1

        equity[i] = cash + shares * closes[i]

    equity[n - 1] = cash + shares * closes[n - 1]

    if shares > 0:
        sell_price = closes[-1] * (1 - total_cost_rate)
        revenue = shares * sell_price
        cash += revenue
        shares = 0
        trades += 1

    final_capital = cash
    total_return = (final_capital - initial_capital) / initial_capital

    max_dd = 0.0
    peak = equity[start_index]
    for i in range(start_index, n):
        if equity[i] > peak:
            peak = equity[i]
        dd = (peak - equity[i]) / peak if peak > 0 else 0
        if dd > max_dd:
            max_dd = dd

    returns = np.diff(equity[start_index:]) / equity[start_index:-1]
    valid_mask = equity[start_index:-1] > 0
    returns = returns[valid_mask]
    sharpe = 0.0
    if len(returns) > 0:
        mean_return = np.mean(returns)
        std_return = np.std(returns)
        if std_return > 0:
            annual_ret = mean_return * 252.0
            annual_std = std_return * np.sqrt(252.0)
            # Match mask/engine path: deduct 2% annual risk-free rate.
            sharpe = (annual_ret - 0.02) / annual_std

    win_rate = wins / max(1, trades // 2) if trades > 0 else 0.0  # trades计买+卖，round-trip = trades//2

    return final_capital, total_return, max_dd, sharpe, trades, win_rate, equity
