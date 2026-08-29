from __future__ import annotations

from typing import Any, Iterable

from .tool_risk import looks_like_live_broker_mcp_tool


CONNECTOR_LIVE_TRADING_POLICY = "blocked_in_agent_connector_surface"

_READ_ONLY_TOOL_TOKENS = (
    "query",
    "quote",
    "kline",
    "minute",
    "tick",
    "history",
    "finance_info",
    "market_snapshot",
    "snapshot",
    "balance",
    "position",
    "positions",
    "orders",
    "deals",
    "account",
    "stock_data",
    "fund",
    "bond",
    "futures",
    "news",
    "dragon_tiger",
    "nav",
)


def _unique_strings(values: Iterable[Any]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = str(value or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result


def _looks_read_only_tool(tool_name: str) -> bool:
    normalized = str(tool_name or "").strip().lower().replace("-", "_")
    return bool(normalized) and any(token in normalized for token in _READ_ONLY_TOOL_TOKENS)


def split_financial_connector_tools(
    *,
    tools_read: Iterable[Any] = (),
    tools_trade: Iterable[Any] = (),
) -> dict[str, list[str]]:
    read_tools = _unique_strings(tools_read)
    blocked_trade_tools: list[str] = []

    for tool_name in _unique_strings(tools_trade):
        if looks_like_live_broker_mcp_tool(tool_name):
            blocked_trade_tools.append(tool_name)
        elif _looks_read_only_tool(tool_name):
            read_tools.append(tool_name)
        else:
            blocked_trade_tools.append(tool_name)

    return {
        "tools_read": _unique_strings(read_tools),
        "tools_trade_blocked": _unique_strings(blocked_trade_tools),
    }


def financial_connector_safety_metadata(
    *,
    tools_read: Iterable[Any] = (),
    tools_trade: Iterable[Any] = (),
) -> dict[str, Any]:
    split = split_financial_connector_tools(tools_read=tools_read, tools_trade=tools_trade)
    blocked_tools = split["tools_trade_blocked"]
    return {
        "tools_read": split["tools_read"],
        "tools_trade": [],
        "tools_trade_executable": [],
        "tools_trade_blocked": blocked_tools,
        "trade_risk_tools_blocked": blocked_tools,
        "live_trading_enabled": False,
        "live_trading_blocked": bool(blocked_tools),
        "live_trading_policy": CONNECTOR_LIVE_TRADING_POLICY,
        "trade_direct_execution_allowed": False,
        "explicit_token_required": bool(blocked_tools),
        "broker_token_required": bool(blocked_tools),
        "side_effect_policy": {
            "read_only_tools_level": "read_only",
            "blocked_tools_level": "trade_risk" if blocked_tools else "none",
        },
    }


def safe_financial_connector_capabilities(capabilities: dict[str, Any] | None) -> dict[str, Any]:
    payload = dict(capabilities or {})
    for key in (
        "place_order",
        "submit_order",
        "send_order",
        "new_order",
        "order_insert",
        "order_submit",
        "cancel_order",
        "order_cancel",
        "live_order",
        "live_trade",
        "live_trading",
    ):
        if key in payload:
            payload[key] = False
    payload.setdefault("live_trading", False)
    return payload
