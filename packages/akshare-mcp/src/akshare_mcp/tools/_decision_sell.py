from ._decision_common import *

async def should_i_sell(
    code: str | None = None,
    buy_price: float = 0.0,
    holding_days: int = 0,
    stock_code: str | None = None,
    symbol: str | None = None,
    ticker: str | None = None,
):
    """
    卖出建议 - 综合止盈止损、技术信号、持仓时间分析

    Args:
        code: 股票代码
        buy_price: 买入价格
        holding_days: 持有天数
    """
    try:
        code = resolve_security_code(code, stock_code=stock_code, symbol=symbol, ticker=ticker)
        if not code:
            return fail('需要提供股票代码（支持 code / stock_code / symbol / ticker）')
        # buy_price <= 0 视为「未提供买入价」，降级为纯技术分析，不再直接拒绝
        has_buy_price = buy_price > 0
        db = get_db()

        # 1. 获取基础信息
        stock_info = await db.get_stock_info(code)
        if not stock_info:
            return fail(f'Stock {code} not found')

        # 2. 获取K线数据
        klines = await db.get_klines(code, limit=100)
        if not klines:
            return fail('No kline data')

        current_price = klines[-1]['close']
        closes = [k['close'] for k in klines]

        analysis_context = {}
        context_error = None
        try:
            context_result = await get_investment_analysis(code)
            if context_result.get('success'):
                analysis_context = context_result.get('data', {}) or {}
            else:
                context_error = context_result.get('error', 'unknown')
        except Exception as ctx_exc:
            context_error = str(ctx_exc)

        technical_ctx = _context_section(analysis_context, 'technical')
        risk_ctx = _context_section(analysis_context, 'risk')

        # 3. 计算盈亏（仅在提供买入价时）
        buy_price_warning = None
        if has_buy_price:
            profit_pct = (current_price - buy_price) / buy_price * 100
            profit_amount = current_price - buy_price
            # F-N22-7 fix (诊断报告 §N22): buy_price 合理性 sanity check。
            # 历史问题: should_i_sell(002594当前96, buy_price=1326) 算出 profit_pct=-92.75%
            # 并据此 sell，无任何告警（典型场景：把茅台价误填给比亚迪）。
            # 修复: buy_price 与当前价数量级严重不符时附告警（不拒绝，提示用户核对）。
            if current_price > 0:
                ratio = buy_price / current_price
                if ratio >= 5.0 or ratio <= 0.2:
                    buy_price_warning = (
                        f"buy_price={buy_price} 与当前价 {current_price} 数量级严重不符"
                        f"（相差 {ratio:.1f}x），请核对是否填错代码或买入价；"
                        f"基于此 buy_price 的盈亏({profit_pct:.1f}%)与止损位可能无意义"
                    )
        else:
            profit_pct = 0.0
            profit_amount = 0.0

        reasons = []
        risks = []
        score = 0  # 正分倾向卖出，负分倾向持有
        score_breakdown = {
            'profit_loss': 0.0,
            'technical': 0.0,
            'holding': 0.0,
            'risk': 0.0,
        }
        signal_breakdown = []

        def _apply_sell_signal(category: str, text: str, delta_score: float, *, source: str = 'fallback') -> None:
            nonlocal score
            if delta_score >= 0:
                reasons.append(text)
            else:
                risks.append(text)
            score += float(delta_score)
            score_breakdown[category] = float(score_breakdown.get(category, 0.0)) + float(delta_score)
            signal_breakdown.append({
                'category': category,
                'text': text,
                'delta_score': float(delta_score),
                'source': source,
            })

        # 4. 止盈止损分析（仅在提供买入价时）
        if has_buy_price:
            if profit_pct >= 30:
                _apply_sell_signal('profit_loss', f'盈利{profit_pct:.1f}%，建议止盈', 40, source='direct_profit_loss')
            elif profit_pct >= 20:
                _apply_sell_signal('profit_loss', f'盈利{profit_pct:.1f}%，可考虑部分止盈', 25, source='direct_profit_loss')
            elif profit_pct >= 10:
                _apply_sell_signal('profit_loss', f'盈利{profit_pct:.1f}%，持有为主', 5, source='direct_profit_loss')
            elif profit_pct <= -15:
                _apply_sell_signal('profit_loss', f'亏损{abs(profit_pct):.1f}%，建议止损', 35, source='direct_profit_loss')
            elif profit_pct <= -10:
                _apply_sell_signal('profit_loss', f'亏损{abs(profit_pct):.1f}%，考虑止损', 20, source='direct_profit_loss')
            elif profit_pct <= -5:
                _apply_sell_signal('profit_loss', f'亏损{abs(profit_pct):.1f}%，注意风险', 10, source='direct_profit_loss')

        # 5. 技术分析
        # RSI
        rsi_value = _maybe_float(technical_ctx.get('rsi_14'))
        rsi_source = 'analysis_context' if rsi_value is not None else 'fallback'
        if rsi_value is None:
            rsi_result = technical_analysis.calculate_rsi(closes)
            if rsi_result:
                rsi_value = rsi_result[-1] if isinstance(rsi_result, list) else rsi_result.get('value', 50)
        if rsi_value is not None:
            if rsi_value > 80:
                _apply_sell_signal('technical', f'RSI严重超买({rsi_value:.1f})，建议卖出', 25, source=rsi_source)
            elif rsi_value > 70:
                _apply_sell_signal('technical', f'RSI超买({rsi_value:.1f})，考虑减仓', 15, source=rsi_source)
            elif rsi_value < 30:
                _apply_sell_signal('technical', f'RSI超卖({rsi_value:.1f})，可能反弹', -15, source=rsi_source)

        # MACD
        macd_result = technical_analysis.calculate_macd(closes)
        if macd_result and 'histogram' in macd_result:
            hist = macd_result['histogram']
            if len(hist) >= 2:
                if hist[-2] > 0 and hist[-1] < 0:
                    _apply_sell_signal('technical', 'MACD死叉，卖出信号', 20, source='fallback')
                elif hist[-2] < 0 and hist[-1] > 0:
                    _apply_sell_signal('technical', 'MACD金叉，买入信号', -20, source='fallback')

        # 均线
        ma_data = technical_ctx.get('moving_averages', {}) if isinstance(technical_ctx.get('moving_averages'), dict) else {}
        ma20_last = _maybe_float(ma_data.get('ma20'))
        ma60_last = _maybe_float(ma_data.get('ma60'))
        if ma20_last is None or ma60_last is None:
            ma20 = technical_analysis.calculate_sma(closes, 20)
            ma60 = technical_analysis.calculate_sma(closes, 60)
            if ma20 and ma60 and len(ma20) > 0 and len(ma60) > 0:
                ma20_last = _maybe_float(ma20[-1])
                ma60_last = _maybe_float(ma60[-1])
        if ma20_last is not None and ma60_last is not None:
            latest_close = float(closes[-1])
            ma_source = 'analysis_context' if isinstance(technical_ctx.get('moving_averages'), dict) else 'fallback'
            if latest_close < ma20_last < ma60_last:
                _apply_sell_signal('technical', '跌破均线，趋势转弱', 20, source=ma_source)
            elif latest_close > ma20_last > ma60_last:
                _apply_sell_signal('technical', '多头排列，趋势向上', -15, source=ma_source)

        # 5b. 布林带位置
        highs = [float(k.get('high', 0) or 0) for k in klines]
        lows_list = [float(k.get('low', 0) or 0) for k in klines]
        boll = technical_analysis.calculate_bollinger_bands(closes)
        if boll:
            boll_upper = boll.get('upper', [])
            boll_lower = boll.get('lower', [])
            boll_upper_val = float(boll_upper[-1]) if boll_upper and boll_upper[-1] else None
            boll_lower_val = float(boll_lower[-1]) if boll_lower and boll_lower[-1] else None
            if boll_upper_val and current_price >= boll_upper_val * 0.99:
                _apply_sell_signal('technical', f'触及布林上轨({boll_upper_val:.1f})，短期超买', 10, source='boll')
            elif boll_lower_val and current_price <= boll_lower_val * 1.01:
                _apply_sell_signal('technical', f'触及布林下轨({boll_lower_val:.1f})，可能反弹', -10, source='boll')

        # 5c. ATR 动态止损评估
        atr_series = technical_analysis.calculate_atr(highs, lows_list, closes, period=14)
        atr_stop_price = None
        if atr_series and has_buy_price:
            atr_val = float(atr_series[-1]) if atr_series[-1] else 0
            if atr_val > 0:
                atr_stop_price = round(buy_price - atr_val * 2, 2)
                if current_price < atr_stop_price:
                    _apply_sell_signal('risk', f'已跌破 ATR 止损位({atr_stop_price:.1f})，建议止损', 30, source='atr_stop')

        # 5d. 支撑/阻力位
        support_60d = min(lows_list[-60:]) if len(lows_list) >= 60 else None
        resistance_60d = max(highs[-60:]) if len(highs) >= 60 else None
        if support_60d and current_price < support_60d:
            _apply_sell_signal('risk', f'已跌破 60 日支撑位({support_60d:.1f})', 15, source='key_level')
        if resistance_60d and current_price >= resistance_60d * 0.98:
            _apply_sell_signal('technical', f'接近 60 日阻力位({resistance_60d:.1f})，上行空间有限', 10, source='key_level')

        # 6. 持仓时间分析
        if holding_days > 0:
            if holding_days < 7:
                _apply_sell_signal('holding', f'持仓仅{holding_days}天，建议再观察', -10, source='direct_holding')
            elif holding_days > 180:
                if not has_buy_price or profit_pct < 5:
                    _apply_sell_signal('holding', f'持仓{holding_days}天收益不佳，考虑换股', 15, source='direct_holding')

        # 7. 波动风险
        volatility = _maybe_float(risk_ctx.get('volatility_20d'))
        if volatility is None:
            recent_window = closes[-21:] if len(closes) >= 21 else closes
            returns = [
                (recent_window[i + 1] - recent_window[i]) / recent_window[i]
                for i in range(max(len(recent_window) - 1, 0))
                if recent_window[i] > 0
            ]
            if len(returns) > 1:
                volatility = statistics.stdev(returns)
        if volatility is not None and volatility > 0.04:
            _apply_sell_signal('risk', '近期波动较大，注意风险', 10, source='analysis_context' if risk_ctx else 'fallback')

        # 8. 生成建议
        if score >= 40:
            recommendation = 'sell'
            action_text = '强烈建议卖出'
        elif score >= 25:
            recommendation = 'reduce'
            action_text = '建议减仓'
        elif score >= 10:
            recommendation = 'consider_sell'
            action_text = '可考虑卖出'
        elif score >= -10:
            recommendation = 'hold'
            action_text = '继续持有'
        else:
            recommendation = 'strong_hold'
            action_text = '坚定持有'

        # 9. 目标卖出价（如果建议卖出）
        target_sell_price = None
        if recommendation in ['sell', 'reduce']:
            if not has_buy_price or profit_pct > 0:
                target_sell_price = current_price
            elif atr_stop_price and atr_stop_price > 0:
                target_sell_price = atr_stop_price
            else:
                target_sell_price = buy_price * 0.95

        payload = {
            'code': code,
            'name': stock_info.get('name', ''),
            'recommendation': recommendation,
            'decision_mode': 'hybrid_score_plus_context',
            'action_text': action_text,
            'score': score,
            'current_price': current_price,
            'buy_price': buy_price if has_buy_price else None,
            'profit_pct': round(profit_pct, 2) if has_buy_price else None,
            'profit_amount': round(profit_amount, 2) if has_buy_price else None,
            'holding_days': holding_days if holding_days > 0 else None,
            'target_sell_price': round(target_sell_price, 2) if target_sell_price else None,
            'reasons': reasons,
            'risks': risks,
            'score_breakdown': {k: round(float(v), 2) for k, v in score_breakdown.items()},
            'signal_breakdown': signal_breakdown,
            'analysis_date': klines[-1].get('date', ''),
            'failed_modules': ([f"investment_analysis:{context_error}"] if context_error else []),
            'analysis_mode': 'technical_only' if not has_buy_price else 'full',
        }
        if buy_price_warning:
            payload['buy_price_warning'] = buy_price_warning

        if analysis_context:
            payload['analysis_context'] = analysis_context

        return ok(payload)

    except Exception as e:
        return fail(str(e))
