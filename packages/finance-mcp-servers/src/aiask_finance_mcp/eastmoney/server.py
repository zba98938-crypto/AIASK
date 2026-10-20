"""东方财富 MCP Server — 基于 efinance 的金融数据服务。

提供工具：
- em_realtime_quote: 实时行情
- em_kline_history: 历史 K 线
- em_fund_info: 基金信息
- em_fund_nav: 基金净值
- em_bond_data: 债券数据
- em_futures_data: 期货数据
- em_news_flow: 财经新闻流
- em_dragon_tiger_list: 龙虎榜数据

环境变量：
    EM_API_TOKEN: 东方财富 API Token（可选，部分接口需要）
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any

from .._shared.redaction import redact_public_text, sanitize_public_payload
import logging

logger = logging.getLogger(__name__)


def _envelope(success: bool, *, data: Any = None, error: str | None = None, tool: str) -> dict[str, Any]:
    return {
        "success": success,
        "data": sanitize_public_payload(data),
        "error": redact_public_text(error) if error is not None else None,
        "meta": {"source": "aiask_finance_mcp.eastmoney", "tool": tool, "side_effect": "read_only"},
    }


def _import_efinance():
    try:
        import efinance as ef
        return ef
    except ImportError:
        raise RuntimeError("efinance is not installed. Run: pip install efinance")


# ------------------------------------------------------------------
# Tool handlers
# ------------------------------------------------------------------


def handle_realtime_quote(arguments: dict[str, Any]) -> dict[str, Any]:
    """实时行情报价。"""
    codes = arguments.get("codes") or arguments.get("code") or ""
    if isinstance(codes, str):
        codes = [c.strip() for c in codes.replace(",", " ").split() if c.strip()]
    if not codes:
        return _envelope(False, error="codes is required", tool="em_realtime_quote")
    try:
        ef = _import_efinance()
        df = ef.stock.get_quote_history(codes, klt=1)
        if df is None or (hasattr(df, "empty") and df.empty):
            # Fallback: try individual quotes
            results = []
            failed_codes: list[str] = []
            for code in codes:
                try:
                    quote_df = ef.stock.get_realtime_quotes([code])
                    if quote_df is not None and not quote_df.empty:
                        results.extend(quote_df.to_dict("records"))
                except Exception as exc:
                    logger.warning("em_realtime_quote: realtime source failed for %s: %s: %s", code, type(exc).__name__, exc)
                    failed_codes.append(code)
            if not results and failed_codes:
                # Every realtime source failed: this is "no data because the
                # source is unreachable", not "the market has no quotes".
                envelope = _envelope(False, tool="em_realtime_quote")
                envelope["error"] = "realtime_source_unavailable"
                envelope["meta"]["reason"] = "realtime_source_unavailable"
                envelope["meta"]["failed_codes"] = failed_codes
                return envelope
            return _envelope(
                True,
                data={"quotes": results, "count": len(results), "failed_codes": failed_codes},
                tool="em_realtime_quote",
            )
        records = df.to_dict("records") if hasattr(df, "to_dict") else []
        return _envelope(True, data={"quotes": records[:50], "count": len(records)}, tool="em_realtime_quote")
    except Exception as exc:
        return _envelope(False, error=str(exc), tool="em_realtime_quote")


def handle_kline_history(arguments: dict[str, Any]) -> dict[str, Any]:
    """历史 K 线数据。"""
    code = str(arguments.get("code") or "").strip()
    if not code:
        return _envelope(False, error="code is required", tool="em_kline_history")
    period = str(arguments.get("period") or "daily").strip()
    klt_map = {"1min": 1, "5min": 5, "15min": 15, "30min": 30, "60min": 60, "daily": 101, "weekly": 102, "monthly": 103}
    klt = klt_map.get(period, 101)
    limit = min(int(arguments.get("limit") or 100), 500)
    try:
        ef = _import_efinance()
        df = ef.stock.get_quote_history(code, klt=klt)
        if df is None or (hasattr(df, "empty") and df.empty):
            return _envelope(True, data={"code": code, "bars": [], "count": 0}, tool="em_kline_history")
        records = df.tail(limit).to_dict("records") if hasattr(df, "to_dict") else []
        return _envelope(True, data={"code": code, "period": period, "bars": records, "count": len(records)}, tool="em_kline_history")
    except Exception as exc:
        return _envelope(False, error=str(exc), tool="em_kline_history")


def handle_fund_info(arguments: dict[str, Any]) -> dict[str, Any]:
    """基金基本信息。"""
    code = str(arguments.get("code") or "").strip()
    if not code:
        return _envelope(False, error="code is required", tool="em_fund_info")
    try:
        ef = _import_efinance()
        df = ef.fund.get_base_info([code])
        if df is None or (hasattr(df, "empty") and df.empty):
            return _envelope(True, data={"code": code, "info": {}}, tool="em_fund_info")
        records = df.to_dict("records") if hasattr(df, "to_dict") else []
        return _envelope(True, data={"code": code, "info": records[0] if records else {}}, tool="em_fund_info")
    except Exception as exc:
        return _envelope(False, error=str(exc), tool="em_fund_info")


def handle_fund_nav(arguments: dict[str, Any]) -> dict[str, Any]:
    """基金净值历史。"""
    code = str(arguments.get("code") or "").strip()
    if not code:
        return _envelope(False, error="code is required", tool="em_fund_nav")
    limit = min(int(arguments.get("limit") or 30), 200)
    try:
        ef = _import_efinance()
        df = ef.fund.get_quote_history(code)
        if df is None or (hasattr(df, "empty") and df.empty):
            return _envelope(True, data={"code": code, "nav": [], "count": 0}, tool="em_fund_nav")
        records = df.tail(limit).to_dict("records") if hasattr(df, "to_dict") else []
        return _envelope(True, data={"code": code, "nav": records, "count": len(records)}, tool="em_fund_nav")
    except Exception as exc:
        return _envelope(False, error=str(exc), tool="em_fund_nav")


def handle_bond_data(arguments: dict[str, Any]) -> dict[str, Any]:
    """可转债数据。"""
    try:
        ef = _import_efinance()
        df = ef.bond.get_all_base_info()
        if df is None or (hasattr(df, "empty") and df.empty):
            return _envelope(True, data={"bonds": [], "count": 0}, tool="em_bond_data")
        limit = min(int(arguments.get("limit") or 50), 200)
        records = df.head(limit).to_dict("records") if hasattr(df, "to_dict") else []
        return _envelope(True, data={"bonds": records, "count": len(records)}, tool="em_bond_data")
    except Exception as exc:
        return _envelope(False, error=str(exc), tool="em_bond_data")


def handle_futures_data(arguments: dict[str, Any]) -> dict[str, Any]:
    """期货行情数据。"""
    code = str(arguments.get("code") or "").strip()
    if not code:
        return _envelope(False, error="code is required (e.g. 'IF2406')", tool="em_futures_data")
    try:
        ef = _import_efinance()
        df = ef.futures.get_quote_history(code)
        if df is None or (hasattr(df, "empty") and df.empty):
            return _envelope(True, data={"code": code, "bars": [], "count": 0}, tool="em_futures_data")
        limit = min(int(arguments.get("limit") or 50), 200)
        records = df.tail(limit).to_dict("records") if hasattr(df, "to_dict") else []
        return _envelope(True, data={"code": code, "bars": records, "count": len(records)}, tool="em_futures_data")
    except Exception as exc:
        return _envelope(False, error=str(exc), tool="em_futures_data")


def handle_news_flow(arguments: dict[str, Any]) -> dict[str, Any]:
    """财经新闻流。"""
    try:
        ef = _import_efinance()
        limit = min(int(arguments.get("limit") or 20), 50)
        df = ef.stock.get_latest_news(limit)
        if df is None or (hasattr(df, "empty") and df.empty):
            return _envelope(True, data={"news": [], "count": 0}, tool="em_news_flow")
        records = df.to_dict("records") if hasattr(df, "to_dict") else []
        return _envelope(True, data={"news": records, "count": len(records)}, tool="em_news_flow")
    except Exception as exc:
        return _envelope(False, error=str(exc), tool="em_news_flow")


def handle_dragon_tiger(arguments: dict[str, Any]) -> dict[str, Any]:
    """龙虎榜数据。"""
    try:
        ef = _import_efinance()
        date = str(arguments.get("date") or "").strip()
        if date:
            df = ef.stock.get_daily_billboard(date)
        else:
            df = ef.stock.get_daily_billboard()
        if df is None or (hasattr(df, "empty") and df.empty):
            return _envelope(True, data={"items": [], "count": 0}, tool="em_dragon_tiger_list")
        limit = min(int(arguments.get("limit") or 30), 100)
        records = df.head(limit).to_dict("records") if hasattr(df, "to_dict") else []
        return _envelope(True, data={"items": records, "count": len(records), "date": date or "latest"}, tool="em_dragon_tiger_list")
    except Exception as exc:
        return _envelope(False, error=str(exc), tool="em_dragon_tiger_list")


# ------------------------------------------------------------------
# MCP Protocol
# ------------------------------------------------------------------

TOOL_DEFINITIONS = [
    {"name": "em_realtime_quote", "description": "东方财富实时行情报价", "inputSchema": {"type": "object", "properties": {"codes": {"type": "string", "description": "股票代码，多个用逗号分隔"}}, "required": ["codes"]}},
    {"name": "em_kline_history", "description": "东方财富历史 K 线数据", "inputSchema": {"type": "object", "properties": {"code": {"type": "string"}, "period": {"type": "string", "enum": ["1min", "5min", "15min", "30min", "60min", "daily", "weekly", "monthly"]}, "limit": {"type": "integer"}}, "required": ["code"]}},
    {"name": "em_fund_info", "description": "基金基本信息", "inputSchema": {"type": "object", "properties": {"code": {"type": "string", "description": "基金代码"}}, "required": ["code"]}},
    {"name": "em_fund_nav", "description": "基金净值历史", "inputSchema": {"type": "object", "properties": {"code": {"type": "string"}, "limit": {"type": "integer"}}, "required": ["code"]}},
    {"name": "em_bond_data", "description": "可转债数据列表", "inputSchema": {"type": "object", "properties": {"limit": {"type": "integer"}}}},
    {"name": "em_futures_data", "description": "期货行情数据", "inputSchema": {"type": "object", "properties": {"code": {"type": "string", "description": "期货合约代码"}, "limit": {"type": "integer"}}, "required": ["code"]}},
    {"name": "em_news_flow", "description": "东方财富财经新闻流", "inputSchema": {"type": "object", "properties": {"limit": {"type": "integer", "description": "新闻条数，默认20"}}}},
    {"name": "em_dragon_tiger_list", "description": "龙虎榜数据", "inputSchema": {"type": "object", "properties": {"date": {"type": "string", "description": "日期 YYYY-MM-DD，留空为最新"}, "limit": {"type": "integer"}}}},
]

TOOL_HANDLERS = {
    "em_realtime_quote": handle_realtime_quote,
    "em_kline_history": handle_kline_history,
    "em_fund_info": handle_fund_info,
    "em_fund_nav": handle_fund_nav,
    "em_bond_data": handle_bond_data,
    "em_futures_data": handle_futures_data,
    "em_news_flow": handle_news_flow,
    "em_dragon_tiger_list": handle_dragon_tiger,
}


def _handle_jsonrpc(request: dict[str, Any]) -> dict[str, Any] | None:
    method = request.get("method", "")
    req_id = request.get("id")
    params = request.get("params") or {}

    if method == "initialize":
        return {"jsonrpc": "2.0", "id": req_id, "result": {"protocolVersion": "2024-11-05", "capabilities": {"tools": {"listChanged": False}, "resources": {"listChanged": False}, "prompts": {"listChanged": False}}, "serverInfo": {"name": "aiask-finance-eastmoney", "version": "0.1.0"}}}
    if method == "notifications/initialized":
        return None
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": req_id, "result": {"tools": TOOL_DEFINITIONS}}
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
    sys.stderr.write("aiask-finance-eastmoney MCP server starting (stdio)...\n")
    sys.stderr.flush()
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except json.JSONDecodeError as _swallowed:
            logger.warning("packages/finance-mcp-servers/src/aiask_finance_mcp/eastmoney/server.py:255: swallowed %s and continued: %s", type(_swallowed).__name__, _swallowed)
            continue
        response = _handle_jsonrpc(request)
        if response is not None:
            sys.stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
