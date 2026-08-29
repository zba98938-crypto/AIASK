"""模拟交易管理器 — P3 订单生命周期 + 佣金 + 风控"""

from typing import Any
import json
import uuid
import logging
from datetime import datetime, timezone
from ...storage import get_db
from ...utils import ok, fail
from ...services.cost_model import build_cost_model
from ..manager_protocol import normalize_manager_payload
from . import _paper_trading_manager_support as _paper_trading_manager_support_mod

from ._paper_trading_manager_support import (
    _check_risk_before_buy,
    _db_supports_acquire,
    _ensure_account,
    _ensure_positions_consistency,
    _fill_order,
    _get_quote_snapshot,
    _get_price,
    _reconcile_account_state,
    _get_sellable_quantity,
    _normalize_kwargs,
    _normalize_risk_pct,
    _record_order_event,
    _refresh_account_prices,
    _serialize_order_event_row,
    _summarize_order_events,
    _validate_price_limit,
    _validate_sell_request,
)

logger = logging.getLogger(__name__)

# 默认风控规则
DEFAULT_RISK_RULES = {
    "max_position_pct": 30.0,   # 单股最大仓位占比 %
    "max_drawdown_pct": 20.0,   # 最大回撤阈值 %
    "stop_loss_pct": 10.0,      # 个股止损线 %
}


def _as_bool(value, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "y", "on"}:
        return True
    if text in {"0", "false", "no", "n", "off"}:
        return False
    return default


def _sync_paper_trading_support_overrides() -> None:
    """Keep nested support helpers aligned with paper_trading_manager monkeypatches."""
    _paper_trading_manager_support_mod._get_quote_snapshot = _get_quote_snapshot
    _paper_trading_manager_support_mod._get_sellable_quantity = _get_sellable_quantity
    _paper_trading_manager_support_mod._validate_sell_request = _validate_sell_request
    _paper_trading_manager_support_mod._get_price = _get_price

def register_paper_trading_manager(mcp):
    """注册模拟交易管理器工具"""

    @mcp.tool()
    async def paper_trading_manager(action: str, params: dict | None = None, kwargs: Any = None, user_id: str | None = None, account_id: str | None = None, code: str | None = None, price: float | None = None, shares: int | None = None, quantity: int | None = None, order_id: str | None = None, trade_type: str | None = None, direction: str | None = None, order_type: str | None = None, stop_price: float | None = None, name: str | None = None, initial_capital: float | None = None, limit: int | None = None) -> dict:
        """模拟交易管理器（统一 action + kwargs 协议）

        Args:
            action (str, required): 操作类型
            kwargs: JSON 字符串或关键字参数

        Supported actions:
            help, create_account, place_order, cancel_order, pending_orders,
            get_positions/list/positions, orders, order_events, summary, list_accounts/accounts,
            nav_history, set_risk_rules, matching_status, nav_status, update_prices
        """
        try:
            db = get_db()
            kwargs = normalize_manager_payload(
                params=params,
                kwargs=kwargs,
                extra={
                    "user_id": user_id,
                    "account_id": account_id,
                    "code": code,
                    "price": price,
                    "shares": shares,
                    "quantity": quantity,
                    "order_id": order_id,
                    "trade_type": trade_type,
                    "direction": direction,
                    "order_type": order_type,
                    "stop_price": stop_price,
                    "name": name,
                    "initial_capital": initial_capital,
                    "limit": limit,
                },
            )
            kwargs = _normalize_kwargs(kwargs)
            _sync_paper_trading_support_overrides()
            user_id = kwargs.get('user_id', 'default')

            SUPPORTED_ACTIONS = {
                'create_account': '创建模拟账户',
                'place_order': '下单（market/limit/stop）',
                'cancel_order': '撤销挂单',
                'pending_orders': '查看挂单列表',
                'get_positions': '获取持仓', 'list': '获取持仓', 'positions': '获取持仓',
                'orders': '获取成交记录',
                'order_events': '查询订单事件',
                'summary': '账户摘要',
                'list_accounts': '列出所有账户',
                'accounts': '列出所有账户',
                'archive_account': '归档空的模拟账户',
                'reconcile': '校准账户账本与持仓快照',
                'nav_history': '查看NAV历史',
                'set_risk_rules': '设置风控规则',
                'matching_status': '撮合引擎状态',
                'nav_status': 'NAV引擎状态',
                'update_prices': '批量刷新持仓现价/市值/盈亏',
                'help': '显示帮助',
            }

            if action == 'help':
                return ok({'supported_actions': SUPPORTED_ACTIONS})

            # --- create_account ---
            elif action == 'create_account':
                initial_capital = kwargs.get('initial_capital', 100000)
                name = kwargs.get('name', f'模拟账户_{user_id}')
                account_id = str(uuid.uuid4())[:8]
                async with db.acquire() as conn:
                    await conn.execute(
                        """INSERT INTO paper_accounts (id,user_id,name,initial_capital,current_capital,total_value,created_at)
                           VALUES ($1,$2,$3,$4,$4,$4,CURRENT_TIMESTAMP)""",
                        account_id, user_id, name, initial_capital
                    )
                return ok({'account_id': account_id})

            # --- place_order (market/limit/stop) ---
            elif action == 'place_order':
                account_id = kwargs.get('account_id')
                if not account_id:
                    account_id = await _ensure_account(user_id, db)

                code = kwargs.get('code')
                direction = kwargs.get('direction', 'buy')
                shares = kwargs.get('shares') or kwargs.get('quantity')
                price = kwargs.get('price')
                order_type = kwargs.get('order_type', 'market')
                stop_price = kwargs.get('stop_price')

                if not code:
                    return fail('需要提供 code 参数')
                # F-N34-2 fix (诊断报告 §N34): place_order 必须校验代码合法性。
                # 历史问题: 市价单因取不到价格而失败，但限价单(显式 price)绕过价格获取，
                # INVALIDXX@50 挂单成功 → 限价单成为非法代码的旁路。
                # 修复: 在 order_type 分支之前统一做存在性校验，市价/限价/止损一致拒绝非法代码。
                from ...utils import resolve_existing_security_code_async
                _resolved_code, _stock_info, _code_err = await resolve_existing_security_code_async(code)
                if _code_err or not _resolved_code:
                    return fail(_code_err or f'代码不存在或非法: {code}')
                code = _resolved_code
                if shares is None:
                    return fail('需要提供 shares 参数')
                try:
                    shares = int(shares)
                except Exception:
                    return fail('shares 必须是整数')
                if shares <= 0:
                    return fail('shares 必须大于 0')

                trade_type = 'buy' if direction in ('buy', 'long') else 'sell'
                if trade_type == 'buy' and shares % 100 != 0:
                    return fail('买入数量必须为100的整数倍（1手=100股）')

                # --- limit / stop 挂单 ---
                if order_type in ('limit', 'stop'):
                    if order_type == 'limit' and price is None:
                        return fail('限价单需要提供 price')
                    if order_type == 'stop' and stop_price is None:
                        return fail('止损单需要提供 stop_price')
                    try:
                        if price is not None:
                            price = float(price)
                        if stop_price is not None:
                            stop_price = float(stop_price)
                    except Exception:
                        return fail('price/stop_price 必须是数字')

                    rule_price = price if order_type == 'limit' else stop_price
                    price_limit_error = await _validate_price_limit(code, rule_price, db)
                    if price_limit_error:
                        return fail(price_limit_error)

                    order_id = str(uuid.uuid4())[:8]
                    async with db.acquire() as conn:
                        if trade_type == 'sell':
                            reject = await _validate_sell_request(conn, account_id, code, shares)
                            if reject:
                                return fail(reject)
                        row = await conn.fetchrow(
                            """INSERT INTO paper_orders
                               (account_id, strategy_id, signal_date, source, code, direction, shares, price,
                                order_type, stop_price, status, signal_id, position_id, created_at, updated_at)
                               VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,'pending',$11,$12,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)
                               RETURNING id""",
                            account_id, kwargs.get('strategy_id'), kwargs.get('signal_date'), kwargs.get('source', 'manual'),
                            code, trade_type, shares, price, order_type, stop_price,
                            kwargs.get('signal_id'), kwargs.get('position_id')
                        )
                        db_order_id = row['id'] if row else order_id
                        await _record_order_event(
                            conn,
                            str(db_order_id),
                            'created',
                            account_id=account_id,
                            code=code,
                            payload={
                                'order_type': order_type,
                                'direction': trade_type,
                                'shares': shares,
                                'price': price,
                                'stop_price': stop_price,
                            }
                        )
                    return ok({
                        'order_id': str(db_order_id), 'status': 'pending', 'order_type': order_type,
                        'account_id': account_id, 'code': code, 'direction': trade_type,
                        'shares': shares, 'price': price, 'stop_price': stop_price,
                    })

                # --- market 即时成交 ---
                if price is None:
                    price = await _get_price(code, db)
                if price is None:
                    return fail('市价单无法获取当前价格，请手动指定 price')
                try:
                    price = float(price)
                except Exception:
                    return fail('price 必须是数字')
                if price <= 0:
                    return fail('price 必须大于 0')

                price_limit_error = await _validate_price_limit(code, price, db)
                if price_limit_error:
                    return fail(price_limit_error)

                amount = price * shares
                async with db.acquire() as conn:
                    if trade_type == 'sell':
                        reject = await _validate_sell_request(conn, account_id, code, shares)
                        if reject:
                            return fail(reject)
                    # 买入前风控检查
                    if trade_type == 'buy':
                        reject = await _check_risk_before_buy(conn, account_id, code, amount)
                        if reject:
                            await _record_order_event(
                                conn,
                                f"risk-reject-{uuid.uuid4().hex[:8]}",
                                'risk_rejected',
                                account_id=account_id,
                                code=code,
                                payload={
                                    'order_type': 'market',
                                    'direction': trade_type,
                                    'shares': shares,
                                    'price': price,
                                    'reason': reject,
                                }
                            )
                            return fail(f'风控拒绝: {reject}')

                    trade_id, commission = await _fill_order(
                        conn, account_id, code, trade_type, shares, price,
                        strategy_id=kwargs.get('strategy_id'),
                        source_order_id=kwargs.get('source_order_id'),
                        signal_id=kwargs.get('signal_id'),
                        position_id=kwargs.get('position_id'),
                    )
                    await _record_order_event(
                        conn,
                        str(trade_id),
                        'filled',
                        account_id=account_id,
                        code=code,
                        payload={
                            'order_type': 'market',
                            'direction': trade_type,
                            'shares': shares,
                            'price': price,
                            'amount': amount,
                            'commission': round(commission, 4),
                        }
                    )

                # P2-4.4.4 fix(诊断报告 §4.4.4):market 单显式标 bypass_matching
                # 历史问题:matching_engine + nav_engine 都 running=false 但 market 单仍直接 filled
                # AI 看不出该订单是否真正经过撮合,可能误以为系统正常运行
                _matching_running = False
                _nav_running = False
                try:
                    from ...services.matching_engine import get_matching_engine
                    _matching_running = bool(get_matching_engine().status().get('running'))
                except Exception:
                    pass
                try:
                    from ...services.nav_engine import get_nav_engine
                    _nav_running = bool(get_nav_engine().status().get('running'))
                except Exception:
                    pass
                _bypass_warnings: list[str] = []
                if not _matching_running:
                    _bypass_warnings.append(
                        "market_orders_bypass_matching=true: matching_engine.running=false, "
                        "市价单直接成交未经撮合,limit 订单将卡 pending"
                    )
                if not _nav_running:
                    _bypass_warnings.append(
                        "nav_engine.running=false: 账户 NAV 不会自动更新"
                    )

                return ok({
                    'order_id': trade_id, 'status': 'filled', 'order_type': 'market',
                    'account_id': account_id, 'trade_type': trade_type,
                    'price': price, 'quantity': shares, 'amount': amount,
                    'commission': round(commission, 4),
                    'matching_engine_running': _matching_running,
                    'nav_engine_running': _nav_running,
                    'engine_warnings': _bypass_warnings,
                })

            # --- cancel_order ---
            elif action == 'cancel_order':
                order_id = kwargs.get('order_id')
                if not order_id:
                    return fail('需要提供 order_id')
                async with db.acquire() as conn:
                    row = await conn.fetchrow(
                        "SELECT * FROM paper_orders WHERE id=$1 AND status='pending'", int(order_id)
                    )
                    if not row:
                        return fail('未找到该挂单或已非 pending 状态')
                    await conn.execute(
                        "UPDATE paper_orders SET status='cancelled', updated_at=CURRENT_TIMESTAMP WHERE id=$1",
                        int(order_id)
                    )
                    await _record_order_event(
                        conn,
                        str(order_id),
                        'cancelled',
                        account_id=row.get('account_id'),
                        code=row.get('code'),
                        payload={'from_status': 'pending'}
                    )
                return ok({'order_id': order_id, 'status': 'cancelled'})

            # --- pending_orders ---
            elif action == 'pending_orders':
                account_id = kwargs.get('account_id')
                if not account_id:
                    account_id = await _ensure_account(user_id, db)
                async with db.acquire() as conn:
                    rows = await conn.fetch(
                        "SELECT * FROM paper_orders WHERE account_id=$1 AND status='pending' ORDER BY created_at DESC",
                        account_id
                    )
                return ok({'account_id': account_id, 'orders': [dict(r) for r in rows], 'count': len(rows)})

            # --- positions ---
            elif action in ('get_positions', 'list', 'positions'):
                account_id = kwargs.get('account_id')
                if not account_id:
                    account_id = await _ensure_account(user_id, db)
                reconcile = await _reconcile_account_state(db, account_id, refresh_prices=False, force=False)
                async with db.acquire() as conn:
                    rows = await conn.fetch(
                        "SELECT * FROM paper_positions WHERE account_id=$1", account_id
                    )
                    positions = []
                    for row in rows:
                        item = dict(row)
                        sellable = await _get_sellable_quantity(conn, account_id, item.get('stock_code'))
                        quantity = int(item.get('quantity') or 0)
                        item['sellable'] = max(0, min(quantity, sellable))
                        positions.append(item)
                return ok({
                    'account_id': account_id,
                    'positions': positions,
                    'count': len(positions),
                    'reconciliation': reconcile,
                })

            # --- orders (trades) ---
            elif action == 'orders':
                account_id = kwargs.get('account_id')
                if not account_id:
                    account_id = await _ensure_account(user_id, db)
                async with db.acquire() as conn:
                    rows = await conn.fetch(
                        "SELECT * FROM paper_trades WHERE account_id=$1 ORDER BY created_at DESC LIMIT 50",
                        account_id
                    )
                return ok({'account_id': account_id, 'orders': [dict(r) for r in rows], 'count': len(rows)})

            # --- order_events ---
            elif action == 'order_events':
                order_id = kwargs.get('order_id')
                account_id = kwargs.get('account_id')
                if not account_id:
                    account_id = await _ensure_account(user_id, db)

                try:
                    limit = int(kwargs.get('limit', 100))
                except Exception:
                    limit = 100
                limit = max(1, min(limit, 500))

                async with db.acquire() as conn:
                    if order_id:
                        rows = await conn.fetch(
                            "SELECT * FROM order_events WHERE order_id=$1 ORDER BY created_at DESC LIMIT $2",
                            str(order_id), limit
                        )
                    else:
                        rows = await conn.fetch(
                            "SELECT * FROM order_events WHERE account_id=$1 ORDER BY created_at DESC LIMIT $2",
                            account_id, limit
                        )
                events = [_serialize_order_event_row(row) for row in rows]
                return ok({
                    'order_id': str(order_id) if order_id else None,
                    'account_id': account_id,
                    'events': events,
                    'summary': _summarize_order_events(events),
                    'count': len(events),
                })

            # --- summary ---
            elif action == 'summary':
                account_id = kwargs.get('account_id')
                if not account_id:
                    account_id = await _ensure_account(user_id, db)
                reconcile = await _reconcile_account_state(db, account_id, refresh_prices=False, force=False)
                async with db.acquire() as conn:
                    account = await conn.fetchrow("SELECT * FROM paper_accounts WHERE id=$1", account_id)
                    if not account:
                        return fail('账户不存在')
                    positions = await conn.fetch(
                        "SELECT * FROM paper_positions WHERE account_id=$1", account_id
                    )
                    pending = await conn.fetchval(
                        "SELECT COUNT(*) FROM paper_orders WHERE account_id=$1 AND status='pending'", account_id
                    )
                acct = dict(account)
                initial = float(acct.get('initial_capital') or 0)
                total = float(acct.get('total_value') or 0)
                return ok({
                    'account_id': account_id,
                    'account': acct,
                    'positions_count': len(positions),
                    'pending_orders_count': int(pending or 0),
                    'total_value': total,
                    'total_return_pct': round((total - initial) / initial * 100, 2) if initial > 0 else 0,
                    'reconciliation': reconcile,
                })

            # --- list_accounts ---
            elif action in ('list_accounts', 'accounts'):
                async with db.acquire() as conn:
                    rows = await conn.fetch(
                        "SELECT * FROM paper_accounts WHERE user_id=$1 AND COALESCE(status, 'active') <> 'archived' ORDER BY created_at", user_id
                    )
                return ok({'accounts': [dict(r) for r in rows], 'count': len(rows)})

            # --- archive_account ---
            elif action == 'archive_account':
                account_id = kwargs.get('account_id')
                if not account_id:
                    return fail('需要提供 account_id')
                reason = str(kwargs.get('reason') or kwargs.get('archive_reason') or 'cleanup').strip() or 'cleanup'
                async with db.acquire() as conn:
                    account = await conn.fetchrow(
                        "SELECT * FROM paper_accounts WHERE id=$1",
                        account_id,
                    )
                    if not account:
                        return fail('账户不存在')
                    account_payload = dict(account)
                    account_user_id = str(account_payload.get('user_id') or '')
                    account_status = str(account_payload.get('status') or 'active').lower()
                    if account_status == 'archived':
                        return ok({'account_id': account_id, 'archived': True, 'status': 'archived', 'reason': reason})
                    if account_user_id and account_user_id != str(user_id):
                        return fail(
                            '账户不属于当前 user_id，拒绝归档',
                            error_code='USER_SCOPE_MISMATCH',
                            data={
                                'account_id': account_id,
                                'requested_user_id': str(user_id),
                                'account_user_id': account_user_id,
                                'scope': 'paper_account',
                            },
                        )
                    pending_count = await conn.fetchval(
                        "SELECT COUNT(*) FROM paper_orders WHERE account_id=$1 AND status='pending'",
                        account_id,
                    ) or 0
                    position_count = await conn.fetchval(
                        "SELECT COUNT(*) FROM paper_positions WHERE account_id=$1 AND COALESCE(quantity, 0) <> 0",
                        account_id,
                    ) or 0
                    if int(pending_count) > 0 or int(position_count) > 0:
                        return fail(
                            '账户存在持仓或待撤订单，拒绝归档',
                            data={
                                'account_id': account_id,
                                'pending_orders_count': int(pending_count),
                                'positions_count': int(position_count),
                            },
                        )
                    try:
                        await conn.execute(
                            "UPDATE paper_accounts SET status='archived', archived_reason=$1, updated_at=CURRENT_TIMESTAMP WHERE id=$2",
                            reason,
                            account_id,
                        )
                    except Exception:
                        await conn.execute(
                            "UPDATE paper_accounts SET status='archived', archived_reason=$1 WHERE id=$2",
                            reason,
                            account_id,
                        )
                return ok({'account_id': account_id, 'archived': True, 'status': 'archived', 'reason': reason})

            # --- reconcile ---
            elif action == 'reconcile':
                account_id = kwargs.get('account_id')
                if not account_id:
                    account_id = await _ensure_account(user_id, db)
                refresh_prices = _as_bool(kwargs.get('refresh_prices'), True)
                force = _as_bool(kwargs.get('force'), False)
                result = await _reconcile_account_state(
                    db,
                    account_id,
                    refresh_prices=refresh_prices,
                    force=force,
                )
                return ok(result)

            # --- update_prices ---
            elif action == 'update_prices':
                if not _db_supports_acquire(db):
                    return fail('update_prices 执行失败')
                account_id = kwargs.get('account_id')
                if not account_id:
                    account_id = await _ensure_account(user_id, db)
                await _ensure_positions_consistency(db, account_id)
                positions = await _refresh_account_prices(db, account_id)
                # P3-5.15 fix: 计算 updated_count / unchanged_count(诊断报告 §5.15)
                updated_count = sum(1 for p in positions if str(p.get('_refresh_status') or '') == 'updated')
                unchanged_count = sum(1 for p in positions if str(p.get('_refresh_status') or '').startswith('unchanged'))
                async with db.acquire() as conn:
                    enriched_positions = []
                    for row in positions:
                        item = dict(row)
                        sellable = await _get_sellable_quantity(conn, account_id, item.get('stock_code'))
                        quantity = int(item.get('quantity') or 0)
                        item['sellable'] = max(0, min(quantity, sellable))
                        enriched_positions.append(item)
                    account = await conn.fetchrow("SELECT * FROM paper_accounts WHERE id=$1", account_id)
                reconcile = await _reconcile_account_state(db, account_id, refresh_prices=False, force=False)
                return ok({
                    'account_id': account_id,
                    'positions': enriched_positions,
                    'count': len(enriched_positions),
                    'updated_count': updated_count,
                    'unchanged_count': unchanged_count,
                    'refresh_success_rate': (
                        round(updated_count / max(len(enriched_positions), 1), 4)
                    ),
                    'account': dict(account) if account else None,
                    'reconciliation': reconcile,
                })

            # --- nav_history ---
            elif action == 'nav_history':
                account_id = kwargs.get('account_id')
                if not account_id:
                    account_id = await _ensure_account(user_id, db)
                limit = int(kwargs.get('limit', 90))
                async with db.acquire() as conn:
                    rows = await conn.fetch(
                        "SELECT * FROM paper_nav WHERE account_id=$1 ORDER BY nav_date DESC LIMIT $2",
                        account_id, limit
                    )
                return ok({'account_id': account_id, 'nav': [dict(r) for r in reversed(list(rows))]})

            # --- set_risk_rules ---
            elif action == 'set_risk_rules':
                account_id = kwargs.get('account_id')
                # P2-4.4.1 fix(诊断报告 §4.4.1):account_id 必填,不允许 silent create
                if not account_id:
                    return fail(
                        "account_id is required for set_risk_rules. "
                        "Use create_account first to create an account explicitly."
                    )
                raw_rules = kwargs.get('rules')
                if isinstance(raw_rules, str):
                    try:
                        raw_rules = json.loads(raw_rules or "{}")
                    except Exception:
                        raw_rules = {}
                if not isinstance(raw_rules, dict):
                    raw_rules = {}
                rules = {
                    'max_position_pct': _normalize_risk_pct(
                        raw_rules.get('max_position_pct', kwargs.get('max_position_pct')),
                        DEFAULT_RISK_RULES['max_position_pct'],
                    ),
                    'max_drawdown_pct': _normalize_risk_pct(
                        raw_rules.get('max_drawdown_pct', kwargs.get('max_drawdown_pct')),
                        DEFAULT_RISK_RULES['max_drawdown_pct'],
                    ),
                    'stop_loss_pct': _normalize_risk_pct(
                        raw_rules.get('stop_loss_pct', kwargs.get('stop_loss_pct')),
                        DEFAULT_RISK_RULES['stop_loss_pct'],
                    ),
                }
                async with db.acquire() as conn:
                    await conn.execute(
                        "UPDATE paper_accounts SET risk_rules=$1, updated_at=CURRENT_TIMESTAMP WHERE id=$2",
                        json.dumps(rules), account_id
                    )
                # P2-4.4.1 fix(诊断报告 §4.4.1):响应增加 unit 标注消除混淆
                # 历史问题:input.max_drawdown=0.15 → output.max_drawdown_pct=20.0(默认值,未识别 0.15 含义)
                # 修复:max_drawdown_pct 字段统一是 percent 单位,显式标 unit + raw_input
                return ok({
                    'account_id': account_id,
                    'risk_rules': rules,
                    'risk_rules_unit': 'percent',
                    'risk_rules_note': (
                        '所有 *_pct 字段以 percent 单位存储 (e.g. 20.0 = 20%). '
                        '若输入 0~1 范围,会自动 ×100 转 percent.'
                    ),
                })

            # --- matching_status ---
            elif action == 'matching_status':
                try:
                    from ...services.matching_engine import get_matching_engine
                    engine = get_matching_engine()
                    return ok(engine.status())
                except Exception as e:
                    return ok({'running': False, 'error': str(e)})

            # --- nav_status ---
            elif action == 'nav_status':
                try:
                    from ...services.nav_engine import get_nav_engine
                    engine = get_nav_engine()
                    return ok(engine.status())
                except Exception as e:
                    return ok({'running': False, 'error': str(e)})

            else:
                return fail(f'Unknown action: {action}. Supported: {", ".join(SUPPORTED_ACTIONS.keys())}')
        except Exception as e:
            logger.error("[PaperTrading] %s error: %s", action, e, exc_info=True)
            message = str(e).strip() or f'{action} 执行失败'
            return fail(message)
