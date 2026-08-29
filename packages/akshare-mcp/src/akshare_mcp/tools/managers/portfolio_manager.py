"""组合管理器 - 创建、调整、查询组合"""

from typing import Any
import json
import time

from ...storage import get_db
from ..manager_protocol import (
    fail_with_meta,
    normalize_manager_code,
    normalize_manager_kwargs,
    normalize_manager_payload,
    ok_with_meta,
)


def _normalize_kwargs(kwargs: dict) -> dict:
    """统一解析 kwargs 参数（兼容 JSON 字符串和 dict）"""
    return normalize_manager_kwargs(kwargs)


def _safe_portfolio_id(val):
    """将 portfolio_id 转为 int（SQLite schema 为 integer id）"""
    if val is None:
        return None
    try:
        return int(val)
    except (ValueError, TypeError):
        return val


def _safe_float(val):
    """安全转换 float，非法值返回 None。"""
    if val is None:
        return None
    try:
        return float(val)
    except (ValueError, TypeError):
        return None


def _normalize_user_id(value):
    text = str(value or "default").strip()
    return text or "default"


def _normalize_metadata(value):
    """兼容 dict / JSON 字符串 / 其他类型的 metadata 输入。"""
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value or "{}")
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            return {}
    return {}


async def _load_portfolio(conn, portfolio_id, user_id=None):
    row = await conn.fetchrow("SELECT * FROM portfolios WHERE id = $1", portfolio_id)
    if not row:
        return None
    item = dict(row)
    owner = str(item.get("user_id") or "default")
    if user_id is not None and owner != str(user_id):
        return None
    return item


def register_portfolio_manager(mcp):
    """注册组合管理器工具"""

    @mcp.tool()
    async def portfolio_manager(action: str, params: dict | None = None, kwargs: Any = None, user_id: str | None = None, portfolio_id: int | None = None, code: str | None = None, shares: int | None = None, cost_price: float | None = None, name: str | None = None, description: str | None = None, initial_capital: float | None = None, updates: dict | None = None):
        """组合管理器（统一 action + kwargs 协议）

        Args:
            action (str, required): 操作类型，可选 help/list/create/get/update/delete/add_holding/remove_holding/get_holdings
            kwargs: 支持 structured ``params``、JSON 字符串 ``kwargs`` 或关键字参数，不同 action 所需参数:
                - help: 无需额外参数
                - list: user_id(str, optional)
                - create: name(str), description(str, optional), metadata(dict, optional)
                - get: portfolio_id(int)
                - update: portfolio_id(int), name(str, optional), description(str, optional)
                - delete: portfolio_id(int)
                - add_holding: portfolio_id(int), code(str), shares(int), cost_price(float)
                - remove_holding: portfolio_id(int), code(str)
                - get_holdings: portfolio_id(int)

        Returns:
            dict: {"success": bool, "data": {...}, "error": str|None}

        Examples:
            # 查看帮助
            portfolio_manager(action="help", kwargs="{}")
            # 创建组合
            portfolio_manager(action="create", kwargs='{"name":"我的组合","initial_capital":500000}')
            # 添加持仓
            portfolio_manager(action="add_holding", kwargs='{"portfolio_id":1,"code":"600519","shares":100,"cost_price":1800}')
            # 查看持仓
            portfolio_manager(action="get_holdings", kwargs='{"portfolio_id":1}')
            # 列出所有组合
            portfolio_manager(action="list", kwargs="{}")
        """
        start_time = time.perf_counter()
        try:
            db = get_db()
            kwargs = normalize_manager_payload(
                params=params,
                kwargs=kwargs,
                extra={
                    "user_id": user_id,
                    "portfolio_id": portfolio_id,
                    "code": code,
                    "shares": shares,
                    "cost_price": cost_price,
                    "name": name,
                    "description": description,
                    "initial_capital": initial_capital,
                    "updates": updates,
                },
            )
            _, kwargs = normalize_manager_code(None, kwargs)
            user_id = _normalize_user_id(kwargs.get('user_id'))

            def _ok(data: dict, source_chain=None):
                return ok_with_meta(
                    data,
                    tool_name="portfolio_manager",
                    action=action,
                    started_at=start_time,
                    source_chain=source_chain,
                )

            def _fail(message: str, source_chain=None):
                return fail_with_meta(
                    message,
                    tool_name="portfolio_manager",
                    action=action,
                    started_at=start_time,
                    source_chain=source_chain,
                )

            if action == 'list':
                async with db.acquire() as conn:
                    rows = await conn.fetch(
                        "SELECT * FROM portfolios WHERE user_id = $1 ORDER BY created_at DESC",
                        user_id
                    )
                    portfolios = [dict(row) for row in rows]
                return _ok({'portfolios': portfolios}, source_chain=['portfolio_manager', 'db.portfolios'])

            elif action == 'create':
                name = kwargs.get('name')
                if not name:
                    return _fail('需要提供 name 参数', source_chain=['portfolio_manager'])
                user_id = _normalize_user_id(kwargs.get('user_id'))
                initial_capital = kwargs.get('initial_capital', 100000)
                description = kwargs.get('description')
                metadata = _normalize_metadata(kwargs.get('metadata'))

                strategy_allocations = kwargs.get('strategy_allocations')
                if isinstance(strategy_allocations, list) and 'strategy_allocations' not in metadata:
                    metadata = {**metadata, 'strategy_allocations': strategy_allocations}

                async with db.acquire() as conn:
                    portfolio_id = await conn.fetchval(
                        """INSERT INTO portfolios (name, description, metadata, user_id, initial_capital, current_value, created_at, updated_at)
                           VALUES ($1, $2, $3, $4, $5, $5, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                           RETURNING id""",
                        name, description, json.dumps(metadata or {}), user_id, initial_capital
                    )
                return _ok(
                    {'portfolio_id': portfolio_id, 'name': name, 'description': description, 'metadata': metadata},
                    source_chain=['portfolio_manager', 'db.portfolios'],
                )

            elif action == 'get':
                portfolio_id = _safe_portfolio_id(kwargs.get('portfolio_id'))
                async with db.acquire() as conn:
                    portfolio = await _load_portfolio(conn, portfolio_id, user_id)
                    if not portfolio:
                        return _fail('组合不存在', source_chain=['portfolio_manager', 'db.portfolios'])
                return _ok(dict(portfolio), source_chain=['portfolio_manager', 'db.portfolios'])

            elif action == 'update':
                # P0-1 修复说明：
                # 旧实现只更新 current_value，且当未传值时写入 NULL，触发 portfolios.current_value NOT NULL 约束冲突。
                # 新实现先读取现有记录并回填必填字段（尤其 current_value），同时兼容顶层字段与 updates 字段。
                portfolio_id = _safe_portfolio_id(kwargs.get('portfolio_id'))
                if not portfolio_id:
                    return _fail('需要提供 portfolio_id 参数', source_chain=['portfolio_manager'])

                raw_updates = kwargs.get('updates', {})
                updates = dict(raw_updates) if isinstance(raw_updates, dict) else {}
                for key in ('name', 'description', 'current_value'):
                    if key not in updates and kwargs.get(key) is not None:
                        updates[key] = kwargs.get(key)

                if not updates:
                    return _fail(
                        '需要提供 updates，或在顶层提供 name/description/current_value',
                        source_chain=['portfolio_manager'],
                    )

                normalized = {}
                if 'name' in updates and updates.get('name') is not None:
                    normalized['name'] = str(updates.get('name'))
                if 'description' in updates:
                    normalized['description'] = updates.get('description')
                if 'current_value' in updates:
                    current_value = _safe_float(updates.get('current_value'))
                    if current_value is None:
                        return _fail('current_value 必须是数字', source_chain=['portfolio_manager'])
                    normalized['current_value'] = current_value

                async with db.acquire() as conn:
                    existing = await conn.fetchrow(
                        "SELECT id, name, description, current_value, user_id FROM portfolios WHERE id = $1",
                        portfolio_id
                    )
                    if not existing:
                        return _fail('组合不存在', source_chain=['portfolio_manager', 'db.portfolios'])
                    existing = dict(existing)
                    if str(existing.get('user_id') or 'default') != user_id:
                        return _fail('组合不存在', source_chain=['portfolio_manager', 'db.portfolios'])

                    final_name = normalized.get('name', existing['name'])
                    final_description = normalized.get('description', existing['description'])
                    final_current_value = normalized.get('current_value', existing['current_value'])

                    if final_current_value is None:
                        return _fail('current_value 不能为空', source_chain=['portfolio_manager', 'db.portfolios'])

                    await conn.execute(
                        "UPDATE portfolios SET name = $1, description = $2, current_value = $3, updated_at = CURRENT_TIMESTAMP WHERE id = $4",
                        final_name, final_description, final_current_value, portfolio_id
                    )
                return _ok(
                    {'portfolio_id': portfolio_id, 'updated': True, 'applied_fields': list(normalized.keys())},
                    source_chain=['portfolio_manager', 'db.portfolios'],
                )

            elif action == 'delete':
                portfolio_id = _safe_portfolio_id(kwargs.get('portfolio_id'))
                async with db.acquire() as conn:
                    portfolio = await _load_portfolio(conn, portfolio_id, user_id)
                    if not portfolio:
                        return _fail('组合不存在', source_chain=['portfolio_manager', 'db.portfolios'])
                    await conn.execute("DELETE FROM holdings WHERE portfolio_id = $1", portfolio_id)
                    await conn.execute("DELETE FROM portfolios WHERE id = $1", portfolio_id)
                return _ok(
                    {'portfolio_id': portfolio_id, 'deleted': True},
                    source_chain=['portfolio_manager', 'db.portfolios', 'db.holdings'],
                )

            elif action == 'add_holding':
                portfolio_id = _safe_portfolio_id(kwargs.get('portfolio_id'))
                code = kwargs.get('code')
                shares = kwargs.get('shares')
                cost_price = kwargs.get('cost_price', 0)

                if not portfolio_id:
                    return _fail('需要提供 portfolio_id 参数', source_chain=['portfolio_manager'])
                if not code:
                    return _fail('需要提供 code 参数（股票代码）', source_chain=['portfolio_manager'])
                if shares is None:
                    return _fail('需要提供 shares 参数（持仓数量）', source_chain=['portfolio_manager'])

                async with db.acquire() as conn:
                    # 验证组合存在
                    portfolio = await _load_portfolio(conn, portfolio_id, user_id)
                    if not portfolio:
                        return _fail(
                            f'组合 {portfolio_id} 不存在，请先创建组合',
                            source_chain=['portfolio_manager', 'db.portfolios'],
                        )

                    await conn.execute(
                        """INSERT INTO holdings (portfolio_id, code, shares, cost_price, created_at, updated_at)
                           VALUES ($1, $2, $3, $4, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                           ON CONFLICT (portfolio_id, code) DO UPDATE 
                           SET shares = holdings.shares + EXCLUDED.shares, updated_at = CURRENT_TIMESTAMP""",
                        portfolio_id, code, int(shares), float(cost_price)
                    )
                return _ok(
                    {
                        'portfolio_id': portfolio_id,
                        'code': code,
                        'shares': int(shares),
                        'cost_price': float(cost_price),
                        'added': True,
                    },
                    source_chain=['portfolio_manager', 'db.portfolios', 'db.holdings'],
                )

            elif action == 'remove_holding':
                portfolio_id = _safe_portfolio_id(kwargs.get('portfolio_id'))
                code = kwargs.get('code')

                if not portfolio_id:
                    return _fail('需要提供 portfolio_id 参数', source_chain=['portfolio_manager'])
                if not code:
                    return _fail('需要提供 code 参数（股票代码）', source_chain=['portfolio_manager'])

                async with db.acquire() as conn:
                    portfolio = await _load_portfolio(conn, portfolio_id, user_id)
                    if not portfolio:
                        return _fail(
                            f'组合 {portfolio_id} 不存在，请先创建组合',
                            source_chain=['portfolio_manager', 'db.portfolios'],
                        )
                    await conn.execute(
                        "DELETE FROM holdings WHERE portfolio_id = $1 AND code = $2",
                        portfolio_id, code
                    )
                return _ok(
                    {'portfolio_id': portfolio_id, 'code': code, 'removed': True},
                    source_chain=['portfolio_manager', 'db.portfolios', 'db.holdings'],
                )

            elif action == 'get_holdings':
                portfolio_id = _safe_portfolio_id(kwargs.get('portfolio_id'))
                if not portfolio_id:
                    return _fail('需要提供 portfolio_id 参数', source_chain=['portfolio_manager'])

                async with db.acquire() as conn:
                    portfolio = await _load_portfolio(conn, portfolio_id, user_id)
                    if not portfolio:
                        return _fail('组合不存在', source_chain=['portfolio_manager', 'db.portfolios'])
                    rows = await conn.fetch(
                        "SELECT * FROM holdings WHERE portfolio_id = $1 ORDER BY created_at",
                        portfolio_id
                    )
                    holdings = [dict(row) for row in rows]
                return _ok(
                    {'portfolio_id': portfolio_id, 'holdings': holdings, 'count': len(holdings)},
                    source_chain=['portfolio_manager', 'db.portfolios', 'db.holdings'],
                )

            elif action == 'help':
                return _ok({
                    'supported_actions': {
                        'list': '列出所有组合',
                        'create': '创建组合（需要 name，可附带 description/metadata）',
                        'get': '获取组合详情（需要 portfolio_id）',
                        'update': '更新组合（需要 portfolio_id, updates）',
                        'delete': '删除组合（需要 portfolio_id）',
                        'add_holding': '添加持仓（需要 portfolio_id, code, shares）',
                        'remove_holding': '删除持仓（需要 portfolio_id, code）',
                        'get_holdings': '获取持仓列表（需要 portfolio_id）',
                        'help': '显示帮助信息',
                    }
                }, source_chain=['portfolio_manager'])

            else:
                return _fail(
                    'Unknown action: {action}. Supported: list, create, get, update, delete, add_holding, remove_holding, get_holdings, help'.format(
                        action=action
                    ),
                    source_chain=['portfolio_manager'],
                )

        except Exception as e:
            return fail_with_meta(
                str(e),
                tool_name='portfolio_manager',
                action=action,
                started_at=start_time,
                source_chain=['portfolio_manager'],
            )
