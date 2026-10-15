"""通达信 MCP Server — 基于 pytdx 的行情数据服务。

提供工具：
- tdx_realtime_quote: 实时行情报价
- tdx_kline_history: 历史 K 线数据
- tdx_minute_data: 分时数据
- tdx_tick_data: 逐笔成交
- tdx_finance_info: 财务数据
- tdx_market_snapshot: 市场快照

# TODO(tdx_block_stocks): 板块成分股工具尚未实现；目前由 eastmoney/akshare-mcp 提供。

启动方式：
    python -m aiask_finance_mcp.tongdaxin.server

环境变量：
    TDX_SERVER_IP: 通达信行情服务器 IP（默认 119.147.212.81）
    TDX_SERVER_PORT: 通达信行情服务器端口（默认 7709）
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any

from .._shared.redaction import redact_public_text, sanitize_public_payload
import logging

logger = logging.getLogger(__name__)


# ------------------------------------------------------------------
# Configuration
# ------------------------------------------------------------------

DEFAULT_SERVER_IP = "119.147.212.81"
DEFAULT_SERVER_PORT = 7709

# 常用通达信行情服务器列表
TDX_SERVERS = [
    ("119.147.212.81", 7709),
    ("112.74.214.43", 7727),
    ("221.231.141.60", 7709),
    ("101.227.73.20", 7709),
    ("101.227.77.254", 7709),
    ("14.215.128.18", 7709),
    ("59.173.18.140", 7709),
    ("180.153.39.51", 7709),
]

# K 线周期映射
KLINE_PERIODS = {
    "5min": 0,
    "15min": 1,
    "30min": 2,
    "1hour": 3,
    "daily": 4,
    "weekly": 5,
    "monthly": 6,
    "1min": 8,
    "quarterly": 11,
    "yearly": 12,
}

# 市场代码
MARKET_SH = 1  # 上海
MARKET_SZ = 0  # 深圳


def _detect_market(code: str) -> int:
    """根据股票代码判断市场。"""
    code = str(code).strip().lstrip("0")
    if code.startswith(("6", "9", "5")):
        return MARKET_SH
    return MARKET_SZ


def _get_server_config() -> tuple[str, int]:
    """获取通达信服务器配置。"""
    ip = str(os.getenv("TDX_SERVER_IP") or DEFAULT_SERVER_IP).strip()
    port = int(os.getenv("TDX_SERVER_PORT") or DEFAULT_SERVER_PORT)
    return ip, port


# ------------------------------------------------------------------
# TDX Client Wrapper
# ------------------------------------------------------------------


class TdxClient:
    """通达信行情客户端封装。"""

    def __init__(self) -> None:
        self._api = None
        self._connected = False

    def connect(self) -> bool:
        """连接到通达信行情服务器。"""
        try:
            from pytdx.hq import TdxHq_API

            self._api = TdxHq_API()
            ip, port = _get_server_config()
            self._api.connect(ip, port)
            self._connected = True
            return True
        except ImportError:
            raise RuntimeError(
                "pytdx is not installed. Run: pip install pytdx"
            )
        except Exception as exc:
            self._connected = False
            raise RuntimeError(f"Failed to connect to TDX server: {exc}")

    def disconnect(self) -> None:
        """断开连接。"""
        if self._api and self._connected:
            try:
                self._api.disconnect()
            except Exception as _swallowed:
                logger.warning("packages/finance-mcp-servers/src/aiask_finance_mcp/tongdaxin/server.py:120: swallowed %s and continued: %s", type(_swallowed).__name__, _swallowed)
                pass
            self._connected = False

    def ensure_connected(self) -> None:
        """确保已连接。"""
        if not self._connected or self._api is None:
            self.connect()

    def realtime_quote(self, codes: list[str]) -> list[dict[str, Any]]:
        """获取实时行情报价。"""
        self.ensure_connected()
        stock_list = [(int(_detect_market(c)), c.zfill(6)) for c in codes]
        data = self._api.get_security_quotes(stock_list)
        if data is None:
            return []
        results = []
        for item in data:
            results.append({
                "code": item.get("code", ""),
                "name": item.get("name", ""),
                "market": item.get("market", 0),
                "price": item.get("price", 0.0),
                "last_close": item.get("last_close", 0.0),
                "open": item.get("open", 0.0),
                "high": item.get("high", 0.0),
                "low": item.get("low", 0.0),
                "volume": item.get("vol", 0),
                "amount": item.get("amount", 0.0),
                "bid1_price": item.get("bid1", 0.0),
                "bid1_volume": item.get("bid_vol1", 0),
                "ask1_price": item.get("ask1", 0.0),
                "ask1_volume": item.get("ask_vol1", 0),
                "change_pct": round(
                    (item.get("price", 0) - item.get("last_close", 0))
                    / max(item.get("last_close", 1), 0.001)
                    * 100,
                    2,
                )
                if item.get("last_close")
                else 0.0,
            })
        return results

    def kline_history(
        self,
        code: str,
        *,
        period: str = "daily",
        count: int = 100,
        start: int = 0,
    ) -> list[dict[str, Any]]:
        """获取历史 K 线数据。"""
        self.ensure_connected()
        market = _detect_market(code)
        period_code = KLINE_PERIODS.get(period, 4)
        data = self._api.get_security_bars(
            period_code, market, code.zfill(6), start, count
        )
        if data is None:
            return []
        results = []
        for item in data:
            results.append({
                "datetime": str(item.get("datetime", "")),
                "open": item.get("open", 0.0),
                "high": item.get("high", 0.0),
                "low": item.get("low", 0.0),
                "close": item.get("close", 0.0),
                "volume": item.get("vol", 0),
                "amount": item.get("amount", 0.0),
            })
        return results

    def minute_data(self, code: str) -> list[dict[str, Any]]:
        """获取当日分时数据。"""
        self.ensure_connected()
        market = _detect_market(code)
        data = self._api.get_minute_time_data(market, code.zfill(6))
        if data is None:
            return []
        return [
            {
                "time": str(item.get("time", "")),
                "price": item.get("price", 0.0),
                "volume": item.get("vol", 0),
            }
            for item in data
        ]

    def tick_data(self, code: str, *, count: int = 100, start: int = 0) -> list[dict[str, Any]]:
        """获取逐笔成交数据。"""
        self.ensure_connected()
        market = _detect_market(code)
        data = self._api.get_transaction_data(market, code.zfill(6), start, count)
        if data is None:
            return []
        return [
            {
                "time": str(item.get("time", "")),
                "price": item.get("price", 0.0),
                "volume": item.get("vol", 0),
                "direction": item.get("buyorsell", 0),
            }
            for item in data
        ]

    def block_stocks(self, block_name: str) -> list[dict[str, Any]]:
        """获取板块成分股。"""
        self.ensure_connected()
        try:
            from pytdx.reader import BlockReader

            reader = BlockReader()
            # Try standard block file paths
            data = reader.get_block_info(block_name)
            if data is None:
                return []
            return [{"code": item[1], "name": item[2]} for item in data]
        except Exception:
            return []

    def finance_info(self, code: str) -> dict[str, Any]:
        """获取股票财务数据。"""
        self.ensure_connected()
        market = _detect_market(code)
        data = self._api.get_finance_info(market, code.zfill(6))
        if data is None:
            return {}
        return dict(data) if isinstance(data, dict) else {"raw": data}


# ------------------------------------------------------------------
# MCP Server
# ------------------------------------------------------------------

_client: TdxClient | None = None


def _get_client() -> TdxClient:
    global _client
    if _client is None:
        _client = TdxClient()
    return _client


def _envelope(success: bool, *, data: Any = None, error: str | None = None, tool: str) -> dict[str, Any]:
    return {
        "success": success,
        "data": sanitize_public_payload(data),
        "error": redact_public_text(error) if error is not None else None,
        "meta": {
            "source": "aiask_finance_mcp.tongdaxin",
            "tool": tool,
        },
    }


# Tool handlers

def handle_realtime_quote(arguments: dict[str, Any]) -> dict[str, Any]:
    """实时行情报价。"""
    codes = arguments.get("codes") or arguments.get("code")
    if isinstance(codes, str):
        codes = [c.strip() for c in codes.replace(",", " ").split() if c.strip()]
    if not codes:
        return _envelope(False, error="codes is required (e.g. '600519' or '600519,000001')", tool="tdx_realtime_quote")
    try:
        client = _get_client()
        data = client.realtime_quote(codes)
        return _envelope(True, data=data, tool="tdx_realtime_quote")
    except Exception as exc:
        return _envelope(False, error=str(exc), tool="tdx_realtime_quote")


def handle_kline_history(arguments: dict[str, Any]) -> dict[str, Any]:
    """历史 K 线数据。"""
    code = str(arguments.get("code") or "").strip()
    if not code:
        return _envelope(False, error="code is required", tool="tdx_kline_history")
    period = str(arguments.get("period") or "daily").strip()
    count = min(int(arguments.get("count") or 100), 800)
    start = int(arguments.get("start") or 0)
    try:
        client = _get_client()
        data = client.kline_history(code, period=period, count=count, start=start)
        return _envelope(True, data={"code": code, "period": period, "count": len(data), "bars": data}, tool="tdx_kline_history")
    except Exception as exc:
        return _envelope(False, error=str(exc), tool="tdx_kline_history")


def handle_minute_data(arguments: dict[str, Any]) -> dict[str, Any]:
    """当日分时数据。"""
    code = str(arguments.get("code") or "").strip()
    if not code:
        return _envelope(False, error="code is required", tool="tdx_minute_data")
    try:
        client = _get_client()
        data = client.minute_data(code)
        return _envelope(True, data={"code": code, "count": len(data), "minutes": data}, tool="tdx_minute_data")
    except Exception as exc:
        return _envelope(False, error=str(exc), tool="tdx_minute_data")


def handle_tick_data(arguments: dict[str, Any]) -> dict[str, Any]:
    """逐笔成交数据。"""
    code = str(arguments.get("code") or "").strip()
    if not code:
        return _envelope(False, error="code is required", tool="tdx_tick_data")
    count = min(int(arguments.get("count") or 100), 2000)
    start = int(arguments.get("start") or 0)
    try:
        client = _get_client()
        data = client.tick_data(code, count=count, start=start)
        return _envelope(True, data={"code": code, "count": len(data), "ticks": data}, tool="tdx_tick_data")
    except Exception as exc:
        return _envelope(False, error=str(exc), tool="tdx_tick_data")


def handle_finance_info(arguments: dict[str, Any]) -> dict[str, Any]:
    """股票财务数据。"""
    code = str(arguments.get("code") or "").strip()
    if not code:
        return _envelope(False, error="code is required", tool="tdx_finance_info")
    try:
        client = _get_client()
        data = client.finance_info(code)
        return _envelope(True, data={"code": code, "finance": data}, tool="tdx_finance_info")
    except Exception as exc:
        return _envelope(False, error=str(exc), tool="tdx_finance_info")


def handle_market_snapshot(arguments: dict[str, Any]) -> dict[str, Any]:
    """市场快照 — 获取多只股票的实时行情概览。"""
    codes = arguments.get("codes") or arguments.get("code") or ""
    if isinstance(codes, str):
        codes = [c.strip() for c in codes.replace(",", " ").split() if c.strip()]
    if not codes:
        # Default: 上证指数 + 深证成指 + 创业板指
        codes = ["999999", "399001", "399006"]
    try:
        client = _get_client()
        data = client.realtime_quote(codes)
        return _envelope(True, data={"count": len(data), "snapshot": data}, tool="tdx_market_snapshot")
    except Exception as exc:
        return _envelope(False, error=str(exc), tool="tdx_market_snapshot")


# ------------------------------------------------------------------
# MCP Protocol Implementation (stdio)
# ------------------------------------------------------------------

TOOL_DEFINITIONS = [
    {
        "name": "tdx_realtime_quote",
        "description": "获取股票实时行情报价（价格、涨跌幅、成交量、买卖盘）",
        "inputSchema": {
            "type": "object",
            "properties": {
                "codes": {
                    "type": "string",
                    "description": "股票代码，多个用逗号分隔。例如: '600519,000001,300750'",
                }
            },
            "required": ["codes"],
        },
    },
    {
        "name": "tdx_kline_history",
        "description": "获取股票历史 K 线数据（开高低收、成交量）",
        "inputSchema": {
            "type": "object",
            "properties": {
                "code": {"type": "string", "description": "股票代码，例如 '600519'"},
                "period": {
                    "type": "string",
                    "enum": ["1min", "5min", "15min", "30min", "1hour", "daily", "weekly", "monthly"],
                    "description": "K 线周期，默认 daily",
                },
                "count": {"type": "integer", "description": "获取条数，默认 100，最大 800"},
            },
            "required": ["code"],
        },
    },
    {
        "name": "tdx_minute_data",
        "description": "获取股票当日分时数据（价格、成交量逐分钟）",
        "inputSchema": {
            "type": "object",
            "properties": {
                "code": {"type": "string", "description": "股票代码"},
            },
            "required": ["code"],
        },
    },
    {
        "name": "tdx_tick_data",
        "description": "获取股票逐笔成交数据",
        "inputSchema": {
            "type": "object",
            "properties": {
                "code": {"type": "string", "description": "股票代码"},
                "count": {"type": "integer", "description": "获取条数，默认 100"},
            },
            "required": ["code"],
        },
    },
    {
        "name": "tdx_finance_info",
        "description": "获取股票财务数据（每股收益、净资产、营收等）",
        "inputSchema": {
            "type": "object",
            "properties": {
                "code": {"type": "string", "description": "股票代码"},
            },
            "required": ["code"],
        },
    },
    {
        "name": "tdx_market_snapshot",
        "description": "获取市场快照（默认返回上证指数、深证成指、创业板指）",
        "inputSchema": {
            "type": "object",
            "properties": {
                "codes": {
                    "type": "string",
                    "description": "股票/指数代码，多个用逗号分隔。留空则返回主要指数",
                }
            },
        },
    },
]

TOOL_HANDLERS = {
    "tdx_realtime_quote": handle_realtime_quote,
    "tdx_kline_history": handle_kline_history,
    "tdx_minute_data": handle_minute_data,
    "tdx_tick_data": handle_tick_data,
    "tdx_finance_info": handle_finance_info,
    "tdx_market_snapshot": handle_market_snapshot,
}


def _handle_jsonrpc(request: dict[str, Any]) -> dict[str, Any]:
    """处理 JSON-RPC 请求（MCP stdio 协议）。"""
    method = request.get("method", "")
    req_id = request.get("id")
    params = request.get("params") or {}

    if method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {
                    "tools": {"listChanged": False},
                    "resources": {"listChanged": False},
                    "prompts": {"listChanged": False},
                },
                "serverInfo": {
                    "name": "aiask-finance-tongdaxin",
                    "version": "0.1.0",
                },
            },
        }

    if method == "notifications/initialized":
        return None  # No response for notifications

    if method == "tools/list":
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {"tools": TOOL_DEFINITIONS},
        }

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
                "result": {
                    "content": [{"type": "text", "text": json.dumps({"error": redact_public_text(f"Unknown tool: {tool_name}")}, ensure_ascii=False)}],
                    "isError": True,
                },
            }
        result = handler(arguments)
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False, default=str)}],
                "isError": not result.get("success", False),
            },
        }

    # Unknown method
    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "error": {"code": -32601, "message": redact_public_text(f"Method not found: {method}")},
    }


def main() -> None:
    """MCP Server 入口 — stdio 传输。"""
    sys.stderr.write("aiask-finance-tongdaxin MCP server starting (stdio)...\n")
    sys.stderr.flush()

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except json.JSONDecodeError as _swallowed:
            logger.warning("packages/finance-mcp-servers/src/aiask_finance_mcp/tongdaxin/server.py:545: swallowed %s and continued: %s", type(_swallowed).__name__, _swallowed)
            continue

        response = _handle_jsonrpc(request)
        if response is not None:
            sys.stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
