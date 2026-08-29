"""期权管理器 - Black-Scholes定价和Greeks计算"""

import calendar
from datetime import datetime
from typing import Any, Optional
import json
from ...utils import ok, fail
from ..manager_protocol import normalize_manager_payload


def _normalize_kwargs(kwargs: dict) -> dict:
    params = kwargs.get("params")
    if isinstance(params, dict):
        kwargs = {**kwargs, **params}
    extra = kwargs.get("kwargs")
    if extra is not None:
        if isinstance(extra, str):
            try:
                extra = json.loads(extra or "{}")
            except Exception:
                extra = None
        if isinstance(extra, dict):
            kwargs = {**kwargs, **extra}

    # P1-1 参数命名统一：option_price 为标准字段，兼容 market_price / OptionPrice / price
    if kwargs.get("option_price") is None:
        kwargs["option_price"] = (
            kwargs.get("market_price")
            or kwargs.get("OptionPrice")
            or kwargs.get("price")
        )

    # 参数别名统一
    if kwargs.get("expiry_date") is None:
        kwargs["expiry_date"] = kwargs.get("maturity") or kwargs.get("expiry") or kwargs.get("expire_date") or kwargs.get("expiration") or kwargs.get("expiry_date_str")
    if kwargs.get("time_to_maturity") is None:
        kwargs["time_to_maturity"] = kwargs.get("time") or kwargs.get("ttm") or kwargs.get("maturity_years") or kwargs.get("time_to_expiry") or kwargs.get("t") or kwargs.get("T") or kwargs.get("years") or kwargs.get("time_years")
    if kwargs.get("risk_free_rate") is None:
        kwargs["risk_free_rate"] = kwargs.get("rate") or kwargs.get("rf_rate") or kwargs.get("rf") or kwargs.get("r") or kwargs.get("risk_free") or kwargs.get("riskfree")
    if kwargs.get("volatility") is None:
        kwargs["volatility"] = kwargs.get("vol") or kwargs.get("sigma") or kwargs.get("iv") or kwargs.get("implied_vol")
    if kwargs.get("dividend_yield") is None:
        kwargs["dividend_yield"] = kwargs.get("div_yield") or kwargs.get("dividend") or kwargs.get("q") or kwargs.get("div")
    if kwargs.get("spot") is None:
        kwargs["spot"] = kwargs.get("S") or kwargs.get("s") or kwargs.get("spot_price") or kwargs.get("current_price") or kwargs.get("underlying_price")
    if kwargs.get("strike") is None:
        kwargs["strike"] = kwargs.get("K") or kwargs.get("k") or kwargs.get("strike_price") or kwargs.get("exercise_price")
    if kwargs.get("option_type") is None:
        kwargs["option_type"] = kwargs.get("type") or kwargs.get("cp") or kwargs.get("call_put")
    if kwargs.get("underlying") is None:
        kwargs["underlying"] = kwargs.get("underlying_symbol") or kwargs.get("symbol")
    if kwargs.get("code") is None:
        kwargs["code"] = kwargs.get("underlying") or kwargs.get("underlying_symbol") or kwargs.get("symbol")
    return kwargs


def _time_to_maturity_from_expiry_month(expiry_month: str) -> float:
    token = str(expiry_month or "").strip().replace("-", "")
    if len(token) != 6 or not token.isdigit():
        return 0.0

    year = int(token[:4])
    month = int(token[4:])
    if month < 1 or month > 12:
        return 0.0

    last_day = calendar.monthrange(year, month)[1]
    expiry = datetime(year, month, last_day, 15, 0, 0)
    now = datetime.now()
    return max((expiry - now).total_seconds() / (365.0 * 24 * 3600), 0.0)


def register_options_manager(mcp):
    """注册期权管理器工具"""
    
    @mcp.tool()
    async def options_manager(action: str, params: dict | None = None, kwargs: Any = None):
        """期权管理器（统一 action + kwargs 协议）

        Args:
            action (str, required): 操作类型，可选 help/list/calculate_greeks/calculate_price/implied_volatility/volatility_smirk
            kwargs: 支持 structured ``params``、JSON 字符串 ``kwargs`` 或关键字参数，不同 action 所需参数:
                - help: 无需额外参数
                - list: underlying(str, 如 "510050"), expiry_month(str, optional)
                - calculate_greeks: S(float, 标的价格), K(float, 行权价), T(float, 到期时间/年), r(float, 无风险利率), sigma(float, 波动率), option_type(str, "call"/"put")
                - calculate_price: 同 calculate_greeks；支持 expiry_date(YYYY-MM-DD) 自动换算 time_to_maturity
                - implied_volatility: option_price(float, 标准参数；兼容 market_price/price), S(float), K(float), T(float), r(float), option_type(str)

        Returns:
            dict: {"success": bool, "data": {...}, "error": str|None}

        Examples:
            # 查看帮助
            options_manager(action="help", kwargs="{}")
            # 计算希腊字母
            options_manager(action="calculate_greeks", kwargs='{"S":3.5,"K":3.0,"T":0.25,"r":0.03,"sigma":0.2,"option_type":"call"}')
            # 计算隐含波动率
            options_manager(action="implied_volatility", kwargs='{"S":3.5,"K":3.0,"T":0.25,"r":0.03,"market_price":0.6,"option_type":"call"}')
        """
        try:
            kwargs = normalize_manager_payload(params=params, kwargs=kwargs)
            kwargs = _normalize_kwargs(kwargs)
            if action == 'help':
                return ok({
                    'supported_actions': {
                        'list': '列出期权合约',
                        'calculate_greeks': '计算希腊字母（需要 code/underlying）',
                        'calculate_price': 'Black-Scholes 定价（需要参数）',
                        'implied_volatility': '隐含波动率计算',
                        'volatility_smirk': '按行权价聚合隐含波动率曲线',
                        'help': '显示帮助信息',
                    }
                })
            
            elif action == 'list':
                underlying = kwargs.get('underlying') or kwargs.get('code') or '510050'
                expiry_month = kwargs.get('expiry_month') or kwargs.get('month') or ""
                limit = kwargs.get('limit', 200)
                try:
                    limit = int(limit)
                except Exception:
                    limit = 200

                from ..options import get_option_chain

                chain = get_option_chain(
                    underlying=str(underlying),
                    expiry_month=str(expiry_month),
                    limit=limit,
                )
                data = (chain or {}).get('data') or {}
                options = data.get('options') or []

                # 契约稳定性优先：外部行情接口不可用时返回 success=True + 空结构，避免 manager 合同失败
                if not (chain or {}).get('success'):
                    return ok({
                        'underlying': data.get('underlying', {'code': str(underlying)}),
                        'expiryMonths': data.get('expiryMonths', []),
                        'selectedExpiry': data.get('selectedExpiry', []),
                        'options': [],
                        'count': 0,
                        'truncated': False,
                        'message': chain.get('error') if isinstance(chain, dict) else '获取期权链失败，已降级为空结果',
                        'degraded': True,
                    })

                return ok({
                    'underlying': data.get('underlying', {}),
                    'expiryMonths': data.get('expiryMonths', []),
                    'selectedExpiry': data.get('selectedExpiry', []),
                    'options': options,
                    'count': len(options),
                    'truncated': bool(data.get('truncated', False)),
                    'degraded': False,
                })
            
            elif action == 'calculate_greeks':
                code = kwargs.get('code') or kwargs.get('underlying')
                spot = kwargs.get('spot')
                strike = kwargs.get('strike')
                time_to_maturity = kwargs.get('time_to_maturity')
                risk_free_rate = kwargs.get('risk_free_rate')
                volatility = kwargs.get('volatility')
                option_type = str(kwargs.get('option_type', 'call') or 'call').lower().strip()
                if option_type not in ('call', 'put'):
                    return fail('option_type 必须为 call 或 put')
                dividend_yield = kwargs.get('dividend_yield')
                
                # 类型转换（参数可能以字符串形式传入）— 使用默认值
                if strike is not None:
                    strike = float(strike)
                if time_to_maturity is not None:
                    time_to_maturity = float(time_to_maturity)
                risk_free_rate = float(risk_free_rate) if risk_free_rate is not None else 0.03
                volatility = float(volatility) if volatility is not None else 0.25
                dividend_yield = float(dividend_yield) if dividend_yield is not None else 0.0
                
                # 如果提供了 code 但没有 spot，自动获取当前价格
                if code and spot is None:
                    try:
                        from ..market import get_kline
                        from ...utils import normalize_code
                        res = await get_kline(normalize_code(code), 'daily', 1)
                        if res.get('success') and res.get('data'):
                            spot = float(res['data'][-1].get('close', 0) or 0)
                    except Exception:
                        pass
                if spot is not None:
                    spot = float(spot)
                else:
                    spot = 100.0
                
                # strike 默认等于 spot（平值期权）
                if strike is None:
                    strike = spot
                
                # 处理到期日期
                expiry_date = kwargs.get('expiry_date')
                if expiry_date:
                    from ...services.options_pricing import options_pricing
                    time_to_maturity = options_pricing.calculate_time_to_maturity(str(expiry_date))
                
                # time_to_maturity 默认 0.25 年（3个月）
                if time_to_maturity is None:
                    time_to_maturity = 0.25
                
                from ...services.options_pricing import options_pricing
                
                option_price = options_pricing.black_scholes(
                    spot=spot,
                    strike=strike,
                    time_to_maturity=time_to_maturity,
                    risk_free_rate=risk_free_rate,
                    volatility=volatility,
                    option_type=option_type,
                    dividend_yield=dividend_yield
                )
                
                greeks = options_pricing.calculate_greeks(
                    spot=spot,
                    strike=strike,
                    time_to_maturity=time_to_maturity,
                    risk_free_rate=risk_free_rate,
                    volatility=volatility,
                    option_type=option_type,
                    dividend_yield=dividend_yield
                )
                
                return ok({
                    'code': code,
                    'option_type': option_type,
                    'spot': spot,
                    'strike': strike,
                    'time_to_maturity': f"{time_to_maturity:.4f} years ({time_to_maturity*365:.0f} days)",
                    'volatility': f"{volatility*100:.2f}%",
                    'risk_free_rate': f"{risk_free_rate*100:.2f}%",
                    'option_price': f"{option_price:.4f}",
                    'greeks': {
                        'delta': f"{greeks['delta']:.4f}",
                        'gamma': f"{greeks['gamma']:.4f}",
                        'theta': f"{greeks['theta']:.4f} (per day)",
                        'vega': f"{greeks['vega']:.4f} (per 1% vol change)",
                        'rho': f"{greeks['rho']:.4f} (per 1% rate change)",
                    },
                    'interpretation': {
                        'delta': f"标的价格变动1元，期权价格变动{abs(greeks['delta']):.4f}元",
                        'gamma': f"Delta对标的价格的敏感度为{greeks['gamma']:.4f}",
                        'theta': f"每天时间价值衰减{abs(greeks['theta']):.4f}元",
                        'vega': f"波动率变动1%，期权价格变动{greeks['vega']:.4f}元",
                        'rho': f"利率变动1%，期权价格变动{greeks['rho']:.4f}元",
                    }
                })
            
            elif action == 'calculate_price':
                # P1-3 修复说明：
                # 旧实现对异常参数（如 option_type 非法、volatility<=0）缺乏校验，且会静默回退默认值。
                # 新实现保留“缺参默认值”以兼容历史调用，但对“已提供且非法”的参数显式报错。
                option_type_raw = kwargs.get('option_type', 'call')
                option_type = str(option_type_raw or 'call').lower().strip()
                if option_type not in ('call', 'put'):
                    return fail('option_type 必须为 call 或 put')

                def _to_float(name: str, raw_value, default_value: float) -> float:
                    # 兼容策略：None/空串使用默认；有值但无法转换时报错
                    if raw_value is None or raw_value == '':
                        return float(default_value)
                    try:
                        return float(raw_value)
                    except Exception:
                        raise ValueError(name)

                try:
                    spot = _to_float('spot', kwargs.get('spot'), 100.0)
                    strike = _to_float('strike', kwargs.get('strike'), 100.0)
                    time_to_maturity = _to_float('time_to_maturity', kwargs.get('time_to_maturity'), 0.25)
                    risk_free_rate = _to_float('risk_free_rate', kwargs.get('risk_free_rate'), 0.03)
                    volatility = _to_float('volatility', kwargs.get('volatility'), 0.25)
                    dividend_yield = _to_float('dividend_yield', kwargs.get('dividend_yield'), 0.0)
                except ValueError:
                    return fail('参数类型错误：spot/strike/time_to_maturity/risk_free_rate/volatility/dividend_yield 必须为数字')

                if spot <= 0:
                    return fail('spot 必须大于 0')
                if strike <= 0:
                    return fail('strike 必须大于 0')
                if time_to_maturity <= 0:
                    return fail('time_to_maturity 必须大于 0')
                if volatility <= 0:
                    return fail('volatility 必须大于 0')

                # 如果提供了 code 但没有显式 spot，自动获取当前价格
                code = kwargs.get('code') or kwargs.get('underlying')
                if code and kwargs.get('spot') is None:
                    try:
                        from ..market import get_kline
                        from ...utils import normalize_code
                        res = await get_kline(normalize_code(code), 'daily', 1)
                        if res.get('success') and res.get('data'):
                            fetched_spot = float(res['data'][-1].get('close', 0) or 0)
                            if fetched_spot > 0:
                                spot = fetched_spot
                                if kwargs.get('strike') is None:
                                    strike = spot  # 平值期权
                    except Exception:
                        pass

                expiry_date = kwargs.get('expiry_date')
                if expiry_date:
                    from ...services.options_pricing import options_pricing
                    time_to_maturity = options_pricing.calculate_time_to_maturity(str(expiry_date))
                    if time_to_maturity <= 0:
                        return fail('expiry_date 对应的剩余期限必须大于 0')

                from ...services.options_pricing import options_pricing

                option_price = options_pricing.black_scholes(
                    spot=spot,
                    strike=strike,
                    time_to_maturity=time_to_maturity,
                    risk_free_rate=risk_free_rate,
                    volatility=volatility,
                    option_type=option_type,
                    dividend_yield=dividend_yield
                )

                if option_type == 'call':
                    intrinsic_value = max(spot - strike, 0)
                else:
                    intrinsic_value = max(strike - spot, 0)

                time_value = option_price - intrinsic_value

                return ok({
                    'option_type': option_type,
                    'spot': spot,
                    'strike': strike,
                    'option_price': f"{option_price:.4f}",
                    'intrinsic_value': f"{intrinsic_value:.4f}",
                    'time_value': f"{time_value:.4f}",
                    'moneyness': 'ITM' if intrinsic_value > 0 else ('ATM' if abs(spot - strike) < 0.01 * spot else 'OTM'),
                })

            elif action == 'implied_volatility':
                # P1-1 修复说明：
                # 统一参数命名为 option_price，并兼容 market_price；错误提示明确列出可用参数名。
                option_price = kwargs.get('option_price')
                if option_price is None:
                    return fail('需要提供 option_price（兼容别名：market_price/price）参数')

                try:
                    option_price = float(option_price)
                    spot = float(kwargs.get('spot', 100.0) or 100.0)
                    strike = float(kwargs.get('strike', 100.0) or 100.0)
                    time_to_maturity = float(kwargs.get('time_to_maturity', 0.25) or 0.25)
                    risk_free_rate = float(kwargs.get('risk_free_rate', 0.03) or 0.03)
                    dividend_yield = float(kwargs.get('dividend_yield', 0.0) or 0.0)
                except Exception:
                    return fail('参数类型错误：option_price/spot/strike/time_to_maturity/risk_free_rate/dividend_yield 必须为数字')

                option_type = str(kwargs.get('option_type', 'call') or 'call').lower().strip()
                if option_type not in ('call', 'put'):
                    return fail('option_type 必须为 call 或 put')
                if option_price <= 0:
                    return fail('option_price 必须大于 0')
                if spot <= 0 or strike <= 0:
                    return fail('spot/strike 必须大于 0')
                if time_to_maturity <= 0:
                    return fail('time_to_maturity 必须大于 0')

                expiry_date = kwargs.get('expiry_date')
                if expiry_date:
                    from ...services.options_pricing import options_pricing
                    time_to_maturity = options_pricing.calculate_time_to_maturity(str(expiry_date))
                    if time_to_maturity <= 0:
                        return fail('expiry_date 对应的剩余期限必须大于 0')

                from ...services.options_pricing import options_pricing

                iv = options_pricing.implied_volatility(
                    option_price=option_price,
                    spot=spot,
                    strike=strike,
                    time_to_maturity=time_to_maturity,
                    risk_free_rate=risk_free_rate,
                    option_type=option_type,
                    dividend_yield=dividend_yield
                )

                if iv is None:
                    return fail('隐含波动率计算未收敛')

                return ok({
                    'option_price': option_price,
                    'implied_volatility': f"{iv*100:.2f}%",
                    'iv_value': iv,
                })

            elif action == 'volatility_smirk':
                underlying = kwargs.get('underlying') or kwargs.get('code') or '510050'
                expiry_month = str(kwargs.get('expiry_month') or kwargs.get('month') or '').strip()
                limit = kwargs.get('limit', 200)
                try:
                    limit = int(limit)
                except Exception:
                    limit = 200
                limit = max(20, min(limit, 1000))

                from ..options import get_option_chain
                from ...services.options_pricing import options_pricing

                chain = get_option_chain(underlying=str(underlying), expiry_month=expiry_month, limit=limit)
                data = (chain or {}).get('data') or {}
                if not (chain or {}).get('success'):
                    return ok({
                        'underlying': {'code': str(underlying)},
                        'selected_expiry': [],
                        'curve': [],
                        'degraded': True,
                        'message': chain.get('error') if isinstance(chain, dict) else '获取期权链失败',
                    })

                underlying_info = data.get('underlying') or {'code': str(underlying)}
                spot = float(underlying_info.get('price') or 0.0)
                selected_expiry = data.get('selectedExpiry') or []
                target_expiry = str(selected_expiry[0] if selected_expiry else expiry_month or '').strip()
                time_to_maturity = _time_to_maturity_from_expiry_month(target_expiry)
                risk_free_rate = float(kwargs.get('risk_free_rate', 0.03) or 0.03)
                dividend_yield = float(kwargs.get('dividend_yield', 0.0) or 0.0)

                rows = {}
                for item in data.get('options') or []:
                    if target_expiry and str(item.get('expiryMonth') or '').strip() != target_expiry:
                        continue

                    try:
                        strike = float(item.get('strike'))
                        option_price = float(item.get('last'))
                    except Exception:
                        continue

                    option_type = str(item.get('type') or '').strip().lower()
                    if option_type not in {'call', 'put'} or strike <= 0 or option_price <= 0 or spot <= 0 or time_to_maturity <= 0:
                        continue

                    iv = options_pricing.implied_volatility(
                        option_price=option_price,
                        spot=spot,
                        strike=strike,
                        time_to_maturity=time_to_maturity,
                        risk_free_rate=risk_free_rate,
                        option_type=option_type,
                        dividend_yield=dividend_yield,
                    )
                    if iv is None:
                        continue

                    bucket = rows.setdefault(strike, {'strike': strike})
                    bucket[f'{option_type}_iv'] = float(iv)

                curve = []
                for strike in sorted(rows.keys()):
                    bucket = rows[strike]
                    call_iv = bucket.get('call_iv')
                    put_iv = bucket.get('put_iv')
                    iv_values = [value for value in (call_iv, put_iv) if isinstance(value, float)]
                    avg_iv = float(sum(iv_values) / len(iv_values)) if iv_values else None
                    curve.append({
                        'strike': float(strike),
                        'moneyness': float(strike / spot) if spot > 0 else None,
                        'call_iv': call_iv,
                        'put_iv': put_iv,
                        'avg_iv': avg_iv,
                        'skew': (put_iv - call_iv) if isinstance(call_iv, float) and isinstance(put_iv, float) else None,
                    })

                atm_point = min(curve, key=lambda item: abs((item.get('moneyness') or 1.0) - 1.0)) if curve else None
                return ok({
                    'underlying': underlying_info,
                    'selected_expiry': selected_expiry,
                    'curve': curve,
                    'spot': spot,
                    'time_to_maturity': time_to_maturity,
                    'degraded': bool(data.get('degraded', False)),
                    'atm_iv': atm_point.get('avg_iv') if atm_point else None,
                    'point_count': len(curve),
                })
            
            else:
                return fail(f'Unknown action: {action}. Supported: help, list, calculate_greeks, calculate_price, implied_volatility, volatility_smirk')
        except Exception as e:
            return fail(str(e))
