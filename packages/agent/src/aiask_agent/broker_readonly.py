from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable
from uuid import uuid4

from .mcp_client import MCPAggregator
from .session_store import AgentSessionStore, now_iso, sanitize_for_audit
import logging

logger = logging.getLogger(__name__)


ToolCaller = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]

FATAL_READ_ONLY_SYNC_ERROR_CODES = frozenset(
    {
        "ACTION_INTENT_REQUIRED",
        "FINANCIAL_ACTION_REQUIRES_INTENT",
        "MCP_STATEFUL_ACTION_BLOCKED",
        "READ_ONLY_TOOL_RESULT_BLOCKED",
    }
)


BROKER_CONNECTORS: dict[str, dict[str, Any]] = {
    "qmt": {
        "provider": "qmt",
        "label": "QMT / MiniQMT",
        "required_env": ["QMT_PATH", "QMT_ACCOUNT"],
        "optional_env": ["QMT_ACCOUNT_TYPE", "QMT_SESSION_ID"],
        "required_tools": ["qmt_query_account", "qmt_query_position", "qmt_query_orders"],
        "environment_checks": [
            "Install and sign in to MiniQMT on the same Windows host as the Agent.",
            "Install the XtQuant SDK in the Agent Python environment.",
            "Set QMT_PATH and QMT_ACCOUNT in the Agent startup environment, then restart Agent.",
            "Register a financial MCP server exposing qmt_query_account, qmt_query_position, and qmt_query_orders.",
        ],
        "authorization_notes": [
            "Desktop only sends provider and explicit read-only consent to Agent HTTP.",
            "Account identifiers are hashed before snapshots are stored.",
            "Live order placement and cancellation remain disabled from this surface.",
        ],
        "test_entry": {
            "method": "POST",
            "path": "/v1/finance/broker/sync",
            "consent_required": True,
            "control_token_required": True,
        },
        "actions": [
            ("account", "broker-qmt", "account"),
            ("positions", "broker-qmt", "positions"),
            ("orders", "broker-qmt", "orders"),
        ],
    },
    "tonghuashun": {
        "provider": "tonghuashun",
        "aliases": ["ths"],
        "label": "Tonghuashun",
        "required_env": ["THS_CLIENT_PATH"],
        "optional_env": ["THS_TRADE_ACCOUNT", "THS_BROKER"],
        "required_tools": ["ths_query_balance", "ths_query_position", "ths_query_orders", "ths_query_deals"],
        "environment_checks": [
            "Install and sign in to the Tonghuashun desktop trading client on Windows.",
            "Install easytrader in the Agent Python environment.",
            "Set THS_CLIENT_PATH and any broker/account variables in the Agent startup environment, then restart Agent.",
            "Register a financial MCP server exposing ths_query_balance, ths_query_position, ths_query_orders, and ths_query_deals.",
        ],
        "authorization_notes": [
            "Desktop only sends provider and explicit read-only consent to Agent HTTP.",
            "Credentials stay in the Agent environment or OS secret store; Desktop does not persist them.",
            "Live order placement and cancellation remain disabled from this surface.",
        ],
        "test_entry": {
            "method": "POST",
            "path": "/v1/finance/broker/sync",
            "consent_required": True,
            "control_token_required": True,
        },
        "actions": [
            ("account", "broker-ths", "balance"),
            ("positions", "broker-ths", "positions"),
            ("orders", "broker-ths", "orders"),
            ("deals", "broker-ths", "deals"),
        ],
    },
}


def normalize_provider(value: Any) -> str:
    raw = str(value or "qmt").strip().lower().replace("-", "_")
    if raw in {"ths", "tonghuashun"}:
        return "tonghuashun"
    return raw or "qmt"


def _bool_env(name: str) -> bool:
    return bool(str(os.getenv(name, "")).strip())


def _safe_float(value: Any) -> float | None:
    if value is None or str(value).strip() == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError) as _swallowed:
        logger.warning("packages/agent/src/aiask_agent/broker_readonly.py:106: swallowed %s and continued: %s", type(_swallowed).__name__, _swallowed)
        return None


def _safe_int(value: Any) -> int | None:
    if value is None or str(value).strip() == "":
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError) as _swallowed:
        logger.warning("packages/agent/src/aiask_agent/broker_readonly.py:115: swallowed %s and continued: %s", type(_swallowed).__name__, _swallowed)
        return None


def _first_value(payload: dict[str, Any], *keys: str) -> Any:
    lowered = {str(key).lower(): value for key, value in payload.items()}
    for key in keys:
        for candidate in {key, key.lower(), key.upper()}:
            if candidate in payload and payload[candidate] not in (None, ""):
                return payload[candidate]
            if candidate.lower() in lowered and lowered[candidate.lower()] not in (None, ""):
                return lowered[candidate.lower()]
    return None


def _list_from_payload(payload: Any, *keys: str) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [dict(item) for item in payload if isinstance(item, dict)]
    if not isinstance(payload, dict):
        return []
    for key in keys:
        value = payload.get(key)
        if isinstance(value, list):
            return [dict(item) for item in value if isinstance(item, dict)]
    data = payload.get("data")
    if isinstance(data, dict):
        return _list_from_payload(data, *keys)
    if isinstance(data, list):
        return [dict(item) for item in data if isinstance(item, dict)]
    return []


def _dict_from_payload(payload: Any, *keys: str) -> dict[str, Any]:
    if isinstance(payload, dict):
        for key in keys:
            value = payload.get(key)
            if isinstance(value, dict):
                return dict(value)
        data = payload.get("data")
        if isinstance(data, dict):
            return dict(data)
        return dict(payload)
    return {}


def _hash_ref(provider: str, value: Any) -> str:
    import hashlib

    text = str(value or "").strip()
    if not text:
        return ""
    return hashlib.sha256(f"{provider}:{text}".encode("utf-8")).hexdigest()[:24]


def _fatal_read_only_sync_error(errors: list[dict[str, Any]]) -> dict[str, Any] | None:
    for item in errors:
        if str(item.get("error_code") or "") in FATAL_READ_ONLY_SYNC_ERROR_CODES:
            return item
    return None


BROKER_ACCOUNT_IDENTIFIER_KEYS = {
    "account",
    "account_id",
    "account_no",
    "client_id",
    "fund_account",
    "fund_account_id",
    "shareholder_account",
    "stock_account",
}
BROKER_ORDER_IDENTIFIER_KEYS = {
    "business_id",
    "contract_no",
    "entrust_id",
    "entrust_no",
    "order_id",
    "order_no",
}
BROKER_DEAL_IDENTIFIER_KEYS = {
    "business_no",
    "deal_id",
    "deal_no",
    "trade_id",
    "trade_no",
}


def _strip_broker_identifiers(
    value: Any,
    *,
    blocked_keys: set[str] | None = None,
    blocked_values: set[str] | None = None,
) -> Any:
    keys = {str(key or "").strip().lower() for key in (blocked_keys or set()) if str(key or "").strip()}
    values = {str(item or "").strip() for item in (blocked_values or set()) if str(item or "").strip()}
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in value.items():
            text_key = str(key or "").strip().lower()
            if text_key in keys:
                continue
            if not isinstance(item, (dict, list, tuple)) and str(item or "").strip() in values:
                continue
            result[key] = _strip_broker_identifiers(item, blocked_keys=keys, blocked_values=values)
        return result
    if isinstance(value, (list, tuple)):
        return [
            _strip_broker_identifiers(item, blocked_keys=keys, blocked_values=values)
            for item in value
            if isinstance(item, (dict, list, tuple)) or str(item or "").strip() not in values
        ]
    return value


def _strip_account_identifiers(payload: dict[str, Any]) -> dict[str, Any]:
    blocked = {"account", "account_id", "fund_account", "资金账号", "股东账号"}
    account_ref = _first_value(payload, "account_id", "account", "fund_account")
    return _strip_broker_identifiers(
        payload,
        blocked_keys=blocked | BROKER_ACCOUNT_IDENTIFIER_KEYS,
        blocked_values={str(account_ref or "").strip()},
    )


def normalize_account(provider: str, payload: dict[str, Any]) -> dict[str, Any]:
    total_asset = _safe_float(_first_value(payload, "total_asset", "asset", "totalAssets", "总资产", "资产总值"))
    cash = _safe_float(_first_value(payload, "cash_available", "cash", "available_cash", "enable_balance", "可用资金"))
    market_value = _safe_float(_first_value(payload, "market_value", "marketValue", "stock_value", "证券市值", "股票市值"))
    frozen_cash = _safe_float(_first_value(payload, "frozen_cash", "frozen", "冻结资金"))
    buying_power = _safe_float(_first_value(payload, "buying_power", "enable_balance", "可用资金"))
    account_ref = _first_value(payload, "account_id", "account", "fund_account", "资金账号", "股东账号")
    return {
        "provider": provider,
        "account_ref_hash": _hash_ref(provider, account_ref),
        "currency": str(_first_value(payload, "currency", "币种") or "CNY"),
        "total_asset": total_asset,
        "cash_available": cash,
        "market_value": market_value,
        "frozen_cash": frozen_cash,
        "buying_power": buying_power,
        "raw": sanitize_for_audit(_strip_account_identifiers(payload)),
    }


def normalize_position(provider: str, payload: dict[str, Any]) -> dict[str, Any]:
    quantity = _safe_float(_first_value(payload, "quantity", "volume", "持仓数量", "股票余额", "current_amount"))
    available = _safe_float(_first_value(payload, "available_quantity", "available", "可用数量", "可用余额", "enable_amount"))
    cost = _safe_float(_first_value(payload, "cost_basis", "cost_price", "成本价", "参考成本价"))
    price = _safe_float(_first_value(payload, "last_price", "price", "最新价", "市价"))
    market_value = _safe_float(_first_value(payload, "market_value", "marketValue", "市值", "参考市值"))
    pnl = _safe_float(_first_value(payload, "unrealized_pnl", "profit", "盈亏", "浮动盈亏"))
    return {
        "provider": provider,
        "symbol": str(_first_value(payload, "symbol", "code", "证券代码", "股票代码") or "").strip(),
        "exchange": str(_first_value(payload, "exchange", "market", "市场") or "").strip() or None,
        "name": str(_first_value(payload, "name", "stock_name", "证券名称", "股票名称") or "").strip() or None,
        "quantity": quantity,
        "available_quantity": available,
        "cost_basis": cost,
        "last_price": price,
        "market_value": market_value,
        "unrealized_pnl": pnl,
        "unrealized_pnl_pct": _safe_float(_first_value(payload, "unrealized_pnl_pct", "profit_rate", "盈亏比例")),
        "raw": sanitize_for_audit(
            _strip_broker_identifiers(payload, blocked_keys=BROKER_ACCOUNT_IDENTIFIER_KEYS)
        ),
    }


def normalize_order(provider: str, payload: dict[str, Any]) -> dict[str, Any]:
    order_ref = _first_value(payload, "order_id", "entrust_no", "委托编号", "合同编号")
    return {
        "provider": provider,
        "order_ref_hash": _hash_ref(provider, order_ref),
        "symbol": str(_first_value(payload, "symbol", "code", "证券代码", "股票代码") or "").strip(),
        "side": str(_first_value(payload, "side", "direction", "买卖方向", "委托方向") or "").strip().lower() or None,
        "order_type": str(_first_value(payload, "order_type", "type", "委托类型") or "").strip() or None,
        "price": _safe_float(_first_value(payload, "price", "委托价格", "entrust_price")),
        "quantity": _safe_float(_first_value(payload, "quantity", "amount", "volume", "委托数量", "entrust_amount")),
        "filled_quantity": _safe_float(_first_value(payload, "filled_quantity", "filled", "成交数量", "business_amount")),
        "status": str(_first_value(payload, "status", "委托状态", "order_status") or "unknown").strip(),
        "submitted_at": str(_first_value(payload, "submitted_at", "entrust_time", "委托时间") or "").strip() or None,
        "updated_at": str(_first_value(payload, "updated_at", "update_time") or "").strip() or None,
        "raw": sanitize_for_audit(
            _strip_broker_identifiers(
                payload,
                blocked_keys=BROKER_ACCOUNT_IDENTIFIER_KEYS | BROKER_ORDER_IDENTIFIER_KEYS,
                blocked_values={str(order_ref or "").strip()},
            )
        ),
    }


def normalize_deal(provider: str, payload: dict[str, Any]) -> dict[str, Any]:
    deal_ref = _first_value(payload, "deal_id", "business_no", "成交编号", "合同编号")
    order_ref = _first_value(payload, "order_id", "entrust_no", "委托编号")
    price = _safe_float(_first_value(payload, "price", "成交价格", "business_price"))
    quantity = _safe_float(_first_value(payload, "quantity", "volume", "成交数量", "business_amount"))
    return {
        "provider": provider,
        "deal_ref_hash": _hash_ref(provider, deal_ref),
        "order_ref_hash": _hash_ref(provider, order_ref),
        "symbol": str(_first_value(payload, "symbol", "code", "证券代码", "股票代码") or "").strip(),
        "side": str(_first_value(payload, "side", "direction", "买卖方向") or "").strip().lower() or None,
        "price": price,
        "quantity": quantity,
        "amount": _safe_float(_first_value(payload, "amount", "成交金额", "business_balance")) or ((price or 0) * (quantity or 0) if price and quantity else None),
        "fee": _safe_float(_first_value(payload, "fee", "commission", "手续费")),
        "occurred_at": str(_first_value(payload, "occurred_at", "business_time", "成交时间") or "").strip() or None,
        "raw": sanitize_for_audit(
            _strip_broker_identifiers(
                payload,
                blocked_keys=BROKER_ACCOUNT_IDENTIFIER_KEYS | BROKER_ORDER_IDENTIFIER_KEYS | BROKER_DEAL_IDENTIFIER_KEYS,
                blocked_values={str(deal_ref or "").strip(), str(order_ref or "").strip()},
            )
        ),
    }


def broker_readiness(store: AgentSessionStore | None = None) -> dict[str, Any]:
    mcp = MCPAggregator()
    tools = mcp.tools_summary(include_all=True)
    tool_names = {str(item.get("name") or "") for item in tools}
    servers = mcp.servers_summary(include_all=True)
    connectors = []
    for provider, config in BROKER_CONNECTORS.items():
        required_env = list(config.get("required_env") or [])
        missing_env = [name for name in required_env if not _bool_env(name)]
        required_tools = list(config.get("required_tools") or [])
        missing_tools = [name for name in required_tools if name not in tool_names]
        configured = not missing_env
        discovered = not missing_tools
        ready = configured and discovered
        connectors.append(
            {
                "provider": provider,
                "label": config.get("label"),
                "status": "ready" if ready else "missing_mcp_tool" if configured else "unconfigured",
                "configured": configured,
                "ready": ready,
                "read_only": True,
                "live_trading_enabled": False,
                "required_env": required_env,
                "missing_env": missing_env,
                "optional_env": list(config.get("optional_env") or []),
                "required_tools": required_tools,
                "missing_tools": missing_tools,
                "environment_checks": list(config.get("environment_checks") or []),
                "authorization_notes": list(config.get("authorization_notes") or []),
                "test_entry": dict(config.get("test_entry") or {}),
            }
        )
    latest = store.latest_broker_analytics() if store else None
    return {
        "object": "aiask.broker_readiness",
        "status": "ready" if any(item["ready"] for item in connectors) else "not_ready",
        "connectors": connectors,
        "mcp": {
            "registration": mcp.registration_diagnostics(),
            "servers": servers,
        },
        "latest_analytics": latest,
        "live_trading_enabled": False,
        "read_only": True,
        "secrets_redacted": True,
    }


@dataclass
class BrokerSyncContext:
    user_id: str
    session_id: str | None = None
    run_id: str | None = None
    trace_id: str | None = None
    source: str = "api"


async def sync_broker_readonly(
    store: AgentSessionStore,
    *,
    provider: str,
    context: BrokerSyncContext,
    tool_caller: ToolCaller,
    consent: bool,
) -> dict[str, Any]:
    normalized_provider = normalize_provider(provider)
    config = BROKER_CONNECTORS.get(normalized_provider)
    if not config:
        return _broker_payload(False, error=f"unsupported broker provider: {provider}", error_code="BROKER_PROVIDER_UNSUPPORTED")
    if not consent:
        return _broker_payload(False, error="broker read-only sync requires explicit user consent", error_code="BROKER_CONSENT_REQUIRED")
    readiness = broker_readiness(store)
    connector = next((item for item in readiness["connectors"] if item["provider"] == normalized_provider), None)
    if connector and not connector["ready"]:
        return _broker_payload(
            False,
            data={"readiness": connector},
            error="broker connector is not ready",
            error_code="BROKER_CONNECTOR_NOT_READY",
        )

    observed_at = now_iso()
    sync_id = f"broker_sync_{uuid4().hex}"
    outputs: dict[str, Any] = {}
    errors: list[dict[str, Any]] = []
    for key, capability_id, action_id in list(config.get("actions") or []):
        result = await tool_caller(
            "/v1/finance/manager/query",
            {
                "capability_id": capability_id,
                "action_id": action_id,
                "params": {},
                "user_id": context.user_id,
                "session_id": context.session_id,
                "run_id": context.run_id,
                "trace_id": context.trace_id,
            },
        )
        outputs[key] = result
        if not bool(result.get("success")):
            errors.append({"key": key, "error": result.get("error"), "error_code": result.get("error_code")})

    fatal_error = _fatal_read_only_sync_error(errors)
    if fatal_error:
        return _broker_payload(
            False,
            data={
                "sync_id": sync_id,
                "errors": errors,
                "counts": {"accounts": 0, "positions": 0, "orders": 0, "deals": 0},
            },
            error="broker read-only sync blocked a non-read-only tool result",
            error_code=str(fatal_error.get("error_code") or "BROKER_READ_ONLY_GUARD_FAILED"),
        )

    profile = store.upsert_broker_profile(
        user_id=context.user_id,
        provider=normalized_provider,
        display_name=str(config.get("label") or normalized_provider),
        read_only_enabled=True,
        consent_status="granted",
        metadata={"sync_id": sync_id, "source": context.source},
    )

    account_payload = _dict_from_payload((outputs.get("account") or {}).get("data"), "account", "balance", "asset")
    if account_payload:
        store.record_broker_account_snapshot(
            broker_profile_id=str(profile["broker_profile_id"]),
            user_id=context.user_id,
            provider=normalized_provider,
            account=normalize_account(normalized_provider, account_payload),
            source_tool=str((outputs.get("account") or {}).get("tool") or ""),
            source_run_id=context.run_id,
            observed_at=observed_at,
        )

    position_rows = _list_from_payload((outputs.get("positions") or {}).get("data"), "positions", "position")
    positions = [normalize_position(normalized_provider, item) for item in position_rows]
    store.record_broker_position_snapshots(
        broker_profile_id=str(profile["broker_profile_id"]),
        user_id=context.user_id,
        provider=normalized_provider,
        positions=positions,
        observed_at=observed_at,
    )

    order_rows = _list_from_payload((outputs.get("orders") or {}).get("data"), "orders", "entrusts")
    orders = [normalize_order(normalized_provider, item) for item in order_rows]
    store.record_broker_order_snapshots(
        broker_profile_id=str(profile["broker_profile_id"]),
        user_id=context.user_id,
        provider=normalized_provider,
        orders=orders,
        observed_at=observed_at,
    )

    deal_rows = _list_from_payload((outputs.get("deals") or {}).get("data"), "deals", "trades")
    deals = [normalize_deal(normalized_provider, item) for item in deal_rows]
    store.record_broker_deal_snapshots(
        broker_profile_id=str(profile["broker_profile_id"]),
        user_id=context.user_id,
        provider=normalized_provider,
        deals=deals,
        observed_at=observed_at,
    )

    analytics = run_behavior_analytics(
        store,
        broker_profile_id=str(profile["broker_profile_id"]),
        user_id=context.user_id,
        provider=normalized_provider,
    )
    profile = store.mark_broker_profile_synced(
        broker_profile_id=str(profile["broker_profile_id"]),
        status="ready" if not errors else "degraded",
        error_code=errors[0].get("error_code") if errors else None,
    )
    return _broker_payload(
        True,
        data={
            "sync_id": sync_id,
            "profile": profile,
            "counts": {
                "accounts": 1 if account_payload else 0,
                "positions": len(positions),
                "orders": len(orders),
                "deals": len(deals),
            },
            "errors": errors,
            "analytics": analytics,
        },
        error=None,
    )


def latest_broker_payload(store: AgentSessionStore, *, user_id: str | None = None, provider: str | None = None) -> dict[str, Any]:
    """Full read-only broker snapshot across every resource.

    Prefer the per-resource helpers below for resource-scoped endpoints: routing
    /positions and /orders through this function made three differently-named
    URLs return byte-identical bodies.
    """
    normalized_provider = normalize_provider(provider) if provider else None
    return _broker_payload(
        True,
        data={
            "profiles": store.list_broker_profiles(user_id=user_id, provider=normalized_provider),
            "accounts": store.list_broker_account_snapshots(user_id=user_id, provider=normalized_provider, limit=20),
            "positions": store.list_broker_position_snapshots(user_id=user_id, provider=normalized_provider, limit=200),
            "orders": store.list_broker_order_snapshots(user_id=user_id, provider=normalized_provider, limit=200),
            "deals": store.list_broker_deal_snapshots(user_id=user_id, provider=normalized_provider, limit=200),
            "analytics": store.latest_broker_analytics(user_id=user_id, provider=normalized_provider),
        },
        error=None,
    )


# Per-resource read-only views. Each returns only its own resource so that
# /accounts, /positions and /orders are actually distinguishable.
_BROKER_RESOURCE_READERS: dict[str, tuple[str, int]] = {
    "accounts": ("list_broker_account_snapshots", 20),
    "positions": ("list_broker_position_snapshots", 200),
    "orders": ("list_broker_order_snapshots", 200),
    "deals": ("list_broker_deal_snapshots", 200),
}


def broker_resource_payload(
    store: AgentSessionStore,
    resource: str,
    *,
    user_id: str | None = None,
    provider: str | None = None,
    limit: int | None = None,
) -> dict[str, Any]:
    """Return a single broker resource collection.

    ``resource`` is one of accounts / positions / orders / deals. An unknown
    resource is an error rather than a silent fallback to the full snapshot.
    """
    key = str(resource or "").strip().lower()
    reader = _BROKER_RESOURCE_READERS.get(key)
    if reader is None:
        return _broker_payload(
            False,
            error=f"unsupported broker resource: {resource}",
            error_code="BROKER_RESOURCE_UNSUPPORTED",
        )
    method_name, default_limit = reader
    normalized_provider = normalize_provider(provider) if provider else None
    effective_limit = default_limit if limit is None else max(1, min(int(limit), 1000))
    items = getattr(store, method_name)(
        user_id=user_id,
        provider=normalized_provider,
        limit=effective_limit,
    )
    payload = _broker_payload(True, data={key: items}, error=None)
    payload["object"] = f"aiask.broker_readonly.{key}"
    payload["meta"] = {
        "resource": key,
        "count": len(items),
        "limit": effective_limit,
        "user_id": user_id,
        "provider": normalized_provider,
        "degraded": False,
    }
    return payload


def run_behavior_analytics(
    store: AgentSessionStore,
    *,
    broker_profile_id: str | None = None,
    user_id: str | None = None,
    provider: str | None = None,
    period_start: str | None = None,
    period_end: str | None = None,
) -> dict[str, Any]:
    normalized_provider = normalize_provider(provider) if provider else None
    accounts = store.list_broker_account_snapshots(user_id=user_id, provider=normalized_provider, broker_profile_id=broker_profile_id, limit=20)
    positions = store.list_broker_position_snapshots(user_id=user_id, provider=normalized_provider, broker_profile_id=broker_profile_id, limit=500)
    orders = store.list_broker_order_snapshots(user_id=user_id, provider=normalized_provider, broker_profile_id=broker_profile_id, limit=500)
    deals = store.list_broker_deal_snapshots(user_id=user_id, provider=normalized_provider, broker_profile_id=broker_profile_id, limit=500)
    latest_account = accounts[0] if accounts else {}
    total_asset = _safe_float(latest_account.get("total_asset"))
    cash = _safe_float(latest_account.get("cash_available"))
    market_value = _safe_float(latest_account.get("market_value"))
    position_values = [(_safe_float(item.get("market_value")) or 0.0) for item in positions]
    total_position_value = sum(value for value in position_values if value > 0)
    denominator = market_value or total_position_value or total_asset or 0.0
    top_positions = sorted(positions, key=lambda item: _safe_float(item.get("market_value")) or 0.0, reverse=True)[:10]
    concentration = ((position_values[0] if position_values else 0.0) / denominator) if denominator else None
    cash_ratio = (cash / total_asset) if cash is not None and total_asset else None
    buy_count = sum(1 for item in [*orders, *deals] if str(item.get("side") or "").lower() in {"buy", "b", "买入"})
    sell_count = sum(1 for item in [*orders, *deals] if str(item.get("side") or "").lower() in {"sell", "s", "卖出"})
    trade_count = len(orders) + len(deals)
    realized_amount = sum((_safe_float(item.get("amount")) or 0.0) for item in deals)
    metrics = {
        "account_count": len(accounts),
        "position_count": len(positions),
        "order_count": len(orders),
        "deal_count": len(deals),
        "total_asset": total_asset,
        "cash_available": cash,
        "market_value": market_value,
        "cash_ratio": cash_ratio,
        "top_position_concentration": concentration,
        "top_positions": [
            {
                "symbol": item.get("symbol"),
                "name": item.get("name"),
                "market_value": item.get("market_value"),
                "position_pct": ((_safe_float(item.get("market_value")) or 0.0) / denominator) if denominator else None,
            }
            for item in top_positions
        ],
        "trade_count": trade_count,
        "buy_count": buy_count,
        "sell_count": sell_count,
        "buy_sell_imbalance": (buy_count - sell_count) / trade_count if trade_count else None,
        "deal_amount_total": realized_amount if deals else None,
    }
    risk_flags = []
    if concentration is not None and concentration >= 0.45:
        risk_flags.append({"code": "HIGH_SINGLE_POSITION_CONCENTRATION", "severity": "warning", "value": concentration})
    if cash_ratio is not None and cash_ratio <= 0.05:
        risk_flags.append({"code": "LOW_CASH_BUFFER", "severity": "info", "value": cash_ratio})
    if trade_count >= 20:
        risk_flags.append({"code": "HIGH_TRADE_FREQUENCY", "severity": "info", "value": trade_count})
    limitations = []
    if not deals:
        limitations.append("deal data is missing; realized win/loss and holding-period metrics are limited")
    if len(accounts) < 2:
        limitations.append("historical account snapshots are insufficient for drawdown analytics")
    source_ids = {
        "accounts": [item.get("snapshot_id") for item in accounts[:20]],
        "positions": [item.get("snapshot_id") for item in positions[:200]],
        "orders": [item.get("snapshot_id") for item in orders[:200]],
        "deals": [item.get("snapshot_id") for item in deals[:200]],
    }
    result = store.record_broker_behavior_analytics(
        broker_profile_id=broker_profile_id or str((latest_account or {}).get("broker_profile_id") or ""),
        user_id=user_id or str((latest_account or {}).get("user_id") or "local"),
        provider=normalized_provider or str((latest_account or {}).get("provider") or "unknown"),
        period_start=period_start,
        period_end=period_end,
        metrics=metrics,
        signals={"limitations": limitations, "generated_at": now_iso()},
        risk_flags=risk_flags,
        source_snapshot_ids=source_ids,
        model_version="deterministic-p0",
    )
    return result


def _broker_payload(
    success: bool,
    *,
    data: Any = None,
    error: str | None = None,
    error_code: str | None = None,
) -> dict[str, Any]:
    return {
        "object": "aiask.broker_readonly",
        "success": bool(success),
        "data": data,
        "error": error,
        "error_code": error_code,
        "read_only": True,
        "live_trading_enabled": False,
        "secrets_redacted": True,
        "source_chain": ["aiask_agent.broker_readonly"],
        "generated_at": int(time.time()),
    }
