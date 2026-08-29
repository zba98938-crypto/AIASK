"""同花顺 MCP Server — 基于 easytrader 的交易接口。

提供工具：
- ths_query_position: 查询持仓
- ths_query_balance: 查询资金余额
- ths_query_deals: 查询成交记录
- ths_query_orders: 查询委托
- ths_place_order: 下单（买入/卖出）— trade_risk，必须携带 broker_token
- ths_cancel_order: 撤单 — trade_risk，必须携带 broker_token

行情类查询请使用 eastmoney / tongdaxin server；本服务仅用于交易动作。

环境变量：
    THS_CLIENT_PATH: 同花顺下单客户端路径
    THS_TRADE_ACCOUNT: 交易账号
    THS_TRADE_PASSWORD: 交易密码（建议使用 Keychain）
    THS_BROKER: 券商名称（用于 easytrader 连接）
    AIASK_FINANCE_THS_BROKER_TOKEN: trade_risk 二次确认 token，未配置则下单被拒
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any

from .._shared.redaction import redact_public_text, sanitize_public_payload
from .._shared.trade_guard import trade_risk_side_effect


LIVE_TOOL_NAMES = {"ths_place_order", "ths_cancel_order"}


def _envelope(success: bool, *, data: Any = None, error: str | None = None, tool: str) -> dict[str, Any]:
    side_effect: str | dict[str, Any] = trade_risk_side_effect() if tool in LIVE_TOOL_NAMES else "read_only"
    return {
        "success": success,
        "data": sanitize_public_payload(data),
        "error": redact_public_text(error) if error is not None else None,
        "meta": {
            "source": "aiask_finance_mcp.tonghuashun",
            "tool": tool,
            "side_effect": side_effect,
        },
    }


class ThsTradeClient:
    """同花顺交易客户端封装（基于 easytrader）。"""

    def __init__(self) -> None:
        self._user = None
        self._connected = False

    def connect(self) -> bool:
        """连接到同花顺客户端。"""
        try:
            import easytrader

            broker = str(os.getenv("THS_BROKER") or "ths").strip()
            self._user = easytrader.use(broker)

            client_path = str(os.getenv("THS_CLIENT_PATH") or "").strip()
            if client_path:
                self._user.connect(client_path)
            else:
                # Try auto-detect
                self._user.connect()

            self._connected = True
            return True
        except ImportError:
            raise RuntimeError("easytrader is not installed. Run: pip install easytrader")
        except Exception as exc:
            self._connected = False
            raise RuntimeError(f"Failed to connect to THS client: {exc}")

    def ensure_connected(self) -> None:
        if not self._connected or self._user is None:
            self.connect()

    def query_balance(self) -> dict[str, Any]:
        """查询资金余额。"""
        self.ensure_connected()
        balance = self._user.balance
        return balance if isinstance(balance, dict) else {"raw": balance}

    def query_position(self) -> list[dict[str, Any]]:
        """查询持仓。"""
        self.ensure_connected()
        positions = self._user.position
        if isinstance(positions, list):
            return positions
        return [positions] if positions else []

    def query_orders(self) -> list[dict[str, Any]]:
        """查询当日委托。"""
        self.ensure_connected()
        orders = self._user.today_entrusts
        if isinstance(orders, list):
            return orders
        return [orders] if orders else []

    def query_deals(self) -> list[dict[str, Any]]:
        """查询当日成交。"""
        self.ensure_connected()
        deals = self._user.today_trades
        if isinstance(deals, list):
            return deals
        return [deals] if deals else []

    def place_order(
        self,
        *,
        code: str,
        price: float,
        amount: int,
        direction: str = "buy",
    ) -> dict[str, Any]:
        """下单。"""
        self.ensure_connected()
        if direction.lower() == "buy":
            result = self._user.buy(code, price=price, amount=amount)
        elif direction.lower() == "sell":
            result = self._user.sell(code, price=price, amount=amount)
        else:
            raise ValueError(f"Invalid direction: {direction}. Use 'buy' or 'sell'.")
        return result if isinstance(result, dict) else {"result": result}

    def cancel_order(self, *, entrust_no: str) -> dict[str, Any]:
        """撤单。"""
        self.ensure_connected()
        result = self._user.cancel_entrust(entrust_no)
        return result if isinstance(result, dict) else {"result": result}


_client: ThsTradeClient | None = None
THS_LIVE_TOOLS_ENV = "AIASK_FINANCE_THS_ENABLE_LIVE_TOOLS"


def _get_client() -> ThsTradeClient:
    global _client
    if _client is None:
        _client = ThsTradeClient()
    return _client


# ------------------------------------------------------------------
# Tool handlers
# ------------------------------------------------------------------


def handle_query_balance(arguments: dict[str, Any]) -> dict[str, Any]:
    try:
        client = _get_client()
        data = client.query_balance()
        return _envelope(True, data=data, tool="ths_query_balance")
    except Exception as exc:
        return _envelope(False, error=str(exc), tool="ths_query_balance")


def handle_query_position(arguments: dict[str, Any]) -> dict[str, Any]:
    try:
        client = _get_client()
        data = client.query_position()
        return _envelope(True, data={"positions": data, "count": len(data)}, tool="ths_query_position")
    except Exception as exc:
        return _envelope(False, error=str(exc), tool="ths_query_position")


def handle_query_orders(arguments: dict[str, Any]) -> dict[str, Any]:
    try:
        client = _get_client()
        data = client.query_orders()
        return _envelope(True, data={"orders": data, "count": len(data)}, tool="ths_query_orders")
    except Exception as exc:
        return _envelope(False, error=str(exc), tool="ths_query_orders")


def handle_query_deals(arguments: dict[str, Any]) -> dict[str, Any]:
    try:
        client = _get_client()
        data = client.query_deals()
        return _envelope(True, data={"deals": data, "count": len(data)}, tool="ths_query_deals")
    except Exception as exc:
        return _envelope(False, error=str(exc), tool="ths_query_deals")


def handle_place_order(arguments: dict[str, Any]) -> dict[str, Any]:
    """下单 — trade_risk 操作，必须携带 broker_token；服务端再校验一次。"""
    from .._shared.trade_guard import (
        TradeGuardError,
        require_broker_token,
        require_live_tools_enabled,
        trade_risk_envelope,
    )

    try:
        require_live_tools_enabled(provider_env_var=THS_LIVE_TOOLS_ENV)
        require_broker_token(arguments or {}, env_var="AIASK_FINANCE_THS_BROKER_TOKEN")
    except TradeGuardError as guard_exc:
        return trade_risk_envelope(guard_exc, tool="ths_place_order")

    code = str(arguments.get("code") or "").strip()
    price = float(arguments.get("price") or 0)
    amount = int(arguments.get("amount") or 0)
    direction = str(arguments.get("direction") or "buy").strip().lower()

    if not code:
        return _envelope(False, error="code is required", tool="ths_place_order")
    if price <= 0:
        return _envelope(False, error="price must be positive", tool="ths_place_order")
    if amount <= 0 or amount % 100 != 0:
        return _envelope(False, error="amount must be positive and multiple of 100", tool="ths_place_order")
    if direction not in ("buy", "sell"):
        return _envelope(False, error="direction must be 'buy' or 'sell'", tool="ths_place_order")

    try:
        client = _get_client()
        result = client.place_order(code=code, price=price, amount=amount, direction=direction)
        return _envelope(True, data={
            "order": result,
            "code": code,
            "price": price,
            "amount": amount,
            "direction": direction,
        }, tool="ths_place_order")
    except Exception as exc:
        return _envelope(False, error=str(exc), tool="ths_place_order")


def handle_cancel_order(arguments: dict[str, Any]) -> dict[str, Any]:
    """撤单 — trade_risk 操作，必须携带 broker_token。"""
    from .._shared.trade_guard import (
        TradeGuardError,
        require_broker_token,
        require_live_tools_enabled,
        trade_risk_envelope,
    )

    try:
        require_live_tools_enabled(provider_env_var=THS_LIVE_TOOLS_ENV)
        require_broker_token(arguments or {}, env_var="AIASK_FINANCE_THS_BROKER_TOKEN")
    except TradeGuardError as guard_exc:
        return trade_risk_envelope(guard_exc, tool="ths_cancel_order")

    entrust_no = str(arguments.get("entrust_no") or "").strip()
    if not entrust_no:
        return _envelope(False, error="entrust_no is required", tool="ths_cancel_order")
    try:
        client = _get_client()
        result = client.cancel_order(entrust_no=entrust_no)
        return _envelope(True, data={"result": result, "entrust_no": entrust_no}, tool="ths_cancel_order")
    except Exception as exc:
        return _envelope(False, error=str(exc), tool="ths_cancel_order")


# ------------------------------------------------------------------
# MCP Protocol
# ------------------------------------------------------------------

TOOL_DEFINITIONS = [
    {
        "name": "ths_query_balance",
        "description": "查询同花顺账户资金余额（可用资金、总资产、冻结资金等）",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "ths_query_position",
        "description": "查询同花顺账户持仓（股票代码、数量、成本、盈亏等）",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "ths_query_orders",
        "description": "查询当日委托记录",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "ths_query_deals",
        "description": "查询当日成交记录",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "ths_place_order",
        "description": "同花顺下单（买入/卖出）— trade_risk，必须携带 broker_token",
        "inputSchema": {
            "type": "object",
            "properties": {
                "code": {"type": "string", "description": "股票代码，例如 '600519'"},
                "price": {"type": "number", "description": "委托价格"},
                "amount": {"type": "integer", "description": "委托数量（必须为 100 的整数倍）"},
                "direction": {"type": "string", "enum": ["buy", "sell"], "description": "买卖方向"},
                "broker_token": {
                    "type": "string",
                    "description": "服务端配置的 AIASK_FINANCE_THS_BROKER_TOKEN；缺失或不符即拒绝",
                },
            },
            "required": ["code", "price", "amount", "direction", "broker_token"],
        },
    },
    {
        "name": "ths_cancel_order",
        "description": "同花顺撤单 — trade_risk，必须携带 broker_token",
        "inputSchema": {
            "type": "object",
            "properties": {
                "entrust_no": {"type": "string", "description": "委托编号"},
                "broker_token": {
                    "type": "string",
                    "description": "服务端配置的 AIASK_FINANCE_THS_BROKER_TOKEN；缺失或不符即拒绝",
                },
            },
            "required": ["entrust_no", "broker_token"],
        },
    },
]

def _listed_tool_definitions() -> list[dict[str, Any]]:
    from .._shared.trade_guard import live_tools_enabled

    if live_tools_enabled(provider_env_var=THS_LIVE_TOOLS_ENV):
        return TOOL_DEFINITIONS
    return [tool for tool in TOOL_DEFINITIONS if tool.get("name") not in LIVE_TOOL_NAMES]

TOOL_HANDLERS = {
    "ths_query_balance": handle_query_balance,
    "ths_query_position": handle_query_position,
    "ths_query_orders": handle_query_orders,
    "ths_query_deals": handle_query_deals,
    "ths_place_order": handle_place_order,
    "ths_cancel_order": handle_cancel_order,
}


def _handle_jsonrpc(request: dict[str, Any]) -> dict[str, Any] | None:
    method = request.get("method", "")
    req_id = request.get("id")
    params = request.get("params") or {}

    if method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {"listChanged": False}, "resources": {"listChanged": False}, "prompts": {"listChanged": False}},
                "serverInfo": {"name": "aiask-finance-tonghuashun", "version": "0.1.0"},
            },
        }

    if method == "notifications/initialized":
        return None

    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": req_id, "result": {"tools": _listed_tool_definitions()}}

    if method == "resources/list":
        return {"jsonrpc": "2.0", "id": req_id, "result": {"resources": []}}

    if method == "prompts/list":
        return {"jsonrpc": "2.0", "id": req_id, "result": {"prompts": []}}

    if method == "tools/call":
        tool_name = params.get("name", "")
        arguments = params.get("arguments") or {}
        handler = TOOL_HANDLERS.get(tool_name)
        if handler is None:
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {"content": [{"type": "text", "text": json.dumps({"error": redact_public_text(f"Unknown tool: {tool_name}")})}], "isError": True},
            }
        result = handler(arguments)
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {"content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False, default=str)}], "isError": not result.get("success", False)},
        }

    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": -32601, "message": redact_public_text(f"Method not found: {method}")}}


def main() -> None:
    sys.stderr.write("aiask-finance-tonghuashun MCP server starting (stdio)...\n")
    sys.stderr.flush()
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except json.JSONDecodeError:
            continue
        response = _handle_jsonrpc(request)
        if response is not None:
            sys.stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
