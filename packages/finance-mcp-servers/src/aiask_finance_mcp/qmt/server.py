"""MiniQMT / 迅投 QMT MCP Server — 量化交易接口。

提供工具：
- qmt_query_account: 查询账户信息
- qmt_query_position: 查询持仓
- qmt_query_stock_data: 查询行情数据
- qmt_place_order: 下单 — 需确认
- qmt_cancel_order: 撤单 — 需确认
- qmt_query_orders: 查询委托

环境变量：
    QMT_PATH: MiniQMT 安装路径
    QMT_ACCOUNT: 交易账号
    QMT_ACCOUNT_TYPE: 账户类型（STOCK/CREDIT）
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any

from .._shared.redaction import redact_public_text, sanitize_public_payload
from .._shared.trade_guard import trade_risk_side_effect
import logging

logger = logging.getLogger(__name__)


LIVE_TOOL_NAMES = {"qmt_place_order", "qmt_cancel_order"}


def _envelope(success: bool, *, data: Any = None, error: str | None = None, tool: str) -> dict[str, Any]:
    side_effect: str | dict[str, Any] = trade_risk_side_effect() if tool in LIVE_TOOL_NAMES else "read_only"
    return {
        "success": success,
        "data": sanitize_public_payload(data),
        "error": redact_public_text(error) if error is not None else None,
        "meta": {"source": "aiask_finance_mcp.qmt", "tool": tool, "side_effect": side_effect},
    }


class QmtClient:
    """MiniQMT 客户端封装。"""

    def __init__(self) -> None:
        self._xt = None
        self._connected = False

    def connect(self) -> bool:
        """连接到 MiniQMT。"""
        try:
            from xtquant import xtdata
            from xtquant.xttrader import XtQuantTrader
            from xtquant.xttype import StockAccount

            qmt_path = str(os.getenv("QMT_PATH") or "").strip()
            if not qmt_path:
                raise RuntimeError("QMT_PATH environment variable is required")

            session_id = int(os.getenv("QMT_SESSION_ID") or "123456")
            self._trader = XtQuantTrader(qmt_path, session_id)
            self._trader.start()

            account_id = str(os.getenv("QMT_ACCOUNT") or "").strip()
            account_type = str(os.getenv("QMT_ACCOUNT_TYPE") or "STOCK").strip()
            self._account = StockAccount(account_id, account_type)

            connect_result = self._trader.connect()
            if connect_result != 0:
                raise RuntimeError(f"QMT connect failed with code: {connect_result}")

            self._xt = xtdata
            self._connected = True
            return True
        except ImportError:
            raise RuntimeError("xtquant is not installed. Please install XtQuant SDK from your QMT provider.")
        except Exception as exc:
            self._connected = False
            raise RuntimeError(f"Failed to connect to QMT: {exc}")

    def ensure_connected(self) -> None:
        if not self._connected:
            self.connect()

    def query_account(self) -> dict[str, Any]:
        self.ensure_connected()
        asset = self._trader.query_stock_asset(self._account)
        if asset is None:
            return {"error": "Failed to query account"}
        return {
            "total_asset": asset.total_asset,
            "cash": asset.cash,
            "market_value": asset.market_value,
            "frozen_cash": asset.frozen_cash,
        }

    def query_position(self) -> list[dict[str, Any]]:
        self.ensure_connected()
        positions = self._trader.query_stock_positions(self._account)
        if not positions:
            return []
        return [
            {
                "code": p.stock_code,
                "volume": p.volume,
                "can_use_volume": p.can_use_volume,
                "open_price": p.open_price,
                "market_value": p.market_value,
            }
            for p in positions
        ]

    def query_orders(self) -> list[dict[str, Any]]:
        self.ensure_connected()
        orders = self._trader.query_stock_orders(self._account)
        if not orders:
            return []
        return [
            {
                "order_id": o.order_id,
                "stock_code": o.stock_code,
                "order_type": o.order_type,
                "price": o.price,
                "volume": o.order_volume,
                "traded_volume": o.traded_volume,
                "status": o.order_status,
            }
            for o in orders
        ]

    def place_order(self, *, code: str, price: float, volume: int, direction: str) -> dict[str, Any]:
        self.ensure_connected()
        from xtquant.xtconstant import STOCK_BUY, STOCK_SELL

        # Everything that was not exactly "buy" used to become a sell. Refuse
        # instead: which side to trade is the caller's decision, and a typo must
        # never place the opposite order.
        normalized = str(direction).strip().lower()
        if normalized == "buy":
            order_type = STOCK_BUY
        elif normalized == "sell":
            order_type = STOCK_SELL
        else:
            raise ValueError(
                f"Invalid direction: {direction!r}. Use 'buy' or 'sell'."
            )
        order_id = self._trader.order_stock(
            self._account, code, order_type, volume, 0, price
        )
        return {"order_id": order_id, "code": code, "price": price, "volume": volume, "direction": direction}

    def cancel_order(self, *, order_id: int) -> dict[str, Any]:
        self.ensure_connected()
        result = self._trader.cancel_order_stock(self._account, order_id)
        return {"order_id": order_id, "cancel_result": result}

    def query_stock_data(self, *, code: str, period: str = "1d", count: int = 100) -> list[dict[str, Any]]:
        self.ensure_connected()
        from xtquant import xtdata
        xtdata.download_history_data(code, period, count=count)
        data = xtdata.get_market_data_ex([], [code], period=period, count=count)
        if not data or code not in data:
            return []
        df = data[code]
        return df.to_dict("records") if hasattr(df, "to_dict") else []


_client: QmtClient | None = None
QMT_LIVE_TOOLS_ENV = "AIASK_FINANCE_QMT_ENABLE_LIVE_TOOLS"


def _get_client() -> QmtClient:
    global _client
    if _client is None:
        _client = QmtClient()
    return _client


def handle_query_account(arguments: dict[str, Any]) -> dict[str, Any]:
    try:
        return _envelope(True, data=_get_client().query_account(), tool="qmt_query_account")
    except Exception as exc:
        return _envelope(False, error=str(exc), tool="qmt_query_account")


def handle_query_position(arguments: dict[str, Any]) -> dict[str, Any]:
    try:
        positions = _get_client().query_position()
        return _envelope(True, data={"positions": positions, "count": len(positions)}, tool="qmt_query_position")
    except Exception as exc:
        return _envelope(False, error=str(exc), tool="qmt_query_position")


def handle_query_orders(arguments: dict[str, Any]) -> dict[str, Any]:
    try:
        orders = _get_client().query_orders()
        return _envelope(True, data={"orders": orders, "count": len(orders)}, tool="qmt_query_orders")
    except Exception as exc:
        return _envelope(False, error=str(exc), tool="qmt_query_orders")


def handle_query_stock_data(arguments: dict[str, Any]) -> dict[str, Any]:
    code = str(arguments.get("code") or "").strip()
    if not code:
        return _envelope(False, error="code is required", tool="qmt_query_stock_data")
    period = str(arguments.get("period") or "1d").strip()
    count = min(int(arguments.get("count") or 100), 500)
    try:
        data = _get_client().query_stock_data(code=code, period=period, count=count)
        return _envelope(True, data={"code": code, "bars": data, "count": len(data)}, tool="qmt_query_stock_data")
    except Exception as exc:
        return _envelope(False, error=str(exc), tool="qmt_query_stock_data")


def handle_place_order(arguments: dict[str, Any]) -> dict[str, Any]:
    """QMT 下单 — trade_risk 操作，必须携带 broker_token。"""
    from .._shared.trade_guard import (
        TradeGuardError,
        require_broker_token,
        require_live_tools_enabled,
        trade_risk_envelope,
    )

    try:
        require_live_tools_enabled(provider_env_var=QMT_LIVE_TOOLS_ENV)
        require_broker_token(arguments or {}, env_var="AIASK_FINANCE_QMT_BROKER_TOKEN")
    except TradeGuardError as guard_exc:
        return trade_risk_envelope(guard_exc, tool="qmt_place_order")

    code = str(arguments.get("code") or "").strip()
    price = float(arguments.get("price") or 0)
    volume = int(arguments.get("volume") or 0)
    direction = str(arguments.get("direction") or "buy").strip().lower()
    if not code or price <= 0 or volume <= 0:
        return _envelope(False, error="code, price (>0), volume (>0) are required", tool="qmt_place_order")
    # Validate before the client picks a side. The client read
    # `STOCK_BUY if direction == "buy" else STOCK_SELL`, so "buy " (trailing
    # space), a misspelling, or any unknown word silently became a SELL order.
    # Tonghuashun already rejects this; QMT must reject it too.
    if direction not in ("buy", "sell"):
        return _envelope(False, error="direction must be 'buy' or 'sell'", tool="qmt_place_order")
    try:
        result = _get_client().place_order(code=code, price=price, volume=volume, direction=direction)
        return _envelope(True, data=result, tool="qmt_place_order")
    except Exception as exc:
        return _envelope(False, error=str(exc), tool="qmt_place_order")


def handle_cancel_order(arguments: dict[str, Any]) -> dict[str, Any]:
    """QMT 撤单 — trade_risk 操作，必须携带 broker_token。"""
    from .._shared.trade_guard import (
        TradeGuardError,
        require_broker_token,
        require_live_tools_enabled,
        trade_risk_envelope,
    )

    try:
        require_live_tools_enabled(provider_env_var=QMT_LIVE_TOOLS_ENV)
        require_broker_token(arguments or {}, env_var="AIASK_FINANCE_QMT_BROKER_TOKEN")
    except TradeGuardError as guard_exc:
        return trade_risk_envelope(guard_exc, tool="qmt_cancel_order")

    order_id = int(arguments.get("order_id") or 0)
    if not order_id:
        return _envelope(False, error="order_id is required", tool="qmt_cancel_order")
    try:
        result = _get_client().cancel_order(order_id=order_id)
        return _envelope(True, data=result, tool="qmt_cancel_order")
    except Exception as exc:
        return _envelope(False, error=str(exc), tool="qmt_cancel_order")


TOOL_DEFINITIONS = [
    {"name": "qmt_query_account", "description": "QMT 查询账户资产", "inputSchema": {"type": "object", "properties": {}}},
    {"name": "qmt_query_position", "description": "QMT 查询持仓", "inputSchema": {"type": "object", "properties": {}}},
    {"name": "qmt_query_orders", "description": "QMT 查询委托", "inputSchema": {"type": "object", "properties": {}}},
    {"name": "qmt_query_stock_data", "description": "QMT 查询行情数据", "inputSchema": {"type": "object", "properties": {"code": {"type": "string"}, "period": {"type": "string", "enum": ["1m", "5m", "15m", "30m", "1h", "1d", "1w", "1mon"]}, "count": {"type": "integer"}}, "required": ["code"]}},
    {"name": "qmt_place_order", "description": "QMT 下单 — trade_risk，必须携带 broker_token", "inputSchema": {"type": "object", "properties": {"code": {"type": "string"}, "price": {"type": "number"}, "volume": {"type": "integer"}, "direction": {"type": "string", "enum": ["buy", "sell"]}, "broker_token": {"type": "string", "description": "AIASK_FINANCE_QMT_BROKER_TOKEN"}}, "required": ["code", "price", "volume", "direction", "broker_token"]}},
    {"name": "qmt_cancel_order", "description": "QMT 撤单 — trade_risk，必须携带 broker_token", "inputSchema": {"type": "object", "properties": {"order_id": {"type": "integer"}, "broker_token": {"type": "string", "description": "AIASK_FINANCE_QMT_BROKER_TOKEN"}}, "required": ["order_id", "broker_token"]}},
]

def _listed_tool_definitions() -> list[dict[str, Any]]:
    from .._shared.trade_guard import live_tools_enabled

    if live_tools_enabled(provider_env_var=QMT_LIVE_TOOLS_ENV):
        return TOOL_DEFINITIONS
    return [tool for tool in TOOL_DEFINITIONS if tool.get("name") not in LIVE_TOOL_NAMES]

TOOL_HANDLERS = {
    "qmt_query_account": handle_query_account,
    "qmt_query_position": handle_query_position,
    "qmt_query_orders": handle_query_orders,
    "qmt_query_stock_data": handle_query_stock_data,
    "qmt_place_order": handle_place_order,
    "qmt_cancel_order": handle_cancel_order,
}


def _handle_jsonrpc(request: dict[str, Any]) -> dict[str, Any] | None:
    method = request.get("method", "")
    req_id = request.get("id")
    params = request.get("params") or {}
    if method == "initialize":
        return {"jsonrpc": "2.0", "id": req_id, "result": {"protocolVersion": "2024-11-05", "capabilities": {"tools": {"listChanged": False}, "resources": {"listChanged": False}, "prompts": {"listChanged": False}}, "serverInfo": {"name": "aiask-finance-qmt", "version": "0.1.0"}}}
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
            return {"jsonrpc": "2.0", "id": req_id, "result": {"content": [{"type": "text", "text": json.dumps({"error": redact_public_text(f"Unknown tool: {tool_name}")})}], "isError": True}}
        try:
            result = handler(arguments)
        except Exception as exc:
            # A malformed argument must not kill the stdio loop. Handlers parse
            # numbers before their own try, so an argument of the wrong JSON type
            # used to propagate out of here and terminate the whole server with
            # no response at all.
            logger.warning("tool %s failed: %s", tool_name, exc)
            return {"jsonrpc": "2.0", "id": req_id, "result": {"content": [{"type": "text", "text": json.dumps({"success": False, "data": None, "error": redact_public_text(exc), "error_code": "TOOL_EXECUTION_FAILED", "tool": tool_name}, ensure_ascii=False)}], "isError": True}}
        return {"jsonrpc": "2.0", "id": req_id, "result": {"content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False, default=str)}], "isError": not result.get("success", False)}}
    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": -32601, "message": redact_public_text(f"Method not found: {method}")}}


def main() -> None:
    sys.stderr.write("aiask-finance-qmt MCP server starting (stdio)...\n")
    sys.stderr.flush()
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except json.JSONDecodeError as _swallowed:
            logger.warning("packages/finance-mcp-servers/src/aiask_finance_mcp/qmt/server.py:334: swallowed %s and continued: %s", type(_swallowed).__name__, _swallowed)
            continue
        response = _handle_jsonrpc(request)
        if response is not None:
            sys.stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
