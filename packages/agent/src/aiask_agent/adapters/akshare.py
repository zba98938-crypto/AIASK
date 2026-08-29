from __future__ import annotations

import inspect
from collections.abc import Callable
from importlib import import_module
from typing import Any

STOCK_ANALYSIS_DISCLAIMER = (
    "NOT_INVESTMENT_ADVICE: This stock analysis is decision support only "
    "and is not a trading instruction."
)
STOCK_ANALYSIS_FRESHNESS_EXPECTATION = "intraday_quote_and_recent_fundamental_context"
STOCK_ANALYSIS_LIMITATIONS = (
    "Freshness depends on upstream quote, kline, financial, and decision-context providers returned by AKShare MCP.",
    "Decision context is omitted by default; when explicitly requested, it remains advisory context only.",
    "No live trading, order placement, cancellation, or portfolio mutation is authorized by this read-only analysis.",
)


class _CaptureMCP:
    def __init__(self) -> None:
        self.tools: dict[str, Callable[..., Any]] = {}

    def tool(self, *args: Any, **kwargs: Any) -> Callable[..., Any]:
        if args and callable(args[0]) and len(args) == 1 and not kwargs:
            fn = args[0]
            self.tools[fn.__name__] = fn
            return fn

        def _decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
            self.tools[fn.__name__] = fn
            return fn

        return _decorator


def load_registered_tool(module_name: str, function_name: str) -> Callable[..., Any]:
    module = import_module(module_name)
    register = getattr(module, "register", None)
    if not callable(register):
        raise RuntimeError(f"{module_name} does not expose register(mcp)")
    probe = _CaptureMCP()
    register(probe)
    fn = probe.tools.get(function_name)
    if not callable(fn):
        raise RuntimeError(f"{module_name} did not register {function_name}")
    return fn


def filter_arguments(fn: Callable[..., Any], arguments: dict[str, Any]) -> dict[str, Any]:
    signature = inspect.signature(fn)
    if any(param.kind == inspect.Parameter.VAR_KEYWORD for param in signature.parameters.values()):
        return dict(arguments)
    return {key: value for key, value in arguments.items() if key in signature.parameters}


async def call_function(fn: Callable[..., Any], arguments: dict[str, Any]) -> Any:
    result = fn(**filter_arguments(fn, arguments))
    if inspect.isawaitable(result):
        return await result
    return result


def _dedupe_text(items: list[Any]) -> list[str]:
    values: list[str] = []
    for item in items:
        text = str(item or "").strip()
        if text and text not in values:
            values.append(text)
    return values


def _dict_or_empty(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _text_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple, set)):
        return list(value)
    return [value]


def _bool_value(value: Any, *, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "y", "on", "enabled"}:
            return True
        if normalized in {"0", "false", "no", "n", "off", "disabled", ""}:
            return False
    return bool(value)


def _source_chain(upstream: dict[str, Any], meta: dict[str, Any], data: Any) -> list[str]:
    data_chain = _dict_or_empty(data).get("source_chain")
    return _dedupe_text(
        [
            "aiask_agent.adapters.akshare",
            *_text_list(meta.get("source_chain")),
            *_text_list(upstream.get("source_chain")),
            *_text_list(data_chain),
            "akshare_mcp.tools.ai_workflows.analyze_stock_workflow",
        ]
    )


def _stock_analysis_quality_gate(*, success: bool, degraded: bool, quality: dict[str, Any]) -> dict[str, Any]:
    status_text = str(quality.get("status") or "").strip().lower()
    blocked = not success
    unknown = status_text in {"", "unknown"}
    partial = degraded or status_text in {"partial", "partial_failed", "failed", "invalid_params", "degraded"}
    status = "blocked" if blocked else "unknown" if unknown else "degraded" if partial else "passed"
    return {
        "mode": "report_only",
        "status": status,
        "minimum_quality_passed": status == "passed",
        "blocking": False,
        "source": "akshare_mcp.analyze_stock_workflow",
        "failed_steps": list(quality.get("failed_steps") or []),
    }


def _normalize_stock_analysis_result(result: Any, payload: dict[str, Any]) -> dict[str, Any]:
    upstream = dict(result) if isinstance(result, dict) else {"success": True, "data": result, "error": None}
    is_envelope = {"success", "data", "error"}.issubset(upstream)
    success = upstream.get("success") is not False if is_envelope else True
    data = upstream.get("data") if is_envelope else result
    error = upstream.get("error") if is_envelope else None
    meta = _dict_or_empty(upstream.get("meta"))
    quality = _dict_or_empty(meta.get("quality") or upstream.get("quality") or _dict_or_empty(data).get("quality"))
    pit = _dict_or_empty(meta.get("pit") or upstream.get("pit") or _dict_or_empty(data).get("pit"))
    degraded = bool(meta.get("degraded") or upstream.get("degraded") or quality.get("degraded"))
    chain = _source_chain(upstream, meta, data)
    code = str(payload.get("code") or payload.get("symbol") or "").strip()

    if isinstance(data, dict):
        analysis_data = dict(data)
    else:
        analysis_data = {"result": data}
    analysis_data.setdefault("code", code)
    analysis_data["disclaimer"] = STOCK_ANALYSIS_DISCLAIMER
    analysis_data["limitations"] = list(STOCK_ANALYSIS_LIMITATIONS)
    analysis_data["freshness"] = {
        "expectation": STOCK_ANALYSIS_FRESHNESS_EXPECTATION,
        "status": "reported_by_source" if (pit or meta.get("data_timestamp") or upstream.get("data_timestamp")) else "unknown",
        "as_of": payload.get("as_of") or pit.get("as_of") or pit.get("cutoff"),
        "pit": pit,
        "source_chain": chain,
    }
    analysis_data["data_quality"] = quality or {
        "status": "unknown",
        "workflow": "analyze_stock_workflow",
    }
    analysis_data["quality_gate"] = _stock_analysis_quality_gate(
        success=success,
        degraded=degraded,
        quality=analysis_data["data_quality"],
    )
    analysis_data["decision_policy"] = {
        "default_include_decision": False,
        "include_decision_requested": bool(payload.get("include_decision")),
        "decision_context_allowed": bool(payload.get("include_decision")),
        "trade_instruction_allowed": False,
        "live_trading_allowed": False,
        "stateful_actions_require_intent": True,
        "execution": "read_only_analysis_only",
    }
    analysis_data["source_chain"] = chain

    upstream_side_effect = _dict_or_empty(meta.get("side_effect"))
    meta["side_effect"] = {
        "level": "read_only",
        "target": code or "agent_analyze_stock",
        "confirmation_required": False,
        "idempotent": True,
        "upstream_level": upstream_side_effect.get("level") if upstream_side_effect else None,
    }
    meta["source_chain"] = chain
    meta.setdefault("quality", analysis_data["data_quality"])
    meta.setdefault("pit", pit)

    envelope = {
        "success": bool(success),
        "data": analysis_data,
        "error": error,
        "meta": meta,
    }
    if upstream.get("error_code"):
        envelope["error_code"] = upstream.get("error_code")
    return envelope


async def analyze_stock(arguments: dict[str, Any]) -> dict[str, Any]:
    payload = dict(arguments or {})
    if payload.get("symbol") and not payload.get("code"):
        payload["code"] = payload["symbol"]
    payload["include_decision"] = _bool_value(payload.get("include_decision"), default=False)
    fn = load_registered_tool("akshare_mcp.tools.ai_workflows", "analyze_stock_workflow")
    result = await call_function(fn, payload)
    return _normalize_stock_analysis_result(result, payload)


async def stock_live_quote(arguments: dict[str, Any]) -> dict[str, Any]:
    payload = dict(arguments or {})
    if payload.get("symbol") and not payload.get("code"):
        payload["code"] = payload["symbol"]
    if payload.get("stock_code") and not payload.get("code"):
        payload["code"] = payload["stock_code"]
    if payload.get("ticker") and not payload.get("code"):
        payload["code"] = payload["ticker"]
    fn = getattr(import_module("akshare_mcp.tools.market.quote"), "get_realtime_quote")
    return await call_function(fn, payload)


async def stock_news_digest(arguments: dict[str, Any]) -> dict[str, Any]:
    payload = dict(arguments or {})
    if payload.get("symbol") and not payload.get("code"):
        payload["code"] = payload["symbol"]
    if payload.get("stock_code") and not payload.get("code"):
        payload["code"] = payload["stock_code"]
    if payload.get("ticker") and not payload.get("code"):
        payload["code"] = payload["ticker"]
    module = import_module("akshare_mcp.tools.news.news_feed")
    code = str(payload.get("code") or "").strip()
    fn = getattr(module, "get_stock_news" if code else "get_market_news")
    return await call_function(fn, payload)


async def governance_check(arguments: dict[str, Any]) -> dict[str, Any]:
    module = import_module("akshare_mcp.tools.governance_workflow")
    fn = getattr(module, "governance_check_workflow")
    return await call_function(fn, dict(arguments or {}))


async def data_validation(arguments: dict[str, Any]) -> dict[str, Any]:
    fn = load_registered_tool("akshare_mcp.tools.adapter_tools", "data_validation")
    return await call_function(fn, dict(arguments or {}))


async def market_temperature_snapshot(arguments: dict[str, Any]) -> dict[str, Any]:
    fn = load_registered_tool("akshare_mcp.tools.market_temperature", "get_market_temperature_snapshot")
    return await call_function(fn, dict(arguments or {}))


async def market_temperature_cache_readiness(arguments: dict[str, Any]) -> dict[str, Any]:
    fn = load_registered_tool("akshare_mcp.tools.db_freshness", "check_market_temperature_cache_readiness")
    return await call_function(fn, dict(arguments or {}))


async def market_temperature_cache_history(arguments: dict[str, Any]) -> dict[str, Any]:
    fn = load_registered_tool("akshare_mcp.tools.market_temperature", "list_market_temperature_snapshot_cache")
    return await call_function(fn, dict(arguments or {}))


async def market_temperature_industry_history(arguments: dict[str, Any]) -> dict[str, Any]:
    fn = load_registered_tool("akshare_mcp.tools.market_temperature", "list_market_temperature_industry_history")
    return await call_function(fn, dict(arguments or {}))


async def market_temperature_industry_constituents(arguments: dict[str, Any]) -> dict[str, Any]:
    fn = load_registered_tool("akshare_mcp.tools.market_temperature", "list_market_temperature_industry_constituents")
    return await call_function(fn, dict(arguments or {}))


async def market_temperature_forward_validation(arguments: dict[str, Any]) -> dict[str, Any]:
    fn = load_registered_tool("akshare_mcp.tools.market_temperature", "get_market_temperature_forward_validation")
    return await call_function(fn, dict(arguments or {}))
