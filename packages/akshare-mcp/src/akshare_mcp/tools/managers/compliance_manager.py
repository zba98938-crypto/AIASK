"""合规管理器 - 交易限制、合规检查"""

from typing import Any
import json
import math
from datetime import datetime, timedelta, timezone
from ...services.market_data_access import FALLBACK_DB_ONLY, get_quote_snapshot_sync
from ...utils import ok, fail, normalize_code
from ..market.order_book import get_order_book
from ..manager_protocol import normalize_manager_payload

MAX_SINGLE_ORDER_SHARES = 1_000_000
MAX_SINGLE_ORDER_AMOUNT = 50_000_000.0
MIN_LOT_SIZE = 100


def _finite_float(value: Any) -> float:
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError("non-finite number")
    return parsed


def _normalize_kwargs(kwargs: dict) -> dict:
    """统一解析 kwargs 参数（兼容 JSON 字符串和 dict）"""
    params = kwargs.get("params")
    if isinstance(params, dict):
        kwargs = {**kwargs, **params}
    raw = kwargs.get("kwargs")
    if isinstance(raw, dict):
        kwargs = {**kwargs, **raw}
    elif isinstance(raw, str):
        try:
            extra = json.loads(raw or "{}")
            if isinstance(extra, dict):
                kwargs = {**kwargs, **extra}
        except Exception:
            pass
    if "code" not in kwargs or kwargs.get("code") is None:
        kwargs["code"] = kwargs.get("Code") or kwargs.get("stock_code") or kwargs.get("symbol")
    if kwargs.get("quantity") is None:
        kwargs["quantity"] = kwargs.get("shares") or kwargs.get("qty")
    if kwargs.get("direction") is None:
        kwargs["direction"] = kwargs.get("side") or kwargs.get("order_side")
    return kwargs


def _in_cn_trading_hours() -> bool:
    """简单交易时段校验（北京时间，工作日 09:30-11:30, 13:00-15:00）"""
    now_cn = datetime.now(timezone.utc).astimezone(timezone(timedelta(hours=8)))
    if now_cn.weekday() >= 5:
        return False
    minutes = now_cn.hour * 60 + now_cn.minute
    return (570 <= minutes <= 690) or (780 <= minutes <= 900)


def _infer_limit_pct(code: str | None, name: str | None) -> float:
    code_text = str(code or "").strip()
    name_text = str(name or "").upper()
    if "ST" in name_text:
        return 0.05
    if code_text.startswith(("300", "301", "688", "689")):
        return 0.20
    if code_text.startswith(("8", "4")):
        return 0.30
    return 0.10


def _augment_realtime_checks(
    *,
    code: str | None,
    direction: str,
    quantity: int | None,
    checks: dict,
    violations: list[str],
    warnings: list[str],
    order_price: float | None = None,
) -> dict:
    realtime = {
        "quote_available": False,
        "quote_source": None,
        "order_book_available": False,
        "order_book_source": None,
        "name": None,
        "current_price": None,
        "pre_close": None,
        "limit_pct": None,
        "limit_up_price": None,
        "limit_down_price": None,
        "at_limit_up": False,
        "at_limit_down": False,
        "top_ask_volume": None,
        "top_bid_volume": None,
        "top5_ask_volume": None,
        "top5_bid_volume": None,
    }
    if not code:
        return realtime

    try:
        quote = get_quote_snapshot_sync(code, fallback_mode=FALLBACK_DB_ONLY)
        payload = (quote or {}).get("data") if isinstance(quote, dict) else None
        if isinstance(payload, dict):
            realtime["quote_available"] = True
            realtime["quote_source"] = payload.get("backend_used") or payload.get("source")
            realtime["name"] = payload.get("name")
            realtime["current_price"] = payload.get("price")
            realtime["pre_close"] = payload.get("preClose")
            checks["realtime_quote"] = True
            checks["st_stock"] = bool("ST" in str(payload.get("name") or "").upper())

            current_price = payload.get("price")
            pre_close = payload.get("preClose")
            if current_price not in (None, "") and pre_close not in (None, ""):
                try:
                    current_val = float(current_price)
                    pre_close_val = float(pre_close)
                    if pre_close_val > 0:
                        limit_pct = _infer_limit_pct(code, payload.get("name"))
                        upper = round(pre_close_val * (1.0 + limit_pct), 2)
                        lower = round(pre_close_val * (1.0 - limit_pct), 2)
                        tol = max(0.01, pre_close_val * 0.0005)
                        at_limit_up = current_val >= upper - tol
                        at_limit_down = current_val <= lower + tol
                        realtime.update(
                            {
                                "limit_pct": limit_pct,
                                "limit_up_price": upper,
                                "limit_down_price": lower,
                                "at_limit_up": at_limit_up,
                                "at_limit_down": at_limit_down,
                            }
                        )
                        checks["limit_up_down"] = not (at_limit_up or at_limit_down)
                        if direction == "buy" and at_limit_up:
                            violations.append("实时行情显示接近或处于涨停，当前不宜买入")
                        if direction == "sell" and at_limit_down:
                            violations.append("实时行情显示接近或处于跌停，当前不宜卖出")
                        # P2-4.4.2 fix: 订单 price 越界涨跌停板 — 这是诊断报告 §4.4.2 真正的 bug
                        # 历史问题:input.price=1828 / limit_up_price=1442.1 但 checks.limit_up_down=true silent pass
                        # 修复:只要订单 price 给定且超出 [lower, upper] 区间,无论实时价是否 at_limit,都 reject
                        if order_price is not None:
                            try:
                                order_price_f = float(order_price)
                                if order_price_f > upper:
                                    checks["limit_up_down"] = False
                                    violations.append(
                                        f"订单价格 {order_price_f} 超过涨停价 {upper}（不可下单）"
                                    )
                                elif order_price_f < lower:
                                    checks["limit_up_down"] = False
                                    violations.append(
                                        f"订单价格 {order_price_f} 低于跌停价 {lower}（不可下单）"
                                    )
                            except (TypeError, ValueError):
                                warnings.append("订单价格格式无效,涨跌停越界校验跳过")
                except Exception:
                    warnings.append("实时涨跌停复核解析失败，已降级为静态规则")
        else:
            warnings.append("实时行情获取失败，涨跌停/ST 仅按静态规则校验")
    except Exception as exc:
        warnings.append(f"实时行情复核失败: {exc}")

    try:
        order_book = get_order_book(code)
        payload = (order_book or {}).get("data") if isinstance(order_book, dict) else None
        if isinstance(payload, dict):
            bids = list(payload.get("bids") or [])
            asks = list(payload.get("asks") or [])
            top_ask = int((asks[0] or {}).get("volume") or 0) if asks else 0
            top_bid = int((bids[0] or {}).get("volume") or 0) if bids else 0
            top5_ask = int(sum(int(item.get("volume") or 0) for item in asks[:5]))
            top5_bid = int(sum(int(item.get("volume") or 0) for item in bids[:5]))
            realtime.update(
                {
                    "order_book_available": True,
                    "order_book_source": payload.get("source"),
                    "top_ask_volume": top_ask,
                    "top_bid_volume": top_bid,
                    "top5_ask_volume": top5_ask,
                    "top5_bid_volume": top5_bid,
                }
            )
            checks["realtime_order_book"] = True
            if direction == "buy":
                if top5_ask <= 0:
                    violations.append("实时盘口卖量为 0，当前疑似无法买入")
                elif quantity is not None and quantity > 0 and top5_ask < quantity:
                    warnings.append(f"实时盘口卖一至卖五总量仅 {top5_ask}，可能无法一次性成交 {quantity} 股")
            elif direction == "sell":
                if top5_bid <= 0:
                    violations.append("实时盘口买量为 0，当前疑似无法卖出")
                elif quantity is not None and quantity > 0 and top5_bid < quantity:
                    warnings.append(f"实时盘口买一至买五总量仅 {top5_bid}，可能无法一次性卖出 {quantity} 股")
        else:
            warnings.append("实时盘口获取失败，流动性校验已降级")
    except Exception as exc:
        warnings.append(f"实时盘口复核失败: {exc}")

    return realtime



def evaluate_order_compliance(code: str, direction: str, quantity_raw, price_raw=None) -> dict:
    """可复用的合规检查核心逻辑（供 execution_manager 强制前置闸门调用）。"""
    code_n = normalize_code(code or '') if code else None
    direction_n = str(direction or '').strip().lower()

    violations = []
    warnings = []

    quantity = None
    if quantity_raw is not None:
        try:
            quantity_value = _finite_float(quantity_raw)
            if not quantity_value.is_integer():
                raise ValueError("quantity must be an integer")
            quantity = int(quantity_value)
        except Exception:
            violations.append('quantity 格式无效，需为正整数')

    price = None
    if price_raw is not None:
        try:
            price = _finite_float(price_raw)
        except Exception:
            violations.append('price 格式无效，需为正数')

    if not code_n:
        violations.append('缺少 code 参数')
    if direction_n not in ('buy', 'sell'):
        violations.append('direction 仅支持 buy/sell')
    if quantity is None or quantity <= 0:
        violations.append('quantity 必须大于 0')
    if price is not None and price <= 0:
        violations.append('price 必须大于 0')

    if quantity is not None and quantity > MAX_SINGLE_ORDER_SHARES:
        violations.append(f'单笔数量超限（>{MAX_SINGLE_ORDER_SHARES}）')

    if direction_n == 'buy' and quantity is not None and quantity % MIN_LOT_SIZE != 0:
        violations.append(f'买入数量必须为 {MIN_LOT_SIZE} 的整数倍')

    order_amount = None
    if quantity is not None and price is not None and price > 0:
        order_amount = quantity * price
        if order_amount > MAX_SINGLE_ORDER_AMOUNT:
            violations.append(f'单笔金额超限（>{MAX_SINGLE_ORDER_AMOUNT:.0f}）')

    trading_hours = _in_cn_trading_hours()
    checks = {
        'position_limit': quantity is not None and quantity <= MAX_SINGLE_ORDER_SHARES,
        'trading_hours': trading_hours,
        'suspended': False,
        'st_stock': False,
        'lot_size': not (direction_n == 'buy' and quantity is not None and quantity % MIN_LOT_SIZE != 0),
        'order_amount': (order_amount is None) or (order_amount <= MAX_SINGLE_ORDER_AMOUNT),
        'limit_up_down': True,
        'realtime_quote': False,
        'realtime_order_book': False,
    }

    if not trading_hours:
        warnings.append('当前时间不在交易时段内（仅提示，部分券商支持预委托）')
    realtime = _augment_realtime_checks(
        code=code_n,
        direction=direction_n,
        quantity=quantity,
        checks=checks,
        violations=violations,
        warnings=warnings,
        order_price=price,
    )
    if not realtime.get("quote_available"):
        warnings.append('停牌/ST/涨跌停校验当前为静态规则，建议在下单前接入实时行情复核')

    blocked = len(violations) > 0
    passed = not blocked
    return {
        'code': code_n,
        'direction': direction_n,
        'quantity': quantity,
        'price': price,
        'order_amount': float(order_amount) if order_amount is not None else None,
        'passed': passed,
        'blocked': blocked,
        'checks': checks,
        'violations': violations,
        'warnings': warnings,
        'realtime': realtime,
    }


def register_compliance_manager(mcp):
    """注册合规管理器工具"""

    @mcp.tool()
    async def compliance_manager(action: str, params: dict | None = None, kwargs: Any = None):
        """合规管理器（统一 action + kwargs 协议）

        Args:
            action (str, required): 操作类型，可选 help/check_order/get_restrictions/check/check_trade/rules
            kwargs: 支持 structured ``params``、JSON 字符串 ``kwargs`` 或关键字参数，不同 action 所需参数:
                - help: 无需额外参数
                - check_order: code(str), direction(str, "buy"/"sell"), quantity(int), price(float)
                - get_restrictions: code(str, optional)
                - check / check_trade: code(str), direction(str), quantity(int)
                - rules: 无需额外参数（列出合规规则）

        Returns:
            dict: {"success": bool, "data": {...}, "error": str|None}

        Examples:
            # 查看帮助
            compliance_manager(action="help", kwargs="{}")
            # 检查订单合规
            compliance_manager(action="check_order", kwargs='{"code":"600519","direction":"buy","quantity":100,"price":1800}')
            # 获取交易限制
            compliance_manager(action="get_restrictions", kwargs='{"code":"600519"}')
            # 查看合规规则
            compliance_manager(action="rules", kwargs="{}")
        """
        try:
            kwargs = normalize_manager_payload(params=params, kwargs=kwargs)
            SUPPORTED_ACTIONS = {
                'check_order': '检查订单合规',
                'get_restrictions': '获取交易限制',
                'check': '检查订单合规（别名）',
                'check_trade': '检查交易合规（别名）',
                'rules': '获取合规规则',
                'help': '显示帮助信息',
            }

            if action == 'help':
                return ok({'supported_actions': SUPPORTED_ACTIONS})

            elif action in ['check_order', 'check', 'check_trade']:
                result = evaluate_order_compliance(
                    code=kwargs.get('code'),
                    direction=kwargs.get('direction'),
                    quantity_raw=kwargs.get('quantity'),
                    price_raw=kwargs.get('price'),
                )
                return ok(result)

            elif action == 'get_restrictions':
                code = kwargs.get('code')
                return ok({
                    'code': code,
                    'restrictions': {
                        'max_position_pct': 0.1,
                        'max_single_order_shares': MAX_SINGLE_ORDER_SHARES,
                        'max_single_order_amount': MAX_SINGLE_ORDER_AMOUNT,
                        'min_lot_size': MIN_LOT_SIZE,
                        'trading_allowed': True,
                        'trading_hours': '09:30-11:30, 13:00-15:00 (Asia/Shanghai)',
                    }
                })

            elif action == 'rules':
                return ok({
                    'rules': [
                        {'name': 'position_limit', 'description': '单只股票持仓不超过净资产10%'},
                        {'name': 'single_order_shares', 'description': f'单笔数量不超过 {MAX_SINGLE_ORDER_SHARES} 股'},
                        {'name': 'single_order_amount', 'description': f'单笔金额不超过 {MAX_SINGLE_ORDER_AMOUNT:.0f} 元'},
                        {'name': 'lot_size', 'description': f'买入数量建议为 {MIN_LOT_SIZE} 的整数倍'},
                        {'name': 'trading_hours', 'description': '只能在交易时间内下单（9:30-11:30, 13:00-15:00）'},
                        {'name': 'st_restriction', 'description': 'ST股票每日涨跌幅限制5%'},
                        {'name': 'suspended', 'description': '停牌股票不可交易'},
                        {'name': 'limit_up_down', 'description': '涨跌停股票限制交易'},
                    ]
                })

            else:
                return fail(f'Unknown action: {action}. Supported: {", ".join(SUPPORTED_ACTIONS.keys())}')
        except Exception as e:
            return fail(str(e))
