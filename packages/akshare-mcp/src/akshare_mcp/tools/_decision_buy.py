from ._decision_common import *

async def should_i_buy(
    code: str | None = None,
    investment_style: str = 'balanced',
    as_of: str = '',
    adjust: str = '',
    price_source_policy: str = 'auto',
    explain: bool = True,
    strict_mode: bool = False,
    stock_code: str | None = None,
    symbol: str | None = None,
    ticker: str | None = None,
):
    """
    买入建议 - 综合估值、技术、基本面、因子分析

    Args:
        code: 股票代码
        investment_style: 投资风格 ('aggressive'激进, 'balanced'平衡, 'conservative'保守)
        as_of: 分析时点（可选）
        adjust: 复权口径（可选）
        price_source_policy: 价格源策略（可选）
        explain: 是否返回证据链
        strict_mode: 严格模式（空值严格处理）
    """
    start_time = time.perf_counter()
    code = resolve_security_code(code, stock_code=stock_code, symbol=symbol, ticker=ticker)
    if not code:
        return fail('需要提供股票代码（支持 code / stock_code / symbol / ticker）')
    # F-N22-5 fix (诊断报告 §N22): investment_style 枚举校验。
    # 历史问题: 'bogus_style_zzz' 被静默接受并回退 balanced，无任何告警。
    _VALID_STYLES = {'aggressive', 'balanced', 'conservative'}
    _style_warning = None
    _requested_style = str(investment_style or '').strip().lower()
    if _requested_style not in _VALID_STYLES:
        _style_warning = (
            f"investment_style='{investment_style}' 非法（支持 {sorted(_VALID_STYLES)}），"
            f"已回退 balanced"
        )
        investment_style = 'balanced'
    else:
        investment_style = _requested_style
    trace_id = f"should_i_buy:{code}:{int(time.time() * 1000)}"
    evidence_chain = None
    try:
        evidence_chain = create_chain(
            trace_id=trace_id,
            code=code,
            action="should_i_buy",
            tool_version="v1.1.0",
            extra={
                "investment_style": investment_style,
                "as_of": as_of,
                "adjust": adjust,
                "price_source_policy": price_source_policy,
                "strict_mode": strict_mode,
            },
        )
    except Exception:
        evidence_chain = None

    def _record_evidence(
        evidence_type: str,
        metric_name: str,
        raw_value,
        delta_score: float,
        *,
        confidence_hint: float | None = None,
        detail: dict | None = None,
    ) -> None:
        nonlocal evidence_chain
        if evidence_chain is None:
            return
        try:
            ev = make_evidence(
                evidence_type=evidence_type,
                source_module="decision.should_i_buy",
                metric_name=metric_name,
                raw_value=raw_value,
                score=float(max(0.0, min(100.0, 50.0 + delta_score))),
                weight=1.0,
                score_contribution=float(delta_score),
                confidence=confidence_hint,
                detail=detail or {},
            )
            evidence_chain = add_evidence(evidence_chain, ev)
        except Exception:
            pass

    def _with_meta(resp: dict, source_chain: list | None = None) -> dict:
        resp['meta'] = {
            'trace_id': trace_id,
            'tool_version': 'v1.1.0',
            'data_timestamp': None,
            'source_chain': source_chain or ['db', 'technical_analysis', 'factor_calculator'],
            'cached': bool(resp.get('cached', False)),
            'latency_ms': int((time.perf_counter() - start_time) * 1000),
            'as_of': as_of,
            'adjust': adjust,
            'price_source_policy': price_source_policy,
            'strict_mode': strict_mode,
        }
        return resp

    def _ok(data: dict, source_chain: list | None = None) -> dict:
        return _with_meta(ok(data), source_chain)

    def _fail(message: str, source_chain: list | None = None) -> dict:
        return _with_meta(fail(message), source_chain)

    try:
        db = get_db()

        # 1. 获取基础信息
        stock_info = await db.get_stock_info(code)
        if not stock_info:
            return _fail(f'Stock {code} not found')

        # 2. 获取K线数据
        klines = await db.get_klines(code, limit=100)
        klines, pit_guard = _filter_klines_by_as_of(klines, as_of)
        if not klines or len(klines) < 20:
            return _fail('Insufficient kline data')

        closes = [k['close'] for k in klines]
        volumes = [k['volume'] for k in klines]
        latest_kline = max(
            klines,
            key=lambda item: str(item.get('date') or item.get('trade_date') or item.get('timestamp') or ''),
        )
        analysis_date = (
            latest_kline.get('date')
            or latest_kline.get('trade_date')
            or latest_kline.get('timestamp')
            or ''
        )
        current_price = float(latest_kline.get('close') or closes[-1])
        time_precision = 'historical_eod_close_as_of' if pit_guard.get('active') else 'historical_eod_close'

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

        valuation_ctx = _context_section(analysis_context, 'valuation')
        fundamentals_ctx = _context_section(analysis_context, 'fundamentals')
        technical_ctx = _context_section(analysis_context, 'technical')
        momentum_ctx = _context_section(analysis_context, 'momentum')

        reasons = []
        risks = []
        score = 0
        confidence = 0
        score_breakdown = {
            'valuation': 0.0,
            'technical': 0.0,
            'fundamental': 0.0,
            'factor': 0.0,
        }
        signal_breakdown = []

        def _apply_signal(
            category: str,
            text: str,
            delta_score: float,
            *,
            confidence_delta: float = 0.0,
            source: str = 'fallback',
            metric_name: str | None = None,
            raw_value=None,
            evidence_type: str | None = None,
            confidence_hint: float | None = None,
            detail: dict | None = None,
        ) -> None:
            nonlocal score, confidence
            if delta_score >= 0:
                reasons.append(text)
            else:
                risks.append(text)
            score += float(delta_score)
            confidence += float(confidence_delta)
            score_breakdown[category] = float(score_breakdown.get(category, 0.0)) + float(delta_score)
            signal_breakdown.append({
                'category': category,
                'text': text,
                'delta_score': float(delta_score),
                'confidence_delta': float(confidence_delta),
                'source': source,
            })
            if evidence_type and metric_name is not None:
                _record_evidence(
                    evidence_type,
                    metric_name,
                    raw_value,
                    float(delta_score),
                    confidence_hint=confidence_hint,
                    detail=detail,
                )

        # 3. 估值分析（从数据库直接查询）
        pe = _maybe_float(valuation_ctx.get('pe'))
        pb = _maybe_float(valuation_ctx.get('pb'))
        valuation_source = 'analysis_context' if (pe is not None or pb is not None) else 'fallback'
        if pe is None or pb is None:
            try:
                async with db.acquire() as conn:
                    valuation_row = await conn.fetchrow(
                        """SELECT pe_ratio, pb_ratio FROM stocks WHERE code = $1""",
                        code
                    )
                    if pe is None:
                        pe = _maybe_float(valuation_row['pe_ratio']) if valuation_row else None
                    if pb is None:
                        pb = _maybe_float(valuation_row['pb_ratio']) if valuation_row else None
            except Exception:
                pass

        if pe and 0 < pe < 15:
            _apply_signal(
                'valuation',
                f'估值偏低(PE={pe:.1f})',
                25,
                confidence_delta=15,
                source=valuation_source,
                metric_name='pe_ratio',
                raw_value=pe,
                evidence_type='valuation',
                confidence_hint=0.75,
            )
        elif pe and 15 <= pe < 30:
            _apply_signal(
                'valuation',
                f'估值合理(PE={pe:.1f})',
                15,
                confidence_delta=10,
                source=valuation_source,
                metric_name='pe_ratio',
                raw_value=pe,
                evidence_type='valuation',
                confidence_hint=0.65,
            )
        elif pe and pe >= 50:
            _apply_signal(
                'valuation',
                f'估值偏高(PE={pe:.1f})',
                -15,
                source=valuation_source,
                metric_name='pe_ratio',
                raw_value=pe,
                evidence_type='valuation',
                confidence_hint=0.70,
            )

        if pb and 0 < pb < 2:
            _apply_signal(
                'valuation',
                f'市净率偏低(PB={pb:.1f})',
                20,
                confidence_delta=10,
                source=valuation_source,
                metric_name='pb_ratio',
                raw_value=pb,
                evidence_type='valuation',
                confidence_hint=0.7,
            )
        elif pb and pb > 5:
            _apply_signal(
                'valuation',
                f'市净率偏高(PB={pb:.1f})',
                -10,
                source=valuation_source,
                metric_name='pb_ratio',
                raw_value=pb,
                evidence_type='valuation',
                confidence_hint=0.65,
            )

        # 4. 技术分析
        # RSI
        rsi_value = _maybe_float(technical_ctx.get('rsi_14'))
        rsi_source = 'analysis_context' if rsi_value is not None else 'fallback'
        if rsi_value is None:
            rsi_result = technical_analysis.calculate_rsi(closes)
            if rsi_result:
                rsi_value = rsi_result[-1] if isinstance(rsi_result, list) else rsi_result.get('value', 50)
        if rsi_value is not None:
            if rsi_value < 30:
                _apply_signal(
                    'technical',
                    f'RSI超卖({rsi_value:.1f})，可能反弹',
                    20,
                    confidence_delta=15,
                    source=rsi_source,
                    metric_name='rsi',
                    raw_value=float(rsi_value),
                    evidence_type='technical',
                    confidence_hint=0.75,
                )
            elif rsi_value > 70:
                _apply_signal(
                    'technical',
                    f'RSI超买({rsi_value:.1f})，短期风险',
                    -15,
                    source=rsi_source,
                    metric_name='rsi',
                    raw_value=float(rsi_value),
                    evidence_type='technical',
                    confidence_hint=0.70,
                )

        # MACD
        macd_result = technical_analysis.calculate_macd(closes)
        if macd_result and 'histogram' in macd_result:
            hist = macd_result['histogram']
            if len(hist) >= 2:
                if hist[-2] < 0 and hist[-1] > 0:
                    _apply_signal(
                        'technical',
                        'MACD金叉，买入信号',
                        25,
                        confidence_delta=20,
                        source='fallback',
                        metric_name='macd_histogram',
                        raw_value=float(hist[-1]),
                        evidence_type='technical',
                        confidence_hint=0.8,
                    )
                elif hist[-2] > 0 and hist[-1] < 0:
                    _apply_signal(
                        'technical',
                        'MACD死叉，卖出信号',
                        -20,
                        source='fallback',
                        metric_name='macd_histogram',
                        raw_value=float(hist[-1]),
                        evidence_type='technical',
                        confidence_hint=0.78,
                    )

        # 均线趋势
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
            if latest_close > ma20_last > ma60_last:
                _apply_signal(
                    'technical',
                    '多头排列，趋势向上',
                    20,
                    confidence_delta=15,
                    source=ma_source,
                    metric_name='ma_trend',
                    raw_value={"close": latest_close, "ma20": float(ma20_last), "ma60": float(ma60_last)},
                    evidence_type='technical',
                    confidence_hint=0.75,
                )
            elif latest_close < ma20_last < ma60_last:
                _apply_signal(
                    'technical',
                    '空头排列，趋势向下',
                    -20,
                    source=ma_source,
                    metric_name='ma_trend',
                    raw_value={"close": latest_close, "ma20": float(ma20_last), "ma60": float(ma60_last)},
                    evidence_type='technical',
                    confidence_hint=0.75,
                )

        # 成交量
        recent_vol = statistics.mean(volumes[-5:])
        avg_vol = statistics.mean(volumes)
        if recent_vol > avg_vol * 1.5:
            _apply_signal(
                'technical',
                '成交量放大，资金关注',
                15,
                confidence_delta=10,
                source='fallback',
                metric_name='volume_ratio',
                raw_value=float(recent_vol / avg_vol) if avg_vol else 0.0,
                evidence_type='technical',
                confidence_hint=0.6,
            )

        # 5. 基本面分析
        roe = _maybe_float(fundamentals_ctx.get('roe'))
        debt_ratio = _maybe_float(fundamentals_ctx.get('debt_ratio'))
        revenue_growth = _maybe_float(fundamentals_ctx.get('revenue_yoy'))
        if roe is None and debt_ratio is None and revenue_growth is None:
            try:
                async with db.acquire() as conn:
                    f_code_col = await db._financials_code_column(conn)
                    financial_row = await conn.fetchrow(
                        f"""SELECT roe, debt_ratio, revenue_growth
                           FROM financials
                           WHERE {f_code_col} = $1
                           ORDER BY report_date DESC
                           LIMIT 1""",
                        code
                    )
                    if financial_row:
                        roe = _maybe_float(financial_row['roe'])
                        debt_ratio = _maybe_float(financial_row['debt_ratio'])
                        revenue_growth = _maybe_float(financial_row['revenue_growth'])
            except Exception:
                pass

        if roe and roe > 15:
            _apply_signal(
                'fundamental',
                f'ROE优秀({roe:.1f}%)',
                20,
                confidence_delta=10,
                source='analysis_context' if fundamentals_ctx else 'fallback',
                metric_name='roe',
                raw_value=roe,
                evidence_type='fundamental',
                confidence_hint=0.72,
            )
        elif roe and roe > 10:
            _apply_signal(
                'fundamental',
                f'ROE良好({roe:.1f}%)',
                10,
                source='analysis_context' if fundamentals_ctx else 'fallback',
                metric_name='roe',
                raw_value=roe,
                evidence_type='fundamental',
                confidence_hint=0.65,
            )

        if debt_ratio and debt_ratio > 70:
            _apply_signal(
                'fundamental',
                f'负债率较高({debt_ratio:.1f}%)',
                -10,
                source='analysis_context' if fundamentals_ctx else 'fallback',
                metric_name='debt_ratio',
                raw_value=debt_ratio,
                evidence_type='fundamental',
                confidence_hint=0.68,
            )

        if revenue_growth and revenue_growth > 20:
            _apply_signal(
                'fundamental',
                f'营收高增长({revenue_growth:.1f}%)',
                20,
                confidence_delta=15,
                source='analysis_context' if fundamentals_ctx else 'fallback',
                metric_name='revenue_growth',
                raw_value=revenue_growth,
                evidence_type='fundamental',
                confidence_hint=0.75,
            )

        # 5b. 布林带位置
        highs = [float(k.get('high', 0) or 0) for k in klines]
        lows = [float(k.get('low', 0) or 0) for k in klines]
        boll = technical_analysis.calculate_bollinger_bands(closes)
        if boll:
            boll_upper = _maybe_float(boll.get('upper', [None])[-1] if isinstance(boll.get('upper'), list) else None)
            boll_lower = _maybe_float(boll.get('lower', [None])[-1] if isinstance(boll.get('lower'), list) else None)
            if boll_lower and current_price <= boll_lower * 1.01:
                _apply_signal(
                    'technical', f'触及布林下轨({boll_lower:.1f})，超卖信号',
                    10, source='fallback', metric_name='boll_position', raw_value=current_price,
                    evidence_type='technical', confidence_hint=0.65,
                )
            elif boll_upper and current_price >= boll_upper * 0.99:
                _apply_signal(
                    'technical', f'触及布林上轨({boll_upper:.1f})，超买信号',
                    -10, source='fallback', metric_name='boll_position', raw_value=current_price,
                    evidence_type='technical', confidence_hint=0.65,
                )

        # 5c. ATR 波动率风险
        atr_series = technical_analysis.calculate_atr(highs, lows, closes, period=14)
        if atr_series:
            atr_val = _maybe_float(atr_series[-1])
            if atr_val and current_price > 0:
                atr_pct = atr_val / current_price
                if atr_pct > 0.05:
                    _apply_signal(
                        'technical', f'ATR 波动率偏高({atr_pct:.1%})，风险较大',
                        -10, source='fallback', metric_name='atr_pct', raw_value=float(atr_pct),
                        evidence_type='technical', confidence_hint=0.60,
                    )

        # 5d. 支撑/阻力位
        support_60d = _maybe_float(technical_ctx.get('support_60d'))
        resistance_60d = _maybe_float(technical_ctx.get('resistance_60d'))
        if support_60d is None and len(lows) >= 60:
            support_60d = min(lows[-60:])
        if resistance_60d is None and len(highs) >= 60:
            resistance_60d = max(highs[-60:])
        if support_60d and current_price > 0:
            dist_to_support = (current_price - support_60d) / current_price
            if dist_to_support < 0.03:
                _apply_signal(
                    'technical', f'接近 60 日支撑位({support_60d:.1f})，安全边际较高',
                    15, confidence_delta=5, source='key_level',
                    metric_name='support_distance', raw_value=float(dist_to_support),
                    evidence_type='technical', confidence_hint=0.68,
                )
            elif dist_to_support < 0:
                _apply_signal(
                    'technical', f'已跌破 60 日支撑位({support_60d:.1f})，风险增加',
                    -10, source='key_level',
                    metric_name='support_distance', raw_value=float(dist_to_support),
                    evidence_type='technical', confidence_hint=0.70,
                )
        if resistance_60d and current_price > 0:
            dist_to_resistance = (resistance_60d - current_price) / current_price
            if dist_to_resistance < 0.03:
                _apply_signal(
                    'technical', f'接近 60 日阻力位({resistance_60d:.1f})，上涨空间有限',
                    -15, source='key_level',
                    metric_name='resistance_distance', raw_value=float(dist_to_resistance),
                    evidence_type='technical', confidence_hint=0.65,
                )

        # 6. 因子分析
        momentum = _maybe_float(momentum_ctx.get('mom_20d'))
        if momentum is None:
            momentum = _maybe_float(momentum_ctx.get('mom_10d'))
        if momentum is None:
            try:
                momentum = factor_calculator.calculate_momentum(closes)
            except Exception:
                momentum = None
        if momentum is not None:
            if momentum > 0.1:
                _apply_signal(
                    'factor',
                    '动量因子强势',
                    15,
                    source='analysis_context' if momentum_ctx else 'fallback',
                    metric_name='momentum',
                    raw_value=float(momentum),
                    evidence_type='factor',
                    confidence_hint=0.62,
                )
            elif momentum < -0.1:
                _apply_signal(
                    'factor',
                    '动量因子弱势',
                    -10,
                    source='analysis_context' if momentum_ctx else 'fallback',
                    metric_name='momentum',
                    raw_value=float(momentum),
                    evidence_type='factor',
                    confidence_hint=0.62,
                )

        confidence = _clamp(confidence, 0.0, 100.0)

        # 7. 根据投资风格调整
        style_thresholds = {
            'aggressive': {'buy': 40, 'confidence': 50},
            'balanced': {'buy': 60, 'confidence': 60},
            'conservative': {'buy': 80, 'confidence': 70}
        }

        threshold = style_thresholds.get(investment_style, style_thresholds['balanced'])

        context_decision = _derive_contextual_decision(analysis_context) if analysis_context else None

        # 8. 生成建议
        if score >= threshold['buy'] and confidence >= threshold['confidence']:
            recommendation = 'buy'
            action_text = '建议买入'
        elif score >= threshold['buy'] * 0.7:
            recommendation = 'hold'
            action_text = '可以持有或小仓位试探'
        elif score >= 0:
            recommendation = 'wait'
            action_text = '建议观望'
        else:
            recommendation = 'avoid'
            action_text = '建议回避'

        # F-N22-3 fix (诊断报告 §N22): score→recommendation 映射保持单调。
        # 历史问题: context_decision 可把 score=60 的 hold 一次性翻到 avoid，
        # 而 score=45 仍是 hold，出现「高分反而更悲观」的非单调割裂。
        # 修复: 限定 context 覆盖最多**下调一档**（hold→wait, wait→avoid），
        # 不允许 hold 直接跳到 avoid；保留 context 的下调能力但维持单调性。
        _REC_ORDER = {'avoid': 0, 'wait': 1, 'hold': 2, 'buy': 3}
        if context_decision and recommendation in {'hold', 'wait'}:
            ctx_rec = context_decision["recommendation"]
            base_level = _REC_ORDER.get(recommendation, 1)
            ctx_level = _REC_ORDER.get(ctx_rec, base_level)
            # 仅允许在 [base-1, base+1] 范围内调整，禁止 hold→avoid 的跨级跳变
            bounded_level = max(base_level - 1, min(base_level + 1, ctx_level))
            if bounded_level != base_level:
                bounded_rec = next(
                    (name for name, lvl in _REC_ORDER.items() if lvl == bounded_level),
                    recommendation,
                )
                recommendation = bounded_rec
                if bounded_rec == ctx_rec:
                    action_text = context_decision["action_text"]
                else:
                    action_text = {
                        'avoid': '建议回避',
                        'wait': '建议观望',
                        'hold': '可以持有或小仓位试探',
                        'buy': '建议买入',
                    }.get(bounded_rec, action_text)
            for item in context_decision["positives"]:
                if item not in reasons:
                    reasons.append(item)
            for item in context_decision["negatives"]:
                if item not in risks:
                    risks.append(item)

        # 9. 目标价位（优先行业中位数PE估值法）
        industry_peer_pes: list[float] = []
        industry_name = stock_info.get('industry') or stock_info.get('industry_name') or ''
        if industry_name:
            try:
                async with db.acquire() as conn:
                    peer_rows = await conn.fetch(
                        """SELECT pe_ratio FROM stocks
                           WHERE industry = $1
                             AND code <> $2
                             AND pe_ratio IS NOT NULL""",
                        industry_name,
                        code,
                    )
                industry_peer_pes = [float(r['pe_ratio']) for r in peer_rows if r.get('pe_ratio')]
            except Exception:
                industry_peer_pes = []

        target_price, valuation_method, industry_median_pe = _estimate_target_price(
            recommendation=recommendation,
            current_price=float(current_price),
            pe=float(pe) if pe else 0.0,
            score=float(score),
            industry_peer_pes=industry_peer_pes,
        )

        confidence = max(0, min(100, confidence))
        volatility_20d = _estimate_volatility(closes, window=20)
        buy_probability = _calibrate_buy_probability(
            score=float(score),
            confidence=float(confidence),
            style=investment_style,
            volatility=float(volatility_20d),
        )
        probability_band = (
            "high" if buy_probability >= 0.7 else ("medium" if buy_probability >= 0.45 else "low")
        )

        threshold_backtest = _build_threshold_backtest(
            closes=closes,
            thresholds=[40, 60, 80],
            horizon=10,
        )
        prediction_quality = _build_probability_quality(
            current_score=float(score),
            buy_probability=float(buy_probability),
            selected_threshold=int(threshold['buy']),
            threshold_backtest=threshold_backtest,
        )
        prediction_interval = _build_prediction_interval(
            closes=closes,
            current_score=float(score),
            thresholds=[40, 60, 80],
            horizon=10,
            confidence=0.8,
        )

        payload = {
            'code': code,
            'name': stock_info.get('name', ''),
            'recommendation': recommendation,
            'decision_mode': 'context_guided_hybrid' if analysis_context else 'hybrid_score_plus_context',
            'action_text': action_text,
            'score': score,
            'confidence': round(confidence, 1),
            'current_price': current_price,
            'target_price': round(target_price, 2) if target_price else None,
            'valuation_method': valuation_method,
            'industry_median_pe': round(industry_median_pe, 2) if industry_median_pe else None,
            'reasons': reasons,
            'risks': risks,
            'score_breakdown': {k: round(float(v), 2) for k, v in score_breakdown.items()},
            'signal_breakdown': signal_breakdown,
            'investment_style': investment_style,
            'analysis_date': analysis_date,
            'time_precision': time_precision,
            'price_basis': 'kline_latest_close',
            'pit_guard': pit_guard,
            'failed_modules': ([f"investment_analysis:{context_error}"] if context_error else []),
            'decision_probability': {
                'buy_probability': round(float(buy_probability), 4),
                'buy_probability_pct': f"{buy_probability * 100:.2f}%",
                'band': probability_band,
                'method': 'logit(score,confidence,volatility)',
            },
            'prediction_quality': prediction_quality,
            'probability_calibration': {
                'thresholds': style_thresholds,
                'selected_style_threshold': threshold,
                'volatility_20d': round(float(volatility_20d), 6),
                'threshold_backtest': {
                    'horizon_days': 10,
                    'records': threshold_backtest,
                },
            },
        }
        if prediction_interval is not None:
            payload['prediction_interval'] = prediction_interval

        # 离线决策评估基线（benchmark_delta = hit_rate - historical_avg）
        empirical_hit_rate = prediction_quality.get('empirical_hit_rate')
        historical_positive_rate = 0.45  # A 股经验基准：约 45% 的情况下 5d 收益 > 0
        benchmark_delta = (
            round(float(empirical_hit_rate) - historical_positive_rate, 4)
            if empirical_hit_rate is not None else None
        )
        payload['offline_decision_baseline'] = {
            'recommendation': recommendation,
            'hit_rate': empirical_hit_rate,
            'benchmark_delta': benchmark_delta,
            'historical_positive_rate': historical_positive_rate,
            'method': 'decision_threshold_bucket_backtest_proxy',
            'support_samples': prediction_quality.get('support_samples', 0),
        }

        # F-N22-1 fix (诊断报告 §N22): decision_probability 失校准修正。
        # 历史问题: logit(score,confidence,volatility) 产出的 buy_probability 与历史实证胜率
        # 严重背离（ECE 0.5-0.75），把 hit_rate 50-75% 的标的标为 buy_probability<1%。
        # 修复: 当 prediction_quality 暴露大 calibration_gap 且样本充足时，
        # 用历史经验命中率对 buy_probability 做收缩混合（empirical shrinkage），
        # 并显式标注 calibrated=true + reliability，避免输出明显失真的概率。
        _ece = prediction_quality.get('ece')
        _support = int(prediction_quality.get('support_samples') or 0)
        _calibration_applied = False
        if (
            empirical_hit_rate is not None
            and _ece is not None
            and _support >= 15
            and abs(float(_ece)) > 0.15
        ):
            # 样本越多越信任历史命中率；混合权重随样本量上升
            w_emp = min(0.7, 0.3 + _support / 200.0)
            calibrated_prob = (1.0 - w_emp) * float(buy_probability) + w_emp * float(empirical_hit_rate)
            payload['decision_probability']['raw_buy_probability'] = round(float(buy_probability), 4)
            payload['decision_probability']['buy_probability'] = round(float(calibrated_prob), 4)
            payload['decision_probability']['buy_probability_pct'] = f"{calibrated_prob * 100:.2f}%"
            payload['decision_probability']['band'] = (
                "high" if calibrated_prob >= 0.7 else ("medium" if calibrated_prob >= 0.45 else "low")
            )
            payload['decision_probability']['method'] = 'empirical_shrinkage(logit, historical_hit_rate)'
            payload['decision_probability']['calibrated'] = True
            payload['decision_probability']['calibration_weight_empirical'] = round(w_emp, 3)
            payload['decision_probability']['reliability'] = 'medium' if _support >= 30 else 'low'
            _calibration_applied = True
        else:
            payload['decision_probability']['calibrated'] = False
            payload['decision_probability']['reliability'] = (
                'high' if _support >= 30 and _ece is not None and abs(float(_ece)) <= 0.08
                else 'medium' if _support >= 15
                else 'low'
            )

        # F-N22-2 fix (诊断报告 §N22): recommendation 与 offline_baseline 一致性校验。
        # 历史问题: recommendation='avoid' 但 benchmark_delta=+0.328/hit_rate=0.778，
        # 'avoid' 与历史正收益证据直接矛盾，AI 无法判断信谁。
        # 修复: 检出矛盾时附 decision_consistency 块显式告警（不强行翻转决策，保留打分体系）。
        consistency_warnings: list[str] = []
        if _style_warning:
            consistency_warnings.append(_style_warning)
        if (
            recommendation in {'avoid', 'wait'}
            and benchmark_delta is not None
            and float(benchmark_delta) > 0.1
            and _support >= 15
        ):
            consistency_warnings.append(
                f"决策为 '{recommendation}' 但历史回测显示正向超额收益"
                f"（benchmark_delta=+{benchmark_delta}, hit_rate={empirical_hit_rate}, "
                f"support={_support}）；评分体系与历史实证存在矛盾，建议人工复核"
            )
        # threshold_inversion 透传到一致性块
        _inversion = any(
            'threshold_inversion' in str(w)
            for row in (threshold_backtest or [])
            for w in (row.get('warnings') or [])
        ) if threshold_backtest else False
        payload['decision_consistency'] = {
            'consistent': not consistency_warnings and not _inversion,
            'warnings': consistency_warnings,
            'threshold_inversion_detected': _inversion,
            'probability_recalibrated': _calibration_applied,
        }

        # 数据新鲜度检查
        data_freshness_warning = None
        if analysis_date:
            try:
                from datetime import datetime as _dt, timedelta
                date_str = str(analysis_date)[:10]
                if len(date_str) >= 10:
                    ad = _dt.strptime(date_str, '%Y-%m-%d')
                    days_old = (_dt.now() - ad).days
                    if days_old > 90:
                        data_freshness_warning = (
                            f"K线/基本面数据截至 {date_str}，距今 {days_old} 天，"
                            f"估值和基本面指标可能已过时，请结合最新财报使用"
                        )
            except Exception:
                pass
        payload['data_freshness_warning'] = data_freshness_warning

        if analysis_context:
            payload['analysis_context'] = analysis_context

        if evidence_chain is not None:
            try:
                confidence_ratio = (
                    buy_probability if recommendation in {'buy', 'hold'} else (1.0 - buy_probability)
                )
                confidence_ratio = max(0.0, min(1.0, confidence_ratio))
                evidence_chain = set_conclusion(
                    evidence_chain,
                    recommendation=recommendation,
                    total_score=float(score),
                    raw_total_score=float(score),
                    reason=action_text,
                    confidence=confidence_ratio,
                    data_quality={
                        "kline_size": len(closes),
                        "has_financial_row": bool('financial_row' in locals() and financial_row),
                        "strict_mode": bool(strict_mode),
                    },
                )
                saved_chain = save_chain(evidence_chain)
                payload['evidence_trace_id'] = saved_chain.get('trace_id')
                if explain:
                    payload['evidence_summary'] = summarize_chain(saved_chain)
            except Exception as chain_exc:
                payload['failed_modules'].append(f"evidence_chain:{chain_exc}")

        if explain:
            payload['diagnostic'] = {
                'trace': [
                    'valuation',
                    'technical:rsi/macd/ma',
                    'fundamental',
                    'factor:momentum',
                    f'style:{investment_style}',
                ]
            }
        result = _ok(payload)
        result['meta']['data_timestamp'] = analysis_date
        result['meta']['time_precision'] = time_precision
        result['meta']['price_basis'] = 'kline_latest_close'
        result['meta']['pit_guard'] = pit_guard
        result['meta']['evidence_chain_saved'] = bool(payload.get('evidence_trace_id'))
        return result

    except Exception as e:
        return _fail(str(e))
